"""Tests for the Ruff analyzer, using small code samples that contain problems."""

import subprocess
import time

import pytest

from ai_bug_detector import ruff_analyzer
from ai_bug_detector.models import Issue, Severity
from ai_bug_detector.ruff_analyzer import (
    AnalysisError,
    analyze_bytes,
    analyze_file,
    analyze_source,
)


def find_issue(issues: list[Issue], rule_code: str) -> Issue:
    return next(issue for issue in issues if issue.rule_code == rule_code)


def fake_ruff_result(returncode=0, stdout=b"[]", stderr=b""):
    """Return a stand-in for subprocess.run with a chosen Ruff result."""

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)

    return fake_run


def raise_when_run(error):
    """Return a stand-in for subprocess.run that fails with the given error."""

    def fake_run(command, **kwargs):
        raise error

    return fake_run


# --- Detection: rule, severity and location ---


@pytest.mark.parametrize(
    ("source", "rule_code", "severity", "line", "column"),
    [
        pytest.param(
            "def f(:\n    pass\n",
            "invalid-syntax",
            Severity.ERROR,
            1,
            7,
            id="syntax-error",
        ),
        pytest.param(
            "x = 1\nprint(x, missing)\n",
            "F821",
            Severity.ERROR,
            2,
            10,
            id="undefined-name",
        ),
        pytest.param(
            "return 1\n",
            "F706",
            Severity.ERROR,
            1,
            1,
            id="return-outside-function",
        ),
        pytest.param(
            "def f(items=[]):\n    return items\n",
            "B006",
            Severity.WARNING,
            1,
            13,
            id="mutable-default-argument",
        ),
        pytest.param(
            "import os\n",
            "F401",
            Severity.WARNING,
            1,
            8,
            id="unused-import",
        ),
        pytest.param(
            "x = 1\nif x == None:\n    pass\n",
            "E711",
            Severity.INFO,
            2,
            9,
            id="comparison-to-none",
        ),
        pytest.param(
            "import sys\nimport os\n\nprint(sys, os)\n",
            "I001",
            Severity.INFO,
            1,
            1,
            id="unsorted-imports",
        ),
    ],
)
def test_detects_issue(source, rule_code, severity, line, column):
    issue = find_issue(analyze_source(source), rule_code)

    assert issue.severity is severity
    assert (issue.line, issue.column) == (line, column)
    assert issue.message


@pytest.mark.parametrize("source", ["", "x = 1\n"])
def test_clean_code_has_no_issue(source):
    assert analyze_source(source) == []


# --- Safety and reliability of the analysis ---


def test_analyzed_code_is_never_executed(tmp_path):
    marker = tmp_path / "executed.txt"
    source = f"open({str(marker)!r}, 'w').close()\nprint(missing)\n"

    issues = analyze_source(source)

    assert not marker.exists()
    assert find_issue(issues, "F821")  # The code was analyzed all the same.


def test_ruff_file_in_current_directory_is_never_executed(tmp_path, monkeypatch):
    marker = tmp_path / "executed.txt"
    (tmp_path / "ruff.py").write_text(
        f"open({str(marker)!r}, 'w').close()\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)

    issues = analyze_source("print(missing)\n")

    assert not marker.exists()
    assert find_issue(issues, "F821")


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("import os  # noqa\n", id="line-comment"),
        pytest.param("# ruff: noqa\nimport os\n", id="file-comment"),
    ],
)
def test_noqa_comments_cannot_hide_issues(source):
    assert find_issue(analyze_source(source), "F401")


def test_accented_characters_are_preserved():
    issues = analyze_source("print(résumé_été)\n")

    assert "résumé_été" in find_issue(issues, "F821").message


# --- Files ---


def test_analyze_file_detects_issue(tmp_path):
    path = tmp_path / "sample.py"
    path.write_text("print(missing)\n", encoding="utf-8")

    assert find_issue(analyze_file(path), "F821")


def test_analyze_file_accepts_utf8_bom(tmp_path):
    path = tmp_path / "bom.py"
    path.write_text("x = 1\n", encoding="utf-8-sig")

    assert analyze_file(path) == []


def test_analyze_file_rejects_missing_file(tmp_path):
    with pytest.raises(AnalysisError, match="Cannot read file"):
        analyze_file(tmp_path / "missing.py")


def test_analyze_file_rejects_directory(tmp_path):
    with pytest.raises(AnalysisError, match="Cannot read file"):
        analyze_file(tmp_path)


def test_analyze_file_rejects_non_utf8_file(tmp_path):
    path = tmp_path / "latin1.py"
    path.write_bytes("x = 'é'\n".encode("latin-1"))

    with pytest.raises(AnalysisError, match="Cannot read file"):
        analyze_file(path)


# --- Uploaded content ---


def test_analyze_bytes_detects_issue():
    assert find_issue(analyze_bytes(b"print(missing)\n"), "F821")


def test_analyze_bytes_accepts_utf8_bom():
    assert analyze_bytes("x = 1\n".encode("utf-8-sig")) == []


def test_analyze_bytes_rejects_non_utf8_content():
    with pytest.raises(AnalysisError, match="not valid UTF-8"):
        analyze_bytes("x = 'é'\n".encode("latin-1"))


# --- Failures: an analysis that failed must never look like clean code ---


@pytest.mark.parametrize(
    ("fake_run", "expected_message"),
    [
        pytest.param(
            fake_ruff_result(returncode=2, stderr=b"bad option"),
            "Ruff failed .*bad option",
            id="ruff-reports-failure",
        ),
        pytest.param(
            raise_when_run(subprocess.TimeoutExpired("ruff", 30)),
            "timed out",
            id="timeout",
        ),
        pytest.param(
            raise_when_run(FileNotFoundError("no such program")),
            "Cannot start Ruff",
            id="cannot-start",
        ),
        pytest.param(
            # What `python -m ruff` produces when the Ruff module is missing.
            fake_ruff_result(returncode=1, stdout=b""),
            "Unexpected Ruff output",
            id="exit-code-1-without-output",
        ),
        pytest.param(
            fake_ruff_result(stdout=b"not json"),
            "Unexpected Ruff output",
            id="invalid-json",
        ),
        pytest.param(
            fake_ruff_result(stdout=b'[{"code": "F821"}]'),
            "Unexpected Ruff output",
            id="missing-field",
        ),
        pytest.param(
            fake_ruff_result(stdout=b"[42]"),
            "Unexpected Ruff output",
            id="wrong-item-type",
        ),
        pytest.param(
            fake_ruff_result(
                stdout=(
                    b'[{"code": null, "message": "m",'
                    b' "location": {"row": 1, "column": 1}}]'
                )
            ),
            "Unexpected Ruff output",
            id="null-rule-code",
        ),
    ],
)
def test_ruff_failure_raises_analysis_error(monkeypatch, fake_run, expected_message):
    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(AnalysisError, match=expected_message):
        analyze_source("x = 1\n")


def test_timeout_really_stops_a_slow_analysis(monkeypatch):
    # A real Ruff run, not a simulation: Ruff is very slow on one huge comment line.
    monkeypatch.setattr(ruff_analyzer, "_TIMEOUT_SECONDS", 1)
    start = time.monotonic()

    with pytest.raises(AnalysisError, match="timed out"):
        analyze_source("#" * (5 * 1024 * 1024))

    assert time.monotonic() - start < 10


def test_text_that_cannot_be_encoded_raises_analysis_error():
    with pytest.raises(AnalysisError, match="not valid text"):
        analyze_source("x = '\udc80'\n")
