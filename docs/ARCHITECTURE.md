# Architecture

Internal architecture guide for maintainers and contributors of `chat-sdk-python`.

## Module Dependency Graph

```
Chat (orchestrator)
 |
 +-- ThreadImpl  (message posting, streaming, state, subscriptions)
 |    |
 |    +-- ChannelImpl  (channel-level posting, thread enumeration, metadata)
 |         |
 |         +-- Adapter  (platform protocol -- Slack, Discord, Teams, etc.)
 |              |
 |              +-- BaseAdapter  (default implementations for optional methods)
 |              +-- FormatConverter  (markdown <-> platform format)
 |              +-- Cards renderer  (CardElement -> platform-specific payload)
 |
 +-- StateAdapter  (subscriptions, locking, cache, queues)
 |    |
 |    +-- MemoryStateAdapter  (dev/testing)
 |    +-- RedisStateAdapter   (production, Lua scripts for atomicity)
 |    +-- PostgresStateAdapter (production, row-level locking)
 |
 +-- Types  (Adapter protocol, Message, Author, events, config dataclasses)
 +-- Errors (ChatError, LockError, ChatNotImplementedError, RateLimitError)
```

### Import Rules

- `types.py` imports only from `cards.py`, `errors.py`, and `logger.py`.
- `thread.py` imports from `types.py` and `errors.py`. It defines the Chat singleton access point (`set_chat_singleton`, `get_chat_singleton`) to avoid circular imports with `chat.py`.
- `channel.py` imports from `thread.py` (for singleton access and helpers) and `types.py`.
- `chat.py` imports from `thread.py`, `channel.py`, and `types.py`. It is the only module that creates `ThreadImpl` and `ChannelImpl` instances in production.
- Adapters import from `types.py`, `shared/`, `cards.py`, and their own sub-packages. They never import `chat.py` directly; they receive a `ChatInstance` reference during `initialize()`.

### Circular Import Avoidance

The `Thread -> Chat` dependency is broken by the singleton pattern in `thread.py`. The `Chat` class calls `set_chat_singleton(self)` during registration, and `ThreadImpl`/`ChannelImpl` call `get_chat_singleton()` for lazy adapter resolution during deserialization. This mirrors the `chat-singleton.ts` pattern from the TS SDK.

## How Adapters Work

### The Adapter Protocol

Defined in `types.py` as a `Protocol` class with `@runtime_checkable`:

```python
@runtime_checkable
class Adapter(Protocol):
    @property
    def name(self) -> str: ...
    @property
    def user_name(self) -> str: ...
    @property
    def bot_user_id(self) -> str | None: ...

    async def post_message(self, thread_id: str, message: AdapterPostableMessage) -> RawMessage: ...
    async def edit_message(self, thread_id: str, message_id: str, message: AdapterPostableMessage) -> RawMessage: ...
    async def delete_message(self, thread_id: str, message_id: str) -> None: ...
    async def fetch_messages(self, thread_id: str, options: FetchOptions | None = None) -> FetchResult: ...
    async def handle_webhook(self, request: Any, options: WebhookOptions | None = None) -> Any: ...
    async def initialize(self, chat: ChatInstance) -> None: ...
    # ... plus ~10 more required methods
```

Required methods cover the complete lifecycle: webhook handling, message CRUD, reactions, typing indicators, thread ID encoding/decoding, and format rendering.

### BaseAdapter

`BaseAdapter` in `types.py` provides default implementations for **optional** methods that raise `ChatNotImplementedError`:

- `stream()` -- native streaming (only Slack implements this currently)
- `open_dm()` -- DM channel creation
- `post_ephemeral()` -- ephemeral messages
- `schedule_message()` -- future delivery
- `open_modal()` -- modal dialogs
- `fetch_channel_info()` -- channel metadata
- `list_threads()` -- thread enumeration

Concrete adapters inherit from `BaseAdapter` and override what they support.

### Format Converters

Each adapter has a `FormatConverter` that extends `BaseFormatConverter`:

