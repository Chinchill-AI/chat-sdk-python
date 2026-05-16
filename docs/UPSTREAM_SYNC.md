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

Before merging an upstream-derived change, check:

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
| Fallback streaming with whitespace-only streams (non-Teams adapters) | Placeholder cleared to `" "` on final edit | Placeholder left visible (`"..."` stuck) | Upstream 4.26 guards against empty edits but leaves the placeholder stranded on the message. We issue one final `edit_message(" ")` so the placeholder disappears when no real content was produced. Teams no longer routes through `_fallback_stream` after vercel/chat#416 (DMs use native streaming, group chats accumulate-and-post), so this divergence applies only to Slack / Discord / GitHub / Telegram / Google Chat / Linear / WhatsApp. |
| Google Chat `<url\|text>` round-trip | `to_ast()` / `extract_plain_text()` parse the custom-label syntax back to a link node / bare label | `toAst()` / `extractPlainText()` leave `<url\|text>` as raw text (or parse the whole string as an autolink with a malformed URL) | Upstream 4.26 emits `<url\|text>` in `from_ast` but never taught the reverse direction to parse it. A message posted with `[label](url)` then read back through `fetch_messages` comes back as unstructured text (or worse, a link node with the full `url\|text` as its URL) in upstream. We close the round-trip via an AST placeholder substitution: each `<url\|text>` is extracted to a private-use sentinel, Markdown is parsed on the rest, and link nodes are injected where the sentinels landed. This avoids the Markdown parser's incomplete handling of balanced-parens link destinations, so URLs like `https://en.wikipedia.org/wiki/Foo_(bar)` round-trip intact. |
| `from_json(data, adapter=X)` → `_adapter_name` | Updated to `X.name` so `to_json()` reflects the bound adapter | Kept at `json.adapterName`, so re-serialization can emit a name that no longer matches the actual adapter | Upstream TS has the same gap but only exposes it via the `fromJSON(json, adapter?)` overload. In Python we lean on this API more (explicit `chat=` / explicit `adapter=` is preferred over the singleton). We sync the name on rebind so runtime and serialize agree. |
| Google Chat link labels with `\|` / `>` / `]` / newline, empty labels, URLs without a scheme, or URLs containing `\|` / `>` | Fall back to `text (url)` (or bare URL for empty labels) when the `<url\|text>` form can't round-trip safely | Always `<url\|text>`, producing malformed or un-parseable output | Google Chat's `<url\|text>` has no escape for `\|` or `>`; `]` breaks our own `to_ast()` regex (which converts `<url\|text>` to Markdown `[text](url)`, and Markdown closes the label at the first `]`); newline breaks the single-line form; schemeless URLs and URLs containing `\|`/`>` don't match our reverse parser. Upstream emits the malformed form regardless; we fall back to the pre-4.26 `text (url)` form (or the bare URL for empty labels) so the label/URL stays intact and Google Chat's auto-link detection still fires for http(s). |
| Google Chat heading rendering | `#`-headings emit as `*text*` (bold) so they're visually distinct | Falls through to default node-to-text (plain concatenation) | Google Chat has no heading syntax; emitting plain text loses the visual hierarchy. Bold is the closest approximation the platform supports. |
| Google Chat image rendering | Images emit as `{alt} ({url})` or bare `url` | No image branch — falls through to default which concatenates children only, dropping the URL | Upstream silently drops image URLs when rendering to Google Chat text. We preserve the URL so the message content isn't lost. |
| Fallback streaming stream-exception capture (non-Teams adapters) | `_fallback_stream` captures exceptions from the stream iterator, flushes whatever content was already rendered, awaits `pending_edit`, and re-raises after cleanup | `try/finally` only — exception propagates immediately, `pendingEdit` is un-awaited, and the placeholder is stranded as `"..."` | Upstream leaves a hard UX failure when streams crash mid-flight (common: LLM connection drops): placeholder visible forever, orphan background task. We flush + clean up before re-raising so the caller still sees the original error and users see the partial content instead of a spinner. As above, this divergence no longer applies to Teams after vercel/chat#416: `_stream_via_emit` cancels the session on iterator exception so `_close_stream_session` skips the final-message activity, and the original exception still propagates to the caller. |
| Fallback streaming final SentMessage content (non-Teams adapters) | SentMessage + final edit carry `final_content` (remend'd — inline markers auto-closed) | SentMessage + final edit carry raw `accumulated` | Narrow UX refinement. If a stream ends with an unclosed `*`/`~~`/etc., upstream ships the unclosed marker; we run `_remend` so the user sees a clean final message. Not observable in the common case where streams close their own markers. Teams native streaming and the Teams accumulate-and-post path both ship raw `accumulated`, matching upstream after #416; this divergence applies only to the remaining adapters that still route through `_fallback_stream`. |
| Teams group-chat / channel streaming via accumulate-and-post | `TeamsAdapter.stream` accumulates the full text and issues a single `post_message` instead of post+edit, even for group chats and channel threads | Same after vercel/chat#416 (`if (activeStream && !activeStream.canceled) … else { accumulate; postMessage }`) — no divergence at the adapter level | Documented for clarity: the Python port matches upstream's post-#416 behavior of avoiding the post+edit flicker where Teams doesn't support native streaming. The adapter no longer touches `_teams_update` from the streaming path. |
| Teams native streaming hand-rolled wire format (DMs) — **transitional** | `_stream_via_emit` and `_close_stream_session` build Bot Framework streaming payloads (`channelData`/`entities`/`streamSequence`/`streamId`) by hand and post them via `_teams_send`, including a per-emit throttle gate (`native_stream_min_emit_interval_ms`, default 1500ms) honoring caller-supplied `StreamOptions.update_interval_ms` | `IStreamer.emit(text)` from `@microsoft/teams.apps` (npm) handles the wire format and throttling under the hood. The equivalent Python SDK (`microsoft-teams-apps`) only went GA on 2026-05-01 — too late for the 4.27 sync window. | Hand-rolled streaming primitives are tracked for migration to `microsoft-teams-apps` (Python) in a follow-up release (0.4.28 / future Python-only). Once we migrate, this row, the divider-rendering row, and the `_active_streams` accounting all simplify or disappear. Until then we own the wire format ourselves — including the 1500ms emit throttle that protects against Teams' ~1 req/sec streaming-endpoint quota — and surface send failures by re-raising (so `Thread.stream`'s outer accumulator can't record text Teams rejected). |
| Teams native streaming `streamId` placement on `streaminfo` entity (DMs) | `streamId` is added to BOTH `channelData` and the `streaminfo` entity on subsequent and final activities (it is omitted from both on the first chunk because the server has not yet assigned an id) | `IStreamer.emit` handles the wire shape; the upstream JS SDK includes `streamId` on both sites as part of its `streaminfo` entity contract per [MS Bot Framework streaming docs](https://learn.microsoft.com/microsoftteams/platform/bots/streaming-ux#continue-streaming) | Bot Framework REST contract requires the entity-level `streamId` for continuation activities — earlier Python versions set it only on `channelData`, which Teams may treat as a malformed continuation and detach from the original stream. This row documents the contract, not a divergence; both Python and the JS SDK comply. |
| Teams native streaming final-send when first chunk's `id` was empty (DMs) | `_close_stream_session` sends the final `message` activity whenever `text` is non-empty, even if `stream_id` is `None` (Bot Framework REST response returned `{"id": ""}` on the first chunk). The final activity omits `streamId` from `channelData` rather than serializing `None`. | Upstream's `streamViaEmit` awaits the `chunk` event for the first activity's `id`; if Teams returns an empty id, `messageId` becomes `""` and the SDK's auto-close emits the final activity through `IStreamer` regardless | Without the final `message` activity, the Teams client's streaming UI keeps spinning until the platform times the session out — a stuck-loading-state UX failure with no user workaround. We mirror upstream's looser check (text non-empty → ship the final) so the streaming indicator clears even when the Bot Framework REST response surface returns an empty `id`. |
| `RawMessage.text` override field — **transitional** | New optional `text: str \| None = None` field on `RawMessage` (`src/chat_sdk/types.py`). When set by an adapter, `Thread._handle_stream` MUST prefer it over its own local accumulator when constructing the recorded `SentMessage` body / message-history entry. `None` falls back to the local accumulator (backward-compatible default for adapters that don't need the override). Set by `_stream_via_emit` so the recorded message matches what Teams actually shipped, even when chunks were buffered into the throttle window and cancellation skipped the flush emit. | Upstream's `RawMessage` (`packages/chat/src/types.ts`) is `{ id; raw; threadId }` only. Cancellation-text reconciliation lives inside `@microsoft/teams.apps`'s `IStreamer.emit` (the npm SDK owns the buffer and never surfaces a buffered-but-unsent suffix to `chat`). | Direct consequence of the hand-rolled Teams native streaming row above. Without the override, the SDK's local accumulator (which captures every chunk yielded to the adapter, including chunks coalesced into the throttle window) would diverge from what Teams actually accepted whenever a session is canceled with buffered text pending — recording text the user never saw. Disappears alongside the hand-rolled wire format once we migrate to `microsoft-teams-apps` (Python). Regression coverage: `tests/test_thread_faithful.py::test_should_prefer_raw_message_text_override_over_local_accumulator` (would fail if someone "fixes" Thread.stream back to upstream's local-accumulator-only behavior) and `tests/test_teams_native_streaming.py::test_canceled_stream_sets_raw_message_text_override`. |
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
