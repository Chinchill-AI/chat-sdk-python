# Changelog

## Unreleased (4.41 wave)

Sync wave from `chat@4.31.0` to `chat@4.41.1` (tracking #184). `UPSTREAM_PARITY` stays `4.31.0` until #203.

- **Tooling: fidelity tooling for the 4.41 wave** (#185, closes #79, advances #78). Tooling only; no consumer-visible behavior change.
  - `scripts/upstream_pin.json` is now the single source for the upstream checkout: `pin` (strict CI tag `chat@4.31.0` plus its commit SHA) and `target` (`chat@4.41.1` plus its SHA). `verify_test_fidelity.py`, `lint.yml` (via `jq`) and the `CLAUDE.md` clone snippet all read it.
  - CI fails if the cloned tag's HEAD differs from `pin.sha`, and writes the SHA to the job summary. The script does the same check when `TS_ROOT` is a git checkout, and only warns for a plain export.
  - New `--check-docs` step in CI: `CLAUDE.md` and `docs/UPSTREAM_SYNC.md` can no longer name a different pin. This fixed the stale `4.30.0` line. The pin's major.minor must also match `UPSTREAM_PARITY`.
  - `it.each` / `test.each` templates now count as one logical test each, with placeholders stripped. `test("…")` also counts, and `describe.each` titles are used for reporting. The strict count at the pin is now **733/733** (was 732): `thread.test.ts`'s existing `$label` template is now checked.
  - Scope is two-tier. `MAPPING` stays strict. New `TARGET_MAPPING` rows are checked only by the new `--report-target` mode. New `UNMAPPED` lists deliberate skips with reasons, and every core `*.test.ts(x)` must be classified in one of the three.
  - Per-file output now shows exact vs fuzzy match counts.
  - Fuzzy-match ties now resolve the same way in every run. They used to depend on the per-process string hash seed, so the target total could flip between 282 and 283 missing.
  - The fuzzy pass matches plain tests before `.each` templates. A template's placeholder-stripped title is short enough to claim a later plain test's translation, which reported `chat.test.ts` "should not match GitHub-style logins as Slack ids (case sensitivity)" as missing while hiding the real `isMention` template gap.
  - Fails closed instead of silently undercounting: `--strict` and `--report-target` fail when an upstream test cannot be extracted (baseline mode only warns). The extractor now also reads modifier chains (`it.skip`, `it.concurrent.each`, …), `.for`, `<T>` type arguments, `.skipIf(c)` / `.runIf(c)` and single-quoted or wrapped titles, and reports any other call form (`it.todo`, `it.extend`, …). The checkout check also fails on local edits under `packages/chat/src`, and when `TS_ROOT/.git` exists but git cannot read it. `--report-target` also refuses a sparse or partial checkout (a core test file in the commit but not on disk) and a plain export missing a mapped file, instead of reporting that file as absent at the target.
  - Test calls are discovered only in code: a `'test("x")'` fixture string or a commented-out `// it.each(…)` is neither counted nor reported as unextractable, and a `describe.only("s", () => { it("t") })` one-liner no longer drops its test. A template literal or block comment left open at end of file is an extraction error instead of silently hiding the tests after it, and a regex right after `if (…)` / `while (…)` / `for (…)` is lexed as a regex. A computed title (`"rejects " + kind`) is an extraction error instead of being matched by its literal prefix. Extraction over every upstream `*.test.ts(x)` at both tags is unchanged.
  - `--report-target` prints its delta against the report committed at `HEAD` (via `git show`), so a second run before committing no longer compares against the first run's output and prints `+0`.
  - `--check-docs` is case-insensitive and also catches `--branch=`, `-b`, line-continued and markdown-decorated pins.
  - `scripts/fidelity_target.json` is committed as the authoritative wave-wide missing list at `chat@4.41.1`: 282 missing of 1036 (130 in strict-tier files, 152 in target-tier files), including 16 `.each` templates.
- **BREAKING (security) — Telegram: webhook verification is required by default; repeated updates are deduplicated** (#224; upstream vercel/chat#858, #799, #813).
  - Telegram webhook deployments without `TELEGRAM_WEBHOOK_SECRET_TOKEN` / `secret_token` now **fail to start or return 401**: `mode="webhook"` raises `ValidationError` in the constructor, `mode="auto"` raises from `initialize()` when it resolves to webhook mode (so `Chat` initialization fails and is retried on every webhook until the config is fixed), and `handle_webhook` returns 401 `"Webhook verification required"` before reading the body. Previously the adapter logged a warning and dispatched every update, including `callback_query` button actions.
  - **Escape hatch:** `TelegramAdapterConfig(allow_unverified_webhooks=True)` or `TELEGRAM_ALLOW_UNVERIFIED_WEBHOOKS=true` (only the exact string `"true"` counts; an explicit `allow_unverified_webhooks=False` wins over the env var; a non-`bool` value such as `"false"` raises `ValidationError` rather than being treated as truthy). Polling mode needs neither.
  - Each accepted webhook update's integer `update_id` (an integral float such as `7.0` counts as `7`, as in JS) is claimed via the state adapter's `set_if_not_exists` (`telegram:webhook-update:{sha256(bot_user_id)}:{update_id}`, 24h TTL) before dispatch, so a Telegram redelivery runs handlers once. Duplicates return 200 without dispatch; a state or bot-identity failure returns 503 without dispatch (Telegram retries later). The scope is derived from the bot's user id, not its token, so it survives token rotation.
  - Bot identity (`getMe`) is now resolved through a shared, retrying lookup: a failed startup `getMe` is retried on the next webhook instead of leaving `bot_user_id` unset. Once resolved it is cached; a repeat `initialize()` keeps the `getMe` username instead of reverting to `Chat.user_name`.
  - `TelegramAdapterConfig.allow_unverified_webhooks` is appended as the last field, so positional construction binds the same parameters as before.
  - `secret_token` now resolves with `??` semantics: an explicit `secret_token=""` no longer falls back to `TELEGRAM_WEBHOOK_SECRET_TOKEN` (it counts as "no secret").
- **Messenger: guard attachment downloads** (#234, **security**; port of vercel/chat 153bd964, chat@4.39.0). `Attachment.fetch_data()` used to GET whatever URL arrived in the webhook `payload.url` and follow redirects, and `fallback` / link-share attachments carry user-controlled URLs (SSRF). Downloads are now restricted to https URLs on `fbsbx.com` / `fbcdn.net` (or a subdomain), checked before any network I/O and on every redirect hop (at most 5), with a 25 MB body cap and a 30 s deadline; failures raise `NetworkError("messenger", ...)`. The check runs inside the download closure, so closures rebuilt by `rehydrate_attachment` from persisted queue/debounce state are covered too.
  - **Consumer-visible:** `fetch_data()` for a Messenger attachment whose URL is not on a Meta CDN host (typically `fallback` / link shares) now raises `NetworkError("messenger", "Refusing to fetch an untrusted attachment URL")` instead of downloading. `attachment.url` is unchanged and still available for display.
  - **Python-specific (divergence from upstream):** no DNS / private-IP resolution check yet (the host allowlist alone rejects IP literals and non-Meta names; tracked for #204/#239), the URL check is stricter than upstream (also rejects userinfo, non-443 ports and non-ASCII or non-DNS-label hosts), and IP-literal / unparseable URLs raise the "untrusted" message instead of upstream's "internal" message or generic download-failure wrapper. See `docs/UPSTREAM_SYNC.md`.
- **Twilio: per-conversation locks and channels** (#235; **security**, **breaking (Twilio)**). Ports upstream `28bc7768` (vercel/chat#849, chat@4.39.0) and the Twilio part of `b7c9316b` (vercel/chat#875, chat@4.40.0). The adapter's `lock_scope` is now `"thread"`, and `twilio_channel_id` / `channel_id_from_thread_id` / `fetch_thread().channel_id` return the full `twilio:{sender}:{recipient}` thread id instead of the shared bot-side `twilio:{sender}`. Different recipients texting the same bot number no longer share a lock (previously they serialized behind each other, and under the default `drop` strategy the later one raised `LockError`), channel history or channel state.
  - **Consumer-visible:** Twilio `channel_id` (and `thread.channel.id`) now equals the thread id. Channel-scoped state and channel-history keys written under the old `twilio:{sender}` id are no longer read. Thread ids and thread history are unchanged; `channel_name` is still the sender number.
- **Twilio: authenticated media downloads restricted to the configured API origin** (#235; **security**, **breaking (Twilio, custom `api_url` only)**). Ports upstream `d8103a10` (vercel/chat#831, chat@4.38.1). `fetch_twilio_media` gains keyword-only `api_url` / `api_base_url` and raises `TwilioApiError("Twilio media URL must match the configured Twilio API origin", status=0)` for any URL whose scheme, host or effective port differs from `api_url` → `api_base_url` → `https://api.twilio.com`. The check runs before credentials are resolved or a request is made. The adapter passes its `api_url` to every attachment download, freshly received webhook media (`MediaUrlN`) as well as rehydrated attachments, so the existing Python-only Twilio host allowlist stays in front as defence in depth (documented in `docs/UPSTREAM_SYNC.md`).
  - **Consumer-visible (custom `api_url` only):** with a non-default `api_url`, media hosted on any other origin, including inbound media on `https://api.twilio.com`, is now refused (upstream behaves the same). With a non-Twilio or `http` `api_url` (a proxy or local mock), no media URL passes both layers: `api.twilio.com` fails the origin check and the proxy origin fails the host allowlist, so attachment downloads always raise. Before this change such configs downloaded `api.twilio.com` media. The default config (`api_url` unset) is unaffected.
- **Teams: cap `microsoft-teams-{apps,api,cards}` at `<2.1`.** `uv.lock` is not committed and the extras were unbounded, so fresh installs resolved `microsoft-teams-apps` 2.1.0 (released 2026-09-16). 2.1.0 removed `App.activity_sender`, which the adapter uses to create native DM streams (`teams/adapter.py:943`), and changed the activities-client `update` signature that `edit_message`'s service-URL retargeting relies on. Native streaming and edits could fail on a fresh install, and CI turned red. The cap resolves to 2.0.16 until the adapter supports 2.1.
- **Core concurrency: lock heartbeat, per-thread drain isolation, debounce drain** (#190; **security** for channel-scoped locks, **consumer-visible**). Ports upstream `eccc6b91` (#656, chat@4.32.0), `076fe5dc` (#659, chat@4.33.0), `6cb933eb` (#832, chat@4.38.1) and `5b538f6f` (#821, chat@4.39.0).
  - **Lock heartbeat.** A held thread/channel lock is now renewed every 10s (`DEFAULT_LOCK_TTL_MS / 3`) while the handler runs, for the `drop`, `queue`, `debounce` and `burst` strategies. Before, a handler running longer than 30s lost its lock and a second message could run **concurrently** on the same thread. New `ConcurrencyConfig.max_lock_lifetime_ms` (default `600_000`, 10 minutes) caps renewal: after it, the lock lapses one TTL later so a hung handler cannot block the thread forever. If an extend returns `False`, or the backend stays unreachable past the lock's last known expiry, the queue/debounce drain stops and leaves the queue to the next holder.
  - **Consumer-visible (default `drop` strategy):** a second message that arrives 30s or more into a long handler (for example a long Slack/Teams stream) now raises `LockError` (logged "Could not acquire lock on thread") instead of running concurrently. Consumers who want those messages handled should pick `queue`/`debounce`/`burst` or set `on_lock_conflict`.
  - **Channel-scoped isolation (security):** with a channel-scoped lock (`lock_scope="channel"`, e.g. Telegram), a queued or debounced message is now dispatched under **its own** `thread_id` instead of the lock holder's, and `context.skipped` only contains messages from that thread (`total_since_last_handler = len(skipped) + 1`). Before, a message from one topic could be answered in another topic, with the other topic's messages in `skipped`. As upstream, a pending message from another thread that is not the latest is dropped rather than surfaced.
  - **Debounce:** the queue now holds up to `max_queue_size` messages (was 1). Superseded messages reach the handler as `context.skipped`, and the handler now always gets a `MessageContext` (was `None`). A message that arrives while the debounced handler runs is debounced and dispatched afterwards under the same lock (it used to wait for the next webhook). The Python-only 20-iteration cap is gone; `max_lock_lifetime_ms` bounds the loop.
  - **Skipped mentions:** mention detection now also runs on every `context.skipped` message (`is_mention` is filled in place). If any skipped message mentions the bot and `on_mention` handlers exist, the batch routes to them even when the latest message does not mention the bot. With no mention handler it still falls through to `on_message` patterns. Today's `or` semantics for an adapter-reported `is_mention=False` are unchanged (#192).
  - `StateAdapter.extend_lock` now documents the token-compare contract (only extend a lock still held with that token; never create or resurrect one). The memory, Redis and Postgres backends already comply.
  - Log changes: `message-queued` / `message-debounce-reset` gain `queue_depth`; `message-expired`, `message-superseded` and `message-dequeued` log the message's own `thread_id` plus `lock_key`. The "Lock lost during ... aborting" warnings are replaced by "Stopping queue drain after lock ownership was lost" / "Stopping debounce loop after lock ownership was lost" and the heartbeat's own warnings.
- **Postgres state: expired `set_if_not_exists` claims are reclaimed; migration-owned schemas** (#240). Ports upstream `d88789c9` (vercel/chat#636, chat@4.35.0) and `ea025af7` (vercel/chat#913, chat@4.41.0).
  - **Fix (consumer-visible):** `PostgresStateAdapter.set_if_not_exists` used `ON CONFLICT DO NOTHING`, so an *expired* row blocked every new claim until a `get()` of that exact key happened to delete it. Dedupe keys and any lease built on `set_if_not_exists` (e.g. Telegram `update_id` claims) refused work they should accept. The query is now upstream's conditional upsert (`DO UPDATE ... WHERE expires_at IS NOT NULL AND expires_at <= now() RETURNING cache_key`), which reclaims an expired row atomically and never overwrites a live or permanent (no-TTL) one. Postgres-backed dedupe and leases now recover without cleanup. No schema change.
  - **New, opt-in:** keyword-only `auto_create_schema: bool = True` on `PostgresStateAdapter` and `create_postgres_state`. With `False`, `connect()` runs no DDL. It runs `SELECT 1`, then one read-only query that checks every table, the column privileges the adapter uses and the list/queue sequences. It raises `chat_sdk.StateSchemaError` naming the problem (the first table PostgreSQL cannot resolve, or every object whose grants are missing), so a wrong `search_path` or a forgotten grant fails at startup. The default is unchanged, and `auto_create_schema=None` also means `True` (upstream `autoCreateSchema ?? true`).
  - **Python-only:** when a `connect()` attempt fails, a pool the adapter created from a URL or env var is closed at once and the next attempt builds a fresh one. asyncpg opens its connections eagerly and `disconnect()` is a no-op until `connect()` succeeds, so a startup retry loop previously leaked a full pool (10 connections) per failed attempt. An injected pool is never closed.
  - **New public API:** `POSTGRES_SCHEMA_STATEMENTS` (the nine DDL statements, in execution order; from `chat_sdk.state` or `chat_sdk.state.postgres`) and `chat_sdk.StateSchemaError` (a `ChatError`). The README's new "PostgreSQL state" section documents the migration SQL and the grants; a test keeps that SQL identical to `POSTGRES_SCHEMA_STATEMENTS`.
  - Tests: the mock pool no longer reclaims expired rows under `DO NOTHING`, which is not what real PostgreSQL does. The old `test_succeeds_after_expired_key` passed against the mock while production failed. Expiry tests advance an injectable mock clock instead of sleeping. An opt-in live suite (ported from upstream `postgres.integration.test.ts`) runs when `POSTGRES_TEST_URL` is set.
- **Cards & modals: `Chart`, `Table` options, button tooltips, `Card` width, `DateInput` / `NumberInput`, `dispatch_action`** (#202). Ports the core slice of upstream `4717a384` (chat@4.34.0), `0153a39f` (chat@4.36.0), `4a0b5c0c` (chat@4.40.0), `84219537` and `ad904325` (chat@4.41.0). Additive only: with the new options unset, existing card and modal output is unchanged.
  - `Chart(title=, chart=)` (alias `chart`) builds a pie, bar, area or line chart element; new `ChartSegment`, `ChartDataPoint`, `ChartSeries`, `PieChartDefinition`, `SeriesChartDefinition` (`x_label` / `y_label`), `ChartDefinition` and `ChartElement` types. `chart_element_to_fallback_text()` renders the title plus the data as an ASCII table, with values formatted as JS `String()` does (`45.0` → `45`). Card fallback text now includes charts, so adapters that fall back to it (Slack until #212, Teams, Google Chat, Discord, GitHub, Linear, Twilio) post them as text; Messenger and WhatsApp drop chart children, as upstream does.
  - `Table()` gains `caption`, `page_size`, `widths`, `vertical_align` (`TableVerticalAlignment`), `grid_lines` and `grid_style` (`TableGridStyle`). `Button()` / `LinkButton()` gain `tooltip`; `Card()` gains `width` (`CardWidth`: `"default"` / `"full"`). Slack renders `caption` / `page_size` in #212; Teams renders `widths`, `vertical_align`, `grid_lines`, `grid_style`, `tooltip` and `width` in #220. Other adapters ignore them. Until #194, a `callback_url` button loses its `tooltip` when the URL is swapped for a token (no adapter renders `tooltip` before #220).
  - New modal children `DateInput()` / `NumberInput()` (aliases `date_input` / `number_input`), accepted by `filter_modal_children`. `Select()` / `RadioSelect()` gain `dispatch_action`. Falsy values (`initial_value=0`, `min=0`, `decimal=False`, `dispatch_action=False`, `grid_lines=False`) are kept. Until #212, a Slack modal containing `DateInput` / `NumberInput` raises `ValueError`.
  - JSX conversions of these props are not ported (no JSX runtime); see `docs/UPSTREAM_SYNC.md`.
- **GitHub: `GITHUB_BOT_USER_ID` env var and bot id learned from the first posted comment** (#233; port of the generic part of upstream `6750d59e`, vercel/chat#650, chat@4.33.0). The GitHub adapter spots its own comments by comparing `sender.id` with the bot user id. That id used to come only from `config["bot_user_id"]` or from best-effort auto-detection (`GET /user`, then `GET /app`). When detection failed, `is_me` never matched and the bot could reply to its own comments in a loop.
  - The id now resolves as `bot_user_id` config → `GITHUB_BOT_USER_ID` env var → auto-detection. If all three are unavailable, `post_message` learns it from the `user` of the comment it just created (issue comments and review-comment replies; `edit_message` does not). From then on, webhooks for the bot's own comments are skipped. A known id is never overwritten.
  - The learned id only helps after the bot's first post. When the token cannot call `GET /user` or `GET /app`, set `bot_user_id` or `GITHUB_BOT_USER_ID`.
  - `GitHubAdapter.bot_user_id` now checks `is not None`, so an explicit `bot_user_id=0` reads as `"0"` instead of `None`. Not breaking.
  - The Vercel Connect half of `6750d59e` (`installationToken`, `webhookVerifier`) is deferred to #189.
  - **Python-specific (divergence from upstream):** a non-empty `GITHUB_BOT_USER_ID` that is not a whole base-10 integer (e.g. `"12abc"`) is ignored with a warning. Upstream's `parseInt` would truncate it to `12`. See `docs/UPSTREAM_SYNC.md`.

### Google Chat: webhook JWT verification bound to configured identities (#222, security)

Ports upstream `270b1c25` (#518, chat@4.35.0), `7a192235` (#787, chat@4.37.0) and `c3b5a08e` (#797, chat@4.37.0). Before this change, Google Chat webhook verification checked only a Google signature and the `aud` claim. The audiences in play (project number, endpoint URL, Pub/Sub push URL) are not secrets, so that check did not identify the caller. Every transport is now bound to a configured identity:

- **Project-number tokens (direct webhooks)** are verified against the Chat service account's own X.509 certificates (`service_accounts/v1/metadata/x509/chat@system.gserviceaccount.com`, cached 1 hour) with issuer `chat@system.gserviceaccount.com`. **This makes project-number verification work.** The previous code checked these tokens against Google's OIDC key set, which never holds the Chat issuer's keys, so every genuine project-number webhook was rejected with 401. (We confirmed on 2026-09-29 that the two published key sets share no key ids. We did not capture a real token for this check.)
- **`endpoint_url` is now a direct-webhook verifier.** It covers Chat apps whose "Authentication audience" is "HTTP endpoint URL", which includes every Workspace Add-on Chat app. The token must be a Google OIDC ID token (`iss` of `accounts.google.com` or `https://accounts.google.com`) whose `aud` is exactly the configured URL, with `email_verified` exactly `true` and `email` equal to `chat@system.gserviceaccount.com` or the configured add-on identity. `endpoint_url` alone now satisfies the constructor's fail-closed check. When both verifiers are configured, the endpoint-URL token is tried first and the project number second.
- **Pub/Sub pushes** need `email_verified` to be `true` and `email` to equal the configured push identity.
- **Google's OIDC keys are fetched asynchronously and cached.** The old `PyJWKClient` did blocking network I/O on the event loop.

#### Breaking (Google Chat)

- **Pub/Sub deployments must set `pubsub_service_account_email`** (env `GOOGLE_CHAT_PUBSUB_SERVICE_ACCOUNT_EMAIL`) to the service account in the subscription's push auth settings. Without it, every Pub/Sub push is rejected with 401 and a warning.
- **Workspace Add-on Chat apps must set `workspace_add_on_service_account_email`** (env `GOOGLE_CHAT_WORKSPACE_ADDON_SERVICE_ACCOUNT_EMAIL`) to their own `service-{projectNumber}@gcp-sa-gsuiteaddons.iam.gserviceaccount.com` identity, and must set `endpoint_url`. Add-on tokens are compared exactly. Without the setting they are rejected with 401 and a warning. Standalone Chat apps are unaffected.
- **Button-click endpoint inference changed.** When `endpoint_url` is not configured, the adapter still infers a routing URL from `request.url`, but only from a *direct* webhook that passed verification (or ran with verification explicitly disabled). Before, the first request of any kind set it before verification ran, including Pub/Sub pushes and requests that were later rejected. The inferred value now lives in its own field (`_inferred_endpoint_url`) and never overwrites `endpoint_url`. The Python port never used the inferred URL as a verification audience, so audience checks are unchanged.
- **Setting `endpoint_url` now turns on direct-webhook verification, even when `disable_signature_verification` is set.** A configured verifier takes precedence over the opt-out (same branch order as upstream). Before, a deployment with `endpoint_url` set (for button routing), no project number and `disable_signature_verification=True` accepted direct webhooks unverified. Such deployments are common, because project-number verification never worked before this change. After upgrading, each direct webhook that does not carry an endpoint-URL-audience Chat token gets a 401. Fix it in one of three ways: set `google_chat_project_number` if the Chat app uses the "Project number" authentication audience; switch the app to the "HTTP endpoint URL" audience with exactly the configured URL; or unset `endpoint_url` and let button routing use the inferred URL. The constructor now logs a warning when `endpoint_url` and `disable_signature_verification` are both set.
- Upgrading also fixes project-number deployments, which were rejecting genuine webhooks (see above). No config change is needed for them.

#### Python-specific (divergence from upstream)

- The two new `GoogleChatAdapterConfig` fields are keyword-only (`field(kw_only=True)`). The dataclass is positional, so adding them positionally would shift existing callers' arguments.
- Verification uses PyJWT with a fixed 1-hour async key cache, not google-auth-library's `verifyIdToken` / `verifySignedJwtWithCertsAsync`. The time checks are ported by hand to match google-auth-library: `exp` and `iat` are required, with 300 s of clock skew, and a token whose `exp` is 24 hours or more in the future is rejected (PyJWT has no such limit). The issuer is compared exactly by hand on both paths, not through PyJWT's `issuer=`, because PyJWT 2.10.0 matched it as a substring (CVE-2024-53861).
- The constructor warning when `endpoint_url` and `disable_signature_verification` are both set is Python-only. Upstream has the same precedence but does not log it.

### Security

- **Linear: comment-thread and agent-session history are now bound to the thread's issue** (#231, security; ports vercel/chat#965 `d7aa75b1` and #974 `2d2b933a`, chat@4.41.1). Linear thread ids carry an issue id plus a comment id or agent session id, and `fetch_messages` trusted the second segment without checking it against the first, so a caller naming issue A could read comment or session history from issue B. Anything that authorizes by issue id was affected, for example conversation-scoped AI tools.
  - `linear:{issue}:c:{comment}` loads the root comment first, together with its `issueId`. It raises `ValidationError("linear", "Comment does not belong to this issue")` when that id is missing or different, before any replies query. Messages are built with the validated issue id.
  - `linear:{issue}:s:{session}` and `linear:{issue}:c:{comment}:s:{session}` raise `ValidationError("linear", "Agent session does not belong to this issue")` when the session's issue is missing or different, before the root comment or replies are read.
  - **Behavior change:** a session or comment whose issue can't be resolved now raises `ValidationError` instead of falling back to the thread's issue id. Before, a session with no issue fell back to the thread's issue, and one with no issue at all raised `AdapterError("... missing issueId")`. `ValidationError` is a subclass of `AdapterError`, so existing `except AdapterError` handlers still catch it. The error messages never include either issue id.
  - **Behavior change:** comment-thread fetches now respect `FetchOptions.direction`, as upstream always has. `forward` returns the first `limit` replies; `backward` (and the default) return the last `limit`. Before, they always returned the first `limit`.
- **Slack: installation-scoped caches, unresolved installs dropped, strict `response_url`** (#205, security). Ports the Slack parts of vercel/chat#877 (webhook tenant isolation), the cache part of #724 (Enterprise Grid), #876 (external request targets) and the Slack half of #779 (bounded URL parsing).
  - In multi-workspace mode, installation-owned cache entries were keyed globally, so data resolved with one workspace's token could be served to another. `RequestContext` now carries `installation_id`, set wherever a context is built from a resolved installation (HTTP and socket events, slash commands and interactive payloads). User profiles, the display-name reverse index, channel names, and unfurl metadata are keyed by it, and unfurl metadata is also keyed by channel. `user_change` invalidates the scoped entry. `_enrich_links` now takes `(links, channel_id, message_ts)`; messages returned by `fetch_message` / `fetch_messages` / channel history carry no `channel` field, so the channel is taken from their thread id and they keep cached unfurl metadata (upstream drops it; documented divergence).
  - Multi-workspace slash commands and interactive payloads (HTTP and socket mode) whose installation is missing or cannot be resolved are now acknowledged with an empty 200 (or a bare socket ack) and **not dispatched**. Before, they reached handlers with no token context.
  - `response_url` is trusted only over `https`, with no userinfo and no explicit port, on exactly `hooks.slack.com` or `hooks.slack-gov.com` (GovSlack is now accepted; other `*.slack.com` hosts are not). It is checked when an ephemeral message id is encoded and decoded, and again before the request. The legacy non-JSON ephemeral-id format is no longer decoded, and `edit_message` / `delete_message` raise `ValidationError("Invalid Slack ephemeral message ID")` for an undecodable `ephemeral:` id instead of passing it to `chat.update` / `chat.delete`. The SDK-free `send_slack_response_url` primitive uses the same check.
  - The bracketed-link fallback in message text is length-bounded (2048 chars), which keeps the scan linear on adversarial input.
  - **Consumer-visible:**
    - Multi-workspace apps no longer run slash-command or interactive handlers for unknown installations.
    - Cache key shapes change: `slack:user:{installation}:{user}`, `slack:user-by-name:{installation}:{name}`, `slack:channel:{installation}:{channel}`, and `slack:unfurls:{installation}:{channel}:{ts}`. The installation segment is omitted in single-workspace mode, but the unfurl key gains the channel there too. Expect a one-time cache miss after upgrading; nothing needs migrating.
    - `response_url` is limited to `hooks.slack.com` / `hooks.slack-gov.com`.
- **Webhook log hygiene: raw bodies and message content no longer reach DEBUG logs** (#187; ports upstream `fc7df9c4` / vercel/chat#500 and the logging parts of `f485255b` / vercel/chat#877). Several adapters logged raw webhook bodies, or previews of them, at DEBUG. In some cases this happened before signature or JWT verification, so unauthenticated input and message content could be copied into log sinks. Webhook handlers now log only request-shape metadata:
  - GitHub: `{bodyBytes, contentType, eventType, signaturePresent}`, under "GitHub webhook signature verification failed" or "GitHub webhook request verified", plus `jsonParseStatus: "error"` on invalid JSON.
  - GChat, Slack (after verification only) and the Teams bridge: `"… webhook received" {bodyLength}`.
  - Linear: the raw-body log is gone.
  - `Chat` "Incoming message": drops `author` and adds `is_bot`, matching upstream's key set.
- **Consumer-visible (DEBUG logs only; no routing, response or status change):**
  - The `"GitHub/GChat/Slack/Teams/Linear/WhatsApp webhook raw body"` messages are gone. GChat, Slack and Teams now emit `"… webhook received"` with `bodyLength`, which is a UTF-8 byte count.
  - GitHub's `"GitHub webhook event type"` is replaced by `"GitHub webhook request verified"`.
  - `bodyPreview` becomes `bodyBytes` on the GitHub, Linear and WhatsApp invalid-JSON errors.
  - "Incoming message" loses `author`.

  Anything that parses these log lines must be updated.

### Python-specific (divergence from upstream)

- **Lock heartbeat timing** (#190). The `max_lock_lifetime_ms` cap is measured with a monotonic clock (upstream: `Date.now()`), so a wall-clock step cannot end renewal early or extend it past the cap. Each extend is awaited before the next 10s sleep (upstream: `setInterval`, which skips a tick while an extend is in flight), and `stop()` waits for an in-flight extend without cancelling it. The heartbeat's last-known-held deadline is also a local monotonic deadline (taken before the `acquire_lock`/`extend_lock` request, plus the TTL) instead of `Lock.expires_at`: the Python Postgres backend stamps `expires_at` with the database clock (upstream state-pg uses the client clock), so a database clock running 20-30s behind the app used to make a fresh lock look lapsed and strand queued or debounced messages. `ConcurrencyConfig.max_lock_lifetime_ms` must be a non-negative `int` (`ValueError` at `Chat` init otherwise), and an unexpected crash of the renewal loop is logged at `error`. Before each queue-drain or debounce dispatch (after collecting the batch) the holder runs a token-checked `extend_lock` (upstream relies on the heartbeat's cached state, checked before collecting), so a worker that lost the lock to a forced takeover stops draining at once instead of up to 10s later; past `max_lock_lifetime_ms` that check is refused, so a busy drain stops at the cap (upstream: when the lock lapses, one TTL later) and leaves the queue for the next holder. A drain that finds the queue empty while a heartbeat extend is in flight waits for it and looks again, so a message enqueued meanwhile is not stranded. If ownership is lost while a batch is being dequeued, the batch is re-enqueued for the next holder (into free queue capacity only, never evicting newer messages) instead of dispatched, and a debounce loop that stops re-enqueues its superseded messages (upstream dispatches the former and drops the latter); logged as `messages-requeued`. Each drain step collects at most `max_queue_size` messages, so a flood of arrivals cannot keep a loop collecting forever. A heartbeat extend still stalled when the lock's known expiry passes is cancelled at release, so it cannot pin a backend connection during shutdown. `stop()` returns without yielding when no extend is in flight (so a message enqueued just as the drain finishes is not stranded) and waits for an in-flight extend only until the lock's known expiry (so a stalled backend call cannot hang handler cleanup or `Chat.shutdown()`). Recorded in `docs/UPSTREAM_SYNC.md`.
- **Linear comment-thread replies query** (#231). Upstream fetches replies through the root `comments(filter: {parent: {id: {eq}}})` connection. The port keeps its existing `comment(id) { children(first, last) }` selection, which returns the same replies with the same pagination. It is documented in `docs/UPSTREAM_SYNC.md`.
- **WhatsApp** drops its raw-body debug log and invalid-JSON `bodyPreview` (#187). Upstream 4.41.1 still logs both.
- **Message-content debug logs** (#187):
  - GChat "message event" logs `{space, textLength}`.
  - GChat "Pub/Sub parsed message" drops `text` and `author`.
  - The `Chat`, Slack and Discord slash-command debug logs log `textLength` instead of `text`.

  Upstream still logs this content. Both divergences are recorded in `docs/UPSTREAM_SYNC.md`.
- **Postgres state** (#240): `auto_create_schema=False` raises `StateSchemaError` (a `ChatError`), not a plain exception. The message matches upstream except that the hint names `auto_create_schema=True`. Concurrent `connect()` calls stay serialized on a lock, so a caller queued behind a failed attempt retries instead of sharing its error (upstream shares one in-flight promise). Both are recorded in `docs/UPSTREAM_SYNC.md`.

## 0.4.31.3

Python-only fixes on top of `4.31.0` (`UPSTREAM_PARITY` unchanged at `4.31.0`). Same content as the `0.4.31.2` tag, which never reached PyPI: the publish action's pinned twine rejected the `Metadata-Version 2.5` that uv's build backend now emits (fixed in #182), and the tag is immutable, so the release ships as 0.4.31.3.

- **Teams: Graph SSRF / token-leak guard hardening** (#178, security). `call_teams_graph_api` decided absolute-vs-relative URLs with a case-sensitive `startswith("http")`, so `HTTPS://evil.example/x`, `HtTpS://…` or the scheme-relative `//evil.example/x` fell into the relative-path branch, where `urljoin` still resolved to the attacker host and the Graph-scoped bearer token was attached without consulting `is_trusted_graph_url`. Routing now uses the same scheme-insensitive parse as the allowlist, so every absolute or scheme-relative target goes through the trust check. Regression tests cover the mixed-case and scheme-relative forms.
- **Tests:** the Teams skip-auth fixture survives the SDK's flag rename (#180). No runtime change.
- **Docs/licensing:** added `NOTICE` reproducing Vercel Chat's MIT copyright notice — this package is a derivative (port) of `vercel/chat`, and the notice now ships in the sdist and in the wheel's `dist-info/licenses/` (#179). No code changes.

## 0.4.31.1

Python-only fixes on top of `4.31.0` (`UPSTREAM_PARITY` unchanged at `4.31.0`). Two Slack adapter bug fixes, both divergences ahead of upstream (which shares the same gaps), documented in `docs/UPSTREAM_SYNC.md`.

- **Slack: empty-DM `thread_ts` fetch routing** (#138). DM roots encode an empty `thread_ts` (`slack:Dxxx:`); the fetch paths (`fetch_messages`/`fetch_message`) called `conversations.replies(ts='')`, which returns no replies and loses the DM root context. They now route an empty `thread_ts` to the existing channel-history path (`conversations.history`) instead — covering DM messages and the #137 DM block-action consumer uniformly. The non-empty (real thread) path is byte-identical.
- **Slack: `chat.startStream` `team_not_found` on Enterprise Grid** (#95). `chat.startStream` requires an explicit `team_id` for Grid orgs, but `stream()` started the stream without one (while non-streaming `chat.postMessage` worked). The streaming call now threads `team_id` (from the already-plumbed `recipient_team_id`, the per-workspace `T…` id) through to `chat.startStream`. Verified end-to-end against `slack_sdk` that the value reaches the `chat.startStream` API call; `append`/`stop` correctly do not need it. (Live Enterprise-Grid server-side acceptance is pending verification against a real Grid tenant.)
- **Docs:** marked Linear `"agent-sessions"` mode **experimental** in `LinearAdapterConfig` — its emit/fetch GraphQL is schema-hardened but unverified against a live Linear agent-session tenant (#151).

## 0.4.31

Synced to upstream `vercel/chat@4.31.0`. The mapped-core **test** files (`packages/chat/src/*.test.ts`) are byte-identical between the `chat@4.30.0` and `chat@4.31.0` tags, so the fidelity re-pin to `chat@4.31.0` is string-only (732/732 mapped-core tests still pass, 0 missing); the core **source** delta is the `LinkButton` stable-id field (below). The headline is the **Linear agent-sessions** mode, plus the **Teams SDK-free primitive subpaths**, the **Slack 4.31** changes, **Telegram rich messages**, and a Python-only opt-in **`ThinkingChunk`** stream type. Sets `UPSTREAM_PARITY = "4.31.0"`.

### Headline: Linear agent-sessions mode (issue #151)

Full port of upstream's Linear agent-sessions interaction model, delivered across five PRs (L1–L5). Because no official Linear Python SDK exists, every `@linear/sdk` call is reproduced as **raw GraphQL** over the existing `_graphql_query` helper and **schema-hardened field-by-field against Linear's published GraphQL schema** (`linear/packages/sdk/src/schema.graphql`).

- **L1 — types** (#168). `LinearAgentSessionThreadId`, the `mode: "agent-sessions" | "comments"` config (default `"comments"`), the `kind`-discriminated raw-message variants (`comment` / `agent_session_comment`), and the `AgentSessionEvent` webhook payload types.
- **L2 — thread-id** (#170). Anchored `linear:{issue}:c:{comment}:s:{session}` / `linear:{issue}:s:{session}` encode/decode with a strict decode order; existing thread-id forms stay byte-identical so cross-SDK state is preserved.
- **L3 — webhook parse + routing** (#171). The `AgentSessionEvent` branch with mutually-exclusive mode gating (agent-session events flow only in `agent-sessions` mode, comment events only in `comments` mode), `_parse_message_from_agent_session_event` (created/prompted actions + null-return/warn paths), app-ownership guard, and `get_user_name_from_profile_url`.
- **L4 — emit** (#172). `post_message` / `start_typing` / `stream` route through raw `agentActivityCreate` / `agentSessionUpdate` mutations (lowercase `AgentActivityType` enum, `content: JSONObject!`, `ephemeral` included only when set); streaming flushes markdown deltas as `response`/`thought` activities and maps `task_update`/`plan_update` chunks to `action`/`error` activities and session-plan updates.
- **L5 — fetch** (#173). `fetch_messages` dispatches agent-session threads to `_fetch_agent_session_messages` (raw `agentSession(id:)` + `comments(filter:{parent})` with forward/backward pagination); `edit_message`/`delete_message` raise append-only errors for session threads.

The schema-hardening caught two live-tenant-breaking selection bugs before they shipped (`AgentActivity` exposes the `agentSession` relation, not a scalar `agentSessionId`; `AgentSession` likewise has no scalar `issueId`). The mutations/queries are confirmed against the published schema but **not yet exercised against a live Linear agent-session tenant** — documented in `docs/UPSTREAM_SYNC.md`.

### Teams: SDK-free primitive subpaths (chat@4.31, commit `8c71411`)

New runtime-free Teams subpaths mirroring upstream's `@chat-adapter/teams/*` exports: `teams/api` (Bot Connector — token grant, post/update/delete message, typing, create conversation), `teams/graph` (Microsoft Graph — channels, messages, pagination), `teams/format` (Teams text/mention/HTML↔Markdown), `teams/webhook` (read/parse, continuation/user/attachment extraction, mention detection), `teams/cards` + `cards_input` (card → Adaptive Card + input parsing), and `teams/modals` (modal → Adaptive Card + dialog-submit parsing). Each network-facing primitive carries an SSRF/token-leak host gate: `call_teams_connector_api` (serviceUrl Bot Framework allowlist) and `call_teams_graph_api` (host pinned to `graph.microsoft.com`, also guarding followed `@odata.nextLink` cursors).

### Slack 4.31 (commit `f801985`, PR #155)

- **`@mention`-inside-URL fix.** Bare `@handle`s inside `http(s)` URLs (paths, query strings, fragments) are no longer rewritten into `<@handle>` mentions (which corrupted the link), via a URL-span exclusion pass.
- **`web_client_options`** config — forwarded to both the default and per-token `slack_sdk` `WebClient`s to tune the underlying HTTP client (timeout, `retry_handlers`, headers), with per-client header isolation. (Maps to slack_sdk kwargs rather than `@slack/web-api`'s axios options — documented divergence.)
- **Stable link-button `action_id`** — `LinkButton(id=…)` now flows to the Slack block `action_id` instead of always deriving it from the URL.

### Telegram: rich messages (commit `4662309`)

Character-for-character port of the new `rich.ts` (Telegram rich-message wire types → Markdown + plain text) as `telegram/rich.py`, plus the rich-message/media type family (`TelegramRichText`/`RichBlock`/`RichMessage`, animation/audio/location/video/voice), native `sendRichMessage`/`sendRichMessageDraft` threading through post/edit/stream with a rich→regular fallback, and a `/slash`-command router.

### Core: `LinkButton` stable id + opt-in `ThinkingChunk`

- **`LinkButton(id=…)`** (chat@4.31, commit `171657a`). Optional action identifier for platforms that report link clicks (matches the `Button`/`Select` `id` convention). The JSX-runtime half of the same commit has no Python equivalent (no JSX runtime) and is documented as such.
- **`ThinkingChunk`** (Python-only, opt-in, default-off; supersedes PR #39, landed in #169). A **separate** `ThinkingChunk(type="thinking", content=str)` stream-input type surfaces AI-SDK `reasoning`/`reasoning-delta` parts. **`StreamChunk` is not widened** — it stays byte-identical to upstream's three variants, so consumers referencing it are unaffected; `ThinkingChunk` is accepted only at the stream boundaries via the `StreamInput = str | StreamChunk | ThinkingChunk` alias. Emitted only when a caller opts in (`emit_thinking=True`); the default stream and persisted `Message` are byte-identical to upstream, so cross-SDK state stays compatible. Gives chinchill a first-class path to stream agent thinking to Slack/Teams without intercepting the model stream out-of-band.

### Not ported (documented)

- **`chat/adapters` static catalog** (new `./adapters` subpath). Upstream's SDK-free adapter/env-var metadata registry is addressed by npm package names and includes ~13 vendor-official adapters this SDK doesn't ship, so it isn't meaningfully portable verbatim; a Python-native equivalent would be a new feature with no current consumer need. Documented in `docs/UPSTREAM_SYNC.md`; deferred demand-driven.

## 0.4.30

Synced to upstream `vercel/chat@4.30.0`. The mapped core (`packages/chat/src`) is content-identical between the `chat@4.29.0` and `chat@4.30.0` upstream tags, so this wave is all adapter work: a **new Twilio adapter**, a **Telegram native-streaming** port, a **Slack primitives-subpath** wave, a batch of **WhatsApp / Slack / Google Chat** fixes, and the headline — the **Teams adapter migration to the official `microsoft-teams-apps` SDK** (issue #93, delivered across four PRs). Sets `UPSTREAM_PARITY = "4.30.0"`; CI fidelity re-pinned to `chat@4.30.0` (732/732 mapped-core tests still pass, 0 missing).

### New adapter: Twilio (SMS / MMS / Voice)

- **`chat_sdk.adapters.twilio`** (vercel/chat#558; PR #142). Twilio Programmable Messaging adapter (10th platform): inbound message webhooks with `X-Twilio-Signature` HMAC-SHA1 verification (`hmac.compare_digest`), outbound SMS/MMS through the Messages REST API (hand-rolled over an injectable transport — no official `twilio` SDK, mirroring upstream), 1:1 DM threads keyed `twilio:{sender}:{recipient}`, plus standalone `api` / `webhook` / `voice` helpers (TwiML builders, call + transcription parsing). New extra: `chat-sdk-python[twilio]`. Imports stay lazy so the package loads without `aiohttp` installed.

### Teams adapter: migration to the official `microsoft-teams-apps` SDK (issue #93)

The hand-rolled Bot Framework REST + JWT stack is replaced by the official Microsoft Teams Python SDK (`microsoft-teams-apps` ≥ 2.0.13, added to the `[teams]` extra), mirroring upstream `@chat-adapter/teams@4.30.0`. Landed as four PRs:

- **PR 1 — inbound + auth** (#143). New `adapters/teams/bridge.py`: a `BridgeHttpAdapter` implementing the SDK `HttpServerAdapter` protocol routes already-authenticated webhooks through the SDK `App`. JWT validation now runs through the SDK's `TokenValidator` (RS256 + audience + Bot Framework issuer via the live JWKS) in place of the hand-rolled `_verify_bot_framework_token` block. Graph reads stay hand-rolled (no `msgraph-sdk` / `[graph]` extra).
- **PR 2 — outbound** (#144). `post_message` / `start_typing` route through `App.send`; `edit_message` / `delete_message` route through `App.api.conversations.activities(...).update` / `.delete`. Per-thread service-URL routing retargets the SDK `ApiClient`'s service-url chain (validated against the SSRF allow-list). The camelCase wire dict is still returned as `RawMessage.raw`, preserving the public contract (attachment shape, file delivery, returned id).
- **PR 3 — native streaming** (#145). DM streaming uses the SDK's native `IStreamer` (`microsoft-teams-apps` `HttpStream`) via `app.activity_sender.create_stream(...)` and `stream.emit(...)` per chunk, replacing the hand-rolled Bot Framework streaming wire format. The SDK owns the streamType/streamSequence framing, the inter-flush throttle (~500ms, 429-safe), and 429 retry. Atomically unwinds the two transitional public-type divergences (`RawMessage.text`, `update_interval_ms`) that PR 3 made unnecessary.
- **PR 4 — release cut** (this entry). Version bump to `0.4.30`, fidelity re-pin to `chat@4.30.0`, docs, and the `@chat-adapter/teams@4.30.0` version-label normalization.

The residual adapter-level divergences (we keep the SDK as the auth + transport layer but route the authenticated activity ourselves through a lenient `CoreActivity`; the streamer is closed in our own `_handle_message_activity` `finally` because our bridge owns dispatch) are documented in `docs/UPSTREAM_SYNC.md`.

### Adapter ports — Telegram, Slack

- **Telegram: native DM draft streaming** (vercel/chat#340; PR #140). DMs stream via the `sendMessageDraft` Bot API method (the draft bubble updates in place, throttled to `update_interval_ms`, default 250ms), then a regular `sendMessage` persists the final text; non-DM threads return `None` before consuming any chunks so the SDK's post+edit fallback handles groups/channels. Adds a shared `with_telegram_markdown_fallback()` retry-without-`parse_mode` path wrapping `post_message` / `edit_message` / `send_document` / `send_attachment`.
- **Slack: webhook + primitives subpaths** (vercel/chat#538, #547, #548, #555, #559; PR #139). New runtime-free `chat_sdk.adapters.slack.webhook` (and `slack.api`) subpaths for lower-level Slack request verification, signed-body reading, and Events/slash/interactive payload parsing into typed dataclasses. The adapter now verifies through the shared `verify_slack_request` / `verify_slack_signature` primitives (the inline `_verify_signature` method is removed, matching upstream); the slack package `__init__` is now lazy (PEP 562) so importing a subpath does not pull in the full adapter runtime. The new `slack/api` primitives carry SSRF/token-leak guards (`send_slack_response_url` + `fetch_slack_file` host allowlists).

### Adapter fixes — WhatsApp, Slack, Google Chat

- **WhatsApp: typing-indicator support** (vercel/chat#320; PR #141). `start_typing` resolves the latest inbound message id from the `ThreadHistoryCache` and posts a `typing_indicator` payload (also marking the message read); Graph API default bumped v21.0 → v25.0; `_graph_api_request` and the typing-indicator failure path raise `AdapterError` instead of `RuntimeError`.
- **Slack / Google Chat: 4.30 rendering fixes** (vercel/chat#523, #553, #573; PR #141). Includes collapsing redundant autolink formatting for Google Chat email/`mailto:` links (port of upstream `177735a`).

#### Python-specific (divergence from upstream)

- **Twilio media-download SSRF guard.** `rehydrate_attachment` validates the rehydrated media URL (https + Twilio-owned host) inside the download closure before forwarding the account SID / auth token as HTTP Basic, where upstream `fetchTwilioMedia` GETs the URL blindly. Folded into the existing `rehydrate_attachment` URL allowlist non-parity row (Slack / Teams / Google Chat / **Twilio**); enforces `CLAUDE.md`'s SSRF rule. Regression: `tests/test_twilio_adapter.py::TestRehydrateAttachment::test_media_downloader_refuses_untrusted_hosts`.

### Pre-existing parity gaps closed (4.30 audit)

A pre-ship parity audit (the CI fidelity check covers only the mapped CORE `packages/chat` tests, never the adapters) surfaced 9 gaps present since 0.4.29 — the `chat@4.29.0` and `chat@4.30.0` tags are content-identical for the mapped core — and all but one (Linear agent sessions, deferred to 4.31) were closed in this wave, across four PRs:

- **Google Chat: clear `cardsV2` on edit-to-plain-text** (PR #148). BUG — editing a card message down to plain text left the old card stranded on the message; `edit_message` now sends an explicit empty `cardsV2` so the card is dropped (streaming finalization is the common trigger). Plus **`Select` / `RadioSelect` → `selectionInput` widgets**: these card elements now render as Google Chat `selectionInput` widgets and the selected option is read back from `formInputs`.
- **Teams: ChoiceSet auto-submit fan-out** (PR #149). BUG / contract break — an Adaptive Card `Action.Submit` carrying multiple input keys now fires one `process_action` per input key, so each `on_action(input_key)` handler runs, instead of a single `__auto_submit` dispatch that no handler matched. Plus **`list_threads` / `post_channel_message`** (were raising `ChatNotImplementedError`, now implemented) and **`api_url` / `TEAMS_API_URL`** for a custom Bot Framework endpoint (GCC-High / sovereign cloud).
- **`api_url` custom-endpoint config across Slack / Discord / GitHub / Linear** (PR #150). Custom base URLs for GovSlack / Enterprise Grid / GitHub Enterprise / self-hosted Linear; an empty string falls back to the default, matching upstream's truthy-spread default. Plus **GitHub public `get_installation_id()`**.
- **Core: root `chat_sdk` re-exports the deprecated `chat/ai` type aliases** (PR #147). `AiMessage`, `AiMessagePart`, `ToAiMessagesOptions`, … resolve from the package root again (they had moved to `chat_sdk.ai`); `chat_sdk.ai` stays the canonical home.

**Documented exceptions.** 0.4.30 matches `chat@4.30.0` *with documented exceptions*. The **Linear agent-sessions** surface (#151) is the largest single gap from the audit and is deferred to the 4.31 wave. `adapter-web`, plus the GitHub / Linear native-client (`octokit` / `linearClient`) and `message.subject` halves, remain documented Known Non-Parity in `docs/UPSTREAM_SYNC.md`. The 4.31 wave is tracked in #152.

## 0.4.29 (2026-06-12)

Synced to upstream `vercel/chat@4.29.0` (release commit `6581d31`, May 18 2026; upstream never tagged `chat@4.27.0`/`chat@4.28.0`). Headlines: **Meta Messenger adapter** (9th platform), **`chat/ai` tool factories** (`create_chat_tools`), **`callback_url` on buttons and modals**, **Transcripts API + `thread_history` rename**, **`burst` concurrency strategy**, a Slack feature wave (verifier precedence flip, external installation providers, native `markdown_text`, `web_client`), the upstream adapter-hardening security pass, and a Python floor bump to 3.12. Sets `UPSTREAM_PARITY = "4.29.0"`; CI fidelity re-pinned to `chat@4.29.0`.

### Upstream parity ports — core (`packages/chat`)

- **`callback_url` on buttons and modals** (vercel/chat#454). New `src/chat_sdk/callback_url.py` plus plumbing through cards, modals, chat, channel, and thread: card buttons and modals can carry a `callback_url` that the SDK POSTs to when the action/submit fires, alongside regular handlers. Serialized wire keys stay camelCase (`callbackUrl`) for cross-SDK state compatibility.
- **Transcripts API + per-thread cache rename to `thread_history`** (vercel/chat#448). `MessageHistoryCache` → `ThreadHistoryCache` (module `message_history` → `thread_history`) with back-compat shims at the old import path and config names (new name wins when both are set, matching upstream's `?? config.messageHistory` fallback). The persisted state key prefix `msg-history:` is **unchanged** (renaming would orphan existing data — upstream kept it too). New Transcripts surface for cross-platform per-user history; `ChatConfig.transcripts` requires `ChatConfig.identity` and raises on misconfiguration, matching upstream's guard.
- **`message.subject` + `fetch_subject` adapter hook** (vercel/chat#459, PR #131). `MessageSubject` dataclass, optional `BaseAdapter.fetch_subject(raw)` hook, lazily-resolved cached `Message.subject` accessor, adapter bound at the `Chat._dispatch_to_handlers` convergence point. The GitHub/Linear adapter halves are blocked on native-client exposure (see Known Non-Parity rows).
- **`burst` concurrency strategy** (vercel/chat#495, PR #114). Hybrid of `debounce` and `queue`: idle threads coalesce a burst window into one dispatch with earlier messages in `context.skipped`; busy threads drain like `queue`. Note: upstream's PR title says "queue-debounce" but the shipped strategy string is `"burst"` — the Python port matches the string (cross-SDK config parity).
- **`process_message` returns the handler task** (core slice of vercel/chat#444). Streaming callers can await full handler completion and observe handler exceptions; `wait_until` keeps swallowed-error semantics; fire-and-forget callers are unaffected. The `@chat-adapter/web` package that motivated #444 is browser-only and not ported.
- **`chat/ai` subpath** (vercel/chat#492, design #109; PRs #116 + #122). `chat_sdk.ai` is now a package: `ai/messages.py` (`to_ai_messages`, moved with deprecation shims) and `ai/tools.py` — `create_chat_tools(chat, preset=, require_approval=, overrides=)` returning the 17 upstream tool factories as `ChatTool` dataclasses (JSON-Schema `input_schema`, async `execute`, `needs_approval`), keyed by upstream's camelCase tool ids. No new runtime dependencies.

### New adapter: Messenger (Meta)

- **`chat_sdk.adapters.messenger`** (vercel/chat#461, design #110; PRs #118 + #124). Full Messenger Platform adapter: GET verification handshake + `X-Hub-Signature-256` HMAC verification, Graph API client with typed error mapping, text/Generic/Button-template sends with documented caps, buffered streaming (no edit API), postback/reaction/delivery/read handlers, attachment extraction with lazy `fetch_data`, local message cache backing `fetch_messages`. New extra: `chat-sdk-python[messenger]`. Capabilities matrix in the design issue; `get_user` tracked as #132.

### Upstream parity ports — adapters

- **Slack: `webhook_verifier` now takes precedence over `signing_secret`** (vercel/chat#468, PR #113). Reverses the 0.4.27 precedence after upstream reversed itself; see the Known Non-Parity row update. **Migration**: if you relied on `signing_secret` shadowing a configured `webhook_verifier`, remove one of the two.
- **Slack: native `markdown_text` for outgoing messages** (vercel/chat#440). Outgoing posts use Slack's native markdown rendering instead of the legacy Block Kit conversion (deferred from 0.4.27).
- **Slack: external installation providers for bot token management** (vercel/chat#467). Pluggable multi-workspace token source composing with the existing dynamic `bot_token` resolver (per-request ContextVar caching preserved).
- **Slack: `web_client` property** (vercel/chat#471/#476/#478, PR #127). Sync `slack_sdk.WebClient` bound to the request-context token; `client` retained as a one-release deprecated alias.
- **Teams: outbound file delivery via data-URI activity attachments** (PR #125, ports upstream `filesToAttachments`). Execution artifacts now reach Teams; `edit_message` delivery is a documented deliberate superset.
- **Telegram: `video_note` extraction + typed attachment uploads** (vercel/chat#457, #485; PR #119).
- **Discord: handle interactions in gateway-only mode** (vercel/chat#490).
- **Adapter hardening pass** (slices of upstream `9824d33`): Slack timing-safe socket-token comparison (PR #126), GitHub eager bot-user-ID auto-detection (PR #128), Linear OAuth tokens encrypted at rest with AES-256-GCM (PR #129), and Google Chat fail-closed webhook verification (PR #130 — **breaking** for gchat configs that previously constructed without any verification gate; set `google_chat_project_number`, `pubsub_audience`, or the explicit `disable_signature_verification=True` escape hatch).

### Documentation alignment

- **DM routing precedence docstrings** (vercel/chat#491, PR #121). `on_direct_message` and `Thread.subscribe` docstrings now state the DM > subscribed > mention > pattern precedence the runtime already implemented; regression test pins DM > pattern.
- **Streaming docstring refresh** (vercel/chat#463, PR #123). `StreamingPlanOptions.update_interval_ms` and `ThreadImpl._handle_stream` no longer imply a binary native-vs-fallback split.

### Python-only improvements

- **Slack `files_upload_v2` confirmation surfaced through `post()`** (PR #117; also shipped early as 0.4.27.1). `SentMessage.raw` carries `uploaded_file_ids`; history persistence nulls `raw` to avoid storage bloat/PII.
- **Slack: DM block-action responses no longer thread** (PR #137, supersedes #133). `_handle_block_actions` mirrors `_handle_message_event`'s DM handling so HITL button replies don't create phantom "1 reply" threads.

### Not ported / deferred (documented in docs/UPSTREAM_SYNC.md)

- **`@chat-adapter/web`** (vercel/chat#444) — browser-only; no Python runtime.
- **`@chat-adapter/tests` kit** (vercel/chat#470) — `chat_sdk.testing` already covers the surface; row added.
- **GitHub `octokit` / Linear `linear_client` getters** (vercel/chat#459/#478 halves) — both Python adapters are hand-rolled over `aiohttp` with no SDK object to expose; rows added with the revisit conditions (`githubkit` adoption / an official Linear Python SDK).
- **Teams migration to `microsoft-teams-apps`** (issue #93) — explicitly deferred to the 0.4.30 cycle; row added. The 3.12 floor prerequisite (#111) shipped in this release.

### Build / infra

- **Python floor bumped 3.10 → 3.12** (PR #111).
- **Fidelity**: CI re-pinned to `chat@4.29.0`; `MAPPING` updated for the 4.29 layout (`ai.test.ts` split into `ai/messages.test.ts` + `ai/index.test.ts`) and extended to the new core test files; converter-exact test renames in `tests/test_ai_tools.py`; two `chat.test.ts` subject-rehydration ports are `skipif`-gated on `BaseAdapter.fetch_subject` and activate automatically when PR #131 lands.
- **Next wave**: upstream `chat@4.30.0` (tagged 2026-06-01) is tracked in #135.

## 0.4.27.1 (2026-05-29)

Python-only point release on the 0.4.27 line (branched from `v0.4.27`; shipped via tag `v0.4.27.1` + merged PR #117). Backports the Slack `files_upload_v2` confirmation fix (PR #117) so `SentMessage.raw` carries `uploaded_file_ids`, plus the history-persistence `raw` null-out. Shipped ahead of 0.4.29 to unblock chinchill-api's delivery-confirmation gating.

## 0.4.29a2 (2026-05-28)

Python-only fix. No upstream version change.

### Fixes

- **Slack now surfaces `files_upload_v2` confirmation through `post()`** — `SlackAdapter._upload_files` already computed the list of Slack-confirmed file IDs but `post_message` discarded the return, and `ThreadImpl._create_sent_message` hardcoded `raw=None`, so the confirmation never reached `SentMessage.raw`. Slack was the only file-capable adapter to drop this; discord/telegram upload inline and expose the platform response naturally. `post_message` now augments `RawMessage.raw` with `"uploaded_file_ids"` on every return path that can carry files (file-only, card, table, text), and `ThreadImpl._create_sent_message` accepts and propagates the adapter's `raw` into `SentMessage.raw`. `None` means no upload occurred; an empty list signals Slack confirmed zero attachments. The `raw` payload is augmented, not replaced, so existing consumers are unaffected. Unblocks chinchill gating UX on actual delivery success. An upstream `vercel/chat` issue is filed in parallel for convergence.

### Test quality

- Added 4 tests: three in `tests/test_slack_api.py` (file-only, text+files, and text-only-no-augment paths) and one end-to-end `tests/test_thread_faithful.py` test verifying `post()` propagates `RawMessage.raw` to `SentMessage.raw`.

## 0.4.29a1 (2026-05-28)

Alpha sync starter for upstream `4.29.0` (`vercel/chat` release commit
`6581d31`, May 18 2026). Upstream skipped tagging `chat@4.27.0` and
`chat@4.28.0` (only `@chat-adapter/shared@4.27.0` and `@chat-adapter/shared@4.28.0`
got tags); `chat@4.29.0` is the next real tag and the target of this
wave. **No feature ports in this release** — parity-bookkeeping bump
that sets `UPSTREAM_PARITY = "4.29.0"` and lays out the porting plan
below.

Each substantive commit lands as its own PR (matching the cadence used
during the 4.27 sync: #83, #85, #86, #87, #88, #89, #90, #91, #92, #99,
#101, #103, #104, #105). Tracking issue: #98.

### Behavior changes (Slack)

- **`webhook_verifier` now takes precedence over `signing_secret`** (vercel/chat#468, commit `0f0c203`). When a Slack adapter is constructed with both `webhook_verifier` and `signing_secret` (or with `webhook_verifier` while `SLACK_SIGNING_SECRET` is set in the env), the verifier wins and the signing-secret path is dropped entirely. This **reverses** the precedence the Python port shipped in 0.4.27 (PR #87), which preferred `signing_secret` to match upstream's intent at that time. Upstream reversed itself in vercel/chat#468 (`chat@4.29.0`) so an env-configured `SLACK_SIGNING_SECRET` could not silently shadow a verifier the caller wired up; this port now follows. **Migration:** if you relied on a configured `signing_secret` overriding `webhook_verifier`, drop the `webhook_verifier` from your `SlackAdapterConfig` (or, if you wired the verifier in deliberately, your signing-secret path is now correctly inert and you can remove it). The built-in HMAC + 5-minute timestamp tolerance only applies on the signing-secret path; verifier implementers remain responsible for replay protection (`slack/types.py` SECURITY contract).

### Sync scope (37 substantive upstream commits between `f55378a..chat@4.29.0`)

#### Core (`packages/chat`)

- [ ] **`chat/ai` subpath for AI SDK utilities** (vercel/chat#492). New
  public API surface: `createChatTools`, `toAiMessages` for LLM/agent
  integration. Vercel AI SDK is TS-only; the Python equivalent needs a
  design call (see open question). Likely the biggest single PR in the
  wave.
- [ ] **`queue-debounce` concurrency strategy** (vercel/chat#495). New
  strategy beyond the existing `drop` / `queue` / `debounce` /
  `concurrent`.
- [ ] **Transcripts API + per-thread cache rename to `threadHistory`**
  (vercel/chat#448). New API surface; the cache rename has chinchill-api
  blast radius.
- [ ] **`callbackUrl` on buttons and modals** (vercel/chat#454).
- [ ] **`message.subject` + adapter client access** (vercel/chat#459).

#### All adapters

- [ ] **`adapter.client` rename → `adapter.octokit` / `adapter.linearClient`
  / `adapter.webClient`** (vercel/chat#478). Public API rename across
  all adapters; deprecation shims advisable for one release.
- [x] **`private` → `protected` for subclassing** (vercel/chat#475).
  Already addressed — Python convention uses `_underscore` (de-facto
  protected); audit confirmed no `__name_mangled` internals across all
  8 adapters. No work needed.

#### Slack (`packages/adapter-slack`)

- [ ] **Native `markdown_text` for outgoing messages** (vercel/chat#440).
  Was listed as "deferred" in 0.4.27.
- [ ] **External installation provider for bot token management**
  (vercel/chat#467). Multi-workspace token mgmt extension.
- [ ] **Flip `webhook_verifier > signing_secret` precedence**
  (vercel/chat#468). Our 0.4.27 explicitly went the other direction
  ("match upstream" intent, with comment). Upstream has since reversed
  itself in #468. The comment on `adapter.py:385` is now stale; flip
  precedence + refresh comment + update tests.
- [ ] **Expose direct `WebClient` via `adapter.client`** (vercel/chat#471,
  reverted in #472, reapplied in #476). Pairs with the #478 rename.

#### Discord (`packages/adapter-discord`)

- [ ] **Handle interactions in gateway-only mode** (vercel/chat#490).
  Related to issue #57 (Discord native Gateway). Decide if Gateway
  support lands in this wave or stays on a separate track.

#### Telegram (`packages/adapter-telegram`)

- [ ] **Typed attachment uploads** (vercel/chat#485). Bundled with
  related Telegram polish.
- [ ] **`video_note` (round video messages) in `extractAttachments`**
  (vercel/chat#457).
- [x] **MarkdownV2 entity safety trim to streaming chunks**
  (vercel/chat#446). Already addressed in our 0.4.27 — the
  `_trim_to_markdown_v2_safe_boundary` / `_find_unclosed_link_dest_open_bracket`
  / `_slice_to_utf16_units` helpers from PR #89 cover this. No work
  needed.

#### Teams (`packages/adapter-teams`)

- [ ] **Migrate to `microsoft-teams-apps` SDK** (issue #93). Replaces our
  hand-rolled Bot Framework REST streaming with `ctx.stream.emit()`.
  Requires Python 3.12 floor bump. Headline Teams change for this wave
  (or 0.4.29.1 if the migration slips).

#### New packages

- [ ] **`@chat-adapter/messenger`** (vercel/chat#461). Brand-new Meta
  Messenger Platform adapter. Similar scope to porting WhatsApp or
  Telegram from scratch — own file tree under `src/chat_sdk/adapters/messenger/`,
  full webhook / message / attachment surface, ~1,500 LOC estimate.
- [⏭️] **`@chat-adapter/web`** (vercel/chat#444). Vue + Svelte browser
  UI for chat-sdk bots. **Out of scope** — no browser runtime in
  chat-sdk-python.
- [ ] **`@chat-adapter/tests` test kit** (vercel/chat#470). Test
  utilities for adapter authors. We already have an adapter-test
  pattern; evaluate whether to mirror.

#### Out of scope for this Python port

- **`@chat-adapter/web`** as above.
- **Documentation site changes** — `apps/docs/`, MDX refreshes, etc.
- **Vercel-specific release/CI automation** (#465, #466, #511, #512,
  #520).

### Open questions (resolve before implementation)

1. **`chat/ai` subpath — Python design.** Detailed scoping in design
   issue (see below). Recommended shape: shared SDK-agnostic core in
   `chat_sdk/ai/tools.py` + thin per-SDK adapters (Anthropic, OpenAI)
   via optional extras (`chat-sdk-python[ai-anthropic]`,
   `chat-sdk-python[ai-openai]`). 17 tool factories + the existing
   `to_ai_messages` (already in `chat_sdk/ai.py`). ~7 engineer-days.
   Three sub-questions: approval-flow contract, hand-written JSON
   Schema vs Pydantic v2, ship OpenAI extras in first cut?
2. **Messenger adapter (vercel/chat#461) — Python port.** Detailed
   scoping in design issue (see below). Mirrors WhatsApp adapter
   conventions; ~1,500 LOC prod + ~2,500 LOC tests; 2 PRs (scaffolding
   then adapter); ~5–6 days. Three sub-questions: init-failure
   semantics, postback `value` passthrough, signature-failure HTTP
   status code (upstream returns 403, our other adapters return 401).
3. **Cadence**: ship as one wave (4.27 → 4.29) or split into 0.4.28
   then 0.4.29?
4. **Python floor bump to 3.12** (required for Teams SDK migration —
   issue #93). Confirm chinchill-api compatibility before committing.
5. **Discord Gateway scope**: ship Gateway support in this wave
   (issue #57) or keep gateway-only mode fix (vercel/chat#490)
   isolated?
6. **`adapter.client` rename**: ship deprecation shim for one release,
   or hard cutover?

### Workflow

1. This alpha PR establishes the sync. CI on this draft is intentionally
   not invoked (lint.yml is gated on `!github.event.pull_request.draft`).
2. Each item above lands as its own PR. Each port PR:
   - Updates the relevant `MAPPING` / fidelity coverage and removes its
     entries from `scripts/fidelity_baseline.json` if previously baselined.
   - Bumps lint.yml's pinned upstream ref to `chat@4.29.0` (the new tag)
     once the first feature port lands.
   - Adds an entry under the next CHANGELOG heading (`0.4.29a2`,
     `0.4.29a3`, …).
3. Once all items are ported (or explicitly documented as divergence in
   `docs/UPSTREAM_SYNC.md`), the final PR cuts `0.4.29` and switches CI
   back to strict fidelity at the upstream tag.

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
