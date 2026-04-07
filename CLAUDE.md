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
- `tests/` -- 2,477+ tests

## Critical Rules
1. **Never use `datetime.utcnow()`** -- use `datetime.now(tz=timezone.utc)`
2. **Never use `asyncio.ensure_future`** -- use `asyncio.get_running_loop().create_task()`
3. **Never pass raw dicts to `self._chat.process_*`** -- use typed dataclasses (ActionEvent, ReactionEvent, etc.)
4. **Never use camelCase keys in dispatch dicts** -- always snake_case
5. **Never use `random.choices` for security tokens** -- use `secrets.token_hex`
6. **Never import optional deps at module level** -- lazy import inside functions
7. **Always use `hmac.compare_digest` for signature verification** -- never `==`
8. **Always use `is not None` for empty-string-valid fields** -- never `or`
9. **Always validate external URLs before HTTP requests** (SSRF prevention)
10. **Always check `extend_lock` return value** in processing loops

## Adding a New Adapter
See docs/ARCHITECTURE.md and CONTRIBUTING.md.

## Upstream Sync
See docs/UPSTREAM_SYNC.md for TS->Python translation patterns.

## Known Limitations
- Markdown parser handles common cases but is not full CommonMark
- StreamingMarkdownRenderer's _remend is simplified vs the npm `remend` library
- No setext headings, no footnotes, no HTML nodes in the parser

## Test Fidelity Verification

After modifying or adding tests, run:
```bash
python3 scripts/verify_test_fidelity.py
```
This verifies every TS `it("...")` test has a matching Python `def test_...()`.
The script must show `0 missing` before committing test changes.

When porting a new TS test file:
1. Add the mapping to `scripts/verify_test_fidelity.py` MAPPING dict
2. Run with `--fix` to generate stubs
3. Translate each stub by reading the TS test body line-by-line
4. Verify with the script before committing
