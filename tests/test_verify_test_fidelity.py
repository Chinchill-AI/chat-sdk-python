"""Tests for ``scripts/verify_test_fidelity.py`` (issue #185).

The script is tooling, not a fidelity-mapped port: these tests pin its
extraction rules (``.each`` templates, ``test(``), the SHA pin, the
doc-drift guard, the pin/``UPSTREAM_PARITY`` consistency check, the
completeness check over ``packages/chat/src/**/*.test.ts(x)``, and the
``--report-target`` output. The ``.each`` fixtures are the real template
shapes from ``chat@4.41.1``.
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "verify_test_fidelity.py"
_PIN_SHA = "2a553aa948e0605d5a93f4c30994ccca72a495f0"
_TARGET_SHA = "f690af3c26a3bd7649387cccad12d215463ed22c"


@pytest.fixture(scope="module")
def vtf():
    spec = importlib.util.spec_from_file_location("verify_test_fidelity_under_test", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _extract(vtf, tmp_path: Path, source: str, warnings: list[str] | None = None):
    ts = _write(tmp_path / "sample.test.ts", source)
    return vtf.extract_ts_tests(str(ts), warnings)


# ---------------------------------------------------------------------------
# .each / test( extraction
# ---------------------------------------------------------------------------

# Real shapes from chat@4.41.1 (tables trimmed, titles and closers verbatim).
_EACH_SHAPES = [
    pytest.param(
        """describe("isMention property", () => {
    it.each([
      ["jane@slack-bot.com", "an email whose domain starts with the bot name"],
      ["https://user@slack-bot.com", "url userinfo"],
    ])("should not treat %s as a mention (%s)", async (text) => {
      chat.onNewMessage(ANY_REGEX, handler);
    });
});
""",
        "isMention property",
        "should not treat %s as a mention (%s)",
        "test_should_not_treat_as_a_mention",
        id="chat-printf-multiline-table",
    ),
    pytest.param(
        """describe("Streaming", () => {
    it.each([
      {
        expectedTeamId: "T345",
        label: "team.id",
        raw: { team: { id: "T345" }, type: "block_actions" },
      },
    ])("should pass stream options from Slack current message context via $label", async ({
      raw,
      expectedTeamId,
    }) => {});
});
""",
        "Streaming",
        "should pass stream options from Slack current message context via $label",
        "test_should_pass_stream_options_from_slack_current_message_context_via",
        id="thread-dollar-label-nested-objects",
    ),
    pytest.param(
        """  describe("maxPerUser eviction", () => {
    it.each([
      { maxPerUser: false as const, expected: 205, first: "msg 0" },
      { maxPerUser: undefined, expected: 200, first: "msg 5" },
    ])("retains $expected entries with maxPerUser=$maxPerUser", async ({
      maxPerUser,
    }) => {});
  });
""",
        "maxPerUser eviction",
        "retains $expected entries with maxPerUser=$maxPerUser",
        "test_retains_entries_with_maxperuser",
        id="history-user-two-dollar-placeholders",
    ),
    pytest.param(
        """describe("Chat — History API wiring", () => {
  it.each([
    50,
    false,
  ] as const)("merges legacy maxPerUser=%s under history.user", async (maxPerUser) => {
    // A config migrating one field at a time: identity moved to history.user,
  });
});
""",
        "Chat — History API wiring",
        "merges legacy maxPerUser=%s under history.user",
        "test_merges_legacy_maxperuser_under_historyuser",
        id="transcripts-wiring-as-const-table",
    ),
    pytest.param(
        """    describe.each([
      "json",
      "standalone",
    ])("%s", (method) => {
      it.each(
        ["adapter", "state", "stream"].flatMap((access) =>
          ["replaced", "cleared"].map((singleton) => ({ access, singleton }))
        )
      )("retains the runtime after resolving $access first with the singleton $singleton", async ({
        access,
      }) => {});
    });
