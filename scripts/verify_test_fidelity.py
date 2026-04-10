#!/usr/bin/env python3
"""Verify Python tests are faithful 1:1 translations of TypeScript tests.

For each TS test file, extracts every it("...") test name, converts to
snake_case, and checks that a corresponding def test_...() exists in the
Python translation.

Usage:
    python scripts/verify_test_fidelity.py [--fix]

With --fix: appends stub test functions for any missing translations.
"""

import os
import re
import sys
from pathlib import Path

TS_ROOT = os.environ.get("TS_ROOT", "/tmp/vercel-chat")
PY_ROOT = os.environ.get("PY_ROOT", str(Path(__file__).parent.parent))

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


def main() -> int:
    fix_mode = "--fix" in sys.argv
    total_missing = 0
    total_matched = 0
    total_ts = 0
    total_absorbers = 0

    print("=" * 70)
    print("TEST FIDELITY REPORT")
    print("=" * 70)

    for ts_rel, py_rel in MAPPING.items():
        ts_path = os.path.join(TS_ROOT, ts_rel)
        if not os.path.exists(ts_path):
            print(f"\n{ts_rel} — SKIPPED (file not found)")
            continue

        ts_tests = extract_ts_tests(ts_path)
        missing, extra, matched = check_fidelity(ts_rel, py_rel)
        py_path = os.path.join(PY_ROOT, py_rel)
        absorbers = count_absorbers(py_path)

        total_ts += len(ts_tests)
        total_matched += matched
        total_missing += len(missing)
        total_absorbers += absorbers

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
                print(f"    MISSING: [{describe}] {ts_name}")
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

    if total_missing > 0:
        print("\nRun with --fix to generate stubs for missing tests.")
        return 1
    print("\nAll TS tests have Python equivalents.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
