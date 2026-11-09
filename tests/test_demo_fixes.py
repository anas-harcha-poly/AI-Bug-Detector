"""Tests that the demo's hand-written fixes are consistent and really fix the bugs.

Only our own demo code is run here: the sample file shipped with the project, and its
corrected version below. Code uploaded by a user is never run.
"""

import json
from importlib import resources

import pytest

from ai_bug_detector.ai_review import DemoReviewer, read_demo_source
from ai_bug_detector.fixes import (
    FixOrigin,
    apply_fixes,
    fixes_from_issues,
    fixes_from_review,
)
from ai_bug_detector.models import FixApplicability
from ai_bug_detector.ruff_analyzer import analyze_bytes, analyze_source

# The demo file once every suggested fix has been applied.
FIXED_SAMPLE = '''\
"""Small shopping cart helpers: a demo file with deliberate bugs."""


def average_price(prices=None):
    if prices is None:
        prices = []
    total = 0
    for price in prices:
        total += price
    if not prices:
        return 0.0
    return total / len(prices)


def apply_discount(price, is_member):
    if is_member is None:
        is_member = False
    discount = 0.10 if is_member else 0
    return price * (1 - discount)


def is_adult(age):
    return age >= 18


def last_item(items):
    return items[-1] if items else None
'''


def load_functions(source: str) -> dict:
    """Run our own demo code and return the functions it defines."""
    namespace: dict = {}
    exec(compile(source, "<demo>", "exec"), namespace)
    return namespace


def suggested_fixes() -> list[str]:
    text = resources.files("ai_bug_detector.demo").joinpath(
        "illustrative_responses.json"
    )
    data = json.loads(text.read_text("utf-8"))
    items = data["explanations"] + data["bugs"]
    return [item["suggested_fix"] for item in items if item["suggested_fix"]]


@pytest.fixture(scope="module")
def original():
    return load_functions(read_demo_source().decode("utf-8"))


@pytest.fixture(scope="module")
def fixed():
    return load_functions(FIXED_SAMPLE)


# --- Consistency of the examples ---


@pytest.mark.parametrize("fix", suggested_fixes())
def test_each_suggested_fix_appears_as_is_in_the_fixed_file(fix):
    # Exact text, indentation included: a fix pasted as shown must fit the file.
    assert fix in FIXED_SAMPLE


def demo_fixes_without_overlap() -> tuple[str, list]:
    """The demo's own fixes, prepared with their line ranges.

    Two of them change lines 17 and 18. The explanation of line 19 rewrites both
    lines, so the logic fix for line 18 is left out, as a user would do.
    """
    content = read_demo_source()
    source = content.decode("utf-8")
    reviewer = DemoReviewer()
    explanations = reviewer.explain_issues(source, analyze_bytes(content))
    logic = reviewer.find_logic_bugs(source)
    fixes = fixes_from_review(source, explanations, FixOrigin.DEMO)[0]
    fixes += [
        fix
        for fix in fixes_from_review(source, logic, FixOrigin.DEMO)[0]
        if fix.description != "Discount is undefined for non-members"
    ]
    return source, fixes


def test_demo_fixes_applied_with_their_ranges_give_the_fixed_file():
    source, fixes = demo_fixes_without_overlap()

    corrected = apply_fixes(source, fixes).corrected

    # The demo answers do not remove the unused imports: Ruff's fixes do that.
    with_imports = FIXED_SAMPLE.replace(
        '"""\n\n\ndef', '"""\n\nimport json\nimport os\n\n\ndef', 1
    )
    assert corrected == with_imports


def test_demo_and_safe_ruff_fixes_together_leave_no_issue():
    source, demo_fixes = demo_fixes_without_overlap()
    ruff_fixes, _ = fixes_from_issues(source, analyze_source(source))
    safe = [fix for fix in ruff_fixes if fix.applicability is FixApplicability.SAFE]

    corrected = apply_fixes(source, demo_fixes + safe).corrected

    assert analyze_source(corrected) == []
    functions = load_functions(corrected)  # Our own demo code only.
    assert functions["average_price"]([10, 20]) == 15
    assert functions["apply_discount"](100, False) == 100
    assert functions["last_item"]([]) is None


def test_fixed_file_has_no_ruff_issues():
    assert analyze_source(FIXED_SAMPLE) == []


# --- The demo bugs are real in the original file ---


def test_original_average_is_wrong(original):
    assert original["average_price"]([10, 20]) == 10


def test_original_discount_crashes(original):
    with pytest.raises(NameError):
        original["apply_discount"](100, True)


def test_original_is_adult_rejects_18(original):
    assert original["is_adult"](18) is False


def test_original_last_item_crashes(original):
    with pytest.raises(IndexError):
        original["last_item"]([1, 2, 3])


# --- The fixes work on normal cases and edge cases ---


@pytest.mark.parametrize(
    ("prices", "expected"),
    [([10, 20], 15), ([5], 5), ([1.5, 2.5], 2.0), ([], 0.0)],
)
def test_fixed_average(fixed, prices, expected):
    assert fixed["average_price"](prices) == pytest.approx(expected)


def test_fixed_average_without_argument_returns_zero(fixed):
    assert fixed["average_price"]() == 0.0


def test_fixed_average_does_not_share_its_default_between_calls(fixed):
    fixed["average_price"]()

    assert fixed["average_price"].__defaults__ == (None,)


@pytest.mark.parametrize(
    ("is_member", "expected"),
    [(True, 90), (False, 100), (None, 100)],
)
def test_fixed_discount(fixed, is_member, expected):
    assert fixed["apply_discount"](100, is_member) == pytest.approx(expected)


def test_fixed_discount_of_zero_price(fixed):
    assert fixed["apply_discount"](0, True) == 0


@pytest.mark.parametrize(
    ("age", "expected"), [(17, False), (18, True), (19, True), (0, False)]
)
def test_fixed_is_adult(fixed, age, expected):
    assert fixed["is_adult"](age) is expected


@pytest.mark.parametrize(
    ("items", "expected"),
    [([1, 2, 3], 3), (["a"], "a"), ([], None), ((4, 5), 5)],
)
def test_fixed_last_item(fixed, items, expected):
    assert fixed["last_item"](items) == expected