""",
        "%s",
        "retains the runtime after resolving $access first with the singleton $singleton",
        "test_retains_the_runtime_after_resolving_first_with_the_singleton",
        id="serialization-computed-table-inside-describe-each",
    ),
]


@pytest.mark.parametrize(("source", "describe", "ts_name", "py_name"), _EACH_SHAPES)
def test_each_template_extracted_as_one_logical_test(vtf, tmp_path, source, describe, ts_name, py_name):
    warnings: list[str] = []
    tests = _extract(vtf, tmp_path, source, warnings)
    assert tests == [vtf.TsTest(describe, ts_name, py_name, each=True)]
    assert warnings == []


def test_describe_each_title_is_used_for_reporting_only(vtf, tmp_path):
    source = """describe.each(["Installed", "Uninstalled"] as const)("on%s", (kind) => {
  it("dispatches to registered handlers", async () => {});
});
"""
    tests = _extract(vtf, tmp_path, source)
    assert tests == [vtf.TsTest("on%s", "dispatches to registered handlers", "test_dispatches_to_registered_handlers")]


def test_plain_it_keeps_literal_percent_and_dollar(vtf, tmp_path):
    source = """describe("progress", () => {
  it("100% done", () => {});
  it("costs $5 via $label", () => {});
});
"""
    tests = _extract(vtf, tmp_path, source)
    assert [(t.ts_name, t.py_name, t.each) for t in tests] == [
        ("100% done", "test_100_done", False),
        ("costs $5 via $label", "test_costs_5_via_label", False),
    ]


def test_plain_test_calls_counted_but_regex_test_method_is_not(vtf, tmp_path):
    source = """describe("catalog", () => {
  test("each entry slug matches its key", () => {
    expect(/^[a-z]+$/.test("slack")).toBe(true);
  });
});
"""
    tests = _extract(vtf, tmp_path, source)
    assert [t.py_name for t in tests] == ["test_each_entry_slug_matches_its_key"]


def test_test_each_with_single_quoted_title(vtf, tmp_path):
    source = """test.each([[1], [2]])('adds %d twice', (n) => {});
"""
    tests = _extract(vtf, tmp_path, source)
    assert tests == [vtf.TsTest("", "adds %d twice", "test_adds_twice", each=True)]


def test_tagged_template_each_table(vtf, tmp_path):
    source = """it.each`
  a    | b    | expected
  ${1} | ${1} | ${2}
`("returns $expected when $a is added to $b", ({ a, b, expected }) => {});
"""
    tests = _extract(vtf, tmp_path, source)
    assert tests == [
        vtf.TsTest("", "returns $expected when $a is added to $b", "test_returns_when_is_added_to", each=True)
    ]


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ("should not treat %s as a mention (%s)", "should not treat  as a mention ()"),
        ("%d/%i/%f/%j/%o/%# of", "///// of"),
        # ``%%`` is the escape for a literal percent: ``%%s`` keeps the ``s``.
        ("100%%s", "100s"),
        ("$a.b.c then $x", " then "),
        ("no placeholders", "no placeholders"),
        ("", ""),
    ],
)
def test_strip_each_placeholders(vtf, template, expected):
    assert vtf.strip_each_placeholders(template) == expected


def test_each_title_that_is_only_placeholders_is_skipped_with_warning(vtf, tmp_path):
    warnings: list[str] = []
    tests = _extract(vtf, tmp_path, 'it.each([["a"]])("%s", (x) => {});\n', warnings)
    assert tests == []
    assert len(warnings) == 1 and "only placeholders" in warnings[0]


def test_each_with_dynamic_title_is_skipped_with_warning(vtf, tmp_path):
    warnings: list[str] = []
    tests = _extract(vtf, tmp_path, "it.each([[1]])(`case ${name}`, (x) => {});\n", warnings)
    assert tests == []
    assert len(warnings) == 1 and "could not extract it.each title" in warnings[0]


def test_each_table_brackets_inside_strings_and_comments_do_not_confuse_scanner(vtf, tmp_path):
    source = """it.each([
  [")", "]"], // a stray ) in a comment
  [`}${"]"}`, "/* ) */"],
])("handles %s", () => {});
"""
    warnings: list[str] = []
    tests = _extract(vtf, tmp_path, source, warnings)
    assert [t.py_name for t in tests] == ["test_handles"]
    assert warnings == []


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('it.each<[string]>([["a"]])("handles %s input", () => {});', ("handles %s input", "test_handles_input", True)),
        (
            'it.each<{ a: Array<string>; f: () => void }>([])("typed %s", () => {});',
            ("typed %s", "test_typed", True),
        ),
        ('it.concurrent.each([[1]])("runs %s at once", () => {});', ("runs %s at once", "test_runs_at_once", True)),
        ('it.skip.each([[1]])("skips %s", () => {});', ("skips %s", "test_skips", True)),
        ('test.only.each([[1]])("focuses %s", () => {});', ("focuses %s", "test_focuses", True)),
        ('it.for([[1]])("iterates %s", () => {});', ("iterates %s", "test_iterates", True)),
        ('it.skipIf(process.env.CI)("handles input", () => {});', ("handles input", "test_handles_input", False)),
        (
            'it.runIf(check(a, b))("runs when enabled", () => {});',
            ("runs when enabled", "test_runs_when_enabled", False),
        ),
        ('it.skip("is skipped for now", () => {});', ("is skipped for now", "test_is_skipped_for_now", False)),
        ("it('uses single quotes', () => {});", ("uses single quotes", "test_uses_single_quotes", False)),
        ('it(\n  "wraps its title", () => {});', ("wraps its title", "test_wraps_its_title", False)),
    ],
    ids=[
        "each-generic",
        "each-nested-generic",
        "concurrent-each",
        "skip-each",
        "test-only-each",
        "for",
        "skipIf",
        "runIf",
        "skip-plain",
        "single-quoted",
        "title-on-next-line",
    ],
)
def test_vitest_call_variants_are_extracted(vtf, tmp_path, source, expected):
    warnings: list[str] = []
    tests = _extract(vtf, tmp_path, source + "\n", warnings)
    assert [(t.ts_name, t.py_name, t.each) for t in tests] == [expected]
    assert warnings == []


@pytest.mark.parametrize(
    ("source", "warning"),
    [
        ('it.todo("port later");', "unrecognized test call form 'it.todo'"),
        ('it.extend({ db })("uses a fixture", () => {});', "unrecognized test call form 'it.extend'"),
        ("it(caseName, () => {});", "could not extract it title"),
        ('it.skipIf`x`("never", () => {});', "unrecognized test call form 'it.skipIf'"),
    ],
    ids=["todo", "extend", "non-literal-title", "skipIf-without-parens"],
)
def test_unknown_test_call_forms_warn_instead_of_vanishing(vtf, tmp_path, source, warning):
    warnings: list[str] = []
    assert _extract(vtf, tmp_path, source + "\n", warnings) == []
    assert len(warnings) == 1 and warnings[0].endswith(f":1: {warning}")


def test_prose_and_strings_are_not_test_calls(vtf, tmp_path):
    source = """// make it (optional) and test `x` here
