# Parser-replacement spike (issue #69 Option B)

This directory is **not part of the runtime SDK**. It exists so the
three candidate markdown libraries can be benchmarked and diffed
against the existing hand-rolled `shared/markdown_parser.py` in a
controlled way before any production code is touched.

## How to run

```bash
# Install spike dev deps (one-off)
uv sync --group dev --group spike-parser

# Diff candidate mdast trees against the baseline
uv run pytest tests/parser_spike/test_mdast_parity.py -s

# Run the benchmark + LOC report
uv run python scripts/parser_spike/benchmark.py
```

## Current results (sample run, local machine)

Numbers will vary on CI hardware but the **relative ordering is stable**
across runs.

### Parse-and-translate time (12KB mixed corpus, 50 iterations)

| parser            | median  | p95     | meets 5ms budget? |
|-------------------|--------:|--------:|-------------------|
| baseline (hand)   |  2.59ms |  2.72ms | ✓                 |
| mistune           | 11.94ms | 13.04ms | ✗ (2.4× over)     |
| markdown-it-py    | 13.36ms | 20.64ms | ✗ (2.7× over)     |
| marko             | 46.62ms | 49.58ms | ✗ (9.3× over)     |

The baseline is **~5× faster** than mistune and markdown-it-py and
**~18× faster** than marko. The 5ms acceptance criterion from issue #69
is met by the baseline alone.

### Translator LOC (excluding blank lines + line comments)

| library         | LOC | 250-LOC budget |
|-----------------|----:|----------------|
| mistune         | 161 | ✓               |
| markdown-it-py  | 215 | ✓               |
| marko           | 152 | ✓               |

All three fit comfortably. mistune and marko both come in under 165
lines for the translator layer.

### mdast fidelity on the happy path (`mixed_content.md`)

Tested against a ≈3KB corpus of headings, tables, code blocks, lists,
links, images, blockquotes, emphasis — constructs the baseline parser
*does* handle.

| library         | divergences |
|-----------------|------------:|
| mistune         | 26          |
| markdown-it-py  | 24          |
| marko           | 27          |

**Important caveat**: of the ~25 divergences each candidate has, the
vast majority are cases where the **baseline diverges from the mdast
spec**, not where the candidate does. The most common patterns:

- **Soft line breaks inside paragraphs / blockquotes**: candidates
  emit `text + text("\n") + text` (per mdast spec); baseline merges
  them into a single text node.
- **Inline link followed by text**: candidates emit
  `link(...) + text(".")`; baseline emits a single trailing text node
  for `link(...).` that drops the URL.
- **Trailing newline in fenced code values**: mistune and marko
  preserve the trailing `\n`; baseline strips it.

These are **structural improvements**, not regressions. Adopting any
of the candidates would also fix several baseline correctness bugs as
a side effect — albeit changing the mdast shape that downstream code
currently depends on.

The one candidate-side bug surfaced was marko losing GFM table
alignment metadata (a translator fix; not investigated further in the
spike).

### Completeness gap on hard constructs (`gap_cases.md`)

The happy-path comparison above is **not the whole picture**: the
baseline parser is documented as not handling several CommonMark / GFM
constructs at all (see `docs/UPSTREAM_SYNC.md:442`). On those
constructs it silently flattens to `text` / `paragraph` nodes — the
same surface area issue #69 was opened to address.

`fixtures/gap_cases.md` exercises six gap constructs. **Silent drop**
means the construct was parsed as ordinary text/paragraph; **recognised**
means the parser emitted the correct mdast node type.

| construct             | baseline    | mistune    | markdown-it-py | marko       |
|-----------------------|-------------|------------|----------------|-------------|
| setext heading        | silent drop | recognised | recognised     | recognised  |
| indented code block   | silent drop | recognised | recognised     | recognised  |
| task list item        | recognised¹ | silent drop| recognised     | recognised  |
| footnote definition   | silent drop | silent drop| silent drop²   | silent drop |
| inline HTML           | silent drop | silent drop| silent drop    | silent drop |
| definition list       | silent drop | silent drop| silent drop    | silent drop |
| **silent-drop count** | **5**       | **4**      | **3**          | **3**       |

¹ Baseline matches `- [x]` as a list item but doesn't extract the
checkbox state.
² markdown-it-py supports footnotes via the `mdit-py-plugins` package
(not pulled in by the spike); enabling it would drop the silent-drop
count to 2.

**The baseline is strictly worse on completeness than every
candidate.** That's the half of the perf comparison the happy-path
numbers don't show: baseline runs faster partly because it does less
work per byte — setext headings, indented code, multi-backtick spans,
escaped chars, and raw HTML all skip straight through the inline
fast-paths instead of being parsed.

## Implication for the Option A/B/C decision

The spike data argues against a clean recommendation in either
direction:

1. **Performance**: baseline wins at 2.59ms median vs 11-47ms for the
   candidates. But that win is at least partly a function of doing
   *less work per byte*: the baseline skips entire construct families
   on the fast path, while the libraries fully tokenise them. Apples
   to apples requires either teaching the baseline to handle setext +
   indented code + escaped chars (Option A) and re-measuring, or
   accepting that the perf gap pays for genuine completeness.

2. **mdast fidelity on the happy path**: all three candidates are
   roughly equivalent (24-27 minor divergences) and each closes some
   baseline correctness bugs. mostly toward greater spec compliance.

