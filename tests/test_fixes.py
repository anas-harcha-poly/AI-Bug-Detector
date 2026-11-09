"""Tests for preparing, checking and applying suggested fixes.

No code is ever run: fixes are checked with ast.parse, which only reads the structure
of the code. The demo's real data is used to check the whole chain.
"""

import json
from dataclasses import replace
from importlib import resources

import pytest

from ai_bug_detector.ai_models import (
    Confidence,
    LogicBug,
    LogicReview,
    parse_explanations,
    parse_logic_bugs,
)
from ai_bug_detector.ai_review import DemoReviewer, read_demo_source
from ai_bug_detector.fixes import (
    MAX_REPLACEMENT_LENGTH,
    FixError,
    FixOrigin,
    apply_fixes,
    check_fix,
    corrected_file_name,
    find_conflicts,
    fixes_from_review,
    prepare_fix,
    source_hash,
    unified_diff,
)
from ai_bug_detector.ruff_analyzer import analyze_bytes, analyze_source

SOURCE = "a = 1\nb = 2\nc = 3\nd = 4\n"


def fix(source=SOURCE, start=1, end=None, replacement="a = 10", fix_id="f1"):
    return prepare_fix(
        source,
        fix_id=fix_id,
        origin=FixOrigin.CLAUDE,
        description="test fix",
        start_line=start,
        end_line=start if end is None else end,
        replacement=replacement,
    )


# --- Preparing a fix ---


def test_prepared_fix_records_the_lines_it_replaces_and_the_file():
    prepared = fix(start=2, end=3, replacement="b = 20\nc = 30")

    assert prepared.original == "b = 2\nc = 3\n"
    assert prepared.source_hash == source_hash(SOURCE)
    assert prepared.replacement == "b = 20\nc = 30"


@pytest.mark.parametrize(
    ("start", "end", "message"),
    [
        (0, 1, "not valid"),
        (-1, 1, "not valid"),
        (True, 1, "not valid"),
        ("1", 1, "not valid"),
        (3, 2, "starts after it ends"),
        (4, 5, "past the end of the file"),
    ],
)
def test_invalid_ranges_are_refused(start, end, message):
    with pytest.raises(FixError, match=message):
        fix(start=start, end=end)


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        ("x" * (MAX_REPLACEMENT_LENGTH + 1), "too long"),
        (None, "too long or not text"),
        ("a = 1\x1b[31m", "control characters"),
        ("a = 1\x00", "control characters"),
        ("a = 1", "identical"),
        ("a = 1\n", "identical"),
        ("a = 1\r\n", "identical"),
    ],
)
def test_invalid_replacements_are_refused(replacement, message):
    with pytest.raises(FixError, match=message):
        fix(replacement=replacement)


def test_tabs_and_line_breaks_are_allowed_in_a_replacement():
    assert fix(replacement="if a:\n\tb = 2").replacement == "if a:\n\tb = 2"


def test_windows_line_breaks_in_a_suggestion_are_accepted_and_normalized():
    prepared = fix(start=1, end=2, replacement="a = 10\r\nb = 20\r\n")

    assert prepared.replacement == "a = 10\nb = 20"


@pytest.mark.parametrize("character", ["\x0b", "\x0c", "\x85"])
def test_other_line_breaking_characters_are_refused(character):
    with pytest.raises(FixError, match="control characters"):
        fix(replacement=f"a = 1{character}b = 2")


# --- Turning review suggestions into fixes: the demo's real data ---


@pytest.fixture(scope="module")
def demo():
    content = read_demo_source()
    source = content.decode("utf-8")
    reviewer = DemoReviewer()
    issues = analyze_bytes(content)
    return (
        source,
        reviewer.explain_issues(source, issues),
        reviewer.find_logic_bugs(source),
    )


@pytest.fixture(scope="module")
def legacy_demo():
    """The same demo answers in the older format, without the lines to replace."""
    content = read_demo_source()
    source = content.decode("utf-8")
    data = json.loads(
        resources.files("ai_bug_detector.demo")
        .joinpath("illustrative_responses.json")
        .read_text("utf-8")
    )
    for item in data["explanations"] + data["bugs"]:
        del item["fix_start_line"], item["fix_end_line"]
    return (
        source,
        parse_explanations(data, analyze_bytes(content)),
        parse_logic_bugs(data, len(source.splitlines())),
    )


