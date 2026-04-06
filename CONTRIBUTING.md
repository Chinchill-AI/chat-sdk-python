# Contributing to chat-sdk-python

Thanks for your interest in contributing! This guide covers the essentials.

## Dev Environment Setup

```bash
# Clone and install (requires Python 3.11+ and uv)
git clone https://github.com/Chinchill-AI/chat-sdk-python.git
cd chat-sdk-python
uv sync --group dev
```

## Running Tests

```bash
uv run pytest tests/               # all tests
uv run pytest tests/ -x            # stop on first failure
uv run pytest tests/unit/          # unit tests only
```

## Code Quality

```bash
uv run ruff check src/ tests/      # lint
uv run ruff format src/ tests/     # auto-format
```

All PRs must pass `ruff check` with zero errors.

## Adding a New Adapter

1. Create `src/chat_sdk/adapters/<platform>/` with at minimum:
   - `adapter.py` -- the adapter class implementing the Adapter protocol
   - `__init__.py` -- public exports and a `create_<platform>_adapter()` factory
2. Follow the patterns in existing adapters (Slack and Teams are good references).
3. Add an optional-dependency group in `pyproject.toml`.
4. Add tests under `tests/unit/adapters/<platform>/`.

## Pull Request Expectations

- **Tests required.** Every bugfix or feature needs at least one test.
- **Ruff clean.** `uv run ruff check src/ tests/` must pass with no errors.
- **Small, focused PRs** are easier to review than large ones.
- **Descriptive commit messages.** Explain *why*, not just *what*.

## Issues and PRs

- Check existing issues before opening a new one.
- Reference the issue number in your PR description (e.g., `Fixes #42`).
- For large changes, open an issue first to discuss the approach.

## License

By contributing you agree that your contributions will be licensed under the MIT License.
