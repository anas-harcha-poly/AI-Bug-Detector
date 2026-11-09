"""End-to-end tests of the "Suggested fixes" section: list, accept, refuse, preview.

Ruff really analyzes the code. Claude is replaced by a fake reviewer: no API call is
made. Fixes are only applied to a preview in memory, and no code is ever run.
"""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from ai_bug_detector import ai_panel
from ai_bug_detector.ai_models import (
    Confidence,
    ExplanationReview,
    LogicBug,
    LogicReview,
)
from ai_bug_detector.ai_review import LIVE_AI_VARIABLE
from ai_bug_detector.docker_sandbox import SANDBOX_VARIABLE
from ai_bug_detector.fix_panel import code_for_display

APP_PATH = Path(__file__).resolve().parents[1] / "app.py"

# Ruff offers a safe fix (F401, line 1) and an unsafe one (E711, line 4).
CODE = b"import os\n\nx = 1\nif x == None:\n    pass\n"
CLAUDE_CODE = (
    b"def total(values):\n"
    b"    result = 0\n"
    b"    for v in values:\n"
    b"        result += v\n"
    b"    return result + 1\n"
)


class FakeClaude:
    """Stands in for ClaudeReviewer: returns a prepared answer, calls nothing."""

    logic = LogicReview([], 0)

    def __init__(self, api_key, model_id):
        pass

    def explain_issues(self, source, issues):
        return ExplanationReview([], 0)

    def find_logic_bugs(self, source):
        return FakeClaude.logic


def bug(line, fix, start, end, title="A possible bug"):
    return LogicBug(line, title, "Some reasoning.", Confidence.HIGH, fix, start, end)


@pytest.fixture(autouse=True)
def environment(monkeypatch):
    monkeypatch.delenv(LIVE_AI_VARIABLE, raising=False)
    monkeypatch.delenv(SANDBOX_VARIABLE, raising=False)
    monkeypatch.setattr(ai_panel, "ClaudeReviewer", FakeClaude)
    FakeClaude.logic = LogicReview([], 0)


@pytest.fixture
def app() -> AppTest:
    return AppTest.from_file(str(APP_PATH), default_timeout=20).run()


@pytest.fixture
def live_app(monkeypatch) -> AppTest:
    monkeypatch.setenv(LIVE_AI_VARIABLE, "1")
    monkeypatch.setattr(ai_panel, "sdk_available", lambda: True)
    return AppTest.from_file(str(APP_PATH), default_timeout=20).run()


def analyze(app: AppTest, content: bytes = CODE, name: str = "code.py") -> None:
    app.file_uploader[0].set_value((name, content, "text/x-python")).run()
    app.button[0].click().run()


def ask_claude(app: AppTest, logic: LogicReview) -> None:
    FakeClaude.logic = logic
    analyze(app, CLAUDE_CODE)
    app.text_input(key="ai_api_key").set_value("fake-key").run()
    next(b for b in app.checkbox if b.label == ai_panel.CONSENT_LABEL).check().run()
    app.button(key="ai_logic").click().run()


def headers(app: AppTest) -> list[str]:
    return [c.value for c in app.caption if c.value.startswith("Fix #")]


def boxes(app: AppTest):
    return [box for box in app.checkbox if box.label == "Accept this fix"]


def accept(app: AppTest, *numbers: int) -> None:
    for number in numbers:
        boxes(app)[number - 1].check().run()


def texts(app: AppTest) -> str:
    groups = (app.caption, app.text, app.info, app.error, app.markdown, app.code)
    return "\n".join(str(item.value) for group in groups for item in group)


def diffs(app: AppTest) -> list[str]:
    return [code.value for code in app.code if code.language == "diff"]


def has_preview(app: AppTest) -> bool:
    return any("Preview with" in info.value for info in app.info)


# --- Listing fixes ---


def test_ruff_fixes_show_origin_safety_and_lines(app):
    analyze(app)

    assert headers(app) == [
        "Fix #1 · Ruff · safe · line 1",
        "Fix #2 · Ruff · unsafe · line 4",
    ]
    assert "⚠️ Ruff marks this fix as unsafe" in texts(app)
    assert "Remove unused import" in texts(app)


def test_nothing_is_accepted_by_default_not_even_safe_fixes(app):
    analyze(app)

    assert [box.value for box in boxes(app)] == [False, False]
    assert not any(box.disabled for box in boxes(app))
    assert "Accept one or more fixes to preview" in texts(app)
    assert diffs(app) == []


def test_file_without_any_fix(app):
    analyze(app, b"print(missing)\n")

    assert "No fix is available for this file." in texts(app)
    assert boxes(app) == []


