"""Tests for the display preparation: sorting, counters and table rows."""

from ai_bug_detector.models import Issue, Severity
from ai_bug_detector.report import count_by_severity, sort_issues, to_table_rows


def make_issue(severity: Severity, line: int = 1, column: int = 1) -> Issue:
    return Issue("X000", "message", severity, line, column)


def test_sort_puts_most_severe_first_even_on_later_lines():
    info = make_issue(Severity.INFO, line=1)
    warning = make_issue(Severity.WARNING, line=2)
    error = make_issue(Severity.ERROR, line=3)

    assert sort_issues([info, warning, error]) == [error, warning, info]


def test_sort_uses_position_within_same_severity():
    later_line = make_issue(Severity.ERROR, line=9, column=1)
    later_column = make_issue(Severity.ERROR, line=2, column=5)
    first = make_issue(Severity.ERROR, line=2, column=1)

    assert sort_issues([later_line, later_column, first]) == [
        first,
        later_column,
        later_line,
    ]


def test_count_includes_severities_without_issues():
    issues = [make_issue(Severity.ERROR), make_issue(Severity.ERROR)]

    assert count_by_severity(issues) == {
        Severity.ERROR: 2,
        Severity.WARNING: 0,
        Severity.INFO: 0,
    }


def test_table_rows_contain_requested_fields_sorted_by_severity():
    issues = [
        Issue("I001", "Unsorted imports", Severity.INFO, 1, 1),
        Issue("F821", "Undefined name", Severity.ERROR, 6, 12),
    ]

    assert to_table_rows(issues) == [
        {
            "Severity": "🔴 ERROR",
            "Line": 6,
            "Description": "Undefined name",
            "Rule": "F821",
        },
        {
            "Severity": "🔵 INFO",
            "Line": 1,
            "Description": "Unsorted imports",
            "Rule": "I001",
        },
    ]
