# Claude Code Quick Reference -- chat-sdk-python

## What is this?
Python port of Vercel Chat SDK. Multi-platform async chat framework.

## Key Commands
- `uv sync --group dev` -- install dependencies
- `uv run pytest tests/ -q` -- run tests
- `uv run ruff check src/` -- lint
- `uv run ruff format src/` -- format

## Architecture
- `src/chat_sdk/chat.py` -- Main Chat orchestrator (handlers, routing, concurrency)
- `src/chat_sdk/thread.py` -- Thread (streaming, pagination, subscriptions)
- `src/chat_sdk/channel.py` -- Channel (thread listing, metadata)
- `src/chat_sdk/types.py` -- All types (Message, Author, Adapter protocol)
- `src/chat_sdk/adapters/` -- 8 platform adapters
- `src/chat_sdk/shared/` -- Markdown parser, format converter, streaming renderer
- `src/chat_sdk/state/` -- Memory, Redis, Postgres backends
- `tests/` -- 3,267 tests

## Principles

1. **Every test must fail when the code is wrong.** No `assert True` stubs, no
   bare truthiness checks when specific values are available, no MagicMock where
   AsyncMock is needed. If a test can't catch a regression, it's not a test.
2. **Every async call must be awaited.** Unawaited coroutines silently return
   truthy objects. Use AsyncMock (not MagicMock) in tests to surface these.
3. **No two tests should verify the same thing.** Duplicates inflate counts
   without catching more bugs.

## Port Rules (TS → Python)

These are specific patterns that broke during the port. The principles above
explain *why*; these explain *what to watch for*.

- `datetime.utcnow()` → `datetime.now(tz=UTC)` (deprecated, naive)
- `asyncio.ensure_future` → `loop.create_task()` (deprecated)
- Raw dicts to `process_*` → typed dataclasses (ActionEvent, etc.)
- camelCase dispatch keys → snake_case
- `random.choices` for tokens → `secrets.token_hex`
- Optional deps at module level → lazy import
- `==` for signatures → `hmac.compare_digest`
- `or` for empty-string-valid fields → `is not None`
- Validate external URLs before requests (SSRF)
- Check `extend_lock` return value in loops

## Adding a New Adapter
See docs/ARCHITECTURE.md and CONTRIBUTING.md.

## Upstream Sync
See docs/UPSTREAM_SYNC.md for TS->Python translation patterns.

## Known Limitations
- Markdown parser handles common cases but is not full CommonMark
- StreamingMarkdownRenderer's _remend is simplified vs the npm `remend` library
- No setext headings, no footnotes, no HTML nodes in the parser

## Test Quality

**CI runs `scripts/audit_test_quality.py` before tests.** It catches phantoms,
async mock bugs, and cross-file duplicates. PRs that introduce hard failures
will not pass CI.

**Fidelity check** (`scripts/verify_test_fidelity.py`) verifies every TS
`it("...")` has a matching Python `def test_*()`. Name match ≠ faithful port —
the audit script catches the quality side.
