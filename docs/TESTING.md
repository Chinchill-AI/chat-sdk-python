# Testing

Test strategy, conventions, and instructions for `chat-sdk-python`.

## Test Categories

### Unit Tests (`tests/test_*.py`)

Test individual modules in isolation with mocked dependencies. Examples:

- `test_cards.py` -- Card builder functions and fallback text generation
- `test_markdown_parser.py` -- Markdown parsing and stringification
- `test_streaming_markdown.py` -- StreamingMarkdownRenderer buffering behavior
- `test_memory_state.py` -- MemoryStateAdapter operations
- `test_types.py` -- Message serialization/deserialization
- `test_chat.py` -- Chat orchestrator handler registration and dispatch
- `test_thread.py` -- ThreadImpl posting, state, serialization
- `test_channel.py` -- ChannelImpl operations

### Adapter Unit Tests (`tests/test_<platform>_*.py`)

Test platform-specific adapter logic:

- `test_slack_adapter.py`, `test_slack_webhook.py`, `test_slack_cards.py`, etc.
- `test_discord_adapter.py`, `test_discord_cards.py`, `test_discord_format.py`, etc.
- `test_teams_adapter.py`, `test_teams_cards.py`, `test_teams_format.py`, etc.
- `test_telegram_adapter.py`, `test_telegram_webhook.py`, `test_telegram_cards.py`, etc.
- `test_whatsapp_adapter.py`, `test_whatsapp_webhook.py`, `test_whatsapp_cards.py`, etc.
- `test_github_adapter.py`, `test_github_webhook.py`, `test_github_cards.py`, etc.
- `test_google_chat_adapter.py`, `test_gchat_webhook.py`, `test_gchat_cards.py`, etc.
- `test_linear_adapter.py`, `test_linear_cards.py`, `test_linear_format.py`, etc.

### Integration Tests (`tests/integration/`)

Test multi-component flows using the mock adapter and memory state backend:

- `test_mention_flow.py` -- Full mention -> handler -> post response flow
- `test_dm_flow.py` -- Direct message routing
- `test_action_flow.py` -- Button click -> action handler
- `test_reaction_flow.py` -- Reaction event handling
- `test_slash_command_flow.py` -- Slash command routing
- `test_concurrency.py` -- Drop/queue/debounce strategies
- `test_dedup.py` -- Message deduplication
- `test_channel_ops.py` -- Channel-level operations
- `test_subscription_flow.py` -- Thread subscription and subscribed message routing
- `test_assistant_threads.py` -- Slack Assistant thread events

### Replay Tests (`tests/integration/test_replay_*.py`)

Replay recorded webhook payloads through the full adapter pipeline to verify end-to-end correctness. These tests simulate the exact JSON payloads that platforms send.

- `test_replay_mention.py` -- Slack @mention webhook replay
- `test_replay_dm.py` -- DM webhook replay
- `test_replay_actions_reactions.py` -- Action and reaction webhooks
- `test_replay_modal.py`, `test_replay_modals_extended.py` -- Modal submit/close
- `test_replay_slash_command.py` -- Slash command webhooks
- `test_replay_streaming.py` -- Streaming response flow
- `test_replay_events.py` -- Miscellaneous event types
- `test_replay_platforms.py` -- Cross-platform webhook replay
- `test_replay_multi_workspace.py` -- Multi-workspace Slack OAuth flow
- `test_replay_fetch_messages.py` -- Message history fetch

### Dispatch Key Validation (`tests/test_dispatch_key_validation.py`)

A dedicated test suite that verifies every adapter dispatches events with snake_case keys. See the detailed section below.

## How to Run Tests

```bash
# All tests
uv run pytest tests/

# Stop on first failure
uv run pytest tests/ -x

# With coverage
uv run pytest tests/ --cov=src/chat_sdk --cov-report=term-missing

# Specific test file
uv run pytest tests/test_cards.py -v

# Specific test class or method
uv run pytest tests/test_dispatch_key_validation.py::TestSlackDispatchKeys -v

# Integration tests only
uv run pytest tests/integration/ -v

# Tests matching a keyword
uv run pytest tests/ -k "streaming" -v
```

### Configuration

