"""The optional "run your tests" part of the page.

Nothing here runs by itself: a run only starts when the user clicks the button, after
ticking a box for these exact two files. Everything coming back from the container is
shown literally (st.text, st.code, st.dataframe), never as Markdown.

A detail of Streamlit matters here. When the user clicks anything during a run,
Streamlit starts a new run of the page at once, without waiting. The old run still
finishes run_tests, container removal included, but it is then stopped as soon as it
touches st.session_state. So its result is first handed over through a small registry
in this module, and the new run of the page picks it up from there.
"""

import threading
import time
import uuid

import streamlit as st

from ai_bug_detector.deployment import is_public
from ai_bug_detector.docker_sandbox import (
    SANDBOX_VARIABLE,
    STATUS_MESSAGES,
    TIMEOUT_SECONDS,
    SandboxCleanupError,
    SandboxError,
    SandboxStatus,
    run_tests,
    sandbox_enabled,
    sandbox_status,
    validate_files,
)
from ai_bug_detector.sandbox_models import (
    RunStatus,
    TestOutcome,
    TestRunResult,
)

RESULT_KEY = "sandbox_result"
TEST_FILE_KEY = "sandbox_test_file"
RUN_BUTTON_KEY = "sandbox_run"
CONSENT_PREFIX = "sandbox_consent"
PENDING_KEY = "sandbox_pending_run"

# A run that has not reported back after this long is considered lost.
_PENDING_EXPIRY_SECONDS = TIMEOUT_SECONDS + 90
# Results nobody collected (for example, the tab was closed) are dropped after this.
_FINISHED_EXPIRY_SECONDS = 600
_finished_runs: dict[str, tuple[float, dict]] = {}
_finished_lock = threading.Lock()

WARNING_TEXT = (
    "**Running tests executes code.** Your code file and your test file will run "
    "inside a temporary Docker container, with no network, no access to the files of "
    f"this computer, and limits on memory, CPU, processes and time ({TIMEOUT_SECONDS} "
    "s). This lowers the risk, but no isolation is perfect: only run code you would "
    "accept to run. Nothing runs until you tick the box below and click the button."
)
DISCLAIMER = (
    "These results come from inside the container, where the tested code could "
    "influence them. Passing tests only show that these cases worked: they do not "
    "prove that the code is correct or safe."
)

_OUTCOME_ICONS = {
    TestOutcome.PASSED: "✅",
    TestOutcome.FAILED: "❌",
    TestOutcome.ERROR: "⚠️",
    TestOutcome.SKIPPED: "⏭️",
    TestOutcome.XFAILED: "➖",
    TestOutcome.XPASSED: "❗",
}
_STOPPED_MESSAGES = {
    RunStatus.TIMED_OUT: (
        f"The tests were stopped after {TIMEOUT_SECONDS} seconds, for example "
        "because of an infinite loop. The container was removed."
    ),
    RunStatus.MEMORY_LIMIT: (
        "The tests used more memory than allowed and were stopped. The container was "
        "removed."
    ),
    RunStatus.OUTPUT_LIMIT: (
        "The tests printed more output than allowed and were stopped. The container "
        "was removed."
    ),
}


@st.cache_data(ttl=15, show_spinner=False)
def _cached_status() -> SandboxStatus:
    """Checking Docker takes a moment, so the answer is reused for a few seconds."""
    return sandbox_status()