expect(email).toBe("user@test.com");
const it2 = describe.name;
"""
    warnings: list[str] = []
    assert _extract(vtf, tmp_path, source, warnings) == []
    assert warnings == []


@pytest.mark.parametrize(
    "source",
    [
        "const source = 'test(\"example\", () => {});';",
        'const source = "it(\\"example\\", () => {});";',
        'const source = `it("example", () => {});`;',
        "// it.each(cases)(caseName, handler);",
        '// it("commented out", () => {});',
        '/* it("block comment", () => {}); */',
        "/*\n  it.each(cases)(caseName, handler);\n*/",
        'const re = /it("example")/;',
    ],
    ids=[
        "single-quoted-fixture",
        "double-quoted-fixture",
        "template-fixture",
        "commented-each",
        "commented-it",
        "block-comment",
        "multiline-block-comment",
        "regex-literal",
    ],
)
def test_calls_inside_strings_comments_and_regexes_are_ignored(vtf, tmp_path, source):
    warnings: list[str] = []
    assert _extract(vtf, tmp_path, source + "\n", warnings) == []
    assert warnings == []


def test_real_tests_around_literals_are_still_extracted(vtf, tmp_path):
    # A regex literal holding a quote, a template with a nested ``${…}``
    # call and a division must not leave the lexer inside a literal.
    source = """const re = /"/;
