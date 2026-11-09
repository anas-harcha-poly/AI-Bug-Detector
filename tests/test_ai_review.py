"""Tests for reviewers, costs and project-wide secret safety. No SDK is needed here."""

import json
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_bug_detector.ai_models import AiReviewError, Usage
from ai_bug_detector.ai_review import (
    AI_MODELS,
    LIVE_AI_VARIABLE,
    MAX_OUTPUT_TOKENS,
    ClaudeReviewer,
    DemoReviewer,
    actual_cost,
    estimate_max_cost,
    live_ai_enabled,
    read_demo_source,
)
from ai_bug_detector.ruff_analyzer import analyze_bytes

PROJECT_ROOT = Path(__file__).resolve().parents[1]
API_KEY_PATTERN = re.compile(rb"sk-ant-[A-Za-z0-9_-]{8,}")
SOURCE = "x = 1\n"


def response(text='{"bugs": []}', stop_reason="end_turn"):
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=1000, output_tokens=500),
    )


def reviewer_returning(monkeypatch, fake_response) -> ClaudeReviewer:
    reviewer = ClaudeReviewer("fake-key", "claude-sonnet-5-5")
    monkeypatch.setattr(reviewer, "_send", lambda *args: fake_response)
    return reviewer


# --- Models and costs ---


def test_sonnet_is_the_default_and_opus_and_haiku_can_be_selected():
    assert [model.model_id for model in AI_MODELS] == [
        "claude-sonnet-5-5",
        "claude-opus-5-5",
        "claude-haiku-5-5",
    ]


@pytest.mark.parametrize(
    ("model_id", "expected"),
    [
        ("claude-sonnet-5-5", 12.0),
        ("claude-opus-5-5", 24.0),
        ("claude-haiku-5-5", 0.6),
    ],
)
def test_cost_uses_each_model_price(model_id, expected):
    usage = Usage(1_000_000, 1_000_000)

    assert actual_cost(usage, model_id) == pytest.approx(expected)


def test_estimate_includes_the_output_limit_and_the_message():
    estimate = estimate_max_cost("x" * 2000, "", "claude-sonnet-5-5")

    assert estimate == pytest.approx((1000 * 2 + MAX_OUTPUT_TOKENS * 10) / 1_000_000)


# --- Real AI is off by default ---


@pytest.mark.parametrize("value", [None, "", "0", "true", "yes"])
def test_live_ai_stays_off_without_the_exact_setting(monkeypatch, value):
    monkeypatch.delenv(LIVE_AI_VARIABLE, raising=False)
    if value is not None:
        monkeypatch.setenv(LIVE_AI_VARIABLE, value)

    assert not live_ai_enabled()


def test_live_ai_turns_on_with_the_exact_setting(monkeypatch):
    monkeypatch.setenv(LIVE_AI_VARIABLE, "1")

    assert live_ai_enabled()


# --- Demo ---


def test_demo_works_with_real_ruff_results_and_nothing_is_ignored():
    content = read_demo_source()
    issues = analyze_bytes(content)
    reviewer = DemoReviewer()

    explanations = reviewer.explain_issues(content.decode(), issues)
    logic = reviewer.find_logic_bugs(content.decode())

    assert explanations.ignored_count == logic.ignored_count == 0
    assert {(e.rule_code, e.line) for e in explanations.explanations} == {
        (issue.rule_code, issue.line) for issue in issues
    }
    assert len(logic.bugs) == 4


def test_demo_has_no_usage_because_no_api_is_called():
    content = read_demo_source()

    assert DemoReviewer().find_logic_bugs(content.decode()).usage is None


def test_demo_sample_shows_all_three_severities():
    severities = {issue.severity for issue in analyze_bytes(read_demo_source())}

    assert len(severities) == 3


def test_demo_works_when_the_sdk_is_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "anthropic", None)

    assert DemoReviewer().find_logic_bugs(read_demo_source().decode()).bugs


# --- Reading Claude's response ---


def test_valid_response_is_parsed_and_usage_is_kept(monkeypatch):
    fake = response(
        '{"bugs": [{"line": 1, "title": "t", "reasoning": "r", '
        '"confidence": "low", "suggested_fix": ""}]}'
    )

    review = reviewer_returning(monkeypatch, fake).find_logic_bugs(SOURCE)

    assert len(review.bugs) == 1
    assert review.usage == Usage(1000, 500)


@pytest.mark.parametrize(
    ("fake", "message"),
    [
        pytest.param(response(stop_reason="refusal"), "declined", id="refusal"),
        pytest.param(response(stop_reason="max_tokens"), "cut off", id="cut-off"),
        pytest.param(response("not json"), "not valid JSON", id="invalid-json"),
        pytest.param(response("[1, 2]"), "expected format", id="wrong-format"),
        pytest.param(
            SimpleNamespace(stop_reason="end_turn", content=[], usage=None),
            "not valid JSON",
            id="no-text-block",
        ),
    ],
)
def test_bad_response_raises_a_clear_error(monkeypatch, fake, message):
    with pytest.raises(AiReviewError, match=message):
        reviewer_returning(monkeypatch, fake).find_logic_bugs(SOURCE)


