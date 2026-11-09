"""Prepare, check and apply suggested fixes, without ever running any code.

A fix replaces an explicit range of lines. It keeps a copy of the lines it expects to
replace and a fingerprint of the whole file, so it is never applied to a file that
changed since it was prepared. Suggestions are untrusted: each one is checked, and
nothing is applied unless the caller passes it explicitly, which the page will only do
after the user accepts it. This is also true for fixes Ruff calls safe.

The only check on the result is ast.parse: it reads the code's structure and never
runs it. Nothing here writes a file: the corrected code is returned as text.
"""

import ast
import bisect
import difflib
import functools
import hashlib
import itertools
import re
from dataclasses import dataclass, field
from enum import StrEnum

from ai_bug_detector.ai_models import (
    ExplanationReview,
    IssueExplanation,
    LogicBug,
    LogicReview,
)
from ai_bug_detector.models import FixApplicability, Issue, RuffFix

MAX_REPLACEMENT_LENGTH = 4000
# A suggestion that rewrites more lines than this is too large to review safely.
MAX_FIX_LINES = 40

# Python and Ruff count lines with \n, \r\n and \r only. str.splitlines() also splits
# on characters such as \x0c or the Unicode line separator, which would shift the
# line numbers.
_LINE = re.compile(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+\Z")
# Control characters, except tab and line breaks (\r is normalized to \n).
_FORBIDDEN_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_SAFE_FILE_STEM = re.compile(r"[^A-Za-z0-9_-]")


class FixError(Exception):
    """A fix cannot be prepared or applied. The message is safe to show."""


class FixOrigin(StrEnum):
    """Where a suggested fix comes from. None of them is trusted blindly."""

    RUFF = "ruff"
    DEMO = "demo"
    CLAUDE = "claude"


@dataclass(frozen=True)
class ProposedFix:
    """Replace lines start_line to end_line (1-based, inclusive) with `replacement`.

    An empty replacement deletes the lines.
    """

    fix_id: str
    origin: FixOrigin
    description: str
    start_line: int
    end_line: int
    replacement: str
    original: str  # The lines as they were when the fix was prepared.
    source_hash: str  # Fingerprint of the whole file it was prepared for.
    # Ruff's own opinion of its fix (safe or unsafe). None for other origins.
    applicability: FixApplicability | None = None


@dataclass(frozen=True)
class UnusableSuggestion:
    """A suggestion that cannot be turned into a fix, and why."""

    suggestion_id: str
    origin: FixOrigin
    description: str
    reason: str


@dataclass(frozen=True)
class FixCheck:
    """Whether a fix can be applied on its own to a given file."""

    fix_id: str
    problems: list[str] = field(default_factory=list)  # Any problem blocks the fix.
    warnings: list[str] = field(default_factory=list)  # Worth a look, not blocking.

    @property
    def applicable(self) -> bool:
        return not self.problems


@dataclass(frozen=True)
class FixResult:
    """The corrected code, computed in memory. The original file is not touched."""

    corrected: str
    applied: list[str]  # Fix ids, in the order of the file.


def source_hash(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def prepare_fix(
    source: str,
    *,
    fix_id: str,
    origin: FixOrigin,
    description: str,
    start_line: int,
    end_line: int,
    replacement: str,
) -> ProposedFix:
    """Return a fix after checking its range and its text. Raise FixError if invalid."""
    lines = _split_lines(source)
    if not _is_line_number(start_line) or not _is_line_number(end_line):
        raise FixError("The line numbers of this fix are not valid.")
    if start_line > end_line:
        raise FixError("This fix starts after it ends.")
    if end_line > len(lines):
        raise FixError(f"This fix points to line {end_line}, past the end of the file.")
    if not isinstance(replacement, str) or len(replacement) > MAX_REPLACEMENT_LENGTH:
        raise FixError("The suggested code is too long or not text.")
    if _FORBIDDEN_CHARACTERS.search(replacement):
        raise FixError("The suggested code contains invisible control characters.")

    replacement = _normalize(replacement)
    original = "".join(lines[start_line - 1 : end_line])
    if replacement == _normalize(original):
        raise FixError("The suggested code is identical to the current code.")

    return ProposedFix(
        fix_id=fix_id,
        origin=origin,
        description=description,
        start_line=start_line,
        end_line=end_line,
        replacement=replacement,
        original=original,
        source_hash=source_hash(source),
    )


def fixes_from_review(
    source: str, review: ExplanationReview | LogicReview, origin: FixOrigin
) -> tuple[list[ProposedFix], list[UnusableSuggestion]]:
    """Turn the suggestions of a review into fixes, or explain why they cannot be.

    A suggestion names the lines it replaces (fix_start_line to fix_end_line). Older
    answers without that range are still read: a one-line suggestion replaces the
    reported line, as before, and a longer one is not applicable. A range that is
    given but wrong is never replaced by a guess.
    """
    if isinstance(review, ExplanationReview):
        kind = "explanation"
        items = [
            (item, f"{item.rule_code}: {item.explanation}")
            for item in review.explanations
        ]
    else:
        kind = "logic"
        items = [(item, item.title) for item in review.bugs]

    fixes, unusable = [], []
    for index, (item, description) in enumerate(items):
        if not item.suggested_fix.strip():
            continue  # No fix was suggested.
        suggestion_id = f"{origin}:{kind}:{index}"
        try:
            start_line, end_line = _suggestion_range(item)
            fixes.append(
                prepare_fix(
                    source,
                    fix_id=suggestion_id,
                    origin=origin,
                    description=description,
                    start_line=start_line,
                    end_line=end_line,
                    replacement=item.suggested_fix,
                )
            )
        except FixError as error:
            unusable.append(
                UnusableSuggestion(suggestion_id, origin, description, str(error))
            )
    return fixes, unusable


def _suggestion_range(item: IssueExplanation | LogicBug) -> tuple[int, int]:
    """Return the lines a suggestion replaces, or raise FixError. Never guesses."""
    start, end, line = item.fix_start_line, item.fix_end_line, item.line
    # The AI sees the code as "17: ..." lines. Starting with the number of the first
    # replaced line means that prefix was copied by mistake.
    if item.suggested_fix.startswith(f"{start or line}: "):
        raise FixError(
            "The suggested code still contains the line numbers it was shown with."
        )
    if start is None and end is None:
        # An older answer, without a range.
        if "\n" in _normalize(item.suggested_fix):
            raise FixError(
                "This suggestion spans several lines but does not say which lines "
                "of the file it replaces, so it cannot be applied safely."
            )
        return line, line
    if start is None or end is None:
        raise FixError("This suggestion gives only part of the lines it replaces.")
    if start <= end and not start <= line <= end:
        raise FixError(
            f"This suggestion replaces lines {start} to {end}, which do not include "
            f"line {line}, where the problem is."
        )
    if end - start + 1 > MAX_FIX_LINES:
        raise FixError(
            f"This suggestion replaces more than {MAX_FIX_LINES} lines, which is too "
            "many to review safely."
        )
    return start, end  # prepare_fix checks the order and the end of the file.


def fixes_from_issues(
    source: str, issues: list[Issue]
) -> tuple[list[ProposedFix], list[UnusableSuggestion]]:
    """Turn Ruff's own fixes into fixes that can be checked and accepted one by one.

    `source` must be the exact text Ruff analyzed, because Ruff's positions refer to
    it. Safe and unsafe fixes are both returned, marked as such: none is applied.
    """
    fixes, unusable = [], []
    for index, issue in enumerate(issues):
        if issue.fix is None:
            continue
        fix_id = f"ruff:{index}:{issue.rule_code}"
        description = f"{issue.rule_code}: {issue.fix.message or issue.message}"
        if issue.fix.applicability is FixApplicability.DISPLAY_ONLY:
            unusable.append(
                UnusableSuggestion(
                    fix_id,
                    FixOrigin.RUFF,
                    description,
                    "Ruff only shows this fix for information: it is not meant to be "
                    "applied.",
                )
            )
            continue
        try:
            fixes.append(_fix_from_ruff(source, fix_id, description, issue.fix))
        except FixError as error:
            unusable.append(
                UnusableSuggestion(fix_id, FixOrigin.RUFF, description, str(error))
            )
    return fixes, unusable


def _fix_from_ruff(
    source: str, fix_id: str, description: str, ruff_fix: RuffFix
) -> ProposedFix:
    """Convert Ruff's character positions into a fix that replaces whole lines.

    The conversion is checked: the result must be exactly what applying Ruff's edits
    directly would give. Otherwise the fix is not offered.
    """
    lines = _split_lines(source)
    if not lines:
        raise FixError("The file is empty.")
    # starts[i] is where line i+1 begins. The extra last value is the end of the file.
    starts = list(itertools.accumulate((len(line) for line in lines), initial=0))

    edits = sorted(
        (
            (
                _offset(lines, starts, edit.start_line, edit.start_column),
                _offset(lines, starts, edit.end_line, edit.end_column),
                edit.content,
            )
            for edit in ruff_fix.edits
        ),
        key=lambda edit: edit[0],  # Stable: insertions at one place keep their order.
    )
    if any(end < start for start, end, _ in edits):
        raise FixError("Ruff's fix has an edit that ends before it starts.")
    for (_, previous_end, _), (start, _, _) in zip(edits, edits[1:], strict=False):
        if start < previous_end:
            raise FixError("Ruff's fix has edits that overlap.")

    expected = _apply_edits(source, edits)
    if expected == source:
        raise FixError("Ruff's fix does not change the code.")

    low = edits[0][0]
    high = max(end for _, end, _ in edits)
    first = _line_index(starts, low)
    last = _line_index(starts, max(high - 1, low))
    while True:
        unchanged_tail = len(source) - starts[last + 1]
        new_text = expected[starts[first] : len(expected) - unchanged_tail]
        joins_next_line = new_text and not new_text.endswith(("\n", "\r"))
        if joins_next_line and last + 1 < len(lines):
            last += 1  # The edit removed a line break: include the next line.
            continue
        break

    replacement = new_text.replace("\r\n", "\n").replace("\r", "\n")
    replacement = replacement.removesuffix("\n")
    fix = ProposedFix(
        fix_id=fix_id,
        origin=FixOrigin.RUFF,
        description=description,
        start_line=first + 1,
        end_line=last + 1,
        replacement=replacement,
        original="".join(lines[first : last + 1]),
        source_hash=source_hash(source),
        applicability=ruff_fix.applicability,
    )
    # Line breaks may differ on purpose: new lines follow the file's own line breaks.
    # Any other difference means the conversion lost something.
    if _unify_line_breaks(_replace(source, [fix])) != _unify_line_breaks(expected):
        raise FixError(
            "Ruff's fix could not be written as whole lines exactly, so it is not "
            "offered."
        )
    return fix


def _unify_line_breaks(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _offset(lines: list[str], starts: list[int], line: int, column: int) -> int:
    """Return the position in the file of a line and column (both from 1)."""
    if 1 <= line <= len(lines):
        content_length = len(lines[line - 1].rstrip("\r\n"))
        if column - 1 > content_length:
            raise FixError(f"Ruff's fix points past the end of line {line}.")
        return starts[line - 1] + column - 1
    if line == len(lines) + 1 and column == 1 and _line_ending(lines[-1]):
        return starts[-1]  # The very end of a file ending with a line break.
    raise FixError("Ruff's fix points outside the file.")


def _line_index(starts: list[int], offset: int) -> int:
    """Return the index of the line holding this position (the last line at the end)."""
    return min(bisect.bisect_right(starts, offset) - 1, len(starts) - 2)


def _apply_edits(source: str, edits: list[tuple[int, int, str]]) -> str:
    """Apply edits to the whole text, from the end so positions stay correct."""
    for start, end, content in reversed(edits):
        source = source[:start] + content + source[end:]
    return source


def check_fix(source: str, fix: ProposedFix) -> FixCheck:
    """Check, without running anything, that this fix alone can be applied."""
    problems = _staleness_problems(source, fix)
    warnings = []
    if fix.applicability is FixApplicability.UNSAFE:
        warnings.append(
            "Ruff marks this fix as unsafe: it may change what the code does. Review "
            "it before accepting it."
        )
    if not problems:
        corrected = _replace(source, [fix])
        problems += _syntax_problems(source, corrected)
        if not problems:
            warnings += _indentation_warnings(fix)
            if not _is_valid_python(source):
                warnings.append(
                    "The current file is not valid Python, so the result could not "
                    "be checked for syntax errors."
                )
    return FixCheck(fix.fix_id, problems, warnings)


def find_conflicts(fixes: list[ProposedFix]) -> list[tuple[str, str]]:
    """Return the pairs of fixes that change some of the same lines."""
    ordered = sorted(fixes, key=lambda fix: (fix.start_line, fix.end_line))
    conflicts = []
    for index, first in enumerate(ordered):
        for second in ordered[index + 1 :]:
            if second.start_line > first.end_line:
                break
            conflicts.append((first.fix_id, second.fix_id))
    return conflicts


def apply_fixes(source: str, accepted: list[ProposedFix]) -> FixResult:
    """Return the code with the accepted fixes applied. Raise FixError otherwise.

    All of them are applied, or none: a fix that is out of date, a conflict, or a
    result that is not valid Python stops everything.
    """
    ids = [fix.fix_id for fix in accepted]
    if len(ids) != len(set(ids)):
        raise FixError("The same fix was accepted twice.")
    for fix in accepted:
        problems = _staleness_problems(source, fix)
        if problems:
            raise FixError(f"Fix {fix.fix_id} cannot be applied: {problems[0]}")
    conflicts = find_conflicts(accepted)
    if conflicts:
        first, second = conflicts[0]
        raise FixError(
            f"Fixes {first} and {second} change the same lines. Accept only one."
        )

    corrected = _replace(source, accepted)
    problems = _syntax_problems(source, corrected)
    if problems:
        raise FixError(f"Together, the accepted fixes break the code: {problems[0]}")
    ordered = sorted(accepted, key=lambda fix: fix.start_line)
    return FixResult(corrected, [fix.fix_id for fix in ordered])


def unified_diff(original: str, corrected: str, file_name: str) -> str:
    """Return a preview of the changes, in the usual diff format."""
    return "".join(
        difflib.unified_diff(
            _split_lines(original),
            _split_lines(corrected),
            fromfile=file_name,
            tofile=corrected_file_name(file_name),
        )
    )


def corrected_file_name(file_name: str) -> str:
    """Return a safe name for the downloaded copy: the original is never replaced."""
    stem = file_name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].removesuffix(".py")
    stem = _SAFE_FILE_STEM.sub("_", stem)[:100] or "code"
    return f"{stem}_fixed.py"


def _staleness_problems(source: str, fix: ProposedFix) -> list[str]:
    if fix.source_hash != source_hash(source):
        return ["it was prepared for another version of the file."]
    lines = _split_lines(source)
    current = "".join(lines[fix.start_line - 1 : fix.end_line])
    if fix.end_line > len(lines) or current != fix.original:
        return ["the lines it replaces have changed."]
    return []


def _replace(source: str, fixes: list[ProposedFix]) -> str:
    """Apply fixes from the bottom up, so earlier line numbers stay correct."""
    lines = _split_lines(source)
    default_ending = "\r\n" if "\r\n" in source else "\n"
    for fix in sorted(fixes, key=lambda item: item.start_line, reverse=True):
        last_ending = _line_ending(lines[fix.end_line - 1])
        ending = last_ending or default_ending
        new_lines = (
            [line + ending for line in fix.replacement.split("\n")]
            if fix.replacement
            else []
        )
        if new_lines and not last_ending:
            # The last line of the file had no line break: keep it that way.
            new_lines[-1] = new_lines[-1][: -len(ending)]
        lines[fix.start_line - 1 : fix.end_line] = new_lines
    return "".join(lines)


def _syntax_problems(source: str, corrected: str) -> list[str]:
    """A fix must not turn valid Python into invalid Python."""
    if not _is_valid_python(source):
        return []
    try:
        ast.parse(corrected)
    except SyntaxError as error:
        return [f"the result is not valid Python ({error.msg}, line {error.lineno})."]
    except (ValueError, RecursionError, MemoryError):
        return ["the result could not be checked."]
    return []


@functools.lru_cache(maxsize=4)
def _is_valid_python(source: str) -> bool:
    # Cached: checking many fixes asks this about the same file each time.
    try:
        ast.parse(source)  # Reads the structure of the code: never runs it.
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return False
    return True


def _indentation_warnings(fix: ProposedFix) -> list[str]:
    old = _first_indentation(fix.original)
    new = _first_indentation(fix.replacement)
    if old is not None and new is not None and old != new:
        return [
            "The suggested code is indented differently from the current code: check "
            "that it stays in the right block."
        ]
    return []


def split_lines(text: str) -> list[str]:
    """Split like Python and Ruff do, keeping each line's own line break.

    Use this, not str.splitlines(), whenever line numbers must match the analysis.
    """
    return _LINE.findall(text)


_split_lines = split_lines


def _first_indentation(text: str) -> str | None:
    for line in _split_lines(text):
        line = line.rstrip("\r\n")
        if line.strip():
            return line[: len(line) - len(line.lstrip())]
    return None


def _normalize(text: str) -> str:
    """Use \\n only, and no line break at the end."""
    return text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")


def _line_ending(line: str) -> str:
    if line.endswith("\r\n"):
        return "\r\n"
    if line.endswith(("\n", "\r")):
        return line[-1]
    return ""


def _is_line_number(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1
