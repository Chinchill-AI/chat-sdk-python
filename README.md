# chat-sdk-python

[![PyPI](https://img.shields.io/pypi/v/chat-sdk)](https://pypi.org/project/chat-sdk/)
[![Python](https://img.shields.io/pypi/pyversions/chat-sdk)](https://pypi.org/project/chat-sdk/)
[![Tests](https://github.com/Chinchill-AI/chat-sdk-python/actions/workflows/test.yml/badge.svg)](https://github.com/Chinchill-AI/chat-sdk-python/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Multi-platform async chat SDK for Python. Port of [Vercel Chat](https://github.com/vercel/chat) (MIT, © Vercel, Inc. — see [NOTICE](NOTICE)).

> **Status: 0.4.31.3 — synced to [Vercel Chat 4.31.0](https://github.com/vercel/chat)** (`UPSTREAM_PARITY = "4.31.0"`). See [CHANGELOG.md](CHANGELOG.md).

## Why chat-sdk?

- **Write once, deploy to 9 platforms.** One handler runs on Slack, Discord, Teams, Telegram, WhatsApp, Messenger, Google Chat, GitHub, and Linear.
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
| Messenger (Meta) | `chat-sdk[messenger]` | Alpha |
| Google Chat | `chat-sdk[google-chat]` | Alpha |
| GitHub | `chat-sdk[github]` | Alpha |
| Linear | `chat-sdk[linear]` | Alpha |

## AI / LLM Integration

Expose chat actions to an LLM agent as tools (`chat/ai` parity, vercel/chat#492):

```python
from chat_sdk.ai import create_chat_tools, to_ai_messages

tools = create_chat_tools(chat, preset="messenger", require_approval=True)
# {"postMessage": ChatTool(description=..., input_schema={...}, execute=..., needs_approval=True), ...}
```

Each `ChatTool` is SDK-agnostic: `input_schema` is a JSON-Schema dict you can
hand to any agent runtime (Anthropic tool use, OpenAI tools, pydantic-ai, ...),
`execute` is the async implementation, and `needs_approval` flags write tools
for human-in-the-loop gating. Presets: `reader`, `messenger`, `moderator`.
`to_ai_messages(thread)` converts thread history into model-ready messages.
Runnable demo: [`examples/ai_tools_example.py`](examples/ai_tools_example.py).

## State Backends

| Backend | Install Extra |
|---------|--------------|
| In-Memory | Built-in |
| Redis | `chat-sdk[redis]` |
| PostgreSQL | `chat-sdk[postgres]` |

### PostgreSQL state

```python
from chat_sdk.state import create_postgres_state

# Explicit url, an existing asyncpg pool (pool=...), or neither to use
# POSTGRES_URL / DATABASE_URL.
state = create_postgres_state(url="postgres://...", key_prefix="chat-sdk")
```

By default `connect()` creates the tables and indexes it needs
(`CREATE ... IF NOT EXISTS`). All rows are namespaced by `key_prefix`.

#### Migration-owned schema

Pass `auto_create_schema=False` (to `create_postgres_state` or
`PostgresStateAdapter`, with a `url`, an env var or a `pool`) when your
migrations own the schema and the runtime role has no DDL rights:

```python
state = create_postgres_state(auto_create_schema=False)  # url from POSTGRES_URL / DATABASE_URL
```

Table and index names are unqualified and resolve through the connection's
`search_path`. To keep them in a dedicated schema, set the runtime role's
default (`ALTER ROLE chat_runtime SET search_path TO chat_state;`), which
survives transaction-mode poolers, or pass
`server_settings={"search_path": "chat_state"}` to `asyncpg.create_pool` on a
direct connection.

Run the migration as the schema owner, with the same `search_path` as the
runtime connection. The statements are exported, in execution order, as
`chat_sdk.state.POSTGRES_SCHEMA_STATEMENTS`:

```python
from chat_sdk.state import POSTGRES_SCHEMA_STATEMENTS

for statement in POSTGRES_SCHEMA_STATEMENTS:
    await conn.execute(statement)
```

The same migration in SQL (the complete adapter schema; `bigserial` also
creates the list and queue sequences):

```sql
CREATE TABLE IF NOT EXISTS chat_state_subscriptions (
  key_prefix text NOT NULL,
  thread_id text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (key_prefix, thread_id)
);

CREATE TABLE IF NOT EXISTS chat_state_locks (
  key_prefix text NOT NULL,
  thread_id text NOT NULL,
  token text NOT NULL,
  expires_at timestamptz NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (key_prefix, thread_id)
);

CREATE TABLE IF NOT EXISTS chat_state_cache (
  key_prefix text NOT NULL,
  cache_key text NOT NULL,
  value text NOT NULL,
  expires_at timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (key_prefix, cache_key)
);

CREATE INDEX IF NOT EXISTS chat_state_locks_expires_idx
  ON chat_state_locks (expires_at);

CREATE INDEX IF NOT EXISTS chat_state_cache_expires_idx
  ON chat_state_cache (expires_at);

CREATE TABLE IF NOT EXISTS chat_state_lists (
  key_prefix text NOT NULL,
  list_key text NOT NULL,
  seq bigserial NOT NULL,
  value text NOT NULL,
  expires_at timestamptz,
  PRIMARY KEY (key_prefix, list_key, seq)
);

CREATE INDEX IF NOT EXISTS chat_state_lists_expires_idx
  ON chat_state_lists (expires_at);

CREATE TABLE IF NOT EXISTS chat_state_queues (
  key_prefix text NOT NULL,
  thread_id text NOT NULL,
  seq bigserial NOT NULL,
  value text NOT NULL,
  expires_at timestamptz NOT NULL,
  PRIMARY KEY (key_prefix, thread_id, seq)
);

CREATE INDEX IF NOT EXISTS chat_state_queues_expires_idx
  ON chat_state_queues (expires_at);
```

Grant the runtime role access to the schema, the five tables and both
sequences, for example:

```sql
GRANT USAGE ON SCHEMA chat_state TO chat_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA chat_state TO chat_runtime;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA chat_state TO chat_runtime;
```

The role also needs database `CONNECT`; it needs neither schema `CREATE` nor
table ownership. These grants cover existing objects only.

With `auto_create_schema=False`, `connect()` runs `SELECT 1` and then one
read-only query, and never issues DDL. The query checks that all five tables
exist and that the current role holds what the adapter uses: `SELECT`,
`INSERT` and `DELETE` on every table, `UPDATE` on the locks, cache and lists
tables, and `nextval` on the list and queue sequences (`USAGE` or `UPDATE`;
identity `seq` columns need no sequence grant). Column-level `SELECT` /
`INSERT` / `UPDATE` grants on the columns the adapter uses are accepted too.
If anything is missing, `connect()` raises `chat_sdk.StateSchemaError`
("PostgreSQL state schema is not ready: ...") naming every missing table or
grant, so a wrong `search_path` or a forgotten grant fails at startup instead
of on the first message. Your migrations also own future adapter schema
changes; the CHANGELOG lists them. A pool you pass in stays open after
`disconnect()`; a pool the adapter created from a URL is closed.

## Compared to Alternatives

| Feature | chat-sdk | Raw platform SDKs | BotFramework SDK |
|---------|----------|--------------------|------------------|
| Multi-platform from one codebase | 9 platforms | 1 per SDK | Teams + limited |
| Async-native (Python 3.12+) | Yes | Varies | No |
| Cross-platform cards | Card model | Platform-specific | Adaptive Cards only |
| Thread locking / dedup | Built-in | DIY | DIY |
| State abstraction (mem/redis/pg) | Built-in | DIY | DIY |
| Drop down to native SDK | Yes | N/A | Partially |

## Documentation

| Document | Description |
|----------|-------------|
| [Architecture](docs/ARCHITECTURE.md) | Module dependency graph, adapter protocol, card system, concurrency strategies, state backends, markdown pipeline, streaming pipeline |
| [Upstream Sync](docs/UPSTREAM_SYNC.md) | How to keep the Python port in sync with the Vercel Chat TS SDK, translation patterns, known footguns |
| [Security](docs/SECURITY.md) | Webhook verification per platform, SSRF protections, crypto details, known limitations, production audit checklist |
| [Testing](docs/TESTING.md) | Test categories, how to run tests, how to add adapter tests, coverage gaps, dispatch key validation |
| [Design Decisions](docs/DECISIONS.md) | Rationale for key architectural choices (hand-rolled parser, PascalCase builders, global singleton, zero deps) |
| [Contributing](CONTRIBUTING.md) | Dev setup, code quality, PR expectations |
| [Changelog](CHANGELOG.md) | Release history |

## Development

```bash
uv sync --group dev
uv run pytest tests/
uv run ruff check src/
```

## License

MIT
