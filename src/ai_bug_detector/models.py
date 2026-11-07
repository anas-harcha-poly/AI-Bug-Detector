"""Data shared across the project, independent of the tool that finds the issues."""

from dataclasses import dataclass
from enum import StrEnum


class Severity(StrEnum):
    """How much attention an issue deserves."""

    ERROR = "error"  # The code is invalid or will probably crash.
    WARNING = "warning"  # Possible bug or suspicious code.
    INFO = "info"  # Style and readability.


@dataclass(frozen=True)
class Issue:
    """A single problem found in the analyzed code.

    Line and column numbers start at 1, as in a code editor.
    """

    rule_code: str
    message: str
    severity: Severity
    line: int
    column: int