it("after a regex with a quote", () => {});
const s = `a ${fn("x", `inner ${y}`)} b`;
it("after a nested template", () => {});
const half = total / 2; const r = /x/g;
it("after division", () => {});
"""
    warnings: list[str] = []
    tests = _extract(vtf, tmp_path, source, warnings)
    assert [t.ts_name for t in tests] == ["after a regex with a quote", "after a nested template", "after division"]
    assert warnings == []


@pytest.mark.parametrize(
    "stmt",
    ["if (enabled) /`/.test(value);", "while (next()) /`/.exec(s);", "for (;;) /`/g.test(v);", "} /`/.test(v);"],
    ids=["if", "while", "for", "after-block"],
)
def test_regex_after_control_flow_parens_does_not_mask_later_tests(vtf, tmp_path, stmt):
    # After ``if (…)`` a ``/`` starts a regex, not a division; lexing it as
    # division would open a template at the backtick and hide the test.
    source = f'{stmt}\nit("after the regex", () => {{}});\nconst t = `x`;\n'
    warnings: list[str] = []
    assert [t.ts_name for t in _extract(vtf, tmp_path, source, warnings)] == ["after the regex"]
    assert warnings == []


@pytest.mark.parametrize(
    ("source", "warning"),
    [
        ('it("a", () => {});\nconst s = `never closed;\nit("b", () => {});\n', ":2: could not lex the file"),
        ('it("a", () => {});\n/* never closed\nit("b", () => {});\n', ":2: could not lex the file"),
        ('it("a", () => {});\nconst s = `${x;\nit("b", () => {});\n', ":2: could not lex the file"),
    ],
    ids=["template", "block-comment", "interpolation"],
)
def test_unterminated_literal_fails_closed_instead_of_masking_the_rest(vtf, tmp_path, source, warning):
    warnings: list[str] = []
    tests = _extract(vtf, tmp_path, source, warnings)
    assert [t.ts_name for t in tests] == ["a", "b"]  # scanned as code, as before the lexer
    assert len(warnings) == 1 and warning in warnings[0]


@pytest.mark.parametrize(
    ("source", "label"),
    [
        ('it.each([[1]])("rejects " + kind + " %s", () => {});', "it.each"),
        ('it("rejects " + kind, () => {});', "it"),
        ("test('rejects ' + kind, () => {});", "test"),
        ('it.skipIf(c)("rejects " + kind, () => {});', "it.skipIf"),
    ],
    ids=["each", "plain", "single-quoted", "skipIf"],
)
def test_computed_title_is_unextractable_not_its_literal_prefix(vtf, tmp_path, source, label):
    warnings: list[str] = []
    assert _extract(vtf, tmp_path, source + "\n", warnings) == []
    assert len(warnings) == 1 and warnings[0].endswith(f":1: could not extract {label} title")


def test_literal_title_followed_by_whitespace_or_paren_still_extracted(vtf, tmp_path):
    source = 'it("spaced" , () => {});\ntest("no callback");\nit.each([[1]])("each %s"\n  , () => {});\n'
    warnings: list[str] = []
    tests = _extract(vtf, tmp_path, source, warnings)
    assert [t.ts_name for t in tests] == ["spaced", "no callback", "each %s"]
    assert warnings == []


@pytest.mark.parametrize(
    "source",
    [
        'describe.only("suite", () => { it("works", () => {}); });',
        'describe.skip("suite", () => { test("works", () => {}); });',
        'describe.each([1])("suite", () => { it("works", () => {}); });',
    ],
    ids=["describe-only", "describe-skip", "describe-each"],
)
def test_describe_call_does_not_consume_the_line_test(vtf, tmp_path, source):
    warnings: list[str] = []
    tests = _extract(vtf, tmp_path, source + "\n", warnings)
    assert [(t.describe, t.ts_name, t.py_name) for t in tests] == [("suite", "works", "test_works")]
    assert warnings == []


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def test_fuzzy_pass_matches_plain_tests_before_each_templates(vtf, tmp_path):
    # chat@4.41.1 chat.test.ts: the template comes first in the file, and
    # stripped of placeholders ("should not treat mention") it clears the
    # fuzzy threshold against the later plain test's translation.
    _write(
        tmp_path / "ts" / "a.test.ts",
        """describe("isMention property", () => {
  it.each([["jane@slack-bot.com", "an email"]])("should not treat %s as a mention (%s)", () => {});
});
describe("getUser", () => {
  it("should not match GitHub-style logins as Slack ids (case sensitivity)", () => {});
  it("returns null for unknown users", () => {});
});
""",
    )
    _write(tmp_path / "py" / "test_a.py", "def test_should_not_match_github_style_logins_as_slack_ids():\n    pass\n")
    r = vtf.check_fidelity("a.test.ts", "test_a.py", ts_root=str(tmp_path / "ts"), py_root=str(tmp_path / "py"))
    assert r.fuzzy_pairs == [
        (
            "should not match GitHub-style logins as Slack ids (case sensitivity)",
            "test_should_not_match_github_style_logins_as_slack_ids",
        )
    ]
    # Missing stays in file order: the template first, then the later plain test.
    assert [t.ts_name for t in r.missing] == ["should not treat %s as a mention (%s)", "returns null for unknown users"]


def test_check_fidelity_reports_exact_fuzzy_and_missing(vtf, tmp_path):
    _write(
        tmp_path / "ts" / "a.test.ts",
        """describe("A", () => {
  it("returns the entry for a known slug", () => {});
  it("keeps the lock alive while streaming replies", () => {});
  it("rejects unknown install ids", () => {});
});
""",
    )
    _write(
        tmp_path / "py" / "test_a.py",
        "def test_returns_the_entry_for_a_known_slug():\n    pass\n\n"
        "def test_keeps_lock_alive_during_streaming():\n    pass\n",
    )
    r = vtf.check_fidelity("a.test.ts", "test_a.py", ts_root=str(tmp_path / "ts"), py_root=str(tmp_path / "py"))
    assert (r.exact, r.fuzzy) == (1, 1)
    assert r.fuzzy_pairs == [("keeps the lock alive while streaming replies", "test_keeps_lock_alive_during_streaming")]
    assert [t.ts_name for t in r.missing] == ["rejects unknown install ids"]
    assert r.py_exists is True


def test_fuzzy_match_breaks_score_ties_independently_of_set_order(vtf):
    candidates = ["test_zeta_keeps_lock_alive", "test_alpha_keeps_lock_alive"]
    for ordering in (candidates, list(reversed(candidates))):
        # dict preserves insertion order, standing in for a set iterated in either order
        pool = dict.fromkeys(ordering).keys()
        assert vtf.fuzzy_match("test_keeps_the_lock_alive", pool) == "test_alpha_keeps_lock_alive"


def test_missing_python_file_reports_every_test_missing(vtf, tmp_path):
    _write(tmp_path / "a.test.ts", 'it("one", () => {});\nit.each([[1]])("two %s", () => {});\n')
    r = vtf.check_fidelity("a.test.ts", "tests/test_nope.py", ts_root=str(tmp_path), py_root=str(tmp_path))
    assert r.py_exists is False
    assert [t.py_name for t in r.missing] == ["test_one", "test_two"]
    assert r.each_templates == 1


# ---------------------------------------------------------------------------
# Pin file, SHA pin, parity, doc drift, completeness
# ---------------------------------------------------------------------------


def test_committed_pin_file_and_parity_agree(vtf):
    pin = vtf.load_pin()
    assert pin["pin"] == {"tag": "chat@4.31.0", "sha": _PIN_SHA}
    assert pin["target"] == {"tag": "chat@4.41.1", "sha": _TARGET_SHA}
    assert vtf.check_pin_parity(pin["pin"]["tag"], vtf._read_upstream_parity()) is None


@pytest.mark.parametrize(
    "payload",
    [
        {"pin": {"tag": "chat@4.31.0", "sha": _PIN_SHA}},
        {"pin": {"tag": "4.31.0", "sha": _PIN_SHA}, "target": {"tag": "chat@4.41.1", "sha": _TARGET_SHA}},
        {"pin": {"tag": "chat@4.31.0", "sha": "2a553aa9"}, "target": {"tag": "chat@4.41.1", "sha": _TARGET_SHA}},
    ],
    ids=["no-target", "tag-without-prefix", "short-sha"],
)
def test_load_pin_rejects_malformed_pin_file(vtf, tmp_path, payload):
    path = _write(tmp_path / "upstream_pin.json", json.dumps(payload))
    with pytest.raises(vtf.PinError):
        vtf.load_pin(path)


@pytest.mark.parametrize(
    ("pin_tag", "parity", "ok"),
    [
        ("chat@4.31.0", "4.31.0", True),
        ("chat@4.41.1", "4.41.0", True),
        ("chat@4.41.1", "4.31.0", False),
        ("chat@5.31.0", "4.31.0", False),
        ("chat@4.31.0", None, False),
    ],
)
def test_pin_parity_compares_major_minor_only(vtf, pin_tag, parity, ok):
    assert (vtf.check_pin_parity(pin_tag, parity) is None) is ok


def test_sha_mismatch_fails_and_match_passes(vtf, capsys):
    expected = {"tag": "chat@4.31.0", "sha": _PIN_SHA}
    error = vtf.verify_checkout_sha("/x", expected, head=_TARGET_SHA)
    assert error is not None and _TARGET_SHA in error and "--branch chat@4.31.0" in error
    assert vtf.verify_checkout_sha("/x", expected, head=_PIN_SHA) is None
    assert f"chat@4.31.0 @ {_PIN_SHA}" in capsys.readouterr().out


def test_sha_check_warns_without_failing_for_plain_export(vtf, tmp_path, capsys):
    expected = {"tag": "chat@4.31.0", "sha": _PIN_SHA}
    assert vtf.verify_checkout_sha(str(tmp_path), expected) is None
    assert "is not a git checkout" in capsys.readouterr().err


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_resolve_checkout_sha_reads_head_only_at_checkout_top_level(vtf, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    head = _git(repo, "rev-parse", "HEAD")
    nested = repo / "export"
    nested.mkdir()
    assert vtf.resolve_checkout_sha(str(repo)) == head
    # An export nested inside some other repository is not that repository.
    assert vtf.resolve_checkout_sha(str(nested)) is None


def test_sha_mismatch_hint_clones_into_a_fresh_directory(vtf, tmp_path):
    ts_root = tmp_path / "vercel-chat"
    ts_root.mkdir()
    expected = {"tag": "chat@4.31.0", "sha": _PIN_SHA}
    error = vtf.verify_checkout_sha(str(ts_root), expected, head=_TARGET_SHA)
    # git refuses to clone into the existing TS_ROOT, so the hint must not target it.
    fresh = tmp_path / "vercel-chat-4.31.0"
    assert f"--branch chat@4.31.0 https://github.com/vercel/chat.git {fresh}\n" in error
    fresh.mkdir()
    error = vtf.verify_checkout_sha(str(ts_root), expected, head=_TARGET_SHA)
    assert "git clone" not in error and f"re-run with TS_ROOT={fresh}" in error


def test_sha_check_fails_closed_when_git_cannot_read_an_existing_dot_git(vtf, tmp_path, capsys):
    ts_root = tmp_path / "checkout"
    _write(ts_root / ".git", f"gitdir: {tmp_path / 'nowhere'}\n")
    error = vtf.verify_checkout_sha(str(ts_root), {"tag": "chat@4.31.0", "sha": _PIN_SHA})
    assert error is not None and "has a .git but git could not read its HEAD" in error
    assert "is not a git checkout" not in capsys.readouterr().err


def test_sha_check_rejects_local_edits_to_upstream_test_files(vtf, tmp_path):
    repo = tmp_path / "repo"
    test_file = _write(repo / "packages/chat/src/chat.test.ts", 'it("real upstream test", () => {});\n')
    _write(repo / "README.md", "upstream\n")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    expected = {"tag": "chat@4.31.0", "sha": _git(repo, "rev-parse", "HEAD")}
    assert vtf.verify_checkout_sha(str(repo), expected) is None
    # Edits outside packages/chat/src do not affect the fidelity count.
    _write(repo / "README.md", "edited\n")
    assert vtf.verify_checkout_sha(str(repo), expected) is None
    test_file.write_text("// pruned\n", encoding="utf-8")
    error = vtf.verify_checkout_sha(str(repo), expected)
    assert error is not None and "local changes" in error and "packages/chat/src/chat.test.ts" in error


def test_doc_drift_flags_stale_branch_and_wrapped_pinned_phrase(vtf, tmp_path):
    _write(
        tmp_path / "CLAUDE.md",
        "git clone --depth 1 --branch chat@4.30.0 \\\n  https://github.com/vercel/chat.git\n"
        "Unrelated mention of chat@4.25.0 is fine.\n",
    )
    _write(tmp_path / "docs" / "UPSTREAM_SYNC.md", "runs in CI pinned\nto `vercel/chat@4.30.0` (matches).\n")
    errors = vtf.check_docs("chat@4.31.0", repo_root=tmp_path)
    assert len(errors) == 2
    assert errors[0].startswith("CLAUDE.md:1:") and "--branch chat@4.30.0" in errors[0]
    assert errors[1].startswith("docs/UPSTREAM_SYNC.md:1:") and "pinned to `vercel/chat@4.30.0" in errors[1]


def test_doc_drift_passes_when_docs_name_the_pin(vtf, tmp_path):
    _write(tmp_path / "CLAUDE.md", "pinned to `chat@4.31.0` (matches).\n--branch chat@4.31.0\n")
    _write(tmp_path / "docs" / "UPSTREAM_SYNC.md", 'git clone --branch "$(jq -r .pin.tag pin.json)"\n')
    assert vtf.check_docs("chat@4.31.0", repo_root=tmp_path) == []


@pytest.mark.parametrize(
    "stale",
    [
        "Pinned to chat@4.30.0.",
        "PINNED TO chat@4.30.0.",
        "git clone --branch=chat@4.30.0 https://github.com/vercel/chat.git",
        "git clone -b chat@4.30.0 https://github.com/vercel/chat.git",
        "git clone --depth 1 --branch \\\n  chat@4.30.0 https://github.com/vercel/chat.git",
        "pinned to **chat@4.30.0**",
        "pinned to [chat@4.30.0](https://github.com/vercel/chat/releases)",
        "pinned to the `chat@4.30.0` tag",
        "--branch 'chat@4.30.0'",
        "pinned to vercel/chat@4.30.0",
    ],
)
def test_doc_drift_catches_stale_pin_variants(vtf, tmp_path, stale):
    _write(tmp_path / "CLAUDE.md", f"Intro.\n{stale}\n")
    _write(tmp_path / "docs" / "UPSTREAM_SYNC.md", "git checkout -b chat-fix\n")
    errors = vtf.check_docs("chat@4.31.0", repo_root=tmp_path)
    assert len(errors) == 1 and errors[0].startswith("CLAUDE.md:2:") and "chat@4.30.0" in errors[0]


def test_completeness_flags_unlisted_core_test_file(vtf, tmp_path):
    src = tmp_path / "packages" / "chat" / "src"
    _write(src / "chat.test.ts", "")
    _write(src / "history" / "brand-new.test.ts", "")
    _write(src / "jsx-react.test.tsx", "")
    _write(src / "node_modules" / "dep" / "x.test.ts", "")
    files = vtf.list_core_test_files(str(tmp_path))
    assert files == [
        "packages/chat/src/chat.test.ts",
        "packages/chat/src/history/brand-new.test.ts",
        "packages/chat/src/jsx-react.test.tsx",
    ]
    assert vtf.find_unclassified(files) == ["packages/chat/src/history/brand-new.test.ts"]


def test_mapping_tiers_are_disjoint(vtf):
    strict, target, skipped = vtf.MAPPING.keys(), vtf.TARGET_MAPPING.keys(), vtf.UNMAPPED.keys()
    assert not (strict & target) and not (strict & skipped) and not (target & skipped)


# ---------------------------------------------------------------------------
# CLI modes
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_upstream(vtf, tmp_path, monkeypatch):
    """A tiny TS tree + Python tree wired into the script's module globals."""
    ts_root = tmp_path / "ts"
    py_root = tmp_path / "py"
    _write(
        ts_root / "packages/chat/src/a.test.ts",
        'describe("A", () => {\n  it("works", () => {});\n  it.each([[1]])("handles %s", () => {});\n});\n',
    )
    _write(ts_root / "packages/chat/src/b.test.ts", 'it("new thing", () => {});\n')
    _write(py_root / "tests/test_a.py", "def test_works():\n    pass\n")
    monkeypatch.setattr(vtf, "TS_ROOT", str(ts_root))
    monkeypatch.setattr(vtf, "PY_ROOT", str(py_root))
    monkeypatch.setattr(vtf, "TARGET_REPORT_PATH", tmp_path / "fidelity_target.json")
    monkeypatch.setattr(vtf, "MAPPING", {"packages/chat/src/a.test.ts": "tests/test_a.py"})
    monkeypatch.setattr(vtf, "TARGET_MAPPING", {"packages/chat/src/b.test.ts": "tests/test_b.py"})
    monkeypatch.setattr(vtf, "UNMAPPED", {})
    monkeypatch.setattr(vtf, "resolve_checkout_sha", lambda _root: None)
    return tmp_path


