# Design Decisions

Key design decisions in `chat-sdk-python` and their rationale. Understanding these prevents well-intentioned refactors from breaking the architecture.

## Why Hand-Rolled Markdown Parser

**Decision**: Implement a custom markdown parser (`shared/markdown_parser.py`) rather than using an existing Python library.

**Rationale**:

1. **mdast compatibility**: The SDK requires an AST that matches the [mdast specification](https://github.com/syntax-tree/mdast) -- the same AST format used by the TS SDK's `unified`/`remark` ecosystem. No Python markdown library produces mdast-compatible output. `markdown-it-py` produces its own token format. `mistune` produces a different AST. `commonmark` produces a different tree structure. All would require a translation layer that is more code than the parser itself.

2. **Round-trip fidelity**: The parser must support `parse -> walk/transform -> stringify` without losing information. Existing libraries optimize for HTML output, not round-trip markdown editing.

3. **Subset is sufficient**: The SDK only needs: paragraphs, headings, code blocks, blockquotes, lists, tables (GFM), thematic breaks, bold, italic, strikethrough, inline code, links, and images. This is roughly 550 lines of well-tested code. A full CommonMark parser (with all edge cases) is ~10x more code and handles constructs the SDK does not need.

4. **Zero dependencies**: The parser uses only `re` from the standard library. Adding a markdown library would violate the zero-runtime-dependency constraint (see below).

5. **Streaming compatibility**: The `StreamingMarkdownRenderer` needs to understand the same constructs the parser recognizes (code fences, tables, inline markers). Using a separate library for parsing and hand-rolling the streaming logic would create divergence.

## Why Not Microsoft Bot Framework SDK for Teams Auth

**Decision**: Implement Teams JWT verification directly rather than using the `botbuilder-python` SDK.

**Rationale**:

1. **No async support**: The Microsoft `botbuilder-python` SDK is synchronous. The chat-sdk is fully async. Wrapping sync calls in `run_in_executor` adds complexity and defeats the purpose of async I/O.

2. **Heavy dependency**: `botbuilder-python` pulls in dozens of transitive dependencies and has a large API surface. We only need JWT verification and HTTP posting, which is ~200 lines of code.

3. **Maintenance status**: The Python Bot Framework SDK has historically lagged behind the .NET and Node.js versions. Features and fixes arrive late.

4. **Consistency**: All other adapters implement their own auth directly. Having Teams use a separate SDK would create an inconsistent pattern.

The tradeoff is that we must maintain the JWKS fetching and JWT validation code ourselves, including handling key rotation (see [SECURITY.md](SECURITY.md#teams-jwks-key-rotation-window)).

## Why PascalCase for Card/Button/Modal Builders

**Decision**: Use PascalCase function names (`Card()`, `Button()`, `Text()`, `Modal()`, `Select()`) as the primary API, with snake_case aliases.

**Rationale**:

1. **TS SDK compatibility**: The TS SDK uses PascalCase for these builders (`Card()`, `Button()`, `Text()`). Users porting code from TS to Python should find the API familiar.

2. **Visual distinction**: PascalCase makes builder calls visually distinct from regular function calls. `Card(title="Hi", children=[Text("Hello")])` reads as a declarative element tree, not a sequence of operations.

3. **SQLAlchemy precedent**: Python's own ecosystem has precedent for PascalCase functions that construct objects. SQLAlchemy's `Column()`, `String()`, `Integer()`, `ForeignKey()` follow this pattern.

4. **snake_case aliases provided**: Every PascalCase builder has a snake_case alias (`card()`, `button()`, `text_element()`). PEP 8 purists can use those. Both are exported in `__all__`.

5. **Not classes**: The builders are functions that return TypedDicts, not class constructors. Using PascalCase for class constructors is standard Python. Using it for factory functions is a deliberate style choice for this domain.

## Why Global Singleton on Chat

**Decision**: `Chat` registers itself as a global singleton via `set_chat_singleton(self)`. Thread and Channel deserialization uses `get_chat_singleton()` to resolve adapters.

**Rationale**:

1. **Deserialization without context**: When a `ThreadImpl` is deserialized from JSON (e.g., from Redis state, from a queued entry, from modal context), it needs to resolve its adapter by name. Without a singleton, every deserialization call would need an explicit `chat` parameter threaded through the call stack.

2. **Matches TS SDK**: The TS SDK uses `chat-singleton.ts` with the same pattern. Keeping the pattern identical reduces the cognitive load of syncing changes.

3. **Single Chat instance is the normal case**: In practice, applications have exactly one `Chat` instance. The singleton is registered during initialization and remains for the lifetime of the process.

4. **Testing escape hatch**: `clear_chat_singleton()` and `has_chat_singleton()` are provided for test isolation. Each test can register its own singleton.

## Why Zero Runtime Dependencies in Core

**Decision**: The `chat-sdk` core package (`pip install chat-sdk`) has zero runtime dependencies. All adapter-specific libraries are optional extras.

**Rationale**:

1. **Install-time safety**: Users should be able to `pip install chat-sdk` without worrying about dependency conflicts. Zero deps means zero conflicts.

2. **Deployment size**: Serverless environments (AWS Lambda, Google Cloud Functions) have package size limits. A core package with no deps keeps the base small.

3. **Adapter isolation**: Installing `chat-sdk[slack]` adds `slack-sdk`. Installing `chat-sdk[discord]` adds `pynacl` and `aiohttp`. Users only pay for what they use.

4. **Lazy imports**: Adapter modules import their optional dependencies inside methods, not at the top of the file. This means `import chat_sdk` never fails, even if no adapter extras are installed. See [UPSTREAM_SYNC.md](UPSTREAM_SYNC.md#9-top-level-imports-of-optional-deps-must-be-lazy).

The optional extras are defined in `pyproject.toml`:

```toml
[project.optional-dependencies]
slack = ["slack-sdk>=3.27.0"]
discord = ["pynacl>=1.5", "aiohttp>=3.9"]
teams = ["aiohttp>=3.9"]
telegram = ["aiohttp>=3.9"]
whatsapp = ["aiohttp>=3.9"]
google-chat = ["aiohttp>=3.9", "google-auth>=2.0", "pyjwt>=2.8"]
github = ["pyjwt[crypto]>=2.8"]
linear = ["aiohttp>=3.9"]
redis = ["redis>=5.0"]
postgres = ["asyncpg>=0.29"]
crypto = ["cryptography>=42.0"]
all = [...]  # everything
```

## Why `from __future__ import annotations` Everywhere

**Decision**: Every Python file in the SDK starts with `from __future__ import annotations`.

**Rationale**:

1. **PEP 604 syntax on Python 3.10**: The SDK uses `X | Y` union syntax (e.g., `str | None`) and `list[str]` lowercase generics. Without the future import, these require Python 3.10+. With it, they work on Python 3.7+. Since the SDK targets Python 3.11+, this is belt-and-suspenders.

2. **Forward reference resolution**: Annotations are stored as strings and resolved lazily. This eliminates circular reference issues in type hints (e.g., `Thread` referencing `Channel` and vice versa).

3. **Consistency**: Rather than deciding per-file whether to use the import, it is applied everywhere as a project convention. This prevents accidental `NameError` when a contributor adds a forward reference.

## Why BaseAdapter with ChatNotImplementedError Defaults

**Decision**: `BaseAdapter` provides default implementations for optional methods that raise `ChatNotImplementedError` rather than using `abc.abstractmethod`.

**Rationale**:

1. **Progressive implementation**: Adapter authors can implement the minimum required methods first and add optional features later. `abc.abstractmethod` would force them to implement everything upfront.

2. **Runtime discovery**: Code that checks `if hasattr(adapter, "stream") and adapter.stream` can discover at runtime whether an adapter supports a feature. With abstract methods, the adapter would have a method that raises `NotImplementedError`, and the check would need to be `try/except`.

3. **Clear error messages**: `ChatNotImplementedError("slack", "scheduling")` produces `"slack does not support scheduling"`, which is more informative than `NotImplementedError`.

4. **Matches TS SDK**: The TS SDK uses optional interface methods (possible in TypeScript but not in Python's Protocol). BaseAdapter with defaults is the Python equivalent.

The required methods from the `Adapter` protocol (18 methods) must still be implemented. `BaseAdapter` only provides defaults for the ~10 optional methods.
