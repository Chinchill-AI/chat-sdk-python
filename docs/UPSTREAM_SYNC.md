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
to `vercel/chat@4.30.0` (matches the `UPSTREAM_PARITY` constant in
`src/chat_sdk/__init__.py`). **CI runs `--strict`** — the repo ships at 0
missing *for mapped core files* as of `0.4.30`. Scope is defined by the
`MAPPING` dict in the script (extending to the remaining unmapped
`packages/chat/src/*.test.ts` files is tracked as issue #78). Unmapped
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
| `packages/state-ioredis/src/index.ts` | `src/chat_sdk/state/redis.py` (`IoRedisStateAdapter`) |
| `packages/state-pg/src/index.ts` | `src/chat_sdk/state/postgres.py` |

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
| Teams inbound activity routing (issue #93 PR 1) | `BridgeHttpAdapter.dispatch` feeds webhooks through the SDK `HttpServer` (JWT validation), but the adapter **overrides `app.server.on_request`** with `_dispatch_activity` instead of letting the SDK's default router run. Our callback dumps the lenient `CoreActivity` to a camelCase dict and routes by `type` to the existing handler logic. | Upstream `@chat-adapter/teams@4.30.0` registers `app.on("message" / "card.action" / …)` and lets `@microsoft/teams.apps` route via its typed dispatcher; handlers receive `ctx.activity` as a strongly-typed `IMessageActivity` etc. | The Python SDK's default `on_request` (`App._process_activity_event` → `ActivityProcessor.process_activity`) runs `ActivityTypeAdapter.validate_python` (strict per-activity validation — `recipient`/`id` required) **and** a live `api.users.token.get` network call inside `_build_context` before any handler fires. Minimal serverless webhook payloads (and the adapter's dict-based handler logic) can't survive strict validation, and the token fetch would make an unwanted outbound call per inbound activity. We keep the SDK as the **auth + transport** layer (JWT genuinely validated by its `TokenValidator`) but route the already-authenticated activity ourselves through the lenient `CoreActivity`, preserving the exact pre-migration handler behavior. The SDK `on_message`/`on_card_action`/… decorators are still registered for parity/forward-compat. Regression coverage: `tests/test_teams_extended.py::TestActivityTypes`, `tests/test_teams_coverage.py::TestSdkInboundAuth`, `tests/test_teams_bridge.py`. |
| Teams dialog/modal inbound (issue #93 PR 1) | `on_dialog_open` / `on_dialog_submit` are registered on the SDK App but only cache user context (no `process_modal_submit`, no task-module response) | Upstream `@chat-adapter/teams@4.30.0` `handleDialogOpen`/`handleDialogSubmit` drive `chat.processModalSubmit` + `modalToAdaptiveCard` | The pre-migration Python Teams adapter never implemented modal/dialog inbound processing, so PR 1 (inbound + auth plumbing) preserves that behavior rather than introducing new modal handling. Wiring dialogs to `chat.process_modal_submit` is tracked as a later wave of the #93 migration. |
| Fallback streaming with whitespace-only streams (non-Teams adapters) | Placeholder cleared to `" "` on final edit | Placeholder left visible (`"..."` stuck) | Upstream 4.26 guards against empty edits but leaves the placeholder stranded on the message. We issue one final `edit_message(" ")` so the placeholder disappears when no real content was produced. Teams does not route through `_fallback_stream` (DMs stream natively through the SDK `IStreamer`; group chats accumulate-and-post), so this divergence applies only to Slack / Discord / GitHub / Telegram / Google Chat / Linear / WhatsApp. |
| Google Chat `<url\|text>` round-trip | `to_ast()` / `extract_plain_text()` parse the custom-label syntax back to a link node / bare label | `toAst()` / `extractPlainText()` leave `<url\|text>` as raw text (or parse the whole string as an autolink with a malformed URL) | Upstream 4.26 emits `<url\|text>` in `from_ast` but never taught the reverse direction to parse it. A message posted with `[label](url)` then read back through `fetch_messages` comes back as unstructured text (or worse, a link node with the full `url\|text` as its URL) in upstream. We close the round-trip via an AST placeholder substitution: each `<url\|text>` is extracted to a private-use sentinel, Markdown is parsed on the rest, and link nodes are injected where the sentinels landed. This avoids the Markdown parser's incomplete handling of balanced-parens link destinations, so URLs like `https://en.wikipedia.org/wiki/Foo_(bar)` round-trip intact. |
| `from_json(data, adapter=X)` → `_adapter_name` | Updated to `X.name` so `to_json()` reflects the bound adapter | Kept at `json.adapterName`, so re-serialization can emit a name that no longer matches the actual adapter | Upstream TS has the same gap but only exposes it via the `fromJSON(json, adapter?)` overload. In Python we lean on this API more (explicit `chat=` / explicit `adapter=` is preferred over the singleton). We sync the name on rebind so runtime and serialize agree. |
| Google Chat link labels with `\|` / `>` / `]` / newline, empty labels, URLs without a scheme, or URLs containing `\|` / `>` | Fall back to `text (url)` (or bare URL for empty labels) when the `<url\|text>` form can't round-trip safely | Always `<url\|text>`, producing malformed or un-parseable output | Google Chat's `<url\|text>` has no escape for `\|` or `>`; `]` breaks our own `to_ast()` regex (which converts `<url\|text>` to Markdown `[text](url)`, and Markdown closes the label at the first `]`); newline breaks the single-line form; schemeless URLs and URLs containing `\|`/`>` don't match our reverse parser. Upstream emits the malformed form regardless; we fall back to the pre-4.26 `text (url)` form (or the bare URL for empty labels) so the label/URL stays intact and Google Chat's auto-link detection still fires for http(s). |
| Google Chat heading rendering | `#`-headings emit as `*text*` (bold) so they're visually distinct | Falls through to default node-to-text (plain concatenation) | Google Chat has no heading syntax; emitting plain text loses the visual hierarchy. Bold is the closest approximation the platform supports. |
| Google Chat image rendering | Images emit as `{alt} ({url})` or bare `url` | No image branch — falls through to default which concatenates children only, dropping the URL | Upstream silently drops image URLs when rendering to Google Chat text. We preserve the URL so the message content isn't lost. |
| Fallback streaming stream-exception capture (non-Teams adapters) | `_fallback_stream` captures exceptions from the stream iterator, flushes whatever content was already rendered, awaits `pending_edit`, and re-raises after cleanup | `try/finally` only — exception propagates immediately, `pendingEdit` is un-awaited, and the placeholder is stranded as `"..."` | Upstream leaves a hard UX failure when streams crash mid-flight (common: LLM connection drops): placeholder visible forever, orphan background task. We flush + clean up before re-raising so the caller still sees the original error and users see the partial content instead of a spinner. This divergence does not apply to Teams: Teams DMs stream natively through the SDK `IStreamer` (`_stream_via_emit`), and a non-cancel iterator exception propagates straight to the caller while the SDK closes the streamer after the handler returns. |
| Slack `stream()` to a top-level DM (empty `thread_ts`) | Normalizes the empty `thread_ts` to `None` and degrades to a single accumulated `post_message` call so the streamed reply still lands (chat-sdk-python#94) | Passes the empty `thread_ts` straight to `chat.startStream` (`adapter-slack/src/index.ts` `stream()`), which Slack rejects (`invalid_thread_ts`) — the streamed DM reply is silently dropped | Top-level DM messages intentionally encode `threadTs=""` on both sides (`_handle_message_event` / `handleMessageEvent`, "matches openDM subscriptions") — that part is faithful to upstream and **not** a bug. The bug is that upstream's `stream()` never reconciled that legitimate value with `startStream`'s requirement for a non-empty `thread_ts`; `postMessage` accepts no `thread_ts` for DMs, so we degrade instead of erroring. Tracked for contribution upstream — remove this divergence once vercel/chat fixes `stream()` to handle empty-`thread_ts` DM thread ids. |
| Fallback streaming final SentMessage content (non-Teams adapters) | SentMessage + final edit carry `final_content` (remend'd — inline markers auto-closed) | SentMessage + final edit carry raw `accumulated` | Narrow UX refinement. If a stream ends with an unclosed `*`/`~~`/etc., upstream ships the unclosed marker; we run `_remend` so the user sees a clean final message. Not observable in the common case where streams close their own markers. Teams DMs stream through the SDK `IStreamer` and the Teams accumulate-and-post path ships raw `accumulated` via `post_message`, matching upstream; this divergence applies only to the remaining adapters that still route through `_fallback_stream`. |
| Teams group-chat / channel streaming via accumulate-and-post | `TeamsAdapter.stream` accumulates the full text and issues a single `post_message` (SDK-backed) instead of post+edit, even for group chats and channel threads | Same (`@chat-adapter/teams@4.30.0`: `if (activeStream && !activeStream.canceled) … else { accumulate; postMessage }`) — no divergence at the adapter level | Documented for clarity: the Python port matches upstream's behavior of avoiding the post+edit flicker where Teams doesn't support native streaming. The buffered fallback routes through the same SDK `App.send` path as a normal `post_message`. |
| Teams native streaming via the SDK `IStreamer` (DMs) | `TeamsAdapter._handle_message_activity` captures a Teams SDK `IStreamer` (`microsoft_teams.apps.StreamerProtocol` / `HttpStream`) for DMs via `app.activity_sender.create_stream(ref)`, registers it in `_active_streams`, and `await`s a `processing_done` gate (a wrapped `wait_until` shim) so the streamer stays alive while the handler streams. `stream()` → `_stream_via_emit` calls `stream.emit(text)` per chunk and NEVER calls `close()`; the adapter's `_handle_message_activity` `finally` calls `stream.close()` once (the lifecycle-owner role the SDK App's `process_activity` plays upstream). | `@chat-adapter/teams@4.30.0` `index.ts` does exactly this: `this.activeStreams.set(threadId, ctx.stream)`, build `processingDone` + wrapped `waitUntil`, `await processingDone`, `streamViaEmit` calls `stream.emit(text)` and never `close()` (the SDK App auto-closes after the handler returns). | **No adapter-level divergence.** The only mechanical difference is the close call site: upstream lets the SDK `App` auto-close `ctx.stream` because the SDK owns dispatch; our bridge overrides `server.on_request`, so we own dispatch and reproduce the close in `_handle_message_activity`'s `finally`. The SDK `HttpStream.close` no-ops when the stream was canceled or had no content, so closing in both success and cancel paths is safe (matching the SDK App, which closes in both its success and `StreamCancelledError` branches). Cancellation is detected via `stream.canceled` (checked before each emit) and by catching `StreamCancelledError` (other exceptions re-raise). The first chunk id is captured via `on_chunk` and awaited only when text was emitted and the stream was not canceled. Replaces the prior hand-rolled wire format, the 1500ms emit throttle, and the `RawMessage.text` / `update_interval_ms` divergences (all unwound in #93 PR 3). |
| Teams streaming throttle / Bot Framework wire format ownership | The SDK `HttpStream` owns the entire Bot Framework streaming wire format (`streamType`/`streamSequence`/`streamId`), the inter-flush throttle, and 429 retry. We hand it text via `emit()` and read back the assigned id via `on_chunk`. | Same — `@microsoft/teams.apps`'s `IStreamer` owns all of this in the JS SDK. | **THROTTLE PARITY (verified against the installed SDK source — `microsoft-teams-apps==2.0.13.4`):** the SDK throttles and is 429-safe, so we don't regress to rate-limit errors: (1) `http_stream.py:266` — after a flush, if more is queued, the next flush is scheduled via `call_later(0.5, …)`, i.e. a 500ms inter-flush delay (the module docstring at `http_stream.py:39-41` states this is "to ensure we dont hit API rate limits with Microsoft Teams"); (2) `http_stream.py:283,290` — `add_stream_update(self._index)` stamps the Bot Framework `streamSequence` and `self._index` increments per stream activity; (3) `http_stream.py:285-288` — each chunk send goes through `retry(..., RetryOptions(max_delay=4.0, max_attempts=8))`, so transient 429s are retried with backoff; (4) `http_stream.py:180-201` — `close()` waits for the queue to drain (`_wait_for_id_and_queue`) and the final `add_stream_final()` send also goes through `retry()`. **A LIVE Teams check (streaming a real long response without a 429) is out of scope for this build and is flagged for the reviewers/maintainer.** |
| Teams divider rendering | `card_to_adaptive_card` hoists `separator: True` onto the next sibling (or emits a non-empty Container for a trailing divider) | `convertDividerToElement` emits an empty `Container` with `separator: True` | Upstream shares the same bug: Microsoft Teams renders an empty Container at zero height, so the separator line is effectively invisible. Python port fixes locally (issue #45) rather than blocking on upstream. |
| `SlackAdapter.current_token` / `current_token_async` / `current_client` | Public accessors that return the request-context-bound token and a preconfigured `AsyncWebClient`. `current_token` (sync `@property`) reads the cache; `current_token_async` (async method) invokes the resolver on demand for callable `bot_token` configs used outside `handle_webhook`. | Not exposed (`getToken()` is private on the TS `SlackAdapter`) | Python-only addition (issue #47). Downstream code that calls Slack Web APIs from inside a handler — email resolution, user profile fetches, reaction bookkeeping — otherwise depends on underscore-prefixed helpers. The async variant is required because the sync `current_token` cannot drive an async resolver (see `bot_token` resolver invocation site row). |
| `SlackAdapterConfig.webhook_verifier` | Optional `Callable[[request, body], bool \| str \| None \| Awaitable[...]]` that fully replaces signing-secret HMAC verification. Lets callers integrate platform-managed verification (e.g. Slack Enterprise Grid edge proxies, KMS-signed payloads, test harness escape hatches). `webhook_verifier` takes precedence over both `signing_secret` (config) and the `SLACK_SIGNING_SECRET` env var — when set, both are ignored. | Upstream has its own `webhookVerifier` field on `SlackAdapterConfig` and matches this precedence direction after vercel/chat#468 (commit `0f0c203`, chat@4.29.0). | Behavior parity restored in 0.4.29 sync wave. The original Python port (PR #87, 0.4.27) preferred `signing_secret` to match upstream's intent at that time; upstream reversed itself in #468 so an env-configured `SLACK_SIGNING_SECRET` could not silently shadow a verifier the caller wired up. This port follows. The contract is documented as a SECURITY surface in `slack/types.py` (`SlackWebhookVerifier`): returning truthy passes the request, falsy/None rejects 401, and a `str` substitutes the request body before dispatch. |
| Slack `bot_token` resolver invocation site | Resolved once at `handle_webhook` entry into a per-request `ContextVar`; sync `_get_token` reads it for the rest of the request. Public adapter methods (`post_message`, `add_reaction`, `upload_files`, etc.) DON'T re-resolve — calling them outside `handle_webhook` (cron jobs, background tasks) with a callable `bot_token` raises `AuthenticationError` until the caller awaits `current_token_async()` first | TS `getToken` is async and resolves on EVERY API call site, so cron/background usage just works | Python keeps `_get_token` sync to preserve the existing pre-resolver public API and to avoid threading `await` through every adapter call site. The trade-off is that callable-`bot_token` usage outside the webhook flow needs an explicit `await adapter.current_token_async()` (or `await adapter._resolve_default_token()`) before the first sync-token-consuming call. Static-string `bot_token` is unaffected (cache primed at construction). |
| Slack `bot_token` resolver caching scope | Single resolution per request, cached in `_resolved_default_token` `ContextVar` for the rest of that request | Provider invoked on every API call within a single request | Within-request caching enables the sync `_get_token` path. Functionally equivalent for rotation (TTL >> request lifetime); diverges only if the resolver is itself sensitive to per-call freshness (rare). |
| `ConcurrencyConfig.max_concurrent` | Enforced via `asyncio.Semaphore` in the `"concurrent"` strategy path; rejects non-integer or `<= 0` values, and rejects any non-`None` `max_concurrent` paired with a non-`"concurrent"` strategy | Accepted into the config type with docstring "Default: Infinity" but never read (3 writes, 0 reads) | Silent correctness bug upstream — consumers setting `max_concurrent=N` with `strategy="concurrent"` reasonably expect an N-way bound on in-flight handlers. We honor the documented contract via a semaphore and fail-fast on misconfiguration so it's never silent. `max_concurrent=None` stays compatible with every strategy (unbounded default). |
| `ConcurrencyConfig.max_concurrent` slot scope | **Single global `asyncio.Semaphore`** — caps total in-flight handlers across all threads to `max_concurrent` | **Per-thread slot map** — `acquireConcurrentSlot(threadId, maxConcurrent)` keys the in-flight counter by `threadId`, so each thread has its own N-way bound | When upstream caught up (vercel/chat#419) it implemented per-thread slots; the Python port shipped earlier with a global semaphore and the slot-scope distinction wasn't visible in the original divergence row. Result: a deployment with `max_concurrent=2` and 100 active threads serializes everything globally on Python (peak in-flight = 2 across all threads) but allows 200 concurrent handlers on TS (2 per thread × 100). The `chat.test.ts > should track slots per thread independently` fidelity entry is `pytest.mark.skip`-ped in `tests/test_chat_faithful.py` until the implementation is restructured to a `dict[thread_id, asyncio.Semaphore]` (with cleanup-on-empty to avoid unbounded growth). Tracked as a follow-up. |
| Redis lock token format | `{token_prefix}_{ms}_{secrets.token_hex(16)}` — always 32 hex chars, CSPRNG-sourced | `ioredis_${Date.now()}_${Math.random().toString(36).substring(2, 15)}` — base36, ≤13 chars, **not** CSPRNG | Interop via `IoRedisStateAdapter(token_prefix="ioredis")` still works for lock-release (release/extend compare by full-string equality, and each runtime only releases what it issued), but the token byte-shape diverges. Intentional — CSPRNG should not be regressed to `Math.random()` for cosmetic byte-for-byte compatibility. |
| `StreamingPlan.is_supported()` / `get_fallback_text()` | Raise `RuntimeError` to fail loudly if a generic posting path (e.g. `ChannelImpl.post`, `post_postable_object`) tries to consume a `StreamingPlan` as a normal `PostableObject` | Silently return `True` / `""` — `ChannelImpl.post` would route through `postPostableObject` and post an empty-string fallback | Prevents `StreamingPlan` being silently routed through non-stream-aware posting paths where upstream would post a blank message or attempt a wrong-shape `adapter.post_object("stream", ...)` call. Internal dispatch is guarded by the `kind == "stream"` short-circuit in `post_postable_object` / `Thread.post`; this also protects third-party code that duck-types PostableObjects. |
| `rehydrate_attachment` URL allowlist (Slack / Teams / Google Chat / Twilio) | Validates the downloaded URL's scheme (https) + host against a per-adapter allowlist inside the fetch closure; raises `ValidationError` on untrusted hosts before forwarding bearer/Basic credentials | No validation — `fetchData` blindly GETs `fetchMetadata.url` (Twilio: `fetchMetadata.twilioMediaUrl`) and forwards the workspace/bot token (Twilio: the account SID + auth token as HTTP Basic) | SSRF + token-exfil risk upstream: after the 4.26 `rehydrateAttachment` hook lands, a crafted `fetchMetadata` in persisted state can redirect auth'd downloads to an arbitrary host. The Twilio adapter (vercel/chat#558) shares the exact pattern — `fetchTwilioMedia` GETs the rehydrated URL with the adapter's Basic auth and no host check. Python port enforces `CLAUDE.md`'s "Validate external URLs before requests (SSRF)" rule. The check runs inside the download closure (not at build time) so an attachment trusted at parse time still fails closed if the allowlist tightens later. Allowlist: Slack = `{files.slack.com, slack.com, *.slack.com, *.slack-edge.com}`; Teams = `{smba.trafficmanager.net, graph.microsoft.com, attachments.office.net, *.botframework.com, *.graph.microsoft.com, *.sharepoint.com, *.officeapps.live.com, *.office.com, *.office365.com, *.onedrive.com, *.microsoft.com}`; Google Chat = `{chat.googleapis.com, googleapis.com, *.googleapis.com, *.googleusercontent.com, *.google.com}`; Twilio = `{twilio.com, api.twilio.com, *.twilio.com, *.twiliocdn.com}`. Regression coverage: `tests/test_twilio_adapter.py::TestRehydrateAttachment::test_media_downloader_refuses_untrusted_hosts`. |
| `_rehydrate_message` with `Message` input | Falls through to the `rehydrate_attachment` pass even when the dequeued entry is already a `Message` instance | Early-returns on `raw instanceof Message` before rehydration | The Python port's Redis + Postgres `dequeue()` upgrade raw JSON to `Message.from_json(...)` before returning (upstream's dequeue returns the raw JSON.parse'd dict). Upstream's `instanceof Message` shortcut therefore only fires for in-memory state, but ours would fire for persistent backends too, leaving `fetch_data` stripped forever. The rehydrate pass still skips any attachment that already has `fetch_data`, so in-memory callers pay no cost. |
| Slack Socket Mode reconnect loop | Outer reconnect loop on top of `slack_sdk.socket_mode.aiohttp.SocketModeClient` (which itself has `auto_reconnect_enabled=True`). Exponential backoff (1s → 30s) with explicit shutdown signaling and a tracked `asyncio.Task` so `disconnect()` can cancel cleanly | Single `SocketModeClient` instance from `@slack/socket-mode`; relies entirely on the package's internal reconnect | Hazard #5 (async task lifecycle): a long-lived WebSocket needs an explicit shutdown path so `disconnect()` doesn't leak the loop, and a guarded outer reconnect path so the adapter survives `connect()` itself raising (which the inner client doesn't retry). Inner auto-reconnect still runs; the outer loop is belt-and-suspenders, not a divergence in observable behavior. |
| Slack Socket Mode listener serverless variant | Not ported | `startSocketModeListener()` / `runSocketModeListener()` open a transient socket for `durationMs` and forward events via HTTP POST | Vercel-specific pattern (cron-triggered ephemeral listener with `waitUntil`). The forwarded-event receiver (`x-slack-socket-token` handling in `handle_webhook`) is ported so a separate Python process can run the long-lived listener; the deployment glue itself isn't part of the SDK. |
| Slack DM block-action threading (#133/#137) | `_handle_block_actions` sets `thread_ts=""` for a top-level DM button click (never falls back to the clicked message's own `ts`), so a handler's `event.thread.post(...)` does not spawn a phantom "1 reply" thread in the DM. Mirrors `_handle_message_event`'s DM handling (`thread_ts=""` for top-level DMs). | `handleBlockActions` (`adapter-slack/src/index.ts:1455-1456,1470`) computes `thread_ts \|\| container.thread_ts \|\| messageTs` and encodes `threadTs \|\| messageTs \|\| ""` — it falls back to the clicked message's `ts` even for DMs, so a DM button click spawns a phantom reply thread. Upstream's `handleMessageEvent` *does* empty-case DMs (`:2158`), but `handleBlockActions` does **not** — an upstream internal inconsistency. | Hard UX failure with no workaround (phantom "1 reply" threads on DM button clicks). We extend upstream's own DM-message convention to the block-action path. The resulting empty DM `thread_ts` is consumed unguarded by `fetch_messages` → `conversations.replies(ts="")` — identical to upstream (`fetchMessages` `:4178` has no empty-`thread_ts` guard) and to the faithful DM-message path; a `conversations.history` fallback for empty DM `thread_ts` is a separate, codebase-wide follow-up. The block-action fix should be contributed upstream (cf. PR #107's stream() divergence) to restore parity. |
| `GitHubAdapter.octokit` native client getter (vercel/chat#459, #478) | Not exposed | `get octokit(): Octokit` (plus deprecated `client` alias) returns the underlying Octokit — fixed instance in PAT/single-tenant App mode, per-installation client resolved from `AsyncLocalStorage` inside a webhook handler in multi-tenant mode | The Python adapter is hand-rolled over raw `aiohttp` (`_github_api_request`) with PyJWT for App JWTs and an installation-token cache; the `github` extra is `pyjwt[crypto]` only — there is no Octokit-equivalent object to return, and exposing the raw session or an invented facade under the name `octokit` would misrepresent the surface. Revisit if the adapter adopts an octokit-style SDK (e.g. `githubkit`) as an optional dependency per hazard #10's "prefer official SDKs" sub-rule; the getter (and the GitHub `fetch_subject` half of #459) ports cleanly then. |
| `LinearAdapter.linear_client` native client getter (vercel/chat#459, #478) | Not exposed | `get linearClient(): LinearClient` (plus deprecated `client` alias) returns the `@linear/sdk` `LinearClient`, per-org from `AsyncLocalStorage` in multi-tenant OAuth mode | `@linear/sdk` is TypeScript-only and no official Linear Python SDK exists; the adapter issues GraphQL directly over `aiohttp` (`_graphql_query`) and already documents that stance. Nothing honest to put behind the name. Revisit only if Linear ships an official Python SDK (the Linear `fetch_subject` half of #459 is blocked on the same). |
| `@chat-adapter/tests` adapter test kit (vercel/chat#470) | Not ported | New TS package with test utilities for adapter authors | Python already ships `chat_sdk.testing` (`MockAdapter`, `MockStateAdapter`, `create_test_message()`) covering the same surface for this repo's adapter tests; mirroring the TS kit verbatim would duplicate it. Revisit if upstream's kit grows capabilities ours lacks (e.g. recorded replay fixtures for third-party adapter authors). |
| Teams modal-submit webhook options (vercel/chat#454 adapter-teams slice) | Not ported — the Python Teams adapter has no task-module/modal-submit flow (`handleTaskSubmit`/`processModalSubmit` are absent), so upstream's change passing `bridgeAdapter.getWebhookOptions(activity.id)` into `processModalSubmit` has no landing site | `TeamsAdapter.handleTaskSubmit` forwards webhook options so modal callbackUrl POSTs are registered with `waitUntil` | Pre-existing gap: Teams modals are unported. The Slack adapter already forwards options to `process_modal_submit`, so the new waitUntil plumbing is exercised there. Add the Teams call when Teams modal support lands. |
| jsx-runtime `callbackUrl` props (vercel/chat#454 slice) | Not ported | `ButtonProps`/`ModalProps` gain `callbackUrl`; `resolveJSXElement` forwards it | Covered by the existing "JSX Card/Modal elements" row — Python has no JSX runtime; `Button()`/`Modal()` builders accept `callback_url` directly. |
| Transcripts API Python adaptations (vercel/chat#448) | `transcripts.delete()` returns a `DeleteResult` dataclass; misconfiguration raises `ValueError` (constructor/`AppendInput` guards, invalid duration) or `ChatError` (`chat.transcripts` accessor); guard messages name the Python kwarg (`options.user_key`); `DurationString` is a `str` alias validated at runtime by `_parse_duration` | Inline `{ deleted: number }`; generic `Error` for all of the above; template-literal `` `${number}${"s"\|"m"\|"h"\|"d"}` `` type | Port rules: typed dataclasses over raw dicts; repo error-type conventions (constructor misconfig → `ValueError`, runtime API misuse → `ChatError`) with upstream-matching message wording; Python has no template-literal types. Same shapes and values throughout. |
| Slack legacy mrkdwn renderer (response_url surface only, post-#440) | `_node_to_mrkdwn` renders headings as `*bold*` and images as `{alt} ({url})` / bare URL | TS `nodeToMrkdwn` has no heading/image branches — both fall through to `defaultNodeToText`, dropping heading emphasis and image URLs | Pre-existing Python improvement; after vercel/chat#440 it affects only `to_response_url_text` (ephemeral edits via response_url). Preserves visual hierarchy and image URLs Slack would otherwise lose. |
| Slack `api` primitives `send_slack_response_url` URL gate (vercel/chat#548) | `send_slack_response_url` (`slack/api/__init__.py`) calls `_assert_slack_response_url(url)` before POSTing — requires an `https://*.slack.com` URL (where Slack-issued `response_url`s always live) and raises `ValueError` for anything else | Upstream `api/client.ts` `sendResponseUrl` POSTs to whatever `response_url` it is handed, with no scheme/host validation | SSRF guard. The `response_url` reaching this primitive can originate from a parsed-but-unverified interaction payload; without a gate a crafted value could redirect the POST (which carries no bearer token but does echo SDK-controlled message content and trigger an arbitrary outbound request) to an attacker host. Enforces `CLAUDE.md`'s "Validate external URLs before requests (SSRF)" rule, mirroring the high-level adapter's `rehydrate_attachment` allowlist row above. Allowlist: scheme `https`, host `slack.com` or `*.slack.com`. |
| Slack `api` primitives `fetch_slack_file` host allowlist (vercel/chat#548) | `fetch_slack_file` (`slack/api/__init__.py`) gates `url` through `is_trusted_slack_file_url` before forwarding the bot token, raising `ValueError` for untrusted hosts | Upstream `api/client.ts` `fetchFile` GETs the supplied URL with `Authorization: Bearer <token>` unconditionally | Token-leak guard. `fetch_slack_file` attaches the workspace bot token; a crafted `url_private` from a parsed file object could otherwise exfiltrate that token to an arbitrary host. `is_trusted_slack_file_url` requires scheme `https` and host in `{files.slack.com, slack.com, *.slack.com, *.slack-edge.com}` — the same allowlist the high-level adapter's `rehydrate_attachment` row uses for Slack. Enforces `CLAUDE.md`'s SSRF/URL-validation rule. |

### Platform-specific gaps

| Area | Python | TS | Rationale |
|------|--------|-----|-----------|
| Teams certificate auth | Rejected (app password only) | Supported | Low demand; can add later |
| Teams `dialog_open_timeout_ms` config | Not implemented | Configurable | Low demand |
| Google Chat outbound file delivery | Files attached to an outbound message are dropped — `post_message` logs "File uploads are not yet supported for Google Chat" and posts text/cards only (no media upload). Inbound attachments **are** parsed: `_create_attachment` builds a full `Attachment` (type detection, `fetch_data` download closure, `fetch_metadata`) for every `message.attachment` entry. | Both directions supported (multipart media upload on send) | Outbound upload (multipart to the Media API) not yet ported; API complexity, can add later. Inbound parity is complete, so this row is outbound-only. |
| Discord Gateway WebSocket | HTTP interactions only | Both HTTP and Gateway | Gateway requires persistent connection |
| Discord gateway-only interactions (vercel/chat#490) | Handled on the forwarded-event surface: a `GATEWAY_INTERACTION_CREATE` envelope (raw INTERACTION_CREATE dispatch payload in `data`) is deferred via `POST /interactions/{id}/{token}/callback` (type 5 slash / type 6 component) and routed through the existing HTTP interaction handlers; a malformed forward missing `id`/`token` is logged and skipped | Upstream handles `Events.InteractionCreate` directly on the resident discord.js client via `deferReply()`/`deferUpdate()`; the upstream forwarder never forwards interactions | Python has no resident Gateway client (row above), so gateway-only deployments run an external listener shim that forwards raw dispatch payloads (`x-discord-gateway-token`). Observable wire behavior is identical — same callback REST calls, same handler routing, same `@original` deferred-response resolution. |
| Teams `User-Agent: Vercel.ChatSDK` outbound header | Not set on `aiohttp` calls | Propagated by `botbuilder` 2.0.8 | Python Teams adapter doesn't use `botbuilder` (raw `aiohttp`). Upstream's vercel/chat#415 was a JS-only `botbuilder` SDK bump that flipped `X-User-Agent` → `User-Agent`. No equivalent dependency to bump on the Python side. Setting a `User-Agent` on the ~9 outbound `aiohttp` call sites would be a defense-in-depth nice-to-have; deferred to a follow-up. |
| Teams adapter on `microsoft-teams-apps` (official MS Python SDK) | Inbound webhook + JWT auth, outbound send/edit/delete/typing, and native DM streaming all delegate to the official `microsoft-teams-apps` SDK `App`; Graph reads stay hand-rolled over `aiohttp` | `@microsoft/teams.apps` owns the wire format, throttling, and activity routing | **Delivered in 0.4.30** (issue #93, PRs 1–4). The migration shipped as four PRs: inbound + auth (#143), outbound (#144), native streaming via the SDK `IStreamer` (#145), and this release cut. The 3.12 floor bump (#111) — the migration's prerequisite — landed in 0.4.29. The residual adapter-level divergences (we keep the SDK as auth + transport but route the authenticated activity ourselves; close the streamer in our own `finally` because our bridge owns dispatch) are documented in the Teams divergence rows above. Graph stays hand-rolled (no `msgraph-sdk` / `[graph]` extra). |
| Telegram `get_user().is_bot` | Always `False` (matches upstream — `getChat` does not expose `is_bot`) | Always `false` (same caveat documented in upstream code comment) | The Telegram Bot API's `getChat` endpoint does not surface the `is_bot` field that's available on the `User` object inside incoming `Message` updates. Callers needing bot detection must use `message.author.is_bot` from webhooks instead of `chat.get_user(...).is_bot`. |
| WhatsApp `get_user` | Raises `ChatNotImplementedError` (`Chat.get_user` translates to "does not support get_user") | Not implemented upstream either (no `getUser` on the WhatsApp adapter) | WhatsApp Cloud API has no user lookup endpoint — phone numbers are the only stable identifier and there's no equivalent of `users.info` exposed to business apps. Documented explicitly so callers don't expect parity with Slack/Teams/Discord. |
| Linear agent sessions | Not ported — the ~120-reference agent-sessions surface (`parseMessageFromAgentSessionEvent`, `agentActivity`, `AgentSessionEventWebhookPayload`, `LinearAgentSessionThreadId`, …) is absent from the Python `LinearAdapter` | Full agent-sessions support (`adapter-linear` 4.27.0, `bc94f0a`): parses agent-session webhook events into messages, emits agent activity, and routes the agent-session thread id | Largest single gap from the 0.4.30 audit; pre-existing (present since 0.4.29). Deferred to the 4.31 wave — tracked in **#151**. |
| `adapter-web` (`@chat-adapter/web`) | Neither half ported. (a) The server-side `WebAdapter` is **deferred** — not yet ported; (b) the client subpaths are out of scope (see Rationale). | Two distinct things: (a) a server-side `WebAdapter` — an `Adapter` implementation serving a browser chat UI over the AI SDK UI stream protocol (`3490a8c`, vercel/chat#444); (b) React/Vue/Svelte client subpaths (`716e934`) | The `WebAdapter` (a) **is portable** to a Python server SDK (it's a standard `Adapter`) and is deferred, not excluded — a future wave can port it. The client subpaths (b) are genuinely browser-only (front-end framework bindings) and are out of scope for a Python server SDK. (Corrects the earlier over-broad "browser-only; no Python runtime" note in CHANGELOG.) |

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