def test_report_target_writes_report_and_never_fails_on_missing(vtf, fake_upstream):
    assert vtf.main(["--report-target"]) == 0
    report = json.loads((fake_upstream / "fidelity_target.json").read_text())
    assert (report["tag"], report["sha"]) == ("chat@4.41.1", _TARGET_SHA)
    assert report["totals"] == {
        "ts_tests": 3,
        "each_templates": 1,
        "matched_exact": 1,
        "matched_fuzzy": 0,
        "missing": 2,
        "extra": 0,
    }
    a = report["files"]["packages/chat/src/a.test.ts"]
    assert (a["tier"], a["missing"]) == ("strict", [["A", "handles %s"]])
    b = report["files"]["packages/chat/src/b.test.ts"]
    assert (b["tier"], b["python_exists"], b["missing"]) == ("target", False, [["", "new thing"]])


def test_target_report_dump_puts_pairs_on_one_line_and_round_trips(vtf):
    report = {
        "files": {
            "a.test.ts": {
                "missing": [['say "hi"', "back\\slash ], [x"], ["", "plain"]],
                "fuzzy_matches": [],
            }
        }
    }
    text = vtf.dump_target_report(report)
    assert json.loads(text) == report
    assert '        ["say \\"hi\\"", "back\\\\slash ], [x"],\n' in text
    assert '        ["", "plain"]\n' in text


