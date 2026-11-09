"""Tests for reading test results from the sandbox, which are untrusted data.

No container is started here: the outputs below are written by hand to imitate what
the runner, or hostile code inside the container, could print.
"""

import json

import pytest

from ai_bug_detector.sandbox_models import (
    MAX_MESSAGE_LENGTH,
    MAX_NAME_LENGTH,
    MAX_OUTPUT_BYTES,
    MAX_TESTS,
    RESULT_MARKER,
    RunStatus,
    SandboxOutputError,
    TestOutcome,
    TestRunResult,
    parse_runner_output,
)


def make_test(**changes) -> dict:
    return {
        "name": "test_math.py::test_add",
        "outcome": "passed",
        "duration": 0.01,
        "message": "",
    } | changes


def result(**changes) -> dict:
    return {"protocol": 1, "exit_code": 0, "tests": [make_test()]} | changes


def output(data: object, before: bytes = b"") -> bytes:
    return before + RESULT_MARKER + json.dumps(data).encode() + b"\n"


# --- Valid results ---


def test_passing_run_is_read():
    run = parse_runner_output(output(result()))

    assert run.status is RunStatus.COMPLETED
    assert run.tests[0].name == "test_math.py::test_add"
    assert run.tests[0].outcome is TestOutcome.PASSED
    assert run.tests[0].duration_seconds == 0.01
    assert run.ignored_count == 0


def test_failing_test_is_reported_with_its_message():
    failure = make_test(
        name="test_math.py::test_add_wrong",
        outcome="failed",
        message="assert 5 == 4\n where 5 = add(2, 2)",
    )

    run = parse_runner_output(output(result(exit_code=1, tests=[failure])))

    assert run.status is RunStatus.COMPLETED
    assert run.count(TestOutcome.FAILED) == 1
    assert "5 == 4" in run.tests[0].message


def test_every_pytest_outcome_is_accepted():
    tests = [make_test(outcome=outcome.value) for outcome in TestOutcome]

    run = parse_runner_output(output(result(exit_code=1, tests=tests)))

    assert [test.outcome for test in run.tests] == list(TestOutcome)
    assert all(run.count(outcome) == 1 for outcome in TestOutcome)


@pytest.mark.parametrize(
    ("exit_code", "status"),
    [
        (0, RunStatus.COMPLETED),
        (1, RunStatus.COMPLETED),
        (2, RunStatus.PYTEST_ERROR),
        (3, RunStatus.PYTEST_ERROR),
        (4, RunStatus.PYTEST_ERROR),
        (5, RunStatus.NO_TESTS),
    ],
)
def test_exit_code_gives_the_run_status(exit_code, status):
    run = parse_runner_output(output(result(exit_code=exit_code, tests=[])))

    assert run.status is status


def test_empty_message_tab_newline_and_accents_are_accepted():
    tests = [make_test(message="Résumé:\n\tété"), make_test(message="")]

    run = parse_runner_output(output(result(tests=tests)))

    assert run.tests[0].message == "Résumé:\n\tété"
    assert run.ignored_count == 0


def test_integer_duration_is_accepted():
    run = parse_runner_output(output(result(tests=[make_test(duration=2)])))

    assert run.tests[0].duration_seconds == 2.0


def test_values_at_the_limits_are_accepted():
    item = make_test(
        name="n" * MAX_NAME_LENGTH,
        message="m" * MAX_MESSAGE_LENGTH,
        duration=3600,
    )

    run = parse_runner_output(output(result(tests=[item])))

    assert run.ignored_count == 0


def test_count_is_zero_for_an_absent_outcome():
    assert TestRunResult(RunStatus.TIMED_OUT).count(TestOutcome.FAILED) == 0


# --- Where the result is found in the output ---


def test_text_printed_before_the_result_is_ignored():
    noise = b"some print from the tested code\n\xff\xfe invalid bytes\n"

    run = parse_runner_output(output(result(), before=noise))

    assert run.status is RunStatus.COMPLETED


def test_windows_line_ending_is_accepted():
    data = RESULT_MARKER + json.dumps(result()).encode() + b"\r\n"

    assert parse_runner_output(data).status is RunStatus.COMPLETED


def test_the_last_result_line_wins():
    fake = output(result(tests=[make_test(name="fake::test")]))

    run = parse_runner_output(output(result(), before=fake))

    assert run.tests[0].name == "test_math.py::test_add"


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(b"", id="empty"),
        pytest.param(b"only some text\n", id="no-marker"),
        pytest.param(b"  " + RESULT_MARKER + b"{}\n", id="marker-not-at-line-start"),
    ],
)
def test_missing_result_raises_an_error(data):
    with pytest.raises(SandboxOutputError, match="without reporting a result"):
        parse_runner_output(data)


# --- Malformed overall results: the whole output is rejected ---


