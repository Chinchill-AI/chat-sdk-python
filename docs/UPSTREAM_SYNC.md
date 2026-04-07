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

1. **Global singleton on Chat**: Required for Thread/Channel deserialization without passing adapter references through every call. Both SDKs use the same pattern.

2. **Thread ID format**: `{adapter}:{platform_id}` (e.g., `slack:C123:1234567890.123456`). State keys depend on this format. Changing it would break cross-language state sharing in deployments that mix TS and Python bots.

3. **Lock token format**: `{backend}_{timestamp}_{random}`. The token must be a cryptographically random string for security. The format itself is not critical for interop, but the lock key format (`dedupe:{adapter}:{message_id}`) must match.

4. **Concurrency strategy semantics**: Queue drain order, debounce loop iteration limits (20), message superseding logic, and TTL handling must match.

5. **Card element type strings**: `"card"`, `"button"`, `"text"`, `"divider"`, `"actions"`, `"fields"`, `"table"`, `"section"`, `"image"`, `"link"`, `"link-button"` must match exactly.

## Python-Specific Hardening

These exist only in the Python port and have no TS equivalent:

- `shared/errors.py`: Typed adapter error hierarchy (`AdapterRateLimitError`, `AuthenticationError`, `ValidationError`, `NetworkError`, `ResourceNotFoundError`, `PermissionError`). TS throws plain `Error` objects.
- `testing/__init__.py` + `shared/mock_adapter.py`: Test utilities with `MockAdapter`, `MockStateAdapter`, `create_test_message()`.
- `from __future__ import annotations` everywhere: Enables PEP 604 union syntax (`X | Y`) on Python 3.10.
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

## Known TS-to-Python Footguns

These are the specific translation mistakes that caused bugs during the original port (caught across 9 rounds of review). Every contributor should internalize this list.

### 1. `fn(x, {opts})` must become keyword args, not a dict

```typescript
// TS
adapter.postMessage(threadId, message, { metadata: true });
```

```python
# WRONG: passing a dict where keyword args are expected
adapter.post_message(thread_id, message, {"metadata": True})

# RIGHT: use the dataclass/keyword pattern
adapter.post_message(thread_id, message, metadata=True)
```

### 2. `or` vs `is not None` for empty string preservation

```python
# WRONG: empty string is falsy in Python, so this silently drops it
text = event.get("text") or default_text

# RIGHT: preserve empty strings when they are valid values
text = event.get("text") if event.get("text") is not None else default_text

# ALSO RIGHT: explicit None check
raw = event.get("text")
text = raw if raw is not None else default_text
```

This bit us in adapter dispatch where empty `text` fields (valid for action events with no text) were being replaced with defaults.

### 3. camelCase keys in dispatch dicts

```python
# WRONG: preserving TS naming in Python dicts
event = {"threadId": thread_id, "messageId": msg_id}

# RIGHT: Python uses snake_case throughout
event = ActionEvent(thread_id=thread_id, message_id=msg_id, ...)
```

This was a systemic bug across all adapters. The `test_dispatch_key_validation.py` test suite was written specifically to catch this. See [TESTING.md](TESTING.md) for details.

### 4. `asyncio.ensure_future` vs `asyncio.create_task`

```python
# WRONG: deprecated since Python 3.10, and does not work outside async context
asyncio.ensure_future(coro)

# RIGHT: explicit create_task with error handling
task = asyncio.get_running_loop().create_task(coro)
task.add_done_callback(lambda t: log_error(t.exception()) if t.exception() else None)
```

The SDK's `_create_task()` helper wraps this pattern and gracefully handles the case where no event loop is running.

### 5. `datetime.utcnow()` vs `datetime.now(tz=timezone.utc)`

```python
# WRONG: returns naive datetime, deprecated in Python 3.12
from datetime import datetime
now = datetime.utcnow()

# RIGHT: timezone-aware datetime
from datetime import UTC, datetime
now = datetime.now(tz=UTC)
```

All timestamps in the SDK use `UTC` from the `datetime` module (aliased from `timezone.utc` in Python 3.11+).

### 6. Raw dicts for process_* events must use typed dataclasses

```python
# WRONG: plain dict loses type safety and makes key typos silent
chat.process_action({
    "action_id": action_id,
    "thred_id": thread_id,  # typo goes undetected
})

# RIGHT: typed dataclass catches typos at construction time
chat.process_action(ActionEvent(
    adapter=self,
    action_id=action_id,
    thread_id=thread_id,  # typo would be a TypeError
    ...
))
```

### 7. `ContextVar` as instance variable, not class variable

```python
# WRONG: shared across all instances, causes cross-request contamination
class SlackAdapter:
    _request_context: ContextVar[RequestContext] = ContextVar("request_context")

# RIGHT: each instance gets its own ContextVar
class SlackAdapter:
    def __init__(self):
        self._request_context: ContextVar[RequestContext] = ContextVar("request_context")
```

### 8. `random.choices` vs `secrets.token_hex` for security tokens

```python
# WRONG: predictable PRNG, not suitable for lock tokens
import random
token = ''.join(random.choices('abcdef0123456789', k=32))

# RIGHT: cryptographically secure random
import secrets
token = secrets.token_hex(16)
```

Lock tokens must be unpredictable because they serve as proof of lock ownership. A compromised token allows unauthorized lock release.

### 9. Top-level imports of optional deps must be lazy

```python
# WRONG: crashes at import time if slack-sdk is not installed
from slack_sdk.web.async_client import AsyncWebClient  # top of file

# RIGHT: import inside the function/method that uses it
def _get_client(self):
    from slack_sdk.web.async_client import AsyncWebClient
    return AsyncWebClient(token=self._bot_token)
```

Optional dependencies (slack-sdk, pynacl, aiohttp, etc.) must only be imported inside methods of their respective adapter. Users who install `chat-sdk` without extras should not get import errors from adapters they are not using.