def test_report_target_errors_on_unclassified_file_without_writing(vtf, fake_upstream):
    _write(fake_upstream / "ts/packages/chat/src/c.test.ts", 'it("x", () => {});\n')
    assert vtf.main(["--report-target"]) == 1
    assert not (fake_upstream / "fidelity_target.json").exists()


def test_strict_warns_on_unclassified_file_but_still_checks_mapping(vtf, fake_upstream, capsys):
    _write(fake_upstream / "ts/packages/chat/src/c.test.ts", 'it("x", () => {});\n')
    _write(fake_upstream / "py/tests/test_a.py", "def test_works():\n    pass\n\ndef test_handles():\n    pass\n")
    assert vtf.main(["--strict"]) == 0
    assert "c.test.ts is not in MAPPING, TARGET_MAPPING or UNMAPPED" in capsys.readouterr().err


def test_strict_fails_when_checkout_sha_differs_from_pin(vtf, fake_upstream, monkeypatch):
    _write(fake_upstream / "py/tests/test_a.py", "def test_works():\n    pass\n\ndef test_handles():\n    pass\n")
    monkeypatch.setattr(vtf, "resolve_checkout_sha", lambda _root: _TARGET_SHA)
    assert vtf.main(["--strict"]) == 1


def test_report_target_rejects_combined_modes(vtf, fake_upstream):
    assert vtf.main(["--report-target", "--strict"]) == 2
    assert vtf.main(["--check-docs", "--fix"]) == 2


