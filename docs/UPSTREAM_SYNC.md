# Upstream Sync Guide

How to keep `chat-sdk-python` in sync with the [Vercel Chat TS SDK](https://github.com/vercel/chat).

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
