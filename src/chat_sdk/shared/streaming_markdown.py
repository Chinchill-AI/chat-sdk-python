"""Streaming markdown renderer that buffers incomplete constructs.

Repairs incomplete markdown during LLM streaming. Key method:
``get_committable_text()`` returns safe-to-render text, holding back
incomplete constructs. ``finish()`` flushes everything.

Python port of streaming-markdown.ts.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

TABLE_ROW_RE = re.compile(r"^\|.*\|$")
TABLE_SEPARATOR_RE = re.compile(r"^\|[\s:]*-{1,}[\s:]*(\|[\s:]*-{1,}[\s:]*)*\|$")

# Characters that can open an inline markdown construct.
INLINE_MARKER_CHARS = frozenset({"*", "~", "`", "["})


# ---------------------------------------------------------------------------
# Helper: lightweight inline-marker repair
# ---------------------------------------------------------------------------
# The TS version uses the `remend` library. We implement a minimal
# equivalent: remend closes any unclosed inline constructs (bold, italic,
# strikethrough, code, links).  For our purposes we only need to know
# whether appending closing markers would make the text *longer* (i.e. it
# has unclosed constructs).


def _strip_fenced_code(text: str) -> str:
    """Return *text* with content between code fences replaced by empty lines.

    This allows inline-marker counting to ignore literal characters inside
    fenced code blocks (e.g. ``*`` inside a code block is not an unclosed
    italic marker).
    """
    lines = text.split("\n")
    result_lines: list[str] = []
    in_fence = False
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            result_lines.append("")  # replace fence line itself
        elif in_fence:
            result_lines.append("")  # replace content inside fence
        else:
            result_lines.append(line)
    return "\n".join(result_lines)


def _close_emphasis(result: str, stripped: str, ch: str) -> str:
    """Close unclosed bold/italic emphasis for a single marker character.

    Scans *stripped* (text with code spans/fences removed) left-to-right,
    grouping consecutive runs of *ch* into delimiter tokens.  Tracks a
    simple open/close stack for ``ch*2`` (bold) and ``ch`` (italic),
    then appends whatever closing sequence is needed to *result*.

    The algorithm mirrors CommonMark's emphasis handling at a simplified
    level: a run of 2+ characters opens/closes bold first, then any
    remaining single character opens/closes italic.

    To guarantee idempotency the suffix is separated from any trailing
    marker run by a zero-width space so it cannot merge with existing
    characters and create new openers on re-scan.
    """
    # Collect runs of the marker character (e.g. *, **, ***).
    runs: list[int] = []
    i = 0
    while i < len(stripped):
        if stripped[i] == "\\":
            i += 2
            continue
        if stripped[i] == ch:
            run_len = 0
            while i < len(stripped) and stripped[i] == ch:
                run_len += 1
                i += 1
            runs.append(run_len)
        else:
            i += 1

    # Walk runs and track open bold / italic state.
    bold_open = False
    italic_open = False
    for run in runs:
        remaining = run
        # Process pairs (bold toggles) first.
        while remaining >= 2:
            bold_open = not bold_open
            remaining -= 2
        # A leftover single character toggles italic.
        if remaining == 1:
            italic_open = not italic_open

    if not bold_open and not italic_open:
        return result

    suffix = ""
    # Close in reverse order: italic first (inner), then bold (outer).
    if italic_open:
        suffix += ch
    if bold_open:
        suffix += ch * 2

    # If the result already ends with the marker character, inserting the
    # suffix directly would merge with the trailing run and change the
    # delimiter structure on re-scan (breaking idempotency).  Insert a
    # zero-width space (U+200B) as a separator so the suffix is parsed as
    # its own distinct run.
    if result.endswith(ch):
        return result + "\u200b" + suffix
    return result + suffix


def _remend(text: str) -> str:
    """Repair incomplete markdown by closing unclosed inline markers.

    This is a simplified Python equivalent of the ``remend`` npm package.
    Fixes issues in the previous implementation:
      - Dead code around ``star_count2`` (removed)
      - ``~~`` counting confused by ``~~~`` code fences (handled by stripping)
      - Missing ``__`` / ``_`` (underscore bold/italic) handling (added)
      - Markers inside code blocks no longer counted as inline markers

    Strategy: count ``**`` (bold) delimiter pairs first, then remaining
    unpaired ``*`` (italic) -- likewise for ``__`` / ``_``.  Count ``~~``
    substrings for strikethrough.  Count backtick characters for inline code.
    """
    result = text

    # --- code fences ---
    # If inside an unclosed code fence, close it and return immediately.
    in_code_fence = False
    for line in result.split("\n"):
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_code_fence = not in_code_fence

    if in_code_fence:
        result += "\n```"
        return result

    # Strip fenced code blocks so their contents don't affect inline counts.
    outside_fences = _strip_fenced_code(result)

    # --- inline code backticks ---
    # Count total backtick characters outside code fences. If odd, one code
    # span is unclosed -- append a single backtick.
    backtick_count = outside_fences.count("`")
    if backtick_count % 2 != 0:
        result += "`"

    # --- bold / italic (* based) ---
    # Strip backtick spans so markers inside inline code are ignored.
    stripped = re.sub(r"`[^`]*`", "", outside_fences)
    result = _close_emphasis(result, stripped, "*")

    # --- bold / italic (_ based) ---
    result = _close_emphasis(result, stripped, "_")

    # --- strikethrough ~~ ---
    # Count non-overlapping ``~~`` substrings outside code fences. If odd,
    # one strikethrough is unclosed -- append ``~~``.
    tilde_pairs = outside_fences.count("~~")
    if tilde_pairs % 2 != 0:
        result += "~~"

    # --- links [text](url) ---
    open_brackets = 0
    m = 0
    while m < len(outside_fences):
        if outside_fences[m] == "\\":
            m += 2
            continue
        if outside_fences[m] == "[":
            open_brackets += 1
        elif outside_fences[m] == "]":
            open_brackets -= 1
        m += 1
    if open_brackets > 0:
        result += "]" * open_brackets

    return result


def _is_clean(text: str) -> bool:
    """Check if text is clean -- _remend doesn't add any closing markers."""
    return len(_remend(text)) <= len(text)