def show_sandbox_panel(uploaded_file, analysis: dict) -> None:
    """Display the optional test run for an analyzed code file."""
    if analysis["error"] or analysis.get("is_empty"):
        return

    st.divider()
    st.subheader("Run your tests (optional)")

    if is_public():
        st.caption(
            "Running tests is not available on this public site, and no code is ever "
            "run here."
        )
        return
    if not sandbox_enabled():
        st.caption(
            "Running tests is off in this version, and no code is ever run. It can be "
            f"turned on with {SANDBOX_VARIABLE}=1 when the app runs on a computer "
            "with Docker: see the README."
        )
        return

    status = _cached_status()
    if status is not SandboxStatus.READY:
        st.info(STATUS_MESSAGES[status])
        return

    _collect_background_result()

    st.caption(
        "Find bugs that static analysis cannot see, such as a function returning a "
        "wrong value, by running your pytest tests against this file."
    )
    test_file = st.file_uploader(
        "Test file (test_*.py)",
        type="py",
        key=TEST_FILE_KEY,
        max_upload_size=1,  # The server checks the real limit below.
    )

    stored = _result_for(uploaded_file, test_file)
    if test_file is None:
        return

    code = uploaded_file.getvalue()
    tests = test_file.getvalue()
    try:
        validate_files(uploaded_file.name, code, test_file.name, tests)
    except SandboxError as error:
        st.error(f"These files cannot be run. {error}")
        return

    st.warning(WARNING_TEXT)
    # Names were validated above: they only contain letters, digits and underscores.
    consent = st.checkbox(
        f"I agree to run `{uploaded_file.name}` and `{test_file.name}` in the sandbox.",
        key=f"{CONSENT_PREFIX}_{uploaded_file.file_id}_{test_file.file_id}",
    )
    running = PENDING_KEY in st.session_state
    clicked = st.button(
        "Run tests in Docker",
        type="primary",
        key=RUN_BUTTON_KEY,
        disabled=not consent or running,
    )

    # Checked again here: this is the last step before any code runs.
    if clicked and consent and not running:
        token = uuid.uuid4().hex
        st.session_state[PENDING_KEY] = {"token": token, "started": time.monotonic()}
        with st.spinner(
            f"Running the tests in an isolated container (up to {TIMEOUT_SECONDS} s)..."
        ):
            try:
                outcome = _run(uploaded_file, code, test_file, tests)
            except Exception:
                outcome = _error_outcome(
                    uploaded_file, test_file, "An unexpected error stopped the run."
                )
            _deliver(token, outcome)
        _collect_background_result()
        stored = _result_for(uploaded_file, test_file)
    elif running:
        _wait_for_background_run()

    if stored is not None:
        _show_outcome(stored)


def _deliver(token: str, outcome: dict) -> None:
    """Hand a finished run over to whichever run of the page shows it."""
    now = time.monotonic()
    with _finished_lock:
        for old_token, (finished, _) in list(_finished_runs.items()):
            if now - finished > _FINISHED_EXPIRY_SECONDS:
                del _finished_runs[old_token]
        _finished_runs[token] = (now, outcome)


def _is_finished(token: str) -> bool:
    with _finished_lock:
        return token in _finished_runs


def _collect_background_result() -> None:
    """Move a finished run of this session into its result, if there is one."""
    pending = st.session_state.get(PENDING_KEY)
    if pending is None:
        return
    with _finished_lock:
        finished = _finished_runs.pop(pending["token"], None)
    if finished is not None:
        st.session_state[RESULT_KEY] = finished[1]
        del st.session_state[PENDING_KEY]
    elif time.monotonic() - pending["started"] > _PENDING_EXPIRY_SECONDS:
        del st.session_state[PENDING_KEY]
        st.session_state[RESULT_KEY] = {
            "code_id": None,
            "test_id": None,
            "lost": True,
        }


@st.fragment(run_every=1)
def _wait_for_background_run() -> None:
    """Check every second for the run started before the page was rerun."""
    pending = st.session_state.get(PENDING_KEY)
    if pending is None or _is_finished(pending["token"]):
        st.rerun()  # The whole page: it collects and shows the result.
    if time.monotonic() - pending["started"] > _PENDING_EXPIRY_SECONDS:
        st.rerun()
    st.info(
        "A test run is still finishing. Its result will appear here, and the "
        "container is removed when it ends."
    )