@pytest.mark.parametrize(
    "line",
    [
        pytest.param(b"not json", id="not-json"),
        pytest.param(b'{"protocol": 1', id="truncated"),
        pytest.param(b"\xff\xfe", id="invalid-utf8"),
        pytest.param(
            b'{"protocol": 1, "protocol": 1, "exit_code": 0, "tests": []}',
            id="duplicate-key",
        ),
        pytest.param(b"[" * 100_000 + b"]" * 100_000, id="deeply-nested"),
    ],
)
def test_invalid_json_is_rejected(line):
    with pytest.raises(SandboxOutputError, match="not valid JSON"):
        parse_runner_output(RESULT_MARKER + line)


@pytest.mark.parametrize("constant", [b"NaN", b"Infinity", b"-Infinity"])
def test_non_standard_json_numbers_are_rejected(constant):
    line = b'{"protocol": 1, "exit_code": 0, "tests": [{"name": "t", '
    line += b'"outcome": "passed", "duration": ' + constant + b', "message": ""}]}'

    with pytest.raises(SandboxOutputError, match="not valid JSON"):
        parse_runner_output(RESULT_MARKER + line)


@pytest.mark.parametrize(
    "data",
    [
        pytest.param([], id="list"),
        pytest.param("text", id="string"),
        pytest.param({"protocol": 1, "exit_code": 0}, id="missing-field"),
        pytest.param(result(extra="field"), id="extra-field"),
        pytest.param(result(tests="not a list"), id="tests-not-a-list"),
    ],
)
def test_unexpected_structure_is_rejected(data):
    with pytest.raises(SandboxOutputError, match="expected fields"):
        parse_runner_output(output(data))


@pytest.mark.parametrize("protocol", [2, 0, "1", True])
def test_unknown_protocol_version_is_rejected(protocol):
    with pytest.raises(SandboxOutputError, match="unknown format version"):
        parse_runner_output(output(result(protocol=protocol)))


@pytest.mark.parametrize("exit_code", [-1, 6, 137, "0", 1.0, True, None])
def test_unknown_exit_code_is_rejected(exit_code):
    with pytest.raises(SandboxOutputError, match="unknown exit code"):
        parse_runner_output(output(result(exit_code=exit_code)))


@pytest.mark.parametrize("outcome", ["failed", "error"])
def test_success_exit_code_with_failures_is_rejected(outcome):
    data = result(exit_code=0, tests=[make_test(outcome=outcome)])

    with pytest.raises(SandboxOutputError, match="contradicts itself"):
        parse_runner_output(output(data))


def test_output_over_the_limit_is_rejected_before_reading_it():
    padding = b"x" * MAX_OUTPUT_BYTES

    with pytest.raises(SandboxOutputError, match="more than the allowed output"):
        parse_runner_output(output(result(), before=padding))


def test_output_exactly_at_the_limit_is_read():
    end = output(result())
    padding = b"x" * (MAX_OUTPUT_BYTES - len(end) - 1) + b"\n"

    run = parse_runner_output(padding + end)

    assert run.status is RunStatus.COMPLETED


# --- Malformed tests: each one is dropped and counted ---


@pytest.mark.parametrize(
    "item",
    [
        pytest.param("not an object", id="not-an-object"),
        pytest.param({"name": "t"}, id="missing-fields"),
        pytest.param(make_test(extra="field"), id="extra-field"),
        pytest.param(make_test(name=""), id="empty-name"),
        pytest.param(make_test(name="   "), id="blank-name"),
        pytest.param(make_test(name=42), id="name-not-text"),
        pytest.param(make_test(name="n" * (MAX_NAME_LENGTH + 1)), id="name-too-long"),
        pytest.param(make_test(name="a\nb"), id="newline-in-name"),
        pytest.param(make_test(name="a\x1b[31mb"), id="escape-code-in-name"),
        pytest.param(make_test(outcome="exploded"), id="unknown-outcome"),
        pytest.param(make_test(outcome="PASSED"), id="outcome-wrong-case"),
        pytest.param(make_test(outcome=None), id="outcome-missing"),
        pytest.param(make_test(duration=-1), id="negative-duration"),
        pytest.param(make_test(duration=3601), id="duration-too-long"),
        pytest.param(make_test(duration="1"), id="duration-as-text"),
        pytest.param(make_test(duration=True), id="duration-as-boolean"),
        pytest.param(make_test(message=None), id="message-missing"),
        pytest.param(
            make_test(message="m" * (MAX_MESSAGE_LENGTH + 1)), id="message-too-long"
        ),
        pytest.param(make_test(message="a\x00b"), id="null-in-message"),
        pytest.param(make_test(message="a\x1b[2Jb"), id="escape-code-in-message"),
        pytest.param(make_test(message="a\rb"), id="carriage-return-in-message"),
        pytest.param(make_test(message="a\x85b"), id="c1-control-in-message"),
    ],
)
def test_invalid_test_is_dropped_and_counted(item):
    run = parse_runner_output(output(result(tests=[item, make_test()])))

    assert len(run.tests) == 1
    assert run.ignored_count == 1


def test_tests_over_the_limit_are_dropped_and_counted():
    run = parse_runner_output(output(result(tests=[make_test()] * (MAX_TESTS + 7))))

    assert len(run.tests) == MAX_TESTS
    assert run.ignored_count == 7
