"""Parser-replacement spike benchmark.

Measures parse-and-translate time for the four parsers (baseline +
three candidates) across a synthetic corpus scaled to ~10KB. Reports
median, p95, and per-construct cost so the Option B decision has
hard numbers to weigh.

Run::

    uv run python scripts/parser_spike/benchmark.py

Acceptance criteria (per issue #69 follow-up):
- 10KB mixed-content document under 5ms median on CI hardware.
- Translator LOC under 250 per library.
"""

from __future__ import annotations

import statistics
import time
from pathlib import Path

from chat_sdk.shared.markdown_parser import parse_markdown as baseline_parse
from chat_sdk.shared.parser_spike.markdown_it_translator import (
    parse_markdown as markdown_it_parse,
)
from chat_sdk.shared.parser_spike.marko_translator import parse_markdown as marko_parse
from chat_sdk.shared.parser_spike.mistune_translator import parse_markdown as mistune_parse

FIXTURE_PATH = Path(__file__).resolve().parents[2] / "tests" / "parser_spike" / "fixtures" / "mixed_content.md"


def _build_corpus(target_bytes: int = 10_240) -> str:
    base = FIXTURE_PATH.read_text(encoding="utf-8")
    out = []
    size = 0
    while size < target_bytes:
        out.append(base)
        size += len(base.encode("utf-8"))
    return "\n".join(out)


def _time_one(fn, text: str, iterations: int) -> list[float]:
    timings = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        fn(text)
        timings.append((time.perf_counter() - t0) * 1000.0)
    return timings


def _translator_loc() -> dict[str, int]:
    root = Path(__file__).resolve().parents[2] / "src" / "chat_sdk" / "shared" / "parser_spike"
    out = {}
    for name, path in [
        ("mistune", root / "mistune_translator.py"),
        ("markdown-it-py", root / "markdown_it_translator.py"),
        ("marko", root / "marko_translator.py"),
    ]:
        text = path.read_text(encoding="utf-8")
        code_lines = [line for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
        # Subtract docstrings as a rough heuristic.
        out[name] = len(code_lines)
    return out


def main() -> None:
    corpus = _build_corpus()
    actual_bytes = len(corpus.encode("utf-8"))
    print(f"Corpus: {actual_bytes:,} bytes (~{actual_bytes / 1024:.1f} KB)")

    # Warm-up: each parser caches some regexes / token-rule chains.
    for fn in (baseline_parse, mistune_parse, markdown_it_parse, marko_parse):
        for _ in range(3):
            fn(corpus)

    iterations = 50
    print(f"Iterations per parser: {iterations}\n")

    print(f"{'parser':<20} {'median (ms)':>12} {'p95 (ms)':>12} {'min (ms)':>12} {'max (ms)':>12}")
    print("-" * 70)
    for name, fn in [
        ("baseline (hand)", baseline_parse),
        ("mistune", mistune_parse),
        ("markdown-it-py", markdown_it_parse),
        ("marko", marko_parse),
    ]:
        timings = _time_one(fn, corpus, iterations)
        timings.sort()
        median = statistics.median(timings)
        p95 = timings[int(len(timings) * 0.95)]
        print(f"{name:<20} {median:>12.2f} {p95:>12.2f} {min(timings):>12.2f} {max(timings):>12.2f}")

    print("\nTranslator LOC (excluding blank lines + line comments):")
    print("-" * 70)
    for name, loc in _translator_loc().items():
        budget_marker = " ✓" if loc < 250 else " ✗ (over 250-LOC budget)"
        print(f"  {name:<20} {loc:>4} lines{budget_marker}")


if __name__ == "__main__":
    main()