def test_missing_sdk_raises_a_clear_error_instead_of_crashing(monkeypatch):
    monkeypatch.setitem(sys.modules, "anthropic", None)

    with pytest.raises(AiReviewError, match="SDK is not installed"):
        ClaudeReviewer("fake-key", "claude-sonnet-5-5").find_logic_bugs(SOURCE)


# --- Secrets ---


def test_no_api_key_is_present_in_project_files():
    listed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert listed

    offenders = [
        name
        for name in listed
        if (PROJECT_ROOT / name).is_file()
        and API_KEY_PATTERN.search((PROJECT_ROOT / name).read_bytes())
    ]

    assert offenders == []


@pytest.mark.parametrize("name", [".env", ".streamlit/secrets.toml"])
def test_secret_files_are_ignored_by_git(name):
    result = subprocess.run(
        ["git", "check-ignore", name], cwd=PROJECT_ROOT, capture_output=True
    )

    assert result.returncode == 0


# --- A simulated Claude answer with ranges, down to the corrected code ---


def test_simulated_claude_fix_on_several_lines_can_be_applied(monkeypatch):
    from ai_bug_detector.fixes import (
        FixOrigin,
        apply_fixes,
        check_fix,
        fixes_from_review,
    )

    source = (
        "def total(values):\n"
        "    result = 0\n"
        "    for v in values:\n"
        "        result += v\n"
        "    return result + 1\n"
    )
    answer = json.dumps(
        {
            "bugs": [
                {
                    "line": 5,
                    "title": "Off by one",
                    "reasoning": "total([]) returns 1 instead of 0.",
                    "confidence": "high",
                    "suggested_fix": "    return sum(values)",
                    "fix_start_line": 2,
                    "fix_end_line": 5,
                }
            ]
        }
    )
    review = reviewer_returning(monkeypatch, response(answer)).find_logic_bugs(source)

    (fix,), unusable = fixes_from_review(source, review, FixOrigin.CLAUDE)

    assert unusable == []
    assert check_fix(source, fix).applicable
    assert apply_fixes(source, [fix]).corrected == (
        "def total(values):\n    return sum(values)\n"
    )


# --- Spending limit, without any network ---


@pytest.fixture
def no_spending(monkeypatch):
    from ai_bug_detector.ai_review import SPEND_LIMIT_VARIABLE, SPENDING

    SPENDING.reset()
    monkeypatch.delenv(SPEND_LIMIT_VARIABLE, raising=False)
    yield SPENDING
    SPENDING.reset()


def test_reservations_and_costs_add_up_to_the_limit(no_spending):
    tracker = no_spending

    assert tracker.reserve(0.6, limit=1.0)
    assert not tracker.reserve(0.5, limit=1.0)  # 0.6 already reserved.
    tracker.settle(0.6, cost=0.1)
    assert (tracker.spent, tracker.reserved) == (pytest.approx(0.1), 0)
    assert tracker.reserve(0.5, limit=1.0)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, 1.0),
        ("", 1.0),
        ("5", 5.0),
        ("0.25", 0.25),
        ("abc", 0.0),
        ("-1", 0.0),
        ("nan", 0.0),
        ("inf", 0.0),
    ],
)
def test_spending_limit_setting(no_spending, monkeypatch, value, expected):
    from ai_bug_detector.ai_review import SPEND_LIMIT_VARIABLE, spend_limit

    if value is not None:
        monkeypatch.setenv(SPEND_LIMIT_VARIABLE, value)

    assert spend_limit() == expected


def test_reviewer_over_the_limit_sends_nothing(no_spending, monkeypatch):
    from ai_bug_detector.ai_review import SPEND_LIMIT_VARIABLE

    monkeypatch.setenv(SPEND_LIMIT_VARIABLE, "0")
    reviewer = ClaudeReviewer("fake-key", "claude-sonnet-5-5")

    def must_not_send(*args):
        raise AssertionError("Nothing may be sent over the limit.")

    monkeypatch.setattr(reviewer, "_send", must_not_send)

    with pytest.raises(AiReviewError, match="Nothing was sent"):
        reviewer.find_logic_bugs(SOURCE)


# --- Claude sees the same line numbers as the fixes use ---


def test_lines_are_numbered_like_python_even_with_special_characters(monkeypatch):
    from ai_bug_detector.ai_prompts import build_logic_bug_message
    from ai_bug_detector.fixes import FixOrigin, apply_fixes, fixes_from_review

    # \x0c and \u2028 end a line for str.splitlines(), but not for Python or Ruff.
    source = 'note = "a\x0cb\u2028c"\ndef f():\n    return 1 + 1\n'
    numbered = json.loads(build_logic_bug_message(source))["numbered_code"]
    assert numbered[2] == "3:     return 1 + 1"

    # A simulated answer using the line numbers Claude was shown.
    answer = (
        '{"bugs": [{"line": 3, "title": "t", "reasoning": "r", "confidence": "low",'
        ' "suggested_fix": "    return 2", "fix_start_line": 3, "fix_end_line": 3}]}'
    )
    reviewer = reviewer_returning(monkeypatch, response(answer))
    (fix,), _ = fixes_from_review(
        source, reviewer.find_logic_bugs(source), FixOrigin.CLAUDE
    )

    assert apply_fixes(source, [fix]).corrected == (
        'note = "a\x0cb\u2028c"\ndef f():\n    return 2\n'
    )