3. **Completeness on hard constructs**: the baseline is strictly
   worse than every candidate. It silently flattens setext, indented
   code, multi-backtick spans, escaped chars, raw HTML, and definition
   lists into plain text — the exact gap list issue #69 enumerated.

4. **Translator LOC**: all under the 250-line budget.

### Three options now, not two

- **Option A (close baseline gaps in-tree)**: write parser code for
  setext, indented code, escaped chars, multi-backtick spans (the
  ones #69 listed as common in LLM output). Estimated ~300-400 LOC of
  carefully-tested regex / state-machine work, plus the existing
  parser keeps its 2.6ms perf. Doesn't address `_remend` gaps from the
  issue #69 follow-up comment.

- **Option B (library swap)**: pay the 5× perf hit (10-15ms median)
  for `mistune` or `markdown-it-py`, eat ~150-215 LOC of translator,
  close the completeness gap *and* most `_remend` gaps in one motion.
  **markdown-it-py is now the preferred candidate** (best
  completeness score, only 1.5ms slower than mistune), with
  `mdit-py-plugins` available for footnotes if needed later. mistune
  is the runner-up. marko drops out on performance.

- **Option D (split the problem)**: keep the fast hand-rolled parser
  *and* close gaps in-tree (Option A), but separately port upstream
  `remend` directly for the streaming side. Two efforts, two PRs, but
  preserves perf while closing both bug classes. More total work than
  Option B but no dependency added.

### Recommendation

The right answer depends on team priorities the spike can't answer:

- **If 10ms median parse time is fine** (likely true for chat
  streaming, where LLM token latency dwarfs this), **Option B with
  markdown-it-py is the cleanest path**. One PR, one dep, both gap
  lists close.
- **If we want zero-dep core preserved**, **Option D** is the only
  path that keeps the install footprint small while closing both bug
  classes. Highest total effort.
- **If neither perf nor zero-dep is sacred**, Option B still wins on
  effort per fix delivered.

Option C (selective parser-side fixes only, the original framing in
the issue) leaves the streaming-side bugs from the #69 follow-up
comment unaddressed and should be ruled out unless we ship it
alongside a separate `_remend` fix.

## Triggers to revisit this decision

The chat-scoped Option A (PRs #99 + #101) is the right call **for the
SDK's current scope** -- LLM output rendered into chat platforms. The
moment the input source or rendering target changes, the spike data
should be re-run with a workload-shaped fixture before deciding
anything.

Concrete triggers that should cause us to re-open this:

- **A non-chat input surface lands.** The chat-scoped assumption is
  "input comes from an LLM; humans don't write the markdown we parse."
  That breaks the moment we start parsing markdown that humans (or
  external corpora) authored:
  - User-authored memory / notes / scratchpads stored in the SDK
  - Ingestion of `*.md` files for RAG-style workflows
  - Parsing incoming GitHub PR/issue bodies for structure extraction
    (today the GitHub adapter mostly emits, not parses)
  - Any "import markdown" public API
  Human-authored content routinely uses setext, indented code,
  footnotes, raw HTML, and multi-backtick spans -- exactly the gaps
  the baseline silently drops.

- **A long-form artifact output surface lands.** When agents start
  emitting research-summary / report / document artifacts (not chat
  messages), the workload shifts toward CommonMark fidelity:
  - Footnotes for citations
  - Math regions rendered (not just sanitised)
  - Multi-backtick code spans for technical documentation
  - Tables with richer cell content
  Parsing for an artifact also happens once per document, not per
  stream chunk -- which makes the 5-18× perf cost of Option B much
  more tolerable than it is for streaming.

- **A web rendering surface for chat-sdk-python.** Upstream added
  `@chat-adapter/web` in v4.27.0 (a browser-side chat UI). It's
  explicitly out of scope for chat-sdk-python today (see PR #83 sync
  scope). If that ever ships in Python, the rendering target tolerates
  richer markdown because the browser can display setext / footnotes /
  HTML natively.

- **A new chat platform that demands richer parsing.** Unlikely in
  the near term -- the existing eight platforms all render a similar
  CommonMark subset. But e.g. a platform with native footnote support
  could surface a gap.

### Upstream check (May 2026)

Spot-checked `vercel/chat`'s `packages/` directory at the time of
writing. The only relevant package besides the eight chat adapters and
the core/state packages is **`adapter-web`** (added in v4.27.0, Python
port deferred). No artifact-rendering, RAG, document-ingestion, or
standalone markdown-rendering packages exist upstream. The triggers
above are forward-looking -- none are imminent in upstream-tracked
work.

### Playbook for re-running

When a trigger materialises:

1. Author a fixture file under `tests/parser_spike/fixtures/` that
   represents the new surface's actual content (not generic
   CommonMark -- workload-shaped).
2. Re-run `pytest tests/parser_spike/test_mdast_parity.py -s` and
   `python scripts/parser_spike/benchmark.py`. Both pick up the new
   fixture automatically if added to `conftest.py`.
3. Compare the silent-drop count and benchmark numbers against the
   chat-scoped findings above. The decision matrix shifts toward
   Option B when:
   - Silent-drop count is materially higher on the new fixture
     (≥6 constructs that the new surface needs)
   - Parse latency is one-shot rather than per-stream-chunk
   - The team is OK adding a dependency to the runtime core
4. If thresholds are met, promote `markdown-it-py` translator from
   `parser_spike/` into runtime (it's the preferred candidate per
   the spike data). Add `markdown-it-py` to the relevant extras
   group (not `dependencies`, to preserve zero-dep core install for
   chat-only consumers).

