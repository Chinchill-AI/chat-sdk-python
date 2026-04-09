"""Scan for JS-to-Python truthiness traps.

Detects:
1. `x or default_value` where x could be 0 or "" (the JS `||` pattern)
2. `if not x:` / `if x:` where x could be 0 or "" and should be treated as valid
"""

import ast
import os
import sys
import textwrap

SRC_DIR = "/tmp/chat-sdk-python/src/chat_sdk"

findings = []


def get_source_line(filepath, lineno):
    """Get a specific line from a file."""
    try:
        with open(filepath) as f:
            lines = f.readlines()
            if 0 < lineno <= len(lines):
                return lines[lineno - 1].rstrip()
    except Exception:
        pass
    return "<unable to read>"


class TruthinessVisitor(ast.NodeVisitor):
    def __init__(self, filepath, source_lines):
        self.filepath = filepath
        self.source_lines = source_lines

    def _line(self, lineno):
        if 0 < lineno <= len(self.source_lines):
            return self.source_lines[lineno - 1].rstrip()
        return ""

    def visit_BoolOp(self, node):
        """Detect `x or default` patterns where x could be 0 or ""."""
        if isinstance(node.op, ast.Or) and len(node.values) >= 2:
            left = node.values[0]
            right = node.values[1]

            # Pattern: `something.get("key") or default`
            # Pattern: `variable or default`
            # Pattern: `something or default` where default is a literal number or string
            is_get_call = (
                isinstance(left, ast.Call)
                and isinstance(left.func, ast.Attribute)
                and left.func.attr == "get"
            )
            is_simple_name = isinstance(left, ast.Name)
            is_attribute = isinstance(left, ast.Attribute)
            is_subscript = isinstance(left, ast.Subscript)

            # Check if the right side is a numeric or string default
            right_is_numeric = isinstance(right, ast.Constant) and isinstance(
                right.value, (int, float)
            )
            right_is_string = isinstance(right, ast.Constant) and isinstance(
                right.value, str
            )
            right_is_list = isinstance(right, (ast.List, ast.Tuple))
            right_is_dict = isinstance(right, ast.Dict)
            right_is_name = isinstance(right, ast.Name)
            right_is_call = isinstance(right, ast.Call)

            # Flag if left side could plausibly be 0 or ""
            if is_get_call or is_simple_name or is_attribute or is_subscript:
                severity = "LOW"
                note = ""

                if is_get_call and right_is_numeric:
                    severity = "HIGH"
                    note = "dict.get() or <number> -- if key maps to 0, default will be used instead"
                elif is_get_call and right_is_string:
                    severity = "MEDIUM"
                    note = 'dict.get() or <string> -- if key maps to "", default will be used instead'
                elif is_get_call and right_is_list:
                    severity = "MEDIUM"
                    note = "dict.get() or <list> -- if key maps to [], default will be used instead"
                elif is_get_call and right_is_dict:
                    severity = "MEDIUM"
                    note = "dict.get() or <dict> -- if key maps to {}, default will be used instead"
                elif is_simple_name and right_is_numeric:
                    severity = "MEDIUM"
                    note = "variable or <number> -- if variable is 0, default will be used"
                elif is_simple_name and right_is_string:
                    severity = "MEDIUM"
                    note = 'variable or <string> -- if variable is "", default will be used'
                elif is_attribute and right_is_numeric:
                    severity = "MEDIUM"
                    note = "attr or <number> -- if attr is 0, default will be used"
                elif is_attribute and right_is_string:
                    severity = "MEDIUM"
                    note = 'attr or <string> -- if attr is "", default will be used'
                elif is_subscript and (right_is_numeric or right_is_string):
                    severity = "MEDIUM"
                    note = "subscript or <literal> -- if subscript is 0/\"\", default will be used"
                elif (is_get_call or is_simple_name or is_attribute) and (
                    right_is_name or right_is_call
                ):
                    severity = "LOW"
                    note = "x or y -- if x is falsy (0, \"\", [], etc.), y will be used"

                if severity in ("HIGH", "MEDIUM"):
                    findings.append(
                        {
                            "file": self.filepath,
                            "line": node.lineno,
                            "severity": severity,
                            "category": "TRUTHINESS: or-default",
                            "note": note,
                            "code": self._line(node.lineno),
                        }
                    )

        self.generic_visit(node)

    def visit_If(self, node):
        """Detect `if not x:` or `if x:` where x could be 0 or ""."""
        test = node.test

        # `if not x:` pattern
        if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
            operand = test.operand
            if isinstance(operand, ast.Name):
                # Check if the body assigns a default or returns
                line = self._line(node.lineno)
                # Check variable name for hints it could be numeric or string
                name = operand.id.lower()
                numeric_hints = [
                    "count",
                    "size",
                    "length",
                    "len",
                    "num",
                    "index",
                    "offset",
                    "limit",
                    "page",
                    "port",
                    "timeout",
                    "max",
                    "min",
                    "total",
                    "width",
                    "height",
                    "depth",
                    "retry",
                    "retries",
                    "delay",
                    "interval",
                    "ttl",
                    "age",
                    "duration",
                    "priority",
                ]
                string_hints = [
                    "name",
                    "text",
                    "title",
                    "content",
                    "message",
                    "body",
                    "label",
                    "description",
                    "key",
                    "value",
                    "path",
                    "url",
                    "id",
                    "prefix",
                    "suffix",
                    "token",
                    "channel",
                    "thread",
                    "user",
                ]
                if any(h in name for h in numeric_hints):
                    findings.append(
                        {
                            "file": self.filepath,
                            "line": node.lineno,
                            "severity": "MEDIUM",
                            "category": "TRUTHINESS: if-not-zero-risk",
                            "note": f"`if not {operand.id}` -- would trigger when {operand.id} is 0",
                            "code": line,
                        }
                    )
                elif any(h in name for h in string_hints):
                    findings.append(
                        {
                            "file": self.filepath,
                            "line": node.lineno,
                            "severity": "LOW",
                            "category": "TRUTHINESS: if-not-empty-string-risk",
                            "note": f'`if not {operand.id}` -- would trigger when {operand.id} is ""',
                            "code": line,
                        }
                    )

            elif isinstance(operand, ast.Call) and isinstance(
                operand.func, ast.Attribute
            ):
                if operand.func.attr == "get":
                    findings.append(
                        {
                            "file": self.filepath,
                            "line": node.lineno,
                            "severity": "MEDIUM",
                            "category": "TRUTHINESS: if-not-get",
                            "note": "`if not dict.get(...)` -- triggers on 0, \"\", [], {} too",
                            "code": self._line(node.lineno),
                        }
                    )

        self.generic_visit(node)

    def visit_Assign(self, node):
        """Detect `x = y or z` assignment patterns."""
        if isinstance(node.value, ast.BoolOp) and isinstance(node.value.op, ast.Or):
            # Already caught by visit_BoolOp, but let's add context about assignment targets
            pass
        self.generic_visit(node)


