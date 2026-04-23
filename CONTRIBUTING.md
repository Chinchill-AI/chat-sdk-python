# Contributing to chat-sdk-python

Thanks for your interest in contributing! This guide covers the essentials.

## Dev Environment Setup

```bash
# Clone and install (requires Python 3.10+ and uv)
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

## Release Procedure

### Version scheme

`0.{upstream_major}.{upstream_minor}[.patch]` — our version embeds the upstream
Vercel Chat version. See [UPSTREAM_SYNC.md](docs/UPSTREAM_SYNC.md#version-mapping).

- `0.4.25` = synced to upstream `4.25.0`
- `0.4.25.1` = Python-only changes (fixes, and additive features during
  alpha) between upstream sync points
- `0.4.26a1` = alpha while porting upstream `4.26.0`

> **Additive changes in `.patch` bumps are OK during alpha**. The package
> is marked `Development Status :: 3 - Alpha` and the `0.x.y` prefix signals
> pre-1.0 per semver convention, so new public APIs can land in `.patch`
> bumps without a version-scheme violation. Once we hit `1.0`, `.patch`
> should be fixes-only.
>
> **Upstream patch releases**: Vercel Chat has historically gone straight to
> minor bumps, but if upstream ships a patch (e.g. `4.25.1`) we sync it by
> bumping to the next minor (`0.4.26`). We don't reuse the `.patch` slot for
> upstream patches — it's reserved for Python-only changes so the two can't
> collide.

### Steps

1. **Full validation** (must all pass):
   ```bash
   uv run ruff check src/ tests/ scripts/
   uv run ruff format --check src/ tests/ scripts/
   uv run python scripts/audit_test_quality.py
   # verify_test_fidelity.py needs the upstream TS repo at $TS_ROOT (default
   # /tmp/vercel-chat). Without it, the script silently skips checks and exits
   # 0, so releases can ship unverified. Clone once:
   #   git clone https://github.com/vercel/chat.git /tmp/vercel-chat
   uv run python scripts/verify_test_fidelity.py
   uv run pytest tests/ --tb=short -q
   ```

2. **Update version** in all locations:
   - `pyproject.toml` → `version = "0.4.26"`
   - `README.md` → status line
   - `CLAUDE.md` → version reference
   - `src/chat_sdk/__init__.py` → `UPSTREAM_PARITY = "4.26.0"` (for upstream syncs)
   - `CHANGELOG.md` → new entry with public-facing release notes

3. **Changelog guidelines**:
   - Lead with what users need to do (upgrading section, breaking changes)
   - Group by impact (new features, bug fixes, internals)
   - Don't list internal engineering details (phantom absorber counts, etc.)
   - Link to upstream release for sync releases

4. **Create PR**, get CI green, merge.

5. **Create GitHub release**:
   ```bash
   gh release create v0.4.26 --target main --title "v0.4.26 — Synced to Vercel Chat 4.26.0"
   ```
   - Tag format: `v{version}` (e.g., `v0.4.26`)
   - Use the pre-release flag for alpha versions (`v0.4.26a1` → `--prerelease`)
   - This triggers the `publish.yml` workflow → PyPI

6. **Verify on PyPI**: `pip install chat-sdk=={version}`

7. **Cleanup**: mark yanked/superseded GitHub releases as deprecated in their
   description — don't delete them (deleting removes history and breaks any
   external links that reference the tag).

### What NOT to do

- Don't publish without CI green on all 4 Python versions (3.10-3.13)
- Don't skip the fidelity check for test changes
- Don't use alpha tags for *final* sync releases — alpha tags (`0.4.26a1`) are
  only for work-in-progress ports while syncing a new upstream version
- Don't amend published releases — create a patch instead

## License

By contributing you agree that your contributions will be licensed under the MIT License.

## Test Translation Process

When porting tests from the TypeScript SDK:

1. **Add the mapping** to `scripts/verify_test_fidelity.py`
2. **Generate stubs**: `python3 scripts/verify_test_fidelity.py --fix`
3. **Translate each stub** by reading the TS `it("...")` block line-by-line
4. **Verify names match**: `python3 scripts/verify_test_fidelity.py` must show 0 missing
5. **Run tests**: `uv run pytest tests/ -q`

Test function names MUST be derivable from the TS `it("...")` description.
Do NOT write new tests — translate the EXISTING TS tests.