def test_demo_suggestions_name_their_lines_and_all_become_fixes(demo):
    source, explanations, logic = demo

    explanation_fixes, unusable_explanations = fixes_from_review(
        source, explanations, FixOrigin.DEMO
    )
    logic_fixes, unusable_logic = fixes_from_review(source, logic, FixOrigin.DEMO)

    assert unusable_explanations == unusable_logic == []
    assert [(f.start_line, f.end_line) for f in explanation_fixes] == [
        (17, 19),
        (7, 7),
        (15, 15),
    ]
    assert [(f.start_line, f.end_line) for f in logic_fixes] == [
        (11, 11),
        (27, 27),
        (17, 18),
        (23, 23),
    ]
    for prepared in explanation_fixes + logic_fixes:
        assert prepared.origin is FixOrigin.DEMO
        assert check_fix(source, prepared).applicable, prepared.fix_id


def test_multi_line_demo_fix_replaces_exactly_its_lines(demo):
    source, explanations, _ = demo
    f821 = fixes_from_review(source, explanations, FixOrigin.DEMO)[0][0]

    corrected = apply_fixes(source, [f821]).corrected

    assert f821.original == (
        "    if is_member:\n        discount = 0.10\n    return price * (1 - rate)\n"
    )
    assert (
        "        is_member = False\n"
        "    discount = 0.10 if is_member else 0\n"
        "    return price * (1 - discount)\n"
        "\n"
    ) in corrected
    rules = {issue.rule_code for issue in analyze_source(corrected)}
    assert not rules & {"F821", "F841"}


def test_overlapping_demo_suggestions_conflict(demo):
    source, explanations, logic = demo
    f821 = fixes_from_review(source, explanations, FixOrigin.DEMO)[0][0]
    discount = next(
        f
        for f in fixes_from_review(source, logic, FixOrigin.DEMO)[0]
        if f.description == "Discount is undefined for non-members"
    )

    assert find_conflicts([f821, discount]) == [(discount.fix_id, f821.fix_id)]
    with pytest.raises(FixError, match="change the same lines"):
        apply_fixes(source, [f821, discount])


def test_suggestions_without_a_fix_are_skipped(demo):
    source, explanations, _ = demo

    fixes, unusable = fixes_from_review(source, explanations, FixOrigin.DEMO)

    # 6 explanations: 3 without a fix, 3 with one.
    assert (len(fixes), len(unusable)) == (3, 0)


# --- Older answers, without the lines to replace, keep their earlier behaviour ---


def test_legacy_one_line_suggestions_replace_the_reported_line(legacy_demo):
    source, explanations, logic = legacy_demo

    explanation_fixes, _ = fixes_from_review(source, explanations, FixOrigin.DEMO)
    logic_fixes, _ = fixes_from_review(source, logic, FixOrigin.DEMO)

    assert [(f.start_line, f.replacement) for f in explanation_fixes] == [
        (15, "    if is_member is None:")
    ]
    assert [f.start_line for f in logic_fixes] == [27, 18, 23]


def test_legacy_multi_line_suggestions_are_never_guessed(legacy_demo):
    source, explanations, logic = legacy_demo

    _, unusable_explanations = fixes_from_review(source, explanations, FixOrigin.DEMO)
    _, unusable_logic = fixes_from_review(source, logic, FixOrigin.DEMO)

    unusable = unusable_explanations + unusable_logic
    assert [u.description.split(":")[0] for u in unusable_explanations] == [
        "F821",
        "B006",
    ]
    assert [u.description for u in unusable_logic] == [
        "Average divides by one too many"
    ]
    assert all("does not say which lines" in u.reason for u in unusable)


def test_fix_ids_are_unique_and_stable(demo):
    source, explanations, logic = demo

    first = fixes_from_review(source, logic, FixOrigin.DEMO)[0]
    second = fixes_from_review(source, logic, FixOrigin.DEMO)[0]
    ids = [f.fix_id for f in first]

    assert ids == [f.fix_id for f in second]
    assert len(set(ids)) == len(ids)


