# chat-sdk-python

Multi-platform async chat SDK for Python. Port of [Vercel Chat](https://github.com/vercel/chat).

> **Status: Alpha (0.0.1a1)** — API may change. Not yet tested in production.

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

## Development

```bash
uv sync --group dev
uv run pytest tests/
uv run ruff check src/
```

## License

MIT
