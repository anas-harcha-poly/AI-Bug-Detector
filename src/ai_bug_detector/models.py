"""Data shared across the project, independent of the tool that finds the issues."""

from dataclasses import dataclass
from enum import StrEnum


class Severity(StrEnum):
    """How much attention an issue deserves."""

    ERROR = "error"  # The code is invalid or will probably crash.
    WARNING = "warning"  # Possible bug or suspicious code.
    INFO = "info"  # Style and readability.


class FixApplicability(StrEnum):
    """How safe Ruff considers its own fix. None of them is ever applied by itself."""

    SAFE = "safe"  # Should not change what the code does.
    UNSAFE = "unsafe"  # May change what the code does.
    DISPLAY_ONLY = "display-only"  # Shown for information: not meant to be applied.


@dataclass(frozen=True)
class RuffEdit:
    """Replace the text between two positions with `content` (empty: delete).

    Positions start at 1. Columns count characters, as Python strings do. The end
    position is excluded, and equal to the start for an insertion.
    """

    start_line: int
    start_column: int
    end_line: int
    end_column: int
    content: str


@dataclass(frozen=True)
class RuffFix:
    """A fix computed by Ruff: one or more edits to apply together."""

    applicability: FixApplicability
    message: str
    edits: tuple[RuffEdit, ...]


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
    fix: RuffFix | None = None  # Only Ruff's own fix, when it offers one.