def test_suggestion_identical_to_the_code_is_unusable():
    same = LogicReview(
        [LogicBug(1, "same", "same", Confidence.LOW, "a = 1")], ignored_count=0
    )

    fixes, unusable = fixes_from_review(SOURCE, same, FixOrigin.CLAUDE)

    assert fixes == []
    assert "identical" in unusable[0].reason


def test_demo_fix_that_would_break_the_code_is_detected(legacy_demo):
    # The older demo answer for line 18 had no range, so it replaced line 18 alone.
    source, _, logic = legacy_demo
    fixes, _ = fixes_from_review(source, logic, FixOrigin.DEMO)
    line_18 = next(f for f in fixes if f.start_line == 18)

    check = check_fix(source, line_18)

    # Replacing line 18 alone leaves `if is_member:` on line 17 without a body.
    assert not check.applicable
    assert "not valid Python" in check.problems[0]


def test_corrected_demo_data_for_line_18_names_both_lines(demo):
    source, _, logic = demo
    discount = next(
        f
        for f in fixes_from_review(source, logic, FixOrigin.DEMO)[0]
        if f.description == "Discount is undefined for non-members"
    )

    assert (discount.start_line, discount.end_line) == (17, 18)
    assert discount.original == "    if is_member:\n        discount = 0.10\n"
    assert check_fix(source, discount).applicable


def test_valid_legacy_fixes_together_give_valid_code_and_fix_the_issue(legacy_demo):
    source, explanations, logic = legacy_demo
    fixes = fixes_from_review(source, explanations, FixOrigin.DEMO)[0]
    fixes += fixes_from_review(source, logic, FixOrigin.DEMO)[0]
    usable = [f for f in fixes if check_fix(source, f).applicable]

    result = apply_fixes(source, usable)

    assert [f.start_line for f in usable] == [15, 27, 23]
    assert "    if is_member is None:\n" in result.corrected
    assert "    return items[-1] if items else None\n" in result.corrected
    assert "    return age >= 18\n" in result.corrected
    rules = {issue.rule_code for issue in analyze_source(result.corrected)}
    assert "E711" not in rules  # Checked by Ruff, which does not run the code.


# --- Suggestions with a range of lines ---


def review(fix: str, start, end, line=2) -> LogicReview:
    bug = LogicBug(line, "title", "reasoning", Confidence.LOW, fix, start, end)
    return LogicReview([bug], ignored_count=0)


def fixes_for(source, fix, start, end, line=2):
    return fixes_from_review(source, review(fix, start, end, line), FixOrigin.CLAUDE)


def test_range_replaces_several_lines_with_any_number_of_lines():
    (prepared,), unusable = fixes_for(SOURCE, "b = 20\nc = 30\ne = 5", 2, 3)

    assert unusable == []
    assert (prepared.start_line, prepared.end_line) == (2, 3)
    assert apply_fixes(SOURCE, [prepared]).corrected == (
        "a = 1\nb = 20\nc = 30\ne = 5\nd = 4\n"
    )


def test_range_can_shrink_several_lines_into_one():
    (prepared,), _ = fixes_for(SOURCE, "bc = 23", 2, 3)

    assert apply_fixes(SOURCE, [prepared]).corrected == "a = 1\nbc = 23\nd = 4\n"


def test_empty_suggestion_with_a_range_is_no_fix_and_never_a_deletion():
    assert fixes_for(SOURCE, "", 2, 3) == ([], [])


@pytest.mark.parametrize(
    ("start", "end", "line", "reason"),
    [
        (2, None, 2, "only part of the lines"),
        (None, 3, 2, "only part of the lines"),
        (3, 4, 2, "do not include line 2"),
        (1, 1, 2, "do not include line 2"),
        (3, 2, 2, "starts after it ends"),
        (3, 9, 3, "past the end of the file"),
    ],
)
def test_wrong_range_is_never_replaced_by_a_guess(start, end, line, reason):
    fixes, (unusable,) = fixes_for(SOURCE, "x = 0", start, end, line)

    assert fixes == []
    assert reason in unusable.reason