# ---------------------------------------------------------------------------
# Pure functions ported from the TS module
# ---------------------------------------------------------------------------


def _is_inside_code_fence(text: str) -> bool:
    """Check if the text ends inside an unclosed code fence."""
    inside = False
    for line in text.split("\n"):
        trimmed = line.lstrip()
        if trimmed.startswith("```") or trimmed.startswith("~~~"):
            inside = not inside
    return inside


def _get_committable_prefix(text: str) -> str:
    """Return the prefix of *text* that can be safely rendered.

    Holds back trailing lines that look like an unconfirmed table (rows
    matching ``|...|`` without a subsequent separator ``|---|---|``).
    """
    ends_with_newline = text.endswith("\n")
    lines = text.split("\n")

    # If the text doesn't end with newline, the last line is still being
    # written. Remove it from consideration for table detection.
    if not ends_with_newline and lines:
        lines.pop()

    # Remove trailing empty string from split (if text ends with \n)
    if ends_with_newline and lines and lines[-1] == "":
        lines.pop()

    # Walk backward to find consecutive table-like lines at the end
    held_count = 0
    separator_found = False

    for i in range(len(lines) - 1, -1, -1):
        trimmed = lines[i].strip()

        # Empty line breaks a table block
        if trimmed == "":
            break

        if TABLE_SEPARATOR_RE.match(trimmed):
            separator_found = True
            break

        if TABLE_ROW_RE.match(trimmed):
            held_count += 1
        else:
            break

    if separator_found or held_count == 0:
        return text

    commit_line_count = len(lines) - held_count
    committed_lines = lines[:commit_line_count]

    result = "\n".join(committed_lines)
    if committed_lines:
        result += "\n"

    return result


def _find_clean_prefix(text: str) -> str:
    """Return the longest prefix where all inline markers are balanced."""
    if not text or _is_clean(text):
        return text

    i = len(text) - 1
    while i >= 0:
        if text[i] in INLINE_MARKER_CHARS:
            # Group consecutive same characters (e.g. ** or ~~)
            while i > 0 and text[i - 1] == text[i]:
                i -= 1
            candidate = text[:i]
            if _is_clean(candidate):
                return candidate
        i -= 1

    return ""


def _wrap_tables_for_append(text: str, close_fences: bool = False) -> str:
    """Wrap confirmed GFM table blocks in code fences for append-only streaming."""
    had_trailing_newline = text.endswith("\n")
    lines = text.split("\n")

    if had_trailing_newline and lines and lines[-1] == "":
        lines.pop()

    result: list[str] = []
    in_table = False
    in_user_code_fence = False

    for i, raw_line in enumerate(lines):
        trimmed = raw_line.strip()

        # Track existing code fences in the source markdown.
        if not in_table and (trimmed.startswith("```") or trimmed.startswith("~~~")):
            in_user_code_fence = not in_user_code_fence
            result.append(raw_line)
            continue

        if in_user_code_fence:
            result.append(raw_line)
            continue

        is_table_line = trimmed != "" and (
            TABLE_ROW_RE.match(trimmed) is not None or TABLE_SEPARATOR_RE.match(trimmed) is not None
        )

        if is_table_line and not in_table:
            # Only wrap if this block has a separator (confirmed table)
            has_separator = False
            for j in range(i, len(lines)):
                t = lines[j].strip()
                if TABLE_SEPARATOR_RE.match(t):
                    has_separator = True
                    break
                if t == "" or TABLE_ROW_RE.match(t) is None:
                    break
            if has_separator:
                result.append("```")
                in_table = True
        elif not is_table_line and in_table:
            result.append("```")
            in_table = False

        result.append(raw_line)

    if in_table and close_fences:
        result.append("```")

    output = "\n".join(result)
    if had_trailing_newline:
        output += "\n"
    return output


