"""Scan for dict access traps (JS->Python).

Detects:
1. dict[key] on external data (API responses, webhook payloads) without .get()
2. dict.get("key").method() without None check (chained call on optional)
"""

import ast
import os
import sys

SRC_DIR = "/tmp/chat-sdk-python/src/chat_sdk"

findings = []


class DictAccessVisitor(ast.NodeVisitor):
    def __init__(self, filepath, source_lines):
        self.filepath = filepath
        self.source_lines = source_lines
        # Track variables known to come from external sources
        self.external_vars = set()

    def _line(self, lineno):
        if 0 < lineno <= len(self.source_lines):
            return self.source_lines[lineno - 1].rstrip()
        return ""

    def _is_external_hint(self, name_str):
        """Check if a variable name suggests external data."""
        hints = [
            "payload",
            "body",
            "data",
            "response",
            "result",
            "event",
            "message",
            "request",
            "webhook",
            "json",
            "params",
            "headers",
            "metadata",
            "config",
            "options",
            "settings",
            "info",
            "details",
            "context",
        ]
        lower = name_str.lower()
        return any(h in lower for h in hints)

    def _get_name(self, node):
        """Extract a human-readable name from a node."""
        if isinstance(node, ast.Name):
            return node.id
        elif isinstance(node, ast.Attribute):
            parent = self._get_name(node.value)
            if parent:
                return f"{parent}.{node.attr}"
            return node.attr
        elif isinstance(node, ast.Call):
            func_name = self._get_name(node.func)
            return f"{func_name}()" if func_name else None
        return None

    def visit_Subscript(self, node):
        """Detect dict[key] on potentially external data."""
        # Only flag string-literal subscripts on variables that look like external data
        if isinstance(node.slice, ast.Constant) and isinstance(
            node.slice.value, str
        ):
            var_name = self._get_name(node.value)
            if var_name and self._is_external_hint(var_name):
                # Check if this is in a try/except or has a guard
                line = self._line(node.lineno)
                findings.append(
                    {
                        "file": self.filepath,
                        "line": node.lineno,
                        "severity": "MEDIUM",
                        "category": "DICT-ACCESS: bare-subscript",
                        "note": f'`{var_name}["{node.slice.value}"]` -- KeyError if key missing (JS returns undefined)',
                        "code": line,
                    }
                )

        # Also flag numeric subscripts on variables with external hints
        elif isinstance(node.slice, ast.Constant) and isinstance(
            node.slice.value, int
        ):
            var_name = self._get_name(node.value)
            if var_name and self._is_external_hint(var_name):
                line = self._line(node.lineno)
                findings.append(
                    {
                        "file": self.filepath,
                        "line": node.lineno,
                        "severity": "LOW",
                        "category": "DICT-ACCESS: numeric-subscript",
                        "note": f"`{var_name}[{node.slice.value}]` -- IndexError if list too short",
                        "code": line,
                    }
                )

        self.generic_visit(node)

    def visit_Call(self, node):
        """Detect .get().method() chains (None propagation risk)."""
        # Pattern: something.method() where something is a .get() call
        if isinstance(node.func, ast.Attribute):
            # The object being called on
            obj = node.func.value
            method_name = node.func.attr

            # Check if obj is a .get() call
            if (
                isinstance(obj, ast.Call)
                and isinstance(obj.func, ast.Attribute)
                and obj.func.attr == "get"
            ):
                # This is dict.get("key").method()
                # Check if .get() has a default argument
                has_default = len(obj.args) >= 2
                if not has_default:
                    var_name = self._get_name(obj.func.value)
                    key = None
                    if obj.args and isinstance(obj.args[0], ast.Constant):
                        key = obj.args[0].value

                    findings.append(
                        {
                            "file": self.filepath,
                            "line": node.lineno,
                            "severity": "HIGH",
                            "category": "DICT-ACCESS: get-then-call",
                            "note": f'`{var_name}.get("{key}").{method_name}()` -- .get() returns None if key missing, then .{method_name}() crashes with AttributeError',
                            "code": self._line(node.lineno),
                        }
                    )

        # Also check for chained attribute access after .get()
        # e.g., dict.get("key").attr (not a call, but an attribute access)
        self.generic_visit(node)

    def visit_Attribute(self, node):
        """Detect .get().attr (attribute access on .get() result without None check)."""
        obj = node.value
        if (
            isinstance(obj, ast.Call)
            and isinstance(obj.func, ast.Attribute)
            and obj.func.attr == "get"
        ):
            has_default = len(obj.args) >= 2
            if not has_default:
                var_name = self._get_name(obj.func.value)
                key = None
                if obj.args and isinstance(obj.args[0], ast.Constant):
                    key = obj.args[0].value

                # Don't double-count with visit_Call
                if not isinstance(node.ctx, ast.Load) or not isinstance(
                    getattr(node, "_parent", None), ast.Call
                ):
                    findings.append(
                        {
                            "file": self.filepath,
                            "line": node.lineno,
                            "severity": "HIGH",
                            "category": "DICT-ACCESS: get-then-attr",
                            "note": f'`{var_name}.get("{key}").{node.attr}` -- .get() returns None if key missing, AttributeError on .{node.attr}',
                            "code": self._line(node.lineno),
                        }
                    )

        self.generic_visit(node)


def _set_parents(tree):
    """Set parent references on all nodes."""
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            child._parent = node


def scan_file(filepath):
    try:
        with open(filepath) as f:
            source = f.read()
        tree = ast.parse(source, filename=filepath)
        _set_parents(tree)
        lines = source.splitlines()
        visitor = DictAccessVisitor(filepath, lines)
        visitor.visit(tree)
    except SyntaxError as e:
        print(f"  SKIP (syntax error): {filepath}: {e}", file=sys.stderr)


def main():
    print("=" * 80)
    print("DICT ACCESS TRAP SCANNER")
    print("=" * 80)

    for root, dirs, files in os.walk(SRC_DIR):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for fname in sorted(files):
            if fname.endswith(".py"):
                scan_file(os.path.join(root, fname))

    if not findings:
        print("\nNo dict access traps found.")
        return

    severity_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    findings.sort(
        key=lambda f: (severity_order.get(f["severity"], 3), f["file"], f["line"])
    )

    high_count = sum(1 for f in findings if f["severity"] == "HIGH")
    med_count = sum(1 for f in findings if f["severity"] == "MEDIUM")
    low_count = sum(1 for f in findings if f["severity"] == "LOW")

    print(f"\nFound {len(findings)} potential dict access traps:")
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