def scan_file(filepath):
    try:
        with open(filepath) as f:
            source = f.read()
        tree = ast.parse(source, filename=filepath)
        lines = source.splitlines()
        visitor = TruthinessVisitor(filepath, lines)
        visitor.visit(tree)
    except SyntaxError as e:
        print(f"  SKIP (syntax error): {filepath}: {e}", file=sys.stderr)


def main():
    print("=" * 80)
    print("TRUTHINESS TRAP SCANNER")
    print("=" * 80)

    for root, dirs, files in os.walk(SRC_DIR):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for fname in sorted(files):
            if fname.endswith(".py"):
                scan_file(os.path.join(root, fname))

    if not findings:
        print("\nNo truthiness traps found.")
        return

    # Sort by severity
    severity_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    findings.sort(key=lambda f: (severity_order.get(f["severity"], 3), f["file"], f["line"]))

    print(f"\nFound {len(findings)} potential truthiness traps:\n")

    for f in findings:
        rel = os.path.relpath(f["file"], "/tmp/chat-sdk-python")
        print(f"[{f['severity']}] {rel}:{f['line']}")
        print(f"  Category: {f['category']}")
        print(f"  Note: {f['note']}")
        print(f"  Code: {f['code']}")
        print()


if __name__ == "__main__":
    main()
