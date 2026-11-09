"""Tests for Ruff's own fixes: reading them, converting them, checking and applying.

Ruff only reads the code. The fixes are applied to text in memory: nothing is ever
run, and no file is changed.
"""

import json

import pytest

from ai_bug_detector.ai_review import DemoReviewer, read_demo_source
from ai_bug_detector.fixes import (
    FixError,
    FixOrigin,
    apply_fixes,
    check_fix,
    find_conflicts,
    fixes_from_issues,
    fixes_from_review,
    prepare_fix,
)
from ai_bug_detector.models import (
    FixApplicability,
    Issue,
    RuffEdit,
    RuffFix,
    Severity,
)
from ai_bug_detector.ruff_analyzer import (
    _parse_ruff_output,
    analyze_bytes,
    analyze_file,
    analyze_source,
)

SAFE = FixApplicability.SAFE
UNSAFE = FixApplicability.UNSAFE


def ruff_fixes(source: str):
    """Analyze with the real Ruff, then convert its fixes."""
    return fixes_from_issues(source, analyze_source(source))


def only_fix(source: str, rule: str):
    fixes, _ = ruff_fixes(source)
    (found,) = [f for f in fixes if f.description.startswith(rule)]
    return found


def issue_with(*edits: tuple, applicability=SAFE) -> Issue:
    """An issue carrying a hand-made Ruff fix, for cases Ruff rarely produces."""
    return Issue(
        "X000",
        "test",
        Severity.INFO,
        1,
        1,
        RuffFix(applicability, "test fix", tuple(RuffEdit(*edit) for edit in edits)),
    )


def convert(source: str, *edits: tuple, applicability=SAFE):
    return fixes_from_issues(source, [issue_with(*edits, applicability=applicability)])


# --- Reading Ruff's fixes ---


def test_safe_fix_is_read_with_its_exact_positions():
    (issue,) = analyze_source("import os\n")

    assert issue.fix == RuffFix(
        SAFE, "Remove unused import: `os`", (RuffEdit(1, 1, 2, 1, ""),)
    )


def test_unsafe_fix_is_read_as_unsafe():
    (issue,) = analyze_source("x = 1\nif x == None:\n    pass\n")

    assert issue.fix.applicability is UNSAFE


def test_issue_without_a_fix_has_none():
    (issue,) = analyze_source("print(missing)\n")

    assert issue.rule_code == "F821"
    assert issue.fix is None


def ruff_output(fix) -> bytes:
    item = {
        "code": "F401",
        "message": "unused",
        "location": {"row": 1, "column": 1},
    }
    if fix != "absent":
        item["fix"] = fix
    return json.dumps([item]).encode()


GOOD_EDIT = {
    "content": "",
    "location": {"row": 1, "column": 1},
    "end_location": {"row": 2, "column": 1},
}


def test_display_only_fix_is_read():
    fix = {"applicability": "display-only", "message": "m", "edits": [GOOD_EDIT]}

    (issue,) = _parse_ruff_output(ruff_output(fix))

    assert issue.fix.applicability is FixApplicability.DISPLAY_ONLY


