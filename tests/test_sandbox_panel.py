"""End-to-end tests of the "run your tests" part of the page, with a fake sandbox.

No container is started here: run_tests and the Docker check are replaced by fakes
that record every call. The real sandbox is tested in the tests marked `docker`.
"""

import time
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from ai_bug_detector import sandbox_panel
from ai_bug_detector.ai_review import LIVE_AI_VARIABLE
from ai_bug_detector.docker_sandbox import (
    SANDBOX_VARIABLE,
    STATUS_MESSAGES,
    SandboxCleanupError,
    SandboxError,
    SandboxStatus,
)
from ai_bug_detector.sandbox_models import (
    RunStatus,
    TestCaseResult,
    TestOutcome,
    TestRunResult,
)

APP_PATH = Path(__file__).resolve().parents[1] / "app.py"

CODE = b"def add(a, b):\n    return a + b + 1\n"
TESTS = b"from calc import add\n\ndef test_add():\n    assert add(2, 2) == 4\n"


def case(name="test_calc.py::test_add", outcome="passed", message="", duration=0.5):
    return TestCaseResult(name, TestOutcome(outcome), duration, message)


MIXED_RESULT = TestRunResult(
    RunStatus.COMPLETED,
    (
        case("test_calc.py::test_ok", duration=0.25),
        case("test_calc.py::test_add", "failed", "assert 5 == 4", 0.5),
        case("test_calc.py::test_setup", "error", "RuntimeError: setup", 0.25),
        case("test_calc.py::test_later", "skipped", "Skipped: later", 0.0),
    ),
)


class FakeSandbox:
    """Stands in for run_tests and the Docker check, and records every call."""

    def __init__(self):
        self.status = SandboxStatus.READY
        self.status_checks = 0
        self.calls = []
        self.answer = MIXED_RESULT

    def sandbox_status(self):
        self.status_checks += 1
        return self.status

    def run_tests(self, code_name, code, test_name, tests):
        self.calls.append((code_name, code, test_name, tests))
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


@pytest.fixture
def sandbox(monkeypatch):
    fake = FakeSandbox()
    monkeypatch.delenv(LIVE_AI_VARIABLE, raising=False)
    monkeypatch.setenv(SANDBOX_VARIABLE, "1")
    monkeypatch.setattr(sandbox_panel, "sandbox_status", fake.sandbox_status)
    monkeypatch.setattr(sandbox_panel, "run_tests", fake.run_tests)
    st.cache_data.clear()
    yield fake
    st.cache_data.clear()


@pytest.fixture
def app(sandbox) -> AppTest:
    return AppTest.from_file(str(APP_PATH), default_timeout=15).run()


def analyze(app: AppTest, content: bytes = CODE, name: str = "calc.py") -> None:
    app.file_uploader[0].set_value((name, content, "text/x-python")).run()
    app.button[0].click().run()


def upload_tests(app: AppTest, content: bytes = TESTS, name="test_calc.py") -> None:
    uploader_for_tests(app).set_value((name, content, "text/x-python")).run()


def uploader_for_tests(app: AppTest):
    return app.file_uploader(key=sandbox_panel.TEST_FILE_KEY)


def consent_box(app: AppTest):
    (box,) = [box for box in app.checkbox if box.label.startswith("I agree to run")]
    return box


def run_button(app: AppTest):
    return app.button(key=sandbox_panel.RUN_BUTTON_KEY)


def prepare_and_run(app: AppTest) -> None:
    analyze(app)
    upload_tests(app)
    consent_box(app).check().run()
    run_button(app).click().run()


def texts(app: AppTest) -> str:
    groups = (
        app.markdown,
        app.caption,
        app.warning,
        app.error,
        app.info,
        app.success,
        app.text,
        app.code,
    )
    return "\n".join(str(item.value) for group in groups for item in group)


def metrics(app: AppTest) -> dict[str, str]:
    return {metric.label: metric.value for metric in app.metric}


# --- Off by default, and Ruff and the demo still work ---


def test_sandbox_is_off_by_default_and_never_checks_docker(sandbox, monkeypatch):
    monkeypatch.delenv(SANDBOX_VARIABLE)
    app = AppTest.from_file(str(APP_PATH), default_timeout=15).run()

    analyze(app)

    assert "Ruff found no issues" in texts(app)  # Ruff results.
    assert len(app.file_uploader) == 1
    assert "Running tests is off" in texts(app)
    assert sandbox.status_checks == 0
    assert sandbox.calls == []


