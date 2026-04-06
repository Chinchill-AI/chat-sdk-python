# chat-sdk-python

Multi-platform async chat SDK for Python. Port of [Vercel Chat](https://github.com/vercel/chat).

> **Status: Alpha (0.0.1a3)** — API may change. Not yet tested in production.

## Why chat-sdk?

- **Write once, deploy to 8 platforms.** One handler runs on Slack, Discord, Teams, Telegram, WhatsApp, Google Chat, GitHub, and Linear.
- **Built-in concurrency primitives.** Deduplication, thread locking, and message queuing are handled for you.
- **Cross-platform cards.** Author a `Card` once and it renders as Block Kit (Slack), Adaptive Cards (Teams), embeds (Discord), and more.
- **Not a replacement for platform SDKs.** chat-sdk is built *on top of* them. You can always drop down to the native SDK when you need to.

## Install

```bash
pip install chat-sdk                   # core only
pip install chat-sdk[slack]            # + Slack adapter
pip install chat-sdk[all]              # all adapters + state backends
```

## Quick Start

```python
from chat_sdk import Chat, Card, Button, Actions, MemoryStateAdapter
from chat_sdk.adapters.slack import create_slack_adapter

chat = Chat(
    adapters={"slack": create_slack_adapter()},
    state=MemoryStateAdapter(),
    user_name="my-bot",
)

@chat.on_mention
async def handle_mention(thread, message):
    await thread.post(
        Card(title="Hello!", children=[
            Actions([Button(id="hi", label="Say Hi")])
        ])
    )
```

## Adapters

| Platform | Install Extra | Status |
|----------|--------------|--------|
| Slack | `chat-sdk[slack]` | Alpha |
| Discord | `chat-sdk[discord]` | Alpha |
| Teams | `chat-sdk[teams]` | Alpha |
| Telegram | `chat-sdk[telegram]` | Alpha |
| WhatsApp | `chat-sdk[whatsapp]` | Alpha |
| Google Chat | `chat-sdk[google-chat]` | Alpha |
| GitHub | `chat-sdk[github]` | Alpha |
| Linear | `chat-sdk[linear]` | Alpha |

## State Backends

| Backend | Install Extra |
|---------|--------------|
| In-Memory | Built-in |
| Redis | `chat-sdk[redis]` |
| PostgreSQL | `chat-sdk[postgres]` |

## Compared to Alternatives

| Feature | chat-sdk | Raw platform SDKs | BotFramework SDK |
|---------|----------|--------------------|------------------|
| Multi-platform from one codebase | 8 platforms | 1 per SDK | Teams + limited |
| Async-native (Python 3.11+) | Yes | Varies | No |
| Cross-platform cards | Card model | Platform-specific | Adaptive Cards only |
| Thread locking / dedup | Built-in | DIY | DIY |
| State abstraction (mem/redis/pg) | Built-in | DIY | DIY |
| Drop down to native SDK | Yes | N/A | Partially |

## Development

```bash
uv sync --group dev
uv run pytest tests/
uv run ruff check src/
```

## License

MIT