@pytest.mark.parametrize(
    "fix",
    [
        pytest.param("absent", id="no-fix-field"),
        pytest.param(None, id="null"),
        pytest.param("text", id="not-an-object"),
        pytest.param({"applicability": "maybe", "edits": [GOOD_EDIT]}, id="unknown"),
        pytest.param({"applicability": "safe", "edits": []}, id="no-edits"),
        pytest.param({"applicability": "safe", "edits": "x"}, id="edits-not-list"),
        pytest.param({"applicability": "safe"}, id="edits-missing"),
        pytest.param(
            {"applicability": "safe", "message": 5, "edits": [GOOD_EDIT]},
            id="message-not-text",
        ),
        pytest.param(
            {"applicability": "safe", "edits": [GOOD_EDIT | {"content": None}]},
            id="content-not-text",
        ),
        pytest.param(
            {
                "applicability": "safe",
                "edits": [GOOD_EDIT | {"location": {"row": 0, "column": 1}}],
            },
            id="row-zero",
        ),
        pytest.param(
            {
                "applicability": "safe",
                "edits": [GOOD_EDIT | {"location": {"row": True, "column": 1}}],
            },
            id="row-boolean",
        ),
        pytest.param(
            {
                "applicability": "safe",
                "edits": [GOOD_EDIT | {"location": {"row": "1", "column": 1}}],
            },
            id="row-text",
        ),
        pytest.param(
            {"applicability": "safe", "edits": [{"content": ""}]},
            id="edit-without-positions",
        ),
    ],
)
def test_missing_or_malformed_fix_is_dropped_but_the_issue_is_kept(fix):
    (issue,) = _parse_ruff_output(ruff_output(fix))

    assert issue.rule_code == "F401"
    assert issue.fix is None


# --- The demo file: every Ruff fix, safe or unsafe ---


@pytest.fixture(scope="module")
def demo():
    source = read_demo_source().decode("utf-8")
    return source, fixes_from_issues(source, analyze_source(source))


def test_demo_fixes_are_offered_and_marked_safe_or_unsafe(demo):
    _, (fixes, unusable) = demo

    assert unusable == []
    assert [(f.description.split(":")[0], f.applicability) for f in fixes] == [
        ("F401", SAFE),
        ("F401", SAFE),
        ("B006", UNSAFE),
        ("E711", UNSAFE),
        ("F841", UNSAFE),
    ]
    assert all(f.origin is FixOrigin.RUFF for f in fixes)


def test_unsafe_fixes_carry_a_warning_and_safe_ones_do_not(demo):
    source, (fixes, _) = demo

    for fix in fixes:
        check = check_fix(source, fix)
        assert check.applicable
        has_warning = any("unsafe" in warning for warning in check.warnings)
        assert has_warning is (fix.applicability is UNSAFE)


def test_nothing_is_applied_until_fixes_are_passed_explicitly(demo):
    source, (fixes, _) = demo

    assert read_demo_source().decode("utf-8") == source
    assert {issue.rule_code for issue in analyze_source(source)} >= {"F401", "E711"}
    assert apply_fixes(source, []).corrected == source


def test_safe_fixes_remove_their_issues(demo):
    source, (fixes, _) = demo

    result = apply_fixes(source, [f for f in fixes if f.applicability is SAFE])

    rules = {issue.rule_code for issue in analyze_source(result.corrected)}
    assert "F401" not in rules
    assert "import json" not in result.corrected
    assert "import os" not in result.corrected


def test_fix_with_two_edits_applies_both(demo):
    source, (fixes, _) = demo
    b006 = next(f for f in fixes if f.description.startswith("B006"))

    corrected = apply_fixes(source, [b006]).corrected

    assert (b006.start_line, b006.end_line) == (7, 7)
    assert (
        "def average_price(prices=None):\n"
        "    if prices is None:\n"
        "        prices = []\n"
        "    total = 0\n"
    ) in corrected
    assert "B006" not in {issue.rule_code for issue in analyze_source(corrected)}


def test_all_demo_fixes_together(demo):
    source, (fixes, _) = demo

    result = apply_fixes(source, fixes)

    rules = {issue.rule_code for issue in analyze_source(result.corrected)}
    assert rules == {"F821"}  # Ruff has no fix for an undefined name.


def test_ruff_and_demo_fixes_on_line_18_conflict(demo):
    source, (ruff, _) = demo
    content = read_demo_source()
    logic = DemoReviewer().find_logic_bugs(content.decode("utf-8"))
    demo_fixes, _ = fixes_from_review(source, logic, FixOrigin.DEMO)
    f841 = next(f for f in ruff if f.description.startswith("F841"))
    line_18 = next(
        f
        for f in demo_fixes
        if f.description == "Discount is undefined for non-members"
    )

    (conflict,) = find_conflicts([f841, line_18])
    assert set(conflict) == {f841.fix_id, line_18.fix_id}
    with pytest.raises(FixError, match="change the same lines"):
        apply_fixes(source, [f841, line_18])


