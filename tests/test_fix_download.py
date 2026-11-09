"""Tests for downloading the corrected code.

The real st.download_button runs, wrapped by a recorder that keeps what it was given:
the exact bytes, the file name and the options. No API is called, no code is run,
and the uploaded file is never changed.
"""

import codecs
from pathlib import Path

import pytest
import streamlit as st
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
from ai_bug_detector.fix_panel import download_bytes

APP_PATH = Path(__file__).resolve().parents[1] / "app.py"

# Ruff offers a safe fix (F401, line 1) and an unsafe one (E711, line 4).
CODE = b"import os\n\nx = 1\nif x == None:\n    pass\n"
BOTH_FIXED = b"\nx = 1\nif x is None:\n    pass\n"
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


@pytest.fixture(autouse=True)
def environment(monkeypatch):
    monkeypatch.delenv(LIVE_AI_VARIABLE, raising=False)
    monkeypatch.delenv(SANDBOX_VARIABLE, raising=False)
    monkeypatch.setattr(ai_panel, "ClaudeReviewer", FakeClaude)


@pytest.fixture
def recorded(monkeypatch) -> dict:
    """{button key: arguments of its latest call}. The real button is still made."""
    calls = {}
    real = st.download_button

    def recorder(label, data, **options):
        calls[options["key"]] = {"label": label, "data": data, **options}
        return real(label, data, **options)

    monkeypatch.setattr(st, "download_button", recorder)
    return calls


@pytest.fixture
def app(recorded) -> AppTest:
    return AppTest.from_file(str(APP_PATH), default_timeout=20).run()


def analyze(app: AppTest, content: bytes = CODE, name: str = "code.py") -> None:
    app.file_uploader[0].set_value((name, content, "text/x-python")).run()
    app.button[0].click().run()


def boxes(app: AppTest):
    return [box for box in app.checkbox if box.label == "Accept this fix"]


def accept(app: AppTest, *numbers: int) -> None:
    for number in numbers:
        boxes(app)[number - 1].check().run()


def buttons(app: AppTest):
    return app.get("download_button")


def download(app: AppTest, recorded: dict, index: int = 0) -> dict:
    """The arguments of a download button shown in the latest run."""
    shown = buttons(app)
    assert len(shown) > index, "No download button is shown."
    key = shown[index].proto.id.rsplit("-", 1)[-1]
    return recorded[key]


def preview_code(app: AppTest) -> list[str]:
    return [code.value for code in app.code if code.language == "python"]


# --- When the download is offered ---


def test_no_download_without_an_accepted_fix(app):
    analyze(app)

    assert buttons(app) == []


def test_download_appears_under_the_preview_once_a_fix_is_accepted(app, recorded):
    analyze(app)

    accept(app, 1)

    (button,) = buttons(app)
    assert button.proto.label == "Download corrected code"
    assert button.proto.ignore_rerun  # Downloading does not rerun the page.
    assert download(app, recorded)["mime"] == "text/x-python"


def test_unchecking_every_fix_removes_the_download(app):
    analyze(app)
    accept(app, 1)

    boxes(app)[0].uncheck().run()

    assert buttons(app) == []


def test_no_download_while_accepted_fixes_conflict(app):
    analyze(app, b"import os, sys\n")

    accept(app, 1, 2)

    assert "cannot be applied together" in app.error[-1].value
    assert buttons(app) == []


def test_download_comes_back_once_the_conflict_is_resolved(app, recorded):
    analyze(app, b"import os, sys\n")
    accept(app, 1, 2)

    boxes(app)[1].uncheck().run()

    assert len(buttons(app)) == 1


def test_no_download_when_accepted_fixes_break_the_code(recorded, monkeypatch):
    monkeypatch.setenv(LIVE_AI_VARIABLE, "1")
    monkeypatch.setattr(ai_panel, "sdk_available", lambda: True)
    app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
    FakeClaude.logic = LogicReview(
        [
            LogicBug(2, "a", "r", Confidence.LOW, "    # removed", 2, 2),
            LogicBug(5, "b", "r", Confidence.LOW, "    # also removed", 3, 5),
        ],
        0,
    )
    analyze(app, CLAUDE_CODE)
    app.text_input(key="ai_api_key").set_value("fake-key").run()
    next(b for b in app.checkbox if b.label == ai_panel.CONSENT_LABEL).check().run()
    app.button(key="ai_logic").click().run()

    accept(app, 1)
    assert len(buttons(app)) == 1  # One alone is valid.
    accept(app, 2)

    assert "break the code" in "\n".join(t.value for t in app.text)
    assert buttons(app) == []


# --- What is downloaded ---


def test_download_contains_only_the_accepted_fixes(app, recorded):
    analyze(app)

    accept(app, 1)

    assert download(app, recorded)["data"] == b"\nx = 1\nif x == None:\n    pass\n"


def test_download_contains_every_accepted_fix(app, recorded):
    analyze(app)

    accept(app, 1, 2)

    assert download(app, recorded)["data"] == BOTH_FIXED