def _result_for(uploaded_file, test_file) -> dict | None:
    """Return the stored result of these exact two uploads, and forget any other."""
    stored = st.session_state.get(RESULT_KEY)
    if stored is None:
        return None
    if stored.get("lost"):
        return stored  # Shown once, whatever the files.
    if test_file is None or (stored["code_id"], stored["test_id"]) != (
        uploaded_file.file_id,
        test_file.file_id,
    ):
        del st.session_state[RESULT_KEY]
        return None
    return stored


def _error_outcome(uploaded_file, test_file, message: str) -> dict:
    return {
        "code_id": uploaded_file.file_id,
        "test_id": test_file.file_id,
        "result": None,
        "error": message,
        "cleanup_failed": False,
    }


def _run(uploaded_file, code: bytes, test_file, tests: bytes) -> dict:
    stored = _error_outcome(uploaded_file, test_file, None)
    try:
        stored["result"] = run_tests(uploaded_file.name, code, test_file.name, tests)
    except SandboxCleanupError as error:
        stored["error"] = str(error)
        stored["cleanup_failed"] = True
    except SandboxError as error:
        stored["error"] = str(error)
    return stored


def _show_outcome(stored: dict) -> None:
    if stored.get("lost"):
        del st.session_state[RESULT_KEY]
        st.error(
            "The previous test run did not report a result. Any container it left "
            "behind is removed at the start of the next run."
        )
        return
    if stored["error"]:
        st.error(f"The tests could not be run. {stored['error']}")
        if stored["cleanup_failed"]:
            st.caption(
                "The container may still exist. The commands above show how to "
                "check and remove it."
            )
        return
    _show_result(stored["result"])


def _show_result(result: TestRunResult) -> None:
    if result.status in _STOPPED_MESSAGES:
        st.error(_STOPPED_MESSAGES[result.status])
        return
    if result.status is RunStatus.NO_TESTS:
        st.warning(
            "pytest found no tests. Test functions must be named test_something."
        )
        return

    if result.status is RunStatus.PYTEST_ERROR:
        st.error(
            "pytest could not run the tests, for example because of a syntax error "
            "or an import that failed. Details are below."
        )
    elif result.count(TestOutcome.FAILED) or result.count(TestOutcome.ERROR):
        st.error("Some tests failed: the code does not behave as the tests expect.")
    else:
        st.success(f"All {len(result.tests)} tests ran without failure.")

    _show_counters(result)
    _show_tests(result)
    if result.ignored_count:
        st.caption(
            f"{result.ignored_count} reported test(s) failed validation and were "
            "ignored."
        )
    st.caption(DISCLAIMER)


def _show_counters(result: TestRunResult) -> None:
    other = sum(
        result.count(outcome)
        for outcome in (TestOutcome.SKIPPED, TestOutcome.XFAILED, TestOutcome.XPASSED)
    )
    duration = sum(test.duration_seconds for test in result.tests)
    columns = st.columns(5)
    columns[0].metric("Passed", result.count(TestOutcome.PASSED), border=True)
    columns[1].metric("Failed", result.count(TestOutcome.FAILED), border=True)
    columns[2].metric("Errors", result.count(TestOutcome.ERROR), border=True)
    columns[3].metric("Other", other, border=True)
    columns[4].metric("Duration", f"{duration:.2f} s", border=True)


def _show_tests(result: TestRunResult) -> None:
    if not result.tests:
        return
    st.dataframe(
        [
            {
                "Result": f"{_OUTCOME_ICONS[test.outcome]} {test.outcome.value}",
                "Test": test.name,
                "Duration (s)": round(test.duration_seconds, 3),
            }
            for test in result.tests
        ],
        hide_index=True,
        width="stretch",
        column_config={
            "Result": st.column_config.TextColumn(width="small"),
            "Test": st.column_config.TextColumn(width="large"),
            "Duration (s)": st.column_config.NumberColumn(width="small"),
        },
    )
    for test in result.tests:
        if test.message and test.outcome is not TestOutcome.PASSED:
            with st.container(border=True):
                st.text(f"{test.outcome.value}: {test.name}")
                st.code(test.message, language="text")
