#!/usr/bin/env python3
"""Verify Python tests are faithful 1:1 translations of TypeScript tests.

For each TS test file, extracts every it("...") test name, converts to
snake_case, and checks that a corresponding def test_...() exists in the
Python translation.

Usage:
    python scripts/verify_test_fidelity.py --strict    # CI path: fail on any missing
    python scripts/verify_test_fidelity.py             # baseline mode (local opt-in)
    python scripts/verify_test_fidelity.py --fix       # append stubs for missing
    python scripts/verify_test_fidelity.py --update-baseline  # rewrite baseline

``--strict`` is the current CI contract (see ``.github/workflows/lint.yml``):
the baseline is ignored and any missing translation — or a missing upstream
checkout — fails the build. This repo ships at strict fidelity for mapped
core files (0 missing) against ``chat@4.26.0``. The ``MAPPING`` dict below
is the authoritative scope list; it currently covers 8 of the 17
``packages/chat/src/*.test.ts`` files (extending it is tracked as a
follow-up).

Baseline mode (the default without ``--strict``) is retained for local
workflows where a few ports land in flight: it succeeds iff the set of
missing tests is a subset of ``scripts/fidelity_baseline.json``. Tests that
are in the baseline but now pass are reported as fixed; new misses outside
the baseline fail. Regenerate via ``--update-baseline`` after documenting
intentional divergence in ``docs/UPSTREAM_SYNC.md``.
"""

import json
import os
import re
import sys
from pathlib import Path

TS_ROOT = os.environ.get("TS_ROOT", "/tmp/vercel-chat")
PY_ROOT = os.environ.get("PY_ROOT", str(Path(__file__).parent.parent))
BASELINE_PATH = Path(__file__).parent / "fidelity_baseline.json"

# Mapping: TS test file -> Python test file
MAPPING = {
    "packages/chat/src/chat.test.ts": "tests/test_chat_faithful.py",
    "packages/chat/src/thread.test.ts": "tests/test_thread_faithful.py",
    "packages/chat/src/channel.test.ts": "tests/test_channel_faithful.py",
    "packages/chat/src/markdown.test.ts": "tests/test_markdown_faithful.py",
    "packages/chat/src/streaming-markdown.test.ts": "tests/test_streaming_markdown.py",
    "packages/chat/src/serialization.test.ts": "tests/test_serialization.py",
    "packages/chat/src/ai.test.ts": "tests/test_ai.py",
    "packages/chat/src/from-full-stream.test.ts": "tests/test_from_full_stream.py",
}


def ts_name_to_python(ts_name: str) -> str:
    """Convert a TS it("should do X") name to test_should_do_x.

    Returns empty string for names that reduce to nothing after
    stripping non-alphanumeric characters (e.g. "\\n").
    """
    name = ts_name.lower()
    name = re.sub(r"[^a-z0-9\s]", "", name)
    name = re.sub(r"\s+", "_", name.strip())
    name = re.sub(r"_+", "_", name)
    if not name:
        return ""
    return f"test_{name}"


def extract_ts_tests(ts_path: str) -> list[tuple[str, str, str]]:
    """Extract (describe, it_name, python_name) from a TS test file."""
    with open(ts_path) as f:
        content = f.read()

    tests = []
    current_describe = ""

    for line in content.split("\n"):
        desc_match = re.search(r'describe\("([^"]+)"', line)
        if desc_match:
            current_describe = desc_match.group(1)

        it_match = re.search(r'\bit\("([^"]+)"', line)
        if it_match:
            ts_name = it_match.group(1)
            py_name = ts_name_to_python(ts_name)
            if py_name:  # skip names that reduce to empty (e.g. "\\n")
                tests.append((current_describe, ts_name, py_name))

    return tests


def extract_py_tests(py_path: str) -> list[str]:
    """Extract all test function names from a Python file (with duplicates)."""
    if not os.path.exists(py_path):
        return []
    with open(py_path) as f:
        content = f.read()
    return re.findall(r"def (test_\w+)", content)


def fuzzy_match(py_name, py_tests):
    """Try to match a derived Python test name against existing tests.

    Uses word-overlap matching: extracts significant words (>2 chars) from
    the TS-derived name and requires at least 60% of them (minimum 2) to
    appear in the candidate Python test name.
    """
    if py_name in py_tests:
        return py_name

    words = [w for w in py_name.replace("test_", "").split("_") if len(w) > 2][:6]
    if not words:
        return None
    threshold = max(2, int(len(words) * 0.6))

    best_match = None
    best_score = 0
    for existing in py_tests:
        score = sum(1 for w in words if w in existing)
        if score >= threshold and score > best_score:
            best_score = score
            best_match = existing
    return best_match


