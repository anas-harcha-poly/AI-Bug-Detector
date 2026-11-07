"""Tests for the validation of AI answers, which are treated as untrusted input."""

import pytest

from ai_bug_detector.ai_models import (
    AiReviewError,
    Confidence,
    parse_explanations,
    parse_logic_bugs,
)
from ai_bug_detector.models import Issue, Severity

ISSUE = Issue("F821", "Undefined name", Severity.ERROR, 3, 1)


def explanation(**changes) -> dict:
    return {
        "rule_code": "F821",
        "line": 3,
        "explanation": "It is undefined.",
        "context": "Here.",
        "suggested_fix": "x = 1",
    } | changes


def bug(**changes) -> dict:
    return {
        "line": 2,
        "title": "Wrong result",
        "reasoning": "Because.",
        "confidence": "high",
        "suggested_fix": "",
    } | changes


def test_valid_explanation_is_kept():
    review = parse_explanations({"explanations": [explanation()]}, [ISSUE])

    assert review.ignored_count == 0
    assert review.explanations[0].rule_code == "F821"
    assert review.explanations[0].suggested_fix == "x = 1"


@pytest.mark.parametrize(
    "item",
    [
        pytest.param(explanation(rule_code="F401"), id="issue-ruff-never-reported"),
        pytest.param(explanation(line=99), id="issue-on-another-line"),
        pytest.param(explanation(line="3"), id="line-as-text"),
        pytest.param(explanation(line=True), id="line-as-boolean"),
        pytest.param(explanation(line=0), id="line-zero"),
        pytest.param(explanation(explanation=""), id="empty-text"),
        pytest.param(explanation(explanation=42), id="text-as-number"),
        pytest.param(explanation(explanation="x" * 1501), id="text-too-long"),
        pytest.param(explanation(suggested_fix="x" * 4001), id="fix-too-long"),
        pytest.param({"rule_code": "F821"}, id="missing-fields"),
        pytest.param("not an object", id="not-an-object"),
    ],
)
def test_invalid_explanation_is_ignored_and_counted(item):
    review = parse_explanations({"explanations": [item, explanation()]}, [ISSUE])

    assert len(review.explanations) == 1
    assert review.ignored_count == 1


def test_valid_logic_bug_is_kept():
    review = parse_logic_bugs({"bugs": [bug()]}, line_count=2)

    assert review.bugs[0].confidence is Confidence.HIGH
    assert review.ignored_count == 0


def test_confidence_ignores_capitalization():
    review = parse_logic_bugs({"bugs": [bug(confidence="HIGH")]}, line_count=2)

    assert review.bugs[0].confidence is Confidence.HIGH


@pytest.mark.parametrize(
    "item",
    [
        pytest.param(bug(line=3), id="line-past-end-of-file"),
        pytest.param(bug(confidence="certain"), id="unknown-confidence"),
        pytest.param(bug(confidence=1), id="confidence-as-number"),
        pytest.param(bug(title="   "), id="blank-title"),
    ],
)
def test_invalid_logic_bug_is_ignored_and_counted(item):
    review = parse_logic_bugs({"bugs": [item]}, line_count=2)

    assert review.bugs == []
    assert review.ignored_count == 1


def test_items_over_the_limit_are_ignored_and_counted():
    review = parse_logic_bugs({"bugs": [bug()] * 25}, line_count=2)

    assert len(review.bugs) == 20
    assert review.ignored_count == 5


@pytest.mark.parametrize("data", [None, [], "text", {}, {"bugs": "x"}])
def test_wrong_overall_format_raises_an_error(data):
    with pytest.raises(AiReviewError, match="expected format"):
        parse_logic_bugs(data, line_count=2)
