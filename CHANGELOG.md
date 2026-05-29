# Changelog

## Unreleased

Part of the 0.4.29 sync wave (tracking issue #98).

### Behavior changes (Slack)

- **`webhook_verifier` now takes precedence over `signing_secret`** (#108, vercel/chat#468, commit `0f0c203`). When a Slack adapter is constructed with both `webhook_verifier` and `signing_secret` (or with `webhook_verifier` while `SLACK_SIGNING_SECRET` is set in the env), the verifier wins and the signing-secret path is dropped entirely. This **reverses** the precedence the Python port shipped in 0.4.27 (PR #87), which preferred `signing_secret` to match upstream's intent at that time. Upstream reversed itself in vercel/chat#468 (`chat@4.29.0`) so an env-configured `SLACK_SIGNING_SECRET` could not silently shadow a verifier the caller wired up; this port now follows. **Migration:** if you relied on a configured `signing_secret` overriding `webhook_verifier`, drop the `webhook_verifier` from your `SlackAdapterConfig` (or, if you wired the verifier in deliberately, your signing-secret path is now correctly inert and you can remove it). The built-in HMAC + 5-minute timestamp tolerance only applies on the signing-secret path; verifier implementers remain responsible for replay protection (`slack/types.py` SECURITY contract).

## 0.4.27 (2026-05-28)

Synced to upstream `vercel/chat@4.27.0` (release commit `f55378a`, Apr 30 2026). Highlights: Slack Socket Mode + dynamic bot-token resolver, Teams native DM streaming, `chat.get_user()` across all 8 adapters, Telegram MarkdownV2 rendering, and a sweep of adapter bug fixes. Sets `UPSTREAM_PARITY = "4.27.0"`.

### Upstream parity ports

#### Core (`packages/chat`)

- **`Chat.get_user(adapter, user_id)`** for cross-platform user lookups (#90, vercel/chat#391). Returns `User | None` with `email`, `display_name`, `avatar_url`, `is_bot` populated from each platform's user-lookup API. Every adapter exposes `async def get_user(user_id)`; Telegram is best-effort (`getChat` only), WhatsApp returns minimal user info (Cloud API has no separate lookup).
- **`ExternalSelect.initial_option` + `option_groups`** (#84, vercel/chat#410, #397). Type extensions on `ExternalSelect`; Slack adapter serializes `option_groups` to Block Kit.
- **`concurrency.max_concurrent` honored in `concurrent` strategy** (vercel/chat#419) — already enforced in the Python port via `asyncio.Semaphore`; upstream has caught up. Divergence row in `docs/UPSTREAM_SYNC.md` downgrades from "silent correctness bug upstream" to "behavior parity restored".

#### Slack (`packages/adapter-slack`)

- **Socket Mode transport** (#86, vercel/chat#162). New `SlackAdapterConfig(mode="socket", app_token="xapp-...")` opens a persistent WebSocket via `slack_sdk.socket_mode.aiohttp.SocketModeClient`. Outer reconnect loop (1s → 30s exp backoff, 250ms shutdown poll) layered on top of the SDK's auto-reconnect. Forwarded-events receiver in `handle_webhook` for the serverless variant (`x-slack-socket-token`, `hmac.compare_digest`). `ModalResponse(action="clear")` lands too. New optional extra: `chat-sdk-python[slack-socket]`. Closes #68.
- **Dynamic `bot_token` resolver + custom `webhook_verifier`** (#87, vercel/chat#421). `bot_token` now accepts `str | Callable[[], str | Awaitable[str]]`; resolver is invoked per request and cached in a per-instance ContextVar so concurrent webhooks don't share tokens. `webhook_verifier` replaces built-in HMAC + timestamp verification (returning a `str` substitutes the canonical body). `signing_secret` precedence over `webhook_verifier` preserved. `schedule_message().cancel()` and `Attachment.fetch_data` are rotation-safe. New `SlackAdapter.current_token_async()` for cron-style callers outside `handle_webhook`.
- **Slack streaming team_id fix for interactive payloads** (#85, vercel/chat#330). `recipient_team_id` extraction now walks `team_id` → `team` (string) → `team.id` (object) → `user.team_id` in order, returning `None` only when no string ID is found. Previously the entire `team` dict was forwarded for `block_actions`, breaking streaming routing.
- **Link-preview unfurl enrichment** (#89, vercel/chat#395). `message_changed` events are routed through a new `_handle_message_changed` handler with a 2s poll window and per-event link cache (1h TTL), so the message handler sees enriched links.
- **`@mention` regex preserves email addresses** (#91, vercel/chat#394). The `@user` matcher now skips `@` characters inside email localparts.
- **Empty `thread_ts` guard** (#89, vercel/chat#292). `stream()` now degrades to a single `post_message` for empty `thread_ts` instead of raising — top-level Slack DMs encode thread IDs with an empty `thread_ts` by design, and the old `ValidationError` silently dropped the reply.

#### Teams (`packages/adapter-teams`)

- **Native streaming for DMs via emit** (#88, vercel/chat#416). DM threads use the Bot Framework streaming protocol (`channelData.streamType=streaming` + `streamSequence`, then a final `streamType=final` message); group chats accumulate and post once (matches upstream's flicker-free behavior). New `TeamsAdapterConfig.native_stream_min_emit_interval_ms` (default 1500ms) honors Teams' ~1 req/sec quota; `StreamOptions.update_interval_ms` overrides. Send-failure mid-stream cancels the session and re-raises so `Thread.stream` history matches user-visible text. Migration to `microsoft-teams-apps` (Python SDK, GA 2026-05-01) tracked as #93 for 0.4.28.
- **DM conversation ID resolution for Graph API** (#85, vercel/chat#403). Bot Framework opaque DM IDs are rejected by Graph's `/chats/{chat-id}/messages` endpoint; the adapter now caches the user's `aadObjectId` from inbound activities into a `TeamsDmContext` keyed by base conversation ID and resolves to the canonical `19:{userAadId}_{botId}@unq.gbl.spaces` form on Graph calls.

#### Telegram (`packages/adapter-telegram`)

- **MarkdownV2 rendering** (#89, vercel/chat#407). Replaces the legacy `Markdown` parse_mode with `MarkdownV2`. Three escape contexts (normal text, code blocks, inline-link URLs) handle the spec's 18-char escape set per region.

#### Discord (`packages/adapter-discord`)

- **Card text deduplication** (#89, vercel/chat#256). Card posts omit `content` on create (Discord renders both `content` and the embed otherwise); edits explicitly send `content: ""` so leftover text from a previous edit is cleared.

### Python-only improvements

- **Markdown parser completeness** (#101). GFM task lists (`- [ ]` / `- [x]` → `checked: bool`), backslash-escaped delimiters (lookbehind `(?<!\\)` on inline regexes), inline math (`$x$`) preserved by `_remend` and the format converter. Sentinel-based escape protection prevents pathological backslash sequences from being eaten by emphasis/strikethrough regexes.
- **Streaming markdown list-marker awareness + table chunk-boundary** (#99, issue #69). `_get_committable_prefix` knows about list-marker positions so a chunk boundary lands cleanly; tables that span chunk boundaries are wrapped so the first chunk doesn't ship a half-table.
- **`SlackAdapter._upload_files`** uses `channel=` not `channel_id=` for `files_upload_v2` (#103, issue #102). The underlying `files_completeUploadExternal` forwards `channel_id=channel` internally, so caller-supplied `channel_id=` collided and raised `TypeError` on every Slack file upload.
- **Adapter dict-StreamChunk support** (#105). `slack`, `github`, and `google_chat` stream loops now honor the dict-shaped `{"type": "markdown_text", ...}` chunks that `thread.py`'s `_from_full_stream` has always forwarded (Teams already honored). Slack `send_structured_chunk` rewritten with a `_read()` helper for dict/dataclass uniformity; fallback warning message rewritten to name the actual possible causes.
- **Google Chat card text rendering** (#92). `GoogleChatFormatConverter` now uses the full markdown parser for card text (was a regex stub that dropped formatting).
- **Adapter init logs + adapter-list in not-found errors** (#104). `GoogleChatAdapter.initialize()` and `GitHubAdapter.initialize()` now log on init (matching Slack/Teams). `Chat.channel()` / `Chat.thread()` "adapter not found" errors append `(registered adapters: [...])` so operators can disambiguate "never constructed" from "wrong lookup name".

### Sync-process documentation

- **Review-loop discipline** (`docs/UPSTREAM_SYNC.md`, `docs/SELF_REVIEW.md`). Codifies the lessons learned from this wave: self-review before opening the PR (cheaper than bot rounds), trace fix cascades across overlapping PRs, prefer official SDKs over hand-rolled implementations, cap drafts to 3–4 in flight, divergence budget of ≤2 per sync PR. `docs/SELF_REVIEW.md` adds adversarial check categories (input sweeps, emit/parse symmetry, pass-interaction, unforgeable sentinels, rebind/state coherence).

### Upstream items not ported

- **`@chat-adapter/web`** (Vue + Svelte browser UI, vercel/chat#444) — no browser runtime in chat-sdk-python.
- **Teams SDK 2.0.8 + `User-Agent` header** (vercel/chat#415) — JS-only. The Python Teams adapter uses raw `aiohttp`, not `botbuilder`; tracked in `docs/UPSTREAM_SYNC.md` non-parity table as a deferred enhancement.
- **Bundled guide markdown + templates manifest** (vercel/chat#423) — TS-monorepo authoring resources, not runtime behavior.

### Upstream tagging note

Upstream cut versions for the entire monorepo on Apr 30 2026 (commit `f55378a`), but only `@chat-adapter/shared@4.27.0` got a git tag — no `chat@4.27.0` tag was published. The fidelity workflow (`scripts/verify_test_fidelity.py`, `.github/workflows/lint.yml`) stays pinned to `chat@4.26.0` for this release; it'll move to a 4.27 SHA pin (or a real tag if upstream publishes one) in the next sync.

## 0.4.26.3 (2026-05-07)

Python-only fix. No upstream version change.

### Fixes

- **`SlackFormatConverter.render_postable` now uses the AST path for all markdown inputs** (issue #81). Previously, `PostableMarkdown` and `{"markdown": ...}` dict inputs were routed through a private regex helper (`_markdown_to_mrkdwn`) that truncated URLs containing parentheses and diverged silently from the TS SDK's `fromAst(parseMarkdown(text))` behavior. Both branches now call `from_markdown`, which goes through the AST. `str` and `raw` branches are unchanged.

### Structural parity

- **Deleted `_markdown_to_mrkdwn`** — a regex-based private method with no call sites after the fix above. The TS SDK has no equivalent; its presence was an undocumented divergence. Removes a confusing dead-code path and restores structural parity with `adapter-slack/src/markdown.ts`.

### Additions

- **`render_postable` now handles card and object-with-ast inputs** — added `{"card": ...}` dict, `{"type": "card", ...}` `CardElement` dict, `{"ast": ...}` dict, and `.card` / `.ast` attribute branches, plus `str(message)` fallback for unrecognized types. Matches the full union of `AdapterPostableMessage` variants.

### Test quality

- Added 19 tests to `tests/test_slack_format.py` covering all `render_postable` branches, every `_node_to_mrkdwn` node type (heading, blockquote, thematic break, image with/without alt), the remaining `extract_plain_text` paths (strikethrough, bare URL, channel mentions), and `to_blocks_with_table` edge cases (non-dict AST, standalone table, column alignment).

## 0.4.26.2 (2026-04-24)

Parity catch-up with upstream `4.26.0`. No upstream version change.

### New public APIs

- **`Thread.get_participants()`**: returns unique non-bot, non-self authors
  who've posted in the thread. Seeds from `current_message.author` (if
  eligible), then iterates `all_messages()` and dedupes by `user_id`.
  Mirrors upstream TS `Thread.getParticipants()`. Issue #54.
- **`Chat.on_options_load(...)` + `Chat.process_options_load(...)`**: port of
  upstream `onOptionsLoad` / `processOptionsLoad` for handling
  external-select option-load events. Specific action IDs run before
  catch-all handlers; errors are logged and skipped so later handlers still
  get a chance. New public types: `OptionsLoadEvent`, `OptionsLoadHandler`.
- **Slack `block_suggestion` dispatch**: the Slack adapter now routes
  `block_suggestion` interactive payloads through `process_options_load`
  and serializes the result to a Slack options JSON response. The handler
  is raced against a 2.5s budget (`OPTIONS_LOAD_TIMEOUT_MS`); on timeout
  the response is empty options and the orphaned task still logs errors
  via `asyncio.shield`. Issue #50.
- **`IoRedisStateAdapter`**: `RedisStateAdapter` subclass defaulting to the
  `ioredis_` lock-token prefix used by upstream Vercel Chat's `ioredis`-backed
  state. Enables cross-runtime Redis sharing between TS and Python chat-sdk
  deployments during migrations. Closes #71.
  Note: the token *shape* after the prefix diverges intentionally — Python
  emits `ioredis_{ms}_{hex32}` (`secrets.token_hex(16)`, CSPRNG) whereas
  upstream emits `ioredis_{ms}_{base36<=13}` (`Math.random().toString(36)`,
  not CSPRNG). Lock-release still works across runtimes because each
  runtime generates its own token on acquire and `release_lock` / `extend`
  compare the full token string — the divergence is observability-only
  (log lines, bytes-in-Redis), not a functional incompatibility. We will
  not regress to `Math.random()` for cosmetic byte-for-byte parity.
- **`RedisStateAdapter(token_prefix=...)`**: new `token_prefix` kwarg
  (default `"redis"`). Parameterizes the lock-token prefix for observability
  and interop.
- **`StreamingPlan` / `StreamingPlanOptions`** (`chat_sdk.plan`): a
  `PostableObject` wrapping an async iterable with platform-specific
  streaming options (`group_tasks`, `end_with`, `update_interval_ms`).
  Mirrors upstream `streaming-plan.ts`. Issue #56.
- **`Adapter.rehydrate_attachment` hook + `Attachment.fetch_metadata`**:
  port of upstream's `rehydrateAttachment` hook. `Chat._rehydrate_message`
  invokes the hook on every attachment that lost its `fetch_data` closure
  during a JSON roundtrip (queue / debounce / persistent state). The new
  serializable `fetch_metadata: dict[str, str] | None` field persists
  adapter-specific identifiers (Slack `url` + `teamId`, Teams `url`,
  Google Chat `resourceName` + `url`, Telegram `fileId`, WhatsApp
  `mediaId`). Implementations land on Slack, Teams, Google Chat, Telegram,
  and WhatsApp. Each rehydrate closure validates the target URL against a
  per-adapter allowlist before forwarding the auth token (SSRF defense).
  Closes #52.

### Upstream parity

- **Teams: `TeamsAuthCertificate` config shape** (Issue #58). Ports the
  upstream `TeamsAuthCertificate` interface (`adapter-teams/src/types.ts:3-10`)
  as a Python dataclass with `certificate_private_key`, `certificate_thumbprint`,
  and `x5c` fields. `TeamsAdapterConfig(certificate=...)` is accepted and
  re-exported from `chat_sdk.adapters.teams` so consumers can code against the
  shape ahead of MS Teams SDK support. Passing a non-`None` value still throws
  at adapter startup — the error message is now verbatim with
  `adapter-teams/src/config.ts:13-18` (`"Certificate-based authentication is
  not yet supported by the Teams SDK adapter. Use appPassword (client secret)
  or federated (workload identity) authentication instead."`). Not a functional
  implementation; upstream does not implement cert auth either.

### Test fidelity

- Ported the 4 `[getParticipants]` tests from `thread.test.ts` and the 4
  `[thread]` factory tests from `chat.test.ts` (existing-behavior coverage
  for `Chat.thread(id)`). Closes 8 fidelity gaps.
- Ported 19 `[post with Plan]` tests from `thread.test.ts` — closes #55.
- Ported 6 `[Streaming]` StreamingPlan option-variant tests from upstream
  `thread.test.ts` — closes #56.

### Fixes

- **`Plan.update_task(input)` now honors `input.id`** — previously only worked on the last in-progress task; with `id` set, targets that specific task and returns `None` for unknown IDs. Matches upstream `UpdateTaskInput` semantics.
- **`Plan.add_task()` / `update_task()` now propagate `adapter.edit_object` errors** — previously swallowed and logged; upstream returns the chained promise so callers see failures.
- **Plan edit queue is now actually sequential under concurrency** — previously racy under `asyncio.gather`; rewrote `_enqueue_edit` to build the chain synchronously before awaiting, matching upstream TS's `.then`-based chain. Fixes out-of-order edits when multiple `add_task`/`update_task` calls interleave.
- **`StreamingPlan` options now wired through `Thread.post()`** — the Python
  port was missing the `StreamingPlan` class entirely, so `group_tasks` /
  `end_with` / `update_interval_ms` were silently dropped (a plain async
  iterable was the only way to stream, and options went nowhere). Upstream
  already had the `kind === "stream"` branch that maps
  `groupTasks → taskDisplayMode`, `endWith → stopBlocks`, and
  `updateIntervalMs → updateIntervalMs` onto `StreamOptions` before invoking
  `adapter.stream(...)` or the fallback `post+edit` path. Issue #56.

### Test hygiene

- Sweep remaining `time.sleep` → `await asyncio.sleep` in async tests
  (`test_memory_state.py`, `test_state_postgres.py`). Closes the same
  flaky-test hazard fixed for the Redis backend in PR #73.

### CI / Internals

- `verify_test_fidelity.py` now enforces against upstream on every PR
  (`.github/workflows/lint.yml`); fails when the upstream clone is missing
  or when any mapped TS file can't be found. Workflow runs `--strict` and
  the clone step no longer carries `continue-on-error: true`, so infra
  failures surface immediately at the job level. Baseline shipped empty
  (all previously-missing tests ported in this release) — strict fidelity
  for *mapped core files* (8 of 17 `packages/chat/src/*.test.ts` files;
  see the `MAPPING` dict in `scripts/verify_test_fidelity.py` for the
  authoritative scope list). Closes #53.

## 0.4.26.1 (2026-04-23)

Python-only follow-up on `0.4.26`. Still alpha — APIs may change.

### Fixes

- **Slack native streaming**: `SlackAdapter.stream()` no longer calls
  `AsyncWebClient.chat_stream(...)` without `await`. The unawaited coroutine
  returned a truthy object, and the first `streamer.append(...)` raised
  `AttributeError`, breaking native Slack streaming for any consumer using
  the default adapter. Issue #44.
- **Teams divider renders at non-zero height**: empty `Container` with
  `separator: True` rendered as zero-height in the Teams UI. Dividers
  between siblings now hoist `separator: True` onto the following element;
  a trailing divider emits a minimal non-empty Container. Issue #45.
- **`ConcurrencyConfig.max_concurrent` is now enforced**: consumers setting
  `concurrency=ConcurrencyConfig(strategy="concurrent", max_concurrent=N)`
  now actually get an `asyncio.Semaphore(N)` cap on in-flight handlers.
  Previously the field was accepted and ignored (upstream TS has the same
  gap). `None` / unset keeps the unbounded default. Issue #51.

### Python-specific (divergence from upstream 4.26)

- **Fallback streaming runtime robustness** (cluster of fixes): framework-
  agnostic `request.text()` handling now tolerates sync Flask-style
  requests (was raising `TypeError: object is not awaitable`). Handlers
  typed `Callable[..., Awaitable[None] | None]` may return sync (`None`) —
  the dispatcher now `await`s only when `inspect.isawaitable()` confirms,
  preventing runtime crashes on sync handlers.
- **`max_concurrent` enforcement** (see above) — upstream accepts the
  config field but never enforces it; we do.

### New public APIs

- **`Chat.thread(thread_id, *, current_message=None)`**: new worker-
  reconstruction factory mirroring TS `chat.thread(threadId)`. Adapter is
  inferred from the thread-ID prefix; state and message history come from
  the Chat instance. `current_message` is preserved so Slack native
  streaming still works post-reconstruction. Issue #46.
- **`SlackAdapter.current_token` / `current_client`**: public `@property`
  accessors for the request-context-bound bot token and a preconfigured
  `AsyncWebClient`. Replaces underscore access from consumer code making
  direct Slack Web API calls inside a handler (email resolution, user
  profile fetches, etc.). Issue #47.

### Internals

- **Pyrefly: 213 → 0 type errors**; baseline file removed. CI now enforces
  zero errors. Root causes fixed: 8-adapter `lock_scope: LockScope | None`
  protocol conformance; `_ChatSingleton` as `Protocol`; submodule-aware
  `replace-imports-with-any`; `NoReturn` on error re-raisers;
  `inspect.isawaitable` guards for duck-typed request handling and
  sync-or-async handler dispatch. No `Any` widening, no new `# type:
  ignore` lines beyond 10 at adapter event-construction sites where
  `thread=None`/`channel=None` get re-wrapped by `Chat` before handler
  dispatch (matches upstream TS's `Omit<>` partial-event pattern).
- Test count: **3545 passed**, 2 skipped.

### Known gaps (not fixed in this release)

- `onOptionsLoad` handler for dynamic select dropdowns — issue #50
- `Thread.getParticipants()` method — issue #54
- `rehydrate_attachment` adapter hook for queue/debounce + attachments —
  issue #52
- 40 upstream tests without Python equivalents (Options Load, Plan variants,
  StreamingPlan options, getParticipants) — issue #53
- Discord native Gateway WebSocket (HTTP-only today) — issue #57
- Teams certificate-based mTLS auth — issue #58
- Google Chat file uploads (TODO upstream too) — issue #59
- Global handler-dispatch bound across reactions/actions/slash/modals — issue #61

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