def test_report_target_lists_missing_without_a_baseline_marker(vtf, fake_upstream, capsys):
    assert vtf.main(["--report-target"]) == 0
    out = capsys.readouterr().out
    assert "    MISSING: [A] handles %s\n" in out
    assert "baselined" not in out


@pytest.mark.parametrize(
    ("head", "code"),
    [(_TARGET_SHA, 0), (_PIN_SHA, 1), ("0" * 40, 1)],
    ids=["target-sha", "pin-sha", "other-sha"],
)
def test_report_target_verifies_checkout_against_target_sha(vtf, fake_upstream, monkeypatch, head, code):
    monkeypatch.setattr(vtf, "resolve_checkout_sha", lambda _root: head)
    assert vtf.main(["--report-target"]) == code
    assert (fake_upstream / "fidelity_target.json").exists() is (code == 0)


def test_report_target_rejects_file_in_both_tiers(vtf, fake_upstream, monkeypatch):
    monkeypatch.setattr(vtf, "TARGET_MAPPING", {**vtf.TARGET_MAPPING, **vtf.MAPPING})
    assert vtf.main(["--report-target"]) == 1
    assert not (fake_upstream / "fidelity_target.json").exists()


def test_report_target_prints_delta_against_the_report_committed_at_head(vtf, fake_upstream, capsys):
    _git(fake_upstream, "init", "-q")
    assert vtf.main(["--report-target"]) == 0
    _git(fake_upstream, "add", "fidelity_target.json")
    _git(fake_upstream, "commit", "-q", "-m", "baseline report")
    _write(fake_upstream / "py/tests/test_a.py", "def test_works():\n    pass\n\ndef test_handles():\n    pass\n")
    # Run twice without committing: both runs compare against HEAD's
    # report, not the working copy the first run just rewrote.
    for _ in range(2):
        capsys.readouterr()
        assert vtf.main(["--report-target"]) == 0
        out = capsys.readouterr().out
        assert "Delta vs committed report (HEAD): missing 2 -> 1 (-1)" in out
        assert "  packages/chat/src/a.test.ts: 1 -> 0 (-1)\n" in out
        assert "packages/chat/src/b.test.ts: 1 -> 1" not in out


