"""Tests for sandbox/runner.py's pure functions, on this computer.

runner.py is made to run inside the container. Here, only its functions that read the
input and format the result are called: pytest is never started and no uploaded code
is run. The real container is tested in test_sandbox_docker.py.
"""

import importlib.util
import json
import re
from pathlib import Path

import pytest

from ai_bug_detector import docker_sandbox, sandbox_models
from ai_bug_detector.docker_sandbox import _build_payload
from ai_bug_detector.sandbox_models import RunStatus, TestOutcome, parse_runner_output

RUNNER_PATH = Path(__file__).resolve().parents[1] / "sandbox" / "runner.py"
_spec = importlib.util.spec_from_file_location("sandbox_runner", RUNNER_PATH)
runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runner)

CODE = b"def add(a, b):\n    return a + b\n"
TESTS = b"from calc import add\n\ndef test_add():\n    assert add(2, 2) == 4\n"


class Stream:
    def __init__(self, data: bytes):
        self.data = data

    def read(self, size):
        return self.data[:size]


def payload(**changes) -> dict:
    data = json.loads(_build_payload("calc.py", CODE, "test_calc.py", TESTS))
    return data | changes


def read(data) -> dict:
    raw = data if isinstance(data, bytes) else json.dumps(data).encode()
    return runner.read_input(Stream(raw))


def make_test(**changes) -> dict:
    return {
        "name": "test_calc.py::test_add",
        "outcome": "passed",
        "duration": 0.01,
        "message": "",
    } | changes


# --- The runner and the application agree ---


@pytest.mark.parametrize(
    ("runner_value", "app_value"),
    [
        ("RESULT_MARKER", sandbox_models.RESULT_MARKER),
        ("PROTOCOL_VERSION", sandbox_models.PROTOCOL_VERSION),
        ("PROTOCOL_VERSION", docker_sandbox.PAYLOAD_PROTOCOL),
        ("MAX_OUTPUT_BYTES", sandbox_models.MAX_OUTPUT_BYTES),
        ("MAX_TESTS", sandbox_models.MAX_TESTS),
        ("MAX_NAME_LENGTH", sandbox_models.MAX_NAME_LENGTH),
        ("MAX_MESSAGE_LENGTH", sandbox_models.MAX_MESSAGE_LENGTH),
        ("MAX_FILE_BYTES", docker_sandbox.MAX_FILE_BYTES),
        ("_RESERVED_MODULES", docker_sandbox._RESERVED_MODULES),
    ],
)
def test_shared_rules_are_identical(runner_value, app_value):
    assert getattr(runner, runner_value) == app_value


def test_module_name_patterns_are_identical():
    assert runner._MODULE_NAME.pattern == docker_sandbox._MODULE_NAME.pattern


@pytest.mark.parametrize(
    "name",
    [
        "calc.py",
        "my_module.py",
        "Calc2.py",
        "os.py",
        "conftest.py",
        "class.py",
        "../x.py",
        "a b.py",
        "x.txt",
        "RUNNER.py",
        "json.py",
        "_pytest.py",
    ],
)
def test_module_name_rule_gives_the_same_answer_on_both_sides(name):
    try:
        docker_sandbox._check_module_name(name)
        app_accepts = True
    except docker_sandbox.SandboxError:
        app_accepts = False

    assert runner.is_safe_module_name(name) is app_accepts


# --- Reading the input ---


def test_payload_built_by_the_application_is_accepted():
    files = read(payload())

    assert files == {"calc.py": CODE.decode(), "test_calc.py": TESTS.decode()}


