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

### mdast fidelity vs the baseline

Tested against the `tests/parser_spike/fixtures/mixed_content.md`
corpus (≈3KB of mixed headings, tables, code blocks, lists, links,
images, blockquotes, emphasis).

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

## Implication for the Option A/B/C decision

The original Option B framing assumed a library swap would be a clear
win. The spike data adds nuance:

1. **Performance budget fails on every candidate** at the 5ms target
   set in the issue. The baseline is the only parser that meets it,
   and it does so by a wide margin (2.59ms vs 11-47ms).

2. **mdast fidelity is a wash**: all three candidates are roughly
   equivalent and each closes some baseline correctness bugs. None is
   "closer to the existing output" — all diverge from it in similar
   ways, mostly toward greater spec compliance.

3. **Translator LOC is not a blocker**: all under the 250-line budget.

This argues for a **revisited Option D**: keep the fast hand-rolled
parser for the static parse path, and port the upstream `remend` npm
package to Python for the streaming-completion path. The three
production bugs from issue #69 all lived in `_remend` /
`_get_committable_prefix`, not in `markdown_parser.py`. A direct
remend port would close that gap without taking the 5× perf hit.

The parser-side gaps from issue #69 (setext, footnotes, escaped chars,
multi-backtick spans, raw HTML, indented code) would remain
unaddressed under Option D. If they become a recurring problem, Option
B can still be reconsidered later — the spike scaffolding stays here
for re-runs.

### Recommendation

Before committing to a path, the team should weigh:

- How much does the 5ms parse target actually matter for chat
  streaming workloads? (A 10ms median may be entirely fine relative to
  LLM token latency.)
- How frequently are users hitting the parser-side gaps vs the
  streaming-side bugs?
- Is the structural mdast improvement (better link/paragraph splitting,
  spec-correct softbreaks) worth the dependency cost?

If those answers come out in favor of the swap, **mistune is the
preferred candidate**: lowest divergence count, smallest translator
LOC, fastest of the three candidates. markdown-it-py is the runner-up
if richer plugin extensibility becomes important (footnotes, tasks,
custom containers). marko drops out on performance alone.