# ---------------------------------------------------------------------------
# StreamingMarkdownRenderer class
# ---------------------------------------------------------------------------


class StreamingMarkdownRenderer:
    """Buffer and repair incomplete markdown during LLM streaming.

    Outputs markdown (not platform text).  Format conversion still happens
    in the adapter's ``edit_message -> render_postable -> from_ast`` pipeline.
    """

    def __init__(self) -> None:
        self._accumulated = ""
        self._dirty = True
        self._cached_render = ""
        self._finished = False
        # Number of code fence toggles from completed lines (odd = inside)
        self._fence_toggles = 0
        # Incomplete trailing line buffer for incremental fence tracking
        self._incomplete_line = ""

    # -- public API ----------------------------------------------------------

    def push(self, chunk: str) -> None:
        """Append a chunk from the LLM stream."""
        self._accumulated += chunk
        self._dirty = True

        # Incrementally track code fence state from completed lines
        self._incomplete_line += chunk
        parts = self._incomplete_line.split("\n")
        self._incomplete_line = parts.pop()  # last (possibly incomplete) segment
        for line in parts:
            trimmed = line.lstrip()
            if trimmed.startswith("```") or trimmed.startswith("~~~"):
                self._fence_toggles += 1

    def render(self) -> str:
        """Get renderable markdown for an intermediate edit.

        - Holds back trailing lines that look like a table header
          until a separator line confirms or the next line denies.
        - Applies _remend() to close incomplete inline markers.
        - Idempotent: returns cached result if no push() since last call.
        """
        if not self._dirty:
            return self._cached_render

        self._dirty = False

        if self._finished:
            self._cached_render = _remend(self._accumulated)
            return self._cached_render

        # If inside an unclosed code fence, don't buffer
        if self._is_accumulated_inside_fence():
            self._cached_render = _remend(self._accumulated)
            return self._cached_render

        committable = _get_committable_prefix(self._accumulated)
        self._cached_render = _remend(committable)
        return self._cached_render

    def get_committable_text(self) -> str:
        """Get text safe for append-only streaming (e.g. Slack native streaming).

        - Holds back unconfirmed table headers until separator arrives.
        - Wraps confirmed tables in code fences so pipes render as literal
          text (not broken mrkdwn).
        - Holds back unclosed inline markers.
        - The final ``edit_message`` replaces everything with properly formatted text.
        """
        if self._finished:
            return _wrap_tables_for_append(self._accumulated, close_fences=True)

        text = self._accumulated
        if text and not text.endswith("\n"):
            last_newline = text.rfind("\n")
            without_incomplete_line = text[: last_newline + 1] if last_newline >= 0 else ""

            # If stripping puts us inside a code fence, keep the incomplete line
            if _is_inside_code_fence(without_incomplete_line):
                return _wrap_tables_for_append(text)

            text = without_incomplete_line

        # Inside a user code fence: skip table holding and inline marker buffering
        if _is_inside_code_fence(text):
            return _wrap_tables_for_append(text)

        committed = _get_committable_prefix(text)
        wrapped = _wrap_tables_for_append(committed)

        # If text ends inside an open table code fence,
        # skip inline marker buffering
        if _is_inside_code_fence(wrapped):
            return wrapped

        return _find_clean_prefix(wrapped)

    def get_text(self) -> str:
        """Raw accumulated text (no remend, no buffering). For the final edit."""
        return self._accumulated

    def finish(self) -> str:
        """Signal stream end. Flushes held-back lines. Returns final render."""
        self._finished = True
        self._dirty = True
        return self.render()

    # -- private helpers -----------------------------------------------------

    def _is_accumulated_inside_fence(self) -> bool:
        """O(1) check if accumulated text is inside an unclosed code fence."""
        inside = self._fence_toggles % 2 == 1
        trimmed = self._incomplete_line.lstrip()
        if trimmed.startswith("```") or trimmed.startswith("~~~"):
            inside = not inside
        return inside
