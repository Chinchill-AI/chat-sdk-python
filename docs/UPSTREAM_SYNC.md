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

The `UPSTREAM_PARITY` constant in `chat_sdk/__init__.py` provides programmatic access
to the upstream version this release is synced to.

## How to Sync an Upstream Release

Step-by-step procedure for porting a new upstream release (e.g., `4.26.0`):

```bash
# 1. Update the TS repo and check what changed
cd /tmp/vercel-chat && git fetch origin
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
uv run python scripts/verify_test_fidelity.py
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

`scripts/verify_test_fidelity.py` runs in CI (`.github/workflows/lint.yml`) pinned
to `vercel/chat@4.26.0` (matches the `UPSTREAM_PARITY` constant in
`src/chat_sdk/__init__.py`). **CI runs `--strict`** — the repo ships at 0
missing *for mapped core files* as of `0.4.26.2` and the baseline
(`scripts/fidelity_baseline.json`) is empty. Scope is defined by the
`MAPPING` dict in the script: 8 of 17 `packages/chat/src/*.test.ts` files
today (extending to the remaining 9 is tracked as a follow-up). Unmapped
files are not checked — tightening scope requires editing `MAPPING` and
re-running `--strict`.

Infra guardrails:

- The workflow's `Clone upstream vercel/chat at pinned parity tag` step does
  **not** use `continue-on-error` — a failed clone aborts the job loudly.
- The script itself fails with exit 1 if any mapped TS file is missing under
  `TS_ROOT` (defense in depth against silent skips).

Workflows:

| Goal | Command |
|------|---------|
| Port a missing test | Write the Python test and land it; CI rejects anything that re-introduces a gap |
| Add a Python-only divergence (intentional skip) | Document in [Known Non-Parity](#known-non-parity-with-typescript-sdk), then `--update-baseline` and switch the workflow back to non-strict default for that file if truly unavoidable |
| Upstream sync | After pulling new upstream, run `--strict` — newly-added TS tests appear as missing and CI fails until ported |
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
# Clone or update the TS repo
git clone https://github.com/vercel/chat.git /tmp/vercel-chat
cd /tmp/vercel-chat
git log --oneline -20  # see recent commits

# Compare a specific adapter
diff -u /tmp/vercel-chat/packages/adapter-slack/src/index.ts \
        /tmp/chat-sdk-python/src/chat_sdk/adapters/slack/adapter.py

# Compare core types
diff -u /tmp/vercel-chat/packages/core/src/types.ts \
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
| `packages/adapter-slack/src/index.ts` | `src/chat_sdk/adapters/slack/adapter.py` |
| `packages/state-memory/src/index.ts` | `src/chat_sdk/state/memory.py` |
| `packages/state-redis/src/index.ts` | `src/chat_sdk/state/redis.py` |

## What to Port vs What to Adapt

### Port 1:1

These must stay structurally identical to the TS SDK:

- **Type definitions** (`types.py`): All dataclass shapes, protocol methods, and event types must match TS. This is the interop contract.
- **Concurrency strategies** (`chat.py`): The drop/queue/debounce/concurrent logic, lock TTLs, and dedup keys must produce identical behavior.
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

4. **Concurrency strategy semantics**: Queue drain order, debounce loop iteration limits (20), message superseding logic, and TTL handling must match.

5. **Card element type strings**: `"card"`, `"button"`, `"text"`, `"divider"`, `"actions"`, `"fields"`, `"table"`, `"section"`, `"image"`, `"link"`, `"link-button"` must match exactly.

## Python-Specific Hardening

These exist only in the Python port and have no TS equivalent:

- `shared/errors.py`: Typed adapter error hierarchy (`AdapterRateLimitError`, `AuthenticationError`, `ValidationError`, `NetworkError`, `ResourceNotFoundError`, `AdapterPermissionError`). TS throws plain `Error` objects.
- `testing/__init__.py` + `shared/mock_adapter.py`: Test utilities with `MockAdapter`, `MockStateAdapter`, `create_test_message()`.
- `from __future__ import annotations` everywhere: Enables PEP 604 union syntax (`X | Y`) without runtime cost.
- Input validation on adapter config dataclasses (e.g., rejecting empty `signing_secret`).
- `ContextVar`-based request context in Slack adapter (instance variable, not class variable).

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
| Markdown parser | Subset of CommonMark (no setext headings, indented code, HTML, escaped chars, backtick spans >1) | Full CommonMark via remark | See [DECISIONS.md](DECISIONS.md#why-hand-rolled-markdown-parser) |
| `_remend` streaming repair | Parity-based emphasis closing | `remend` npm package | Simplified; handles common cases |
| `walkAst` | Deep-copies the tree (immutable) | Mutates the tree in place | Python convention; safer |
| `ast_to_plain_text` | Joins blocks with `\n` | Concatenates directly | More readable output |
| `renderPostable` on unknown input | Returns `str(message)` | Throws `Error` | More resilient |
| Chat resolver | 3-level: explicit → ContextVar → global | Process-global singleton | See [DECISIONS.md](DECISIONS.md#why-3-level-chat-resolver) |
| PostableObject history | Cached in message history with real message ID | Not cached (skips history) | Upstream gap — posted messages should appear in thread/channel history |
| Teams `msteams` transport key | Stripped from action values | Not stripped | Upstream gap — SDK-injected metadata should not leak to handlers |
| Fallback streaming with whitespace-only streams | Placeholder cleared to `" "` on final edit | Placeholder left visible (`"..."` stuck) | Upstream 4.26 guards against empty edits but leaves the placeholder stranded on the message. We issue one final `edit_message(" ")` so the placeholder disappears when no real content was produced. |
| Google Chat `<url\|text>` round-trip | `to_ast()` / `extract_plain_text()` parse the custom-label syntax back to a link node / bare label | `toAst()` / `extractPlainText()` leave `<url\|text>` as raw text (or parse the whole string as an autolink with a malformed URL) | Upstream 4.26 emits `<url\|text>` in `from_ast` but never taught the reverse direction to parse it. A message posted with `[label](url)` then read back through `fetch_messages` comes back as unstructured text (or worse, a link node with the full `url\|text` as its URL) in upstream. We close the round-trip via an AST placeholder substitution: each `<url\|text>` is extracted to a private-use sentinel, Markdown is parsed on the rest, and link nodes are injected where the sentinels landed. This avoids the Markdown parser's incomplete handling of balanced-parens link destinations, so URLs like `https://en.wikipedia.org/wiki/Foo_(bar)` round-trip intact. |
| `from_json(data, adapter=X)` → `_adapter_name` | Updated to `X.name` so `to_json()` reflects the bound adapter | Kept at `json.adapterName`, so re-serialization can emit a name that no longer matches the actual adapter | Upstream TS has the same gap but only exposes it via the `fromJSON(json, adapter?)` overload. In Python we lean on this API more (explicit `chat=` / explicit `adapter=` is preferred over the singleton). We sync the name on rebind so runtime and serialize agree. |
| Google Chat link labels with `\|` / `>` / `]` / newline, empty labels, URLs without a scheme, or URLs containing `\|` / `>` | Fall back to `text (url)` (or bare URL for empty labels) when the `<url\|text>` form can't round-trip safely | Always `<url\|text>`, producing malformed or un-parseable output | Google Chat's `<url\|text>` has no escape for `\|` or `>`; `]` breaks our own `to_ast()` regex (which converts `<url\|text>` to Markdown `[text](url)`, and Markdown closes the label at the first `]`); newline breaks the single-line form; schemeless URLs and URLs containing `\|`/`>` don't match our reverse parser. Upstream emits the malformed form regardless; we fall back to the pre-4.26 `text (url)` form (or the bare URL for empty labels) so the label/URL stays intact and Google Chat's auto-link detection still fires for http(s). |
| Google Chat heading rendering | `#`-headings emit as `*text*` (bold) so they're visually distinct | Falls through to default node-to-text (plain concatenation) | Google Chat has no heading syntax; emitting plain text loses the visual hierarchy. Bold is the closest approximation the platform supports. |
| Google Chat image rendering | Images emit as `{alt} ({url})` or bare `url` | No image branch — falls through to default which concatenates children only, dropping the URL | Upstream silently drops image URLs when rendering to Google Chat text. We preserve the URL so the message content isn't lost. |
| Fallback streaming stream-exception capture | `_fallback_stream` captures exceptions from the stream iterator, flushes whatever content was already rendered, awaits `pending_edit`, and re-raises after cleanup | `try/finally` only — exception propagates immediately, `pendingEdit` is un-awaited, and the placeholder is stranded as `"..."` | Upstream leaves a hard UX failure when streams crash mid-flight (common: LLM connection drops): placeholder visible forever, orphan background task. We flush + clean up before re-raising so the caller still sees the original error and users see the partial content instead of a spinner. |
| Fallback streaming final SentMessage content | SentMessage + final edit carry `final_content` (remend'd — inline markers auto-closed) | SentMessage + final edit carry raw `accumulated` | Narrow UX refinement. If a stream ends with an unclosed `*`/`~~`/etc., upstream ships the unclosed marker; we run `_remend` so the user sees a clean final message. Not observable in the common case where streams close their own markers. |
| Teams divider rendering | `card_to_adaptive_card` hoists `separator: True` onto the next sibling (or emits a non-empty Container for a trailing divider) | `convertDividerToElement` emits an empty `Container` with `separator: True` | Upstream shares the same bug: Microsoft Teams renders an empty Container at zero height, so the separator line is effectively invisible. Python port fixes locally (issue #45) rather than blocking on upstream. |
| `SlackAdapter.current_token` / `current_client` | Public `@property` accessors that return the request-context-bound token and a preconfigured `AsyncWebClient` | Not exposed (`getToken()` is private on the TS `SlackAdapter`) | Python-only addition (issue #47). Downstream code that calls Slack Web APIs from inside a handler — email resolution, user profile fetches, reaction bookkeeping — otherwise depends on underscore-prefixed helpers. |
| `ConcurrencyConfig.max_concurrent` | Enforced via `asyncio.Semaphore` in the `"concurrent"` strategy path; rejects non-integer or `<= 0` values, and rejects any non-`None` `max_concurrent` paired with a non-`"concurrent"` strategy | Accepted into the config type with docstring "Default: Infinity" but never read (3 writes, 0 reads) | Silent correctness bug upstream — consumers setting `max_concurrent=N` with `strategy="concurrent"` reasonably expect an N-way bound on in-flight handlers. We honor the documented contract via a semaphore and fail-fast on misconfiguration so it's never silent. `max_concurrent=None` stays compatible with every strategy (unbounded default). |
| Redis lock token format | `{token_prefix}_{ms}_{secrets.token_hex(16)}` — always 32 hex chars, CSPRNG-sourced | `ioredis_${Date.now()}_${Math.random().toString(36).substring(2, 15)}` — base36, ≤13 chars, **not** CSPRNG | Interop via `IoRedisStateAdapter(token_prefix="ioredis")` still works for lock-release (release/extend compare by full-string equality, and each runtime only releases what it issued), but the token byte-shape diverges. Intentional — CSPRNG should not be regressed to `Math.random()` for cosmetic byte-for-byte compatibility. |
| `StreamingPlan.is_supported()` / `get_fallback_text()` | Raise `RuntimeError` to fail loudly if a generic posting path (e.g. `ChannelImpl.post`, `post_postable_object`) tries to consume a `StreamingPlan` as a normal `PostableObject` | Silently return `True` / `""` — `ChannelImpl.post` would route through `postPostableObject` and post an empty-string fallback | Prevents `StreamingPlan` being silently routed through non-stream-aware posting paths where upstream would post a blank message or attempt a wrong-shape `adapter.post_object("stream", ...)` call. Internal dispatch is guarded by the `kind == "stream"` short-circuit in `post_postable_object` / `Thread.post`; this also protects third-party code that duck-types PostableObjects. |
| `rehydrate_attachment` URL allowlist (Slack / Teams / Google Chat) | Validates the downloaded URL's scheme + host against a per-adapter allowlist inside the fetch closure; raises `ValidationError` on untrusted hosts before forwarding bearer tokens | No validation — `fetchData` blindly GETs `fetchMetadata.url` and forwards the workspace/bot token | SSRF + token-exfil risk upstream: after the 4.26 `rehydrateAttachment` hook lands, a crafted `fetchMetadata` in persisted state can redirect auth'd downloads to an arbitrary host. Python port enforces `CLAUDE.md`'s "Validate external URLs before requests (SSRF)" rule. Allowlist: Slack = `{files.slack.com, slack.com, *.slack.com, *.slack-edge.com}`; Teams = `{smba.trafficmanager.net, graph.microsoft.com, attachments.office.net, *.botframework.com, *.graph.microsoft.com, *.sharepoint.com, *.officeapps.live.com, *.office.com, *.office365.com, *.onedrive.com, *.microsoft.com}`; Google Chat = `{chat.googleapis.com, googleapis.com, *.googleapis.com, *.googleusercontent.com, *.google.com}`. |
| `_rehydrate_message` with `Message` input | Falls through to the `rehydrate_attachment` pass even when the dequeued entry is already a `Message` instance | Early-returns on `raw instanceof Message` before rehydration | The Python port's Redis + Postgres `dequeue()` upgrade raw JSON to `Message.from_json(...)` before returning (upstream's dequeue returns the raw JSON.parse'd dict). Upstream's `instanceof Message` shortcut therefore only fires for in-memory state, but ours would fire for persistent backends too, leaving `fetch_data` stripped forever. The rehydrate pass still skips any attachment that already has `fetch_data`, so in-memory callers pay no cost. |

### Platform-specific gaps

| Area | Python | TS | Rationale |
|------|--------|-----|-----------|
| Teams certificate auth | Rejected (app password only) | Supported | Low demand; can add later |
| Teams `dialog_open_timeout_ms` config | Not implemented | Configurable | Low demand |
| Google Chat file uploads | Ignored in message parse | Supported | API complexity; can add later |
| Discord Gateway WebSocket | HTTP interactions only | Both HTTP and Gateway | Gateway requires persistent connection |

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
| Postgres state | Medium | Mocked; no live Postgres tests |
