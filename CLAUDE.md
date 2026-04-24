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

Before declaring any change ready, run the adversarial checks in
[docs/SELF_REVIEW.md](docs/SELF_REVIEW.md). In short: input sweeps,
emit/parse symmetry, pass-interaction, unforgeable sentinels, divergence
budget, rebind/state coherence, and the pre-ship "what would an
adversarial reviewer find?" question. Apply these especially to novel
logic, new regex/substitution passes, and anything that lands as a
divergence from upstream.

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
`it("...")` has a matching Python `def test_*()`, pinned to `chat@4.26.0`.
Default mode is baseline-enforced: CI fails on any NEW miss not listed in
`scripts/fidelity_baseline.json`. Run `--update-baseline` after porting a
missing test (or documenting an intentional skip in `UPSTREAM_SYNC.md`). Use
`--strict` to verify the final 0-missing target locally.