def test_download_is_exactly_the_code_shown_in_the_preview(app, recorded):
    analyze(app)
    accept(app, 1, 2)

    data = download(app, recorded)["data"].decode("utf-8")

    # The preview keeps the blank first line; st.code only drops the last line break.
    shown = preview_code(app)[-1]
    assert shown.startswith("\nx = 1")
    assert data == shown + "\n"


def test_windows_line_breaks_are_kept_byte_for_byte(app, recorded):
    analyze(app, CODE.replace(b"\n", b"\r\n"))

    accept(app, 1, 2)

    assert download(app, recorded)["data"] == BOTH_FIXED.replace(b"\n", b"\r\n")


def test_missing_final_line_break_stays_missing(app, recorded):
    analyze(app, b"import os\n\nx = 1\nif x == None:\n    pass")

    accept(app, 1, 2)

    assert download(app, recorded)["data"] == b"\nx = 1\nif x is None:\n    pass"


def test_byte_order_mark_of_the_original_is_kept(app, recorded):
    analyze(app, codecs.BOM_UTF8 + CODE)

    accept(app, 1, 2)

    assert download(app, recorded)["data"] == codecs.BOM_UTF8 + BOTH_FIXED


def test_tabs_and_accents_are_kept(app, recorded):
    code = "import os\n\ndef f():\n\tmessage = 'été'\n\tif message == None:\n\t\tpass\n"

    analyze(app, code.encode("utf-8"))
    accept(app, 3)  # Fixes: 1 sorts imports, 2 removes the import, 3 is E711.

    data = download(app, recorded)["data"].decode("utf-8")
    assert data == code.replace("message == None", "message is None")


def test_download_bytes_helper():
    assert download_bytes("x = 1\r\n", keep_bom=False) == b"x = 1\r\n"
    assert download_bytes("é\n", keep_bom=True) == codecs.BOM_UTF8 + "é\n".encode()


# --- The file name ---


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("original.py", "original_fixed.py"),
        ("my script.py", "my_script_fixed.py"),
    ],
)
def test_file_name_never_replaces_the_original(app, recorded, name, expected):
    analyze(app, CODE, name=name)

    accept(app, 1)

    assert download(app, recorded)["file_name"] == expected
    assert expected != name


# --- Safety ---


def test_downloading_never_changes_the_original_or_runs_code(app, recorded, tmp_path):
    marker = tmp_path / "ran.txt"
    code = f"import os\n\nopen({str(marker)!r}, 'w').close()\n".encode()
    analyze(app, code)

    accept(app, 1)

    assert b"open(" in download(app, recorded)["data"]
    assert app.file_uploader[0].value.getvalue() == code
    assert not marker.exists()


# --- Reruns and file changes ---


def test_a_rerun_keeps_the_choices_and_the_download(app, recorded):
    analyze(app)
    accept(app, 1, 2)

    app.run()

    assert [box.value for box in boxes(app)] == [True, True]
    assert download(app, recorded)["data"] == BOTH_FIXED


def test_other_widgets_do_not_reset_the_choices(app, recorded):
    analyze(app)
    accept(app, 1, 2)

    app.button(key="demo_toggle").click().run()  # Open the demo.

    assert [box.value for box in boxes(app)[:2]] == [True, True]
    assert download(app, recorded)["data"] == BOTH_FIXED


def test_a_new_upload_drops_the_download_until_fixes_are_accepted_again(app, recorded):
    analyze(app)
    accept(app, 1, 2)

    other = b"import sys\n\ny = 2\nif y == None:\n    pass\n"
    analyze(app, other)  # Same name, different content.

    assert buttons(app) == []
    accept(app, 1)
    assert download(app, recorded)["data"] == b"\ny = 2\nif y == None:\n    pass\n"


def test_running_the_analysis_again_drops_the_download(app):
    analyze(app)
    accept(app, 1)

    app.button[0].click().run()

    assert buttons(app) == []


def test_removing_the_file_removes_the_download(app):
    analyze(app)
    accept(app, 1)

    app.file_uploader[0].clear().run()

    assert buttons(app) == []


# --- The demo ---


def test_demo_offers_its_own_download(app, recorded):
    app.button(key="demo_toggle").click().run()
    assert buttons(app) == []

    accept(app, 1)  # The demo's first fix: Ruff removes `import json`.

    data = download(app, recorded)
    assert data["file_name"] == "sample_code_fixed.py"
    assert b"import json" not in data["data"]
    assert b"import os" in data["data"]


def test_upload_and_demo_downloads_are_separate(app, recorded):
    analyze(app)
    accept(app, 1, 2)
    app.button(key="demo_toggle").click().run()
    accept(app, 3)  # The demo's first fix comes after the upload's two.

    upload, demo = download(app, recorded, 0), download(app, recorded, 1)

    assert upload["file_name"] == "code_fixed.py"
    assert upload["data"] == BOTH_FIXED
    assert demo["file_name"] == "sample_code_fixed.py"
    assert upload["key"] != demo["key"]
