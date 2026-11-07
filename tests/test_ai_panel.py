"""End-to-end tests of the AI part of the page: free mode, demo, consent and safety.

No test here can reach the API: real calls are replaced by a fake reviewer, or by a fake
response at the last step before the network.
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from streamlit.testing.v1 import AppTest

from ai_bug_detector import ai_panel
from ai_bug_detector.ai_models import (
    AiReviewError,
    ExplanationReview,
    LogicReview,
    Usage,
)
from ai_bug_detector.ai_review import LIVE_AI_VARIABLE, ClaudeReviewer

APP_PATH = Path(__file__).resolve().parents[1] / "app.py"

FAKE_KEY = "fake-key-for-tests-only"
CODE_WITH_ISSUE = b"print(missing)\n"
CLEAN_CODE = b"x = 1\n"
SONNET = "claude-sonnet-5-5"
OPUS = "claude-opus-5-5"


class FakeReviewer:
    """Stands in for ClaudeReviewer and records every call."""

    calls: list = []
    error: str | None = None
    created_with: list = []

    def __init__(self, api_key, model_id):
        FakeReviewer.created_with.append((api_key, model_id))

    def _answer(self, review):
        if FakeReviewer.error:
            raise AiReviewError(FakeReviewer.error)
        return review

    def explain_issues(self, source, issues):
        FakeReviewer.calls.append(("explain", source, issues))
        return self._answer(ExplanationReview([], 0, Usage(10, 5)))

    def find_logic_bugs(self, source):
        FakeReviewer.calls.append(("logic", source))
        return self._answer(LogicReview([], 0, Usage(10, 5)))


@pytest.fixture(autouse=True)
def default_environment(monkeypatch):
    monkeypatch.delenv(LIVE_AI_VARIABLE, raising=False)
    FakeReviewer.calls = []
    FakeReviewer.created_with = []
    FakeReviewer.error = None
    monkeypatch.setattr(ai_panel, "ClaudeReviewer", FakeReviewer)


@pytest.fixture
def app() -> AppTest:
    return AppTest.from_file(str(APP_PATH), default_timeout=15).run()


@pytest.fixture
def live_app(monkeypatch) -> AppTest:
    monkeypatch.setenv(LIVE_AI_VARIABLE, "1")
    monkeypatch.setattr(ai_panel, "sdk_available", lambda: True)
    return AppTest.from_file(str(APP_PATH), default_timeout=15).run()


def analyze(app: AppTest, content: bytes, name: str = "sample.py") -> None:
    upload(app, content, name)
    app.button[0].click().run()


def upload(app: AppTest, content: bytes, name: str = "sample.py") -> None:
    app.file_uploader[0].set_value((name, content, "text/x-python")).run()


def agree(app: AppTest, key: str = FAKE_KEY) -> None:
    app.text_input(key="ai_api_key").set_value(key).run()
    app.checkbox[0].check().run()


def all_text(app: AppTest) -> str:
    groups = (app.markdown, app.caption, app.warning, app.text, app.error, app.code)
    return "\n".join(str(item.value) for group in groups for item in group)


# --- Free mode ---


def test_free_mode_shows_ruff_results_without_asking_for_a_key(app):
    analyze(app, CODE_WITH_ISSUE)

    assert not app.exception
    assert app.dataframe
    assert not app.text_input
    assert not app.checkbox
    assert "Real AI review is off" in all_text(app)
    assert FakeReviewer.calls == []


def test_free_mode_and_demo_work_when_the_sdk_is_missing(app, monkeypatch):
    monkeypatch.setitem(sys.modules, "anthropic", None)

    analyze(app, CODE_WITH_ISSUE)
    app.button(key="demo_toggle").click().run()

    assert not app.exception
    assert app.dataframe


def test_public_default_never_reads_the_sdk_or_shows_real_ai(app, monkeypatch):
    def fail():
        raise AssertionError("The SDK must not be looked up when real AI is off.")

    monkeypatch.setattr(ai_panel, "sdk_available", fail)

    analyze(app, CODE_WITH_ISSUE)

    assert not app.exception
    assert not app.text_input


def test_live_mode_without_the_sdk_explains_how_to_install_it(monkeypatch):
    monkeypatch.setenv(LIVE_AI_VARIABLE, "1")
    monkeypatch.setattr(ai_panel, "sdk_available", lambda: False)
    app = AppTest.from_file(str(APP_PATH), default_timeout=15).run()

    analyze(app, CODE_WITH_ISSUE)

    assert "optional Anthropic SDK" in all_text(app)
    assert not app.text_input
    assert app.dataframe


@pytest.mark.parametrize(
    "content", [pytest.param(b"", id="empty"), pytest.param(b"\xff\xfe", id="error")]
)
def test_no_ai_review_when_there_is_nothing_to_review(live_app, content):
    analyze(live_app, content)

    assert not live_app.text_input


# --- Demo ---


def test_demo_is_closed_until_requested(app):
    assert not app.dataframe
    assert "not real responses" not in all_text(app)


def test_demo_is_clearly_labeled_as_illustrative(app):
    app.button(key="demo_toggle").click().run()

    assert "not real responses from Claude" in app.warning[0].value
    assert "written by hand" in app.warning[0].value
    headings = [item.value for item in app.markdown]
    assert "#### Explanations (illustrative)" in headings
    assert "#### Possible logic bugs (illustrative)" in headings


def test_demo_shows_real_ruff_results_and_illustrative_reviews(app):
    app.button(key="demo_toggle").click().run()

    assert not app.exception
    assert {metric.label: metric.value for metric in app.metric} == {
        "Total": "6",
        "Errors": "1",
        "Warnings": "4",
        "Info": "1",
    }
    text = all_text(app)
    assert "Average divides by one too many" in text
    assert "Suggested change: not applied" in text
    assert "unverified hypotheses" in text


def test_demo_needs_no_key_and_calls_nothing(app):
    app.button(key="demo_toggle").click().run()

    assert not app.text_input
    assert FakeReviewer.created_with == []


def test_demo_button_label_follows_the_state(app):
    assert app.button(key="demo_toggle").label == "Try the demo"

    app.button(key="demo_toggle").click().run()
    assert app.button(key="demo_toggle").label == "Close demo"

    app.button(key="demo_toggle").click().run()
    assert app.button(key="demo_toggle").label == "Try the demo"


def test_demo_can_be_closed(app):
    app.button(key="demo_toggle").click().run()
    app.button(key="demo_toggle").click().run()

    assert not app.dataframe


def test_demo_does_not_replace_the_results_of_an_uploaded_file(app):
    analyze(app, CODE_WITH_ISSUE)

    app.button(key="demo_toggle").click().run()

    assert len(app.dataframe) == 2
    assert app.dataframe[0].value.iloc[0]["Rule"] == "F821"


# --- Model choice ---


def test_sonnet_is_selected_by_default(live_app):
    analyze(live_app, CODE_WITH_ISSUE)

    assert live_app.selectbox(key="ai_model").value == SONNET


def test_selected_model_is_used_for_the_call(live_app):
    analyze(live_app, CODE_WITH_ISSUE)
    live_app.selectbox(key="ai_model").set_value(OPUS).run()
    agree(live_app)

    live_app.button(key="ai_logic").click().run()

    assert FakeReviewer.created_with == [(FAKE_KEY, OPUS)]


# --- Consent ---


def test_buttons_stay_disabled_without_a_key_and_consent(live_app):
    analyze(live_app, CODE_WITH_ISSUE)

    assert live_app.button(key="ai_explain").disabled
    assert live_app.button(key="ai_logic").disabled


def test_key_alone_is_not_enough(live_app):
    analyze(live_app, CODE_WITH_ISSUE)

    live_app.text_input(key="ai_api_key").set_value(FAKE_KEY).run()

    assert live_app.button(key="ai_logic").disabled


def test_consent_alone_is_not_enough(live_app):
    analyze(live_app, CODE_WITH_ISSUE)

    live_app.checkbox[0].check().run()

    assert live_app.button(key="ai_logic").disabled


def test_blank_key_is_not_accepted(live_app):
    analyze(live_app, CODE_WITH_ISSUE)

    agree(live_app, key="    ")

    assert live_app.button(key="ai_logic").disabled


def test_nothing_is_sent_until_a_button_is_clicked(live_app):
    analyze(live_app, CODE_WITH_ISSUE)

    agree(live_app)

    assert not live_app.button(key="ai_logic").disabled
    assert FakeReviewer.calls == []


def test_consent_screen_says_what_is_sent_where_and_the_cost(live_app):
    analyze(live_app, CODE_WITH_ISSUE)

    notice = live_app.warning[0].value

    assert "sent to the Anthropic API" in notice
    assert "file name is not sent" in notice
    assert "up to 30 days" in notice
    assert "$" in notice


def test_new_file_with_the_same_name_needs_new_consent(live_app):
    analyze(live_app, CODE_WITH_ISSUE, name="same.py")
    agree(live_app)

    analyze(live_app, CODE_WITH_ISSUE, name="same.py")

    assert not live_app.checkbox[0].value
    assert live_app.button(key="ai_logic").disabled


def test_changing_the_model_needs_new_consent(live_app):
    analyze(live_app, CODE_WITH_ISSUE)
    agree(live_app)

    live_app.selectbox(key="ai_model").set_value(OPUS).run()

    assert not live_app.checkbox[0].value
    assert live_app.button(key="ai_logic").disabled


# --- Calls ---


def test_one_click_makes_one_call_with_the_code(live_app):
    analyze(live_app, CODE_WITH_ISSUE)
    agree(live_app)

    live_app.button(key="ai_logic").click().run()

    assert not live_app.exception
    assert FakeReviewer.calls == [("logic", CODE_WITH_ISSUE.decode())]


def test_finished_review_cannot_be_paid_for_twice(live_app):
    analyze(live_app, CODE_WITH_ISSUE)
    agree(live_app)
    live_app.button(key="ai_logic").click().run()

    assert live_app.button(key="ai_logic").disabled
    assert not live_app.button(key="ai_explain").disabled


def test_rerunning_the_page_makes_no_new_call(live_app):
    analyze(live_app, CODE_WITH_ISSUE)
    agree(live_app)
    live_app.button(key="ai_logic").click().run()

    live_app.run()

    assert len(FakeReviewer.calls) == 1


def test_cost_is_shown_after_a_call(live_app):
    analyze(live_app, CODE_WITH_ISSUE)
    agree(live_app)

    live_app.button(key="ai_logic").click().run()

    assert "10 input and 5 output tokens" in all_text(live_app)


def test_failed_call_shows_a_message_and_is_not_retried(live_app):
    FakeReviewer.error = "The API key was rejected."
    analyze(live_app, CODE_WITH_ISSUE)
    agree(live_app)

    live_app.button(key="ai_logic").click().run()

    assert "key was rejected" in live_app.error[0].value
    assert len(FakeReviewer.calls) == 1
    assert live_app.dataframe  # Ruff results stay on screen.
    assert not live_app.exception


def test_new_file_clears_previous_ai_results(live_app):
    FakeReviewer.error = "Old error"
    analyze(live_app, CODE_WITH_ISSUE)
    agree(live_app)
    live_app.button(key="ai_logic").click().run()
    assert live_app.error

    analyze(live_app, CODE_WITH_ISSUE)

    assert not live_app.error


def test_file_without_issues_can_only_ask_for_logic_review(live_app):
    analyze(live_app, CLEAN_CODE)
    agree(live_app)

    assert live_app.button(key="ai_explain").disabled
    assert not live_app.button(key="ai_logic").disabled


# --- Limits ---


def test_file_over_30_kb_is_refused_without_truncation(live_app):
    analyze(live_app, b"x = 1\n" + b"#" * (30 * 1024))

    assert "limited to 30 KB" in live_app.warning[0].value
    assert not live_app.text_input
    assert live_app.success  # Ruff still analyzed the file.


def test_file_of_exactly_30_kb_is_accepted(live_app):
    line = b"#" * 63 + b"\n"

    analyze(live_app, line * (30 * 1024 // len(line)))

    assert live_app.text_input


def test_only_the_20_most_severe_issues_are_sent_and_the_user_is_told(live_app):
    # 25 lines with an INFO issue, then one ERROR that must be kept.
    code = b"x = 1\n" + b"y = x == None\n" * 25 + b"print(missing)\n"
    analyze(live_app, code)

    assert "20 most severe of 26 issues" in all_text(live_app)

    agree(live_app)
    live_app.button(key="ai_explain").click().run()

    sent_issues = FakeReviewer.calls[0][2]
    assert len(sent_issues) == 20
    assert sent_issues[0].rule_code == "F821"


# --- Security, with the real reviewer down to the last step before the network ---


@pytest.fixture
def sent_messages(monkeypatch):
    """Use the real ClaudeReviewer with a fake response, and record what it sends."""
    sent = []
    answer = {"data": {"bugs": []}}

    def fake_send(self, system, message, schema, effort):
        sent.append(message)
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=json.dumps(answer["data"]))],
            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        )

    monkeypatch.setattr(ai_panel, "ClaudeReviewer", ClaudeReviewer)
    monkeypatch.setattr(ClaudeReviewer, "_send", fake_send)
    return sent, answer


def test_file_name_is_never_sent(live_app, sent_messages):
    sent, _ = sent_messages
    analyze(live_app, CODE_WITH_ISSUE, name="secret_project_name.py")
    agree(live_app)

    live_app.button(key="ai_logic").click().run()

    assert len(sent) == 1
    assert "secret_project_name" not in sent[0]
    assert "print(missing)" in sent[0]


def test_key_is_not_displayed_or_stored_with_the_results(live_app, sent_messages):
    analyze(live_app, CODE_WITH_ISSUE)
    agree(live_app)

    live_app.button(key="ai_logic").click().run()

    assert FAKE_KEY not in all_text(live_app)
    assert FAKE_KEY not in repr(live_app.session_state[ai_panel.RESULTS_KEY])


def test_hostile_ai_text_is_shown_literally_and_never_as_markdown(
    live_app, sent_messages
):
    _, answer = sent_messages
    hostile = "![tracker](https://evil.example/a.png) <script>alert(1)</script>"
    answer["data"] = {
        "bugs": [
            {
                "line": 1,
                "title": hostile,
                "reasoning": hostile,
                "confidence": "high",
                "suggested_fix": hostile,
            }
        ]
    }
    analyze(live_app, CODE_WITH_ISSUE)
    agree(live_app)

    live_app.button(key="ai_logic").click().run()

    assert hostile in [item.value for item in live_app.text]
    assert hostile in [item.value for item in live_app.code]
    rendered_as_markdown = [
        item.value
        for group in (live_app.markdown, live_app.caption, live_app.warning)
        for item in group
    ]
    assert not any("evil.example" in value for value in rendered_as_markdown)


def test_ai_answer_about_a_line_that_does_not_exist_is_ignored(live_app, sent_messages):
    _, answer = sent_messages
    answer["data"] = {
        "bugs": [
            {
                "line": 999,
                "title": "Invented",
                "reasoning": "Invented",
                "confidence": "high",
                "suggested_fix": "",
            }
        ]
    }
    analyze(live_app, CODE_WITH_ISSUE)
    agree(live_app)

    live_app.button(key="ai_logic").click().run()

    assert "Invented" not in all_text(live_app)
    assert "1 part(s) of the answer failed validation" in all_text(live_app)


def test_suggested_fix_is_never_executed(live_app, sent_messages, tmp_path):
    _, answer = sent_messages
    marker = tmp_path / "fix-was-executed.txt"
    answer["data"] = {
        "bugs": [
            {
                "line": 1,
                "title": "t",
                "reasoning": "r",
                "confidence": "low",
                "suggested_fix": f"open({str(marker)!r}, 'w').close()",
            }
        ]
    }
    analyze(live_app, CODE_WITH_ISSUE)
    agree(live_app)

    live_app.button(key="ai_logic").click().run()

    assert not marker.exists()
    assert str(marker.name) in all_text(live_app)


def test_analyzed_code_is_never_executed_before_being_sent(
    live_app, sent_messages, tmp_path
):
    marker = tmp_path / "code-was-executed.txt"
    code = f"open({str(marker)!r}, 'w').close()\n".encode()
    analyze(live_app, code)
    agree(live_app)

    live_app.button(key="ai_logic").click().run()

    assert not marker.exists()
