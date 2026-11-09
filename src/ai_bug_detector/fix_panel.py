"""Review suggested fixes one by one, then preview the corrected code.

Fixes come from Ruff, and from the active AI review (the demo, or Claude). The user
accepts or leaves each one. Nothing is ever applied to the uploaded file: accepted
fixes only produce a preview, computed in memory. Nothing is run either.

Text that comes from the AI or from the analyzed code is shown with st.text and
st.code, never as Markdown. Checkbox labels are fixed texts for the same reason.
"""

import codecs
import hashlib
from dataclasses import dataclass

import streamlit as st

from ai_bug_detector.ai_models import ExplanationReview, LogicReview
from ai_bug_detector.fixes import (
    FixCheck,
    FixError,
    FixOrigin,
    ProposedFix,
    UnusableSuggestion,
    apply_fixes,
    check_fix,
    corrected_file_name,
    find_conflicts,
    fixes_from_issues,
    fixes_from_review,
    unified_diff,
)
from ai_bug_detector.models import FixApplicability, Issue

# Every check reads the whole file, so the section stays within these limits.
MAX_FIX_SOURCE_BYTES = 256 * 1024
MAX_FIXES = 50

_CACHE_KEY = "fix_panel_cache"
_KEY_PREFIX = "fix_accept"

_ORIGIN_LABELS = {
    FixOrigin.DEMO: "Demo (illustrative example)",
    FixOrigin.CLAUDE: "Claude",
}


@dataclass(frozen=True)
class FixEntry:
    """One line of the list: a fix that can be accepted, or one that cannot."""

    number: int  # Shown to the user as "Fix #number".
    origin_label: str
    description: str
    key: str  # Checkbox key: changes with the analysis and with the fix itself.
    fix: ProposedFix | None = None
    check: FixCheck | None = None
    unusable: UnusableSuggestion | None = None
    overlaps: tuple[int, ...] = ()  # Numbers of fixes that change the same lines.

    @property
    def applicable(self) -> bool:
        return self.fix is not None and self.check.applicable


def show_upload_fixes(
    uploaded_file, analysis: dict, reviews: list[ExplanationReview | LogicReview]
) -> None:
    """The fixes for the uploaded file: Ruff's, and Claude's when there are any."""
    if analysis["error"] or analysis.get("is_empty"):
        return
    content = uploaded_file.getvalue()
    show_fix_panel(
        context=f"upload-{analysis['run_id']}",
        file_name=uploaded_file.name,
        # The exact text Ruff analyzed: its positions refer to it.
        source=content.decode("utf-8-sig"),
        issues=analysis["issues"],
        reviews=[(review, FixOrigin.CLAUDE) for review in reviews],
        keep_bom=content.startswith(codecs.BOM_UTF8),
    )


def show_fix_panel(
    *,
    context: str,
    file_name: str,
    source: str,
    issues: list[Issue],
    reviews: list[tuple[ExplanationReview | LogicReview, FixOrigin]],
    keep_bom: bool = False,
) -> None:
    """Display the fixes of one analysis, the checkboxes, and the preview."""
    st.divider()
    st.subheader("Suggested fixes (optional)")
    st.caption(
        "Accept the fixes you want, one by one. Nothing is applied to your file: "
        "accepted fixes only build a preview below, and no code is run."
    )

    if len(source.encode("utf-8")) > MAX_FIX_SOURCE_BYTES:
        st.caption(
            f"Fixes are offered for files up to {MAX_FIX_SOURCE_BYTES // 1024} KB."
        )
        return

    entries, hidden = _entries(context, source, issues, reviews)
    if not entries:
        st.caption("No fix is available for this file.")
        return
    if hidden:
        st.caption(f"Only the first {MAX_FIXES} fixes are shown ({hidden} more).")

    accepted = [entry for entry in entries if _show_entry(entry)]
    _show_preview(context, file_name, source, accepted, keep_bom)


def _entries(
    context: str,
    source: str,
    issues: list[Issue],
    reviews: list[tuple[ExplanationReview | LogicReview, FixOrigin]],
) -> tuple[list[FixEntry], int]:
    """Prepare and check every fix once per analysis, then reuse the result.

    Streamlit reruns the page after each click, and each check reads the whole file.
    """
    token = _digest(context, source, repr(_with_fixes(issues)), repr(reviews))
    slot = context.split("-")[0]  # One place for the uploaded file, one for the demo.
    cache = st.session_state.setdefault(_CACHE_KEY, {})
    if slot not in cache or cache[slot][0] != token:
        cache[slot] = (token, _build_entries(context, source, issues, reviews))
    return cache[slot][1]


def _build_entries(
    context: str,
    source: str,
    issues: list[Issue],
    reviews: list[tuple[ExplanationReview | LogicReview, FixOrigin]],
) -> tuple[list[FixEntry], int]:
    ruff_issues = _with_fixes(issues)
    fixes, unusable = fixes_from_issues(source, ruff_issues[:MAX_FIXES])
    for review, origin in reviews:
        more_fixes, more_unusable = fixes_from_review(source, review, origin)
        fixes += more_fixes
        unusable += more_unusable

    fixes.sort(key=lambda fix: (fix.start_line, fix.end_line))
    total = len(fixes) + len(unusable) + max(0, len(ruff_issues) - MAX_FIXES)
    fixes = fixes[:MAX_FIXES]
    unusable = unusable[: MAX_FIXES - len(fixes)]
    numbers = {fix.fix_id: number for number, fix in enumerate(fixes, 1)}

    overlaps: dict[str, list[int]] = {fix.fix_id: [] for fix in fixes}
    for first, second in find_conflicts(fixes):
        overlaps[first].append(numbers[second])
        overlaps[second].append(numbers[first])

    entries = [
        FixEntry(
            number=numbers[fix.fix_id],
            origin_label=_origin_label(fix),
            description=fix.description,
            key=_key(context, fix.fix_id, fix),
            fix=fix,
            check=check_fix(source, fix),
            overlaps=tuple(sorted(overlaps[fix.fix_id])),
        )
        for fix in fixes
    ]
    entries += [
        FixEntry(
            number=len(fixes) + index,
            origin_label=_ORIGIN_LABELS.get(item.origin, "Ruff"),
            description=item.description,
            key=_key(context, item.suggestion_id, item),
            unusable=item,
        )
        for index, item in enumerate(unusable, 1)
    ]
    return entries, total - len(entries)


