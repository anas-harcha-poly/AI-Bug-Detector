"""Runs inside the sandbox container: receive two files, run pytest, report as JSON.

This file is copied into the Docker image and never runs on Windows. It cannot import
the ai_bug_detector package, so the few rules it shares with it are repeated here. The
tests check that both sides stay identical.

Input, on standard input: {"protocol": 1, "code_file": {"name", "content"},
"test_file": {"name", "content"}}.
Output: a last line made of RESULT_MARKER followed by the JSON that
ai_bug_detector.sandbox_models.parse_runner_output reads.
"""

import json
import keyword
import os
import re
import sys
from pathlib import Path

RESULT_MARKER = b"AI-BUG-DETECTOR-RESULT:"
PROTOCOL_VERSION = 1

MAX_FILE_BYTES = 256 * 1024
MAX_INPUT_BYTES = 4 * 1024 * 1024  # Two files, with JSON escaping.
MAX_OUTPUT_BYTES = 1024 * 1024
MAX_TESTS = 500
MAX_NAME_LENGTH = 500
MAX_MESSAGE_LENGTH = 2000
# Keeps the result well under the output limit, with room for other output.
_RESULT_BUDGET_BYTES = MAX_OUTPUT_BYTES // 2

WORK_DIR = Path("/tmp/work")
CONFIG_FILE = Path("/opt/sandbox/pytest.ini")

_MODULE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\.py")
_RESERVED_MODULES = {"conftest", "pytest", "_pytest", "pluggy", "py", "runner"}
_INPUT_KEYS = {"protocol", "code_file", "test_file"}
_FILE_KEYS = {"name", "content"}
# pytest's exit code for a usage error: used when the input itself is invalid.
_USAGE_ERROR = 4


class InputError(Exception):
    """The input sent by the application is not what this runner expects."""


def main() -> int:
    try:
        files = read_input(sys.stdin.buffer)
        test_path = write_files(files, WORK_DIR)
    except InputError as error:
        print(f"Invalid input: {error}", file=sys.stderr)
        report(_USAGE_ERROR, [])
        return 0

    exit_code, tests = run_pytest(test_path)
    report(exit_code, tests)
    return 0


def read_input(stream) -> dict[str, str]:
    """Return {file name: content} after checking every rule."""
    raw = stream.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise InputError("too large")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise InputError("not valid JSON") from None

    if not isinstance(data, dict) or set(data) != _INPUT_KEYS:
        raise InputError("unexpected fields")
    if data["protocol"] != PROTOCOL_VERSION or isinstance(data["protocol"], bool):
        raise InputError("unknown protocol")

    code_name, code = _read_file(data["code_file"])
    test_name, tests = _read_file(data["test_file"])
    if code_name == test_name:
        raise InputError("the two files must be different")
    if code_name.startswith("test_") or code_name.endswith("_test.py"):
        raise InputError("the code file must not look like a test file")
    if not test_name.startswith("test_"):
        raise InputError("the test file name must start with test_")
    return {code_name: code, test_name: tests}


def _read_file(item: object) -> tuple[str, str]:
    if not isinstance(item, dict) or set(item) != _FILE_KEYS:
        raise InputError("unexpected file fields")
    name, content = item["name"], item["content"]
    if not isinstance(name, str) or not isinstance(content, str):
        raise InputError("file name and content must be text")
    if not is_safe_module_name(name):
        raise InputError(f"unsafe file name: {name!r}")
    if len(content.encode("utf-8", errors="replace")) > MAX_FILE_BYTES:
        raise InputError(f"{name} is too large")
    return name, content


def is_safe_module_name(name: str) -> bool:
    """Same rule as ai_bug_detector.docker_sandbox: a plain, non-shadowing module."""
    stem = name.removesuffix(".py")
    return bool(
        _MODULE_NAME.fullmatch(name)
        and not keyword.iskeyword(stem)
        and stem.lower() not in _RESERVED_MODULES
        and stem not in sys.stdlib_module_names
    )


def write_files(files: dict[str, str], work_dir: Path) -> Path:
    """Write the files into a new folder under /tmp. Return the test file's path."""
    work_dir.mkdir(mode=0o700)  # Fails if it exists: nothing is overwritten.
    test_path = None
    for name, content in files.items():
        path = work_dir / name
        # Checked again here, so no name can ever point outside the folder.
        if path.parent != work_dir:
            raise InputError(f"unsafe file name: {name!r}")
        with open(path, "x", encoding="utf-8") as file:
            file.write(content)
        if name.startswith("test_"):
            test_path = path
    return test_path


