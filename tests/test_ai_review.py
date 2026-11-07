"""Tests for reviewers, costs and project-wide secret safety. No SDK is needed here."""

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
