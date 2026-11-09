"""AI review data, and the validation of answers before they reach the screen.

An AI answer is untrusted input, like the code it talks about: it can be incomplete,
invented or influenced by instructions hidden in the analyzed code. Everything here is
plain Python, so the free mode works without the Anthropic SDK.
"""

from dataclasses import dataclass
from enum import StrEnum

from ai_bug_detector.models import Issue

_MAX_ITEMS = 20
_MAX_TEXT_LENGTH = 1500
_MAX_FIX_LENGTH = 4000


class AiReviewError(Exception):
    """The AI review failed. The message is safe to show to the user."""


class Confidence(StrEnum):
    """How sure the AI says it is about a possible bug."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True)
class Usage:
    """Tokens used by one API call: this is what gets billed."""

    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class IssueExplanation:
    """An AI explanation of one problem found by Ruff."""

    rule_code: str
    line: int
    explanation: str
    context: str
    suggested_fix: str  # Empty when the AI proposes no fix.
    # The lines suggested_fix replaces. None when not given, as in older answers.
    fix_start_line: int | None = None
    fix_end_line: int | None = None


@dataclass(frozen=True)
class LogicBug:
    """A possible logic bug: a hypothesis from the AI, not a verified fact."""

    line: int
    title: str
    reasoning: str
    confidence: Confidence
    suggested_fix: str  # Empty when the AI proposes no fix.
    # The lines suggested_fix replaces. None when not given, as in older answers.
    fix_start_line: int | None = None
    fix_end_line: int | None = None


@dataclass(frozen=True)
class ExplanationReview:
    """The outcome of asking for explanations of Ruff issues."""

    explanations: list[IssueExplanation]
    ignored_count: int  # Items dropped because they failed validation.
    usage: Usage | None = None  # None when no API was called, as in the demo.


@dataclass(frozen=True)
class LogicReview:
    """The outcome of asking for a search for logic bugs."""

    bugs: list[LogicBug]
    ignored_count: int  # Items dropped because they failed validation.
    usage: Usage | None = None  # None when no API was called, as in the demo.


def _object_schema(properties: dict) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


_STRING = {"type": "string"}
_INTEGER = {"type": "integer"}
# 0 for both when there is no fix: null values in schemas are avoided on purpose.
_FIX_RANGE = {"fix_start_line": _INTEGER, "fix_end_line": _INTEGER}

# The API rejects length limits in a schema, so they are enforced in the code below.
EXPLANATION_SCHEMA = _object_schema(
    {
        "explanations": {
            "type": "array",
            "items": _object_schema(
                {
                    "rule_code": _STRING,
                    "line": _INTEGER,
                    "explanation": _STRING,
                    "context": _STRING,
                    "suggested_fix": _STRING,
                    **_FIX_RANGE,
                }
            ),
        }
    }
)
LOGIC_BUG_SCHEMA = _object_schema(
    {
        "bugs": {
            "type": "array",
            "items": _object_schema(
                {
                    "line": _INTEGER,
                    "title": _STRING,
                    "reasoning": _STRING,
                    "confidence": {
                        "type": "string",
                        "enum": [level.value for level in Confidence],
                    },
                    "suggested_fix": _STRING,
                    **_FIX_RANGE,
                }
            ),
        }
    }
)


def parse_explanations(data: object, issues: list[Issue]) -> ExplanationReview:
    """Keep only valid explanations of issues that were really sent to the AI."""
    known_issues = {(issue.rule_code, issue.line) for issue in issues}
    items, ignored_count = _read_items(data, "explanations")
    explanations = []
    for item in items:
        try:
            explanation = IssueExplanation(
                rule_code=_text(item, "rule_code"),
                line=_line(item),
                explanation=_text(item, "explanation"),
                context=_text(item, "context"),
                suggested_fix=_fix(item),
                **_fix_range(item),
            )
        except ValueError:
            ignored_count += 1
            continue
        if (explanation.rule_code, explanation.line) in known_issues:
            explanations.append(explanation)
        else:
            ignored_count += 1  # The AI invented an issue that Ruff never reported.
    return ExplanationReview(explanations, ignored_count)


def parse_logic_bugs(data: object, line_count: int) -> LogicReview:
    """Keep only valid possible bugs that point to a line that exists in the code."""
    items, ignored_count = _read_items(data, "bugs")
    bugs = []
    for item in items:
        try:
            bug = LogicBug(
                line=_line(item),
                title=_text(item, "title"),
                reasoning=_text(item, "reasoning"),
                confidence=_confidence(item),
                suggested_fix=_fix(item),
                **_fix_range(item),
            )
        except ValueError:
            ignored_count += 1
            continue
        if bug.line <= line_count:
            bugs.append(bug)
        else:
            ignored_count += 1  # The AI pointed to a line that does not exist.
    return LogicReview(bugs, ignored_count)


def _read_items(data: object, field: str) -> tuple[list, int]:
    """Return the items to validate, and how many were over the limit."""
    items = data.get(field) if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise AiReviewError("The AI answer did not have the expected format.")
    return items[:_MAX_ITEMS], max(0, len(items) - _MAX_ITEMS)


def _field(item: object, name: str) -> object:
    if not isinstance(item, dict) or name not in item:
        raise ValueError(f"Missing field: {name}")
    return item[name]


def _text(item: object, name: str) -> str:
    value = _field(item, name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Invalid text: {name}")
    if len(value) > _MAX_TEXT_LENGTH:
        raise ValueError(f"Text too long: {name}")
    return value


def _fix(item: object) -> str:
    value = _field(item, "suggested_fix")
    if not isinstance(value, str) or len(value) > _MAX_FIX_LENGTH:
        raise ValueError("Invalid suggested fix")
    return value


def _fix_range(item: object) -> dict[str, int | None]:
    """Read the lines a fix replaces. Missing or 0 means "not given".

    Only the types are checked here. Whether the range fits the file is checked when
    the fix is prepared, where the file is known.
    """
    values = {}
    for name in ("fix_start_line", "fix_end_line"):
        value = item.get(name) if isinstance(item, dict) else None
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or value < 0
        ):
            raise ValueError(f"Invalid line number: {name}")
        values[name] = value or None  # 0 becomes None.
    return values


def _line(item: object) -> int:
    value = _field(item, "line")
    # bool is checked because True is also an int in Python.
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError("Invalid line number")
    return value


def _confidence(item: object) -> Confidence:
    value = _field(item, "confidence")
    if not isinstance(value, str):
        raise ValueError("Invalid confidence")
    return Confidence(value.lower())  # Raises ValueError for an unknown level.
