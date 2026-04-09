#!/usr/bin/env python3
"""Automated test quality audit.

Catches problems that pytest and coverage cannot:
- Phantom absorbers (assert True stubs)
- Cross-file exact duplicate tests
- MagicMock used for known async methods

Exit code 1 if any hard failures found. Warnings are advisory.

Usage:
    python scripts/audit_test_quality.py
"""

import ast
import collections
import os
import sys

TEST_DIR = os.path.join(os.path.dirname(__file__), os.pardir, "tests")

# Async methods that must use AsyncMock, not MagicMock
KNOWN_ASYNC_METHODS = frozenset({
    "get", "set", "delete", "set_if_not_exists", "append_to_list", "get_list",
    "subscribe", "unsubscribe", "is_subscribed",
    "acquire_lock", "release_lock", "extend_lock", "force_release_lock",
    "enqueue", "dequeue", "queue_depth", "connect", "disconnect",
    "post_message", "edit_message", "delete_message",
    "add_reaction", "remove_reaction", "start_typing",
    "fetch_messages", "fetch_thread", "fetch_message",
    "fetch_channel_info", "fetch_channel_messages",
    "list_threads", "open_dm", "open_modal", "post_channel_message",
})

# JSX-specific tests that legitimately use assert True
JSX_ABSORBER_KEYWORDS = {"jsx", "cardelement"}


def find_test_files():
    for root, _dirs, files in os.walk(TEST_DIR):
        for fname in sorted(files):
            if fname.startswith("test_") and fname.endswith(".py"):
                yield os.path.join(root, fname)


def check_phantoms(test_files):
    """Find tests whose only statement is `assert True`."""
    issues = []
    for fpath in test_files:
        tree = ast.parse(open(fpath).read())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("test_"):
                continue
            stmts = [
                s for s in node.body
                if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))
            ]
            if (
                len(stmts) == 1
                and isinstance(stmts[0], ast.Assert)
                and isinstance(stmts[0].test, ast.Constant)
                and stmts[0].test.value is True
            ):
                # Allow JSX-specific absorbers
                if any(kw in node.name.lower() for kw in JSX_ABSORBER_KEYWORDS):
                    continue
                issues.append((fpath, node.lineno, node.name))
    return issues


def check_cross_file_duplicates(test_files):
    """Find tests with identical bodies in different files."""
    bodies = collections.defaultdict(list)
    for fpath in test_files:
        source = open(fpath).read()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("test_"):
                continue
            body_lines = source.split("\n")[node.lineno - 1 : node.end_lineno]
            body = "\n".join(body_lines).strip()
            if len(body) > 80:  # Skip trivially short tests
                bodies[body].append(f"{fpath}:{node.lineno} {node.name}")

    dupes = []
    for _body, locations in bodies.items():
        files = set(loc.split(":")[0] for loc in locations)
        if len(files) >= 2:
            dupes.append(locations)
    return dupes


def check_magicmock_for_async(test_files):
    """Find MagicMock assigned to known async method names."""
    issues = []
    for fpath in test_files:
        source = open(fpath).read()
        for i, line in enumerate(source.split("\n"), 1):
            if "MagicMock" not in line or "AsyncMock" in line:
                continue
            # Pattern: something.async_method = MagicMock(...)
            for method in KNOWN_ASYNC_METHODS:
                if f".{method} = MagicMock" in line or f".{method}=MagicMock" in line:
                    issues.append((fpath, i, method, line.strip()))
    return issues


def main() -> int:
    test_files = list(find_test_files())
    hard_failures = 0

    # --- Phantoms ---
    phantoms = check_phantoms(test_files)
    if phantoms:
        print(f"FAIL: {len(phantoms)} phantom absorber(s) (assert True with no real test)")
        for fpath, line, name in phantoms:
            print(f"  {fpath}:{line} {name}")
        hard_failures += len(phantoms)
    else:
        print("OK: No phantom absorbers")

    # --- Cross-file duplicates ---
    dupes = check_cross_file_duplicates(test_files)
    total_dupe_tests = sum(len(locs) for locs in dupes)
    if dupes:
        print(f"\nWARN: {len(dupes)} cross-file duplicate group(s) ({total_dupe_tests} tests)")
        for locs in dupes[:5]:
            for loc in locs[:2]:
                print(f"  {loc}")
            if len(locs) > 2:
                print(f"  ... +{len(locs) - 2} more")
            print()
        if len(dupes) > 5:
            print(f"  ... +{len(dupes) - 5} more groups")
    else:
        print("OK: No cross-file duplicate tests")

    # --- MagicMock for async ---
    mock_issues = check_magicmock_for_async(test_files)
    if mock_issues:
        print(f"\nFAIL: {len(mock_issues)} MagicMock used for async method(s)")
        for fpath, line, method, code in mock_issues:
            print(f"  {fpath}:{line} .{method}: {code}")
        hard_failures += len(mock_issues)
    else:
        print("OK: No MagicMock for async methods")

    # --- Summary ---
    print(f"\n{'=' * 60}")
    print(f"Files scanned: {len(test_files)}")
    print(f"Hard failures: {hard_failures}")
    print(f"Warnings: {total_dupe_tests} duplicate tests in {len(dupes)} groups")

    return 1 if hard_failures > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
