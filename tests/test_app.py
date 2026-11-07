"""End-to-end tests of the page: a simulated user uploads a file and analyzes it.

AppTest runs app.py without a browser or network, so it does not cover what happens in
the browser, such as the file picker refusing a file by itself.
"""

import subprocess
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

APP_PATH = Path(__file__).resolve().parents[1] / "app.py"

CODE_WITH_ISSUES = b"import os\n\n\ndef f():\n    return missing\n"
CLEAN_CODE = b"x = 1\n"
MAX_UPLOAD_BYTES = 5 * 1024 * 1024


@pytest.fixture
def app() -> AppTest:
    # The default 3-second timeout is too short for a slow machine.
    return AppTest.from_file(str(APP_PATH), default_timeout=15).run()


def upload(app: AppTest, content: bytes, name: str = "sample.py") -> None:
    app.file_uploader[0].set_value((name, content, "text/x-python")).run()


def upload_and_analyze(app: AppTest, content: bytes, name: str = "sample.py") -> None:
    upload(app, content, name)
    app.button[0].click().run()


def metrics(app: AppTest) -> dict[str, str]:
    return {metric.label: metric.value for metric in app.metric}


# --- Upload ---


def test_analyze_button_is_disabled_until_a_file_is_uploaded(app):
    assert app.button[0].disabled

    upload(app, CLEAN_CODE)

    assert not app.button[0].disabled


def test_uploader_accepts_python_files_only(app):
    assert app.file_uploader[0].allowed_type == [".py"]


def test_nothing_is_analyzed_before_the_button_is_clicked(app):
    upload(app, CODE_WITH_ISSUES)

    assert not app.metric
    assert not app.dataframe


# --- Results ---


def test_results_show_counters_and_table_sorted_by_severity(app):
    upload_and_analyze(app, CODE_WITH_ISSUES)

    assert not app.exception
    assert metrics(app) == {"Total": "2", "Errors": "1", "Warnings": "1", "Info": "0"}

    rows = app.dataframe[0].value
    assert list(rows.columns) == ["Severity", "Line", "Description", "Rule"]
    assert rows.iloc[0].to_dict() == {
        "Severity": "🔴 ERROR",
        "Line": 5,
        "Description": "Undefined name `missing`",
        "Rule": "F821",
    }
    assert rows.iloc[1]["Severity"] == "🟠 WARNING"
    assert rows.iloc[1]["Rule"] == "F401"


def test_syntax_error_is_reported_as_an_issue(app):
    upload_and_analyze(app, b"def f(:\n    pass\n")

    assert app.dataframe[0].value.iloc[0]["Rule"] == "invalid-syntax"
    assert not app.error


def test_results_stay_visible_when_the_page_runs_again(app):
    upload_and_analyze(app, CODE_WITH_ISSUES)

    app.run()

    assert metrics(app)["Total"] == "2"


def test_clean_file_shows_a_success_message(app):
    upload_and_analyze(app, CLEAN_CODE)

    assert "no issues" in app.success[0].value
    assert not app.dataframe


@pytest.mark.parametrize("content", [b"", b"  \n\n"])
def test_empty_file_shows_a_clear_message(app, content):
    upload_and_analyze(app, content)

    assert "empty" in app.info[0].value
    assert not app.success


# --- Old results must disappear ---


def test_new_upload_with_the_same_name_clears_old_results(app):
    upload_and_analyze(app, CODE_WITH_ISSUES, name="same.py")
    assert app.dataframe

    upload(app, CLEAN_CODE, name="same.py")

    assert not app.dataframe
    assert not app.metric

    app.button[0].click().run()

    assert not app.dataframe
    assert app.success


def test_same_name_with_same_content_still_clears_old_results(app):
    upload_and_analyze(app, CODE_WITH_ISSUES, name="same.py")

    upload(app, CODE_WITH_ISSUES, name="same.py")

    assert not app.dataframe


def test_removing_the_file_clears_results(app):
    upload_and_analyze(app, CODE_WITH_ISSUES)

    app.file_uploader[0].clear().run()

    assert not app.dataframe
    assert app.button[0].disabled


def test_old_error_disappears_after_new_upload(app):
    upload_and_analyze(app, b"\xff\xfe invalid")
    assert app.error

    upload(app, CLEAN_CODE)

    assert not app.error


# --- Errors ---


def test_non_utf8_file_shows_an_error(app):
    upload_and_analyze(app, "x = 'é'\n".encode("latin-1"))

    assert "not valid UTF-8" in app.error[0].value
    assert not app.success
    assert not app.exception


def test_ruff_failure_shows_an_error_instead_of_no_issues(app, monkeypatch):
    def failing_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 2, b"", b"Ruff is broken")

    upload(app, CLEAN_CODE)
    monkeypatch.setattr(subprocess, "run", failing_run)
    app.button[0].click().run()

    assert "Ruff is broken" in app.error[0].value
    assert not app.success
    assert not app.exception


def test_file_over_the_limit_is_refused_by_the_server(app):
    upload_and_analyze(app, b"#" * (MAX_UPLOAD_BYTES + 1))

    assert "larger than 5 MB" in app.error[0].value


def test_file_exactly_at_the_limit_is_analyzed(app):
    # Many short lines on purpose: Ruff is very slow on a single huge line.
    line = b"#" * 63 + b"\n"
    upload_and_analyze(app, line * (MAX_UPLOAD_BYTES // len(line)))

    assert not app.error
    assert not app.exception
    assert app.success


# --- Safety ---


def test_uploaded_code_is_never_executed(app, tmp_path):
    marker = tmp_path / "executed.txt"
    source = f"open({str(marker)!r}, 'w').close()\nprint(missing)\n"

    upload_and_analyze(app, source.encode("utf-8"))

    assert not marker.exists()
    assert metrics(app)["Errors"] == "1"