```
Markdown string
     |
     v  parse_markdown()
   mdast AST (dict)       <-- canonical internal representation
     |
     v  from_ast()
Platform format string    (mrkdwn, HTML, Adaptive Card text, etc.)
```

The `BaseFormatConverter` provides:
- `from_markdown(md) -> str` -- parse then render
- `to_markdown(platform_text) -> str` -- parse then stringify
- `render_postable(message)` -- handles the full `AdapterPostableMessage` union (str, PostableRaw, PostableMarkdown, PostableAst, PostableCard, CardElement)
- Template helpers: `_render_list()`, `_default_node_to_text()`

Each adapter subclass implements `from_ast(ast)` and `to_ast(platform_text)` for its platform's native format:

| Adapter | Format | Converter |
|---------|--------|-----------|
| Slack | mrkdwn (Slack markdown) | `SlackFormatConverter` |
| Discord | Discord markdown | `DiscordFormatConverter` |
| Teams | HTML subset | `TeamsFormatConverter` |
| Telegram | HTML (MarkdownV2 considered too fragile) | `TelegramFormatConverter` |
| WhatsApp | WhatsApp formatting (*bold*, _italic_) | `WhatsAppFormatConverter` |
| Google Chat | Google Chat markup | `GoogleChatFormatConverter` |
| GitHub | Standard GFM | `GitHubFormatConverter` |
| Linear | Standard markdown | `LinearFormatConverter` |

### Webhook Flow

```
HTTP POST from platform
     |
     v
chat.webhooks["slack"](request)
     |
     v
Chat._handle_webhook(adapter_name, request, options)
     |
     v
adapter.handle_webhook(request, options)
     |  (adapter verifies signature, parses event, normalizes to typed event)
     v
chat.process_message(adapter, thread_id, message)
  or chat.process_action(event)
  or chat.process_reaction(event)
  or chat.process_slash_command(event)
  or chat.process_modal_submit(event)
     |
     v
asyncio.create_task(handler coroutine)
```

## How the Card System Works

Cards provide cross-platform rich messaging. The card model is defined as TypedDicts in `cards.py`:

```
CardElement (root)
  +-- title, subtitle, image_url
  +-- children: list[CardChild]
       |
       +-- TextElement        -> Slack: section block, Teams: TextBlock
       +-- ImageElement       -> Slack: image block, Teams: Image
       +-- DividerElement     -> Slack: divider block, Teams: ---
       +-- ActionsElement     -> Slack: actions block, Teams: ActionSet
       |    +-- ButtonElement  -> Slack: button, Teams: Action.Submit
       |    +-- LinkButtonElement -> Slack: button with url, Teams: Action.OpenUrl
       +-- FieldsElement      -> Slack: section with fields, Teams: FactSet
       +-- TableElement       -> Slack: ASCII table in code block, Teams: Table
       +-- SectionElement     -> Groups children
       +-- LinkElement        -> Inline hyperlink
```

### PascalCase Builders

Builder functions use PascalCase (`Card()`, `Button()`, `Text()`) to match the TS SDK. snake_case aliases are also provided (`card()`, `button()`, `text_element()`).

### Platform Rendering

Each adapter has a `cards.py` module with a renderer:

- **Slack**: `card_to_block_kit()` -- produces Block Kit JSON
- **Discord**: `card_to_discord_embed()` -- produces Discord embed dicts
- **Teams**: `card_to_adaptive_card()` -- produces Adaptive Card JSON
- **Telegram**: `card_to_telegram_inline_keyboard()` -- produces inline keyboard markup
- **WhatsApp**: `card_to_whatsapp_interactive()` -- produces WhatsApp interactive message
- **Google Chat**: `card_to_gchat_card()` -- produces Google Chat card v2
- **GitHub**: Falls back to markdown text
- **Linear**: Falls back to markdown text

Platforms that cannot render cards natively get `card_to_fallback_text()`, which produces a plain-text representation with `**title**`, field labels, ASCII tables, and `[alt](url)` for images.