def _show_entry(entry: FixEntry) -> bool:
    """Display one fix. Return True if the user accepted it."""
    with st.container(border=True):
        # Only fixed texts and numbers go into Markdown.
        st.caption(f"Fix #{entry.number} · {entry.origin_label}{_lines(entry)}")
        st.text(entry.description)

        if entry.fix is not None:
            st.caption("Current code")
            st.code(code_for_display(entry.fix.original), language="python")
            st.caption("Suggested code")
            st.code(
                code_for_display(entry.fix.replacement) or "(these lines are removed)",
                language="python",
            )
            for warning in entry.check.warnings:
                st.caption(f"⚠️ {warning}")
            if entry.overlaps:
                others = ", ".join(f"#{number}" for number in entry.overlaps)
                st.caption(
                    f"Changes the same lines as fix {others}: only one of them can "
                    "be applied."
                )

        if not entry.applicable:
            reason = (
                entry.unusable.reason
                if entry.unusable is not None
                else entry.check.problems[0]
            )
            st.caption("Cannot be accepted:")
            st.text(reason)  # May quote the code: never shown as Markdown.

        return (
            st.checkbox(
                "Accept this fix",
                key=entry.key,
                disabled=not entry.applicable,
            )
            and entry.applicable
        )


def _show_preview(
    context: str,
    file_name: str,
    source: str,
    accepted: list[FixEntry],
    keep_bom: bool,
) -> None:
    """Show the corrected code, and offer it for download only if it is valid."""
    st.markdown("#### Preview")
    if not accepted:
        st.caption("Accept one or more fixes to preview the corrected code.")
        return

    numbers = {entry.fix.fix_id: entry.number for entry in accepted}
    fixes = [entry.fix for entry in accepted]
    conflicts = find_conflicts(fixes)
    if conflicts:
        pairs = "; ".join(
            f"fix #{numbers[first]} and fix #{numbers[second]}"
            for first, second in conflicts
        )
        st.error(
            f"These accepted fixes change the same lines, so they cannot be applied "
            f"together: {pairs}. Keep only one fix of each pair."
        )
        return

    try:
        result = apply_fixes(source, fixes)
    except FixError as error:
        st.error("The accepted fixes cannot be applied together.")
        st.text(str(error))
        return

    st.info(
        f"Preview with {len(fixes)} accepted fix(es). Your file has not been changed, "
        "and this code has not been run."
    )
    st.caption("Changes")
    st.code(
        code_for_display(unified_diff(source, result.corrected, file_name)),
        language="diff",
    )
    st.caption("Corrected code")
    st.code(code_for_display(result.corrected), language="python", line_numbers=True)

    # Only reached when the preview above is valid: no fix, a conflict or an error
    # returns earlier, so there is nothing to download in those cases.
    name = corrected_file_name(file_name)
    st.download_button(
        "Download corrected code",
        data=download_bytes(result.corrected, keep_bom),
        file_name=name,
        mime="text/x-python",
        key=f"{_KEY_PREFIX}_download_{_digest(context)}",
        on_click="ignore",  # Downloading does not rerun the page.
    )
    st.caption(
        f"Saves a new file, {name}, with the code shown above and your file's own "
        "line breaks. Your original file is not changed, and this code is not run."
    )


def download_bytes(corrected: str, keep_bom: bool) -> bytes:
    """The bytes of the corrected file: the preview's text, encoded like the original.

    The text keeps its own line breaks (\\r\\n for a Windows file). Only the preview
    on screen shows them unified.
    """
    data = corrected.encode("utf-8")
    return codecs.BOM_UTF8 + data if keep_bom else data


def code_for_display(text: str) -> str:
    """Prepare text for st.code so that every line, blank ones included, shows.

    st.code drops one line break at the start and one at the end. A file that starts
    with a blank line would lose it on screen, and every line number shown would be
    off by one. Line breaks are also unified, for display only.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return "\n" + text if text.startswith("\n") else text


def _with_fixes(issues: list[Issue]) -> list[Issue]:
    return [issue for issue in issues if issue.fix is not None]


def _origin_label(fix: ProposedFix) -> str:
    if fix.origin is FixOrigin.RUFF:
        safe = fix.applicability is FixApplicability.SAFE
        return "Ruff · safe" if safe else "Ruff · unsafe"
    return _ORIGIN_LABELS[fix.origin]


def _lines(entry: FixEntry) -> str:
    if entry.fix is None:
        return ""
    start, end = entry.fix.start_line, entry.fix.end_line
    return f" · line {start}" if start == end else f" · lines {start}–{end}"


def _key(context: str, item_id: str, content: object) -> str:
    return f"{_KEY_PREFIX}_{_digest(context, item_id, repr(content))}"


def _digest(*parts: str) -> str:
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()[:24]