def test_demo_still_works_with_the_sandbox_on(app):
    app.button(key="demo_toggle").click().run()

    assert not app.exception
    assert "not real responses from Claude" in texts(app)


def test_header_explains_what_runs_and_what_does_not(app):
    header = app.caption[0].value

    assert "Static analysis reads your code without running it" in header
    assert "only after you confirm" in header
    assert "never executed" not in header


@pytest.mark.parametrize(
    "status",
    [
        SandboxStatus.DOCKER_NOT_FOUND,
        SandboxStatus.ENGINE_UNAVAILABLE,
        SandboxStatus.NOT_LINUX,
        SandboxStatus.IMAGE_MISSING,
    ],
)
def test_missing_requirement_is_explained_and_nothing_can_run(sandbox, app, status):
    sandbox.status = status

    analyze(app)

    assert STATUS_MESSAGES[status] in texts(app)
    assert len(app.file_uploader) == 1  # No test uploader.
    assert "Ruff found no issues" in texts(app)  # Ruff results stay.
    assert sandbox.calls == []


@pytest.mark.parametrize(
    "content", [pytest.param(b"", id="empty"), pytest.param(b"\xff\xfe", id="error")]
)
def test_no_test_section_when_ruff_could_not_analyze(app, content):
    analyze(app, content)

    assert len(app.file_uploader) == 1


# --- Nothing runs without an explicit confirmation ---


def test_uploading_a_test_file_runs_nothing(sandbox, app):
    analyze(app)

    upload_tests(app)

    assert sandbox.calls == []
    assert run_button(app).disabled
    assert not consent_box(app).value


def test_warning_explains_the_risk_before_any_run(app):
    analyze(app)
    upload_tests(app)

    warning = next(w.value for w in app.warning if "Running tests executes" in w.value)

    assert "Docker container" in warning
    assert "no network" in warning
    assert "no isolation is perfect" in warning
    assert "Nothing runs until" in warning


def test_confirmation_names_both_files(app):
    analyze(app)
    upload_tests(app)

    assert consent_box(app).label == (
        "I agree to run `calc.py` and `test_calc.py` in the sandbox."
    )


def test_consent_alone_runs_nothing(sandbox, app):
    analyze(app)
    upload_tests(app)

    consent_box(app).check().run()

    assert not run_button(app).disabled
    assert sandbox.calls == []


def test_one_click_runs_the_two_files_once(sandbox, app):
    prepare_and_run(app)

    assert sandbox.calls == [("calc.py", CODE, "test_calc.py", TESTS)]


def test_reruns_never_run_the_tests_again(sandbox, app):
    prepare_and_run(app)

    app.run()
    app.run()

    assert len(sandbox.calls) == 1
    assert metrics(app)["Failed"] == "1"  # The result is kept.


def test_running_again_needs_another_click(sandbox, app):
    prepare_and_run(app)

    run_button(app).click().run()

    assert len(sandbox.calls) == 2


def test_analyzing_again_does_not_run_the_tests(sandbox, app):
    prepare_and_run(app)

    app.button[0].click().run()

    assert len(sandbox.calls) == 1


# --- Results and consent belong to the exact two files ---


def test_new_code_file_with_the_same_name_resets_consent_and_result(sandbox, app):
    prepare_and_run(app)

    analyze(app)  # Same name, new upload.
    upload_tests(app, TESTS)  # Back to the test section after analysis.

    assert not consent_box(app).value
    assert "Failed" not in metrics(app)
    assert run_button(app).disabled


def test_new_test_file_resets_consent_and_result(sandbox, app):
    prepare_and_run(app)

    upload_tests(app, TESTS + b"\n")

    assert not consent_box(app).value
    assert "Failed" not in metrics(app)
    assert len(sandbox.calls) == 1


def test_removing_the_test_file_clears_the_result(app):
    prepare_and_run(app)

    uploader_for_tests(app).clear().run()

    assert "Failed" not in metrics(app)
    assert not any(b.label.startswith("I agree to run") for b in app.checkbox)


# --- Displaying results ---