def test_range_of_more_than_40_lines_is_refused_and_40_is_accepted():
    source = "".join(f"v{n} = {n}\n" for n in range(1, 61))

    too_many, (unusable,) = fixes_for(source, "x = 0", 1, 41, line=1)
    accepted, _ = fixes_for(source, "x = 0", 1, 40, line=1)

    assert too_many == []
    assert "more than 40 lines" in unusable.reason
    assert len(accepted) == 1


def test_copied_line_numbers_are_detected():
    fixes, (unusable,) = fixes_for(SOURCE, "2: b = 20", 2, 2)

    assert fixes == []
    assert "line numbers" in unusable.reason


def test_dictionary_entry_is_not_mistaken_for_a_line_number():
    source = "d = {\n    1: 'a',\n}\n"

    (prepared,), _ = fixes_for(source, "    1: 'b',", 2, 2)

    assert check_fix(source, prepared).applicable


def test_range_with_code_that_breaks_the_file_is_refused_when_checked():
    (prepared,), _ = fixes_for(SOURCE, "b = (", 2, 2)

    assert "not valid Python" in check_fix(SOURCE, prepared).problems[0]


def test_ranged_fixes_still_conflict_and_go_out_of_date():
    first = fixes_for(SOURCE, "b = 0\nc = 0", 2, 3)[0][0]
    second = fixes_for(SOURCE, "c = 9", 3, 3, line=3)[0][0]

    assert find_conflicts([first, second])
    assert not check_fix(SOURCE + "e = 5\n", first).applicable


# --- Checking a fix on its own ---


def test_valid_fix_has_no_problem():
    check = check_fix(SOURCE, fix())

    assert check.applicable
    assert check.problems == check.warnings == []


def test_fix_for_another_version_of_the_file_is_refused():
    check = check_fix(SOURCE + "e = 5\n", fix())

    assert not check.applicable
    assert "another version of the file" in check.problems[0]


def test_fix_whose_lines_changed_is_refused():
    tampered = replace(fix(), original="something else\n")

    assert "have changed" in check_fix(SOURCE, tampered).problems[0]


def test_fix_that_breaks_valid_code_is_refused():
    check = check_fix(SOURCE, fix(replacement="a = (1"))

    assert not check.applicable
    assert "not valid Python" in check.problems[0]
    assert "line 1" in check.problems[0]


def test_fix_on_a_file_that_is_already_invalid_gets_a_warning():
    broken = "a = (\nb = 2\n"

    check = check_fix(broken, fix(broken, start=2, replacement="b = 3"))

    assert check.applicable
    assert "could not be checked" in check.warnings[0]


def test_changed_indentation_gets_a_warning():
    source = "if a:\n    b = 2\nc = 3\n"

    check = check_fix(source, fix(source, start=3, replacement="    c = 3"))

    assert check.applicable
    assert "indented differently" in check.warnings[0]


# --- Conflicts ---


@pytest.mark.parametrize(
    ("first", "second"),
    [((1, 2), (2, 3)), ((2, 2), (2, 2)), ((1, 4), (2, 3)), ((3, 4), (1, 3))],
)
def test_overlapping_fixes_conflict(first, second):
    a = fix(start=first[0], end=first[1], replacement="x = 0", fix_id="A")
    b = fix(start=second[0], end=second[1], replacement="y = 0", fix_id="B")

    assert len(find_conflicts([a, b])) == 1
    with pytest.raises(FixError, match="change the same lines"):
        apply_fixes(SOURCE, [a, b])


def test_neighbouring_fixes_do_not_conflict():
    a = fix(start=1, end=2, replacement="x = 0", fix_id="A")
    b = fix(start=3, end=4, replacement="y = 0", fix_id="B")

    assert find_conflicts([a, b]) == []
    assert apply_fixes(SOURCE, [b, a]).corrected == "x = 0\ny = 0\n"


# --- Applying fixes ---


def test_nothing_accepted_means_nothing_changes():
    assert apply_fixes(SOURCE, []).corrected == SOURCE