def test_ruff_and_ai_fixes_on_different_lines_combine(demo):
    source, (ruff, _) = demo
    e711 = next(f for f in ruff if f.description.startswith("E711"))
    ai_fix = prepare_fix(
        source,
        fix_id="claude:1",
        origin=FixOrigin.CLAUDE,
        description="adult",
        start_line=23,
        end_line=23,
        replacement="    return age >= 18",
    )

    corrected = apply_fixes(source, [ai_fix, e711]).corrected

    assert "    if is_member is None:\n" in corrected
    assert "    return age >= 18\n" in corrected


# --- Positions, characters and line endings ---


def test_columns_count_characters_not_bytes():
    source = 's = "é😀"; t = s == None\n'

    fix = only_fix(source, "E711")

    assert apply_fixes(source, [fix]).corrected == 's = "é😀"; t = s is None\n'


def test_windows_line_endings_are_kept_by_every_fix():
    source = (
        "import sys\r\nimport os\r\n\r\nprint(sys, os)\r\n"
        "x = 1\r\nif x == None:\r\n    pass\r\n"
    )

    for fix in ruff_fixes(source)[0]:
        corrected = apply_fixes(source, [fix]).corrected
        assert "\n" not in corrected.replace("\r\n", ""), fix.description


def test_sorting_imports_rewrites_several_lines():
    source = "import sys\nimport os\n\nprint(sys, os)\n"

    corrected = apply_fixes(source, [only_fix(source, "I001")]).corrected

    assert corrected == "import os\nimport sys\n\nprint(sys, os)\n"


def test_last_line_without_line_break():
    source = "x = 1\nimport os"

    assert apply_fixes(source, [only_fix(source, "F401")]).corrected == "x = 1\n"


def test_insertion_at_the_end_of_the_file():
    (fix,), _ = convert("x = 1\n", (2, 1, 2, 1, "y = 2\n"))

    assert apply_fixes("x = 1\n", [fix]).corrected == "x = 1\ny = 2\n"


def test_two_insertions_at_one_place_keep_their_order():
    (fix,), _ = convert("x = 1\n", (1, 1, 1, 1, "a = 0\n"), (1, 1, 1, 1, "b = 0\n"))

    assert apply_fixes("x = 1\n", [fix]).corrected == "a = 0\nb = 0\nx = 1\n"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("a = (1,\n2)\n", "a = (1,2)\n"),
        ("a = (1,\r\n2)\r\n", "a = (1,2)\r\n"),
    ],
)
def test_edit_removing_a_line_break_joins_the_lines(source, expected):
    (fix,), _ = convert(source, (1, 8, 2, 1, ""))

    assert (fix.start_line, fix.end_line) == (1, 2)
    assert apply_fixes(source, [fix]).corrected == expected


def test_ruff_content_with_other_line_endings_follows_the_file():
    source = "x = 1\r\n"

    (fix,), _ = convert(source, (1, 1, 1, 1, "y = 0\n"))

    # Applied as is, Ruff's edit would mix \n and \r\n in one file.
    assert apply_fixes(source, [fix]).corrected == "y = 0\r\nx = 1\r\n"


# --- Fixes that cannot be offered ---