## How Concurrency Works

The `Chat` class manages four concurrency strategies, configured via `ChatConfig.concurrency`:

### Drop (default)

```
Message arrives -> acquire_lock(thread_id, 30s TTL)
  Lock acquired?
    Yes -> dispatch to handlers -> release lock
    No  -> raise LockError (message dropped)
```

The simplest strategy. If another handler is already processing the same thread, the new message is dropped. Suitable for bots where only the latest context matters.

### Queue

```
Message arrives -> acquire_lock
  Lock acquired?
    Yes -> dispatch to handlers -> drain_queue() -> release lock
    No  -> enqueue(message, max_size) -> return
           (overflow behavior: drop-oldest or drop-newest)

drain_queue():
  while queue not empty:
    dequeue all entries
    skip expired entries
    dispatch latest entry (skip intermediate messages)
    extend lock
```

Messages that arrive while the lock is held are queued. After the current handler completes, the queue is drained. Only the latest queued message is actually processed; intermediate messages are passed as `context.skipped`.

### Debounce

```
Message arrives -> acquire_lock
  Lock acquired?
    Yes -> enqueue message -> debounce_loop()
    No  -> enqueue message (max_size=1, replaces previous)

debounce_loop() (max 20 iterations):
  sleep(debounce_ms)
  extend lock
  dequeue entry
  if queue empty -> break (no new messages arrived, process this one)
  if queue has more -> entry superseded, loop again
  dispatch final message
```

Waits for the user to stop typing. Each new message resets the debounce timer. Only the final message after a quiet period is processed.

### Concurrent

```
Message arrives -> dispatch to handlers (no lock, no queue)
```

No locking at all. Every message is processed immediately. Use when handlers are idempotent and fast.

### Lock Scope

Locks can be scoped to `thread` (default) or `channel`. The scope is determined by:
1. `ChatConfig.lock_scope` (static or callable)
2. `adapter.lock_scope` property (adapter default)

Channel-scoped locking serializes all messages in a channel, which is useful for bots that maintain channel-level state.

## How State Backends Work

### StateAdapter Protocol

The `StateAdapter` protocol in `types.py` defines 18 async methods across 6 categories:

| Category | Methods |
|----------|---------|
| Subscriptions | `subscribe`, `unsubscribe`, `is_subscribed` |
| Locking | `acquire_lock`, `release_lock`, `extend_lock`, `force_release_lock` |
| Key/Value Cache | `get`, `set`, `set_if_not_exists`, `delete` |
| Lists | `append_to_list`, `get_list` |
| Queues | `enqueue`, `dequeue`, `queue_depth` |
| Lifecycle | `connect`, `disconnect` |

### Lock Semantics