@pytest.mark.parametrize("content", [b"", b"\xff\xfe"], ids=["empty", "error"])
def test_no_fix_section_when_ruff_could_not_analyze(app, content):
    analyze(app, content)

    assert not any(h.value == "Suggested fixes (optional)" for h in app.subheader)


def test_large_file_gets_no_fix_list(app):
    analyze(app, b"import os\n" + b"#" * (256 * 1024))

    assert "Fixes are offered for files up to 256 KB." in texts(app)
    assert boxes(app) == []


def test_only_the_first_50_fixes_are_listed(app):
    analyze(app, b"x = 1\n" + b"y = x == None\n" * 60)

    assert len(boxes(app)) == 50
    assert "Only the first 50 fixes are shown (10 more)." in texts(app)


# --- Accepting, refusing and previewing ---


def test_accepting_a_fix_shows_the_diff_and_the_corrected_code(app):
    analyze(app)

    accept(app, 1)

    assert has_preview(app)
    assert "-import os\n" in diffs(app)[0]
    # The blank first line stays visible, so line numbers match the real file.
    assert "\nx = 1\nif x == None:\n    pass" in [code.value for code in app.code]
    assert "Your file has not been changed" in texts(app)


def test_compatible_fixes_are_combined(app):
    analyze(app)

    accept(app, 1, 2)

    assert "\nx = 1\nif x is None:\n    pass" in [code.value for code in app.code]
    assert "Preview with 2 accepted fix(es)" in texts(app)


def test_preview_of_a_windows_file_shows_clean_lines(app):
    analyze(app, CODE.replace(b"\n", b"\r\n"))

    accept(app, 1)

    assert "\nx = 1\nif x == None:\n    pass" in [code.value for code in app.code]
    assert not any("\r" in code.value for code in app.code)


def test_display_keeps_a_blank_first_line_and_unifies_line_breaks():
    # st.code drops one line break at each end: the extra one keeps the blank line.
    assert code_for_display("\nx = 1\n") == "\n\nx = 1\n"
    assert code_for_display("x = 1\r\ny = 2\r\n") == "x = 1\ny = 2\n"
    assert code_for_display("x = 1") == "x = 1"


def test_unchecking_a_fix_refuses_it_again(app):
    analyze(app)
    accept(app, 1)

    boxes(app)[0].uncheck().run()

    assert not has_preview(app)
    assert diffs(app) == []


def test_the_uploaded_file_and_ruff_results_are_never_changed(app):
    analyze(app)
    accept(app, 1, 2)

    assert app.file_uploader[0].value.getvalue() == CODE
    rules = list(app.dataframe[0].value["Rule"])
    assert sorted(rules) == ["E711", "F401"]


# --- Conflicts ---


def test_overlapping_fixes_are_explained_and_cannot_be_applied_together(app):
    analyze(app, b"import os, sys\n")
    assert len(boxes(app)) >= 2
    assert "Changes the same lines as fix #2" in texts(app)

    accept(app, 1, 2)

    assert "cannot be applied together: fix #1 and fix #2" in app.error[-1].value
    assert not has_preview(app)
    assert diffs(app) == []


# --- Reruns and file changes ---


def test_choices_survive_a_rerun(app):
    analyze(app)
    accept(app, 1)

    app.run()

    assert boxes(app)[0].value
    assert has_preview(app)


def test_a_new_upload_with_the_same_name_drops_earlier_choices(app):
    analyze(app)
    accept(app, 1, 2)

    analyze(app)  # Same name and content, new upload.

    assert [box.value for box in boxes(app)] == [False, False]
    assert not has_preview(app)


def test_running_the_analysis_again_drops_earlier_choices(app):
    analyze(app)
    accept(app, 1)

    app.button[0].click().run()

    assert [box.value for box in boxes(app)] == [False, False]
    assert not has_preview(app)


def test_removing_the_file_removes_the_fixes(app):
    analyze(app)
    accept(app, 1)

    app.file_uploader[0].clear().run()

    assert boxes(app) == []
    assert not has_preview(app)


# --- Claude's suggestions (simulated) ---


def test_claude_fix_on_several_lines_is_listed_and_can_be_previewed(live_app):
    ask_claude(
        live_app,
        LogicReview([bug(5, "    return sum(values)", 2, 5, "Off by one")], 0),
    )

    assert headers(live_app) == ["Fix #1 · Claude · lines 2–5"]
    accept(live_app, 1)
    assert "def total(values):\n    return sum(values)" in [
        code.value for code in live_app.code
    ]