`pyproject.toml` configures pytest:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
```

The `asyncio_mode = "auto"` setting means all `async def test_*` functions are automatically treated as async tests without needing `@pytest.mark.asyncio`.

## How to Add Tests for a New Adapter

1. **Create test files** following the naming convention:
   ```
   tests/test_<platform>_adapter.py    # Core adapter tests
   tests/test_<platform>_webhook.py    # Webhook verification tests
   tests/test_<platform>_cards.py      # Card rendering tests
   tests/test_<platform>_format.py     # Format converter tests
   ```

2. **Use the shared fixtures** from `tests/conftest.py`:
   ```python
   from tests.conftest import FakeRequest, make_request, compute_hmac_sha256
   ```

3. **Add dispatch key validation** to `tests/test_dispatch_key_validation.py`:
   ```python
   class TestNewPlatformDispatchKeys:
       def _make_adapter(self):
           from chat_sdk.adapters.new_platform.adapter import NewPlatformAdapter
           adapter = NewPlatformAdapter(config)
           adapter._chat = _make_mock_chat()
           return adapter

       def test_message_dispatch_keys(self):
           adapter = self._make_adapter()
           # Simulate event, verify process_message called with correct args
           adapter._handle_message_event(fake_event)
           adapter._chat.process_message.assert_called_once()
           # ...

       def test_action_dispatch_keys(self):
           adapter = self._make_adapter()
           adapter._handle_action(fake_action)
           action_obj = adapter._chat.process_action.call_args[0][0]
           assert_no_camel_case_keys(action_obj)
   ```

4. **Add integration replay tests** under `tests/integration/`:
   ```python
   # tests/integration/test_replay_new_platform.py
   async def test_new_platform_mention_replay():
       chat = Chat(ChatConfig(
           user_name="bot",
           adapters={"new_platform": adapter},
           state=memory_state,
       ))
       # Submit recorded webhook JSON
       # Assert handler was called with correct arguments
   ```

5. **Test webhook signature verification** specifically:
   ```python
   async def test_rejects_invalid_signature():
       result = await adapter.handle_webhook(FakeRequest(
           headers={"X-Signature": "invalid"},
           body=b'{"event": "test"}',
       ))
       assert result.status == 401
   ```

## Known Coverage Gaps

The following modules are under 60% coverage as of the initial alpha release. These are tracked for improvement:

| Module | Approximate Coverage | Gap Description |
|--------|---------------------|-----------------|
| `adapters/whatsapp/adapter.py` | ~55% | Media download, message status webhooks, group message handling |
| `adapters/google_chat/adapter.py` | ~50% | Workspace events, user info resolution, space membership |
| `state/postgres.py` | ~45% | Connection pooling edge cases, schema migration, concurrent lock contention |
| `state/redis.py` | ~50% | Lua script error handling, connection retry, cluster mode |

These gaps exist primarily because:
- WhatsApp and Google Chat have complex webhook payload variations that need more recorded fixtures.
- Redis and PostgreSQL state adapters require running databases for full integration testing. The unit tests mock the database clients, which limits coverage of error paths.

## The Dispatch Key Validation Pattern

### What It Is

`test_dispatch_key_validation.py` contains a test class for each adapter that verifies the adapter dispatches events to `Chat.process_*` methods using snake_case keys (Python convention) rather than camelCase keys (JavaScript/TypeScript convention).

### What It Catches

During the original TS-to-Python port, adapters were passing camelCase keys like `threadId`, `messageId`, `actionId`, and `privateMetadata` in the event objects dispatched to `Chat.process_message()`, `Chat.process_action()`, etc. The `Chat` class expects snake_case keys (`thread_id`, `message_id`, `action_id`, `private_metadata`). CamelCase keys silently produce `None` lookups or `KeyError` downstream.

This was a **systemic bug** across all adapters -- every adapter had at least one camelCase key in its dispatch path.

### How It Works

1. Each test creates a minimal adapter instance with a mock `ChatInstance` that records all `process_*` calls.
2. The test calls the adapter's internal dispatch method (e.g., `_handle_message_event()`, `_handle_block_actions()`) with a realistic platform payload.
3. The test extracts the event object passed to the mock's `process_*` method.
4. `assert_no_camel_case_keys(event_obj)` recursively walks the object (supporting dicts, dataclasses, and lists) and asserts no key matches the camelCase pattern `^[a-z]+[A-Z]`.
5. The `raw` key is intentionally excluded from checking because it contains the original platform payload with the platform's native casing.

### Helper Function

```python
CAMEL_RE = re.compile(r"^[a-z]+[A-Z]")

def assert_no_camel_case_keys(d, path="", *, skip_raw=True):
    """Recursively check that no dict keys use camelCase."""
    if isinstance(d, dict):
        for k, v in d.items():
            if isinstance(k, str) and CAMEL_RE.match(k):
                raise AssertionError(f"camelCase key '{k}' found at {path}.{k}")
            if skip_raw and k == "raw":
                continue
            assert_no_camel_case_keys(v, f"{path}.{k}")
    elif isinstance(d, (list, tuple)):
        for i, item in enumerate(d):
            assert_no_camel_case_keys(item, f"{path}[{i}]")
```

### Adapters Covered

- Slack: message, reaction, action, slash command, modal submit
- Google Chat: action (card click)
- Discord: action (component interaction)
- Telegram: action (callback query), reaction
- Teams: action (message with value)
- Linear: reaction, comment

### Why It Exists

This test suite is a regression guard. Without it, any refactoring of adapter dispatch code could reintroduce camelCase keys without any test failure. The test runs on every CI build and takes < 1 second.

## Webhook JSON Fixtures (Done)

28 webhook JSON fixtures were copied from the TS SDK repository into `tests/fixtures/` and 46 replay tests now pass against them. The fixtures enable:

1. **Cross-language parity testing**: The Python adapter produces the same normalized `Message` objects as the TS adapter for identical webhook payloads.
2. **Platform version regression**: When platforms change their webhook format, the fixture files make it obvious what changed.
3. **Replay tests without mocks**: The full adapter pipeline is driven with real payloads instead of hand-constructed dicts.

The fixture layout:
```
tests/fixtures/
  slack/
    mention.json
    dm.json
    action_button_click.json
    reaction_added.json
    slash_command.json
    modal_submit.json
  discord/
    message_create.json
    interaction_button.json
  teams/
    message_activity.json
    action_submit.json
  ...
```