def test_fixes_that_change_the_line_count_are_applied_correctly():
    longer = fix(start=1, replacement="a = 1\na2 = 1\na3 = 1", fix_id="A")
    deletion = fix(start=3, replacement="", fix_id="B")

    result = apply_fixes(SOURCE, [longer, deletion])

    assert result.corrected == "a = 1\na2 = 1\na3 = 1\nb = 2\nd = 4\n"
    assert result.applied == ["A", "B"]


def test_original_text_is_left_untouched():
    original = str(SOURCE)

    apply_fixes(SOURCE, [fix()])

    assert SOURCE == original


def test_same_fix_accepted_twice_is_refused():
    with pytest.raises(FixError, match="accepted twice"):
        apply_fixes(SOURCE, [fix(), fix()])


def test_out_of_date_fix_stops_everything():
    with pytest.raises(FixError, match="another version"):
        apply_fixes(SOURCE.replace("4", "5"), [fix()])


def test_fixes_valid_alone_but_broken_together_are_refused():
    source = "def f():\n    a = 1\n    b = 2\n"
    first = fix(source, start=2, replacement="", fix_id="A")
    second = fix(source, start=3, replacement="", fix_id="B")

    assert check_fix(source, first).applicable
    assert check_fix(source, second).applicable
    with pytest.raises(FixError, match="Together, the accepted fixes break the code"):
        apply_fixes(source, [first, second])


def test_windows_line_endings_are_kept():
    source = "a = 1\r\nb = 2\r\n"

    result = apply_fixes(source, [fix(source, start=1, replacement="a = 1\nz = 0")])

    assert result.corrected == "a = 1\r\nz = 0\r\nb = 2\r\n"


def test_missing_final_line_break_is_kept():
    source = "a = 1\nb = 2"

    result = apply_fixes(source, [fix(source, start=2, replacement="b = 3\nc = 4")])

    assert result.corrected == "a = 1\nb = 3\nc = 4"


def test_deleting_the_last_line_without_line_break():
    source = "a = 1\nb = 2"

    assert apply_fixes(source, [fix(source, start=2, replacement="")]).corrected == (
        "a = 1\n"
    )


def test_special_characters_do_not_shift_line_numbers():
    # \x0c and \u2028 end a line for str.splitlines(), but not for Python or Ruff.
    source = 'x = "a\x0cb\u2028c"\ny = 1\n'

    prepared = fix(source, start=2, replacement="y = 2")
    result = apply_fixes(source, [prepared])

    assert prepared.original == "y = 1\n"
    assert result.corrected == 'x = "a\x0cb\u2028c"\ny = 2\n'


# --- Safety: nothing is ever run ---


def test_suggested_code_is_never_run(tmp_path):
    marker = tmp_path / "ran.txt"
    source = "x = 1\n"
    dangerous = fix(source, replacement=f"open({str(marker)!r}, 'w').close()")

    check_fix(source, dangerous)
    corrected = apply_fixes(source, [dangerous]).corrected

    assert not marker.exists()
    assert "open(" in corrected  # Only text was produced.


def test_file_too_deeply_nested_to_check_is_handled():
    source = "x = " + "(" * 300 + "1" + ")" * 300 + "\ny = 1\n"

    check = check_fix(source, fix(source, start=2, replacement="y = 2"))

    assert check.applicable
    assert check.warnings


# --- Preview and download name ---


def test_preview_shows_the_change_as_a_diff():
    result = apply_fixes(SOURCE, [fix()])

    diff = unified_diff(SOURCE, result.corrected, "calc.py")

    assert diff.startswith("--- calc.py\n+++ calc_fixed.py\n")
    assert "-a = 1\n+a = 10\n" in diff


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("calc.py", "calc_fixed.py"),
        ("my script.py", "my_script_fixed.py"),
        ("../../evil.py", "evil_fixed.py"),
        ("C:\\Users\\x\\code.py", "code_fixed.py"),
        ("", "code_fixed.py"),
        ("é$%.py", "____fixed.py"),
        ("a" * 300 + ".py", "a" * 100 + "_fixed.py"),
    ],
)
def test_download_name_is_safe_and_never_the_original(name, expected):
    assert corrected_file_name(name) == expected