def check_fidelity(ts_rel: str, py_rel: str) -> tuple[list, list, int]:
    """Returns (missing, extra, matched)."""
    from collections import Counter

    ts_path = os.path.join(TS_ROOT, ts_rel)
    py_path = os.path.join(PY_ROOT, py_rel)

    if not os.path.exists(ts_path):
        return [], [], 0

    ts_tests = extract_ts_tests(ts_path)
    py_tests = extract_py_tests(py_path)
    # Use Counter as a multiset so duplicate names in different classes both count
    remaining_py = Counter(py_tests)

    missing = []
    matched = 0

    def consume(name: str) -> bool:
        if remaining_py.get(name, 0) > 0:
            remaining_py[name] -= 1
            if remaining_py[name] == 0:
                del remaining_py[name]
            return True
        return False

    # Pass 1: exact matches first (prevents fuzzy from stealing exact names)
    unmatched_ts: list[tuple[str, str, str]] = []
    for describe, ts_name, py_name in ts_tests:
        if consume(py_name):
            matched += 1
        else:
            unmatched_ts.append((describe, ts_name, py_name))

    # Pass 2: fuzzy matches for remainder
    remaining_set = set(remaining_py.keys())
    for describe, ts_name, py_name in unmatched_ts:
        m = fuzzy_match(py_name, remaining_set)
        if m and consume(m):
            matched += 1
            if remaining_py.get(m, 0) == 0:
                remaining_set.discard(m)
        else:
            missing.append((describe, ts_name, py_name))

    extra = sorted(remaining_py.keys())
    return missing, extra, matched


def generate_stubs(ts_rel, missing):
    """Generate Python test stubs for missing translations."""
    lines = [
        "",
        "",
        f"# ===== STUBS: {len(missing)} tests need faithful translation =====",
        f"# Source: {ts_rel}",
        "# Each stub must be translated line-by-line from the TS it() block.",
        "# Do NOT write new tests — translate the EXISTING TS test.",
    ]
    current_class = ""

    for describe, ts_name, py_name in missing:
        class_name = "Test" + re.sub(r"[^a-zA-Z0-9]", "", describe.title().replace(" ", ""))
        if class_name != current_class:
            current_class = class_name
            lines.append(f"\n\nclass {class_name}Stubs:")
            lines.append(f'    """Stubs for: {describe}"""')

        lines.append("")
        lines.append(f"    async def {py_name}(self):")
        lines.append(f'        # TS: it("{ts_name}")')
        lines.append(f'        raise NotImplementedError("Translate from {ts_rel}")')

    return "\n".join(lines)


