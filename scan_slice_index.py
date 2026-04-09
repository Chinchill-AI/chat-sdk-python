"""Scan for slice/index traps (JS->Python).

Detects:
1. str.split(x)[N] where N might be out of bounds (JS returns undefined, Python throws IndexError)
2. arr[-1] usage (valid in Python, but check for JS-port assumptions)
3. Hardcoded index access on dynamic data
"""

import ast
import os
import sys

SRC_DIR = "/tmp/chat-sdk-python/src/chat_sdk"

findings = []


class SliceIndexVisitor(ast.NodeVisitor):
    def __init__(self, filepath, source_lines):
        self.filepath = filepath
        self.source_lines = source_lines

    def _line(self, lineno):
        if 0 < lineno <= len(self.source_lines):
            return self.source_lines[lineno - 1].rstrip()
        return ""

    def _get_name(self, node):
        if isinstance(node, ast.Name):
            return node.id
        elif isinstance(node, ast.Attribute):
            parent = self._get_name(node.value)
            return f"{parent}.{node.attr}" if parent else node.attr
        return None

    def visit_Subscript(self, node):
        """Detect dangerous index access patterns."""
        # Pattern 1: str.split(...)[N]
        if isinstance(node.value, ast.Call) and isinstance(
            node.value.func, ast.Attribute
        ):
            method_name = node.value.func.attr
            if method_name in ("split", "rsplit", "splitlines"):
                if isinstance(node.slice, ast.Constant) and isinstance(
                    node.slice.value, int
                ):
                    idx = node.slice.value
                    if idx != 0:  # [0] on split is generally safe
                        severity = "HIGH" if idx > 1 else "MEDIUM"
                        findings.append(
                            {
                                "file": self.filepath,
                                "line": node.lineno,
                                "severity": severity,
                                "category": "SLICE-INDEX: split-index",
                                "note": f"`.{method_name}(...)[{idx}]` -- IndexError if fewer than {idx+1} parts (JS returns undefined)",
                                "code": self._line(node.lineno),
                            }
                        )

        # Pattern 2: Negative index on something that could be empty
        if isinstance(node.slice, ast.Constant) and isinstance(
            node.slice.value, int
        ):
            if node.slice.value < 0:
                var_name = self._get_name(node.value)
                # Flag if the source could be empty
                if isinstance(node.value, ast.Call):
                    func = node.value.func
                    func_name = None
                    if isinstance(func, ast.Attribute):
                        func_name = func.attr
                    elif isinstance(func, ast.Name):
                        func_name = func.id

                    if func_name in ("split", "rsplit", "splitlines", "readlines"):
                        findings.append(
                            {
                                "file": self.filepath,
                                "line": node.lineno,
                                "severity": "MEDIUM",
                                "category": "SLICE-INDEX: negative-on-split",
                                "note": f"`.{func_name}()[{node.slice.value}]` -- IndexError if result is empty",
                                "code": self._line(node.lineno),
                            }
                        )
                elif var_name:
                    # General negative indexing -- lower severity
                    findings.append(
                        {
                            "file": self.filepath,
                            "line": node.lineno,
                            "severity": "LOW",
                            "category": "SLICE-INDEX: negative-index",
                            "note": f"`{var_name}[{node.slice.value}]` -- IndexError if sequence is empty",
                            "code": self._line(node.lineno),
                        }
                    )

        # Pattern 3: Hardcoded high index on dynamic data
        if isinstance(node.slice, ast.Constant) and isinstance(
            node.slice.value, int
        ):
            idx = node.slice.value
            if idx >= 2:  # Accessing [2] or higher
                # Check if this is on a variable (not a literal)
                if isinstance(node.value, ast.Name) or isinstance(
                    node.value, ast.Attribute
                ):
                    var_name = self._get_name(node.value)
                    if var_name:
                        external_hints = [
                            "parts",
                            "segments",
                            "tokens",
                            "chunks",
                            "items",
                            "elements",
                            "pieces",
                            "components",
                            "fields",
                            "columns",
                            "rows",
                            "lines",
                            "words",
                            "args",
                            "values",
                            "keys",
                            "entries",
                        ]
                        if any(h in var_name.lower() for h in external_hints):
                            findings.append(
                                {
                                    "file": self.filepath,
                                    "line": node.lineno,
                                    "severity": "MEDIUM",
                                    "category": "SLICE-INDEX: high-index-on-dynamic",
                                    "note": f"`{var_name}[{idx}]` -- IndexError if fewer than {idx+1} elements",
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
        visitor = SliceIndexVisitor(filepath, lines)
        visitor.visit(tree)
    except SyntaxError as e:
        print(f"  SKIP (syntax error): {filepath}: {e}", file=sys.stderr)


def main():
    print("=" * 80)
    print("SLICE/INDEX TRAP SCANNER")
    print("=" * 80)

    for root, dirs, files in os.walk(SRC_DIR):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for fname in sorted(files):
            if fname.endswith(".py"):
                scan_file(os.path.join(root, fname))

    if not findings:
        print("\nNo slice/index traps found.")
        return

    severity_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    findings.sort(
        key=lambda f: (severity_order.get(f["severity"], 3), f["file"], f["line"])
    )

    high_count = sum(1 for f in findings if f["severity"] == "HIGH")
    med_count = sum(1 for f in findings if f["severity"] == "MEDIUM")
    low_count = sum(1 for f in findings if f["severity"] == "LOW")

    print(f"\nFound {len(findings)} potential slice/index traps:")
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
