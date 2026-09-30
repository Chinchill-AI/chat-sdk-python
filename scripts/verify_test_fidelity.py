#!/usr/bin/env python3
"""Verify Python tests are faithful 1:1 translations of TypeScript tests.

For each TS test file, extracts every ``it("...")`` / ``test("...")`` test
name (plus one logical test per ``it.each`` / ``test.each`` template),
converts it to snake_case, and checks that a corresponding
``def test_...()`` exists in the Python translation.

Usage:
    python scripts/verify_test_fidelity.py --strict    # CI path: fail on any missing
    python scripts/verify_test_fidelity.py             # baseline mode (local opt-in)
    python scripts/verify_test_fidelity.py --fix       # append stubs for missing
    python scripts/verify_test_fidelity.py --update-baseline  # rewrite baseline
    python scripts/verify_test_fidelity.py --check-docs       # pin/doc drift guard
    python scripts/verify_test_fidelity.py --report-target    # wave report (never fails on gaps)

The upstream checkout is pinned in ``scripts/upstream_pin.json``: ``pin`` is
the strict CI tag (with its commit SHA) and ``target`` is the tag an
in-flight sync wave is porting towards. ``TS_ROOT`` (default
``/tmp/vercel-chat``) must be a checkout of the pin tag — or of the target
tag for ``--report-target``. When ``TS_ROOT`` is a git checkout its HEAD is
compared against the recorded SHA and a mismatch fails; a plain export
(no ``.git``) only produces a warning.

``--strict`` is the CI contract (see ``.github/workflows/lint.yml``): the
baseline is ignored and any missing translation — or a missing upstream
checkout — fails the build. The ``MAPPING`` dict below is the strict scope;
``TARGET_MAPPING`` adds rows that are only checked by ``--report-target``
(files that do not exist at the pin, or whose Python counterpart still has
gaps); ``UNMAPPED`` records every core test file we deliberately skip, with
the reason. Every ``packages/chat/src/**/*.test.ts(x)`` must appear in one of
the three (warning at the pin, error in ``--report-target``).

``--report-target`` checks ``MAPPING`` + ``TARGET_MAPPING`` against the
target checkout, never fails on missing tests, and writes
``scripts/fidelity_target.json`` — the authoritative wave-wide missing list.

Baseline mode (the default without ``--strict``) is retained for local
workflows where a few ports land in flight: it succeeds iff the set of
missing tests is a subset of ``scripts/fidelity_baseline.json``. Tests that
are in the baseline but now pass are reported as fixed; new misses outside
the baseline fail. Regenerate via ``--update-baseline`` after documenting
intentional divergence in ``docs/UPSTREAM_SYNC.md``.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
TS_ROOT = os.environ.get("TS_ROOT", "/tmp/vercel-chat")
PY_ROOT = os.environ.get("PY_ROOT", str(REPO_ROOT))
BASELINE_PATH = SCRIPT_DIR / "fidelity_baseline.json"
PIN_PATH = SCRIPT_DIR / "upstream_pin.json"
TARGET_REPORT_PATH = SCRIPT_DIR / "fidelity_target.json"
INIT_PATH = REPO_ROOT / "src" / "chat_sdk" / "__init__.py"
CORE_TEST_DIR = "packages/chat/src"
# Docs whose prose may name the pin; ``--check-docs`` keeps them honest.
PIN_DOC_FILES = ("CLAUDE.md", "docs/UPSTREAM_SYNC.md")

# Strict tier: TS test file -> Python test file. CI runs --strict over these
# at the pin; every row must be at 0 missing.
MAPPING = {
    "packages/chat/src/chat.test.ts": "tests/test_chat_faithful.py",
    "packages/chat/src/thread.test.ts": "tests/test_thread_faithful.py",
    "packages/chat/src/channel.test.ts": "tests/test_channel_faithful.py",
    "packages/chat/src/markdown.test.ts": "tests/test_markdown_faithful.py",
    "packages/chat/src/streaming-markdown.test.ts": "tests/test_streaming_markdown.py",
    "packages/chat/src/serialization.test.ts": "tests/test_serialization.py",
    # chat@4.29.0 moved ai.test.ts into ai/ and split it (vercel/chat#492)
    "packages/chat/src/ai/messages.test.ts": "tests/test_ai_messages.py",
    "packages/chat/src/ai/index.test.ts": "tests/test_ai_tools.py",
    # New core test files in chat@4.29.0
    "packages/chat/src/callback-url.test.ts": "tests/test_callback_url.py",
    "packages/chat/src/thread-history.test.ts": "tests/test_thread_history.py",
    "packages/chat/src/transcripts.test.ts": "tests/test_transcripts.py",
    "packages/chat/src/transcripts-wiring.test.ts": "tests/test_transcripts_wiring.py",
    "packages/chat/src/from-full-stream.test.ts": "tests/test_from_full_stream.py",
}

# Target tier: only checked by --report-target (together with MAPPING).
# Rows are files that do not exist at the pin, plus files whose Python
# counterpart is not yet at 0 missing. A row moves into MAPPING once it is
# at 0 missing at the pin. A Python file that does not exist yet reports
# every TS test as missing.
TARGET_MAPPING = {
    # History API (chat@4.39.0). history/user is the renamed transcripts
    # suite; it points at test_transcripts.py until #197 re-points it.
    "packages/chat/src/history/user.test.ts": "tests/test_transcripts.py",
    "packages/chat/src/history/thread.test.ts": "tests/test_history_thread.py",
    "packages/chat/src/history/channel.test.ts": "tests/test_history_channel.py",
    "packages/chat/src/history/to-prompt.test.ts": "tests/test_history_to_prompt.py",
    # Lifecycle / agent-session events (#196, #201)
    "packages/chat/src/agent-session.test.ts": "tests/test_agent_session.py",
    "packages/chat/src/app-context.test.ts": "tests/test_app_context.py",
    "packages/chat/src/installation-events.test.ts": "tests/test_installation_events.py",
    # Present at the pin but with gaps (issue #78)
    "packages/chat/src/cards.test.ts": "tests/test_cards.py",
    "packages/chat/src/modals.test.ts": "tests/test_modals.py",
    "packages/chat/src/emoji.test.ts": "tests/test_emoji.py",
    # Counterpart not yet settled: no Python file has more than 3 exact name
    # matches; test_types.py is the closest (8 of 19 at the pin incl. fuzzy).
    "packages/chat/src/message.test.ts": "tests/test_types.py",
}

# Core test files deliberately not checked, with the reason.
UNMAPPED = {
    "packages/chat/src/ai/tanstack/messages.test.ts": (
        "JS-only TanStack AI adapter (chat@4.41.0, 2d0d3bf3); no Python equivalent — non-parity row lands in #203"
    ),
    "packages/chat/src/ai/tanstack/tools.test.ts": (
        "JS-only TanStack AI adapter (chat@4.41.0, 2d0d3bf3); no Python equivalent — non-parity row lands in #203"
    ),
    "packages/chat/src/workflow/approval.test.ts": (
        "Vercel Workflow SDK integration (chat@4.35.0, 4cb7e5d5); no Python equivalent"
    ),
    "packages/chat/src/jsx-react.test.tsx": "JSX runtime — Known Non-Parity row 'JSX Card/Modal elements'",
    "packages/chat/src/jsx-runtime.test.ts": "JSX runtime — Known Non-Parity row 'JSX Card/Modal elements'",
    "packages/chat/src/jsx-runtime.test.tsx": "JSX runtime — Known Non-Parity row 'JSX Card/Modal elements'",
    "packages/chat/src/adapters/index.test.ts": (
        "static adapter catalog not ported — Known Non-Parity row '`chat/adapters` static adapter catalog'"
    ),
    "packages/chat/src/errors.test.ts": "#78 pending — no Python counterpart identified (0 exact name matches)",
    "packages/chat/src/logger.test.ts": "#78 pending — no Python counterpart identified (0 exact name matches)",
    "packages/chat/src/chat-singleton.test.ts": "#78 pending — no Python counterpart identified (0 exact name matches)",
}


# ---------------------------------------------------------------------------
# Name extraction
# ---------------------------------------------------------------------------


class TsTest(NamedTuple):
    describe: str
    ts_name: str
    py_name: str
    each: bool = False


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


# Vitest/Jest ``.each`` title placeholders: printf-style (``%s %d %i %f %j
# %o %O %c %p %# %$``), the ``%%`` escape, and ``$name`` / ``$a.b`` object
# interpolation. Left-to-right alternation so ``%%s`` strips the ``%%``
# escape and keeps the literal ``s``, as the runner renders it. Applied to
# ``.each`` templates only — a literal ``%``/``$`` in a plain ``it("…")``
# title is part of the name.
_EACH_PLACEHOLDER_RE = re.compile(r"%%|%[sdifjoOcp#$]|\$[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*")


def strip_each_placeholders(template: str) -> str:
    return _EACH_PLACEHOLDER_RE.sub("", template)


_DESCRIBE_RE = re.compile(r'describe\("([^"]+)"')
# ``(?<![\w.$])`` keeps ``regex.test("…")`` / ``foo.it("…")`` from counting.
_PLAIN_TEST_RE = re.compile(r'(?<![\w.$])(?:it|test)\("([^"]+)"')
_EACH_RE = re.compile(r"(?<![\w.$])(it|test|describe)\.each\s*[(`]")
_CALL_OPEN_RE = re.compile(r"\s*\(\s*")
_CLOSERS = {"(": ")", "[": "]", "{": "}"}


def _skip_string(src: str, i: int) -> int:
    """``src[i]`` is a quote; return the index just past the closing quote, or -1."""
    quote = src[i]
    i += 1
    while i < len(src):
        c = src[i]
        if c == "\\":
            i += 2
            continue
        if c == quote:
            return i + 1
        if quote == "`" and src.startswith("${", i):
            i = _match_balanced(src, i + 1)
            if i < 0:
                return -1
            continue
        if quote != "`" and c == "\n":
            return -1
        i += 1
    return -1


def _match_balanced(src: str, i: int) -> int:
    """``src[i]`` opens ``(``/``[``/``{``; return the index just past its closer, or -1.

    Skips string/template literals and ``//`` / ``/* */`` comments. Regex
    literals are not understood; a table containing one with an unbalanced
    bracket yields -1 (reported as an unextractable template).
    """
    stack = [_CLOSERS[src[i]]]
    i += 1
    while i < len(src):
        c = src[i]
        if c in "\"'`":
            i = _skip_string(src, i)
            if i < 0:
                return -1
            continue
        if src.startswith("//", i):
            nl = src.find("\n", i)
            if nl < 0:
                return -1
            i = nl + 1
            continue
        if src.startswith("/*", i):
            end = src.find("*/", i + 2)
            if end < 0:
                return -1
            i = end + 2
            continue
        if c in _CLOSERS:
            stack.append(_CLOSERS[c])
        elif c in ")]}":
            if c != stack[-1]:
                return -1
            stack.pop()
            if not stack:
                return i + 1
        i += 1
    return -1


def _read_each_template(src: str, table_start: int) -> str | None:
    """Return the title template of ``X.each(<table>)("<title>", …)``.

    ``table_start`` is the index of the ``(`` after ``.each`` — or of the
    backtick for the tagged-template table form ``X.each`<rows>`("<title>")``.
    Handles multi-line tables whose title sits on the closing ``])("…"`` or
    ``] as const)("…"`` line. Returns None for a non-literal title.
    """
    tagged = src[table_start] == "`"
    end = _skip_string(src, table_start) if tagged else _match_balanced(src, table_start)
    if end < 0:
        return None
    m = _CALL_OPEN_RE.match(src, end)
    if not m:
        return None
    j = m.end()
    if j >= len(src) or src[j] not in "\"'`":
        return None
    close = _skip_string(src, j)
    if close < 0:
        return None
    raw = src[j + 1 : close - 1]
    if src[j] == "`" and "${" in raw:
        return None
    return re.sub(r"\\(.)", r"\1", raw)


def extract_ts_tests(ts_path: str, warnings: list[str] | None = None) -> list[TsTest]:
    """Extract one ``TsTest`` per ``it``/``test`` call and per ``.each`` template.

    ``describe`` is the most recent ``describe("…")`` / ``describe.each``
    title seen (flat, not scoped). A ``.each`` template counts as one
    logical test: its placeholders are stripped before deriving the Python
    name, while ``ts_name`` keeps the raw template for reporting.
    """
    with open(ts_path, encoding="utf-8") as f:
        content = f.read()

    tests: list[TsTest] = []
    current_describe = ""
    offset = 0

    for lineno, line in enumerate(content.split("\n"), start=1):
        line_start = offset
        offset += len(line) + 1

        desc_match = _DESCRIBE_RE.search(line)
        if desc_match:
            current_describe = desc_match.group(1)

        each_match = _EACH_RE.search(line)
        if each_match:
            kind = each_match.group(1)
            template = _read_each_template(content, line_start + each_match.end() - 1)
            if template is None:
                if warnings is not None:
                    warnings.append(f"{ts_path}:{lineno}: could not extract {kind}.each title")
            elif kind == "describe":
                current_describe = template
            else:
                py_name = ts_name_to_python(strip_each_placeholders(template))
                if py_name:
                    tests.append(TsTest(current_describe, template, py_name, each=True))
                elif warnings is not None:
                    warnings.append(f"{ts_path}:{lineno}: {kind}.each title {template!r} is only placeholders")
            continue

        it_match = _PLAIN_TEST_RE.search(line)
        if it_match:
            ts_name = it_match.group(1)
            py_name = ts_name_to_python(ts_name)
            if py_name:  # skip names that reduce to empty (e.g. "\\n")
                tests.append(TsTest(current_describe, ts_name, py_name))

    return tests


def extract_py_tests(py_path: str) -> list[str]:
    """Extract all test function names from a Python file (with duplicates)."""
    if not os.path.exists(py_path):
        return []
    with open(py_path, encoding="utf-8") as f:
        content = f.read()
    return re.findall(r"def (test_\w+)", content)


def fuzzy_match(py_name, py_tests):
    """Try to match a derived Python test name against existing tests.

    Uses word-overlap matching: extracts significant words (>2 chars) from
    the TS-derived name and requires at least 60% of them (minimum 2) to
    appear in the candidate Python test name. Candidates are scanned in
    sorted order so score ties resolve the same way in every process
    (``py_tests`` is usually a set, whose order follows the per-process
    string hash seed).
    """
    if py_name in py_tests:
        return py_name

    words = [w for w in py_name.replace("test_", "").split("_") if len(w) > 2][:6]
    if not words:
        return None
    threshold = max(2, int(len(words) * 0.6))

    best_match = None
    best_score = 0
    for existing in sorted(py_tests):
        score = sum(1 for w in words if w in existing)
        if score >= threshold and score > best_score:
            best_score = score
            best_match = existing
    return best_match


@dataclass
class FileResult:
    ts_tests: list[TsTest]
    missing: list[TsTest] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    exact: int = 0
    fuzzy: int = 0
    py_exists: bool = True
    # (TS name, Python test it was fuzzy-matched to) — audit these: a fuzzy
    # match can claim an unrelated leftover Python test and hide a gap.
    fuzzy_pairs: list[tuple[str, str]] = field(default_factory=list)

    @property
    def matched(self) -> int:
        return self.exact + self.fuzzy

    @property
    def each_templates(self) -> int:
        return sum(1 for t in self.ts_tests if t.each)


def check_fidelity(
    ts_rel: str,
    py_rel: str,
    ts_root: str | None = None,
    py_root: str | None = None,
    warnings: list[str] | None = None,
) -> FileResult | None:
    """Match one TS test file against its Python translation.

    Returns None when the TS file does not exist under ``ts_root``.
    """
    ts_path = os.path.join(ts_root if ts_root is not None else TS_ROOT, ts_rel)
    py_path = os.path.join(py_root if py_root is not None else PY_ROOT, py_rel)

    if not os.path.exists(ts_path):
        return None

    result = FileResult(ts_tests=extract_ts_tests(ts_path, warnings), py_exists=os.path.exists(py_path))
    # Use Counter as a multiset so duplicate names in different classes both count
    remaining_py = Counter(extract_py_tests(py_path))

    def consume(name: str) -> bool:
        if remaining_py.get(name, 0) > 0:
            remaining_py[name] -= 1
            if remaining_py[name] == 0:
                del remaining_py[name]
            return True
        return False

    # Pass 1: exact matches first (prevents fuzzy from stealing exact names)
    unmatched_ts: list[TsTest] = []
    for test in result.ts_tests:
        if consume(test.py_name):
            result.exact += 1
        else:
            unmatched_ts.append(test)

    # Pass 2: fuzzy matches for remainder
    remaining_set = set(remaining_py.keys())
    for test in unmatched_ts:
        m = fuzzy_match(test.py_name, remaining_set)
        if m and consume(m):
            result.fuzzy += 1
            result.fuzzy_pairs.append((test.ts_name, m))
            if remaining_py.get(m, 0) == 0:
                remaining_set.discard(m)
        else:
            result.missing.append(test)

    result.extra = sorted(remaining_py.keys())
    return result


def generate_stubs(ts_rel: str, missing: list[TsTest]) -> str:
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

    for test in missing:
        class_name = "Test" + re.sub(r"[^a-zA-Z0-9]", "", test.describe.title().replace(" ", ""))
        if class_name != current_class:
            current_class = class_name
            lines.append(f"\n\nclass {class_name}Stubs:")
            lines.append(f'    """Stubs for: {test.describe}"""')

        lines.append("")
        lines.append(f"    async def {test.py_name}(self):")
        if test.each:
            lines.append(f'        # TS: it.each(...)("{test.ts_name}") — one @pytest.mark.parametrize test')
        else:
            lines.append(f'        # TS: it("{test.ts_name}")')
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


# ---------------------------------------------------------------------------
# Pin file, SHA pin, parity consistency, doc drift, completeness
# ---------------------------------------------------------------------------


class PinError(Exception):
    pass


_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_TAG_RE = re.compile(r"^chat@(\d+)\.(\d+)\.(\d+)(?:-[0-9A-Za-z.]+)?$")


def load_pin(path: Path | None = None) -> dict[str, dict[str, str]]:
    """Load and validate ``upstream_pin.json`` (``pin`` and ``target`` entries)."""
    if path is None:
        path = PIN_PATH
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise PinError(f"cannot read {path}: {exc}") from exc
    out: dict[str, dict[str, str]] = {}
    for key in ("pin", "target"):
        entry = data.get(key) if isinstance(data, dict) else None
        if not isinstance(entry, dict):
            raise PinError(f"{path.name}: missing '{key}' object")
        tag, sha = entry.get("tag"), entry.get("sha")
        if not isinstance(tag, str) or not _TAG_RE.match(tag):
            raise PinError(f"{path.name}: '{key}.tag' must look like chat@X.Y.Z, got {tag!r}")
        if not isinstance(sha, str) or not _SHA_RE.match(sha):
            raise PinError(f"{path.name}: '{key}.sha' must be a full 40-char lowercase commit SHA, got {sha!r}")
        out[key] = {"tag": tag, "sha": sha}
    return out


def _read_upstream_parity(init_path: Path | None = None) -> str | None:
    """Read ``UPSTREAM_PARITY`` from ``__init__.py`` by regex (never imports chat_sdk)."""
    path = init_path if init_path is not None else INIT_PATH
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        content = f.read()
    m = re.search(r'^UPSTREAM_PARITY\s*=\s*"([^"]+)"', content, re.MULTILINE)
    return m.group(1) if m else None


def _current_parity_tag() -> str | None:
    """Return the baseline-format parity tag (``chat@X.Y.Z``) for the current repo.

    Reads ``UPSTREAM_PARITY`` from ``src/chat_sdk/__init__.py`` without
    importing the package (avoids pulling optional runtime deps during a
    script run). Returns None if the constant can't be located.
    """
    parity = _read_upstream_parity()
    return f"chat@{parity}" if parity is not None else None


def check_pin_parity(pin_tag: str, parity: str | None) -> str | None:
    """Return an error message unless ``pin_tag`` and ``UPSTREAM_PARITY`` share major.minor.

    Exact equality is not required: the pin may be a patch tag (``chat@4.41.1``)
    while ``UPSTREAM_PARITY`` names the minor (``4.41.0``).
    """
    if parity is None:
        return "UPSTREAM_PARITY not found in src/chat_sdk/__init__.py"
    tag_m = _TAG_RE.match(pin_tag)
    parity_m = re.match(r"^(\d+)\.(\d+)(?:\.\d+)?", parity)
    if not tag_m or not parity_m:
        return f"cannot compare pin {pin_tag!r} with UPSTREAM_PARITY {parity!r}"
    if tag_m.group(1, 2) != parity_m.group(1, 2):
        return (
            f"pin {pin_tag} does not match UPSTREAM_PARITY {parity} (major.minor differ) — "
            "bump scripts/upstream_pin.json and UPSTREAM_PARITY together"
        )
    return None


def resolve_checkout_sha(ts_root: str) -> str | None:
    """Return HEAD of ``ts_root`` if it is the top level of a git checkout, else None.

    A plain export nested inside some other repository is treated as "not a
    checkout" (its enclosing repo's HEAD says nothing about the export).
    """
    env = {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_WORK_TREE")}

    def git(*args: str) -> str | None:
        try:
            proc = subprocess.run(
                ["git", "-C", ts_root, *args],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
                env=env,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return proc.stdout.strip() if proc.returncode == 0 else None

    if not os.path.isdir(ts_root):
        return None
    top = git("rev-parse", "--show-toplevel")
    if top is None or Path(top).resolve() != Path(ts_root).resolve():
        return None
    return git("rev-parse", "HEAD")


def verify_checkout_sha(ts_root: str, expected: dict[str, str], head: str | None = None) -> str | None:
    """Return an error if the ``ts_root`` checkout is at the wrong commit.

    Prints the resolved SHA (or a warning when ``ts_root`` is not a git
    checkout, which cannot be verified and is allowed).
    """
    if head is None:
        head = resolve_checkout_sha(ts_root)
    tag, sha = expected["tag"], expected["sha"]
    if head is None:
        print(
            f"warning: TS_ROOT={ts_root!r} is not a git checkout; cannot verify it is {tag} ({sha}).",
            file=sys.stderr,
        )
        return None
    if head != sha:
        return (
            f"TS_ROOT={ts_root!r} is at {head}, but {tag} is pinned to {sha} in "
            f"scripts/upstream_pin.json. Re-clone with:\n"
            f"  git clone --depth 1 --branch {tag} https://github.com/vercel/chat.git {ts_root}"
        )
    print(f"  upstream: {tag} @ {sha} (TS_ROOT HEAD verified)")
    return None


_VERSION = r"(\d+\.\d+\.\d+(?:-[0-9A-Za-z]+(?:\.[0-9A-Za-z]+)*)?)"
# ``--branch chat@X`` (clone snippets) and ``pinned to [`][vercel/]chat@X``
# (prose; the phrase may wrap across lines).
_DOC_PIN_RE = re.compile(rf"--branch\s+[`\"']?chat@{_VERSION}|pinned\s+to\s+`?(?:vercel/)?chat@{_VERSION}")


def check_docs(pin_tag: str, repo_root: Path | None = None, files: tuple[str, ...] = PIN_DOC_FILES) -> list[str]:
    """Return one error per pin literal in ``files`` that disagrees with ``pin_tag``."""
    root = repo_root if repo_root is not None else REPO_ROOT
    expected = pin_tag.removeprefix("chat@")
    errors: list[str] = []
    for rel in files:
        path = root / rel
        if not path.exists():
            errors.append(f"{rel}: file not found")
            continue
        text = path.read_text(encoding="utf-8")
        for m in _DOC_PIN_RE.finditer(text):
            found = m.group(1) or m.group(2)
            if found != expected:
                line = text.count("\n", 0, m.start()) + 1
                phrase = " ".join(m.group(0).split())
                errors.append(f"{rel}:{line}: '{phrase}' disagrees with pin {pin_tag}")
    return errors


def list_core_test_files(ts_root: str) -> list[str]:
    """All ``packages/chat/src/**/*.test.ts(x)`` files, as sorted repo-relative paths."""
    base = Path(ts_root) / CORE_TEST_DIR
    if not base.is_dir():
        return []
    found = {p for pattern in ("*.test.ts", "*.test.tsx") for p in base.rglob(pattern)}
    return sorted(p.relative_to(ts_root).as_posix() for p in found if "node_modules" not in p.parts)


def find_unclassified(files: list[str]) -> list[str]:
    classified = MAPPING.keys() | TARGET_MAPPING.keys() | UNMAPPED.keys()
    return [f for f in files if f not in classified]


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------


def load_baseline(path: Path) -> dict[str, set[tuple[str, str]]]:
    """Load fidelity baseline. Missing file returns empty baseline.

    Exits with code 1 when the baseline's ``ts_parity`` disagrees with the
    current ``UPSTREAM_PARITY`` constant — a stale baseline could otherwise
    silently mask upstream drift after a version bump.
    """
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
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
    "Ratchet-down baseline for scripts/verify_test_fidelity.py. This "
    "repo ships at strict fidelity for mapped core files (0 missing) "
    "against the current UPSTREAM_PARITY tag, so the baseline is "
    "normally empty. Scope: the MAPPING dict in "
    "scripts/verify_test_fidelity.py is the authoritative list of TS "
    "files checked (extending to the remaining unmapped files is issue #78) "
    "packages/chat/src/*.test.ts files. Default CI mode runs --strict "
    "via .github/workflows/lint.yml; this file is retained for local "
    "workflows that want to opt back into baseline mode (e.g. during "
    "an upstream sync where several ports land in flight). To "
    "baseline genuinely-divergent tests, run "
    "scripts/verify_test_fidelity.py --update-baseline after "
    "documenting the divergence in docs/UPSTREAM_SYNC.md."
)


def write_baseline(path: Path, all_missing: dict[str, list[TsTest]], total_ts: int, fallback_parity: str) -> None:
    """Persist the current set of missing tests as the new baseline.

    If ``path`` already exists and has a ``_comment`` field, that curated
    comment is preserved so hand-written context (e.g. scope qualifiers,
    shipping-posture notes) isn't silently overwritten on every
    ``--update-baseline`` run. Only fresh files get the default boilerplate.
    """
    existing_comment: str | None = None
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                existing = json.load(f)
            if isinstance(existing.get("_comment"), str):
                existing_comment = existing["_comment"]
        except (OSError, json.JSONDecodeError):
            existing_comment = None

    # Derive ts_parity from UPSTREAM_PARITY so a fresh regen after an
    # upstream version bump doesn't self-trap on a stale literal. Fall
    # back to the pin tag only if UPSTREAM_PARITY can't be read (e.g.
    # __init__.py missing during an in-flight refactor).
    current_parity = _current_parity_tag()
    payload = {
        "_comment": existing_comment if existing_comment is not None else _DEFAULT_BASELINE_COMMENT,
        "ts_parity": current_parity if current_parity is not None else fallback_parity,
        "total_ts_tests": total_ts,
        "total_missing": sum(len(v) for v in all_missing.values()),
        "missing": {
            ts_rel: [[t.describe, t.ts_name] for t in sorted(entries, key=lambda e: (e.describe, e.ts_name))]
            for ts_rel, entries in sorted(all_missing.items())
            if entries
        },
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=False)
        f.write("\n")


# ---------------------------------------------------------------------------
# Target report
# ---------------------------------------------------------------------------

_TARGET_REPORT_COMMENT = (
    "Generated by `TS_ROOT=<checkout of target.tag> uv run python scripts/verify_test_fidelity.py "
    "--report-target`. Authoritative wave-wide list of upstream tests with no Python translation "
    "at the target tag in scripts/upstream_pin.json. Checks MAPPING (tier 'strict') plus "
    "TARGET_MAPPING (tier 'target'). Each .each template counts as one test. Do not hand-edit; "
    "every sync-wave PR regenerates it and quotes the delta."
)


def build_target_report(target: dict[str, str], results: dict[str, tuple[str, str, FileResult]], absent: list[str]):
    files = {}
    for ts_rel, (py_rel, tier, r) in results.items():
        files[ts_rel] = {
            "python": py_rel,
            "tier": tier,
            "python_exists": r.py_exists,
            "ts_tests": len(r.ts_tests),
            "each_templates": r.each_templates,
            "matched_exact": r.exact,
            "matched_fuzzy": r.fuzzy,
            "missing_count": len(r.missing),
            "extra_count": len(r.extra),
            "missing": [[t.describe, t.ts_name] for t in r.missing],
            "fuzzy_matches": [[ts_name, py_name] for ts_name, py_name in r.fuzzy_pairs],
        }
    all_r = [r for _p, _t, r in results.values()]
    return {
        "_comment": _TARGET_REPORT_COMMENT,
        "tag": target["tag"],
        "sha": target["sha"],
        "totals": {
            "ts_tests": sum(len(r.ts_tests) for r in all_r),
            "each_templates": sum(r.each_templates for r in all_r),
            "matched_exact": sum(r.exact for r in all_r),
            "matched_fuzzy": sum(r.fuzzy for r in all_r),
            "missing": sum(len(r.missing) for r in all_r),
            "extra": sum(len(r.extra) for r in all_r),
        },
        "absent_ts_files": absent,
        "files": files,
    }


_JSON_STR = r'"(?:[^"\\]|\\.)*"'
_TWO_STRING_ARRAY_RE = re.compile(rf"\[\n\s+({_JSON_STR}),\n\s+({_JSON_STR})\n\s+\]")


def dump_target_report(report: dict) -> str:
    """Serialize the report as indented JSON with each ``[a, b]`` pair on one line.

    Wave PRs regenerate this file in parallel, so one pair per line keeps
    diffs and merge conflicts small. Only whitespace changes; the result
    parses back to ``report``.
    """
    text = json.dumps(report, indent=2, ensure_ascii=False)
    return _TWO_STRING_ARRAY_RE.sub(r"[\1, \2]", text) + "\n"


def print_report_delta(previous: dict | None, report: dict) -> None:
    """Print the change in missing counts versus a previously committed report."""
    if not previous or previous.get("tag") != report["tag"]:
        print("\n(no previous report for this tag — no delta)")
        return
    prev_total = previous.get("totals", {}).get("missing", 0)
    now_total = report["totals"]["missing"]
    print(f"\nDelta vs committed report: missing {prev_total} -> {now_total} ({now_total - prev_total:+d})")
    prev_files = previous.get("files", {})
    for ts_rel in sorted(set(prev_files) | set(report["files"])):
        before = prev_files.get(ts_rel, {}).get("missing_count", 0)
        after = report["files"].get(ts_rel, {}).get("missing_count", 0)
        if before != after:
            print(f"  {ts_rel}: {before} -> {after} ({after - before:+d})")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--strict", action="store_true", help="fail on any missing test (CI)")
    parser.add_argument("--fix", action="store_true", help="append stubs for missing tests")
    parser.add_argument("--update-baseline", action="store_true", help="rewrite fidelity_baseline.json")
    parser.add_argument("--check-docs", action="store_true", help="fail if docs name a different pin")
    parser.add_argument("--report-target", action="store_true", help="write fidelity_target.json for the target tag")
    return parser.parse_args(argv)


def _print_file_result(ts_rel: str, py_rel: str, r: FileResult, absorbers: int, new_keys: set) -> None:
    absorber_note = f" ({absorbers} absorbers)" if absorbers else ""
    each_note = f" (incl. {r.each_templates} .each)" if r.each_templates else ""
    status = "OK" if not r.missing else f"GAPS ({len(r.missing)})"
    py_note = "" if r.py_exists else " (python file not found)"
    print(f"\n{ts_rel}")
    print(f"  -> {py_rel}{py_note}")
    print(
        f"  TS: {len(r.ts_tests)}{each_note} | Matched: {r.matched} (exact {r.exact}, fuzzy {r.fuzzy})"
        f"{absorber_note} | Missing: {len(r.missing)} | Extra: {len(r.extra)} | {status}"
    )
    for t in r.missing[:5]:
        marker = "NEW" if (t.describe, t.ts_name) in new_keys else "baselined"
        print(f"    MISSING ({marker}): [{t.describe}] {t.ts_name}")
    if len(r.missing) > 5:
        print(f"    ... and {len(r.missing) - 5} more")


def run_check_docs(pin: dict[str, dict[str, str]]) -> int:
    print(f"Pin: {pin['pin']['tag']} @ {pin['pin']['sha']} | target: {pin['target']['tag']} @ {pin['target']['sha']}")
    errors = check_docs(pin["pin"]["tag"])
    for err in errors:
        print(f"doc drift: {err}")
    if errors:
        print(f"\n{len(errors)} doc pin literal(s) disagree with scripts/upstream_pin.json.")
        return 1
    print(f"Docs agree with pin {pin['pin']['tag']} ({', '.join(PIN_DOC_FILES)}).")
    return 0


def run_report_target(pin: dict[str, dict[str, str]]) -> int:
    target = pin["target"]
    overlap = MAPPING.keys() & TARGET_MAPPING.keys()
    if overlap:
        print(f"error: files in both MAPPING and TARGET_MAPPING: {sorted(overlap)}", file=sys.stderr)
        return 1
    print("=" * 70)
    print(f"TARGET FIDELITY REPORT ({target['tag']})")
    print("=" * 70)
    if not (Path(TS_ROOT) / CORE_TEST_DIR).is_dir():
        print(f"\nupstream checkout missing: {CORE_TEST_DIR} not found under TS_ROOT={TS_ROOT!r}")
        return 1
    sha_error = verify_checkout_sha(TS_ROOT, target)
    if sha_error:
        print(f"\nerror: {sha_error}")
        return 1
    unclassified = find_unclassified(list_core_test_files(TS_ROOT))
    if unclassified:
        print("\nerror: core test files not in MAPPING, TARGET_MAPPING or UNMAPPED:")
        for f in unclassified:
            print(f"  - {f}")
        return 1

    warnings: list[str] = []
    results: dict[str, tuple[str, str, FileResult]] = {}
    absent: list[str] = []
    rows = [(ts, py, "strict") for ts, py in MAPPING.items()]
    rows += [(ts, py, "target") for ts, py in TARGET_MAPPING.items()]
    for ts_rel, py_rel, tier in rows:
        r = check_fidelity(ts_rel, py_rel, warnings=warnings)
        if r is None:
            print(f"\n{ts_rel} — not present at {target['tag']}")
            absent.append(ts_rel)
            continue
        results[ts_rel] = (py_rel, tier, r)
        _print_file_result(ts_rel, f"{py_rel} [{tier}]", r, 0, set())

    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)

    report = build_target_report(target, results, absent)
    previous = None
    if TARGET_REPORT_PATH.exists():
        try:
            previous = json.loads(TARGET_REPORT_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = None
    t = report["totals"]
    print(f"\n{'=' * 70}")
    print(
        f"TARGET TOTAL: {t['matched_exact'] + t['matched_fuzzy']}/{t['ts_tests']} matched "
        f"(exact {t['matched_exact']}, fuzzy {t['matched_fuzzy']}), {t['missing']} missing, "
        f"{t['each_templates']} .each templates, {t['extra']} extra"
    )
    print_report_delta(previous, report)
    with open(TARGET_REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(dump_target_report(report))
    print(f"\nReport written to {TARGET_REPORT_PATH} (never fails on missing tests).")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    fix_mode, strict_mode, update_baseline = args.fix, args.strict, args.update_baseline

    if strict_mode and update_baseline:
        print(
            "error: --strict and --update-baseline are mutually exclusive.\n"
            "  --strict says 'no missing allowed'; --update-baseline says "
            "'snapshot whatever is missing into the allowlist'. Pick one.",
            file=sys.stderr,
        )
        return 2
    if (args.check_docs or args.report_target) and (
        strict_mode or update_baseline or fix_mode or (args.check_docs and args.report_target)
    ):
        print("error: --check-docs and --report-target must be run on their own.", file=sys.stderr)
        return 2

    try:
        pin = load_pin()
    except PinError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    parity_error = check_pin_parity(pin["pin"]["tag"], _read_upstream_parity())
    if parity_error:
        print(f"error: {parity_error}", file=sys.stderr)
        return 1

    if args.check_docs:
        return run_check_docs(pin)
    if args.report_target:
        return run_report_target(pin)

    baseline = {} if (strict_mode or update_baseline) else load_baseline(BASELINE_PATH)

    total_missing = 0
    total_matched = 0
    total_ts = 0
    total_absorbers = 0
    all_missing: dict[str, list[TsTest]] = {}
    new_misses: dict[str, list[tuple[str, str]]] = {}
    fixed: dict[str, list[tuple[str, str]]] = {}
    missing_ts_files: list[str] = []
    warnings: list[str] = []

    print("=" * 70)
    print("TEST FIDELITY REPORT")
    if strict_mode:
        print("  mode: --strict (baseline ignored)")
    elif update_baseline:
        print("  mode: --update-baseline (rewriting baseline)")
    else:
        print(f"  mode: baseline ({BASELINE_PATH.name})")
    print("=" * 70)

    if os.path.isdir(TS_ROOT):
        sha_error = verify_checkout_sha(TS_ROOT, pin["pin"])
        if sha_error:
            print(f"\nerror: {sha_error}")
            return 1
        for f in find_unclassified(list_core_test_files(TS_ROOT)):
            print(f"warning: {f} is not in MAPPING, TARGET_MAPPING or UNMAPPED", file=sys.stderr)

    for ts_rel, py_rel in MAPPING.items():
        r = check_fidelity(ts_rel, py_rel, warnings=warnings)
        if r is None:
            ts_path = os.path.join(TS_ROOT, ts_rel)
            print(f"\n{ts_rel} — MISSING (upstream TS file not found at {ts_path})")
            missing_ts_files.append(ts_path)
            continue

        py_path = os.path.join(PY_ROOT, py_rel)
        absorbers = count_absorbers(py_path)

        total_ts += len(r.ts_tests)
        total_matched += r.matched
        total_missing += len(r.missing)
        total_absorbers += absorbers
        all_missing[ts_rel] = r.missing

        current_missing_keys = {(t.describe, t.ts_name) for t in r.missing}
        baseline_keys = baseline.get(ts_rel, set())
        file_new = sorted(current_missing_keys - baseline_keys)
        file_fixed = sorted(baseline_keys - current_missing_keys)
        if file_new:
            new_misses[ts_rel] = file_new
        if file_fixed:
            fixed[ts_rel] = file_fixed

        _print_file_result(ts_rel, py_rel, r, absorbers, set(file_new))

        if fix_mode and r.missing:
            stubs = generate_stubs(ts_rel, r.missing)

            if os.path.exists(py_path):
                with open(py_path, "a", encoding="utf-8") as f:
                    f.write(stubs)
                print(f"  -> Appended {len(r.missing)} stubs to {py_rel}")
            else:
                with open(py_path, "w", encoding="utf-8") as f:
                    f.write(f'"""Faithful translation of {ts_rel}"""\n\nimport pytest\n')
                    f.write(stubs)
                print(f"  -> Created {py_rel} with {len(r.missing)} stubs")

    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)

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
            "\nClone the upstream repo at the pinned parity tag (scripts/upstream_pin.json), e.g.:\n"
            f"  git clone --depth 1 --branch {pin['pin']['tag']} "
            "https://github.com/vercel/chat.git /tmp/vercel-chat\n"
            "then re-run with TS_ROOT=/tmp/vercel-chat."
        )
        return 1

    if update_baseline:
        write_baseline(BASELINE_PATH, all_missing, total_ts, pin["pin"]["tag"])
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
