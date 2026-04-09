"""Scan for None propagation traps (missing optional chaining).

In TS: obj?.foo?.bar?.baz safely returns undefined
In Python: must use explicit None checks

Detects:
1. Long attribute chains (a.b.c.d) that may have None intermediaries
2. Attribute access on results of functions that may return None
3. Method calls on potentially None values
"""

import ast
import os
import sys

SRC_DIR = "/tmp/chat-sdk-python/src/chat_sdk"

findings = []


class NonePropagationVisitor(ast.NodeVisitor):
    def __init__(self, filepath, source_lines):
        self.filepath = filepath
        self.source_lines = source_lines

    def _line(self, lineno):
        if 0 < lineno <= len(self.source_lines):
            return self.source_lines[lineno - 1].rstrip()
        return ""

    def _get_chain_depth(self, node):
        """Count the depth of chained attribute accesses."""
        depth = 0
        current = node
        while isinstance(current, ast.Attribute):
            depth += 1
            current = current.value
        return depth

    def _get_chain_str(self, node, max_depth=6):
        """Build a string representation of a chained attribute access."""
        parts = []
        current = node
        while isinstance(current, ast.Attribute) and len(parts) < max_depth:
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            parts.append(current.id)
        elif isinstance(current, ast.Call):
            func_parts = []
            fc = current.func
            while isinstance(fc, ast.Attribute):
                func_parts.append(fc.attr)
                fc = fc.value
            if isinstance(fc, ast.Name):
                func_parts.append(fc.id)
            parts.append(".".join(reversed(func_parts)) + "()")
        parts.reverse()
        return ".".join(parts)

    def _returns_optional(self, func_name):
        """Check if a function name suggests it might return None."""
        hints = [
            "get",
            "find",
            "search",
            "lookup",
            "fetch",
            "first",
            "pop",
            "query",
            "resolve",
            "parse",
        ]
        if func_name:
            lower = func_name.lower()
            return any(h in lower for h in hints)
        return False

    def visit_Attribute(self, node):
        """Detect long attribute chains and chains after potentially-None-returning calls."""
        depth = self._get_chain_depth(node)

        # Long chains (4+ deep) are suspicious
        if depth >= 4:
            chain = self._get_chain_str(node)
            findings.append(
                {
                    "file": self.filepath,
                    "line": node.lineno,
                    "severity": "LOW",
                    "category": "NONE-PROP: deep-chain",
                    "note": f"Deep attribute chain ({depth} levels): `{chain}` -- any intermediate None causes AttributeError",
                    "code": self._line(node.lineno),
                }
            )

        # Check for attribute access after a function call that might return None
        if isinstance(node.value, ast.Call):
            func = node.value.func
            func_name = None
            if isinstance(func, ast.Attribute):
                func_name = func.attr
            elif isinstance(func, ast.Name):
                func_name = func.id

            if func_name and self._returns_optional(func_name):
                chain = self._get_chain_str(node)
                findings.append(
                    {
                        "file": self.filepath,
                        "line": node.lineno,
                        "severity": "MEDIUM",
                        "category": "NONE-PROP: optional-call-chain",
                        "note": f"`{func_name}()` may return None, then `.{node.attr}` causes AttributeError",
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
        visitor = NonePropagationVisitor(filepath, lines)
        visitor.visit(tree)
    except SyntaxError as e:
        print(f"  SKIP (syntax error): {filepath}: {e}", file=sys.stderr)


def main():
    print("=" * 80)
    print("NONE PROPAGATION TRAP SCANNER")
    print("=" * 80)

    for root, dirs, files in os.walk(SRC_DIR):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for fname in sorted(files):
            if fname.endswith(".py"):
                scan_file(os.path.join(root, fname))

    if not findings:
        print("\nNo None propagation traps found.")
        return

    severity_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    findings.sort(
        key=lambda f: (severity_order.get(f["severity"], 3), f["file"], f["line"])
    )

    high_count = sum(1 for f in findings if f["severity"] == "HIGH")
    med_count = sum(1 for f in findings if f["severity"] == "MEDIUM")
    low_count = sum(1 for f in findings if f["severity"] == "LOW")

    print(f"\nFound {len(findings)} potential None propagation traps:")
    print(f"  HIGH: {high_count}, MEDIUM: {med_count}, LOW: {low_count}\n")

    for f in findings:
        rel = os.path.relpath(f["file"], "/tmp/chat-sdk-python")
        print(f"[{f['severity']}] {rel}:{f['line']}")
        print(f"  Category: {f['category']}")
        print(f"  Note: {f['note']}")
        print(f"  Code: {f['code']}")
        print()


if __name__ == "__main__":
    main()