@pytest.mark.parametrize(
    ("source", "edit", "reason"),
    [
        ("x = 1\n", (1, 7, 1, 7, "y"), "past the end of line 1"),
        ("x = 1\r\n", (1, 7, 1, 7, "y"), "past the end of line 1"),  # Inside \r\n.
        ("x = 1\n", (3, 1, 3, 1, "y"), "outside the file"),
        ("x = 1", (2, 1, 2, 1, "y"), "outside the file"),
        ("", (1, 1, 1, 1, "y = 1\n"), "file is empty"),
        ("x = 1\n", (1, 5, 1, 2, ""), "ends before it starts"),
        ("x = 1\n", (1, 1, 1, 2, "x"), "does not change"),
    ],
)
def test_fix_with_invalid_positions_is_not_offered(source, edit, reason):
    fixes, (unusable,) = convert(source, edit)

    assert fixes == []
    assert reason in unusable.reason
    assert unusable.origin is FixOrigin.RUFF


def test_fix_with_overlapping_edits_is_not_offered():
    fixes, (unusable,) = convert("x = 123\n", (1, 5, 1, 7, "4"), (1, 6, 1, 8, "5"))

    assert fixes == []
    assert "overlap" in unusable.reason


def test_fix_that_cannot_be_written_exactly_is_not_offered():
    # Emptying a line keeps a blank line, which "replace these lines" cannot express.
    fixes, (unusable,) = convert("x = 1\ny = 2\n", (1, 1, 1, 6, ""))

    assert fixes == []
    assert "exactly" in unusable.reason


def test_display_only_fix_is_never_offered():
    source = "import os\n"

    fixes, (unusable,) = convert(
        source, (1, 1, 2, 1, ""), applicability=FixApplicability.DISPLAY_ONLY
    )

    assert fixes == []
    assert "not meant to be applied" in unusable.reason


# --- Conflicts and out-of-date fixes ---


def test_fixes_for_names_on_one_line_conflict():
    source = "import os, sys\n"
    fixes, _ = ruff_fixes(source)

    assert len(fixes) >= 2
    assert find_conflicts(fixes)
    with pytest.raises(FixError, match="change the same lines"):
        apply_fixes(source, fixes)
    for fix in fixes:  # Each one alone is fine.
        assert check_fix(source, fix).applicable


def test_import_sorting_and_import_removal_conflict():
    source = "import sys\nimport os\n\nprint(sys)\n"
    i001 = only_fix(source, "I001")
    f401 = only_fix(source, "F401")

    assert find_conflicts([i001, f401])


def test_ruff_fix_on_a_changed_file_is_refused():
    source = "import os\n"
    fix = only_fix(source, "F401")

    assert "another version" in check_fix("import os\nx = 1\n", fix).problems[0]
    with pytest.raises(FixError, match="another version"):
        apply_fixes("import os\nx = 1\n", [fix])


# --- The original is never changed and nothing runs ---


def test_analyzing_a_file_does_not_change_it(tmp_path):
    path = tmp_path / "code.py"
    path.write_bytes(b"import os\r\nx = 1\r\nif x == None:\r\n    pass\r\n")
    before = path.read_bytes()

    issues = analyze_file(path)
    fixes, _ = fixes_from_issues(path.read_bytes().decode("utf-8"), issues)

    assert path.read_bytes() == before
    assert fixes  # The fixes exist, but the file was left as it was.


def test_uploaded_bytes_and_fixes_use_the_same_text():
    content = "import os\r\nx = 1\r\n".encode("utf-8-sig")  # BOM, Windows endings.
    source = content.decode("utf-8-sig")  # The text Ruff analyzed.

    fixes, _ = fixes_from_issues(source, analyze_bytes(content))
    f401 = next(f for f in fixes if f.description.startswith("F401"))

    assert apply_fixes(source, [f401]).corrected == "x = 1\r\n"


def test_code_inserted_by_a_fix_is_never_run(tmp_path):
    marker = tmp_path / "ran.txt"
    source = "x = 1\n"
    (fix,), _ = convert(source, (1, 1, 1, 1, f"open({str(marker)!r}, 'w').close()\n"))

    check_fix(source, fix)
    corrected = apply_fixes(source, [fix]).corrected

    assert not marker.exists()
    assert corrected.startswith("open(")
