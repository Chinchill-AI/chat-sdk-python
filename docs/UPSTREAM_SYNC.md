# Upstream Sync Guide

How to keep `chat-sdk-python` in sync with the [Vercel Chat TS SDK](https://github.com/vercel/chat).

## Version Mapping

Our version embeds the upstream Vercel Chat version: `0.{upstream_major}.{upstream_minor}[.patch]`

| Python version | Upstream version | Meaning |
|---|---|---|
| `0.4.25` | `4.25.0` | Synced to upstream 4.25.0 |
| `0.4.25.1` | `4.25.0` | Python-only fix on top of 4.25.0 |
| `0.4.25a1` | `4.25.0` | Alpha while porting 4.25.0 |
| `0.4.26` | `4.26.0` | Synced to upstream 4.26.0 |
| `0.4.26.3` | `4.26.0` | Python-only fixes on top of 4.26.0 |
| `0.4.27` | `4.27.0` | Synced to upstream 4.27.0 |
| `0.4.27.1` | `4.27.0` | Python-only fix on top of 4.27.0 (Slack upload confirmation backport) |
| `0.4.29` | `4.29.0` | Synced to upstream 4.29.0 (upstream never tagged `chat@4.27.0`/`chat@4.28.0`) |
| `0.4.30` | `4.30.0` | Synced to upstream 4.30.0 (Teams adapter migrated to the `microsoft-teams-apps` SDK, issue #93) |

The `UPSTREAM_PARITY` constant in `chat_sdk/__init__.py` provides programmatic access
to the upstream version this release is synced to.

## How to Sync an Upstream Release

Step-by-step procedure for porting a new upstream release (e.g., `4.26.0`):

```bash
# 1. Update the full TS clone (for reading diffs; see "How to Diff Upstream
#    Changes") and check what changed. Keep it separate from /tmp/vercel-chat,
#    the pinned checkout the fidelity script verifies (step 5).
cd /tmp/vercel-chat-full && git fetch origin --tags
git log --oneline chat@4.25.0..chat@4.26.0 -- packages/

# 2. For each commit, read the diff
git diff chat@4.25.0..chat@4.26.0 -- packages/chat/src/
git diff chat@4.25.0..chat@4.26.0 -- packages/adapter-slack/src/
# ... repeat for each changed package

# 3. Create a sync branch
cd /tmp/chat-sdk-python
git checkout -b sync/upstream-v4.26.0

# 4. Port each change following the TS → Python Porting Hazards below

# 5. Run full validation
uv run ruff check src/ tests/ scripts/
uv run ruff format --check src/ tests/ scripts/
uv run python scripts/audit_test_quality.py
# TS_ROOT (default /tmp/vercel-chat) must be a clean checkout of the tag in
# scripts/upstream_pin.json; the full clone from step 1 sits at another
# commit and fails the SHA check. If /tmp/vercel-chat is at an older pin,
# delete it first (git refuses to clone into a non-empty directory).
git clone --depth 1 --branch "$(jq -r .pin.tag scripts/upstream_pin.json)" \
  https://github.com/vercel/chat.git /tmp/vercel-chat
TS_ROOT=/tmp/vercel-chat uv run python scripts/verify_test_fidelity.py --strict
uv run pytest tests/ --tb=short -q

# 6. Update version
#    - pyproject.toml: version = "0.4.26"
#    - README.md: status line
#    - __init__.py: UPSTREAM_PARITY = "4.26.0"
#    - CLAUDE.md: version reference
#    - CHANGELOG.md: new entry

# 7. PR, merge, publish
gh pr create --title "sync: upstream v4.26.0"
```

### What to check for each upstream commit

- [ ] New types or fields → add to `types.py`
- [ ] New methods on Thread/Channel → add to `thread.py`/`channel.py`
- [ ] New adapter features → update the adapter + integration-style tests
- [ ] New TS tests → run fidelity script, port missing tests
- [ ] Changed behavior → verify Python matches with regression tests
- [ ] Review the porting hazards below for each change
- [ ] Every new `fetch_thread()` behavior → round-trip test with channel APIs
- [ ] Every new adapter feature → at least one end-to-end test (not just unit/conversion)

### Upstream behavior is one input, not source of truth

If upstream tests are missing coverage for a feature, add Python-only regression
tests. If upstream tests lock in inconsistent behavior, choose one of:
- **Preserve parity** and document the inconsistency in the non-parity section below
- **Intentionally diverge** and document the divergence in the non-parity section

### Test fidelity (strict mode)

`scripts/verify_test_fidelity.py` runs in CI (`.github/workflows/lint.yml`)
pinned to `vercel/chat@4.31.0`. **CI runs `--strict`** — the repo ships at 0
missing *for mapped core files* (733/733 at the pin, counting each `it.each`
template as one test).

**Single pin source: `scripts/upstream_pin.json`.** It holds
`pin: {tag, sha}` (the strict CI tag and its full commit SHA) and
`target: {tag, sha}` (the tag an in-flight sync wave is porting towards;
`chat@4.41.1` for the 4.41 wave, #184). The script and `lint.yml` (via
`jq`) read it; no other file hard-codes the tag. Resolve SHAs with
`git rev-parse <tag>^{commit}`. The major.minor of `pin.tag` must equal
the major.minor of `UPSTREAM_PARITY` in `src/chat_sdk/__init__.py`
(exact equality is not required: `UPSTREAM_PARITY = "4.41.0"` may pin
`chat@4.41.1`). Only the pin-bump PR changes `pin` (#203 for this wave).

Clone the pinned checkout locally with:

```bash
git clone --depth 1 --branch "$(jq -r .pin.tag scripts/upstream_pin.json)" \
  https://github.com/vercel/chat.git /tmp/vercel-chat
```

**Scope is two-tier, plus explicit skips** (all dicts in the script):

- `MAPPING` — the strict set. CI fails on any missing test here.
- `TARGET_MAPPING` — rows checked only by `--report-target` (together with
  `MAPPING`): files that do not exist at the pin (`history/*`,
  `agent-session`, `app-context`, `installation-events`), plus files whose
  Python counterpart still has gaps at the pin (`cards`, `modals`, `emoji`,
  `message`; issue #78). A row moves to `MAPPING` once it is at 0 missing
  at the pin. A Python file that does not exist yet reports every TS test
  as missing.
- `UNMAPPED` — core test files deliberately not checked, with the reason:

| TS file(s) | Reason |
|---|---|
| `ai/tanstack/messages.test.ts`, `ai/tanstack/tools.test.ts` | JS-only TanStack AI adapter (chat@4.41.0); no Python equivalent. Non-parity row lands in #203. |
| `workflow/approval.test.ts` | Vercel Workflow SDK integration (chat@4.35.0); no Python equivalent. |
| `jsx-react.test.tsx`, `jsx-runtime.test.ts`, `jsx-runtime.test.tsx` | Covered by the "JSX Card/Modal elements" non-parity row. |
| `adapters/index.test.ts` | Covered by the "`chat/adapters` static adapter catalog" non-parity row. |
| `errors.test.ts`, `logger.test.ts`, `chat-singleton.test.ts` | #78 pending: no Python counterpart identified yet (0 exact name matches). |

Every `packages/chat/src/**/*.test.ts(x)` must appear in exactly one of
the three. An unlisted file is a warning at the pin and an error in
`--report-target`.

**Name extraction.** `it("…")` and `test("…")` each count as one test
(`regex.test("…")` does not). Each `it.each` / `test.each` template counts
as **one** logical test, which corresponds to one `@pytest.mark.parametrize`
test in Python. Its placeholders (`%s %d %i %f %j %o %O %c %p %# %$ %%`,
`$name`, `$a.b`) are stripped before the snake_case conversion. Placeholders
are stripped only in `.each` titles; a literal `%` or `$` in a plain `it`
title stays part of the name. `describe.each` titles are used only for
reporting. The script reports exact and fuzzy match counts per file,
because the word-overlap fuzzy matcher can claim an unrelated leftover
Python test and hide a gap. The target report lists every fuzzy pair so
it can be audited.

**Target report.** Run
`TS_ROOT=<checkout of target.tag> uv run python scripts/verify_test_fidelity.py --report-target`.
It checks `MAPPING` + `TARGET_MAPPING` at the target SHA and never fails
on missing tests. It rewrites `scripts/fidelity_target.json` with the tag,
SHA, totals, and per-file missing `[describe, it]` pairs, extra counts and
fuzzy pairs, and prints the delta against the report committed at `HEAD`
(read with `git show`, so re-running before committing still compares
against the committed baseline, not the previous run's output). The
committed report is the authoritative wave-wide list. Every wave PR
regenerates it and quotes the delta in its description, taken before
the regenerated report is committed.

Infra guardrails:

- The workflow's `Clone upstream vercel/chat at pinned parity tag` step does
  **not** use `continue-on-error` — a failed clone aborts the job loudly.
  It also fails if the cloned HEAD differs from `pin.sha` (a moved tag),
  and it writes the resolved SHA to the job summary.
- The script fails if `TS_ROOT` is a git checkout whose HEAD differs from
  the expected SHA (`pin.sha`, or `target.sha` for `--report-target`), if
  tracked files under `packages/chat/src` have local edits, or if
  `TS_ROOT/.git` exists but git cannot read it (e.g. a "dubious ownership"
  refusal). A plain export without `.git` cannot be verified and only warns.
- The script fails with exit 1 if any mapped TS file is missing under
  `TS_ROOT` (defense in depth against silent skips).
- `--report-target` records a mapped file as absent only when a verified
  git checkout's HEAD commit lacks it. It fails without writing the report
  if a core test file in the commit is missing on disk (a sparse or partial
  checkout, which passes the clean-tree check), or if any mapped file is
  absent from a plain export, where absence cannot be verified.
- `--strict` and `--report-target` fail if any upstream test cannot be
  extracted (a non-literal title, an unknown call form such as `it.todo`,
  an unreadable `.each` table) — an unextracted test would otherwise go
  uncounted. Understood forms: `it`/`test` (any quote style), modifier
  chains (`it.skip`, `it.only`, `it.concurrent`, …), `.each` / `.for`
  (with optional `<T>` type arguments), and `.skipIf(c)` / `.runIf(c)`.
  Baseline mode only warns. Calls inside comments and string, template or
  regex literals (fixture source, commented-out examples) are ignored, and
  a `describe.only("s", () => { it("t") })` one-liner still yields its test.
  A template literal or block comment still open at end of file, and a
  computed title such as `"rejects " + kind`, are extraction errors (so
  they fail `--strict`) rather than hiding tests or matching a prefix.
- `--check-docs` (a separate CI step) fails if a clone snippet
  (`--branch chat@X`, `--branch=chat@X`, `-b chat@X`) or a `pinned to
  [the] [vercel/]chat@X` phrase in `CLAUDE.md` or this file disagrees with
  `pin.tag`. Matching is case-insensitive, tolerates markdown decoration
  and line continuations, and the phrase may wrap across lines.

Workflows:

| Goal | Command |
|------|---------|
| Port a missing test | Write the Python test and land it; CI rejects anything that re-introduces a gap |
| Add a Python-only divergence (intentional skip) | Document in [Known Non-Parity](#known-non-parity-with-typescript-sdk), then `--update-baseline` and switch the workflow back to non-strict default for that file if truly unavoidable |
| Sync-wave progress | `TS_ROOT=<target checkout> uv run python scripts/verify_test_fidelity.py --report-target`, commit `scripts/fidelity_target.json`, and quote the delta in the PR |
| Bump the pin | Update `pin.tag` + `pin.sha` in `scripts/upstream_pin.json` together with `UPSTREAM_PARITY`, then run `--strict` and `--check-docs` |
| Final parity check | Same as CI: `TS_ROOT=/tmp/vercel-chat uv run python scripts/verify_test_fidelity.py --strict` |

Baseline mode (the default without `--strict`) is retained for local
development where a few ports land in flight. Regenerate the baseline via
`--update-baseline` rather than hand-editing.

## Divergence Policy

Every divergence from upstream has a cost: merge conflicts on future syncs,
cross-SDK state drift, and a gradual slide from "port" toward "fork". Follow
the rules below before adding one.

### When to diverge

1. **Default: preserve parity.** Matching upstream behavior — even buggy —
   reduces merge conflicts and keeps cross-SDK state predictable. If the
   behavior is cosmetic or stylistic, preserve parity and move on.
2. **Diverge only when upstream** causes one of:
   - **Data loss or corruption** (e.g. dropping fields on round-trip).
   - **Malformed wire output** the platform itself mis-renders.
   - **Hard UX failure with no workaround** (e.g. stuck loading state
     that users can't clear).
3. **Before diverging**, open an issue upstream
   ([vercel/chat](https://github.com/vercel/chat/issues)) linking the bug.
   If upstream accepts and fixes it, delete the divergence on the next sync.
4. **Budget**: a sync PR that accumulates **more than 2 divergences** is a
   signal — escalate to a design discussion ("is this still a port?")
   before landing quietly.

### How to land a divergence

1. **Commit prefix**: use `diverge(scope): ...`, not `fix:` — `fix:` implies
   parity with upstream's intent.
2. **Add a row to the [Known Non-Parity](#known-non-parity-with-typescript-sdk)
   table** with: Python behavior, TS behavior, rationale, and upstream
   issue link (if filed).
3. **Drop a one-line breadcrumb at the divergence site**:
   ```python
   # Divergence from upstream — see docs/UPSTREAM_SYNC.md
   ```
   So a future porter doesn't delete the code thinking it's drift.
4. **Add a regression test** that fails if someone "fixes" the divergence
   back to upstream's behavior. The test's docstring should cite the reason.
5. **CHANGELOG entry** under a "Python-specific (divergence from upstream)"
   subsection.
6. **Run the self-review adversarial checks** from
   [docs/SELF_REVIEW.md](SELF_REVIEW.md). Divergence code is exactly the
   kind of novel, Python-specific logic that bot reviewers consistently
   find bugs in — catch them yourself first.

### Review signal

- **Sync PR titles**: `sync: upstream v<ver>` (not a branch name). Reviewers
  scanning the PR list need to see "this is a sync" at a glance.
- **Divergence commits are separate** from the sync commit. Don't bundle a
  divergence into `sync: upstream v...`; split it into its own
  `diverge(scope): ...` commit with the non-parity table update in the same
  commit.

## How to Diff Upstream Changes

```bash
# Clone or update a full TS clone. Use its own path: /tmp/vercel-chat is
# the pinned shallow checkout the fidelity script verifies by SHA.
git clone https://github.com/vercel/chat.git /tmp/vercel-chat-full
cd /tmp/vercel-chat-full
git log --oneline -20  # see recent commits

# Compare a specific adapter
diff -u /tmp/vercel-chat-full/packages/adapter-slack/src/index.ts \
        /tmp/chat-sdk-python/src/chat_sdk/adapters/slack/adapter.py

# Compare core types
diff -u /tmp/vercel-chat-full/packages/core/src/types.ts \
        /tmp/chat-sdk-python/src/chat_sdk/types.py
```

The Python module layout mirrors the TS package layout:

| TS Package | Python Module |
|-----------|---------------|
| `packages/core/src/chat.ts` | `src/chat_sdk/chat.py` |
| `packages/core/src/thread.ts` | `src/chat_sdk/thread.py` |
| `packages/core/src/channel.ts` | `src/chat_sdk/channel.py` |
| `packages/core/src/types.ts` | `src/chat_sdk/types.py` |
| `packages/core/src/cards.ts` | `src/chat_sdk/cards.py` |
| `packages/core/src/modals.ts` | `src/chat_sdk/modals.py` |
| `packages/core/src/from-full-stream.ts` | `src/chat_sdk/from_full_stream.py` |
| `packages/core/src/markdown.ts` | `src/chat_sdk/shared/markdown_parser.py` + `base_format_converter.py` |
| `packages/core/src/streaming-markdown.ts` | `src/chat_sdk/shared/streaming_markdown.py` |
| `packages/adapter-shared/src/mentions.ts` | `src/chat_sdk/shared/mentions.py` |
| `packages/adapter-shared/src/code-fences.ts` | `src/chat_sdk/shared/code_fences.py` |
| `packages/adapter-slack/src/index.ts` | `src/chat_sdk/adapters/slack/adapter.py` |
| `packages/adapter-shared/src/download.ts` | `src/chat_sdk/shared/download.py` (#204) |
| `packages/state-memory/src/index.ts` | `src/chat_sdk/state/memory.py` |
| `packages/state-redis/src/index.ts` | `src/chat_sdk/state/redis.py` |
| `packages/state-ioredis/src/index.ts` | `src/chat_sdk/state/redis.py` (`IoRedisStateAdapter`) |
| `packages/state-pg/src/index.ts` | `src/chat_sdk/state/postgres.py` |

### Shared text utilities (chat@4.33–4.41, #193)

`adapter-shared/src/mentions.ts` (`replaceBareMentions`, `maskCodeSpans`; `d4c52cad`, `683eadc1`) and `adapter-shared/src/code-fences.ts` (`normalizeCodeFences`; `e71bfead`) are ported character-for-character as `chat_sdk.shared.mentions` / `chat_sdk.shared.code_fences` and exported from `chat_sdk.shared`. They are utilities only: adapters adopt them in #206/#209 (Slack), #229 (Discord) and #239 (WhatsApp). Teams must not adopt the scanner, because upstream removed Teams outbound mention conversion in `7062c395` (#216).

JS string semantics are reproduced explicitly, so there is no boundary-character gap:

- `isLetter`/`isNumber`/`isWord` are ASCII range checks, not `str.isalpha()`/`\w` (`é@x` → `é<@x>`).
- `isBoundary`'s `char.trim() === ""` and `code-fences.ts`'s `trimStart()` use the JS whitespace set in `shared/_js_compat.py` (`JS_WHITESPACE`): U+FEFF counts as whitespace, U+001C–U+001F and U+0085 do not. The table-row emptiness check in `ast_to_plain_text` uses the same set.
- `startsWith(text, index, value)` lowercases the slice, so `HTTPS://` is a URL.
- JS `text[i]` outside the string is `undefined`; the port bounds-checks every look-behind/look-ahead instead of letting `text[-1]` wrap.
- The code-fence patterns use `\Z` for JS `$` (no `m` flag) and `[0-9]` for `\d`.
- JS indexes UTF-16 code units and Python indexes code points. The output is the same, because every cut point is an ASCII delimiter or a BMP whitespace character, so no slice can split a surrogate pair.

Both modules were checked against the TS sources at `chat@4.41.1` (run under Node) with a differential fuzz of about 130k random inputs built from the delimiter alphabet plus JS/Python whitespace edge characters. There were 0 mismatches.

`ast_to_plain_text` follows upstream `toPlainText` from `5c926f19` (chat@4.34.0) and the core half of `764e4759` (chat@4.38.1). Root children are joined with `"\n\n"`. `list`, `listItem` and `blockquote` children are joined with `"\n"`. A `tableRow` joins its cells with `"\t"` and keeps empty cells. A `table` joins its rows with `"\n"` and drops rows that are empty after `trim()`. A string `value`/`alt` is returned as-is. The Python parser already kept soft line breaks inside text nodes, which was the remark half of #604. `table_to_ascii` reads cells, so its output does not change.

### Slack outbound mentions (chat@4.32–4.37, #206)

Slack adopts the shared scanner in both outbound passes: `SlackFormatConverter` (`_finalize` and mrkdwn text nodes) via `_link_bare_mention_names` (`a8c4af74`), and `SlackAdapter._resolve_outgoing_mentions` (`07c11129`, `a8c4af74`, `d4c52cad`). The old ASCII lookbehind regex (converter) and Unicode-`\w` regex (resolver) are gone. The native `stream()` path resolves mentions line by line before each `append` (`6f0d2f02`). `resolve_committed` ports `resolveCommitted`, and `last_appended` tracks the resolved buffer. Fence lines toggle state only once their newline is committed. #208 replaces this with a marker-matching tracker.

Parity fix: the final native-stream delta now comes from `renderer.get_committable_text()` after `renderer.finish()`, as upstream does (4.31 and 4.37). It used to come from `finish()`'s return value, which is the `_remend`'d render. That value is not guaranteed to extend the committable prefix, so it could not share the resolved buffer's coordinate space. As a side effect, a stream that ends with an unclosed inline marker (`**bold`) no longer gets a closing marker appended on the native path. Upstream behaves the same way.

Known upstream-parity edge: a segment committed after an inline-marker holdback cut is resolved without the text before the cut. So in `https://x.io/*@alice`, where the renderer cuts at the unclosed `*`, the handle is resolved. Upstream `resolveCommitted` does the same, and the adapter code has a comment noting it.

Renderer-dependent edge (native stream only): holdback cut positions come from the Python `StreamingMarkdownRenderer`, whose `_remend` is simplified compared with npm `remend` (a known limitation in CLAUDE.md). Rare inputs with inline markers inside URLs or code spans can therefore be cut at different points, and because each post-cut segment is resolved without the text before it, a handle can resolve differently from upstream on the native stream (for example, a `@name` inside a URL after a `*` cut). Post/edit resolve the full text and are not affected. Fence detection likewise uses Python `str.lstrip()` rather than JS `trimStart()`, to stay consistent with the Python renderer's fence tracking; the two differ only for lines starting with U+FEFF or U+001C–U+001F.

### SDK-free primitive subpaths (Teams, chat@4.31)

These six runtime-free Teams primitive subpaths mirror upstream's
`@chat-adapter/teams/*` subpath exports (NEW in chat@4.31.0, commit `8c71411`).
Each is importable **without** loading the `microsoft_teams` SDK, an HTTP
client, or the high-level adapter — the package's `teams/__init__.py` is PEP-562
lazy (mirrors the 0.4.30 Slack subpath pattern, `slack/__init__.py`). The
runtime-free guarantee is pinned by `tests/test_teams_primitives_packaging.py`.
A future syncer extending these primitives should keep them adapter/SDK-free
and add the new module + its boundary coverage to that packaging test.

| TS Package | Python Module |
|-----------|---------------|
| `packages/adapter-teams/src/format/` | `src/chat_sdk/adapters/teams/format.py` |
| `packages/adapter-teams/src/webhook/` | `src/chat_sdk/adapters/teams/webhook/` |
| `packages/adapter-teams/src/api/client.ts` | `src/chat_sdk/adapters/teams/api/` |
| `packages/adapter-teams/src/cards-primitives/input.ts` | `src/chat_sdk/adapters/teams/cards_input.py` |
| `packages/adapter-teams/src/graph/` | `src/chat_sdk/adapters/teams/graph/` |
| `packages/adapter-teams/src/modals-primitives/` | `src/chat_sdk/adapters/teams/modals.py` |

The canonical `TeamsFieldElement` (`{label, value}`) lives in `cards_input.py`
and is re-imported by `modals.py` — mirroring upstream, where
`modals-primitives/types.ts` imports `TeamsFieldElement` from
`../cards-primitives` rather than redefining it. (The Slack adapter exposes the
analogous `slack/{webhook,api,blocks,format}` SDK-free subpaths from 0.4.30.)

### Twilio conversation boundaries (chat@4.39–4.40, #235)

Parity, not a divergence. Upstream `28bc7768` (vercel/chat#849, chat@4.39.0)
switched the Twilio adapter's `lockScope` from `"channel"` to `"thread"`, and
the Twilio part of `b7c9316b` (vercel/chat#875, chat@4.40.0) made
`twilioChannelId(threadId)` validate the id and return it unchanged. The Python
port mirrors both: `TwilioAdapter.lock_scope == "thread"` and
`twilio_channel_id(thread_id)` / `channel_id_from_thread_id` /
`fetch_thread().channel_id` return the full `twilio:{sender}:{recipient}` thread
id. Before this, every recipient texting one bot number shared a channel
(`twilio:{sender}`), so their handlers serialized on (or, under `drop`, were
dropped by) a single lock and their channel history / channel state was shared.

**Breaking for Twilio consumers:** channel ids change, so channel-state and
channel-history keys written under the old `twilio:{sender}` id are no longer
read (orphaned, as upstream intends). Thread ids, `encode_thread_id` /
`decode_thread_id` and thread history are unchanged; `channel_name` is still the
bot-side sender. Because `channel_id == thread_id`, `persist_thread_history`
no longer appends a second copy of each inbound message to a channel list.
Regression coverage: `tests/test_twilio_adapter.py::TestThreadIds`
(`test_uses_the_full_dm_thread_id_as_its_channel_id`,
`test_isolates_concurrent_recipients_with_thread_scoped_locks`).

### Callback-URL tokens (chat@4.40, #194)

Parity, with two Python-only divergences recorded in the non-parity table:
*Callback-token lease fence* and *Channel edit callback scope*. The
callback-token part of upstream `b7c9316b`
(vercel/chat#875, chat@4.40.0) plus the button copy from `4a0b5c0c`
(vercel/chat#895):

- **Stored record.** `chat:callback:<token>` now holds
  `{"actionId", "url", "originalValue"?, "scope": {"id", "type"}}`. The keys
  stay camelCase so either SDK can resolve the other's tokens.
  `originalValue` is omitted when the button has no value. The TTL is 7 days
  (was 30).
- **Scope.** Thread posts, schedules and edits bind to
  `{thread.id, "thread"}`. Channel posts and schedules bind to
  `{channel.id, "channel"}`. A channel `SentMessage.edit` also binds to
  `{channel.id, "channel"}` (a Python-only divergence; upstream binds it to
  the reported thread id). The `post_ephemeral` DM fallback mints tokens only
  after `open_dm`, bound to
  `{adapter.channel_id_from_thread_id(dm_thread_id), "channel"}`. When neither
  the native path nor the DM fallback runs, no token is minted.
- **Resolve.** `resolve_callback_url(token, state, context)` acquires
  `acquire_lock("chat:callback:<token>", 10_000)` and returns `None` if the
  lock is taken. Otherwise it validates the record, requires `actionId` and
  the scope id to match the click's `CallbackContext`, deletes the record,
  checks with `extend_lock` that the lease never lapsed (Python-only), and
  releases the lock in `finally`. The scope id is `channel_id` for a channel
  scope and `thread_id` for a thread scope. The action's POST body carries the
  stored `actionId`.
- **Legacy records are rejected.** A bare URL string (the "legacy string
  format" the old resolver accepted), a pre-upgrade `{url, originalValue}`
  record, or any other object without `actionId` / `scope` no longer resolves. Such a click
  dispatches the raw `__cb:` value and nothing is POSTed, as upstream.
  Validation uses `isinstance` checks, not truthiness, so an empty-string
  `actionId` still passes, and a present `originalValue` must be a `str`.
- **Dropped Python-only fallback.** The resolver used to fall back to a
  snake_case `original_value` key. No Python release wrote that key, and
  upstream never read it, so it is gone. Only `originalValue` is read.
- **Button copy.** The token swap copies every button key except
  `callback_url` (it used to copy a whitelist), so fields such as `tooltip`
  (#202) survive.

Channel-scoped tokens resolve only when `adapter.channel_id_from_thread_id`
of the clicked message's thread equals the `ChannelImpl.id` that minted the
token. In every adapter, `thread.channel` derives its id with that same
function (`derive_channel_id`). For a `chat.channel(id)` handle the ids match
when `id` is canonical for the adapter (Slack `slack:C123`, Discord
`discord:{guild}:{channel}`, Google Chat `gchat:spaces/X`, GitHub
`github:owner/repo`, Linear `linear:{issueId}`, Telegram `telegram:{chatId}`,
Teams: the thread id re-encoded with `;messageid=…` stripped from the
conversation id; WhatsApp, Messenger and
Twilio (#235): the thread id itself).

Thread-scoped tokens resolve only when the adapter's click `thread_id`
equals the thread id the card was posted or edited under. One known mismatch
remains, and it is upstream behavior at chat@4.41.1, not a divergence: the
click still runs `on_action` handlers with the raw `__cb:…` value, but
nothing is POSTed.

- **Google Chat cards posted to a DM thread** (`gchat:spaces/X:dm`) by
  `thread.post`. `_handle_card_click` encodes the clicked message's thread
  name without the `:dm` suffix, as upstream `handleCardClick` does. The
  `post_ephemeral` DM fallback is unaffected, because it binds to the DM
  *channel*, which both ids share.

The channel-edit divergence exists because the thread id reported for a
channel post often never equals a click's thread id:
- Teams and Google Chat `post_channel_message` return the channel id as the
  thread id (upstream too);
- Slack `post_channel_message` returns the synthetic `slack:C…:` until #209
  ports upstream `92530dd3` (vercel/chat#720), while a click reports
  `slack:C…:<message_ts>`. After #209 a Slack *DM* post reports
  `slack:D…:<ts>`, but a DM click reports `slack:D…:` (Python's DM
  `_handle_block_actions` divergence), so a thread scope would still miss;
- chained edits (`sent = await sent.edit(...)` twice), whose returned
  `SentMessage` dropped the thread-id override before #195 ported `16ea171e`
  (it now keeps it, so this reason no longer applies on its own).

Binding to the channel resolves all of these, independent of #209's merge
order, because every click on the message derives the same channel id.

**Breaking for `callback_url` users:** tokens minted before the upgrade stop
resolving, a repeat click no longer POSTs, tokens expire after 7 days, and the
POST's `actionId` is the minted button's id. The record is deleted before the
POST, so a failed POST is not retried, as upstream. Regression coverage:
`tests/test_callback_url.py` (including the Python-specific
`TestResolveCallbackUrlValidation` / `TestResolveCallbackUrlLocking`),
`tests/test_chat_faithful.py::TestActionsCallbackTokenBinding`, and the
`should bind fallback DM callbacks to the DM channel` ports in
`tests/test_thread_faithful.py` and `tests/test_channel_faithful.py`.

### WhatsApp business-scoped user IDs (chat@4.37–4.39, #236)

Parity apart from malformed-payload hardening (below) and one divergence:
contact matching (see the
[Known Non-Parity](#known-non-parity-with-typescript-sdk) row "WhatsApp contact
matching"). Ports upstream `3e6e866a` (vercel/chat#818,
chat@4.39.0) and the type-only context variants of `16879fdc` (vercel/chat#723,
chat@4.37.0). Meta can now deliver username-only / BSUID-only webhooks whose
messages carry `from_user_id` / `from_parent_user_id` and no `from`; before
this port the Python adapter indexed `inbound["from"]` and the per-message
`try/except` silently dropped them.

- **Identity precedence** (`_fields`, upstream `fields()`): phone
  (`system.wa_id ?? from ?? contact.wa_id`), then BSUID, then parent BSUID,
  ported as `is not None` chains. `_author` keeps upstream's `||` (an empty
  profile name falls through to `username`, then the user ID).
- **State keys are byte-identical to the TS SDK**, so TS and Python
  deployments sharing a state backend interoperate:
  `whatsapp:identity:alias:{phone_number_id}:{identifier}` → canonical user id,
  and `whatsapp:identity:route:{phone_number_id}:{user_id}` →
  `{"bsuid"?, "parent"?, "phone"?}` with absent keys omitted (never `None`).
  `_link` writes only aliases that differ and a route whose bsuid/parent/phone
  changed. Aliases are read concurrently and honored in list order.
- **State errors never drop a message**: `_resolve`, `_handle_user_id_update`
  and `_recipient` log a warning and fall back to the un-linked identity or the
  `_BSUID_PATTERN` recipient, as upstream does. A stored alias or route of the
  wrong type is read as absent.
- **Outbound addressing**: `post_message` resolves `{"to"?, "recipient"?}` once
  per logical post (shared by every chunk); `add_reaction` / `remove_reaction`
  resolve their own. `_BSUID_PATTERN` uses `fullmatch`, so, as with JS
  `/^...$/`, a trailing newline does not match.
- **Malformed changes (Python-specific hardening):** a `messages` change whose
  `metadata.phone_number_id` is missing, null or not a string fails each of its
  messages with the per-message `"Failed to handle inbound message"` error and
  the webhook still returns 200, so later changes in the same POST are
  dispatched. That matches upstream for a null `metadata` (read inside its
  per-message `try`); upstream would instead dispatch a `metadata` without
  `phone_number_id` under `whatsapp:undefined:...`. A `user_id_update` change
  without a business number is skipped with a warning (upstream throws out of
  `handleWebhook`, a 500 and a Meta retry of the whole batch), and a non-dict
  `user_id` / `parent_user_id` is read as absent, like upstream's `?.`. No
  identity key is ever written under an empty business number. A non-dict
  change `value` is skipped. Regression tests:
  `TestBusinessScopedUserIdsMalformedPayloads`.
- **Casing:** upstream's `WhatsAppRawMessage.userId` is `user_id` here,
  matching the existing snake_case raw key `phone_number_id`.
- **Not yet ported here:** the `recipient()` calls in upstream media sends
  (#238) and `reply` (#239), because those send paths do not exist in the
  Python adapter yet. `send_template` (#237) resolves its recipient the same
  way `post_message` does. `mark_as_read` and typing indicators
  address a `message_id` only, so they need no recipient.
- **Known limitations kept at parity** (upstream behaves the same at
  chat@4.41.1; revisit if upstream changes them):
  - Identity resolution runs before `process_message`, outside the Chat lock,
    and `_link` is read-then-write. Two first-contact webhooks for the same
    user delivered concurrently (one with phone and BSUID, one with the BSUID
    only) can pick different canonical ids and split into two threads. Closing
    this needs an atomic claim on alias keys.
  - Route writes carry no ordering. A Meta redelivery of an older message,
    processed after a `user_id_update`, can write the retired phone/BSUID back
    into the route. Core dedupe does not help because `_resolve` runs before
    dispatch.
  - Aliases never expire. After a user moves from phone P to Q, the alias
    `P → canonical` stays, so it keeps pre-change threads reachable. If the
    carrier later reassigns P to someone else, that person's first message
    (P plus a new BSUID) resolves to the old user's canonical id and links
    their BSUID to it. Fixing this needs a retirement rule for phone aliases,
    and a different thread key for the new owner, because thread ids prefer the
    phone. That is an identity-model change for upstream to settle first.
  - System messages use Meta's documented shapes (upstream
    `packages/adapter-whatsapp/sample-messages.md`). `user_changed_number`
    carries the old number in `from` and the new `wa_id` / `user_id` in
    `system`. `user_changed_user_id` has no `from`: its old-to-new mapping
    arrives in the separate `user_id_update` webhook. The adapter does not
    special-case undocumented shapes, such as a message-level `from_user_id`
    on a system message or a `from` without `system.wa_id`.

Regression coverage: `tests/test_whatsapp_webhook.py`
(`TestHandleWebhookBusinessScopedUserIds`, `TestParseMessageBusinessScopedUserIds`,
`TestPostMessageBusinessScopedRecipients`, `TestBusinessScopedUserIdsPythonSpecific`,
`TestBusinessScopedUserIdsMalformedPayloads`, `TestBusinessScopedUserIdsIdentityInvariants`).

### WhatsApp templates and typed Graph API errors (chat@4.34–4.40, #237)

Ports upstream `2338a665` (vercel/chat#588, chat@4.34.0; `sendTemplate`, with
the `...recipient()` spread from `3e6e866a`, chat@4.39.0) and `31bce0a7`
(vercel/chat#896, chat@4.40.0; `errors.ts`, `graphFetchJson`).

- **`send_template(thread_id, template)`** posts `type: "template"` with
  `template: {name, language: {code}, components?}`. `components` is sent only
  when non-empty (`if template.get("components")`, like `components?.length`).
  Emoji placeholders are converted only in `type == "text"` parameters of
  every component; payloads, URLs and media references stay literal. New dicts
  are built, so the caller's template is not mutated. The template TypedDicts
  (`WhatsAppTemplateMessage`, `WhatsAppTemplateComponent`,
  `WhatsAppTemplateParameter`, `WhatsAppTemplateButtonParameter`) are
  snake_case wire shapes, as upstream's are.
- **`WhatsAppApiError(message, status, body)`** subclasses
  `AdapterError(message, "whatsapp", code)`, so existing `except AdapterError`
  handlers still catch it. **Casing:** upstream's camelCase fields
  `errorCode`, `providerMessage`, `traceId` are `error_code`,
  `provider_message`, `trace_id` here; `status`, `type`, `details`, `subcode`
  and `raw` keep their names. The message is `"{label}: {status}
  {error.message ?? body}"` (a body without a Meta message is cut to 500
  characters plus `…`; `raw` keeps it whole). `code` maps onto the shared
  taxonomy exactly as upstream `taxonomyCode` does.
- **`_integer` and JS numbers:** `bool` is rejected (it subclasses `int`;
  JSON `true` is not a JS number). An integral float such as JSON `4.0` or
  `1e3` is accepted as an `int`, because `JSON.parse` yields the JS number `4`
  and `Number.isInteger(4)` holds. Numeric strings must fully match ASCII
  `-?[0-9]+` (JS `\d` has no Unicode digits, and `$` does not match before a
  trailing newline). A numeric string past CPython's 4300-digit `int()` limit
  reads as absent (upstream's `Number()` gives `Infinity`, which matches no
  taxonomy code).
- **JSON parsing (`parse_json_text`)** is shared by `WhatsAppApiError` and the
  `_graph_fetch_json` success path, and follows `JSON.parse`: `NaN` /
  `Infinity` are rejected (error bodies stay text in `raw`; success bodies
  raise `NetworkError`), an integer literal past the `int()` digit limit reads
  as a float like a JS number, and **Python-specific** nesting deep enough to
  raise `RecursionError` in CPython's recursive scanner is treated as invalid
  JSON, so it still surfaces as `WhatsAppApiError` / `NetworkError`.
- **`_graph_fetch_json`** (upstream `graphFetchJson`) backs `_graph_api_request`
  (label `"WhatsApp API error"`) and the `download_media` metadata GET (label
  `"Failed to get media URL"`, which used to raise `RuntimeError`), and
  `_graph_api_upload` (label `"WhatsApp API upload error"`, #238). The
  binary download step moves to the shared downloader in #239.
  - **Status range:** success is any 2xx, like `response.ok`. The old port
    accepted only 200, so a 201/204 used to raise.
  - The body is read as bytes, decoded like WHATWG `Response.text()` (UTF-8
    with replacement, one leading BOM stripped: `utf-8-sig`), and parsed with
    `parse_json_text`. aiohttp's
    `response.json()` would reject a non-`application/json` content type and
    `text()` would sniff the charset; `fetch` does neither.
  - **Python-specific:** a transport failure while reading the body
    (`aiohttp.ClientPayloadError`, a timeout) is also wrapped as
    `NetworkError("{label}: request failed")`. Upstream wraps only the
    `fetch()` call, so a body-read failure there escapes as a raw `TypeError`.

Regression coverage: `tests/test_whatsapp_errors.py`,
`tests/test_whatsapp_api.py` (`TestSendTemplate`, `TestGraphApiErrors`,
`TestGraphFetchJsonPythonSpecific`).

### WhatsApp outbound media and CTA URL link buttons (chat@4.34–4.37, #238)

Ports upstream `8bd8a575` (vercel/chat#537, chat@4.34.0; outbound files and
attachments), `09b72e9d` (vercel/chat#736, chat@4.35.0; no duplicate card
title on Card + file posts) and `6abf4807` (vercel/chat#781, chat@4.37.0;
`cta_url` link buttons), as they stand at chat@4.41.1 (with the
`...recipient()` spread from `3e6e866a`). One narrow divergence, in the
multipart filename (see **Upload**); otherwise no behavioral divergence.

- **Names.** `getWhatsAppMediaType` / `validateFileSize` / `WhatsAppMediaType`
  are `get_whatsapp_media_type` / `validate_file_size` / `WhatsAppMediaType`,
  exported from `chat_sdk.adapters.whatsapp`. `uploadMedia`,
  `sendMediaMessage`, `postMessageWithMedia`, `resolveMedia`,
  `graphApiUpload`, `renderPostableText` and `inferMimeType` are the private
  `_upload_media`, `_send_media_message`, `_post_message_with_media`,
  `_resolve_media`, `_graph_api_upload`, `_render_postable_text` and
  `_infer_mime_type`. `cardToWhatsApp(card, {allowCtaUrl})` is
  `card_to_whatsapp(card, *, allow_cta_url=True)`; `cardLinkButtonLines` is
  the public `card_link_button_lines`. The `caption` / `filename` /
  `recipient` parameters of the new private helpers are keyword-only, so #239
  can add `reply_id` beside them.
- **Upload.** An `aiohttp.FormData` (`messaging_product=whatsapp`, then
  `file` with the filename and MIME type) is posted through
  `_graph_fetch_json`; aiohttp writes the multipart `Content-Type` and
  boundary. Media resolve concurrently (`asyncio.gather`, like
  `Promise.all`) and are sent one at a time, in order. The part is
  serialized like Node's WHATWG `FormData`: `FormData(quote_fields=False)`
  keeps spaces and non-ASCII raw (aiohttp's default would percent-encode
  them), the filename has only LF / CR / `"` escaped as `%0A` / `%0D` /
  `%22`, and the part `Content-Type` follows `Blob` type rules (lowercased;
  any character outside U+0020–U+007E gives `application/octet-stream`). The
  document message keeps the unescaped filename. **Divergence:** aiohttp
  rejects every other C0 control and DEL in a part header with a bare
  `ValueError`, where Node sends them raw, so those are percent-escaped too;
  and aiohttp writes a backslash as `\\` (quoted-string form) where Node
  writes it raw.
- **Truthiness.** Upstream's `Buffer` is truthy even when empty, so
  `FileUpload(data=b"")` is still uploaded, and `Attachment(data=b"")` is
  uploaded rather than falling back to its URL (`is None` checks, not
  truthiness). A link attachment's `size` is checked whenever it is a number,
  including `0`. The `"File upload data is empty"` / `"Attachment data is
  empty"` guards are unreachable here, as they are upstream (`to_buffer`
  raises `"Unsupported file data type"` first).
- **JS string semantics.** The 1024 caption limit compares the UTF-16 length
  (`text.length`), so astral characters count twice. The `cta_url` blank-label
  check strips the JS `trim` whitespace set (`JS_WHITESPACE`, which includes
  U+FEFF), and the `http(s)` scheme test is ASCII case-insensitive
  (`re.IGNORECASE | re.ASCII`; without `re.ASCII` Python folds U+017F to `s`,
  which a JS regex without the `u` flag does not).
- **Types.** `WhatsAppInteractiveMessage` stays one `total=False` TypedDict
  (`type` is `"button" | "list" | "cta_url"`) rather than upstream's union
  discriminated on `type`, because pyrefly does not narrow TypedDict unions on
  a tag; the `cta_url` action is modelled by `WhatsAppCtaUrlAction` /
  `WhatsAppCtaUrlParameters`. `WhatsAppMediaUploadResponse` is new.
- **Kept as upstream (not divergences).** A text-fallback card posted with
  media is captioned with the shared `card_to_fallback_text`, which omits
  `image_url` and image children (the full `card_to_whatsapp_text` is sent
  only when that caption is empty). `cta_url` eligibility checks element
  types, not lengths, so a title over 60 or a body over 1024 characters is
  truncated in the interactive envelope rather than kept as text. Both match
  chat@4.41.1 (`index.ts` `postMessageWithMedia`, `cards.ts`
  `findPromotableCtaLink` / `buildInteractiveEnvelope`).
- `_extract_reply_buttons` already returned `None` (never `[]`) for an actions
  row without reply buttons; `card_to_whatsapp` now tests `is not None`, like
  upstream's `if (actionButtons)`.
- `FileUpload` and `Attachment` are told apart with `isinstance(item,
  FileUpload)` instead of upstream's `"filename" in item`; postable files and
  attachments are dataclasses in Python, as the other adapters assume.

Regression coverage: `tests/test_whatsapp_api.py` (`TestPostMessageFileUploads`,
`TestGetWhatsAppMediaType`, `TestOutboundMediaPythonSpecific`, and the
`media uploads` row of `TestGraphApiErrors`), `tests/test_whatsapp_cards.py`
(`TestCardToWhatsApp` cta_url cases, `TestCtaUrlPythonSpecific`).

### Postgres state: expired claims and migration-owned schemas (chat@4.35–4.41, #240)

Parity with upstream `d88789c9` (vercel/chat#636, chat@4.35.0) and `ea025af7`
(vercel/chat#913, chat@4.41.0).

- `set_if_not_exists` uses upstream's conditional upsert:
  `ON CONFLICT (key_prefix, cache_key) DO UPDATE ... WHERE
  chat_state_cache.expires_at IS NOT NULL AND chat_state_cache.expires_at <=
  now() RETURNING cache_key`. An expired row is reclaimed in the same
  statement, while live and permanent (`expires_at IS NULL`) rows are never
  overwritten. The result comes from `fetchval(...) is not None`, not from the
  command tag. Checked with asyncpg against PostgreSQL 16 (#240 "verify
  first"): absent → tag `INSERT 0 1` / `fetchval` = key; live → `INSERT 0 0` /
  `None`; expired → `INSERT 0 1` / key; permanent → `INSERT 0 0` / `None`. The
  tags agree too, but `RETURNING` is the less ambiguous signal. `ttl_ms` stays
  falsy-means-permanent (`0` → no expiry), as upstream's `ttlMs ? … : null`.
- `auto_create_schema=True` (keyword-only, default) on `PostgresStateAdapter`
  and `create_postgres_state` mirrors `autoCreateSchema`. With `False`,
  `connect()` runs `SELECT 1` and then one probe query (`_SCHEMA_PROBE`, built
  from `_TABLE_PRIVILEGES` plus the `seq` sequence checks), and never issues
  DDL. `_TABLE_PRIVILEGES` was re-derived from this module's SQL and matches
  upstream's map exactly (conflict targets, `WHERE` clauses and `RETURNING`
  lists need column `SELECT`; `set` / `set_if_not_exists` / `acquire_lock` /
  `extend_lock` / list-TTL refresh need the listed `UPDATE` columns; every
  table is deleted from). The flag flows through the `url` argument, the
  `POSTGRES_URL` / `DATABASE_URL` fallbacks and an injected `pool`, and
  `None` means `True` (upstream `autoCreateSchema ?? true`). An
  adapter-created pool is closed on `disconnect()`, an injected one never is.
  `test_probes_only_the_privileges_each_table_needs` pins every
  `has_column_privilege` term against a literal copy of upstream's map.
- `POSTGRES_SCHEMA_STATEMENTS` (a `tuple[str, ...]`, exported from
  `chat_sdk.state.postgres` and `chat_sdk.state`) mirrors
  `postgresSchemaStatements`: the same nine statements, in the same order. The
  README's "Migration-owned schema" SQL block is kept identical by
  `tests/test_state_postgres.py::TestPostgresStateSchemaInitialization::test_keeps_the_published_migration_in_sync_with_the_statements_connect_runs`.
- The opt-in live suite (`TestPostgresMigrationOwnedSchemaIntegration`, ported
  from `postgres.integration.test.ts`) runs only when `POSTGRES_TEST_URL`
  points at a disposable database. It never falls back to `POSTGRES_URL`. It
  needs `asyncpg` installed, which the `dev` group does not include (e.g.
  `uv run --with asyncpg pytest ...`).

Three Python-surface adaptations, all listed under
[Known Non-Parity](#known-non-parity-with-typescript-sdk):

1. **Error type.** Upstream throws a plain `Error`. Python raises
   `chat_sdk.StateSchemaError(ChatError)`. The message is the same
   (`"PostgreSQL state schema is not ready: …"` naming the first relation
   PostgreSQL cannot resolve, or every object whose grants are missing, then
   the hint), except that the hint names the Python option
   (`auto_create_schema=True`). A probe failure, such as a missing table, is
   chained as `__cause__` (upstream `{ cause }`).
2. **Concurrent `connect()`.** Upstream shares one in-flight promise, so
   concurrent callers all reject together when it fails. Python serializes
   `connect()` on an `asyncio.Lock`, so a caller queued behind a failed
   attempt retries it. This behavior predates #240 and is kept. The upstream
   test "retries a failed connectivity check without DDL" is ported as a
   sequential retry, and
   `test_concurrent_connect_retries_after_a_failed_connectivity_check` pins
   the Python behavior.
3. **Owned pool closed on a failed `connect()`.** Upstream's lazy `pg.Pool`
   keeps at most one idle client, which times out, and its `disconnect()` is
   a no-op until `connect()` succeeds. asyncpg's `create_pool` opens
   `min_size` (10) connections eagerly and keeps them, so the same shape
   leaked a full pool per failed attempt (for example `StateSchemaError` in a
   startup retry loop). When `connect()` fails, including by cancellation,
   Python closes a pool that attempt created and resets it, so the next
   attempt builds a fresh one. An injected pool is never closed. Pinned by
   `test_closes_the_owned_pool_when_connect_fails`.

### Card and modal builders (chat@4.34–4.41, #202)

Parity, not a divergence. The core slice of upstream `4717a384` (chat@4.34.0),
`0153a39f` (chat@4.36.0), `4a0b5c0c` (chat@4.40.0), `84219537` and `ad904325`
(chat@4.41.0) is ported in `cards.py` / `modals.py`: `Chart()` and the
`Chart*` TypedDicts, `Table()` `caption` / `page_size` / `widths` /
`vertical_align` / `grid_lines` / `grid_style`, `tooltip` on
`Button()` / `LinkButton()`, `Card(width=…)`, `DateInput()` / `NumberInput()`,
and `dispatch_action` on `Select()` / `RadioSelect()`. Adapter rendering lands
separately (Slack #212, Teams #220); other adapters ignore the new fields.

- **Omitted keys.** Upstream assigns `undefined` options and `JSON.stringify`
  drops them. The Python builders omit a key whose argument is `None`, and keep
  falsy values (`grid_lines=False`, `decimal=False`, `dispatch_action=False`,
  `NumberInput(initial_value=0, min=0)`). With the new options unset,
  `Card()` / `Table()` / `Button()` / `LinkButton()` / `Select()` /
  `RadioSelect()` output is unchanged.
- **Keys** are snake_case inside the SDK (`page_size`, `vertical_align`,
  `grid_lines`, `grid_style`, `initial_value`, `dispatch_action`, `x_label`,
  `y_label`); adapters convert at the wire boundary.
- **Chart fallback numbers.** `chart_element_to_fallback_text` (upstream
  `chartElementToFallbackText`, `markdown.ts`) formats each value as JS
  `String(value)` does, via `cards._js_number_to_string`, an implementation of
  ECMAScript `Number::toString` over `repr(float)`'s shortest round-trip
  digits: `45.0` → `"45"`, `0.00001` → `"0.00001"`, `1e21` → `"1e+21"`,
  `-0.0` → `"0"`, NaN → `"NaN"`. It was checked against Node's `String()`
  on 80,000 random doubles with no mismatch. `int` values below `1e21` render
  exactly (JS would round above 2**53); other numeric types (`Decimal` from
  Postgres `NUMERIC`, `Fraction`, NumPy scalars) are converted to `int` /
  `float` first, so `Decimal("45.00")` → `"45"`; `bool` renders
  `"true"` / `"false"`.
  A series point is found by category label, and a missing point is an empty
  cell.
- **`chart` fallback wiring.** `card_child_to_fallback_text` (and therefore
  `card_to_fallback_text`, `BaseFormatConverter.render_postable`, and each
  adapter's unknown-child fallback) and `shared/card_utils.py` render a chart
  as its title plus an ASCII table. Adapters whose unknown-child branch uses
  the core card fallback (Slack, Teams, Google Chat, Discord, GitHub, Linear,
  Twilio) therefore post a chart as text. Messenger and WhatsApp drop chart
  children silently, as upstream does (their `cards.ts` `default` branch
  returns `[]`).
- **Slack rendering** landed in #212 (see *Slack data tables, charts and modal
  inputs* below).
- **Callback-URL button copy (#194).** Upstream `4a0b5c0c` also changed the
  callback-token swap to keep every button field except `callbackUrl`. That
  half of the commit is ported by #194, so a `Button(callback_url=…)` keeps
  its `tooltip` (see *Callback-URL tokens* above).
- **Not ported (JSX):** `fromReactElement` / `fromReactModalElement` handling of
  `Chart`, `DateInput`, `NumberInput`, `tooltip`, `width` and `dispatchAction`,
  and `929878b5` (chat@4.39.0, link-button ids in JSX). See the jsx-runtime row
  in the non-parity table.

### Conversation context and agent-tool scoping (chat@4.36–4.40, #195)

Parity, with one Python-only divergence (*Link-preview fence slicing* in the
non-parity table). Ports `c5d86b10` (vercel/chat#751, chat@4.36.0),
`85e3d22b` (#774, chat@4.37.0), the core half of `500b7e6d` (#857,
chat@4.39.0), the `getUser` / `startTyping` / drain / link-fence parts of
`b7c9316b` (#875, chat@4.40.0) and `16ea171e` (#848, chat@4.39.0).

- **Active conversation.** `chat_sdk.context` (internal, not re-exported from
  the root, as upstream) holds a `ContextVar[str | None]`.
  `conversation(cid)` is upstream's `runInConversation`: a `None` id runs the
  block bare (it inherits the current value); otherwise it sets the var and
  resets it in `finally` in the same task. `active_conversation()` is `None`
  outside handlers.
- **Wrapped dispatch paths.** Each wrap is entered *inside* the coroutine that
  runs the handlers, because `asyncio.create_task` copies the context when the
  task is created. Tasks a handler spawns inherit the conversation, as with
  Node's async context.
  - `handle_incoming_message` (message thread id; covers `process_message` and
    the `drop` / `queue` / `debounce` / `burst` / `concurrent` strategies).
  - `process_reaction`, `process_action`, `process_assistant_thread_started`,
    `process_assistant_context_changed` (event thread id).
  - The slash-command handler loop, after the channel is resolved, and
    `process_app_home_opened` / `process_member_joined_channel` (channel id).
  - `process_modal_submit` / `process_modal_close`: `related_thread.id`, then
    `related_channel.id`, then bare.
  - Queue and debounce drains run in the lock holder's task, so each drained
    message re-enters its **own** thread id (a channel-scoped lock drains
    other threads' messages).
  - `process_options_load` stays unwrapped, as upstream.
  - The `deduplicate=False` bypass in `process_message` (#191) wraps its
    direct `_route_incoming_message` call in `conversation(thread_id)`, as
    upstream `processMessage` does.
- **Scope guard.** `chat_sdk.ai.scope.create_scope_guard(chat, scope, strict)`
  mirrors `createScopeGuard`: `scope=False` returns `None` (opt out);
  otherwise each call resolves the explicit scope, else
  `active_conversation()`. With neither, the call is allowed and the guard
  warns once through `chat.get_logger()` (the flag lives in the closure, per
  toolset). A call is in scope iff it resolves to the same channel
  (`adapter.channel_id_from_thread_id`, falling back to the first two `:`
  segments) and, under `strict`, the scope is a channel or the id is the
  scoped conversation. A rejection raises `ChatError` with upstream's text
  (upstream throws a plain `Error`).
- **Guarded tools.** `create_chat_tools(..., scope=None, strict_scope=False)`
  calls the guard before any platform call in the six read tools,
  `startTyping`, `postMessage`, `postChannelMessage`, `editMessage`,
  `deleteMessage`, `addReaction`, `removeReaction`, `subscribeThread` and
  `unsubscribeThread`. `sendDirectMessage` and `getUser` take user ids and
  are gated by approval instead; `getUser` now defaults to
  `needs_approval=True` (`ChatApprovalToolName`). The standalone factories
  take an optional guard (`ToolOptions.guard` for write tools) and are
  unguarded without one, as upstream.
- **Link fence.** `to_ai_messages` renders link previews with upstream's
  `renderLinkForPrompt`: url/title/description/site are whitespace-normalized
  and bounded (2048/300/1000/100), metadata is `&`/`<`/`>`-escaped and
  re-bounded, and the metadata lines sit inside
  `<untrusted-third-party-link-metadata>` fences. Normalization uses the JS
  `\s`/`trim` code-point set, not Python's.
- **Channel edit thread id (`16ea171e`).** A channel `SentMessage.edit()`
  returns a message carrying the adapter-reported thread id instead of the
  channel id.
- Out of scope here: the web half of `500b7e6d` (no `WebAdapter`; see the
  `adapter-web` row), lifecycle and agent-session event wraps (#196, #201),
  `chat.history`-backed read tools (#197), link-/attachment-only message
  retention (#198) and the thread `SentMessage.edit` thread id (#200).

### Chat lifecycle: init retry, dedupe TTL, `wait_until` errors (chat@4.33–4.41, #191)

Parity with the core halves of upstream `0b63791b` (chat@4.33.0), `f233ffe8`,
`c21ccbc0` and `91683e52` (chat@4.41.0).

- **Init retry (`f233ffe8`, adopted as upstream).** `_ensure_initialized`
  shares one attempt task between concurrent callers. The attempt awaits
  `state.connect()` and then `_do_initialize()`. If `connect()` fails, the
  attempt clears `_init_promise`, but only when it is still the current attempt
  (`self._init_promise is asyncio.current_task()`, upstream's
  `this.initPromise === attempt`), so a failing pre-`shutdown()` attempt cannot
  clear a newer one. An adapter `initialize()` failure stays cached and is
  re-raised to every caller until `shutdown()` clears it. The old Python
  behavior (retry on any failure) re-ran `initialize()` on adapters that had
  already started: Teams re-registered its handlers, and Slack Socket Mode could
  start a second connection. Trade-off: a transient adapter-init failure wedges
  the instance until `shutdown()`. `_do_initialize` logs it at error level
  (upstream only rejects). Python-specific: callers await the attempt through
  `asyncio.shield`, so a cancelled caller (an aborted webhook request) does not
  cancel the attempt other callers share. A done-callback marks the attempt's
  exception retrieved, so a failure nobody is left awaiting (every caller
  cancelled) does not trigger asyncio's "Task exception was never retrieved".
  The attempt is built with `asyncio.Task(...)`, not `loop.create_task`, so an
  eager task factory cannot finish a failing `connect()` before
  `_init_promise` is assigned (which would cache the failure). `shutdown()`
  forgets but does not cancel an in-flight attempt, as upstream.
  The redundant `_init_lock` is gone;
  there is no `await` between the check and the assignment.
- **Dedupe TTL.** `DEDUPE_TTL_MS` is 10 minutes (was 5), so an entry outlives
  Slack's ~+5 min Events API retry. `ChatConfig.dedupe_ttl_ms` now defaults to
  `None` and resolves with `is not None` (upstream `??`). Before this change the
  dataclass default `300000` always won and `0` fell through `or` to the
  constant. `0` now reaches the state adapter, and the bundled backends treat a
  `0` TTL as no expiry (upstream `state-redis` does the same). The Slack Socket Mode retry-envelope half of `0b63791b` is #209.
- **`wait_until` and handler errors (`c21ccbc0`, `91683e52`).** Upstream hands
  `waitUntil` a `task.catch(log)` promise that fulfils when the handler fails.
  Python passed the raw task, so a host that awaited it saw handler errors
  (effectively upstream's opt-in mode). Now `Chat._tracked(task)` builds the
  equivalent: a real `asyncio.Task` (the Teams DM gate below needs a Task) that
  awaits `asyncio.shield(task)` and swallows the handler's exception, which the
  handler task's done-callback has already logged. Cancelling the wrapper leaves
  the handler running. A cancelled handler counts as completion, and
  `shutdown()` cancels both. `WebhookOptions.propagate_handler_errors=True`
  hands `wait_until` the raw task for message, action and slash-command. Reaction
  and the lifecycle `process_*` methods (modal close, assistant thread
  started/context changed, app home opened, member joined) always hand over the
  wrapper, as upstream's `.catch`ed lifecycle tasks do. The modal-submit
  callback-URL task already catches its own errors and is passed as is.
  `process_reaction` / `process_action` / `process_slash_command` return the raw
  handler task (`None` without a running loop), which raises on handler failure.
- **`WebhookOptions.deduplicate=False` (`91683e52`).** `handle_incoming_message`
  is split into `_route_incoming_message(..., deduplicate=True)` (self-filter,
  then dedupe) and `_dispatch_incoming_message` (history, lock key, strategy).
  `process_message` skips the `dedupe:` claim when `options.deduplicate is
  False` (`None` dedupes). Telegram polling adopts it in #227. The bypass
  runs inside `conversation(thread_id)`, as upstream's `runInConversation`
  (#195).
- **Not ported:** the Slack agent-view bridge rethrow in `c21ccbc0` (#214) and
  the Teams `handleDialogOpen` parts of `c21ccbc0` / `91683e52` (see the Teams
  dialog-open row in the non-parity table).

### Slack data tables, charts and modal inputs (chat@4.34–4.41, #212)

Parity, apart from the length-counting row in the non-parity table. Ports the
Slack half of upstream `4717a384` (chat@4.34.0), `0153a39f` (chat@4.36.0) and
`ad904325` (chat@4.41.0) into `slack/cards.py`, `slack/modals.py`,
`slack/adapter.py` and the SDK-free `slack/blocks` subpath.

- **Tables.** A card `Table()` with at least one data row renders as a
  paginated, sortable `data_table` block (`caption` defaults to `"Table"`,
  also for `caption=""` as with upstream's `||`; `page_size` is sent only when
  set, floored and clamped to 1–100). A header-only table keeps the plain
  `table` block. More than 100 rows, 20 columns or 10,000 combined cell
  characters, or a second table in the message, falls back to an ASCII code
  block, now cut to Slack's 3,000-character section limit with a `…` and the
  closing fence kept. In `slack.blocks`, `column_settings` (from `align`) is
  kept only on the header-only `table` block; `data_table` has no such field.
- **Charts.** `Chart()` renders as a `data_visualization` block. A chart that
  breaks a Slack constraint (title 1–50 chars after emoji conversion, labels
  1–20, 1–12 segments or series, 1–20 unique categories, one point per
  category, positive pie values, axis labels ≤ 50) or is the third valid chart
  in a message falls back to the chart's fallback text in a code block. An
  invalid chart does not use up the two-chart budget. Series points are
  reordered to category order. `slack.blocks` formats fallback values with
  `cards._js_number_to_string` (JS `String(n)`), like the core fallback.
  Values go into the block through `cards._chart_value_to_json_number`:
  `Decimal`, `Fraction` and NumPy scalars become `int` / `float` (JS numbers
  always serialize; these raise `TypeError` in `json.dumps`), and a value that
  is not a finite number makes the chart fall back to text (upstream would
  send `null` for `NaN`, which Slack rejects).
- **Modals.** `DateInput` → `datepicker`: an `initial_value` that is not a
  real `YYYY-MM-DD` date is dropped with a `logging` warning (the module
  logger `chat_sdk.adapters.slack.modals`), since Slack fails the whole
  `views.open`. The check is `re.fullmatch("[0-9]{4}-[0-9]{2}-[0-9]{2}")` plus
  a `date.fromisoformat` round trip, so impossible dates raise instead of
  rolling over as JS `Date` does. `NumberInput` → `number_input`:
  `is_decimal_allowed` is always sent (default `False`), and
  `initial_value` / `min_value` / `max_value` are sent as JS `String(n)`
  strings (`1.0` → `"1"`, `0` kept). `Select` / `RadioSelect` input blocks carry
  `dispatch_action` only when it is not `None` (`False` is sent). The existing
  `block_actions` path for `view` containers delivers the selection change to
  `on_action`.
- **View submission** values resolve `value` → `selected_date` →
  `selected_option.value` → `""` with `is not None` checks (upstream `??`). A
  cleared text input now submits `""` instead of falling through to another
  key. A `block_actions` value is likewise `selected_option.value` → `value`
  with `is not None` (upstream `selected_option?.value ?? value`).
- **`invalid_blocks`.** When `chat.postMessage` for a card fails with
  `invalid_blocks`, `post_message` logs `"Slack rejected blocks
  (invalid_blocks)"` at error level with Slack's `errors` and
  `response_metadata.messages` and the blocks, then raises
  `AdapterError("Slack rejected blocks (invalid_blocks): [...]", "slack",
  "invalid_blocks")` with the original error as `__cause__`. Upstream throws a
  plain `Error` with `cause`; `AdapterError` is this SDK's adapter error base.
  Only the card path of `post_message` is wrapped, as upstream.

### Teams Adaptive Card 1.5 rendering (chat@4.36–4.41, #220)

Parity, not a divergence. The Teams half of `0153a39f` (chat@4.36.0),
`4a0b5c0c` (chat@4.40.0) and `84219537` (chat@4.41.0) is ported in
`teams/cards.py`, `teams/cards_input.py` and `teams/modals.py`. Python's
`teams/cards.py` is the single converter behind both the adapter and the
SDK-free cards-primitives surface, so upstream's two table converters (and the
`it.each` that keeps them in step) collapse into one.

- Every Teams card, input-request card and modal card declares `version: "1.5"`.
- `Table` renders as the native Adaptive Card `Table`: one column definition
  per column (weight from `widths`, else 1; `horizontalCellContentAlignment`
  from `align`), rows padded to the widest row, a `weight: "Bolder"` header
  row when `headers` is non-empty, `firstRowAsHeaders` (plural, deliberately),
  `showGridLines` (default `True`; an explicit `False` wins), and `gridStyle` /
  `verticalCellContentAlignment` when set. A table with no columns emits
  nothing. `card_to_fallback_text` is unchanged.
- **Column weights.** Upstream accepts `Number.isInteger(w) && w > 0`. Python
  rejects `bool` (an `int` subclass, not a number upstream) and accepts an
  integral float such as `2.0`, emitted as the int `2`, since JS has one number
  type and serializes `2.0` as `2`.
- Buttons and link buttons emit `tooltip` (emoji-converted) when it is
  truthy. `Card(width="full")` sets `msteams: {"width": "full"}`.
- The modals primitive renders `date_input` as `Input.Date` and
  `number_input` as `Input.Number` (`max` / `min` / `initialValue` whenever
  present, so `0` is kept; `placeholder` only when truthy). Keys are the
  primitive's literal camelCase.
- **Dialog submit numbers.** `parse_teams_dialog_submit_values` stringifies
  numbers as JS `String(value)` does, via `cards._js_number_to_string`
  (`5.0` → `"5"`, `1e21` → `"1e+21"`, and `1e400` or an over-long int
  literal → `"Infinity"`, as `JSON.parse` yields `Infinity` for both). Every
  `int` / `float` is kept, as upstream keeps every `typeof value === "number"`
  (NaN, which Python's `json` accepts, renders as `"NaN"`).
  `bool` is dropped, as upstream drops every non-number.
- **Primitive emoji.** Upstream's plain-object converter resolves Slack-style
  `:white_check_mark:` shortcodes in cell text and tooltips; the shared
  Python converter resolves the SDK's `{{emoji:…}}` placeholders, as it
  already did for every other card text before #220.
- **No adapter modal path.** Upstream's SDK-bound `modals.ts` also renders the
  core `DateInput` / `NumberInput`. The Python adapter never converts core
  modals to cards (see the "Teams dialog/modal inbound" row in the non-parity
  table), so only the primitive gains the new children.

## What to Port vs What to Adapt

### Port 1:1

These must stay structurally identical to the TS SDK:

- **Type definitions** (`types.py`): All dataclass shapes, protocol methods, and event types must match TS. This is the interop contract.
- **Concurrency strategies** (`chat.py`): The drop/queue/debounce/burst/concurrent logic, lock TTLs, and dedup keys must produce identical behavior.
- **Card element types** (`cards.py`): The TypedDict shapes must match TS so that platform card renderers produce the same output.
- **Thread ID encoding/decoding**: Each adapter's `encode_thread_id` / `decode_thread_id` must produce the same strings as TS for cross-language state sharing.
- **State key prefixes**: `thread-state:`, `channel-state:`, `dedupe:`, `modal-context:` must match.
- **Webhook signature verification**: Must use the same algorithms and constant-time comparison as TS.

### Adapt for Python

These are intentionally different from TS:

- **Async model**: TS uses Promises; Python uses `async/await` with `asyncio`. `Promise.all()` becomes `asyncio.gather()`. `setTimeout()` becomes `asyncio.sleep()`.
- **Module structure**: TS uses one file per package. Python splits into modules (`adapter.py`, `cards.py`, `format_converter.py`, `types.py`) per adapter.
- **Error hierarchy**: Python uses exception classes instead of TS error strings.
- **Type system**: TS interfaces become `Protocol` classes. TS unions become `Union` types or `|` syntax. TS generics become `TypeVar`.
- **Optional dependencies**: TS uses package dependencies. Python uses extras (`pip install chat-sdk[slack]`) with lazy imports.

## Architecture Decisions That Must Stay 1:1

1. **Chat resolver**: Thread/Channel deserialization needs a Chat instance for adapter resolution. Python uses a 3-level resolver (explicit `chat=` → `ContextVar` → global fallback) rather than TS's pure global. The `register_singleton()` API is preserved for upstream parity, but `chat.activate()` and `from_json(data, chat=chat)` are preferred in Python.

2. **Thread ID format**: `{adapter}:{platform_id}` (e.g., `slack:C123:1234567890.123456`). State keys depend on this format. Changing it would break cross-language state sharing in deployments that mix TS and Python bots.

3. **Lock token format**: `{backend}_{timestamp}_{random}`. The token must be a cryptographically random string for security. The format itself is not critical for interop, but the lock key format (`dedupe:{adapter}:{message_id}`) must match.

4. **Concurrency strategy semantics**: Queue drain order, debounce superseding (superseded messages accumulate into `context.skipped`), per-message dispatch thread (a drained message is dispatched under its own `thread_id`, with `skipped` filtered to that thread), lock heartbeat cadence (`DEFAULT_LOCK_TTL_MS / 3`) and `max_lock_lifetime_ms` cap, and TTL handling must match. There is no debounce iteration cap: the Python-only `max_iterations = 20` was removed in #190 (upstream never had one). As upstream, `max_lock_lifetime_ms` bounds the loop: renewal stops at the cap, the lock lapses one TTL later, and `is_ownership_lost()` ends the loop.

   **Parity-accepted races (lock heartbeat, chat@4.41.1 `startLockHeartbeat` / `debounceLoop` / `drainQueue`).** The heartbeat is best-effort liveness, not a fencing token. These windows exist upstream too and are kept on purpose; a review finding about one of them is closed as parity unless it names a Python-specific cause (see the Divergence Policy):
   - Ownership is checked only at the top of each drain/debounce iteration. A forced takeover on another worker is seen at the next heartbeat tick (up to 10 s).
   - A batch collected while the lock was lost is still dispatched. Dequeue is destructive, so the messages are invisible to the new holder; dispatching them is the only loss-free choice (dropping loses them, and handing them back needs an atomic push-front-if-room primitive the `StateAdapter` protocol does not have).
   - A debounce loop that stops on lost ownership drops its accumulated `skipped` context; whatever is still queued goes to the next holder.
   - A message enqueued between the drain's last empty dequeue and `release_lock` waits for the next webhook.
   - `stop()` waits for an in-flight extend without a bound (backend timeouts belong in the state client's configuration). A late extend cannot resurrect a released lock: `extend_lock` compares the token.
   - A busy drain ends when the lock lapses, one TTL after the lifetime cap, not at the cap.
   - Collection is unbounded (dequeue until empty).

5. **Card element type strings**: `"card"`, `"button"`, `"text"`, `"divider"`, `"actions"`, `"fields"`, `"table"`, `"section"`, `"image"`, `"link"`, `"link-button"` must match exactly.

## Python-Specific Hardening

These exist only in the Python port and have no TS equivalent:

- `shared/errors.py`: Typed adapter error hierarchy (`AdapterRateLimitError`, `AuthenticationError`, `ValidationError`, `NetworkError`, `ResourceNotFoundError`, `AdapterPermissionError`). TS throws plain `Error` objects.
- `testing/__init__.py` + `shared/mock_adapter.py`: Test utilities with `MockAdapter`, `MockStateAdapter`, `create_test_message()`.
- `from __future__ import annotations` everywhere: Enables PEP 604 union syntax (`X | Y`) without runtime cost.
- Input validation on adapter config dataclasses (e.g., rejecting empty `signing_secret`).
- `ContextVar`-based request context in Slack adapter (instance variable, not class variable).
- **Webhook log hygiene (#187, ports upstream `fc7df9c4` + the logging parts of `f485255b`).** Webhook handlers log request-shape metadata, never bodies. GitHub logs `{bodyBytes, contentType, eventType, signaturePresent}` (plus `jsonParseStatus: "error"` on invalid JSON). GChat, Slack and the Teams bridge log `"<Platform> webhook received" {bodyLength}`. Byte lengths go through `shared/log_utils.utf8_byte_length` (UTF-8 bytes like `Buffer.byteLength`, `None`→0, lone surrogates never raise). Slack logs `bodyLength` only **after** `verify_slack_request` succeeds, and GitHub logs its metadata once verification has decided, so a rejected request never gets more than size and header metadata into the logs. `Chat` "Incoming message" uses upstream's key set (`adapter`, `thread_id`, `message_id`, `is_bot`, `is_me`) with no author. Two further steps go beyond upstream and are recorded in [Known Non-Parity](#known-non-parity-with-typescript-sdk): WhatsApp raw-body logging, and message-content logs. Linear's raw-body log was Python-only (upstream Linear never had one), so removing it restores parity; its Python-only invalid-JSON error now logs `{bodyBytes, contentType}`.

## Common TS-to-Python Translation Patterns

### Async Patterns

```typescript
// TS
await Promise.all([taskA(), taskB()]);
const result = await new Promise(resolve => setTimeout(() => resolve(x), 1000));
```

```python
# Python
await asyncio.gather(task_a(), task_b())
await asyncio.sleep(1.0)
result = x
```

### Object Construction

```typescript
// TS
adapter.handle_webhook(request, { waitUntil: fn });
```

```python
# Python
adapter.handle_webhook(request, WebhookOptions(wait_until=fn))
```

### Type Guards

```typescript
// TS
if ('markdown' in message) { ... }
```

```python
# Python
if isinstance(message, PostableMarkdown): ...
# or
if hasattr(message, 'markdown'): ...
```

## TS → Python Porting Hazards

These are the highest-risk failure modes when mechanically porting changes from the TypeScript SDK into `chat-sdk-python`. Review this list before merging upstream-derived changes.

### 1. Truthiness Is Not Parity

TypeScript `||` patterns often do not translate directly to Python. In Python, `0`, `""`, and `False` are falsy, so `x or default` can silently change valid values.

```python
# WRONG
limit = options.limit or 50

# RIGHT
limit = options.limit if options.limit is not None else 50
```

Watch for this in: pagination limits, optional IDs, empty text fields, booleans with valid `False`.

Rule: use `is not None` when `0`, `""`, or `False` are valid.

### 2. Snake Case Inside, Camel Case at Boundaries

The TS SDK uses camelCase everywhere. Python should use snake_case internally and only translate at serialization and external API boundaries.

```python
# WRONG
chat.process_action({"threadId": thread_id, "messageId": message_id})

# RIGHT
chat.process_action(ActionEvent(thread_id=thread_id, message_id=message_id, ...))
```

Watch for this in: adapter dispatch objects, modal context payloads, serialized queue/state entries.

Rule: internal Python objects use snake_case; wire format may use camelCase.

### 3. Prefer Explicit Context Over Ambient State

TS tolerates module-global resolution patterns more easily than Python. In Python, explicit context is safer and easier to test.

Current resolver order:
1. explicit `chat=` / `adapter=`
2. `ContextVar` active chat
3. process-global singleton
4. error

Rule: explicit object > `ContextVar` > global fallback.

### 4. Convenience Helpers Must Not Reintroduce Globals

After adding better resolution paths, helper APIs can still accidentally mutate global state if they register singletons internally.

Example risk areas: JSON revivers, deserialization helpers, modal context restoration.

Rule: helpers should pass explicit `chat=self` where possible instead of registering ambient global state.

### 5. Async Task Lifecycle Is Stricter in Python

TS fire-and-forget patterns do not map cleanly to Python. Bare coroutines, untracked tasks, and shutdown races cause real bugs.

```python
# WRONG
asyncio.ensure_future(coro)

# RIGHT
task = asyncio.get_running_loop().create_task(coro)
task.add_done_callback(lambda t: log_error(t.exception()) if t.exception() else None)
```

Watch for: background refresh tasks, webhook-triggered async handlers, shutdown cancellation, garbage collection of unreferenced tasks.

Rule: always create, track, and clean up tasks explicitly.

### 6. Context Propagation Differs From Node

Node async-local patterns do not map 1:1 to Python. `ContextVar` is the right primitive, but task boundaries matter.

Watch for: spawned tasks that should inherit request context, per-request auth/session state, chat resolver activation across concurrent tasks.

Rule: if context matters across task creation, test it explicitly.

### 7. `undefined`, `None`, and Omitted Keys Are Not Equivalent

TS often distinguishes missing keys from `undefined`. Python tends to collapse these unless you are careful.

Watch for: serialization output, adapter payload generation, webhook response bodies, optional config fields.

Rule: omit keys when the TS contract omits them; do not blindly serialize `None`.

### 8. Datetime Semantics Need Explicit UTC

JS `Date` behavior hides many timezone issues. Python does not.

```python
# WRONG
datetime.utcnow()

# RIGHT
datetime.now(tz=timezone.utc)
```

Also: `datetime.fromisoformat()` on Python 3.10 does not accept `Z` suffix or >6 fractional digits. Use the `_parse_iso()` helper from `types.py`.

Rule: always use timezone-aware UTC datetimes.

### 9. Raw Dict Ports Are Fragile

TS code often passes plain objects around. In Python, raw dicts make typos and shape drift easy to miss.

Watch for: `process_*` event calls, adapter dispatch objects, stored queue entries, modal context structures.

Rule: use dataclasses / typed objects for internal event flow.

### 10. Optional Dependencies Must Stay Lazy

TS package imports assume installed dependencies more often than Python can.

```python
# WRONG
from slack_sdk.web.async_client import AsyncWebClient  # top of file

# RIGHT
def _get_client(self):
    from slack_sdk.web.async_client import AsyncWebClient
    return AsyncWebClient(token=self._bot_token)
```

Rule: no optional adapter dependency imports at module top level.

**Sub-rule: prefer official SDKs over hand-rolling.** When an official
maintained SDK exists for a platform, use it as an optional dependency
(per the lazy-import rule above) rather than hand-rolling the wire
format. Hand-rolled wire formats become wide bot-finding surfaces —
every protocol quirk the SDK abstracts becomes a defect waiting to be
flagged in review. See [Review-Loop Discipline](#review-loop-discipline)
item 2 for the cost-accounting from the 4.27 Teams streaming PR.
Justify any hand-roll in the PR description (SDK missing the feature,
unmaintained, Python-version incompatible, etc.).

### 11. Session and Connection Lifecycle Matter More in Python

Ported code often starts with per-request HTTP clients. In Python async code, shared sessions plus explicit cleanup are usually the right design.

Watch for: `aiohttp.ClientSession` creation in hot paths, missing `disconnect()` cleanup, token-refresh races, connection pool churn.

Rule: reuse sessions, lock refresh paths, and close resources on shutdown.

### 12. Security Randomness vs Cosmetic IDs

Some TS code uses random-looking IDs that are only cosmetic. Others are security-sensitive.

Rule: use `secrets` for lock tokens, signatures, secrets, ownership proofs. Casual random suffixes are acceptable only for non-security display IDs.

### 13. Markdown Is a Known Divergence Zone

The Python markdown parser and `StreamingMarkdownRenderer` are intentionally a subset and do not fully match the TS `remark` + `remend` behavior.

Watch for: parser edge cases, streaming repair behavior, table buffering, plain-text fallback generation.

Rule: treat markdown changes as high-risk parity work and run the markdown/streaming test suites.

### 14. Core Parity Is Better Enforced Than Adapter Parity

The fidelity script covers core TS tests well, but adapter behavior is much more vulnerable to drift through real webhook payloads and platform-specific behavior.

Rule: for adapter changes, prefer replay fixtures and recorded payload tests over hand-built mocks whenever possible.

### 15. Type Parity Does Not Guarantee Behavior Parity

Matching names, signatures, and serialized shapes is necessary but not sufficient.

High-risk semantic areas: concurrency strategies, debounce/queue behavior, modal context restoration, webhook verification, message/reaction self-filtering, streaming fallback behavior.

Rule: behavior changes need regression tests, not just matching types.

## Review Checklist for Upstream Ports

Before **opening** an upstream-derived PR (not before merging — see
[Review-Loop Discipline](#review-loop-discipline) for why timing
matters), check:

### Correctness

- [ ] Are any `or default` patterns incorrectly changing valid falsy values?
- [ ] Did any camelCase keys leak into internal Python event/state objects?
- [ ] Did any helper API reintroduce process-global state where explicit context is available?
- [ ] Are all spawned tasks tracked, error-handled, and safe on shutdown?
- [ ] Are `ContextVar`-dependent behaviors covered by tests?
- [ ] Are optional keys omitted correctly instead of serialized as `None`?
- [ ] Are all datetimes timezone-aware UTC?
- [ ] Are optional deps still lazily imported?
- [ ] Are shared HTTP sessions/tokens/caches lifecycle-safe?
- [ ] Is any randomness security-sensitive?
- [ ] Did markdown or streaming behavior change?
- [ ] Does this need replay coverage rather than only unit coverage?

### Review-loop economics

- [ ] **Did I run [`docs/SELF_REVIEW.md`](SELF_REVIEW.md) before opening?**
      (Catches what bots would flag in rounds 2–5; pays back ~10×.)
- [ ] **Am I hand-rolling a wire format an official SDK provides?**
      If yes, justify in the PR description or use the SDK instead
      (Microsoft `microsoft-teams-apps`, Slack `slack_sdk`, etc.).
- [ ] **Did I trace fix cascades end-to-end?** If a fix in module X
      changes the contract module Y depends on, walk the chain before
      pushing — don't ship and wait for the bot to find Y.
- [ ] **Is this opening as ready-for-review, not draft?** Bots skip
      drafts in this repo's config.
- [ ] **How many in-flight drafts will this make on the sync branch?**
      Cap at 3–4.

## Review-Loop Discipline

Each round of automated review (Codex, CodeRabbit, github-code-quality)
costs ~1–2 hours of wall time per PR: push → bot runs → triage → fix →
push. The 4.27 sync wave averaged 5+ rounds per PR before convergence;
most of that cost was avoidable. Apply the rules below on every sync
PR, not as an after-the-fact patch once a third or fourth review round
hits.

### Before opening the PR

1. **Run [`docs/SELF_REVIEW.md`](SELF_REVIEW.md) first, not after bots converge.**
   Five minutes of honest adversarial review catches what the bots will
   eventually find, in the original commit — eliminating 3–5 sequential
   review rounds. PR #88's formal self-review pass caught two real
   defects Codex had missed across 5 rounds; running it first would
   have caught them in commit 1.

2. **Prefer official SDKs over hand-rolled wire formats.** When an
   official maintained SDK exists for a platform (Slack `slack_sdk`,
   Microsoft `microsoft-teams-apps`, Discord `discord.py`, etc.), use
   it. Each hand-rolled wire format is a wide bot-finding surface:
   every protocol quirk the SDK abstracts becomes a defect waiting to
   be flagged. PR #88's Teams native streaming consumed ~6 of 9 review
   rounds on Bot Framework REST details that `microsoft-teams-apps`
   handles internally. Cost of using an SDK: optional dependency
   surface, possibly a Python-version floor bump. Saved: most of the
   adapter-PR bot-iteration cost.

3. **Trace fix cascades end-to-end before pushing.** When a fix in
   module X might affect downstream consumers in module Y, walk the
   chain before committing. Pushing fix A, waiting for the bot to flag
   B, fixing B, waiting for the bot to flag C is the most expensive
   pattern. PR #88's cancellation-text chain was 5 sequential commits
   because the adapter-level fix kept revealing downstream integration
   gaps in `Thread.stream`. One end-to-end trace before the first
   commit would have collapsed it.

### Opening the PR

4. **Open as ready-for-review, not draft.** Draft mode delays serious
   bot review in this repo's config (CodeRabbit's "Review skipped"
   message appears on every draft PR). Either the PR is ready and you
   want feedback now, or it isn't ready and shouldn't be open. The
   "open as draft and let it bake" pattern bought nothing in the 4.27
   wave — bots didn't engage until PRs flipped to ready anyway, so the
   incubation time was pure lag.

5. **Cap in-flight drafts at 3–4 per sync wave.** Each open PR is its
   own review queue and context switch. Smaller batches ship faster
   than bigger batches. The 4.27 wave had 7 drafts open simultaneously
   for over a week; reducing to 3–4 concurrent and merging in series
   would have shipped sooner.

### After bot findings land

6. **Triage every finding with a clear rubric**:

   | Severity | Action |
   |---|---|
   | **P1** (real defect, exploitable) | Fix today |
   | **P2** (correctness gap, narrow scope) | Fix if small + scope-preserving |
   | **Nit / style** | Batch into a single cleanup commit, OR skip if not a concrete defect |
   | **False positive** | Reply once with rationale; add an in-code comment if the pattern will keep recurring; then stop engaging on re-flags |
   | **Stale** (references prior PR state) | Reply with a brief commit-history pointer; no code change |

7. **Bundle fixes when iterating.** Multiple back-to-back commits
   trigger multiple bot reviews of overlapping content. Squash before
   push when fixing related findings — same outcome, one round-trip
   cost instead of N.

8. **Don't engage every bot re-flag.** `github-code-quality` re-flags
   the same site on every push regardless of prior threads. Reply once
   with the rationale, drop an in-code comment explaining the
   load-bearing semantics (e.g. `await task` inside
   `contextlib.suppress` for deterministic drain), then ignore repeats.
   PR #86 burned context responding to 4+ re-flags of the same false
   positive before this rule was applied.

### Author / agent practices

9. **Detect echoes; stay silent.** Don't reply to your own webhook
   echoes (the system sometimes re-broadcasts a comment you just
   posted). A silent acknowledgment is sufficient.

10. **Parallelize multi-PR triage on day one.** When several PRs are
    in the same review state, dispatch parallel agents (one per PR,
    each with its own `git worktree add`) immediately. The 4.27 wave
    converged 6 PRs in ~1 hour of wall time once parallelized; running
    them sequentially would have taken days.

### Compounding effect

Items (1) + (2) + (6) alone would have shaved most of the lag on
PR #88: from ~9 review rounds spanning days to ~2–3 rounds spanning
hours. The economics never favor "let bots find issues so I don't
have to" — a single sequential push-wait-fix loop costs more than the
self-review that would have pre-empted it.

## Known Non-Parity with TypeScript SDK

Intentional differences from the Vercel Chat TS SDK, collected here so they
stay explicit instead of being rediscovered in code review.

### By design (won't fix)

| Area | Python behavior | TS behavior | Rationale |
|------|----------------|-------------|-----------|
| JSX Card/Modal elements | Not supported; tests skipped | `Card()` returns JSX element | Python has no JSX runtime |
| Channel edit callback scope (4.41 wave, #194) | A channel `SentMessage.edit` binds new callback tokens to `{channel.id, "channel"}`, the scope the original `channel.post` used | `createSentMessage(...).edit` binds to `{threadId, "thread"}`, where `threadId` is the id the adapter reported for the post (or the channel id) | The reported thread id often never equals a click's thread id: Teams and Google Chat report the channel id (upstream as well; Teams clicks carry `;messageid=`, Google Chat clicks carry the thread name), Python's Slack reports the synthetic `slack:C…:` until #209 while clicks carry the message ts, a Slack DM click carries no ts even after #209 makes the post report one, and a chained edit dropped the override until #195 ported `16ea171e`. Upstream's edited buttons never POST in those cases. Every click on the message derives the channel id, and the channel scope is no broader than the original post's. Regression tests: `tests/test_channel_faithful.py::TestCallbackUrlProcessing::test_edited_slack_channel_card_resolves_for_the_real_click` (real Slack id functions and `_handle_block_actions`, channel and DM, before and after #209), `::test_edited_teams_channel_card_resolves_for_a_click_in_that_channel` (real Teams id functions) and `::test_chained_edit_keeps_callback_tokens_resolvable`. To be filed as an upstream issue against vercel/chat (Teams and Google Chat edited channel cards never POST); upstream Slack is unaffected, since its `postChannelMessage` and DM clicks both carry the message ts. |
| Callback-token lease fence (4.41 wave, #194) | After deleting a matched record, `resolve_callback_url` calls `extend_lock(lock, CALLBACK_LOCK_TTL_MS)`. If that fails, the 10 s lease lapsed mid-consume, and the call returns `None` instead of the record (fail closed: no POST, raw `__cb:` value to handlers) | `resolveCallbackUrl` returns the record after `delete` regardless of lease state, so if a `get`/`delete` stalls past the lease, a second click that takes the expired lock also resolves it and both POST | Keeps the single-use contract under state-backend stalls. `extend_lock` checks token ownership and expiry in every backend (Memory, Redis script, Postgres `WHERE token = $4 AND expires_at > now()`), and `Chat` already relies on it for lock heartbeats. The cost is one extra state call per resolved click. A stalled consume that loses its lease also burns the token without a POST. To be filed as an upstream issue against vercel/chat (a stalled `get`/`delete` past the 10 s lease lets a second click double-POST). Regression test: `tests/test_callback_url.py::TestResolveCallbackUrlLocking::test_lost_lease_fails_closed_instead_of_double_consuming`. |
| Link-preview fence slicing (4.41 wave, #195) | `_render_link_for_prompt` bounds url/title/description/site with Python slicing, which counts code points | `renderLinkForPrompt` slices with `String.prototype.slice`, which counts UTF-16 code units | Only differs for astral characters (emoji and the like): a bounded field keeps up to the limit in code points, where JS keeps half as many astral characters and can end on a lone surrogate. Emulating UTF-16 slicing would produce lone surrogates that break UTF-8 encoding of the prompt. Whitespace handling is not a divergence: the normalizer uses JS's exact `\s`/`trim` set. Regression test: `tests/test_ai_messages.py::TestLinkPreviews::test_link_metadata_bounds_count_code_points_not_utf16_units`. |
| Markdown parser | Subset of CommonMark (no setext headings, indented code, HTML, escaped chars, backtick spans >1) | Full CommonMark via remark | See [DECISIONS.md](DECISIONS.md#why-hand-rolled-markdown-parser) |
| `_remend` streaming repair | Parity-based emphasis closing | `remend` npm package | Simplified; handles common cases |
| `walkAst` | Deep-copies the tree (immutable) | Mutates the tree in place | Python convention; safer |
| `ast_to_plain_text` | Joins blocks with `\n` | Concatenates directly | More readable output |
| `renderPostable` on unknown input | Returns `str(message)` | Throws `Error` | More resilient |
| Chat resolver | 3-level: explicit → ContextVar → global | Process-global singleton | See [DECISIONS.md](DECISIONS.md#why-3-level-chat-resolver) |
| PostableObject history | Cached in message history with real message ID | Not cached (skips history) | Upstream gap — posted messages should appear in thread/channel history |
| Teams `msteams` transport key | Stripped from action values | Not stripped | Upstream gap — SDK-injected metadata should not leak to handlers |
| Teams inbound activity routing (issue #93 PR 1) | `BridgeHttpAdapter.dispatch` feeds webhooks through the SDK `HttpServer` (JWT validation), but the adapter **overrides `app.server.on_request`** with `_dispatch_activity` instead of letting the SDK's default router run. Our callback dumps the lenient `CoreActivity` to a camelCase dict and routes by `type` to the existing handler logic. | Upstream `@chat-adapter/teams@4.30.0` registers `app.on("message" / "card.action" / …)` and lets `@microsoft/teams.apps` route via its typed dispatcher; handlers receive `ctx.activity` as a strongly-typed `IMessageActivity` etc. | The Python SDK's default `on_request` (`App._process_activity_event` → `ActivityProcessor.process_activity`) runs `ActivityTypeAdapter.validate_python` (strict per-activity validation — `recipient`/`id` required) **and** a live `api.users.token.get` network call inside `_build_context` before any handler fires. Minimal serverless webhook payloads (and the adapter's dict-based handler logic) can't survive strict validation, and the token fetch would make an unwanted outbound call per inbound activity. We keep the SDK as the **auth + transport** layer (JWT genuinely validated by its `TokenValidator`) but route the already-authenticated activity ourselves through the lenient `CoreActivity`, preserving the exact pre-migration handler behavior. The SDK `on_message`/`on_card_action`/… decorators are still registered for parity/forward-compat. Regression coverage: `tests/test_teams_extended.py::TestActivityTypes`, `tests/test_teams_coverage.py::TestSdkInboundAuth`, `tests/test_teams_bridge.py`. |
| Teams dialog/modal inbound (issue #93 PR 1) | `on_dialog_open` / `on_dialog_submit` are registered on the SDK App but only cache user context (no `process_modal_submit`, no task-module response) | Upstream `@chat-adapter/teams@4.30.0` `handleDialogOpen`/`handleDialogSubmit` drive `chat.processModalSubmit` + `modalToAdaptiveCard` | The pre-migration Python Teams adapter never implemented modal/dialog inbound processing, so PR 1 (inbound + auth plumbing) preserves that behavior rather than introducing new modal handling. Wiring dialogs to `chat.process_modal_submit` is tracked as a later wave of the #93 migration. |
| Teams dialog-open action options (4.41 wave, #191) | N/A: dialog-open inbound is not ported (see the row above), so there is no `process_action` call with `on_open_modal` to spread `WebhookOptions` into or to guard against a rejected action task | `c21ccbc0` spreads `...webhookOptions` into `handleDialogOpen`'s `processAction` call; `91683e52` resolves the empty dialog response when that action rejects | Lands with the dialog inbound wave of the #93 migration. The DM message path does spread the caller's options (see the native streaming row). |
| Fallback streaming with whitespace-only streams (non-Teams adapters) | Placeholder cleared to `" "` on final edit | Placeholder left visible (`"..."` stuck) | Upstream 4.26 guards against empty edits but leaves the placeholder stranded on the message. We issue one final `edit_message(" ")` so the placeholder disappears when no real content was produced. Teams does not route through `_fallback_stream` (DMs stream natively through the SDK `IStreamer`; group chats accumulate-and-post), so this divergence applies only to Slack / Discord / GitHub / Telegram / Google Chat / Linear / WhatsApp. |
| Google Chat `<url\|text>` round-trip | `to_ast()` / `extract_plain_text()` parse the custom-label syntax back to a link node / bare label | `toAst()` / `extractPlainText()` leave `<url\|text>` as raw text (or parse the whole string as an autolink with a malformed URL) | Upstream 4.26 emits `<url\|text>` in `from_ast` but never taught the reverse direction to parse it. A message posted with `[label](url)` then read back through `fetch_messages` comes back as unstructured text (or worse, a link node with the full `url\|text` as its URL) in upstream. We close the round-trip via an AST placeholder substitution: each `<url\|text>` is extracted to a private-use sentinel, Markdown is parsed on the rest, and link nodes are injected where the sentinels landed. This avoids the Markdown parser's incomplete handling of balanced-parens link destinations, so URLs like `https://en.wikipedia.org/wiki/Foo_(bar)` round-trip intact. |
| `from_json(data, adapter=X)` → `_adapter_name` | Updated to `X.name` so `to_json()` reflects the bound adapter | Kept at `json.adapterName`, so re-serialization can emit a name that no longer matches the actual adapter | Upstream TS has the same gap but only exposes it via the `fromJSON(json, adapter?)` overload. In Python we lean on this API more (explicit `chat=` / explicit `adapter=` is preferred over the singleton). We sync the name on rebind so runtime and serialize agree. |
| Google Chat link labels with `\|` / `>` / `]` / newline, empty labels, URLs without a scheme, or URLs containing `\|` / `>` | Fall back to `text (url)` (or bare URL for empty labels) when the `<url\|text>` form can't round-trip safely | Always `<url\|text>`, producing malformed or un-parseable output | Google Chat's `<url\|text>` has no escape for `\|` or `>`; `]` breaks our own `to_ast()` regex (which converts `<url\|text>` to Markdown `[text](url)`, and Markdown closes the label at the first `]`); newline breaks the single-line form; schemeless URLs and URLs containing `\|`/`>` don't match our reverse parser. Upstream emits the malformed form regardless; we fall back to the pre-4.26 `text (url)` form (or the bare URL for empty labels) so the label/URL stays intact and Google Chat's auto-link detection still fires for http(s). |
| Google Chat heading rendering | `#`-headings emit as `*text*` (bold) so they're visually distinct | Falls through to default node-to-text (plain concatenation) | Google Chat has no heading syntax; emitting plain text loses the visual hierarchy. Bold is the closest approximation the platform supports. |
| Google Chat image rendering | Images emit as `{alt} ({url})` or bare `url` | No image branch — falls through to default which concatenates children only, dropping the URL | Upstream silently drops image URLs when rendering to Google Chat text. We preserve the URL so the message content isn't lost. |
| Fallback streaming stream-exception capture (non-Teams adapters) | `_fallback_stream` captures exceptions from the stream iterator, flushes whatever content was already rendered, awaits `pending_edit`, and re-raises after cleanup | `try/finally` only — exception propagates immediately, `pendingEdit` is un-awaited, and the placeholder is stranded as `"..."` | Upstream leaves a hard UX failure when streams crash mid-flight (common: LLM connection drops): placeholder visible forever, orphan background task. We flush + clean up before re-raising so the caller still sees the original error and users see the partial content instead of a spinner. This divergence does not apply to Teams: Teams DMs stream natively through the SDK `IStreamer` (`_stream_via_emit`), and a non-cancel iterator exception propagates straight to the caller while the SDK closes the streamer after the handler returns. |
| Slack `stream()` to a top-level DM (empty `thread_ts`) | Normalizes the empty `thread_ts` to `None` and degrades to a single accumulated `post_message` call so the streamed reply still lands (chat-sdk-python#94) | Passes the empty `thread_ts` straight to `chat.startStream` (`adapter-slack/src/index.ts` `stream()`), which Slack rejects (`invalid_thread_ts`) — the streamed DM reply is silently dropped | Top-level DM messages intentionally encode `threadTs=""` on both sides (`_handle_message_event` / `handleMessageEvent`, "matches openDM subscriptions") — that part is faithful to upstream and **not** a bug. The bug is that upstream's `stream()` never reconciled that legitimate value with `startStream`'s requirement for a non-empty `thread_ts`; `postMessage` accepts no `thread_ts` for DMs, so we degrade instead of erroring. Tracked for contribution upstream — remove this divergence once vercel/chat fixes `stream()` to handle empty-`thread_ts` DM thread ids. |
| Slack `stream()` on Enterprise Grid (`chat.startStream` `team_not_found`) | Threads the workspace `team_id` into `client.chat_stream(...)` (= `options.recipient_team_id`, the `team.id` extracted on the inbound path), which slack_sdk forwards into `_stream_args` → `chat.startStream`. `chat.appendStream`/`chat.stopStream` don't receive `_stream_args` and don't need `team_id`. Harmless on non-Grid workspaces — a correct `team_id` is always valid. (chat-sdk-python#95) | Builds the `chat.startStream` args from `channel`/`threadTs`/`recipientUserId`/`recipientTeamId`/`taskDisplayMode` only (`adapter-slack/src/index.ts` `stream()`); never passes a workspace `team_id`. On Grid orgs `chat.startStream` then fails with `team_not_found` (the per-workspace bot token alone isn't sufficient to disambiguate the team), even though `chat.postMessage` on the same workspace succeeds without it. | Upstream has the same gap — its `stream()` never threads `team_id`, so streaming is broken on Grid while non-streaming posts work. `chat.startStream` requires `team_id` for Grid disambiguation; `chat.postMessage` does not, which is why only streaming regresses. We source `team_id` from the already-plumbed `recipient_team_id` (the workspace where the interaction happened = the streaming target workspace). Live verification needs a real Grid workspace; the unit regression (`tests/test_slack_api.py::TestStream::test_stream_threads_team_id_to_chat_stream_for_grid` + the `team_not_found` mutation guard) simulates Grid by raising `team_not_found` from the streamer's lazy `chat.startStream` when `team_id` is absent. Tracked for contribution upstream. |
| Fallback streaming final SentMessage content (non-Teams adapters) | SentMessage + final edit carry `final_content` (remend'd — inline markers auto-closed) | SentMessage + final edit carry raw `accumulated` | Narrow UX refinement. If a stream ends with an unclosed `*`/`~~`/etc., upstream ships the unclosed marker; we run `_remend` so the user sees a clean final message. Not observable in the common case where streams close their own markers. Teams DMs stream through the SDK `IStreamer` and the Teams accumulate-and-post path ships raw `accumulated` via `post_message`, matching upstream; this divergence applies only to the remaining adapters that still route through `_fallback_stream`. |
| Teams group-chat / channel streaming via accumulate-and-post | `TeamsAdapter.stream` accumulates the full text and issues a single `post_message` (SDK-backed) instead of post+edit, even for group chats and channel threads | Same (`@chat-adapter/teams@4.30.0`: `if (activeStream && !activeStream.canceled) … else { accumulate; postMessage }`) — no divergence at the adapter level | Documented for clarity: the Python port matches upstream's behavior of avoiding the post+edit flicker where Teams doesn't support native streaming. The buffered fallback routes through the same SDK `App.send` path as a normal `post_message`. |
| Teams native streaming via the SDK `IStreamer` (DMs) | `TeamsAdapter._handle_message_activity` captures a Teams SDK `IStreamer` (`microsoft_teams.apps.StreamerProtocol` / `HttpStream`) for DMs via `app.activity_sender.create_stream(ref)` on `microsoft-teams-apps` 2.0.x, or `HttpStream(app.api.from_service_url(ref.service_url), ref)` on 2.1.x, which removed `ActivitySender` (#250), registers it in `_active_streams`, and `await`s a `processing_done` gate (a wrapped `wait_until` shim) so the streamer stays alive while the handler streams. The shim builds its options with `dataclasses.replace(options or WebhookOptions(), wait_until=...)`, so `propagate_handler_errors` / `deduplicate` reach `Chat.process_message` (upstream `{ ...baseOptions, waitUntil }`). Since #191 `wait_until` receives Chat's error-swallowing wrapper Task by default (the raw handler task with `propagate_handler_errors=True`). The gate's `add_done_callback` hooks the handler task that `process_message` returns, not the handed wrapper: a host can cancel the wrapper while the shielded handler keeps streaming, whereas upstream's `.catch` promise cannot be cancelled and always settles with the handler. The handed task is only the fallback when `process_message` returns no Task (pinned by `tests/test_teams_native_streaming.py::TestHandleMessageActivityWithRealChat`). `stream()` → `_stream_via_emit` calls `stream.emit(text)` per chunk and NEVER calls `close()`; the adapter's `_handle_message_activity` `finally` calls `stream.close()` once (the lifecycle-owner role the SDK App's `process_activity` plays upstream). | `@chat-adapter/teams@4.30.0` `index.ts` does exactly this: `this.activeStreams.set(threadId, ctx.stream)`, build `processingDone` + wrapped `waitUntil`, `await processingDone`, `streamViaEmit` calls `stream.emit(text)` and never `close()` (the SDK App auto-closes after the handler returns). | **No adapter-level divergence.** The only mechanical difference is the close call site: upstream lets the SDK `App` auto-close `ctx.stream` because the SDK owns dispatch; our bridge overrides `server.on_request`, so we own dispatch and reproduce the close in `_handle_message_activity`'s `finally`. The SDK `HttpStream.close` no-ops when the stream was canceled or had no content, so closing in both success and cancel paths is safe (matching the SDK App, which closes in both its success and `StreamCancelledError` branches). Cancellation is detected via `stream.canceled` (checked before each emit) and by catching `StreamCancelledError` (other exceptions re-raise). The first chunk id is captured via `on_chunk` and awaited only when text was emitted and the stream was not canceled. Replaces the prior hand-rolled wire format, the 1500ms emit throttle, and the `RawMessage.text` / `update_interval_ms` divergences (all unwound in #93 PR 3). |
| Teams: `microsoft-teams-apps` 2.0.x and 2.1.x both supported (#250) | The `[teams]` extra allows `>=2.0.13,<2.2` for `microsoft-teams-{apps,api,cards,common}` (common is imported directly and apps leaves it unbounded, so it is declared and capped too). SDK differences are feature-detected, never version-sniffed. **Streaming:** `_create_streamer` uses `app.activity_sender.create_stream(ref)` when the App has one (2.0.x) and otherwise builds `HttpStream` on `app.api.from_service_url(ref.service_url)` (2.1.x, mirroring 2.1's `ActivityContext.stream`). Either way the stream owns a client pinned to the inbound service URL, so `_point_app_api_at` retargeting the shared `App.api` for an outbound call cannot redirect an in-flight stream. An SDK with neither entry point falls back to buffered posting. **Edit/delete:** unchanged. `app.api.conversations.activities(id).update/delete` exists on every supported version; on 2.1 it routes through `conversations.update_activity(..., service_url=None)`, which falls back to the retargeted client URL. The flattened `update_activity`/`delete_activity` are not used because 2.0.13.4 (the floor) lacks them. **Inbound auth:** 2.1 replaced the Bot Framework-only `TokenValidator.for_service` with `InboundActivityTokenValidator`, which also accepts Entra ID (Agent 365 "Agent ID") tokens whose audience is the app id, from any tenant (issuer taken from the token's `tid`) and without the `serviceurl` claim check. It also picks the Entra branch from the token's *unverified* `iss`, building a per-`tid` validator and fetching that tenant's JWKS (blocking) before the signature is checked. So `BridgeHttpAdapter.dispatch` calls the adapter's `_rejects_before_auth` hook before the SDK route handler: it decodes the Bearer token without verification and answers 401 unless `iss == app.cloud.token_issuer` (an unreadable token is refused too; the SDK would refuse it). A token that passes still gets the SDK's full validation. `_dispatch_activity` repeats the issuer check on the validated `JsonWebToken` as defence in depth. On 2.0.x the SDK already enforces the issuer; the pre-check only spares the Bot Framework JWKS fetch for tokens it would reject. Requests with no `Bearer` header, and all requests in `dangerously_allow_unauthenticated_requests` / `skip_auth` mode (the SDK ignores the header there), go to the SDK unchanged. That mode includes a configured `webhook_verifier` (#221): the bridge runs the verifier first and the App is built with the skip-auth flag, so the verifier replaces this pre-check just as it replaces the SDK's JWT validation (pinned by `tests/test_teams_connect.py::TestConnectWebhookDispatch::test_verifier_replaces_the_bot_framework_issuer_precheck`) | `@chat-adapter/teams@4.41.1` still depends on `@microsoft/teams.*` `^2.0.14` and calls `ctx.stream` / `app.api.conversations.activities(...)` | Python-only SDK compatibility, not a behavior divergence: on either SDK line the adapter streams, edits and authenticates as it did on 2.0.x. Agent ID activities are not supported by this adapter, so accepting their tokens would only widen who can deliver activities. Pinned by `TestCreateStreamer` (both SDK shapes plus an unstubbed real-SDK test that retargets `App.api` mid-stream), `TestOutboundServiceUrlRouting` (edit/delete checked at the HTTP boundary) and `TestInboundTokenIssuer` (real bridge → SDK validator path with RS256 test-key tokens, only JWKS key resolution stubbed: Entra tokens refused with no JWKS fetched, Bot Framework tokens accepted and still audience/`serviceurl`-checked, sovereign-cloud issuer via `CLOUD=USGov`, the post-validation guard, and unauthenticated mode) and `TestSdkDependencyDeclarations` (every imported `microsoft_teams.*` package declared and capped). CI installs 2.1.x only; 2.0.16 was run locally. **A live Teams check of native DM streaming, edit and delete on 2.1 is still owed before release.** |
| Teams streaming throttle / Bot Framework wire format ownership | The SDK `HttpStream` owns the entire Bot Framework streaming wire format (`streamType`/`streamSequence`/`streamId`), the inter-flush throttle, and 429 retry. We hand it text via `emit()` and read back the assigned id via `on_chunk`. | Same — `@microsoft/teams.apps`'s `IStreamer` owns all of this in the JS SDK. | **THROTTLE PARITY (verified against the installed SDK source — `microsoft-teams-apps==2.0.13.4`):** the SDK throttles and is 429-safe, so we don't regress to rate-limit errors: (1) `http_stream.py:266` — after a flush, if more is queued, the next flush is scheduled via `call_later(0.5, …)`, i.e. a 500ms inter-flush delay (the module docstring at `http_stream.py:39-41` states this is "to ensure we dont hit API rate limits with Microsoft Teams"); (2) `http_stream.py:283,290` — `add_stream_update(self._index)` stamps the Bot Framework `streamSequence` and `self._index` increments per stream activity; (3) `http_stream.py:285-288` — each chunk send goes through `retry(..., RetryOptions(max_delay=4.0, max_attempts=8))`, so transient 429s are retried with backoff; (4) `http_stream.py:180-201` — `close()` waits for the queue to drain (`_wait_for_id_and_queue`) and the final `add_stream_final()` send also goes through `retry()`. **A LIVE Teams check (streaming a real long response without a 429) is out of scope for this build and is flagged for the reviewers/maintainer.** |
| Teams divider rendering | `card_to_adaptive_card`'s `_hoist_dividers` post-processing pass (`teams/cards.py`) hoists `separator: True` onto the next sibling (or emits a non-empty Container for a trailing divider) | `convertDividerToElement` emits an empty `Container` with `separator: True` | Upstream shares the same bug: Microsoft Teams renders an empty Container at zero height, so the separator line is effectively invisible. Python port fixes locally (issue #45) via the `_hoist_dividers` pass rather than blocking on upstream. |
| `SlackAdapter.current_token` / `current_token_async` / `current_client` | Public accessors that return the request-context-bound token and a preconfigured `AsyncWebClient`. `current_token` (sync `@property`) reads the cache; `current_token_async` (async method) invokes the resolver on demand for callable `bot_token` configs used outside `handle_webhook`. | Not exposed (`getToken()` is private on the TS `SlackAdapter`) | Python-only addition (issue #47). Downstream code that calls Slack Web APIs from inside a handler — email resolution, user profile fetches, reaction bookkeeping — otherwise depends on underscore-prefixed helpers. The async variant is required because the sync `current_token` cannot drive an async resolver (see `bot_token` resolver invocation site row). |
| `SlackAdapterConfig.webhook_verifier` | Optional `Callable[[request, body], bool \| str \| None \| Awaitable[...]]` that fully replaces signing-secret HMAC verification. Lets callers integrate platform-managed verification (e.g. Slack Enterprise Grid edge proxies, KMS-signed payloads, test harness escape hatches). `webhook_verifier` takes precedence over both `signing_secret` (config) and the `SLACK_SIGNING_SECRET` env var — when set, both are ignored. | Upstream has its own `webhookVerifier` field on `SlackAdapterConfig` and matches this precedence direction after vercel/chat#468 (commit `0f0c203`, chat@4.29.0). | Behavior parity restored in 0.4.29 sync wave. The original Python port (PR #87, 0.4.27) preferred `signing_secret` to match upstream's intent at that time; upstream reversed itself in #468 so an env-configured `SLACK_SIGNING_SECRET` could not silently shadow a verifier the caller wired up. This port follows. The contract is documented as a SECURITY surface in `slack/types.py` (`SlackWebhookVerifier`): returning truthy passes the request, falsy/None rejects 401, and a `str` substitutes the request body before dispatch. |
| Slack `bot_token` resolver invocation site | Resolved once at `handle_webhook` entry into a per-request `ContextVar`; sync `_get_token` reads it for the rest of the request. Public adapter methods (`post_message`, `add_reaction`, `upload_files`, etc.) DON'T re-resolve — calling them outside `handle_webhook` (cron jobs, background tasks) with a callable `bot_token` raises `AuthenticationError` until the caller awaits `current_token_async()` first | TS `getToken` is async and resolves on EVERY API call site, so cron/background usage just works | Python keeps `_get_token` sync to preserve the existing pre-resolver public API and to avoid threading `await` through every adapter call site. The trade-off is that callable-`bot_token` usage outside the webhook flow needs an explicit `await adapter.current_token_async()` (or `await adapter._resolve_default_token()`) before the first sync-token-consuming call. Static-string `bot_token` is unaffected (cache primed at construction). |
| Slack `bot_token` resolver caching scope | Single resolution per request, cached in `_resolved_default_token` `ContextVar` for the rest of that request | Provider invoked on every API call within a single request | Within-request caching enables the sync `_get_token` path. Functionally equivalent for rotation (TTL >> request lifetime); diverges only if the resolver is itself sensitive to per-call freshness (rare). |
| `ConcurrencyConfig.max_concurrent` | Enforced via `asyncio.Semaphore` in the `"concurrent"` strategy path; rejects non-integer or `<= 0` values, and rejects any non-`None` `max_concurrent` paired with a non-`"concurrent"` strategy | Accepted into the config type with docstring "Default: Infinity" but never read (3 writes, 0 reads) | Silent correctness bug upstream — consumers setting `max_concurrent=N` with `strategy="concurrent"` reasonably expect an N-way bound on in-flight handlers. We honor the documented contract via a semaphore and fail-fast on misconfiguration so it's never silent. `max_concurrent=None` stays compatible with every strategy (unbounded default). |
| `ConcurrencyConfig.max_concurrent` slot scope | **Single global `asyncio.Semaphore`** — caps total in-flight handlers across all threads to `max_concurrent` | **Per-thread slot map** — `acquireConcurrentSlot(threadId, maxConcurrent)` keys the in-flight counter by `threadId`, so each thread has its own N-way bound | When upstream caught up (vercel/chat#419) it implemented per-thread slots; the Python port shipped earlier with a global semaphore and the slot-scope distinction wasn't visible in the original divergence row. Result: a deployment with `max_concurrent=2` and 100 active threads serializes everything globally on Python (peak in-flight = 2 across all threads) but allows 200 concurrent handlers on TS (2 per thread × 100). The `chat.test.ts > should track slots per thread independently` fidelity entry is `pytest.mark.skip`-ped in `tests/test_chat_faithful.py` until the implementation is restructured to a `dict[thread_id, asyncio.Semaphore]` (with cleanup-on-empty to avoid unbounded growth). Tracked as a follow-up. |
| Lock heartbeat `held_until` seed (#190) | `_LockHeartbeat` seeds `held_until` with `_now_ms() + DEFAULT_LOCK_TTL_MS` when the heartbeat starts, right after `acquire_lock` returns. `Lock.expires_at` is not read. Each successful extend refreshes it with `_now_ms() + TTL`, as upstream does | `heldUntil` is seeded from `lock.expiresAt` | Python-specific backend difference: `PostgresStateAdapter.acquire_lock` stamps `expires_at` with the **database** clock (`now() + make_interval(...)`, `state/postgres.py`), while upstream state-pg computes it on the client (`Date.now() + ttlMs`). With a database clock more than ~20 s behind the app host, a seed from `expires_at` makes `is_ownership_lost()` true before the first 10 s renewal, and at 30 s or more of lag it is true immediately: the debounce loop strands its own message and a queue drain stops after one dispatch. The client-side seed overestimates by one acquire round trip, the same tolerance upstream accepts on every extend. It also covers custom backends. Delete this row if `postgres.py` switches to a client-computed `expires_at`. Regression test: `tests/test_chat_lock_heartbeat.py::TestHeldUntilSeed::test_lagging_database_clock_does_not_make_a_fresh_lock_look_lapsed` |
| Lock heartbeat asyncio translation (#190; translation notes, not a behavioral divergence) | One renewal task per held lock, ticking at a fixed rate on a monotonic clock (`_monotonic_ms()` start `+ k * TTL/3`; the lifetime cap and `held_until` stay on epoch time, as upstream's `Date.now()`): at each tick it runs the extend as its own task awaited through `asyncio.shield`, and ticks that came due while the extend was in flight are skipped. `stop()` sets `stopped`, cancels the loop task and, only if an extend is in flight, awaits it (shielded); with none in flight it returns without yielding. `_run` logs an unexpected exception at `error` | `setInterval` skips a tick while `inFlight` is set; `stop()` is `stopped = true; clearInterval(); await inFlight`; the interval callback's promise chain ends in `.catch` | asyncio has no `setInterval`; sleeping to fixed tick deadlines and skipping the ones missed while an extend was in flight is the same schedule and the same "don't stack extends" rule (sleeping a full interval after each extend would let a slow backend push the next extend past the lock's expiry, and deadlines on the epoch clock would let a wall-clock step back delay it). `asyncio` cancellation interrupts the I/O a task awaits (a JS promise cannot be cancelled), so without the shield `stop()` would abort `extend_lock` mid-command (redis-py/asyncpg connection state). Awaiting a cancelled task costs a full event-loop iteration, while `clearInterval(); await undefined` costs none, so an idle `stop()` must not await the loop task or it opens a Python-only window between the drain's last empty-queue check and `release_lock`. An exception in an un-awaited task surfaces only at GC. Regression tests: `tests/test_chat_lock_heartbeat.py::TestHeartbeatStop`, `::TestHeartbeatCadence` |
| `ConcurrencyConfig.max_lock_lifetime_ms` validation (#190; config validation, not a behavioral divergence) | `Chat.__init__` raises `ValueError` unless the resolved value is a non-negative `int` (`bool` rejected) | No validation | JS coerces a numeric string (`Date.now() - startedAt >= "600000"` still works); Python raises `TypeError` on the heartbeat's first tick, which would silently end renewal and let a second message run concurrently. Mirrors the fail-fast `max_concurrent` validation. Regression test: `tests/test_chat_lock_heartbeat.py::TestLockLifetimeConfig::test_invalid_max_lock_lifetime_ms_is_rejected_at_init` |
| Redis lock token format | `{token_prefix}_{ms}_{secrets.token_hex(16)}` — always 32 hex chars, CSPRNG-sourced | `ioredis_${Date.now()}_${Math.random().toString(36).substring(2, 15)}` — base36, ≤13 chars, **not** CSPRNG | Interop via `IoRedisStateAdapter(token_prefix="ioredis")` still works for lock-release (release/extend compare by full-string equality, and each runtime only releases what it issued), but the token byte-shape diverges. Intentional — CSPRNG should not be regressed to `Math.random()` for cosmetic byte-for-byte compatibility. |
| `StreamingPlan.is_supported()` / `get_fallback_text()` | Raise `RuntimeError` to fail loudly if a generic posting path (e.g. `ChannelImpl.post`, `post_postable_object`) tries to consume a `StreamingPlan` as a normal `PostableObject` | Silently return `True` / `""` — `ChannelImpl.post` would route through `postPostableObject` and post an empty-string fallback | Prevents `StreamingPlan` being silently routed through non-stream-aware posting paths where upstream would post a blank message or attempt a wrong-shape `adapter.post_object("stream", ...)` call. Internal dispatch is guarded by the `kind == "stream"` short-circuit in `post_postable_object` / `Thread.post`; this also protects third-party code that duck-types PostableObjects. |
| `rehydrate_attachment` URL allowlist (Slack / Teams / Twilio / Messenger) | Validates the downloaded URL's scheme (https) + host against a per-adapter allowlist inside the fetch closure; raises `ValidationError` on untrusted hosts before forwarding bearer/Basic credentials. **Twilio is layered:** the suffix allowlist runs first in the adapter closure, then the `fetch_twilio_media` primitive applies upstream's exact-origin check (below), so both must pass. **Messenger** raises `NetworkError` instead (see the Messenger download-guard row below) | Teams: no validation — `fetchData` blindly GETs `fetchMetadata.url` and forwards the bot token. **Slack (since chat@4.39.0, vercel/chat `7c269653` #859 / `b6fa24c6` #865):** fetches any URL through `downloadAttachment` (no `hosts`), resolving and sending the token only when the URL's exact origin is a Slack auth origin (`isSlackAuthUrl`, see the Slack guarded-download row below); other URLs are fetched without credentials. **Twilio (since chat@4.38.1, vercel/chat#831 / `d8103a10`):** `fetchTwilioMedia` requires `new URL(url).origin === new URL(apiUrl ?? apiBaseUrl ?? DEFAULT_API_URL).origin` before resolving credentials, else `TwilioApiError("Twilio media URL must match the configured Twilio API origin", {status: 0})`; there is no separate host allowlist. **Messenger (since chat@4.39.0, vercel/chat 153bd964):** validates via the shared `downloadAttachment` host allowlist (see the Messenger download-guard row below) | SSRF + token-exfil risk upstream: after the 4.26 `rehydrateAttachment` hook lands, a crafted `fetchMetadata` in persisted state can redirect auth'd downloads to an arbitrary host. Python port enforces `CLAUDE.md`'s "Validate external URLs before requests (SSRF)" rule. The check runs inside the download closure (not at build time) so an attachment trusted at parse time still fails closed if the allowlist tightens later. Allowlist: Slack = `{files.slack.com, slack.com, *.slack.com, *.slack-edge.com, slack-gov.com, *.slack-gov.com, slack-files.com, slack-files-gov.com}` plus the configured `api_url` origin (exact scheme/host/port), shared with the `api` subpath's `is_trusted_slack_file_url` (#213 added the GovSlack, `slack-files` and `api_url` entries; the token decision itself is upstream's exact-origin `is_slack_auth_url`, so e.g. `*.slack-edge.com` downloads carry no token); Teams = `{smba.trafficmanager.net, graph.microsoft.com, attachments.office.net, *.botframework.com, *.graph.microsoft.com, *.sharepoint.com, *.officeapps.live.com, *.office.com, *.office365.com, *.onedrive.com, *.microsoft.com}`; Twilio = `{twilio.com, api.twilio.com, *.twilio.com, *.twiliocdn.com}`; Messenger = `{fbsbx.com, *.fbsbx.com, fbcdn.net, *.fbcdn.net}` (no credentials are forwarded, but `fallback`/link-share payload URLs are user-controlled, so the guard is an SSRF fix; it raises `NetworkError("messenger", "Refusing to fetch an untrusted attachment URL")`, upstream's message for a non-allowlisted host (IP-literal / unparseable-URL messages differ, see divergence (3) of the Messenger row below) — **upstream Messenger now validates too** since vercel/chat 153bd964 / chat@4.39.0, so Messenger is parity here, see the Messenger download-guard row below). **Twilio origin check is parity** (4.41 wave, #235): `fetch_twilio_media(url, *, api_url=None, api_base_url=None, ...)` compares `(scheme, lowercased host, effective port)` from `urlsplit` against `api_url` → `api_base_url` → `DEFAULT_API_URL` (`is not None` fallbacks), and the adapter passes its `api_url`, so a regional `api_url` constrains media downloads. Python-only strictness inside that check: URLs with whitespace, ASCII control characters or a backslash are refused outright (WHATWG strips/rewrites those, `urlsplit` does not match it exactly, so failing closed avoids a parser differential). The origin check covers every Twilio attachment download (freshly received webhook `MediaUrlN` as well as rehydrated attachments), as upstream's does, so any non-default `api_url` refuses inbound media on `api.twilio.com`. The Twilio suffix allowlist is **kept** as defence in depth: with the origin check it only adds a refusal when `api_url` points at a non-Twilio or non-`https` host (e.g. a proxy or local mock). Combined, that config can download **no** media at all: `api.twilio.com` URLs fail the origin check and URLs on the `api_url` origin fail the allowlist (pre-PR, such configs still downloaded `api.twilio.com` media; upstream, which has no allowlist, would download proxy-origin media); `*.twiliocdn.com` is now effectively unreachable because it is never the API origin. Redirects: `api.twilio.com` media answers with a redirect to a pre-signed CDN URL; the adapter's aiohttp transport follows it and aiohttp (every version in our `>=3.9` floor; verified in 3.9.0 and the locked 3.14.3 `client.py`) drops the `Authorization` header when the redirect changes origin, so Basic auth is not forwarded to the CDN. Redirect/byte-cap policy now lives in the shared guarded downloader (`shared/download.py`, #204; see the row below); **this row is revisited as each adapter adopts it** (Slack adopted in #213 but keeps this initial-URL allowlist, see the Slack guarded-download row; Teams #218, #225, WhatsApp/Messenger #239, Discord #229): an adapter that moves onto `download_attachment(..., hosts=...)` gets upstream's allowlist semantics and error messages, and its entry here shrinks to whatever Python-only host list or check remains. **Google Chat left this row in #223:** upstream `32687038` (vercel/chat#830, chat@4.38.1) removed the `downloadUri` fetch, so Google Chat attachment bytes come only from the media API by `resourceName` and no URL is ever fetched (parity); the `_is_trusted_gchat_download_url` allowlist is deleted. The remaining Python-only check is the media `resourceName` row below. Regression coverage: `tests/test_twilio_adapter.py::TestRehydrateAttachment::test_rejects_rehydrated_media_from_an_untrusted_origin` (both layers) and `::test_rehydrated_media_is_constrained_to_the_configured_api_url` and `::test_non_twilio_api_url_refuses_media_on_every_origin` (proxy dead end); `tests/test_twilio_api.py::TestFetchTwilioMedia` (origin cases); `tests/test_messenger_webhook.py::TestRehydrateAttachment::test_rehydrated_closure_refuses_untrusted_url_without_io`. |
| Messenger attachment download guard (vercel/chat 153bd964, chat@4.39.0; #234) | `MessengerAdapter._download_attachment` checks the URL with `_is_trusted_messenger_media_url` **before any I/O** and inside the download closure (so fresh and `rehydrate_attachment`-rebuilt closures are both covered; `attachment.url` and the `fetch_metadata={"url": ...}` shape are unchanged). Follows redirects manually (`allow_redirects=False`, at most 5, `Location` resolved against the current hop and re-validated; as upstream, the hop limit is checked before the `Location` is validated, so a 6th redirect always raises `"Too many attachment redirects"`), 25 MB cap (declared `Content-Length` rejected up front, streamed body aborted past the cap), 30 s deadline over every hop + body read (`asyncio.timeout`, plus a per-request `aiohttp.ClientTimeout(total=30)`), non-2xx → `NetworkError("messenger", "Failed to fetch file: {status} {reason}")`, any other failure → `NetworkError("messenger", "Failed to download Messenger attachment")`. **Three divergences:** (1) **weaker — no DNS / private-IP resolution check.** The host allowlist alone rejects IP literals (including decimal-integer hosts like `2130706433`) and non-Meta names, but a Meta-CDN hostname is not re-checked against private ranges after resolution. Connection-bound DNS checks arrive with the shared guarded downloader (SH1, #204); remove this divergence when Messenger migrates onto it (#239). (2) **stricter URL parsing** — also rejects userinfo (`user@host`), an explicit port other than 443, and any host that is not plain lowercase DNS labels (percent-encoded, non-ASCII — the raw authority must be ASCII, so e.g. U+212A KELVIN SIGN cannot fold to `k` via `str.lower()` — bracketed IPv6, empty label, trailing dot), plus whitespace/control characters and backslashes anywhere in the URL, so `urlsplit` and aiohttp/yarl cannot disagree about which host is contacted. (3) **error-message text for IP literals and unparseable URLs.** Every URL/`Location` the check rejects raises the single `"Refusing to fetch an untrusted attachment URL"`. Upstream instead raises `"Refusing to fetch an internal attachment URL"` for a private/internal IP literal (e.g. `https://127.0.0.1/file`, `https://169.254.169.254/...`) and, when WHATWG `new URL(...)` throws on an unparseable URL or `Location` (e.g. `not a url`, `https://[::1/file`), the generic `"Failed to download Messenger attachment"` wrapper from `fetch.ts`. Both sides raise `NetworkError("messenger", ...)` with no network I/O, so only the message differs (upstream's `fetch.test.ts` asserts only `toThrow(NetworkError)` for these). Disappears when Messenger moves onto the shared downloader (#239). | `packages/adapter-messenger/src/fetch.ts` `download()` → `@chat-adapter/shared` `downloadAttachment(url, {adapter: "messenger", hosts: ["fbsbx.com", "fbcdn.net"]})`: https-only, exact host or subdomain (case-insensitive), redirects revalidated (max 5), 25 MB decoded-body cap, 30 s deadline, private/internal IPs refused both as literals and after DNS resolution through a pinned resolver. | **Consumer-visible:** Messenger `fetch_data()` for `fallback`/link-share (or any other) attachments whose URL is not on a Meta CDN host now raises `NetworkError` instead of downloading; `attachment.url` is still populated for display. Regression coverage: `tests/test_messenger_fetch.py` (ports `fetch.test.ts` `describe("Messenger attachment fetch")` + Python-specific redirect/cap/deadline/parser-differential cases) and `tests/test_messenger_webhook.py` (`test_downloads_attachment_successfully`, `test_rejects_external_fallback_downloads_before_the_network`). |
| Shared guarded downloader `shared/download.py` (vercel/chat `bb926884` #850, `153bd964` #856, `b6fa24c6` #865, shared slice of `6adca361` #916; #204) | `download_attachment(url, *, adapter, headers, hosts, limit=25 MB, on_response, redirects=5, timeout_ms=30_000, transport)` ports `downloadAttachment`: HTTPS only, the blocked IPv4/IPv6 range tables copied exactly, internal literals refused, manual redirects re-validated per hop, per-hop `headers` (mapping or function of the URL), `on_response` before the body, decoded-body cap, one deadline over every hop and the body read, upstream's error strings. **Resolver pinning:** the default transport is aiohttp (lazy import) with a per-request `TCPConnector(resolver=<pinned resolver>, force_close=True, use_dns_cache=False)`, `auto_decompress=False`, `trust_env=False`, `allow_redirects=False`. The pinned resolver (`create_resolver`) refuses the host when **any** answer is internal and hands aiohttp only the vetted addresses, so the socket connects to exactly what was checked (SNI and certificate checks still use the hostname). A caller-supplied transport skips the resolver; scheme, literal and allowlist checks still run every hop. **Three divergences:** (1) **stricter — credential stripping for a static `headers` mapping.** `authorization`, `cookie` and `proxy-authorization` from a mapping are dropped on any hop whose origin (scheme, host, port) differs from the first URL; the function form stays caller-controlled, as upstream. (2) **`br` only with a bounded Brotli.** `accept-encoding` advertises `br`, and `content-encoding: br` is decoded, only when `brotli` ≥ 1.2 (`Decompressor.can_accept_more_data`) is installed; otherwise a `br` body raises `"Unsupported attachment encoding: br"`. Older Brotli bindings cannot bound one step's output, so the cap could only apply after a large allocation. (3) **stricter — bytes after the end of a `br` stream raise** (`brotli.error`), where Node ignores them. The binding cannot report how much input a step consumed, so trailing bytes in the same chunk as the stream end cannot be skipped; bytes in a later chunk raise too, so the result never depends on how the body was chunked. **Python adaptations (same behavior, different mechanism):** the transport is `(url: str, headers: dict) -> Awaitable[AttachmentResponse]` with no `AbortSignal`; the deadline cancels the call and gives up without waiting, so a transport or body iterator that ignores cancellation cannot stall it (a late response is closed). `urllib.parse` does not canonicalize hosts like WHATWG `URL`, so hosts are percent-decoded, IDNA-mapped with UTS46 non-transitional processing and then parsed with the WHATWG IPv4 rules. The IDNA step uses the `idna` package, which is what WHATWG and yarl follow (`faß.de` → `xn--fa-hia.de`, not IDNA 2003's `fass.de`); without the package a non-ASCII host is refused. All of this runs before the checks, so forms such as `2130706433`, `0x7f.1`, `127.1` and full-width digits are caught. Hosts WHATWG rejects are refused as untrusted: a forbidden code point after percent-decoding (so `2606%3a4700%3a%3a1` is not read as an IPv6 literal), anything but an IPv6 address in brackets (IPvFuture such as `[v1.x]`, zone identifiers), an invalid IPv4 number, and an `xn--` label whose Punycode does not decode to a valid non-ASCII UTS46 label (`xn--zz`, `xn--a-ecp`). The `idna` package applies IDNA 2008 to non-ASCII input, which is stricter than UTS46 for some symbols (e.g. emoji), so such Unicode hosts are refused where Node would accept them (their valid `xn--` form is accepted). An IP literal matches a `hosts` entry only exactly, never as a suffix; upstream's string suffix match would let `1.2.3.4` match an entry `2.3.4`. Before parsing, a URL (and a redirect `Location`, before it is resolved against the current hop) gets WHATWG's clean-up that `urllib.parse` lacks: C0 controls and spaces are stripped from both ends, tab and newline are removed, `\` becomes `/` before the query, and any number of slashes after `https:` introduces the host. A redirect `Location` is then resolved with WHATWG's relative-reference rules, not `urljoin` (which collapses `a//b` and keeps the old query for `?`), so `/\host/x`, `///host/x` and `https:\\host/x` name another host as they do in Node, and that host is then validated. The returned URL is Node's `URL.href` minus the fragment: canonical host, default port 443 dropped, empty userinfo dropped, the userinfo, path and query percent-encoded with WHATWG's userinfo, path and special-query sets (existing escapes stay byte-for-byte), dot segments (including `%2e` forms) resolved, and an empty path serialized as `/`. The default transport sends that URL without requoting (`yarl.URL(url, encoded=True)`), so signed URLs survive, and it refuses the request if yarl's `raw_host` differs from the validated host. A slow async `close()` gets only the time left before the deadline and then finishes in the background, so cleanup cannot stretch the deadline. If the caller cancels the download, cleanup does not wait at all. Once the deadline has passed, no new hop, `on_response` await or body read starts, and a step that completes at or after it counts as timed out. A URL `new URL` would reject raises the "untrusted" `NetworkError` instead of a `TypeError`. A few URLs Node accepts are refused the same way (fail closed): a space or control character inside the authority, `[` or `]` in the userinfo (which `urlsplit` rejects). A bare string passed as `hosts` raises `TypeError` (TypeScript's `readonly string[]` rejects it at compile time; in Python it would iterate characters). `Content-Length` counts only as plain decimal digits. Some Node behavior is reproduced by hand: repeated `Content-Encoding` / `Content-Length` fields are joined with `", "`, so `gzip, gzip` is refused as unsupported; after a gzip member, zero padding is ignored, and any other byte must start a valid member; trailing data after a deflate stream is ignored. Each decompression step produces at most 64 KiB (Brotli rounds up to its next output block, under 128 KiB), so the cap applies before a small chunk can expand far. As upstream, transport, `on_response` and decoder errors propagate unchanged (adapters wrap them). | `@chat-adapter/shared` `download.ts` (chat@4.39.0–4.41.0): static `headers` sent on every hop; `br` always advertised and decoded via Node zlib; `AbortSignal` passed to the transport | (1) A static credential mapping is the easy call to write, and upstream's own docs tell callers to use the function form or `hosts` to keep credentials off redirects. Dropping them on a cross-origin hop removes that footgun without changing the function form. (2) Keeps the decoded-size cap meaningful: a small `br` chunk must not expand without bound before the cap applies. (3) A chunking-dependent result would be worse than either consistent choice, and ignoring the trailing bytes exactly would mean decoding every `br` body twice; well-formed servers never send them. **Consumer-visible:** none here, since no adapter calls it yet. Adapters that adopt it (#213, #218, #225, #239, #229) gain the 25 MB cap and 30 s deadline. Regression coverage: `tests/test_shared_download.py` (ports every `download.test.ts` case, plus `TestHostCanonicalization`, `TestStaticHeaderCredentials`, `TestBrotli`, `TestDeadlineAndCancellation`, `TestDefaultTransport`). |
| Teams file attachment URL source (`_create_attachment`) | For a `application/vnd.microsoft.teams.file.download.info` attachment, prefers the nested `content.downloadUrl` (a short-lived pre-signed link) over the top-level `contentUrl`. Every other attachment type keeps the upstream `contentUrl`-first path (with `content.downloadUrl` only as a fallback when `contentUrl` is missing/falsy). | Reads `att.contentUrl` only — `createAttachment(att)`'s param type doesn't even include `content` (`adapter-teams/src/index.ts:833`, `const url = att.contentUrl`) | **Hard-UX-failure divergence.** A SharePoint/OneDrive file shared in a personal or group chat arrives as a `file.download.info` attachment that carries BOTH a top-level `contentUrl` (the SharePoint/OneDrive item, e.g. `https://contoso.sharepoint.com/.../file.txt`, which returns **403** to an anonymous GET because it needs a SharePoint auth context) AND a nested `content.downloadUrl` — a pre-authenticated link the [Bot Framework docs](https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/bots-filesv4#message-activity-with-file-attachment-example) explicitly say to issue an `HTTP GET` against. Upstream (and our pre-fix adapter) read `contentUrl` only, so the attachment is **undownloadable** (`fetch_data` 403s). We prefer `content.downloadUrl` for this attachment type so the download actually works. The resulting URL still flows through the unchanged `_build_teams_fetch_data` / `rehydrate_attachment` SSRF allowlist (download hosts are SharePoint/OneDrive for Business → already covered by `*.sharepoint.com` / `*.onedrive.com` in the row above — no host added). To be filed as an upstream issue against vercel/chat. Supersedes stale PR #136. Regression coverage: `tests/test_teams_adapter.py::TestFileDownloadInfoAttachment`. |
| `_rehydrate_message` with `Message` input | Falls through to the `rehydrate_attachment` pass even when the dequeued entry is already a `Message` instance | Early-returns on `raw instanceof Message` before rehydration | The Python port's Redis + Postgres `dequeue()` upgrade raw JSON to `Message.from_json(...)` before returning (upstream's dequeue returns the raw JSON.parse'd dict). Upstream's `instanceof Message` shortcut therefore only fires for in-memory state, but ours would fire for persistent backends too, leaving `fetch_data` stripped forever. The rehydrate pass still skips any attachment that already has `fetch_data`, so in-memory callers pay no cost. |
| Postgres state schema error type (#240) | `PostgresStateAdapter.connect()` with `auto_create_schema=False` raises `chat_sdk.StateSchemaError` (a `ChatError`); the message matches upstream except that the hint names `auto_create_schema=True`; a probe failure is chained as `__cause__` | Plain `Error` with `{ cause }`; the hint names `autoCreateSchema: true` | Python-surface adaptation: callers can catch a dedicated exception type instead of matching message text. Pinned by `tests/test_state_postgres.py::TestPostgresStateSchemaInitialization::test_rejects_connect_when_a_migration_owned_table_is_missing`. |
| Postgres state owned pool on a failed `connect()` (#240) | A pool the adapter created from a URL / env var is closed and reset as soon as that `connect()` attempt fails (or is cancelled); the next attempt creates a fresh pool. An injected pool is never closed | `disconnect()` is a no-op before a successful `connect()`; the lazy `pg.Pool` holds at most one idle client that times out | asyncpg opens `min_size` connections eagerly, so without this a startup retry loop leaked 10 connections per failed attempt. Pinned by `tests/test_state_postgres.py::TestPostgresStateSchemaInitialization::test_closes_the_owned_pool_when_connect_fails`. |
| Postgres state concurrent `connect()` (#240) | Serialized on an `asyncio.Lock`: a caller queued behind a failed attempt retries it (and may succeed) | Concurrent callers share one in-flight promise and all reject with the same error | Predates #240 and kept: a queued caller retrying is harmless, and `connect()` stays idempotent. Pinned by `tests/test_state_postgres.py::TestPostgresStateSchemaInitialization::test_concurrent_connect_retries_after_a_failed_connectivity_check`; see [Postgres state: expired claims and migration-owned schemas](#postgres-state-expired-claims-and-migration-owned-schemas-chat435441-240). |
| Slack Socket Mode reconnect loop | Outer reconnect loop on top of `slack_sdk.socket_mode.aiohttp.SocketModeClient` (which itself has `auto_reconnect_enabled=True`). Exponential backoff (1s → 30s) with explicit shutdown signaling and a tracked `asyncio.Task` so `disconnect()` can cancel cleanly | Single `SocketModeClient` instance from `@slack/socket-mode`; relies entirely on the package's internal reconnect | Hazard #5 (async task lifecycle): a long-lived WebSocket needs an explicit shutdown path so `disconnect()` doesn't leak the loop, and a guarded outer reconnect path so the adapter survives `connect()` itself raising (which the inner client doesn't retry). Inner auto-reconnect still runs; the outer loop is belt-and-suspenders, not a divergence in observable behavior. |
| Slack guarded file downloads and egress config (vercel/chat `7c269653` #859, `b6fa24c6` #865, `6adca361` #916; #213) | `_fetch_slack_file(url, token)` runs `download_attachment` (#204) with per-hop headers: the token (a string or lazy resolver, resolved only when the URL is on a Slack auth origin) is attached only to hops whose exact origin passes `is_slack_auth_url(url, api_url)`, `on_response` rejects `text/html` (the login page) with upstream's message, and any non-`NetworkError` failure becomes `NetworkError("slack", "Failed to fetch Slack file")`. `rehydrate_attachment` passes a resolver, so a non-auth URL never looks up an installation token. `SlackAdapterConfig.file_transport` (upstream `fileTransport`) is returned by `_create_file_transport()`, which a subclass can override. `SlackAdapterConfig.http_client_factory: Callable[[], httpx.AsyncClient]` is the Python form of upstream `fetch` and is used for `response_url` posts; the adapter calls it once per request and closes the client. The `aiohttp` default transport is now in the `slack` extra (slack_sdk's `AsyncWebClient` already needed it) | `fetchSlackFile` does the same, but first fetches **any** URL (no initial allowlist); `fetch` also covers Socket Mode event forwarding | **One divergence (kept from before, host list extended):** a download whose initial URL is off the Slack allowlist (see the `rehydrate_attachment` allowlist row) raises `ValidationError` before the token is resolved or any request is made, where upstream fetches it without credentials. The allowlist (and the decision to resolve the token) is applied to the URL as the downloader will request it, normalized by `validate_attachment_url`, so a host-parsing difference between `urlsplit` and the downloader cannot pass it (`test_allowlist_applies_to_the_url_the_downloader_requests`). Redirects are not restricted by host (no `hosts`), as upstream: a redirect to a public host is followed without the token, and an internal target is refused by the downloader. **Not ported:** Socket Mode event forwarding (Python has no transient listener, see the next row), so `http_client_factory` only covers `response_url`. Upstream's "enforces the download deadline when a signal-ignoring transport %s" shortens `AbortSignal.timeout`; Python checks the 30 s default by advancing the loop clock, for a slow transport (`test_enforces_the_30_second_download_deadline`) and for a stalled body (`test_enforces_the_30_second_download_deadline_when_the_body_stalls`); the signal-ignoring cases themselves are covered in `tests/test_shared_download.py`. Upstream's "keeps the agent on default, rotated, and scoped WebClients" and "preserves env authentication with transport-only config" test axios/`globalThis.fetch` wiring with no Python counterpart (slack_sdk clients get `web_client_options` on every construction site). **Consumer-visible:** Slack downloads are capped at 25 MB (decoded) and 30 s, internal addresses are refused, and the token is no longer sent to `*.slack.com`/`*.slack-edge.com` hosts that are not auth origins. **Python-only behavior change:** downloads no longer honor `HTTPS_PROXY`/`HTTP_PROXY` (the old path used `httpx.AsyncClient()`, `trust_env=True`; the default guarded transport pins DNS with `trust_env=False`, since a proxy would bypass the private-address check), so env-proxied deployments must set `file_transport`; upstream's Node transport never read env proxies. Regression coverage: `tests/test_slack_file_transport.py`, `tests/test_slack_socket_mode.py::TestSocketModeTransport`, `tests/test_slack_webhook.py::TestRehydrateAttachment`, `tests/test_slack_installation_provider.py::TestInstallationProviderRehydrate`. |
| Slack Socket Mode listener serverless variant | Not ported | `startSocketModeListener()` / `runSocketModeListener()` open a transient socket for `durationMs` and forward events via HTTP POST | Vercel-specific pattern (cron-triggered ephemeral listener with `waitUntil`). The forwarded-event receiver (`x-slack-socket-token` handling in `handle_webhook`) is ported so a separate Python process can run the long-lived listener; the deployment glue itself isn't part of the SDK. |
| Slack DM block-action threading (#133/#137) | `_handle_block_actions` sets `thread_ts=""` for a top-level DM button click (never falls back to the clicked message's own `ts`), so a handler's `event.thread.post(...)` does not spawn a phantom "1 reply" thread in the DM. Mirrors `_handle_message_event`'s DM handling (`thread_ts=""` for top-level DMs). | `handleBlockActions` (`adapter-slack/src/index.ts:1455-1456,1470`) computes `thread_ts \|\| container.thread_ts \|\| messageTs` and encodes `threadTs \|\| messageTs \|\| ""` — it falls back to the clicked message's `ts` even for DMs, so a DM button click spawns a phantom reply thread. Upstream's `handleMessageEvent` *does* empty-case DMs (`:2158`), but `handleBlockActions` does **not** — an upstream internal inconsistency. | Hard UX failure with no workaround (phantom "1 reply" threads on DM button clicks). We extend upstream's own DM-message convention to the block-action path. The resulting empty DM `thread_ts` is consumed by `fetch_messages` → now routed to `conversations.history` for empty `thread_ts` (see the #138 row below); the block-action fix should be contributed upstream (cf. PR #107's stream() divergence) to restore parity. |
| Slack empty-DM `thread_ts` fetch routing (#138) | `fetch_messages` routes an empty (falsy) `thread_ts` — every top-level DM root, encoded `slack:Dxxx:` — to the channel-history path (`_fetch_channel_messages_forward` / `_fetch_channel_messages_backward`, both `conversations.history`) instead of `conversations.replies(ts="")`, preserving direction/limit/cursor. `fetch_message` likewise reads a single empty-`thread_ts` message via `conversations.history(channel, latest=message_id, inclusive=True, limit=1)` (mirroring the inner link-preview `fetch_message` at `slack/adapter.py:3293`). Non-empty `thread_ts` stays byte-identical on `conversations.replies`. | `fetchMessages` (`adapter-slack/src/index.ts:4135` → `fetchMessagesForward`/`Backward` `:4187`/`:4250`) and `fetchMessage` (`:4350`) call `conversations.replies({ ts: threadTs })` with **no** empty-`thread_ts` guard. With `ts=""` Slack returns no replies and the DM root context is lost. | Hard UX failure on **every** DM root: history/single-message fetches over a DM silently return nothing (or lose the root) because the DM root legitimately encodes `threadTs=""` (faithful to `_handle_message_event` / `handleMessageEvent`, "matches openDM subscriptions"). For a DM the channel **is** the conversation, so `conversations.history` is the correct source. This supersedes the "separate follow-up" noted in the #133/#137 row and covers DM message fetches **and** the #137 DM block-action consumer uniformly. Candidate to file upstream against vercel/chat (an empty-`thread_ts` guard in `fetchMessages`/`fetchMessage`); remove this divergence once upstream adds it. |
| `GitHubAdapter.octokit` native client getter (vercel/chat#459, #478) | Not exposed | `get octokit(): Octokit` (plus deprecated `client` alias) returns the underlying Octokit — fixed instance in PAT/single-tenant App mode, per-installation client resolved from `AsyncLocalStorage` inside a webhook handler in multi-tenant mode | The Python adapter is hand-rolled over raw `aiohttp` (`_github_api_request`) with PyJWT for App JWTs and an installation-token cache; the `github` extra is `pyjwt[crypto]` only — there is no Octokit-equivalent object to return, and exposing the raw session or an invented facade under the name `octokit` would misrepresent the surface. Revisit if the adapter adopts an octokit-style SDK (e.g. `githubkit`) as an optional dependency per hazard #10's "prefer official SDKs" sub-rule; the getter (and the GitHub `fetch_subject` half of #459) ports cleanly then. |
| GitHub `GITHUB_BOT_USER_ID` env parse (upstream `6750d59e`, chat@4.33; #233) | The bot id resolves as `config["bot_user_id"]` (when not `None`, so an explicit `0` still wins) → `GITHUB_BOT_USER_ID` → auto-detection → learned from the `user` of the first comment `post_message` creates (`_capture_bot_user_id`, both branches, never `edit_message`, never overwrites a known id): all parity. The divergence is the env parse. After `strip()` the value must be a whole ASCII base-10 integer of at most 20 digits (`[+-]?[0-9]{1,20}`; the cap keeps `int()` clear of CPython's 4300-digit conversion limit, which would otherwise raise in the constructor). Anything else that is non-empty (`"12abc"`, `"0x10"`, `"1_000"`, `"4.2"`, non-ASCII digits, whitespace only, 21+ digits) is ignored with a warning `"Ignoring GITHUB_BOT_USER_ID: not a base-10 integer" {length}` (the value is not logged), and the id stays unset. Unset or `""` is silent, as upstream. | `Number.parseInt(process.env.GITHUB_BOT_USER_ID, 10)`, dropped only when `NaN`, so `"12abc"` → `12` and `"1_000"` → `1` | A truncated id would make `is_me` match an unrelated user and still miss the bot's own comments, which is the self-reply loop this setting exists to prevent. Python's plain `int()` is not a safe substitute either: it accepts `"1_000"` and non-ASCII digits. **Not ported:** the Vercel Connect half of `6750d59e` (`installationToken` provider, `webhookVerifier`, skipping detection in Connect mode, `verifySignature` returning `false` without a secret) is deferred to #189. The learned id only takes effect after the bot's first post, so set `bot_user_id` or `GITHUB_BOT_USER_ID` when the token can call neither `GET /user` nor `GET /app`. Regression coverage: `tests/test_github_adapter.py::TestGitHubBotUserIdEnv` (`test_malformed_env_value_is_ignored_with_a_warning` fails if the parse goes back to prefix truncation) and `::TestGitHubCaptureBotUserId`. |
| `LinearAdapter.linear_client` native client getter (vercel/chat#459, #478) | Not exposed | `get linearClient(): LinearClient` (plus deprecated `client` alias) returns the `@linear/sdk` `LinearClient`, per-org from `AsyncLocalStorage` in multi-tenant OAuth mode | `@linear/sdk` is TypeScript-only and no official Linear Python SDK exists; the adapter issues GraphQL directly over `aiohttp` (`_graphql_query`) and already documents that stance. Nothing honest to put behind the name. Revisit only if Linear ships an official Python SDK (the Linear `fetch_subject` half of #459 is blocked on the same). |
| `@chat-adapter/tests` adapter test kit (vercel/chat#470) | Not ported | New TS package with test utilities for adapter authors | Python already ships `chat_sdk.testing` (`MockAdapter`, `MockStateAdapter`, `create_test_message()`) covering the same surface for this repo's adapter tests; mirroring the TS kit verbatim would duplicate it. Revisit if upstream's kit grows capabilities ours lacks (e.g. recorded replay fixtures for third-party adapter authors). |
| Teams modal-submit webhook options (vercel/chat#454 adapter-teams slice) | Not ported — the Python Teams adapter has no task-module/modal-submit flow (`handleTaskSubmit`/`processModalSubmit` are absent), so upstream's change passing `bridgeAdapter.getWebhookOptions(activity.id)` into `processModalSubmit` has no landing site | `TeamsAdapter.handleTaskSubmit` forwards webhook options so modal callbackUrl POSTs are registered with `waitUntil` | Pre-existing gap: Teams modals are unported. The Slack adapter already forwards options to `process_modal_submit`, so the new waitUntil plumbing is exercised there. Add the Teams call when Teams modal support lands. |
| jsx-runtime `callbackUrl` props (vercel/chat#454 slice) | Not ported | `ButtonProps`/`ModalProps` gain `callbackUrl`; `resolveJSXElement` forwards it | Covered by the existing "JSX Card/Modal elements" row — Python has no JSX runtime; `Button()`/`Modal()` builders accept `callback_url` directly. |
| jsx-runtime `id` prop for link buttons (stable-id-for-link-buttons, chat@4.31.0 commit `171657a`) | Not ported | `LinkButtonProps` gains `id?`; `resolveJSXElement` forwards `id: props.id` | Covered by the existing "JSX Card/Modal elements" row — Python has no JSX runtime. The core half of the same commit (`LinkButton()` factory + `LinkButtonElement` `id`) **is** ported: the `LinkButton(id=…)` builder accepts the optional stable identifier directly (matching the `Button`/`Select` `id` convention). |
| jsx-runtime 4.34–4.41 card/modal props (#202; upstream `929878b5`, `4717a384`, `0153a39f`, `4a0b5c0c`, `ad904325`) | Not ported | `fromReactElement` converts `Chart`, `Card` `width` and `Button`/`LinkButton` `tooltip`; `fromReactModalElement` converts `DateInput`, `NumberInput` and `Select`/`RadioSelect` `dispatchAction`; `929878b5` (chat@4.39.0) lets JSX `LinkButton` carry an `id` | Covered by the existing "JSX Card/Modal elements" row — Python has no JSX runtime. The builders (`Chart()`, `Card(width=…)`, `tooltip=`, `DateInput()`, `NumberInput()`, `dispatch_action=`, `LinkButton(id=…)`) take these directly. The JSX-only upstream tests (`modals.test.ts` "should convert a DateInput/NumberInput react element" and the `fromReactModalElement` copy of "preserves dispatchAction=%s"; `jsx-runtime.test.ts` tooltip/width cases) are skipped. |
| Transcripts API Python adaptations (vercel/chat#448) | `transcripts.delete()` returns a `DeleteResult` dataclass; misconfiguration raises `ValueError` (constructor/`AppendInput` guards, invalid duration) or `ChatError` (`chat.transcripts` accessor); guard messages name the Python kwarg (`options.user_key`); `DurationString` is a `str` alias validated at runtime by `_parse_duration` | Inline `{ deleted: number }`; generic `Error` for all of the above; template-literal `` `${number}${"s"\|"m"\|"h"\|"d"}` `` type | Port rules: typed dataclasses over raw dicts; repo error-type conventions (constructor misconfig → `ValueError`, runtime API misuse → `ChatError`) with upstream-matching message wording; Python has no template-literal types. Same shapes and values throughout. |
| Slack legacy mrkdwn renderer (response_url surface only, post-#440) | `_node_to_mrkdwn` renders headings as `*bold*` and images as `{alt} ({url})` / bare URL | TS `nodeToMrkdwn` has no heading/image branches — both fall through to `defaultNodeToText`, dropping heading emphasis and image URLs | Pre-existing Python improvement; after vercel/chat#440 it affects only `to_response_url_text` (ephemeral edits via response_url). Preserves visual hierarchy and image URLs Slack would otherwise lose. |
| Slack `api` primitives `send_slack_response_url` URL gate (vercel/chat#548; allowlist aligned with vercel/chat#876 in #205) | `send_slack_response_url` (`slack/api/__init__.py`) calls `_assert_slack_response_url(url)` before POSTing, which routes through the same `_is_trusted_slack_response_url` helper the high-level adapter uses, and raises `ValueError` for anything else | Upstream's **adapter** validates `response_url` since chat@4.40 (`isTrustedSlackResponseUrl`, vercel/chat#876 — checked at ephemeral-id encode, decode and before the send; the Python adapter ports that 1:1). Upstream's SDK-free `api/client.ts` `sendResponseUrl` still POSTs to whatever `response_url` it is handed, with no scheme/host validation | SSRF guard. The only remaining divergence is that the SDK-free primitive also validates. The `response_url` reaching this primitive can originate from a parsed-but-unverified interaction payload; without a gate a crafted value could redirect the POST (which carries no bearer token but does echo SDK-controlled message content and trigger an arbitrary outbound request) to another host. Enforces `CLAUDE.md`'s "Validate external URLs before requests (SSRF)" rule. Allowlist (shared with the adapter): scheme `https`, no userinfo, no explicit port, host exactly `hooks.slack.com` or `hooks.slack-gov.com` (set membership, never a suffix match; a non-numeric port is untrusted). The shared helper is marginally stricter than upstream's WHATWG-`URL` check on two edges Slack never emits: an explicit default port (`:443`) and an empty userinfo (`https://@hooks.slack.com/...`) are rejected, where `new URL()` normalizes both away. Before #205 this primitive accepted any `*.slack.com` host. Regression coverage: `tests/test_slack_api_primitives.py::TestSlackApiPrimitives::test_rejects_non_slack_response_urls`. |
| Slack unfurl-cache channel for fetched messages (vercel/chat#877 port, #205) | `_parse_slack_message` passes `_unfurl_channel_for(event, thread_id)` to `_enrich_links`: `event["channel"]` when present, otherwise the channel decoded from the `thread_id` the message is parsed under | Upstream `parseSlackMessage` passes `event.channel` only. Messages from `conversations.history` / `conversations.replies` have no `channel` field, so upstream's `enrichLinks` returns early for every fetched message (`fetchMessage`, `fetchMessages`, channel history, `listThreads`) | Avoids a regression from pre-#205 Python, where the ts-only unfurl key let fetched messages pick up `message_changed` unfurl metadata. The fetch paths always build `thread_id` from the channel they queried, so the fallback names the same channel the `message_changed` writer keyed by; the installation scope still comes from the request ContextVar, so nothing crosses installations. Regression coverage: `tests/test_slack_api.py::TestFetchedMessagesKeepCachedUnfurls`. |
| Slack `api` primitives `fetch_slack_file` host allowlist (vercel/chat#548; token scoping vercel/chat `7c269653` #859, #213) | `fetch_slack_file(*, url, token, api_url=None, fetch=None)` (`slack/api/__init__.py`) first gates `url` through `is_trusted_slack_file_url(url, api_url=...)`, raising `ValueError` for untrusted hosts before the token is resolved. It then matches upstream: the token is resolved and sent only when `is_slack_auth_url(url, api_url)` (exact origin in `{https://files.slack.com, https://files.slack-gov.com, https://slack-files.com, https://slack-files-gov.com, https://slack.com, https://slack-gov.com}` or the `api_url` origin), and the raw response of the injected `fetch` is returned | Upstream `api/client.ts` `fetchSlackFile` (chat@4.39.0+) sends `Authorization` only for `isSlackAuthUrl` URLs and fetches every other URL without credentials; it does not use the guarded downloader either | Token-leak / SSRF guard: a crafted `url_private` is refused instead of fetched. Same allowlist as the adapter's `rehydrate_attachment` row. The primitive keeps upstream's contract (plain `fetch`, response returned) rather than moving onto `download_attachment`, which would change its return type and bypass the injectable `fetch`; the default `fetch` (httpx) does not follow redirects. Regression coverage: `tests/test_slack_api_primitives.py::TestSlackApiPrimitives::test_refuses_external_url_without_resolving_the_token` (upstream's "fetches external URL %s without bearer auth" cases), `::test_authenticates_file_urls_on_the_configured_api_origin`, `::test_fetches_allowlisted_non_auth_origin_without_resolving_the_token`. |
| Slack `web_client_options` → slack_sdk `WebClient` kwargs (vercel/chat#8336a3e, chat@4.31) | `SlackAdapterConfig.web_client_options: dict[str, Any] \| None` is spread (gated on `is not None`, so an explicit `{}` still spreads as a no-op) into **both** WebClient construction sites — the default `AsyncWebClient` (`_get_client`) and the per-token sync `WebClient` (`_get_web_client_for_token`). The keys are **slack_sdk** `WebClient` constructor kwargs: `timeout` (int seconds), `retry_handlers` (a list of `slack_sdk.http_retry.RetryHandler`), `headers`, etc. Any nested `headers` dict is **deep-copied per client** (`_web_client_kwargs`) so cached per-token clients never share a mutable dict and the caller's input is never mutated. | Upstream `webClientOptions?: Omit<WebClientOptions, "slackApiUrl">` forwards to `@slack/web-api`'s axios-backed `WebClient`; its headline keys are `retryConfig` (a `retryPolicies.*` policy), `rejectRateLimitedCalls`, and `timeout` (ms). | No 1:1 mapping: `slack_sdk` has no `retryConfig`/`rejectRateLimitedCalls` (retry behavior is configured via `retry_handlers`) and its `timeout` is seconds, not ms. So the option bag maps to slack_sdk `WebClient` kwargs rather than axios options. Same intent (tune the underlying HTTP client the adapter doesn't otherwise expose) and same per-client header isolation. Documented inline in `slack/types.py` (`web_client_options` docstring) and `slack/adapter.py` (`_web_client_kwargs`). **Socket Mode (vercel/chat `6adca361` #916, #213):** upstream's `socketTransportOptions` forwards only `agent`, `tls` and `slackApiUrl` to `SocketModeClient`; Python's counterpart `_socket_client_kwargs` passes `web_client_options["proxy"]` as `SocketModeClient(proxy=...)` (the WebSocket) and a fresh `AsyncWebClient` built from the `proxy`/`ssl` subset plus `api_url` as `base_url` as `web_client=` (`apps.connections.open`). Headers, timeouts and retry handlers are not forwarded, as upstream. With none of them set the client gets only `app_token`, so slack_sdk still falls back to its `HTTPS_PROXY` env handling. Regression coverage: `tests/test_adapter_api_url_config.py::TestSlackWebClientOptions`, `tests/test_slack_socket_mode.py::TestSocketModeTransport`. |
| Slack card/modal length limits and date check (#212; upstream `4717a384`, `0153a39f`) | The `data_table` 10,000-character cell budget, the 3,000-character ASCII fallback cut, and the chart title/label/axis-label limits count Python code points (`len`, slicing). A `DateInput` `initial_value` of `0000-MM-DD` is dropped with a warning (`datetime.date` has no year 0). | Counts UTF-16 code units (`.length`, `.slice`), so a character outside the BMP (most emoji) counts as 2; `new Date("0000-01-01T00:00:00Z")` round-trips, so year 0 is kept. | Near a limit, a table or chart with astral characters can render natively in Python where upstream falls back, or be cut a few characters later; Python's cut never splits a surrogate pair (upstream's can). Counting UTF-16 units would need a Python-only helper on every limit check. Slack's own datepicker cannot pick year 0. Breadcrumbs in `slack/cards.py` (`_convert_table_to_blocks`), `slack/blocks/__init__.py` (`_table_to_blocks`) and `slack/modals.py` (`_to_initial_date`). Regression tests: `tests/test_slack_cards.py::TestCardToBlockKitWithDataTables::test_counts_table_characters_in_code_points` and `tests/test_slack_modals.py::TestDateAndNumberInputs::test_drops_a_year_zero_initial_date`. |
| Teams service-URL allowlist host list (`call_teams_connector_api` + adapter `_validate_service_url`; vercel/chat#876 / `7609d8f6`, chat@4.40.0) | Both lists accept `https` on `smba.trafficmanager.net`, `msteams.botframework.azure.cn`, `smba.infra.(gcc\|gov\|dod).teams.microsoft.(com\|us)` **and the wildcards `*.botframework.com`, `*.botframework.us`, `*.teams.microsoft.com`, `*.teams.microsoft.us`**, matched case-insensitively on scheme and host (`re.IGNORECASE | re.ASCII`, like upstream's `url.hostname.toLowerCase()`; `re.ASCII` stops non-ASCII case folds such as the Kelvin sign), plus plain-`http` loopback for the local Emulator (`localhost` / `127.x.x.x` / `::1`, matched with `fullmatch` on the *parsed* hostname; userinfo, an invalid port, whitespace, control characters and `\` are refused). `call_teams_connector_api` also refuses a resolved URL whose origin differs from `service_url` (upstream parity). Both checks run before any token request. A refusal raises `ValueError` with upstream's message text (`Refusing to send a Teams bot token to an untrusted Connector serviceUrl` / `... outside the Connector serviceUrl origin`). | Upstream `getTrustedConnectorUrl` (`api/client.ts`) accepts only the exact hosts `msteams.botframework.azure.cn`, `smba.infra.{dod,gov}.teams.microsoft.us`, `smba.infra.gcc.teams.microsoft.com`, `smba.trafficmanager.net` over `https`, plus `http` loopback (`/^(?:localhost\|127(?:\.\d{1,3}){3}\|\[::1\])$/i`), and throws `TeamsApiError`. The high-level adapter delegates outbound calls to the Teams SDK with no host check of its own | **Remaining delta = the four wildcards + the exception type.** The wildcards pre-date upstream's list (Python-first hardening from the 4.31 port, when upstream validated nothing) and cover regional Bot Framework hosts such as `smba.uk.botframework.com`; dropping them could break a deployment that receives such a `serviceUrl`, so they stay (4.41 wave, #221). `ValueError` is kept so existing `except ValueError` callers do not change. Regression coverage: `tests/test_teams_api_primitive.py::TestTeamsApiSsrfDivergence` (incl. the upstream `it.each` `test_rejects_untrusted_service_url_before_acquiring_a_token`, origin and loopback cases) and `tests/test_teams_coverage.py::TestValidateServiceUrl`. |
| Teams `graph` primitives `call_teams_graph_api` host gate (vercel/chat#876 / `7609d8f6`, chat@4.40.0) | `call_teams_graph_api` (`teams/graph/__init__.py`) validates the **final** request URL (an absolute `path_or_url`, an `@odata.nextLink` followed by `paginate_teams_graph`, or a relative path joined onto a caller-supplied `graph_url`) through `is_trusted_graph_url` **before** resolving/attaching the `Bearer` token: `https` and a host exactly in `{graph.microsoft.com, graph.microsoft.us, dod-graph.microsoft.us, graph.microsoft.de, microsoftgraph.chinacloudapi.cn}` (the same set as upstream). Absolute-vs-relative routing uses `urlparse` (scheme or netloc ⇒ absolute), so `HTTPS://evil` and `//evil` are gated too. A refusal raises `ValueError("Refusing to send a Microsoft Graph token to an untrusted URL: ...")` | Upstream `getTrustedGraphUrl` (`graph/client.ts`) resolves `pathOrUrl` (absolute when it `startsWith("http")`, else joined onto `graphUrl`) and requires `https` plus a host in `TRUSTED_GRAPH_HOSTS` (same five hosts), throwing `TeamsApiError("Refusing to send a Microsoft Graph token to an untrusted URL")` | **Host list is parity** (4.41 wave, #221; previously Python pinned to `graph.microsoft.com` only and did not check a relative path joined onto `graph_url`). Remaining delta: `ValueError` instead of `TeamsApiError` (kept for existing callers) and the parse-based absolute/relative routing (both end in the same host check on the resolved URL). Regression coverage: `tests/test_teams_graph_primitive.py::TestTeamsGraphSsrfDivergence`. |
| Teams custom `token` factory vs `CLIENT_SECRET` (vercel/chat#732 / `e06b4b60`, chat@4.35.0) | `TeamsAdapterConfig.token` is forwarded to the SDK `AppOptions.token` and no `client_secret` is emitted (neither `app_password` nor `TEAMS_APP_PASSWORD`). The adapter builds the SDK `App` from a thin subclass whose `_init_credentials` returns `TokenCredentials` whenever a `token` factory and a client id are present, so a stray `CLIENT_SECRET` env var cannot win. The hand-rolled Bot Framework / Graph token paths (`open_dm`, `get_user`, `fetch_channel_info`, Graph reads) call `token(scope, tenant_id)` directly, uncached, and never read `app_password`; `tenant_id` follows the SDK's own rule (`TokenManager._resolve_tenant_id`): the tenant the SDK `App` was given (`credentials.tenant_id`, unset for `app_type="MultiTenant"`), else the cloud login tenant (`botframework.com`) for the Bot Framework scope and `common` for the Graph scope, so a tenant-routing factory sees the same tenant whether the SDK or a hand-rolled path asks. The Bot Framework scope is the SDK `App`'s `cloud.bot_scope` (e.g. `https://api.botframework.us/.default` under `CLOUD=USGov`); the Graph scope stays `https://graph.microsoft.com/.default` because the hand-rolled Graph reads always target `graph.microsoft.com` | `toAppOptions` passes `clientSecret: ""` next to `token` to suppress the SDK's generic `CLIENT_SECRET` env fallback (the JS SDK treats `""` as set), and every outbound call goes through the SDK App | The Python SDK (`microsoft-teams-apps` 2.0.13 – 2.0.16, `App._init_credentials`) reads `options.client_secret or os.getenv("CLIENT_SECRET")` and checks `client_id and client_secret` **before** `client_id and token`, so upstream's empty-string trick does not carry over. The subclass restores upstream's documented precedence ("takes precedence over appPassword, federated credentials, and client-secret environment variables, including CLIENT_SECRET"); without a factory it defers to the SDK unchanged. Regression coverage: `tests/test_teams_connect.py::TestCustomTokenPrecedence` (fails if the subclass is dropped: `test_token_factory_beats_client_secret_env_alone`; tenant rule: `test_hand_rolled_factory_tenant_matches_the_sdk`). |
| Teams `cards_input` empty-options default (vercel/chat#8c71411, chat@4.31) | `input_request_to_teams_adaptive_card` reads `options = request.get("options") or []` | Upstream `cards-primitives/input.ts` reads `const options = request.options ?? []` | Benign truthiness divergence. The only values that differ between `or []` and `?? []` are the falsy-but-present ones (`[]`, `None`); for an options list both produce the same empty list, so the rendered card is byte-identical. Documented (not "fixed" to `is not None`) because there is no observable behavior difference — a present empty `options` and an absent `options` both yield "no choices". |
| Teams `graph` path-segment encoding (vercel/chat#8c71411, chat@4.31) | Path segments (`team_id` / `channel_id` / `chat_id` / `message_id`) are interpolated through `quote(segment, safe='')` | Upstream `graph/{channels,messages}.ts` use `encodeURIComponent(...)` | Benign over-encoding divergence. `quote(safe='')` percent-encodes a strictly larger set than `encodeURIComponent` (notably `!`, `'`, `(`, `)`, `*` — which `encodeURIComponent` leaves literal). Graph IDs (team/channel/chat/message GUIDs and thread tokens) never contain those characters, so the encoded path is identical in practice; where they did differ, the stricter `quote` is the safer choice (no URL injection via an unescaped sub-delimiter). Documented rather than narrowed to match `encodeURIComponent` exactly. |
| Teams `graph` defensive shape coercion (vercel/chat#8c71411, chat@4.31) | `to_graph_message` / channel + message readers coerce unexpected Graph payload shapes with `isinstance` guards (`x if isinstance(x, Mapping) else {}`, `value if isinstance(value, list) else []`) before reading fields | Upstream `graph/messages.ts` reads `message.from?.user` / `message.body?.content ?? ""` etc. with optional chaining — a non-object where an object is expected throws at the property access | Benign defensive divergence. Upstream's optional chaining tolerates `null`/`undefined` but throws on a wrong-typed non-null (e.g. a string where an object is expected); our `isinstance` coercion fails closed to an empty mapping/list instead of raising. For well-formed Graph responses the behavior is identical; the divergence only manifests on malformed payloads, where returning an empty-shape result is more resilient than throwing. Mirrors the repo's general "more resilient than throw" stance (cf. the `renderPostable on unknown input` row). |
| `ThinkingChunk` opt-in stream-input type (Python-only, default-off; supersedes PR #39) | A **separate, opt-in** dataclass — `ThinkingChunk(type="thinking", content=str)` — surfaces AI-SDK `reasoning`/`reasoning-delta` (and pydantic-ai `part_kind == "thinking"`) parts. **`StreamChunk` is NOT widened**: the canonical union stays `StreamChunk = MarkdownTextChunk \| TaskUpdateChunk \| PlanUpdateChunk` — byte-identical to upstream's three variants, so a consumer doing an exhaustive `match` over `StreamChunk` sees zero change on upgrade. `ThinkingChunk` is accepted only at the **stream-input/output boundaries** via the public alias `StreamInput = str \| StreamChunk \| ThinkingChunk` (the `Adapter.stream()` protocol signature, `from_full_stream`/`_from_full_stream` returns, `Thread._wrapped_stream`, and each receiving adapter's `stream()` signature). A producer can yield `ThinkingChunk` (opt-in) and the adapters that receive the stream type-check; code that only references `StreamChunk` never touches it. **OPT-IN, default-off**: emitted only when a caller passes `from_full_stream(stream, emit_thinking=True)` or sets the thread-level `emit_thinking=True` config; the internal `_from_full_stream` threads the same flag. With the default (`emit_thinking=False`) the normalized stream is **byte-for-byte identical** to upstream — reasoning parts are dropped and **no** `ThinkingChunk` is produced. Consumption is graceful: `Thread._handle_stream` never accumulates a `ThinkingChunk` into the posted-message text, and every adapter's stream handler skips it (Slack/Teams expose an optional `render_thinking` hook via `chat_sdk.shared.adapter_utils.maybe_render_thinking`; the text-accumulate adapters ignore it structurally). **Streaming-only — never persisted**: `Message` has no `thinking` field, `to_json()` is unchanged, and a round-tripped `Message` is byte-identical, so cross-SDK state (Redis/Postgres shared with the TS SDK) stays compatible. | Upstream `from-full-stream.ts` forwards only `text-delta` + `finish-step`; AI-SDK `reasoning`/`reasoning-delta` parts fall through and are discarded. `StreamChunk = MarkdownTextChunk \| TaskUpdateChunk \| PlanUpdateChunk` — no reasoning variant and no stream-input alias. Upstream leaves reasoning display to the AI-SDK web UI. | chinchill actively streams agent thinking to Slack/Teams but has to intercept the model stream out-of-band today because the chat-platform SDK has no path for it. This gives the SDK a first-class, opt-in one without changing any default behavior — and crucially **without widening the public `StreamChunk` union**, so consumers referencing it are unaffected. The whole design constraint is that default-off == upstream and `StreamChunk` == upstream: separate opt-in input type, opt-in emit, graceful/skip consume, zero state pollution. Regression coverage: `tests/test_thinking_chunk.py`, `tests/test_from_full_stream.py::TestThinkingOptIn`, `tests/test_types.py::TestThinkingChunk`, plus per-adapter no-crash tests in `tests/test_slack_api.py`, `tests/test_teams_native_streaming.py`, `tests/test_twilio_adapter.py`, `tests/test_messenger_api.py`. |
| Linear agent-activity emit: raw GraphQL (chat@4.31 / #151 — L4) | The agent-session EMIT path (`post_message` session branch, `start_typing` session branch, `stream` → `_stream_in_agent_session` with its flush/`task_update`/`plan_update` logic, and `_parse_message_from_agent_activity`) is ported as **raw GraphQL mutations** over the existing `_graphql_query` helper, **schema-hardened against Linear's published GraphQL schema**. Mutation names: `agentActivityCreate(input: AgentActivityCreateInput!)` and `agentSessionUpdate(id: String!, input: AgentSessionUpdateInput!)`. `content` is sent inside the `AgentActivityCreateInput.content` **`JSONObject!`** scalar — so `type`/`body`/`action`/`parameter`/`result` are inline JSON fields, with the **lowercase** `AgentActivityType` enum values `"response"` / `"thought"` / `"error"` / `"action"` (confirmed lowercase, NOT PascalCase). `ephemeral: Boolean` is a sibling of `content` and is **included only when set** (absent — not `false` — on response/thought/error). The `agentActivityCreate` return selection requests only schema-valid fields (`success`, `agentActivity { id agentSession { id } sourceComment { id body parentId createdAt updatedAt url user{…} botActor{…} } }`) — `agentSessionId` is **not** a scalar field on Linear's `AgentActivity` type (the schema exposes only the relation `agentSession: AgentSession!`), so the session id is read off the nested `agentSession { id }` relation; requesting the non-existent scalar would server-reject the whole mutation under GraphQL strict selection validation. `plan` items are `{content, status:"completed"}`. `initialize` additionally captures the viewer's `organization.id` into `_default_organization_id` (mirroring upstream's `defaultOrganizationId`) for the emitted raw message's `organizationId`; on the emit path `organizationId` falls back to `""` when `_default_organization_id` is unset (no per-request installation context is plumbed — a pre-existing adapter-wide divergence from upstream, which throws `AuthenticationError` when no organization is resolvable). | Upstream `adapter-linear/src/index.ts` calls `@linear/sdk`'s `createAgentActivity({agentSessionId, content, ephemeral?})` / `updateAgentSession(id, {plan})`; the SDK owns the GraphQL document, the `AgentActivityType` enum, and the `AgentActivityPayload`/`Comment`/`sourceComment` resolution | `@linear/sdk` is TypeScript-only (no official Linear Python SDK — cf. the `linear_client` getter row), so the SDK calls are reproduced as raw GraphQL. Mutation names, the `content: JSONObject!` shape, the lowercase enum casing, and the `plan` item shape are **schema-hardened against the published schema** (`https://linear.app/developers/agent-interaction`, `https://linear.app/developers/graphql`, and the SDK's generated GraphQL documents). **Live-tenant verification pending**: the exact mutation/field names and enum casing are confirmed against the published schema/docs but have **not** been exercised against a live Linear agent-session tenant; if a future live run surfaces a casing/field mismatch (e.g. an enum the schema renders differently at runtime), update the mutation strings here. Faithful-port hazards preserved: `status ?? "Thinking..."` → `is not None` (an empty status stays `""`); `[title, output].filter(Boolean).join("\n")` → `"\n".join(x for x in [title, output] if x)` (drops `None` and `""`); `markdown.slice(...).trim()` uses the JS-`.trim()` whitespace set (`_JS_WHITESPACE`, mirroring `adapters/telegram/rich.py`), not Python's broader `str.strip()`; `if delta or force`; `ephemeral: status != "complete"`; the missing-final-flush bare `throw new Error(...)` → `RuntimeError`. Regression coverage: `tests/test_linear_agent_session_emit.py`. |
| Linear comment-thread fetch: root-first ownership check, `children` connection (chat@4.41.1 / vercel/chat#965 — #231) | `_fetch_comment_thread` issues **two** raw GraphQL queries in order: `CommentThreadRoot` (`comment(id:)` plus the nullable scalar `issueId` — the same field the SDK's `comment` document selects; unlike `AgentSession`, `Comment` does expose it in the published schema), then, only once the root's issue id is present and equal to the thread's, `CommentThreadChildren` = `comment(id:) { children(first:, last:) { nodes pageInfo } }` (`forward` → `first`, otherwise `last`, default 50). A missing or foreign issue raises `ValidationError("linear", "Comment does not belong to this issue")` with no children query and no parse; messages are built with the validated root issue id. A `null` root (raw-GraphQL not-found) still returns an empty result without a children query. Fetched messages keep the requested (fixed) thread id, as before. | `linear.comment({ id })` then `linear.comments({ filter: { parent: { id: { eq: commentId } } }, first|last })` (the root `comments` connection); the SDK throws on a missing root; `commentsToMessages` encodes each comment's own id in its thread id. | Query shape only: `Comment.children` and the parent-filtered root `comments` connection return the same replies with the same `first`/`last` pagination, and `children` was the port's existing, already-exercised selection, so keeping it avoids a second query document for the same data. Regression: `tests/test_linear_extended.py::TestFetchMessages::test_fetches_same_issue_comment_thread_in_order` pins the `children` variables (`commentId` + `first`/`last`, no `filter`) for `forward`, `backward` and the default (no direction → `last`). The fixed thread id and empty-on-null-root are pre-existing port behavior, not introduced here. |
| Linear agent-session fetch: raw GraphQL (chat@4.31 / #151 — L5) | The agent-session FETCH/read path (`fetch_messages` session dispatch → `_fetch_agent_session_messages`, plus the append-only guards on `edit_message`/`delete_message` and the `agentSessionId` key in `fetch_thread` metadata) is ported as **raw GraphQL queries** over the existing `_graphql_query` helper, **schema-hardened against Linear's published GraphQL schema** (`linear/packages/sdk/src/schema.graphql` @ master). Two queries: (1) `agentSession(id: String!): AgentSession!` selecting `id`, `issue { id }`, and the nullable `comment { id body parentId createdAt updatedAt url user{…} botActor{…} }` root relation; (2) `comments(filter: CommentFilter, first: Int, last: Int): CommentConnection!` filtered by `{parent: {id: {eq: rootComment.id}}}` for the children, selecting the same `Comment` sub-fields + `pageInfo { hasNextPage endCursor }`. Upstream passes ONLY `first`/`last` here — it never reads `options.cursor` — and the sibling `_fetch_issue_comments`/`_fetch_comment_thread` paths forward no cursor either, so no inbound `after` is plumbed (only `next_cursor` is RETURNED, off `pageInfo.endCursor`). **CRITICAL schema-hardening: `AgentSession` has NO scalar `issueId` field in the published schema** (it exposes only the `issue: Issue` relation alongside `comment`/`sourceComment`/`id`); upstream's `agentSession.issueId` works because `@linear/sdk`'s model derives it from the serialized object, but in raw GraphQL requesting a non-existent `issueId` field would server-reject the whole query (the L4 blocking-bug class). So the issue id is read off the `issue { id }` relation — equivalent to upstream's `agentSession.issueId`. **Ownership check (chat@4.41.1, vercel/chat#974, #231):** when that id is missing (`None`/`""`) or differs from the thread's issue id, `ValidationError("linear", "Agent session does not belong to this issue")` is raised before the root `comment` is read and before the children query — there is **no** `thread.issue_id` fallback (the pre-4.41 `agentSession.issueId ?? thread.issueId` let a thread id naming issue A read a session on issue B). The session query still selects the root `comment` in the same request (upstream lazy-loads it); the content is discarded unread on a failed check. Pagination is direction-driven (`forward` → `first`, otherwise `last`, default limit 50); `next_cursor = endCursor if hasNextPage else None`. Each of `[rootComment, *children.nodes]` is parsed via the upstream `parseMessageFromComment(comment, issueId, agentSession.id)` semantics — reusing L4's `_raw_message_from_source_comment` (user-vs-`botActor` author resolution) + `_parse_agent_session_message` (the `parseMessage` agent-session branch), so **each message's `thread_id` encodes the comment's OWN id** (`linear:{issueId}:c:{comment.id}:s:{agentSessionId}`, NOT a single fixed thread id) and `is_mention=True`. `edit_message`/`delete_message` raise `AdapterError` with the exact upstream strings ("…append-only and cannot be edited" / "…cannot be deleted") for session threads, before any network call. | Upstream `adapter-linear/src/index.ts` calls `@linear/sdk`'s `linear.agentSession(id)` (lazy-resolving `issueId` + the `comment` relation off the SDK model) and `linear.comments({filter, first/last})`; the SDK owns the GraphQL documents and the `Comment`/author resolution. | `@linear/sdk` is TypeScript-only (no official Linear Python SDK — cf. the `linear_client` getter row), so the SDK calls are reproduced as raw GraphQL. Query names, the `agentSession(id)` shape, the root `comments(filter: CommentFilter, first/last)` connection, the `CommentFilter.parent → NullableCommentFilter.id → IDComparator.eq` chain, and every selected `AgentSession`/`Comment`/`User`/`ActorBot`/`PageInfo` field were each **verified field-by-field against the published `schema.graphql`** (this is how the absence of a scalar `AgentSession.issueId` was caught). **Live-tenant verification pending**: the query/field names are confirmed against the published schema but have **not** been exercised against a live Linear agent-session tenant; if a future live run surfaces a field mismatch, update the query strings here. Hazards: the ownership check is deliberately truthy (`not issue_id or issue_id != thread.issue_id`, mirroring upstream's `!issueId`, so `""` never matches `""`); `endCursor ?? undefined` → `is not None`. Regression coverage: `tests/test_linear_agent_session_fetch.py`. |
| `chat/adapters` static adapter catalog (chat@4.31.0, new `./adapters` package subpath) | Not ported | A new SDK-free metadata module (`packages/chat/src/adapters/index.ts`, 19 `test()` cases): types `EnvVar`/`EnvGroup`/`AdapterEnvSpec`/`CatalogAdapter`, an `ADAPTERS` registry of ~25 official + vendor-official adapters, and exports `ADAPTER_NAMES`/`AdapterSlug`/`getAdapter`/`isAdapterSlug`/`listPlatformAdapters`/`listStateAdapters`/`listEnvVars`/`getSecretEnvVars`. Imports no provider SDK, so it's safe for build scripts, onboarding/setup screens, and config-discovery UIs that need package + env-var metadata (incl. secret-masking flags) without loading an adapter. | **Not meaningfully portable verbatim, and not yet needed.** The catalog's spine is TypeScript-ecosystem-specific: every entry is addressed by an **npm `packageName`** (`@chat-adapter/slack`, `@kapso/chat-adapter`, …) and the registry includes ~13 **vendor-official adapters this Python SDK does not ship** (AgentPhone, Kapso, Lark/Feishu, Liveblocks, Beeper Matrix, Resend, Sendblue, ioredis, …). A 1:1 port would ship actively-misleading data to a `pip`-installed consumer (`@chat-adapter/slack` instead of `chat-sdk[slack]`; `get_adapter("kapso")` returning metadata for something uninstallable here). The only Python-applicable slice — per-platform env-var requirements + secret flags — is the same across the 9 platform + 3 state adapters we do ship, but exposing it would be a **new Python-native feature** (a divergent rewrite around `pip` extras, not a port), and no current consumer (chinchill-api configures adapters in code, not via env-var discovery) needs it. Nothing in chat-sdk's core depends on the catalog. Deferred demand-driven: a Python-native `adapters` catalog (our adapters only, `pip`-extra install names, `get_secret_env_vars` masking) can be designed against real requirements if/when a consumer needs config discovery. The unmapped test file is additionally covered by the issue #78 fidelity-scope note. |
| `GoogleChatAdapterConfig` identity fields (#222; upstream `7a192235` / `c3b5a08e`) | `workspace_add_on_service_account_email` and `pubsub_service_account_email` are declared `field(default=None, kw_only=True)` after `disable_signature_verification`, which stays the last *positional* field. Passing an 11th positional argument raises `TypeError` | Optional properties on an options object (`workspaceAddOnServiceAccountEmail`, `pubsubServiceAccountEmail`), which have no positional order | `GoogleChatAdapterConfig` is a positional dataclass. Adding the fields positionally would shift existing callers' arguments, and a shifted argument could land in an identity field the verifier trusts. Pinned by `TestDisableSignatureVerificationFieldOrder` in `tests/test_gchat_verification.py`. #223 adds `bot_user_id` the same way |
| Google Chat webhook JWT verification mechanics (#222; upstream `270b1c25` / `7a192235` / `c3b5a08e`) | OIDC tokens (endpoint-URL and Pub/Sub) are decoded with PyJWT against Google's JWKS (`oauth2/v3/certs`): RS256, strict `aud` equality, `exp`/`iat`/`iss` required, 300 s leeway. `iss` is then checked by hand against {`accounts.google.com`, `https://accounts.google.com`}. Project-number tokens are decoded against the Chat issuer's X.509 certs with the same PyJWT options, and `iss` is then compared by hand with `chat@system.gserviceaccount.com` (not PyJWT's `issuer=`, which PyJWT 2.10.0 matched as a substring, CVE-2024-53861). On both paths, a token whose `exp` is 24 h or more ahead of the wall clock is rejected by hand, because PyJWT has no maximum-lifetime check. Both key sets are fetched with the shared aiohttp session and cached as `(fetched_at, {kid: key})` for a fixed 1 hour on an injectable monotonic clock. A failed or empty fetch is never cached, and an unknown `kid` is rejected without a refetch | `OAuth2Client.verifyIdToken` (federated certs, cached per `Cache-Control`) and `verifySignedJwtWithCertsAsync` with the Chat certs, cached for 1 hour | No maintained async google-auth equivalent exists, and the old `PyJWKClient` did blocking I/O on the event loop. The claim checks are ported to match google-auth-library, including its `exp >= now + 86400` rejection (`DEFAULT_MAX_TOKEN_LIFETIME_SECS_`), and `email_verified` checked by identity (`is True`) and add-on emails compared with `==` and shape-tested with `re.fullmatch`. Google's keys are published hours before use (`max-age` is about 6 h), so a 1-hour TTL does not miss rotations. Pinned by `TestEndpointUrlVerification`, `TestProjectNumberVerification`, `TestDirectTokenTimeClaims` and `TestVerificationKeyCache` |
| Google Chat media `resourceName` validation (#223; upstream `32687038`) | `fetch_data` (fresh and `rehydrate_attachment`) runs `_validate_media_resource_name` before minting a token: a non-string or empty value, `?`, `#`, `%`, backslash, anything outside printable ASCII (whitespace, control and non-ASCII characters), or a `.` / `..` path segment raise `ValidationError("gchat", "Invalid Google Chat attachment resource name: ...")`. Anything else is accepted, since `resourceName` is opaque. The value is then interpolated into `https://chat.googleapis.com/v1/media/{resourceName}?alt=media` | Passes `resourceName` to the googleapis client's `media.download`, which encodes it; no shape check | The Python port builds the URL itself, and `resourceName` can come from serialized `fetch_metadata`. Without the check a tampered value could add query parameters, or `..` segments (which the URL layer normalizes) could point the service-account token at another `/v1/` endpoint. Media errors go through `_handle_google_chat_error(error, "fetchAttachmentData")` as upstream (429 → `AdapterRateLimitError`, otherwise the `_GoogleApiError` carrying the HTTP status is re-raised). Pinned by `tests/test_google_chat_adapter.py::TestRehydrateAttachment::test_rehydrated_fetch_data_rejects_unsafe_resource_names` |
| Google Chat bot identity edge cases (#223; upstream `f485255b`) | `bot_user_id` resolves config → `GOOGLE_CHAT_BOT_USER_ID` with `is not None`, and an empty string then counts as unset. `_normalize_bot_mentions` returns early when the id is unset, so no annotation is rewritten. The "not configured" warning is logged once per adapter, not on every `initialize()`. `parse_message_name` returns a `GoogleChatMessageName` `NamedTuple` (`space_name`, `message_id`) and raises `ValidationError` for a non-`str` id | `botUserId = config.botUserId ?? env ?? undefined`, so `""` is kept (every check treats it as falsy, except that an annotation whose `user.name` is also `""` would be rewritten). With no id, `botUser.name !== this.botUserId` still rewrites a BOT annotation that has no `user.name`. Warns on every `initialize()` | Upstream intends "only annotations matching the configured bot user ID are rewritten"; the early return makes "none when unset" hold for malformed annotations too. Warning once avoids repeat noise when a serverless host re-initializes. Pinned by `tests/test_gchat_comprehensive.py::TestConstructorEnvVarResolution::test_empty_bot_user_id_env_var_is_treated_as_unset`, `::TestConstructorEnvVarResolution::test_empty_configured_bot_user_id_does_not_fall_back_to_env_var` (config `""` wins over the env var, so `is not None` rather than `or`), `::TestNormalizeBotMentionsComprehensive::test_nameless_bot_annotation_not_rewritten_when_bot_user_id_unset` and `::TestInitializeBotUserId::test_warns_once_when_bot_user_id_is_unset` |
| Google Chat forward `fetch_messages` with `limit=0` (#223; upstream `d6343460`) | `limit` resolves with `is not None` (repo-wide port rule), so `0` stays `0`; the upstream clamp `min(max(limit, 1), 1000)` then sends `pageSize=1`, and the call returns at most one message. Negative limits also send `pageSize=1`, as upstream | `const limit = options.limit \|\| 100` turns `0` into `100` before the same clamp, so `limit: 0` fetches a page of 100 | The truthiness fallback is the trap the port rules remove in every adapter (`limit=0` is never silently replaced by a default). Keeping the clamp means `0` still makes one bounded API call rather than an unbounded or default-sized one. Backward `limit=0` is unchanged by #223. Pinned by `tests/test_gchat_api.py::TestFetchMessages::test_forward_page_size_is_clamped` (`(0, "1")`) |
| WhatsApp webhook raw-body logging (#187) | `handle_webhook` logs no body: the pre-verification `"WhatsApp webhook raw body"` debug log is removed, and `"WhatsApp webhook invalid JSON"` logs `{bodyBytes, contentType}` | Upstream `adapter-whatsapp/src/index.ts` (still at chat@4.41.1) logs `body.substring(0, 500)` **before** signature verification and `bodyPreview: body.substring(0, 200)` on invalid JSON | Log hygiene. The body arrives before authentication and carries message content and phone numbers, and DEBUG is commonly on in dev/staging. It is the same weakness class upstream fixed for GitHub (`fc7df9c4`) and GChat/Slack/Teams (`f485255b`). Regression tests: `tests/test_webhook_log_hygiene.py::TestWhatsAppLogHygiene`. Delete this row once upstream drops the logs. |
| Message-content debug logs (#187) | GChat `"message event"` logs `{space, textLength}` (no `sender` display name, no `text` prefix). GChat `"Pub/Sub parsed message"` drops `text` and `author`. The slash-command debug logs (`Chat` `"Incoming slash command"`, Slack `"Processing Slack slash command"`, Discord `"Processing Discord slash command"`) log `textLength` instead of `text`. `textLength` counts characters (code points). | Upstream (chat@4.41.1) still logs the GChat sender display name + `text.slice(0, 50)`, the full Pub/Sub message `text` + author `fullName`, and slash-command `text` | Upstream's `f485255b` removed message text from `chat.ts` "Incoming message", and these are the same class of log. User-authored message text is kept out of DEBUG sinks. Action/reaction logs that still carry `user`/`user_name` stay at parity. Regression tests: `tests/test_webhook_log_hygiene.py` (`TestGoogleChatLogHygiene`, `TestSlackLogHygiene::test_slash_command_log_has_text_length_not_text`, `TestDiscordLogHygiene`, `TestChatLogHygiene::test_slash_command_log_has_text_length_not_text`). |
| WhatsApp contact matching (#236) | `_match_contact` matches a message's contact by `user_id`, `parent_user_id` or `wa_id`. An unmatched message gets `contacts[0]` only when the payload has exactly one contact **and** the message carries no `from` / `from_user_id` / `from_parent_user_id` **and** it is not a `type: "system"` message (those carry their identifiers in `system`); otherwise it gets no contact (its display name falls back to the user id) | Upstream `3e6e866a` (chat@4.39.0, unchanged at chat@4.41.1) matches `user_id` / `wa_id` only and otherwise falls back to `contacts[0]` | In a batched webhook the unmatched contact can belong to another sender. `fields()` fills a message's missing phone/BSUID from its contact, so upstream can combine one sender's phone with another's BSUID and `link()` then merges the two users: both message streams land in one thread, and that thread's outbound route gets overwritten with the other sender's phone. Regression tests: `tests/test_whatsapp_webhook.py::TestBusinessScopedUserIdsPythonSpecific` (`test_does_not_borrow_identity_from_an_unrelated_contact`, `test_does_not_pair_a_single_unmatched_contact_with_another_sender`, `test_system_message_does_not_take_an_unmatched_contact`, `test_uses_the_only_contact_when_the_message_has_no_sender_ids`). |

### Platform-specific gaps

| Area | Python | TS | Rationale |
|------|--------|-----|-----------|
| Teams certificate auth | Config accepted, **throws at startup** (not yet supported) | **Same** — config shape present but throws at startup, not yet supported (`adapter-teams/src/types.ts:31`, `config.ts:13`) | **Parity — neither SDK implements Teams certificate auth yet**; both carry the deprecated `certificate` config that throws at startup. Tracked as issue #58. |
| Teams `dialog_open_timeout_ms` config | Not implemented | Configurable | Low demand |
| Google Chat outbound file delivery | Inbound attachment parsing implemented — `_create_attachment` builds a full `Attachment` (type detection, `fetch_data` download closure, `fetch_metadata`) for every `message.attachment` entry. Outbound file delivery logs "File uploads are not yet supported for Google Chat" and ignores files (posts text/cards only, no media upload). | **Same** — logs "File uploads are not yet supported for Google Chat" and leaves a `media.upload` TODO (`adapter-gchat/src/index.ts:1282-1289`). | **Parity — neither SDK implements Google Chat outbound file delivery.** A Python-only enhancement attempt (PR #112) exists but is unmerged; landing it would be a divergence (upstream lacks it), gated on real need. |
| Discord Gateway WebSocket | HTTP interactions only | Both HTTP and Gateway | Gateway requires persistent connection |
| Discord gateway-only interactions (vercel/chat#490) | Handled on the forwarded-event surface: a `GATEWAY_INTERACTION_CREATE` envelope (raw INTERACTION_CREATE dispatch payload in `data`) is deferred via `POST /interactions/{id}/{token}/callback` (type 5 slash / type 6 component) and routed through the existing HTTP interaction handlers; a malformed forward missing `id`/`token` is logged and skipped | Upstream handles `Events.InteractionCreate` directly on the resident discord.js client via `deferReply()`/`deferUpdate()`; the upstream forwarder never forwards interactions | Python has no resident Gateway client (row above), so gateway-only deployments run an external listener shim that forwards raw dispatch payloads (`x-discord-gateway-token`). Observable wire behavior is identical — same callback REST calls, same handler routing, same `@original` deferred-response resolution. |
| Teams `User-Agent: Vercel.ChatSDK` outbound header | Not set on `aiohttp` calls | Propagated by `botbuilder` 2.0.8 | Python Teams adapter doesn't use `botbuilder` (raw `aiohttp`). Upstream's vercel/chat#415 was a JS-only `botbuilder` SDK bump that flipped `X-User-Agent` → `User-Agent`. No equivalent dependency to bump on the Python side. Setting a `User-Agent` on the ~9 outbound `aiohttp` call sites would be a defense-in-depth nice-to-have; deferred to a follow-up. |
| Teams adapter on `microsoft-teams-apps` (official MS Python SDK) | Inbound webhook + JWT auth, outbound send/edit/delete/typing, and native DM streaming all delegate to the official `microsoft-teams-apps` SDK `App`; Graph reads stay hand-rolled over `aiohttp` | `@microsoft/teams.apps` owns the wire format, throttling, and activity routing | **Delivered in 0.4.30** (issue #93, PRs 1–4). The migration shipped as four PRs: inbound + auth (#143), outbound (#144), native streaming via the SDK `IStreamer` (#145), and this release cut. The 3.12 floor bump (#111) — the migration's prerequisite — landed in 0.4.29. The residual adapter-level divergences (we keep the SDK as auth + transport but route the authenticated activity ourselves; close the streamer in our own `finally` because our bridge owns dispatch) are documented in the Teams divergence rows above. Graph stays hand-rolled (no `msgraph-sdk` / `[graph]` extra). |
| Telegram `get_user().is_bot` | Always `False` (matches upstream — `getChat` does not expose `is_bot`) | Always `false` (same caveat documented in upstream code comment) | The Telegram Bot API's `getChat` endpoint does not surface the `is_bot` field that's available on the `User` object inside incoming `Message` updates. Callers needing bot detection must use `message.author.is_bot` from webhooks instead of `chat.get_user(...).is_bot`. |
| Telegram webhook verification + `update_id` dedupe (4.41 wave, #224) | **Parity** with upstream `c4a359e7` (vercel/chat#858, chat@4.39), `1d2b78d9` (#799, chat@4.38) and the bot-identity scope helper from `7a1150ce` (#813): webhook mode requires `secret_token` unless `allow_unverified_webhooks` / `TELEGRAM_ALLOW_UNVERIFIED_WEBHOOKS=true`; each integer `update_id` is claimed as `telegram:webhook-update:{sha256(bot_user_id)}:{update_id}` (24h) before dispatch; state/identity failure → 503. Python-surface adaptations only: the `ValidationError` text names options in snake_case (`secret_token`, `allow_unverified_webhooks=True`); the shared `getMe` is an `asyncio.Task` awaited through `asyncio.shield` so a cancelled webhook does not cancel it for other waiters; `update_id` claiming mirrors `Number.isInteger`: an integral JSON float (`7.0`, `7e0` — floats under `json.loads`, the number 7 in JS) is normalised to `int` and shares the `...:7` key, while `bool` (an `int` subclass) and fractional floats are not claimed. **Deliberate Python divergence:** a non-`bool` `allow_unverified_webhooks` (e.g. the string `"false"`) raises `ValidationError` instead of being coerced truthy — upstream's TS `boolean` type rules this out at compile time, Python has no such guard and `bool("false")` would silently fail open. `secret_token` now resolves with `??` semantics (`is not None`), replacing the earlier `or` fallback, so an explicit `""` no longer silently picks up the env secret. **Deliberate Python divergence (fixes an upstream bug):** the `getMe` username is cached and reapplied on a repeat `initialize()` — upstream's `ensureBotIdentity` returns early once `webhookScope` is set, so after `initialize` re-applies `chat.getUserName()` the Telegram username is never restored and `@real_bot` / `/cmd@real_bot` stop routing after a shutdown + re-initialize. `allow_unverified_webhooks` is the *last* `TelegramAdapterConfig` field (not alphabetical) so positional callers are not shifted. | Same | Not ported here: the Vercel Connect async `bot_token` resolver (#189) and the polling checkpoint keyed on the same scope (#227). Cross-instance dedupe on Postgres relies on #240, which makes `set_if_not_exists` reclaim expired rows. |
| Telegram inbound parsing, allowlist and typing (4.41 wave, #225) | **Parity** with upstream `4ee187ac` (#612), `2531a422` (#621, Telegram half), `0701679e` (#706, regex cache only), `54eea715` (#742), `53bf73db` (#752), `a0ba9868` (#835), `a18e7922` (#836) and the Telegram half of `b6fa24c6` (#865). Python-surface adaptations only, no behavior divergence beyond the mention-regex fold and the `allowed_user_ids` type check noted here: location coordinates and dice values go through `_js_number_str`, which renders a JSON number as JS `String()` does (`51.0` -> `"51"`, `1e-05` -> `"0.00001"`, `1.5e-07` -> `"1.5e-7"`), since `json.loads` keeps floats that JS prints differently; invoice amounts use `f"{amount / 10**e:.{e}f}"` for `toFixed(e)`; the mention pattern is `@{re.escape(name)}(?![A-Za-z0-9_-])` with `re.IGNORECASE`, because JS `\w` without the `u` flag is ASCII-only while Python's is Unicode (JS `/i` still folds non-ASCII letters, so `re.ASCII` is not used: `@ботик` matches `@БОТИК` on both sides; one residual difference is that Python's Unicode folding also equates the Kelvin sign with `k` and long s `ſ` with `s`, which JS's non-`u` canonicalization never maps onto ASCII, so `@` + U+212A + `bot` mentions a `kbot` in Python only); allowlist ids are stringified with the same `_js_number_str` (`456.0` -> `"456"`); a non-list `allowed_user_ids` (e.g. a bare `"123,456"` string, which Python would iterate per character, where upstream's `.map` throws) raises `ValidationError`; and the acting-user chain (`callback_query.from` -> `message_reaction.user` -> first non-`None` of `message`/`edited_message`/`channel_post`/`edited_channel_post` `.from`) uses `is not None` at every step; the typing action runs as an `asyncio` task created before `Chat.process_message`/`process_slash_command` schedules the handler task (JS fires the request synchronously; in Python both are tasks and run FIFO, so typing still goes first), is passed to `wait_until` when given and otherwise held in `_typing_tasks` so it is not garbage-collected (`disconnect()` cancels and awaits any still pending before closing the aiohttp session, so none reopens it); the download uses the shared aiohttp session with `ClientTimeout(total=30)` (covers the body read) and a running byte count over `response.content.iter_chunked`, raising `NetworkError`. It deliberately does **not** use `chat_sdk.shared.download` (#204): its HTTPS/public-address checks would break self-hosted Bot API servers, which upstream also exempts, and `read_attachment_body` decodes `Content-Encoding` itself while the shared aiohttp session already auto-decompresses. | Same | Not ported here: the media-group (album) path, which must also start typing and honour the allowlist, plus the `pollingGroup` allowlist check (#227); `reply_to_message` parsing and `mentionOnReply` (#228); `business_message.from` in the allowlist chain (#189). |
| WhatsApp `get_user` | Raises `ChatNotImplementedError` (`Chat.get_user` translates to "does not support get_user") | Not implemented upstream either (no `getUser` on the WhatsApp adapter) | WhatsApp Cloud API has no user lookup endpoint — phone numbers are the only stable identifier and there's no equivalent of `users.info` exposed to business apps. Documented explicitly so callers don't expect parity with Slack/Teams/Discord. |
| Messenger `get_user` | Raising stub (`ChatNotImplementedError`); a Graph-API-backed impl is tracked as issue #132 | No `getUser` method on the Messenger adapter | **Parity — upstream Messenger has no user-lookup method**; the Python raising stub matches. (Meta's Graph API *could* back a real implementation, unlike WhatsApp — hence #132 stays open as an enhancement.) |
| Linear agent sessions | **Complete** (5-PR wave, **#151** — Wave D done). All five landed on `main`: L1 agent-session types (`LinearAgentSessionThreadId`, `LinearAgentSessionCommentRawMessage`, `mode`/`kind`), L2 the `:s:{session}` thread-id encode/decode, L3 the webhook PARSE + routing (`_parse_message_from_agent_session_event`, `_handle_agent_session_event`), L4 the agent-activity EMIT path (`post_message`/`start_typing`/`stream` session branches as raw GraphQL — see the "Linear agent-activity emit" divergence row above), and **L5 (this change)**: the agent-session FETCH path (`fetch_messages` → `_fetch_agent_session_messages`, the `edit_message`/`delete_message` append-only guards, and `fetch_thread` `agentSessionId` metadata as raw GraphQL — see the "Linear agent-session fetch" divergence row above). | Full agent-sessions support (`adapter-linear` 4.27.0, `bc94f0a`): parses agent-session webhook events into messages, emits agent activity, fetches the session thread, and routes the agent-session thread id | Largest single gap from the 0.4.30 audit; pre-existing (present since 0.4.29). Closed across the 4.31 wave — tracked in **#151**. |
| `adapter-web` (`@chat-adapter/web`) | Neither half ported. (a) The server-side `WebAdapter` is **deferred** — not yet ported; (b) the client subpaths are out of scope (see Rationale). | Two distinct things: (a) a server-side `WebAdapter` — an `Adapter` implementation serving a browser chat UI over the AI SDK UI stream protocol (`3490a8c`, vercel/chat#444); (b) React/Vue/Svelte client subpaths (`716e934`) | The `WebAdapter` (a) **is portable** to a Python server SDK (it's a standard `Adapter`) and is deferred, not excluded — a future wave can port it. The client subpaths (b) are genuinely browser-only (front-end framework bindings) and are out of scope for a Python server SDK. (Corrects the earlier over-broad "browser-only; no Python runtime" note in CHANGELOG.) **A future `WebAdapter` port must include the web half of upstream `500b7e6d` (vercel/chat#857, chat@4.39.0):** consume only the latest user message from the client-supplied `messages` array and strip tool parts from it, so a client cannot forge tool-approval results and bypass `needs_approval`. The core half (guarded write tools) is ported by #195. |

### Serialization differences

| Area | Python | TS |
|------|--------|-----|
| `to_json()` keys | camelCase (matches TS) | camelCase |
| `from_json()` | Accepts both camelCase and snake_case | camelCase only |
| Slack installation keys | camelCase (matches TS, with snake_case fallback) | camelCase |
| Redis/Postgres queue entries | Different wire format (message serialized via `to_json()`) | `JSON.stringify(entry)` directly |
| `Attachment.data` (bytes) | Not serialized by `to_json()` (bytes aren't JSON-safe). Preserved through in-memory rehydrate paths (`_coerce_attachments`, `Message.from_json{_compat}`) when raw dicts carry the field. A JSON roundtrip through Redis/Postgres state drops `data`; adapters should rely on `fetch_metadata` + `rehydrate_attachment` to reconstruct the download closure instead. | Same — `data` is not part of `SerializedAttachment` |

### Coverage confidence by module

| Module | Confidence | Gap |
|--------|-----------|-----|
| Core (chat/thread/channel) | High | 519 TS tests matched |
| Slack adapter | High | Extensive replay + unit tests |
| Discord adapter | Medium-High | Good replay coverage |
| Teams adapter | Medium | Replay tests; JWT auth hand-rolled |
| Telegram adapter | Medium | Good unit tests; no recorded fixtures |
| Google Chat adapter | Medium-Low | Complex; workspace events undertested |
| WhatsApp adapter | Medium-Low | Media download, group messages undertested |
| GitHub adapter | Medium | PR + issue comment coverage |
| Linear adapter | Medium | Comment + reaction coverage |
| Redis state | Medium | Mocked; no live Redis tests |
| Postgres state | Medium | Mocked unit tests; the live suite runs only with `POSTGRES_TEST_URL` (not in CI) |