def test_results_show_counters_duration_and_a_table(app):
    prepare_and_run(app)

    assert metrics(app) == {
        "Passed": "1",
        "Failed": "1",
        "Errors": "1",
        "Other": "1",
        "Duration": "1.00 s",
    }
    table = app.dataframe[-1].value
    assert list(table.columns) == ["Result", "Test", "Duration (s)"]
    assert len(table) == 4
    assert "Some tests failed" in texts(app)


def test_failure_messages_are_shown_as_literal_text(app):
    prepare_and_run(app)

    codes = [item.value for item in app.code]
    assert "assert 5 == 4" in codes
    assert "RuntimeError: setup" in codes


def test_results_are_never_presented_as_a_guarantee(sandbox, app):
    sandbox.answer = TestRunResult(RunStatus.COMPLETED, (case(),))

    prepare_and_run(app)

    assert "All 1 tests ran without failure." in texts(app)
    assert "do not prove that the code is correct or safe" in texts(app)
    assert "the tested code could influence them" in texts(app)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (RunStatus.TIMED_OUT, "stopped after 30 seconds"),
        (RunStatus.MEMORY_LIMIT, "more memory than allowed"),
        (RunStatus.OUTPUT_LIMIT, "more output than allowed"),
    ],
)
def test_stopped_runs_are_explained(sandbox, app, status, expected):
    sandbox.answer = TestRunResult(status)

    prepare_and_run(app)

    assert expected in app.error[-1].value
    assert "container was removed" in app.error[-1].value


def test_no_tests_found_is_explained(sandbox, app):
    sandbox.answer = TestRunResult(RunStatus.NO_TESTS)

    prepare_and_run(app)

    assert "pytest found no tests" in texts(app)


def test_pytest_error_shows_its_details(sandbox, app):
    sandbox.answer = TestRunResult(
        RunStatus.PYTEST_ERROR,
        (case("test_calc.py", "error", "SyntaxError: invalid syntax", 0.0),),
    )

    prepare_and_run(app)

    assert "pytest could not run the tests" in texts(app)
    assert "SyntaxError: invalid syntax" in [item.value for item in app.code]


def test_ignored_results_are_mentioned(sandbox, app):
    sandbox.answer = TestRunResult(RunStatus.COMPLETED, (case(),), ignored_count=2)

    prepare_and_run(app)

    assert "2 reported test(s) failed validation" in texts(app)


def test_hostile_test_names_and_messages_are_never_markdown(sandbox, app):
    hostile = "![x](https://evil.example/a.png) [click](https://evil.example)"
    sandbox.answer = TestRunResult(
        RunStatus.COMPLETED, (case(f"test_calc.py::{hostile}", "failed", hostile),)
    )

    prepare_and_run(app)

    markdown_like = (app.markdown, app.caption, app.warning, app.error, app.success)
    assert not any(
        "evil.example" in str(item.value) for group in markdown_like for item in group
    )
    assert hostile in [item.value for item in app.code]
    assert any(hostile in str(item.value) for item in app.text)


# --- Errors ---


def test_sandbox_error_is_shown_and_ruff_results_stay(sandbox, app):
    sandbox.answer = SandboxError("Docker could not start the test container: boom")

    prepare_and_run(app)

    assert "could not be run" in app.error[-1].value
    assert "boom" in app.error[-1].value
    assert "Ruff found no issues" in texts(app)  # Ruff results.
    assert not app.exception


def test_cleanup_failure_is_shown_with_the_commands(sandbox, app):
    sandbox.answer = SandboxCleanupError(
        "The test container x could not be confirmed as removed. Check with "
        "`docker ps -a --filter label=ai-bug-detector.sandbox` and remove it with "
        "`docker rm -f x`."
    )

    prepare_and_run(app)

    assert "docker rm -f x" in app.error[-1].value
    assert "may still exist" in texts(app)


# --- File limits and names ---


@pytest.mark.parametrize("which", ["code", "tests"])
def test_file_over_256_kb_cannot_be_run(sandbox, app, which):
    big = b"x = 1\n" + b"#" * (256 * 1024)
    analyze(app, big if which == "code" else CODE)
    upload_tests(app, big if which == "tests" else TESTS)

    assert "larger than 256 KB" in texts(app)
    assert not any(b.label.startswith("I agree to run") for b in app.checkbox)
    assert sandbox.calls == []