def count_absorbers(py_path: str) -> int:
    """Count tests whose body is only `assert True` (phantom absorbers)."""
    if not os.path.exists(py_path):
        return 0
    import ast

    with open(py_path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test_"):
            continue
        stmts = [s for s in node.body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
        if (
            len(stmts) == 1
            and isinstance(stmts[0], ast.Assert)
            and isinstance(stmts[0].test, ast.Constant)
            and stmts[0].test.value is True
        ):
            count += 1
    return count


def _current_parity_tag() -> str | None:
    """Return the baseline-format parity tag (``chat@X.Y.Z``) for the current repo.

    Reads ``UPSTREAM_PARITY`` from ``src/chat_sdk/__init__.py`` without
    importing the package (avoids pulling optional runtime deps during a
    script run). Returns None if the constant can't be located.
    """
    init_path = Path(__file__).parent.parent / "src" / "chat_sdk" / "__init__.py"
    if not init_path.exists():
        return None
    with open(init_path) as f:
        content = f.read()
    m = re.search(r'^UPSTREAM_PARITY\s*=\s*"([^"]+)"', content, re.MULTILINE)
    if not m:
        return None
    return f"chat@{m.group(1)}"


def load_baseline(path: Path) -> dict[str, set[tuple[str, str]]]:
    """Load fidelity baseline. Missing file returns empty baseline.

    Exits with code 1 when the baseline's ``ts_parity`` disagrees with the
    current ``UPSTREAM_PARITY`` constant — a stale baseline could otherwise
    silently mask upstream drift after a version bump.
    """
    if not path.exists():
        return {}
    with open(path) as f:
        data = json.load(f)
    baseline_parity = data.get("ts_parity")
    current_parity = _current_parity_tag()
    if baseline_parity and current_parity and baseline_parity != current_parity:
        print(
            f"\nbaseline parity mismatch: {path.name} was generated for "
            f"upstream {baseline_parity}, but current parity is "
            f"{current_parity} — re-run with `--update-baseline` after "
            f"confirming the diff.",
            file=sys.stderr,
        )
        sys.exit(1)
    out: dict[str, set[tuple[str, str]]] = {}
    for ts_rel, entries in data.get("missing", {}).items():
        out[ts_rel] = {(e[0], e[1]) for e in entries}
    return out


_DEFAULT_BASELINE_COMMENT = (
    "Ratchet-down baseline for scripts/verify_test_fidelity.py. "
    "Each entry is a [describe_block, ts_it_name] pair that is known "
    "to be missing a Python translation. CI runs --strict (see "
    ".github/workflows/lint.yml) and ignores this file; baseline "
    "mode is a local-dev opt-in that accepts any subset of this "
    "list as missing and fails on new misses outside it. To remove "
    "entries: port the TS test to its Python counterpart, then "
    "regenerate this file with --update-baseline."
)


def write_baseline(path: Path, all_missing: dict[str, list], total_ts: int) -> None:
    """Persist the current set of missing tests as the new baseline.

    If ``path`` already exists and has a ``_comment`` field, that curated
    comment is preserved so hand-written context (e.g. scope qualifiers,
    shipping-posture notes) isn't silently overwritten on every
    ``--update-baseline`` run. Only fresh files get the default boilerplate.
    """
    existing_comment: str | None = None
    if path.exists():
        try:
            with open(path) as f:
                existing = json.load(f)
            if isinstance(existing.get("_comment"), str):
                existing_comment = existing["_comment"]
        except (OSError, json.JSONDecodeError):
            existing_comment = None

    payload = {
        "_comment": existing_comment if existing_comment is not None else _DEFAULT_BASELINE_COMMENT,
        "ts_parity": "chat@4.26.0",
        "total_ts_tests": total_ts,
        "total_missing": sum(len(v) for v in all_missing.values()),
        "missing": {
            ts_rel: [[d, t] for d, t, _p in sorted(entries, key=lambda e: (e[0], e[1]))]
            for ts_rel, entries in sorted(all_missing.items())
            if entries
        },
    }
    with open(path, "w") as f:
        json.dump(payload, f, indent=2, sort_keys=False)
        f.write("\n")


def main() -> int:
    fix_mode = "--fix" in sys.argv
    strict_mode = "--strict" in sys.argv
    update_baseline = "--update-baseline" in sys.argv

    if strict_mode and update_baseline:
        print(
            "error: --strict and --update-baseline are mutually exclusive.\n"
            "  --strict says 'no missing allowed'; --update-baseline says "
            "'snapshot whatever is missing into the allowlist'. Pick one.",
            file=sys.stderr,
        )
        return 2

    baseline = {} if (strict_mode or update_baseline) else load_baseline(BASELINE_PATH)

    total_missing = 0
    total_matched = 0
    total_ts = 0
    total_absorbers = 0
    all_missing: dict[str, list] = {}
    new_misses: dict[str, list[tuple[str, str]]] = {}
    fixed: dict[str, list[tuple[str, str]]] = {}
    missing_ts_files: list[str] = []

    print("=" * 70)
    print("TEST FIDELITY REPORT")
    if strict_mode:
        print("  mode: --strict (baseline ignored)")
    elif update_baseline:
        print("  mode: --update-baseline (rewriting baseline)")
    else:
        print(f"  mode: baseline ({BASELINE_PATH.name})")
    print("=" * 70)

    for ts_rel, py_rel in MAPPING.items():
        ts_path = os.path.join(TS_ROOT, ts_rel)
        if not os.path.exists(ts_path):
            print(f"\n{ts_rel} — MISSING (upstream TS file not found at {ts_path})")
            missing_ts_files.append(ts_path)
            continue

        ts_tests = extract_ts_tests(ts_path)
        missing, extra, matched = check_fidelity(ts_rel, py_rel)
        py_path = os.path.join(PY_ROOT, py_rel)
        absorbers = count_absorbers(py_path)

        total_ts += len(ts_tests)
        total_matched += matched
        total_missing += len(missing)
        total_absorbers += absorbers
        all_missing[ts_rel] = missing

        current_missing_keys = {(d, t) for d, t, _p in missing}
        baseline_keys = baseline.get(ts_rel, set())
        file_new = sorted(current_missing_keys - baseline_keys)
        file_fixed = sorted(baseline_keys - current_missing_keys)
        if file_new:
            new_misses[ts_rel] = file_new
        if file_fixed:
            fixed[ts_rel] = file_fixed

        absorber_note = f" ({absorbers} absorbers)" if absorbers else ""
        status = "OK" if not missing else f"GAPS ({len(missing)})"
        print(f"\n{ts_rel}")
        print(f"  -> {py_rel}")
        print(
            f"  TS: {len(ts_tests)} | Matched: {matched}{absorber_note}"
            f" | Missing: {len(missing)} | Extra: {len(extra)} | {status}"
        )

        if missing:
            for describe, ts_name, _py_name in missing[:5]:
                marker = "NEW" if (describe, ts_name) in set(file_new) else "baselined"
                print(f"    MISSING ({marker}): [{describe}] {ts_name}")
            if len(missing) > 5:
                print(f"    ... and {len(missing) - 5} more")

        if fix_mode and missing:
            py_path = os.path.join(PY_ROOT, py_rel)
            stubs = generate_stubs(ts_rel, missing)

            if os.path.exists(py_path):
                with open(py_path, "a") as f:
                    f.write(stubs)
                print(f"  -> Appended {len(missing)} stubs to {py_rel}")
            else:
                with open(py_path, "w") as f:
                    f.write(f'"""Faithful translation of {ts_rel}"""\n\nimport pytest\n')
                    f.write(stubs)
                print(f"  -> Created {py_rel} with {len(missing)} stubs")

    real_total = total_matched - total_absorbers
    pct = total_matched * 100 // max(total_ts, 1)
    print(f"\n{'=' * 70}")
    if total_absorbers:
        print(
            f"TOTAL: {total_matched}/{total_ts} matched ({pct}%), {total_missing} missing, {total_absorbers} absorbers"
        )
        print(f"  Real tests: {real_total} | Absorbers: {total_absorbers}")
    else:
        print(f"TOTAL: {total_matched}/{total_ts} matched ({pct}%), {total_missing} missing")

    # Infra guard: if any mapped TS file is missing, we cannot verify fidelity.
    # Do NOT treat this as success — a failed upstream clone would otherwise
    # silently pass CI. Fail loudly before any downstream success branches.
    if missing_ts_files:
        print(
            f"\nupstream checkout missing — cannot verify fidelity. "
            f"{len(missing_ts_files)} mapped TS file(s) not found under TS_ROOT={TS_ROOT!r}:"
        )
        for path in missing_ts_files:
            print(f"  - {path}")
        print(
            "\nClone the upstream repo at the pinned parity tag, e.g.:\n"
            "  git clone --depth 1 --branch chat@4.26.0 "
            "https://github.com/vercel/chat.git /tmp/vercel-chat\n"
            "then re-run with TS_ROOT=/tmp/vercel-chat."
        )
        return 1

    if update_baseline:
        write_baseline(BASELINE_PATH, all_missing, total_ts)
        print(f"\nBaseline written to {BASELINE_PATH}")
        print(f"  {total_missing} missing tests baselined across {sum(1 for v in all_missing.values() if v)} files")
        return 0

    if total_missing == 0:
        print("\nAll TS tests have Python equivalents.")
        if any(baseline.values()):
            print("Baseline is stale — run with --update-baseline to clear it.")
        return 0

    if strict_mode:
        print(f"\n{total_missing} missing (strict mode — baseline ignored).")
        print("Run with --fix to generate stubs for missing tests.")
        return 1

    if new_misses:
        new_count = sum(len(v) for v in new_misses.values())
        print(f"\n{new_count} NEW miss(es) outside the baseline:")
        for ts_rel, entries in new_misses.items():
            for describe, ts_name in entries:
                print(f"  - {ts_rel} :: [{describe}] {ts_name}")
        print("\nOptions:")
        print("  1. Port the missing TS test(s) to the matching Python file")
        print("  2. If intentional divergence, document in docs/UPSTREAM_SYNC.md")
        print("     and re-baseline with --update-baseline")
        print("\nRun with --fix to generate Python stubs for missing tests.")
        return 1

    if fixed:
        fixed_count = sum(len(v) for v in fixed.values())
        print(f"\n✓ {fixed_count} test(s) fixed since baseline (no longer missing):")
        for _ts_rel, entries in fixed.items():
            for describe, ts_name in entries[:5]:
                print(f"    - [{describe}] {ts_name}")
            if len(entries) > 5:
                print(f"    ... and {len(entries) - 5} more")
        print("\nRun with --update-baseline to tighten the baseline.")

    baseline_total = sum(len(v) for v in baseline.values())
    print(f"\n{total_missing}/{baseline_total} baseline miss(es) still present — no new drift.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