def test_ai_section_shows_every_line_a_suggestion_replaces(live_app):
    ask_claude(live_app, LogicReview([bug(5, "    return sum(values)", 2, 5)], 0))

    assert "Current code (lines 2–5)" in texts(live_app)
    assert (
        "    result = 0\n    for v in values:\n        result += v\n"
        "    return result + 1"
    ) in [code.value for code in live_app.code]


def test_ai_section_still_shows_one_line_for_older_answers(live_app):
    ask_claude(live_app, LogicReview([bug(5, "    return result", None, None)], 0))

    assert "Current code (line 5)" in texts(live_app)


def test_ai_section_flags_a_range_that_is_not_valid(live_app):
    ask_claude(live_app, LogicReview([bug(5, "    return result", 4, 9)], 0))

    assert "missing or not valid: see Suggested fixes below" in texts(live_app)


def test_suggestion_that_cannot_be_applied_stays_visible_but_locked(live_app):
    ask_claude(
        live_app,
        LogicReview([bug(3, "    pass", 5, 5, "Wrong place")], 0),
    )

    (box,) = boxes(live_app)
    assert box.disabled
    assert "Cannot be accepted:" in texts(live_app)
    assert "do not include line 3" in texts(live_app)
    assert "Wrong place" in texts(live_app)


def test_fixes_valid_alone_but_broken_together_are_refused(live_app):
    ask_claude(
        live_app,
        LogicReview(
            [
                bug(2, "    # removed", 2, 2),
                bug(5, "    # also removed", 3, 5),
            ],
            0,
        ),
    )
    accept(live_app, 1)
    assert has_preview(live_app)  # Each one alone is fine.

    accept(live_app, 2)

    assert "cannot be applied together" in live_app.error[-1].value
    assert "break the code" in texts(live_app)
    assert not has_preview(live_app)


def test_hostile_ai_text_is_never_shown_as_markdown(live_app):
    hostile = "![x](https://evil.example/a.png) [click](https://evil.example)"
    ask_claude(live_app, LogicReview([bug(5, "    return result", 5, 5, hostile)], 0))

    shown_as_text = [item.value for item in live_app.text]
    assert hostile in shown_as_text
    markdown = (live_app.markdown, live_app.caption, live_app.info, live_app.error)
    assert not any("evil.example" in str(m.value) for group in markdown for m in group)


def test_suggested_code_is_never_run_even_when_accepted(live_app, tmp_path):
    marker = tmp_path / "ran.txt"
    fix = f"    open({str(marker)!r}, 'w').close()"
    ask_claude(live_app, LogicReview([bug(2, fix, 2, 2)], 0))

    accept(live_app, 1)

    assert has_preview(live_app)
    assert not marker.exists()


def test_claude_fixes_follow_the_selected_model(live_app):
    ask_claude(live_app, LogicReview([bug(5, "    return sum(values)", 2, 5)], 0))
    assert boxes(live_app)

    live_app.selectbox(key="ai_model").set_value("claude-opus-5-5").run()

    assert boxes(live_app) == []  # No review from this model yet.


# --- The demo ---


@pytest.fixture
def demo_app(app) -> AppTest:
    app.button(key="demo_toggle").click().run()
    return app


def test_demo_lists_ruff_and_illustrative_fixes(demo_app):
    labels = headers(demo_app)

    assert len(labels) == 12  # 5 from Ruff, 7 from the demo answers.
    assert sum("Ruff · safe" in label for label in labels) == 2
    assert sum("Ruff · unsafe" in label for label in labels) == 3
    assert sum("Demo (illustrative example)" in label for label in labels) == 7
    assert any(label.endswith("lines 17–19") for label in labels)


def test_demo_ai_section_shows_all_replaced_lines(demo_app):
    assert "Current code (lines 17–19)" in texts(demo_app)


def test_demo_fixes_can_be_previewed_and_conflicts_are_shown(demo_app):
    labels = headers(demo_app)
    f821 = labels.index(next(lab for lab in labels if lab.endswith("lines 17–19"))) + 1
    discount = (
        labels.index(next(lab for lab in labels if lab.endswith("lines 17–18"))) + 1
    )

    accept(demo_app, f821)
    assert has_preview(demo_app)

    accept(demo_app, discount)
    assert "cannot be applied together" in demo_app.error[-1].value


def test_demo_and_upload_choices_are_kept_apart(demo_app):
    accept(demo_app, 1)

    analyze(demo_app)

    upload_boxes = boxes(demo_app)[:2]  # The uploaded file's section comes first.
    assert [box.value for box in upload_boxes] == [False, False]
    assert boxes(demo_app)[2].value  # The demo's first fix is still accepted.