def test_file_of_exactly_256_kb_can_be_run(app):
    exact = b"#" * (256 * 1024)
    analyze(app)
    upload_tests(app, exact)

    assert consent_box(app)


@pytest.mark.parametrize(
    ("code_name", "test_name", "expected"),
    [
        ("os.py", "test_calc.py", "cannot be used"),
        ("my calc.py", "test_calc.py", "cannot be used"),
        ("calc.py", "checks.py", "must start with test_"),
    ],
)
def test_unusable_file_names_are_explained(app, code_name, test_name, expected):
    analyze(app, CODE, name=code_name)
    upload_tests(app, TESTS, name=test_name)

    assert expected in texts(app)
    assert not any(b.label.startswith("I agree to run") for b in app.checkbox)


# --- Living alongside the AI review ---


def test_ai_review_and_test_run_work_side_by_side(sandbox, monkeypatch):
    monkeypatch.setenv(LIVE_AI_VARIABLE, "1")
    monkeypatch.setattr("ai_bug_detector.ai_panel.sdk_available", lambda: True)
    app = AppTest.from_file(str(APP_PATH), default_timeout=15).run()

    prepare_and_run(app)

    assert app.text_input(key="ai_api_key")  # The AI review is still offered.
    assert len(sandbox.calls) == 1


# --- A run that outlives a rerun of the page ---
# When the user clicks during a run, Streamlit starts a new run of the page at once.
# The old run finishes and hands its result over through the panel's registry.


def start_pending_run(app: AppTest, token="tok", started_ago=1.0) -> None:
    analyze(app)
    upload_tests(app)
    consent_box(app).check().run()
    app.session_state[sandbox_panel.PENDING_KEY] = {
        "token": token,
        "started": time.monotonic() - started_ago,
    }
    app.run()


def outcome_for(app: AppTest, result: TestRunResult) -> dict:
    code_id = app.file_uploader[0].value.file_id
    test_id = uploader_for_tests(app).value.file_id
    return {
        "code_id": code_id,
        "test_id": test_id,
        "result": result,
        "error": None,
        "cleanup_failed": False,
    }


def test_run_still_going_is_shown_and_cannot_be_started_twice(sandbox, app):
    start_pending_run(app)

    assert "still finishing" in texts(app)
    assert run_button(app).disabled
    assert sandbox.calls == []


def test_result_finished_in_the_background_is_shown(sandbox, app):
    start_pending_run(app, token="tok")
    result = TestRunResult(RunStatus.COMPLETED, (case(),))
    sandbox_panel._deliver("tok", outcome_for(app, result))

    app.run()

    assert "All 1 tests ran without failure." in texts(app)
    assert sandbox_panel.PENDING_KEY not in app.session_state
    assert not run_button(app).disabled
    assert sandbox.calls == []


def test_background_result_for_other_files_is_not_shown(sandbox, app):
    start_pending_run(app, token="tok")
    outcome = outcome_for(app, TestRunResult(RunStatus.COMPLETED, (case(),)))
    sandbox_panel._deliver("tok", outcome | {"test_id": "another upload"})

    app.run()

    assert "ran without failure" not in texts(app)
    assert sandbox_panel.PENDING_KEY not in app.session_state


def test_run_that_never_reports_back_is_given_up(sandbox, app):
    start_pending_run(app, started_ago=sandbox_panel._PENDING_EXPIRY_SECONDS + 1)

    assert "did not report a result" in texts(app)
    assert not run_button(app).disabled


def test_unexpected_error_during_a_run_is_shown_and_not_left_pending(sandbox, app):
    sandbox.answer = RuntimeError("unexpected")

    prepare_and_run(app)

    assert "An unexpected error stopped the run." in texts(app)
    assert sandbox_panel.PENDING_KEY not in app.session_state
    assert not app.exception


def test_registry_drops_results_nobody_collected(monkeypatch):
    sandbox_panel._deliver("old", {"result": None})
    now = time.monotonic() + sandbox_panel._FINISHED_EXPIRY_SECONDS + 1
    monkeypatch.setattr(sandbox_panel.time, "monotonic", lambda: now)

    sandbox_panel._deliver("new", {"result": None})

    assert not sandbox_panel._is_finished("old")
    assert sandbox_panel._is_finished("new")
    sandbox_panel._finished_runs.pop("new")
