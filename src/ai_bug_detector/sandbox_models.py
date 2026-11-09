"""Results of running pytest in the Docker sandbox, and their strict validation.

Everything read from the container is untrusted: the tested code runs in the same
container as pytest, so it can print fake results or garbage. This module only checks
that the data is well-formed and within limits. It cannot prove that the results are
true, and the page must say so.
"""

import json
import math
from dataclasses import dataclass
from enum import StrEnum

# The line the runner prints last, followed by the JSON result on the same line.
RESULT_MARKER = b"AI-BUG-DETECTOR-RESULT:"
PROTOCOL_VERSION = 1

MAX_OUTPUT_BYTES = 1024 * 1024
MAX_TESTS = 500
MAX_NAME_LENGTH = 500
MAX_MESSAGE_LENGTH = 2000
MAX_DURATION_SECONDS = 3600

# pytest exit codes: 0 all passed, 1 some failed, 2 interrupted (for example by a
# collection error), 3 internal error, 4 usage error, 5 no tests collected.
_COMPLETED_EXIT_CODES = (0, 1)
_PYTEST_ERROR_EXIT_CODES = (2, 3, 4)
_NO_TESTS_EXIT_CODE = 5

_RESULT_KEYS = {"protocol", "exit_code", "tests"}
_TEST_KEYS = {"name", "outcome", "duration", "message"}


class SandboxOutputError(Exception):
    """The container's output is missing, too large or malformed."""


class TestOutcome(StrEnum):
    """The outcome of one test, as pytest names it."""

    __test__ = False  # Not a test class, despite its name.

    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"
    XFAILED = "xfailed"  # Expected to fail, and failed.
    XPASSED = "xpassed"  # Expected to fail, but passed.


class RunStatus(StrEnum):
    """How a test run ended, seen from outside the container."""

    COMPLETED = "completed"  # pytest ran the tests: some may have failed.
    NO_TESTS = "no_tests"  # pytest found no tests.
    PYTEST_ERROR = "pytest_error"  # pytest stopped, for example on a syntax error.
    TIMED_OUT = "timed_out"  # The time limit was reached and the run was stopped.
    MEMORY_LIMIT = "memory_limit"  # The memory limit was reached.
    OUTPUT_LIMIT = "output_limit"  # The run printed too much and was stopped.


@dataclass(frozen=True)
class TestCaseResult:
    """One test as reported from inside the container."""

    __test__ = False  # Not a test class, despite its name.

    name: str
    outcome: TestOutcome
    duration_seconds: float
    message: str  # Empty when pytest gave no details, for example on success.


@dataclass(frozen=True)
class TestRunResult:
    """A whole test run. `tests` is empty when the run did not complete."""

    __test__ = False  # Not a test class, despite its name.

    status: RunStatus
    tests: tuple[TestCaseResult, ...] = ()
    ignored_count: int = 0  # Reported tests dropped because they failed validation.

    def count(self, outcome: TestOutcome) -> int:
        """Return how many tests had this outcome."""
        return sum(1 for test in self.tests if test.outcome is outcome)


def parse_runner_output(output: bytes) -> TestRunResult:
    """Return the validated result printed by the runner inside the container."""
    if len(output) > MAX_OUTPUT_BYTES:
        raise SandboxOutputError("The test run printed more than the allowed output.")

    data = _load_json(_find_result_line(output))
    if not isinstance(data, dict) or set(data) != _RESULT_KEYS:
        raise SandboxOutputError("The test result does not have the expected fields.")
    if data["protocol"] != PROTOCOL_VERSION or isinstance(data["protocol"], bool):
        raise SandboxOutputError("The test result uses an unknown format version.")

    exit_code = _exit_code(data["exit_code"])
    tests, ignored_count = _read_tests(data["tests"])

    if exit_code == 0 and any(
        test.outcome in (TestOutcome.FAILED, TestOutcome.ERROR) for test in tests
    ):
        raise SandboxOutputError("The test result contradicts itself.")

    if exit_code in _COMPLETED_EXIT_CODES:
        status = RunStatus.COMPLETED
    elif exit_code == _NO_TESTS_EXIT_CODE:
        status = RunStatus.NO_TESTS
    else:
        status = RunStatus.PYTEST_ERROR
    return TestRunResult(status, tuple(tests), ignored_count)


def _find_result_line(output: bytes) -> bytes:
    """Return the JSON after the last marker line. Only that line is decoded."""
    for line in reversed(output.splitlines()):
        if line.startswith(RESULT_MARKER):
            return line[len(RESULT_MARKER) :]
    raise SandboxOutputError("The test run ended without reporting a result.")


def _load_json(text: bytes) -> object:
    try:
        return json.loads(
            text.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise SandboxOutputError("The test result is not valid JSON.") from None


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("Duplicate key")
    return dict(pairs)


def _reject_constant(name: str) -> object:
    # Python's json module would otherwise accept NaN and Infinity.
    raise ValueError(f"Invalid constant: {name}")


def _exit_code(value: object) -> int:
    known_codes = (
        *_COMPLETED_EXIT_CODES,
        *_PYTEST_ERROR_EXIT_CODES,
        _NO_TESTS_EXIT_CODE,
    )
    if not _is_int(value) or value not in known_codes:
        raise SandboxOutputError("The test result has an unknown exit code.")
    return value


def _read_tests(items: object) -> tuple[list[TestCaseResult], int]:
    """Return the valid tests, and how many were dropped or over the limit."""
    if not isinstance(items, list):
        raise SandboxOutputError("The test result does not have the expected fields.")

    tests = []
    ignored_count = max(0, len(items) - MAX_TESTS)
    for item in items[:MAX_TESTS]:
        try:
            tests.append(_read_test(item))
        except ValueError:
            ignored_count += 1
    return tests, ignored_count


def _read_test(item: object) -> TestCaseResult:
    if not isinstance(item, dict) or set(item) != _TEST_KEYS:
        raise ValueError("Unexpected test fields")
    return TestCaseResult(
        name=_text(item["name"], MAX_NAME_LENGTH, allow_empty=False, multiline=False),
        outcome=TestOutcome(item["outcome"]),  # Raises ValueError if unknown.
        duration_seconds=_duration(item["duration"]),
        message=_text(item["message"], MAX_MESSAGE_LENGTH, allow_empty=True),
    )


def _text(value: object, max_length: int, *, allow_empty: bool, multiline=True) -> str:
    if not isinstance(value, str) or len(value) > max_length:
        raise ValueError("Invalid text")
    if not allow_empty and not value.strip():
        raise ValueError("Empty text")
    allowed = {"\n", "\t"} if multiline else set()
    # Control characters could hide text or alter how it looks on screen.
    if any(_is_control(character) and character not in allowed for character in value):
        raise ValueError("Control character in text")
    return value


def _is_control(character: str) -> bool:
    code = ord(character)
    return code < 0x20 or 0x7F <= code < 0xA0


def _duration(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("Invalid duration")
    if not math.isfinite(value) or not 0 <= value <= MAX_DURATION_SECONDS:
        raise ValueError("Invalid duration")
    return float(value)


def _is_int(value: object) -> bool:
    # bool is checked because True is also an int in Python.
    return isinstance(value, int) and not isinstance(value, bool)