def test_input_over_the_size_limit_is_refused():
    with pytest.raises(runner.InputError, match="too large"):
        read(b" " * (runner.MAX_INPUT_BYTES + 1))


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(b"not json", id="not-json"),
        pytest.param(b"\xff\xfe", id="invalid-utf8"),
        pytest.param(b"[" * 100_000, id="deeply-nested"),
    ],
)
def test_invalid_json_is_refused(data):
    with pytest.raises(runner.InputError, match="not valid JSON"):
        read(data)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (["list"], "unexpected fields"),
        ({"protocol": 1}, "unexpected fields"),
        (payload(extra=1), "unexpected fields"),
        (payload(protocol=2), "unknown protocol"),
        (payload(protocol=True), "unknown protocol"),
        (payload(code_file="calc.py"), "unexpected file fields"),
        (payload(code_file={"name": "calc.py"}), "unexpected file fields"),
        (payload(code_file={"name": 1, "content": ""}), "must be text"),
        (payload(code_file={"name": "../calc.py", "content": ""}), "unsafe"),
        (payload(code_file={"name": "os.py", "content": ""}), "unsafe"),
        (payload(code_file={"name": "conftest.py", "content": ""}), "unsafe"),
        (payload(code_file={"name": "test_calc.py", "content": ""}), "different"),
        (payload(code_file={"name": "calc_test.py", "content": ""}), "look like"),
        (payload(test_file={"name": "checks.py", "content": ""}), "start with test_"),
        (
            payload(code_file={"name": "calc.py", "content": "#" * (256 * 1024 + 1)}),
            "too large",
        ),
    ],
)
def test_invalid_input_is_refused(data, message):
    with pytest.raises(runner.InputError, match=message):
        read(data)


# --- Writing the files ---


def test_files_are_written_in_a_new_folder(tmp_path):
    work = tmp_path / "work"

    test_path = runner.write_files(read(payload()), work)

    assert test_path == work / "test_calc.py"
    assert (work / "calc.py").read_text("utf-8") == CODE.decode()


def test_an_existing_folder_is_never_reused(tmp_path):
    (tmp_path / "work").mkdir()

    with pytest.raises(FileExistsError):
        runner.write_files(read(payload()), tmp_path / "work")


def test_a_name_leaving_the_folder_is_refused_even_without_validation(tmp_path):
    with pytest.raises(runner.InputError, match="unsafe"):
        runner.write_files({"../escape.py": ""}, tmp_path / "work")

    assert not (tmp_path / "escape.py").exists()


# --- The result: always readable by the application ---


def parse(exit_code: int, tests: list[dict]):
    return parse_runner_output(
        runner.RESULT_MARKER + runner.build_result(exit_code, tests)
    )


def test_result_is_read_by_the_application():
    run = parse(1, [make_test(), make_test(name="t::b", outcome="failed", message="x")])

    assert run.status is RunStatus.COMPLETED
    assert run.count(TestOutcome.FAILED) == 1
    assert run.ignored_count == 0


def test_control_characters_are_replaced_so_tests_are_not_dropped():
    tests = [make_test(name="t::a\nb\x1b", message="red \x1b[31m\r\n\tend\x85")]

    run = parse(0, tests)

    assert run.ignored_count == 0
    assert run.tests[0].name == "t::a?b?"
    assert run.tests[0].message == "red ?[31m?\n\tend?"


def test_long_names_and_messages_are_shortened():
    run = parse(0, [make_test(name="n" * 9000, message="m" * 9000)])

    assert len(run.tests[0].name) == sandbox_models.MAX_NAME_LENGTH
    assert len(run.tests[0].message) == sandbox_models.MAX_MESSAGE_LENGTH


def test_empty_name_is_replaced():
    assert parse(0, [make_test(name="")]).tests[0].name == "unnamed test"


def test_durations_are_kept_within_the_accepted_range():
    run = parse(0, [make_test(duration=-1), make_test(duration=99999)])

    assert [t.duration_seconds for t in run.tests] == [0.0, 3600.0]


def test_tests_over_the_limit_are_not_sent():
    run = parse(0, [make_test()] * 800)

    assert len(run.tests) == sandbox_models.MAX_TESTS


def test_huge_results_are_shortened_until_they_fit():
    # 500 tests with long text in a script using 4 bytes per character.
    big = "𝔘" * 3000
    tests = [make_test(name=f"t::{n}{big}", message=big) for n in range(500)]

    encoded = runner.build_result(1, tests)

    assert len(encoded) <= runner._RESULT_BUDGET_BYTES
    assert parse(1, tests).status is RunStatus.COMPLETED


def test_report_prints_one_result_line(capsysbinary):
    runner.report(5, [])

    output = capsysbinary.readouterr().out
    assert output.startswith(runner.RESULT_MARKER)
    assert output.endswith(b"\n")
    assert parse_runner_output(output).status is RunStatus.NO_TESTS


def test_result_line_contains_no_line_break():
    line = runner.build_result(1, [make_test(message="a\nb\r\nc")])

    assert not re.search(rb"[\r\n]", line)