- `acquire_lock(thread_id, ttl_ms)` returns a `Lock` object with a unique token (CSPRNG-generated), or `None` if already held.
- `release_lock(lock)` releases only if the token matches (prevents releasing someone else's lock).
- `extend_lock(lock, ttl_ms)` extends the TTL, returning `False` if the lock was lost.
- `force_release_lock(thread_id)` unconditionally releases (admin escape hatch).

Lock tokens are generated using `secrets.token_hex(16)` for cryptographic randomness, prefixed with the backend name for debuggability (`mem_`, `redis_`, `pg_`).

### Backend Implementations

| Backend | Lock mechanism | Atomicity | Production-ready |
|---------|---------------|-----------|-----------------|
| Memory | In-process dict with expiry | Single-process only | No (dev/test) |
| Redis | `SET NX PX` + Lua scripts | Atomic via Lua | Yes |
| PostgreSQL | `INSERT ... ON CONFLICT` + row locks | Atomic via transactions | Yes |

Redis uses Lua scripts for `release_lock` and `extend_lock` to ensure token-check-and-delete is atomic. PostgreSQL uses `SELECT FOR UPDATE SKIP LOCKED` for non-blocking lock acquisition.

## The Markdown Pipeline

```
Input markdown string
        |
        v
parse_markdown(text) -> Root (mdast-compatible dict AST)
        |
        v
walk_ast(root, visitor) -> Root  (transform nodes)
        |
        v
stringify_markdown(ast) -> str   (back to markdown)
   or
from_ast(ast) -> str             (to platform format)
```

### Parser Details (`shared/markdown_parser.py`)

The parser is hand-rolled (not based on any library) and produces [mdast](https://github.com/syntax-tree/mdast)-compatible dict nodes.

**Block-level parsing** (line-by-line):
- Fenced code blocks (``` and ~~~)
- Thematic breaks (`---`, `***`, `___`)
- Headings (`# ` through `###### `)
- GFM tables (pipe-delimited with alignment row)
- Blockquotes (`> `)
- Ordered lists (`1. `, `2) `)
- Unordered lists (`- `, `* `, `+ `)
- Paragraphs (everything else)

**Inline parsing** (regex-based, priority-ordered):
- Images: `![alt](url "title")`
- Links: `[text](url "title")`
- Inline code: `` `code` ``
- Bold: `**text**`, `__text__`
- Strikethrough: `~~text~~`
- Emphasis: `*text*`, `_text_`

The inline parser uses an iterative approach for suffix text (to avoid stack overflow on long strings) while recursing into match content (bounded by match length).

### AST Utilities

- `walk_ast(node, visitor)` -- deep-copy + transform visitor pattern
- `ast_to_plain_text(node)` -- strip all formatting
- `table_to_ascii(node)` -- render mdast table as padded ASCII
- `stringify_markdown(ast)` -- AST back to markdown string

## The Streaming Pipeline

```
LLM stream (AsyncIterable)
        |
        v
from_full_stream()           -- normalize text-delta events, inject step separators
        |
        v
StreamingMarkdownRenderer    -- buffer incomplete constructs
  .push(chunk)               -- append text
  .render()                  -- get safe-to-display markdown (for edit_message)
  .get_committable_text()    -- get safe-for-append text (for native streaming)
  .finish()                  -- flush everything
        |
        v
adapter.stream() or fallback (post + edit loop)
```

### StreamingMarkdownRenderer

The renderer solves the problem of rendering incomplete markdown during LLM streaming. Key behaviors:

1. **Table buffering**: Lines matching `|...|` are held back until a separator row (`|---|---|`) confirms them as a table. Without this, pipe characters in regular text would be misinterpreted.

2. **Inline marker repair** (`_remend()`): Closes unclosed `**`, `*`, `~~`, `` ` ``, and `[` constructs by appending matching closers. This prevents broken formatting during mid-token streaming.

3. **Code fence tracking**: Uses incremental O(1) fence toggle counting. When inside a code fence, table buffering and inline repair are skipped.

4. **Table wrapping** (`_wrap_tables_for_append()`): For append-only streaming (Slack's native streaming API), confirmed tables are wrapped in code fences so pipe characters render as literal text.

5. **Clean prefix detection** (`_find_clean_prefix()`): Finds the longest prefix where all inline markers are balanced, used by `get_committable_text()`.

### Fallback Streaming (post + edit)

When an adapter does not support native streaming, `ThreadImpl._fallback_stream()` uses a post-then-edit pattern:

1. Post an initial placeholder message (configurable, default `"..."`)
2. Start a background `_edit_loop()` that updates the message at intervals (default 500ms)
3. Accumulate text from the stream
4. After stream ends, stop the edit loop and send a final edit with the complete text

The edit loop uses `asyncio.create_task()` for the background timer and checks a `stopped` flag to terminate cleanly.

Empty/whitespace-only content is guarded throughout: intermediate edits with empty content are skipped (platforms reject empty bodies), and if the final content is whitespace-only the placeholder is cleared to `" "` rather than left stranded. This is a small divergence from upstream 4.26 — see [Known Non-Parity](UPSTREAM_SYNC.md#known-non-parity-with-typescript-sdk).
