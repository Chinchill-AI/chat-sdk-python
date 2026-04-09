"""Scan for async iteration traps.

Detects:
1. `async for` on something that might not be an async iterable
2. `for` where `async for` should be used
3. `await` on non-coroutines or missing `await` patterns
"""

import ast
import os
import sys

SRC_DIR = "/tmp/chat-sdk-python/src/chat_sdk"

findings = []


class AsyncIterationVisitor(ast.NodeVisitor):
    def __init__(self, filepath, source_lines):
        self.filepath = filepath
        self.source_lines = source_lines
        self.in_async_func = False
        self.async_func_stack = []

    def _line(self, lineno):
        if 0 < lineno <= len(self.source_lines):
            return self.source_lines[lineno - 1].rstrip()
        return ""

    def visit_AsyncFunctionDef(self, node):
        self.async_func_stack.append(True)
        old = self.in_async_func
        self.in_async_func = True
        self.generic_visit(node)
        self.in_async_func = old
        self.async_func_stack.pop()

    def visit_FunctionDef(self, node):
        self.async_func_stack.append(False)
        old = self.in_async_func
        self.in_async_func = False
        self.generic_visit(node)
        self.in_async_func = old
        self.async_func_stack.pop()

    def visit_AsyncFor(self, node):
        """Check async for usage."""
        # async for on a regular iterable is a TypeError
        # We can't always tell statically, but flag suspicious patterns
        iter_node = node.iter
        if isinstance(iter_node, ast.List) or isinstance(iter_node, ast.Tuple):
            findings.append(
                {
                    "file": self.filepath,
                    "line": node.lineno,
                    "severity": "HIGH",
                    "category": "ASYNC-ITER: async-for-on-sync",
                    "note": "`async for` on a literal list/tuple -- this will fail at runtime",
                    "code": self._line(node.lineno),
                }
            )
        elif isinstance(iter_node, ast.Call):
            func_name = ""
            if isinstance(iter_node.func, ast.Name):
                func_name = iter_node.func.id
            elif isinstance(iter_node.func, ast.Attribute):
                func_name = iter_node.func.attr

            # Sync builtins used with async for
            sync_builtins = [
                "range",
                "enumerate",
                "zip",
                "map",
                "filter",
                "sorted",
                "reversed",
                "list",
                "set",
                "dict",
                "tuple",
                "iter",
            ]
            if func_name in sync_builtins:
                findings.append(
                    {
                        "file": self.filepath,
                        "line": node.lineno,
                        "severity": "HIGH",
                        "category": "ASYNC-ITER: async-for-on-sync-builtin",
                        "note": f"`async for` on `{func_name}()` -- sync iterable, not async iterable",
                        "code": self._line(node.lineno),
                    }
                )

        self.generic_visit(node)

    def visit_For(self, node):
        """Check if `for` is used where `async for` should be."""
        if self.in_async_func:
            iter_node = node.iter
            if isinstance(iter_node, ast.Call):
                func_name = ""
                if isinstance(iter_node.func, ast.Name):
                    func_name = iter_node.func.id
                elif isinstance(iter_node.func, ast.Attribute):
                    func_name = iter_node.func.attr

                # Async functions being iterated with sync for
                async_hints = [
                    "aiter",
                    "async_iter",
                    "arange",
                    "stream",
                    "astream",
                ]
                if func_name.lower() in async_hints:
                    findings.append(
                        {
                            "file": self.filepath,
                            "line": node.lineno,
                            "severity": "HIGH",
                            "category": "ASYNC-ITER: sync-for-on-async",
                            "note": f"`for` used on `{func_name}()` which appears to be async -- use `async for`",
                            "code": self._line(node.lineno),
                        }
                    )

            # Check if iterating over an await expression (should already be resolved)
            if isinstance(iter_node, ast.Await):
                # This is fine: `for x in await some_async_func()`
                pass

        self.generic_visit(node)

    def visit_Await(self, node):
        """Detect await on things that aren't coroutines."""
        # await on a non-coroutine will fail
        # We can detect some obvious cases
        if isinstance(node.value, ast.Constant):
            findings.append(
                {
                    "file": self.filepath,
                    "line": node.lineno,
                    "severity": "HIGH",
                    "category": "ASYNC-ITER: await-on-literal",
                    "note": "`await` on a literal value -- not a coroutine",
                    "code": self._line(node.lineno),
                }
            )
        elif isinstance(node.value, ast.List) or isinstance(node.value, ast.Dict):
            findings.append(
                {
                    "file": self.filepath,
                    "line": node.lineno,
                    "severity": "HIGH",
                    "category": "ASYNC-ITER: await-on-collection",
                    "note": "`await` on a collection literal -- not a coroutine",
                    "code": self._line(node.lineno),
                }
            )

        self.generic_visit(node)


def scan_file(filepath):
    try:
        with open(filepath) as f:
            source = f.read()
        tree = ast.parse(source, filename=filepath)
        lines = source.splitlines()
        visitor = AsyncIterationVisitor(filepath, lines)
        visitor.visit(tree)
    except SyntaxError as e:
        print(f"  SKIP (syntax error): {filepath}: {e}", file=sys.stderr)


def main():
    print("=" * 80)
    print("ASYNC ITERATION TRAP SCANNER")
    print("=" * 80)

    for root, dirs, files in os.walk(SRC_DIR):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for fname in sorted(files):
            if fname.endswith(".py"):
                scan_file(os.path.join(root, fname))

    if not findings:
        print("\nNo async iteration traps found.")
        return

    severity_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    findings.sort(
        key=lambda f: (severity_order.get(f["severity"], 3), f["file"], f["line"])
    )

    print(f"\nFound {len(findings)} potential async iteration traps:\n")
    for f in findings:
        rel = os.path.relpath(f["file"], "/tmp/chat-sdk-python")
        print(f"[{f['severity']}] {rel}:{f['line']}")
        print(f"  Category: {f['category']}")
        print(f"  Note: {f['note']}")
        print(f"  Code: {f['code']}")
        print()


if __name__ == "__main__":
    main()