def run_pytest(test_path: Path) -> tuple[int, list[dict]]:
    """Run pytest in this process, and return its exit code and the test results."""
    import pytest

    collector = ResultCollector()
    arguments = [
        str(test_path),
        "-c",
        str(CONFIG_FILE),  # Never a configuration file found among uploaded files.
        "--rootdir",
        str(test_path.parent),
        "-p",
        "no:cacheprovider",  # Writes nothing besides the tests' own files.
        "--noconftest",
        "--color=no",
        "-q",
    ]
    # pytest's own report is not needed: the JSON result replaces it.
    real_stdout = sys.stdout
    with open(os.devnull, "w") as quiet:
        sys.stdout = quiet
        try:
            exit_code = int(pytest.main(arguments, plugins=[collector]))
        finally:
            sys.stdout = real_stdout
    return exit_code, collector.results()


class ResultCollector:
    """A pytest plugin that records each test's outcome, duration and message."""

    def __init__(self) -> None:
        self._tests: dict[str, dict] = {}

    def pytest_collectreport(self, report) -> None:
        if report.failed:  # For example, a syntax error in an uploaded file.
            self._record(report.nodeid or "collection", "error", 0.0, report)

    def pytest_runtest_logreport(self, report) -> None:
        if report.when == "call":
            outcome = _call_outcome(report)
        elif report.failed:
            outcome = "error"  # setup or teardown failed.
        elif report.skipped:
            outcome = "xfailed" if hasattr(report, "wasxfail") else "skipped"
        else:
            # A passing setup or teardown only adds to the duration.
            if report.nodeid in self._tests:
                self._tests[report.nodeid]["duration"] += report.duration
            return
        self._record(report.nodeid, outcome, report.duration, report)

    def _record(self, name: str, outcome: str, duration: float, report) -> None:
        previous = self._tests.get(name)
        if previous is not None and previous["outcome"] in ("failed", "error"):
            return  # Keep the first failure of a test.
        self._tests[name] = {
            "name": name,
            "outcome": outcome,
            "duration": duration + (previous["duration"] if previous else 0.0),
            "message": _message(report),
        }

    def results(self) -> list[dict]:
        return list(self._tests.values())


def _call_outcome(report) -> str:
    if hasattr(report, "wasxfail"):
        return "xfailed" if report.skipped else "xpassed"
    if report.passed:
        return "passed"
    if report.skipped:
        return "skipped"
    return "failed"


def _message(report) -> str:
    if report.passed:
        return ""
    if report.skipped and isinstance(report.longrepr, tuple):
        return str(report.longrepr[2])  # (file, line, reason)
    return report.longreprtext


def report(exit_code: int, tests: list[dict]) -> None:
    """Print the result line, cleaned and kept within the output limits."""
    line = RESULT_MARKER + build_result(exit_code, tests) + b"\n"
    sys.stdout.flush()
    sys.stdout.buffer.write(line)
    sys.stdout.buffer.flush()


def build_result(exit_code: int, tests: list[dict]) -> bytes:
    """Return the JSON result, shortened until it fits the result budget."""
    tests = [_prepare(test) for test in tests[:MAX_TESTS]]
    name_length, message_length = MAX_NAME_LENGTH, MAX_MESSAGE_LENGTH
    while True:
        data = {
            "protocol": PROTOCOL_VERSION,
            "exit_code": exit_code,
            "tests": [
                {
                    "name": test["name"][:name_length] or "unnamed test",
                    "outcome": test["outcome"],
                    "duration": test["duration"],
                    "message": test["message"][:message_length],
                }
                for test in tests
            ],
        }
        encoded = json.dumps(data, ensure_ascii=False).encode("utf-8")
        if len(encoded) <= _RESULT_BUDGET_BYTES:
            return encoded
        # Too large: shorten messages first, then names, then drop tests.
        if message_length > 0:
            message_length //= 2
        elif name_length > 40:
            name_length //= 2
        else:
            tests = tests[: len(tests) // 2]


_CONTROL_CHARACTERS = [*range(0x20), *range(0x7F, 0xA0)]
# str.translate works in C: fast even on very long text, unlike a Python loop.
_CLEAN_SINGLE_LINE = str.maketrans(dict.fromkeys(_CONTROL_CHARACTERS, "?"))
_CLEAN_MULTILINE = str.maketrans(
    {code: "?" for code in _CONTROL_CHARACTERS if chr(code) not in "\n\t"}
)


def _prepare(test: dict) -> dict:
    """Clean a test once, at its maximum size, before any shortening."""
    return {
        "name": _clean(test["name"], MAX_NAME_LENGTH, multiline=False),
        "outcome": test["outcome"],
        "duration": min(max(float(test["duration"]), 0.0), 3600.0),
        "message": _clean(test["message"], MAX_MESSAGE_LENGTH),
    }


def _clean(text: object, max_length: int, multiline: bool = True) -> str:
    """Shorten, then replace control characters, which the application refuses."""
    table = _CLEAN_MULTILINE if multiline else _CLEAN_SINGLE_LINE
    return str(text)[:max_length].translate(table)


if __name__ == "__main__":
    sys.exit(main())
