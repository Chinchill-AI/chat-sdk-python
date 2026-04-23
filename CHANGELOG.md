# Changelog

## 0.4.26 (2026-04-16)

Synced to [Vercel Chat 4.26.0](https://github.com/vercel/chat).

### New features (from upstream 4.26.0)
- **Standalone `reviver`**: new top-level `chat_sdk.reviver` function for deserializing `Thread`, `Channel`, and `Message` objects without importing a `Chat` instance. Designed for Vercel Workflow step functions and any environment where pulling adapter dependencies is undesirable. Use it as `json.loads(payload, object_hook=reviver)`. Lazy adapter resolution: `chat.register_singleton()` / `chat.activate()` must still be called before thread methods like `post()` are invoked.
- **Workflow-safe `to_json()`**: `Thread.to_json()` and `Channel.to_json()` now prefer the stored `_adapter_name` over `self.adapter.name`, so objects revived without a singleton can still be re-serialized.

### Fixes (from upstream 4.26.0)
- **Fallback streaming no longer edits/posts empty content**: `Thread.post(stream)` on adapters without native streaming no longer sends `{markdown: ""}` during the LLM warm-up or when a chunk buffers to whitespace. Empty streams with placeholders disabled now post a single space rather than an empty string (a non-empty `SentMessage` is required by the stream contract).
- **Slack empty header cells**: Markdown tables with an empty header cell now render as a single space in the Slack table block instead of being rejected by the Slack API. Replaces a truthiness-based fallback with an explicit length check, matching upstream.
- **Google Chat custom link labels**: `[Click here](https://example.com)` now renders as `<https://example.com|Click here>` (Google Chat's supported custom-label syntax) instead of `Click here (https://example.com)`.

### Python-specific (divergence from upstream 4.26)
- **Fallback streaming clears stranded placeholders**: when a stream produces only whitespace with the default placeholder enabled, the final edit replaces `"..."` with `" "` so the message doesn't render as permanently loading. Upstream 4.26 intentionally leaves the placeholder visible to avoid empty-edit API calls; we issue one final edit to `" "` instead. Documented under [Known Non-Parity](docs/UPSTREAM_SYNC.md#known-non-parity-with-typescript-sdk).
- **Google Chat `<url|text>` round-trip**: upstream 4.26 emits Google Chat's custom-label link syntax in the outgoing direction but doesn't parse it back in `to_ast()` / `extract_plain_text()`. A `[label](url)` posted through the gchat adapter would round-trip back as raw `"<url|label>"` text with no link node, breaking downstream handlers. We added the inverse regex to close the round-trip. Documented under Known Non-Parity.
- **`from_json(data, adapter=X)` syncs `_adapter_name`**: upstream leaves `_adapterName` at the payload value even when an explicit adapter is bound, so `to_json()` can emit a stale name that refers to a different adapter than what runtime calls use. We update `_adapter_name = adapter.name` on explicit rebind so serialize and runtime stay consistent. Documented under Known Non-Parity.
- **Google Chat `<url|text>` emit falls back to `text (url)` when it can't round-trip**: the custom-label syntax is only safe when the label doesn't contain `|` / `>` / `]` / newline, the label is non-empty, and the URL has an RFC 3986 scheme and no `|` or `>`. Upstream unconditionally emits `<url|text>`, producing malformed output for the edge cases. We fall back to `text (url)` (or bare URL for empty labels) so the content survives the round-trip and Google Chat's auto-link detection still fires for http(s) URLs. Documented under Known Non-Parity.
- **Google Chat headings render as bold**: `#` / `##` / etc. emit as `*text*` for visual distinction. Upstream falls through to plain-text concatenation and loses the visual hierarchy entirely. Google Chat has no heading syntax, and bold is the closest approximation the platform supports. Documented under Known Non-Parity.
- **Google Chat images render as `{alt} ({url})` (or bare URL)**: upstream has no image branch — the default fallback concatenates children only and silently drops the URL. We preserve the URL so the content isn't lost. Documented under Known Non-Parity.
- **Fallback streaming captures stream exceptions and flushes before re-raising**: if the text stream iterator raises mid-flight (e.g. LLM connection drops), `_fallback_stream` now awaits `pending_edit`, flushes whatever partial content was rendered, clears the placeholder if appropriate, and THEN re-raises the original exception. Upstream propagates immediately, orphaning `pendingEdit` as a background task and stranding `"..."` on the message. Documented under Known Non-Parity.
- **Fallback streaming final SentMessage carries repaired markdown**: the returned `SentMessage.markdown` is `renderer.finish()` output (`_remend`'d — inline markers auto-closed). Upstream ships raw `accumulated`. Narrow UX refinement — unobservable unless the stream ends mid-marker. Documented under Known Non-Parity.

## 0.4.25 (2026-04-10)

Synced to [Vercel Chat 4.25.0](https://github.com/vercel/chat). New versioning: `0.{upstream_major}.{upstream_minor}` embeds the upstream version directly.

### New features (from upstream 4.25.0)
- **Plan blocks**: `Plan` PostableObject for structured task lists with live updates. Post a plan to a thread, then `add_task()`, `update_task()`, and `complete()` with automatic card rendering.
- **Streaming table option**: `StreamingMarkdownRenderer(wrap_tables_for_append=False)` disables code-fence wrapping for platforms with native table support. Slack adapter now uses this by default.
- **Teams Select/RadioSelect**: `Select` and `RadioSelect` card elements now render as Adaptive Card `Input.ChoiceSet` with auto-injected submit button.
- **GitHub issue threads**: `issue_comment` webhooks on plain issues (not just PRs) now create threads with format `github:owner/repo:issue:42`.
- **Slack OAuth redirect fix**: `handle_oauth_callback` correctly forwards `redirect_uri` option.

### Versioning
- Version scheme changed from `0.0.1aX` to `0.{upstream_major}.{upstream_minor}[.patch]`
- `UPSTREAM_PARITY` constant in `chat_sdk.__init__` for programmatic access
- Sync procedure documented in [UPSTREAM_SYNC.md](docs/UPSTREAM_SYNC.md)

## 0.0.1a12 (2026-04-10)

Python 3.10 support, async-safe Chat resolver, and a large correctness audit.

### Upgrading

**Python 3.10 is now supported.** CI tests 3.10 through 3.13.

**Breaking changes** (all alpha — no stable API guarantees yet):

- **Serialization keys are now camelCase** (`threadId`, `channelId`, `adapterName`) to match the TS SDK. `from_json()` accepts both camelCase and snake_case, so existing stored data still loads.
- **`PermissionError` → `AdapterPermissionError`**: the old name shadowed Python's builtin. If you import it, update the name.
- **`StateNotConnectedError`** replaces bare `RuntimeError` when calling state methods before `connect()`. Catch `StateNotConnectedError` instead of `RuntimeError`.
- **`OnLockConflict` callbacks** should return `"force"` or `"drop"` (strings). Returning `True` still works for backward compat but is deprecated.
- **`reviver()`** no longer registers a global singleton. Each reviver is bound to the Chat that created it.

### New: async-safe Chat resolver

Thread and Channel deserialization now supports three resolution levels:

```python
# 1. Explicit (best for library code, multi-tenant)
thread = ThreadImpl.from_json(data, chat=my_chat)

# 2. Context-local (best for tests, request scoping)
with chat.activate():
    thread = ThreadImpl.from_json(data)

# 3. Global (existing pattern, unchanged)
chat.register_singleton()
thread = ThreadImpl.from_json(data)
```

Concurrent async tasks using `activate()` are fully isolated — each task resolves its own Chat without interference.

### Bug fixes

- Fixed streaming: intermediate edits now use the markdown renderer (was sending raw text), paragraph separators between agent steps, 500ms latency on stream end eliminated
- Fixed all adapters: token refresh race conditions, HTTP session reuse (was creating one per request), `limit=0` no longer silently replaced by defaults
- Fixed serialization: Slack installations now interoperate with the TS SDK, card fallback text extracted properly, AI SDK field names corrected
- Fixed Teams: status code comparison, modal dialog buttons, table cell escaping
- Fixed shutdown: in-flight handler tasks are cancelled, fire-and-forget tasks tracked for GC safety

### Internals

- 3,359 tests (up from 3,267), 0 warnings, 0 lint errors
- Automated test quality gate in CI (`audit_test_quality.py`)
- Comprehensive [porting guide](docs/UPSTREAM_SYNC.md) with 15 hazards and merge checklist
- [Known non-parity](docs/UPSTREAM_SYNC.md#known-non-parity-with-typescript-sdk) documented in one place

## 0.0.1a11 (2026-04-03)

Coverage and quality improvements.

- **Teams adapter**: 69% -> 79% line coverage (error handling, Graph API mapping, stream, card extraction, HTTP helpers)
- **Telegram adapter**: 68% -> 80% line coverage (webhook handling, reaction dispatch, emoji helpers, polling config, pagination, caching)
- **Test fidelity**: 100% test name alignment with TypeScript SDK (529/529 matched)
- Faithful line-by-line translations of chat/thread/channel test suites
- `MockAdapter.open_modal` accepts positional args (bug fix)

## 0.0.1a10 (2026-04-02)

Test fidelity enforcement + process improvements.

- Added test fidelity verification script
- Aligned all markdown, serialization, and AI test names with TS source
- 100% test name fidelity across all 529 TypeScript tests

## 0.0.1a9 (2026-04-02)

Faithful test translations and fidelity tooling.

- Faithful line-by-line translations of chat, thread, and channel tests
- Test fidelity verification infrastructure

## 0.0.1a8 (2026-04-07)

Full test parity with TypeScript SDK.

- **3,106 tests**, all passing
- Chat orchestrator: 96% of TS (concurrency, lock conflict, slash commands)
- Thread: 137% of TS (streaming, pagination, ephemeral, scheduling)
- Channel: 144% of TS (state, threads, metadata, serialization)
- Markdown: 126% of TS (node builders, round-trips, type guards)
- Integration: 94% of TS (recorded fixture replays for all platforms)
- All 8 adapters: 100%+ of TS test count

## 0.0.1a7 (2026-04-07)

Coverage improvements + webhook fixtures.

## 0.0.1a6 (2026-04-07)

Systematic port fidelity scan — 10 bugs fixed.

## 0.0.1a5 (2026-04-07)

Port fidelity release — 10 critical/high bugs fixed.

## 0.0.1a4 (2026-04-06)

Security hardening + launch documentation.

## 0.0.1a3 (2026-04-06)

Initial alpha release.
