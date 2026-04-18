# Claude Code Quick Reference -- chat-sdk-python

## What is this?
Python port of [Vercel Chat SDK](https://github.com/vercel/chat) (v4.26.0). Multi-platform async chat framework.

## Key Commands
```bash
uv sync --group dev           # install dependencies
uv run pytest tests/ -q       # run tests
uv run ruff check src/ tests/ scripts/  # lint
uv run ruff format src/ tests/ scripts/ # format

# Full validation — run before declaring any task done
uv run ruff check src/ tests/ scripts/ && \
uv run ruff format --check src/ tests/ scripts/ && \
uv run python scripts/audit_test_quality.py && \
uv run python scripts/verify_test_fidelity.py && \
uv run pytest tests/ --tb=short -q
```

## Version Mapping
Our version embeds the upstream Vercel Chat version: `0.{upstream_major}.{upstream_minor}[.patch]`
- `0.4.25` = synced to upstream `4.25.0`
- `0.4.25.1` = Python-only fix on top of `4.25.0`
- `0.4.26` = synced to upstream `4.26.0`
- `0.4.26a1` = alpha while porting upstream `4.26.0`
- `UPSTREAM_PARITY` constant in `__init__.py` = programmatic access

## Architecture
- `src/chat_sdk/chat.py` -- Chat orchestrator (handlers, routing, concurrency)
- `src/chat_sdk/thread.py` -- Thread (streaming, pagination, subscriptions)
- `src/chat_sdk/channel.py` -- Channel (thread listing, metadata)
- `src/chat_sdk/plan.py` -- Plan (PostableObject for structured task lists)
- `src/chat_sdk/types.py` -- All types (Message, Author, Adapter protocol)
- `src/chat_sdk/adapters/` -- 8 platform adapters
- `src/chat_sdk/shared/` -- Markdown parser, format converter, streaming renderer
- `src/chat_sdk/state/` -- Memory, Redis, Postgres backends
- `tests/` -- 3,400+ tests

### Thread ID Format
All thread IDs follow: `{adapter}:{channel}:{thread}`
- Slack: `slack:C123ABC:1234567890.123456`
- Teams: `teams:{base64(conversationId)}:{base64(serviceUrl)}`
- Google Chat: `gchat:spaces/ABC123:{base64(threadName)}`
- Discord: `discord:{guildId}:{channelId}[:{threadId}]`
- GitHub: `github:owner/repo:42` (PR) or `github:owner/repo:issue:42`

### Message Handling Flow
1. Platform sends webhook → adapter verifies + parses
2. Adapter calls `chat.process_message()` (or `process_action`, etc.)
3. Chat acquires lock, deduplicates, then routes:
   - Subscribed thread → `on_subscribed_message` handlers
   - @mention → `on_mention` handlers
   - DM → `on_direct_message` handlers
   - Pattern match → `on_message` handlers

## Principles

1. **Every test must fail when the code is wrong.** No `assert True` stubs, no
   bare truthiness checks when specific values are available, no MagicMock where
   AsyncMock is needed. If a test can't catch a regression, it's not a test.
2. **Every async call must be awaited.** Unawaited coroutines silently return
   truthy objects. Use AsyncMock (not MagicMock) in tests to surface these.
3. **No two tests should verify the same thing.** Duplicates inflate counts
   without catching more bugs.

## Self-Review Discipline

Review bots (Codex, CoderRabbit) outperformed agent self-review ~3× on recent
PRs — not because they're magic, but because they run adversarial checks
consistently. Run these yourself *before* declaring code ready. They target
the specific blindspots that have cost us silent bugs.

1. **Adversarial input sweep.** For every regex, substitution, or tokenization
   pass you add, enumerate ~5 inputs that could break it:
   - empty input, single-char input
   - input containing the marker/sentinel your code produces (forgery)
   - input containing each delimiter your pattern uses
   - the pattern appearing in unintended contexts (code spans, quoted strings)
   - very long input, odd unicode, PUA code points

2. **Emit/parse symmetry.** If you add `X → Y` (emit), verify your `Y → X`
   (parse) accepts every `X` the emit might produce. Every URL scheme, every
   label shape, every edge case (empty / relative / None). Asymmetry between
   emit and parse is a common source of silent data loss.

3. **Pass-interaction check.** When inserting a new transformation pass into
   a pipeline (markdown parsing, stripping, rendering), walk through every
   existing pass: does yours break any? do any break yours? does ORDER
   matter? Example: a link-strip inserted after code-stripping corrupts code
   content containing link syntax.

4. **Unforgeable sentinels.** Any placeholder token that later code maps
   back to structured data must carry a per-call nonce
   (`secrets.token_hex(n)`). Fixed tokens like `\ue000LINK0\ue000` can be
   forged by user input. Also tolerate out-of-range / unrecognized
   sentinels without raising — fall back to literal text.

5. **Divergence budget.** `docs/UPSTREAM_SYNC.md` sets a max of **2
   divergences per sync**. Enforce this against yourself. A 3rd divergence
   = stop and ask whether this is still a sync or becoming a fork. Document
   every divergence in the non-parity table, add a breadcrumb at the code
   site, write a regression test that would fail if someone "fixes" the
   divergence back to upstream.

6. **The Codex pre-ship question.** Before declaring ready, ask yourself:
   "What would Codex find in this code right now?" Be adversarial. If the
   honest answer is "probably X," test X first. Don't wait for a bot to
   file it.

## Port Rules (TS → Python)

See docs/UPSTREAM_SYNC.md for the full 15-hazard guide. Key patterns:

- `x or default` → `x if x is not None else default` (truthiness trap)
- `datetime.utcnow()` → `datetime.now(tz=timezone.utc)` (deprecated, naive)
- Raw dicts to `process_*` → typed dataclasses (ActionEvent, etc.)
- camelCase dispatch keys → snake_case internally, camelCase at serialization boundary
- `random.choices` for tokens → `secrets.token_hex`
- Optional deps at module level → lazy import inside methods
- `==` for signatures → `hmac.compare_digest`
- Validate external URLs before requests (SSRF)
- `chat.activate()` > `register_singleton()` > error (3-level resolver)

## Upstream Sync

See docs/UPSTREAM_SYNC.md for the full sync procedure, porting hazards, review
checklist, and known non-parity list.

## Known Limitations
- Markdown parser handles common cases but is not full CommonMark
- StreamingMarkdownRenderer's _remend is simplified vs the npm `remend` library
- No setext headings, no footnotes, no HTML nodes in the parser

## Test Quality

**CI runs `scripts/audit_test_quality.py` before tests.** It catches phantoms,
async mock bugs, and cross-file duplicates. PRs that introduce hard failures
will not pass CI.

**Fidelity check** (`scripts/verify_test_fidelity.py`) verifies every TS
`it("...")` has a matching Python `def test_*()`. Must show 0 missing before
committing test changes.