def test_report_target_ignores_an_uncommitted_working_copy_for_the_delta(vtf, fake_upstream, capsys):
    _git(fake_upstream, "init", "-q")
    assert vtf.main(["--report-target"]) == 0  # written, never committed
    capsys.readouterr()
    assert vtf.main(["--report-target"]) == 0
    out = capsys.readouterr().out
    assert "(no committed report for this tag at HEAD — no delta)" in out
    assert "Delta vs committed report" not in out


def test_strict_fails_when_pin_and_upstream_parity_disagree(vtf, fake_upstream, monkeypatch):
    _write(fake_upstream / "py/tests/test_a.py", "def test_works():\n    pass\n\ndef test_handles():\n    pass\n")
    assert vtf.main(["--strict"]) == 0
    monkeypatch.setattr(vtf, "_read_upstream_parity", lambda: "4.41.0")
    assert vtf.main(["--strict"]) == 1


@pytest.mark.parametrize(
    ("claude_md", "code"),
    [
        ("--branch chat@4.31.0\n", 0),  # the pin
        ("--branch chat@4.30.0\n", 1),  # stale
        ("--branch chat@4.41.1\n", 1),  # the target is not the pin
    ],
    ids=["pin", "stale", "target"],
)
def test_check_docs_cli_compares_docs_against_the_pin(vtf, tmp_path, monkeypatch, claude_md, code):
    _write(tmp_path / "CLAUDE.md", claude_md)
    _write(tmp_path / "docs" / "UPSTREAM_SYNC.md", "pinned to `chat@4.31.0`\n")
    monkeypatch.setattr(vtf, "REPO_ROOT", tmp_path)
    assert vtf.main(["--check-docs"]) == code


# An upstream file with three tests the extractor cannot read, next to one it can.
_UNEXTRACTABLE = """describe("A", () => {
  it("works", () => {});
  it.each([[/[a-z/, "x"]])("rejects bad pattern %s", () => {});
  it.each([[1]])(`handles ${kind} input %s`, () => {});
  it.each([["a"]])("%s", () => {});
});
"""


def test_strict_fails_closed_on_unextractable_upstream_tests(vtf, fake_upstream, capsys):
    _write(fake_upstream / "ts/packages/chat/src/a.test.ts", _UNEXTRACTABLE)
    assert vtf.main(["--strict"]) == 1
    captured = capsys.readouterr()
    assert captured.err.count("error: ") == 3 and "a.test.ts:3: could not extract it.each title" in captured.err
    assert "3 upstream test(s) could not be extracted" in captured.out
    assert "All TS tests have Python equivalents." not in captured.out


def test_report_target_fails_closed_on_unextractable_upstream_tests(vtf, fake_upstream):
    _write(fake_upstream / "ts/packages/chat/src/a.test.ts", _UNEXTRACTABLE)
    assert vtf.main(["--report-target"]) == 1
    assert not (fake_upstream / "fidelity_target.json").exists()


def test_baseline_mode_only_warns_on_unextractable_upstream_tests(vtf, fake_upstream, monkeypatch, capsys):
    _write(fake_upstream / "ts/packages/chat/src/a.test.ts", _UNEXTRACTABLE)
    monkeypatch.setattr(vtf, "BASELINE_PATH", fake_upstream / "no_baseline.json")
    assert vtf.main([]) == 0
    err_lines = capsys.readouterr().err.splitlines()
    assert sum(1 for line in err_lines if line.startswith("warning: ") and "a.test.ts:" in line) == 3
    assert not any(line.startswith("error: ") for line in err_lines)
