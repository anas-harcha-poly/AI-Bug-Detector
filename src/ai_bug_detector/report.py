"""Prepare analysis results for display, independently of any user interface."""

from collections import Counter

from ai_bug_detector.models import Issue, Severity

# Most important first. Enum members keep their definition order.
_SEVERITY_RANK = {severity: rank for rank, severity in enumerate(Severity)}

_SEVERITY_ICONS = {
    Severity.ERROR: "🔴",
    Severity.WARNING: "🟠",
    Severity.INFO: "🔵",
}


def sort_issues(issues: list[Issue]) -> list[Issue]:
    """Return the issues by severity, most important first, then by position."""
    return sorted(
        issues,
        key=lambda issue: (_SEVERITY_RANK[issue.severity], issue.line, issue.column),
    )


def count_by_severity(issues: list[Issue]) -> dict[Severity, int]:
    """Return the number of issues per severity, including severities with none."""
    counts = Counter(issue.severity for issue in issues)
    return {severity: counts[severity] for severity in Severity}


def to_table_rows(issues: list[Issue]) -> list[dict[str, str | int]]:
    """Return one table row per issue, sorted by severity."""
    return [
        {
            "Severity": f"{_SEVERITY_ICONS[issue.severity]} {issue.severity.name}",
            "Line": issue.line,
            "Description": issue.message,
            "Rule": issue.rule_code,
        }
        for issue in sort_issues(issues)
    ]
