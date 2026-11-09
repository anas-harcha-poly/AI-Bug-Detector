"""Static analysis with Ruff: the analyzed code is only read, never executed."""

import json
import subprocess
import sysconfig
from pathlib import Path

from ai_bug_detector.models import (
    FixApplicability,
    Issue,
    RuffEdit,
    RuffFix,
    Severity,
)

_TIMEOUT_SECONDS = 30

# Ruff exit codes: 0 = no issue, 1 = issues found, anything else = Ruff failed.
_SUCCESS_EXIT_CODES = (0, 1)

# The Ruff program installed next to this Python, as an absolute path. Two simpler
# options are avoided on purpose:
# - `python -m ruff` starts Ruff as a second process on Windows, which the timeout
#   cannot stop, and it can run a `ruff.py` found in the current directory.
# - `import ruff` could also load such a file into this process.
_RUFF_PATH = Path(sysconfig.get_path("scripts")) / (
    "ruff" + (sysconfig.get_config_var("EXE") or "")
)

_RUFF_COMMAND = (
    str(_RUFF_PATH),
    "check",
    "--isolated",  # Ignore configuration files: results stay reproducible.
    "--no-cache",  # Do not write a cache to disk.
    "--ignore-noqa",  # Analyzed code cannot hide its own issues.
    "--select=E,F,I,B",
    "--output-format=json",
    "--stdin-filename=input.py",  # Fixed name: never trust a user-supplied path.
    "-",  # Read the code from standard input.
)

# Ruff reports the same severity for almost everything, so the project ranks rules
# itself. Rules are matched by prefix, and the first matching level wins.
_ERROR_PREFIXES = (
    "invalid-syntax",
    "E9",  # Errors reported at parsing time.
    "F7",  # Misplaced statements, such as `return` outside a function.
    "F82",  # Undefined names.
)
_WARNING_PREFIXES = (
    "F",  # Pyflakes: likely bugs, such as unused imports.
    "B",  # Bugbear: risky patterns, such as mutable default arguments.
)


class AnalysisError(Exception):
    """The analysis could not be completed, so its result is unknown.

    This is different from an empty result, which means that no issue was found.
    """


def analyze_file(path: str | Path) -> list[Issue]:
    """Return the issues found in a UTF-8 Python file, without executing it."""
    try:
        # "utf-8-sig" also accepts the BOM that some Windows editors add.
        source = Path(path).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as error:
        raise AnalysisError(f"Cannot read file '{path}': {error}") from error

    return analyze_source(source)


def analyze_bytes(content: bytes) -> list[Issue]:
    """Return the issues found in UTF-8 file content, without executing it."""
    try:
        # "utf-8-sig" also accepts the BOM that some Windows editors add.
        source = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise AnalysisError("The file is not valid UTF-8 text.") from error

    return analyze_source(source)


def analyze_source(source: str) -> list[Issue]:
    """Return the issues found in Python source code, without executing it."""
    return _parse_ruff_output(_run_ruff(source))


def _run_ruff(source: str) -> bytes:
    """Send the code to Ruff through standard input and return its raw output."""
    try:
        # Bytes are used on both sides because the default text encoding on Windows
        # is not always UTF-8, which would corrupt accented characters.
        result = subprocess.run(
            _RUFF_COMMAND,
            input=source.encode("utf-8"),
            capture_output=True,
            timeout=_TIMEOUT_SECONDS,
        )
    except UnicodeEncodeError as error:
        raise AnalysisError(f"The code is not valid text: {error}") from error
    except subprocess.TimeoutExpired as error:
        raise AnalysisError(f"Ruff timed out after {_TIMEOUT_SECONDS}s.") from error
    except OSError as error:
        raise AnalysisError(f"Cannot start Ruff: {error}") from error

    if result.returncode not in _SUCCESS_EXIT_CODES:
        details = result.stderr.decode("utf-8", errors="replace").strip()
        raise AnalysisError(f"Ruff failed ({result.returncode}): {details}")

    return result.stdout


def _parse_ruff_output(output: bytes) -> list[Issue]:
    """Convert Ruff's JSON output into Issue objects."""
    try:
        return [_to_issue(item) for item in json.loads(output)]
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise AnalysisError(f"Unexpected Ruff output: {error}") from error


def _to_issue(item: dict) -> Issue:
    """Convert one Ruff diagnostic into an Issue."""
    return Issue(
        rule_code=item["code"],
        message=item["message"],
        severity=_severity_for(item["code"]),
        line=item["location"]["row"],
        column=item["location"]["column"],
        fix=_to_fix(item.get("fix")),
    )


def _to_fix(value: object) -> RuffFix | None:
    """Read Ruff's own fix, if any. It is only stored here, never applied.

    The fix is an optional extra: if its shape is unexpected (for example after a
    Ruff update), it is dropped, and the issue itself is still reported.
    """
    if not isinstance(value, dict):
        return None
    try:
        applicability = FixApplicability(value["applicability"])
        message = value.get("message") or ""
        edits = tuple(_to_edit(edit) for edit in value["edits"])
    except (KeyError, TypeError, ValueError):
        return None
    if not edits or not isinstance(message, str):
        return None
    return RuffFix(applicability, message, edits)


def _to_edit(edit: dict) -> RuffEdit:
    numbers = (
        edit["location"]["row"],
        edit["location"]["column"],
        edit["end_location"]["row"],
        edit["end_location"]["column"],
    )
    content = edit["content"]
    # bool is checked because True is also an int in Python.
    if not all(isinstance(n, int) and not isinstance(n, bool) for n in numbers):
        raise ValueError("Invalid position")
    if min(numbers) < 1 or not isinstance(content, str):
        raise ValueError("Invalid edit")
    return RuffEdit(*numbers, content)


def _severity_for(rule_code: str) -> Severity:
    """Return the project's severity level for a Ruff rule."""
    if rule_code.startswith(_ERROR_PREFIXES):
        return Severity.ERROR
    if rule_code.startswith(_WARNING_PREFIXES):
        return Severity.WARNING
    return Severity.INFO
