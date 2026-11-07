"""The optional AI part of the page: the demo, consent, and how results are displayed.

Text from the AI goes through st.text and st.code, which show it literally. Markdown
could turn an answer into links or external images, so it is never used for that text.
Suggested fixes are only displayed: nothing here can apply or run them.
"""

from collections.abc import Callable

import streamlit as st

from ai_bug_detector.ai_models import AiReviewError, ExplanationReview, LogicReview
from ai_bug_detector.ai_prompts import (
    EXPLANATION_SYSTEM_PROMPT,
    LOGIC_BUG_SYSTEM_PROMPT,
    MAX_ISSUES,
    MAX_SOURCE_BYTES,
    build_explanation_message,
    build_logic_bug_message,
    select_issues,
)
from ai_bug_detector.ai_review import (
    AI_MODELS,
    ClaudeReviewer,
    DemoReviewer,
    actual_cost,
    estimate_max_cost,
    live_ai_enabled,
    read_demo_source,
    sdk_available,
)
from ai_bug_detector.ruff_analyzer import AnalysisError, analyze_bytes

DEMO_OPEN_KEY = "ai_demo_open"
RESULTS_KEY = "ai_results"
RESULTS_FILE_KEY = "ai_results_file"

EXPLAIN_ACTION = "explain"
LOGIC_ACTION = "logic"

DEMO_NOTICE = (
    "Illustrative examples written by hand. These are not real responses from Claude. "
    "No code is sent anywhere and no API key is needed."
)
CONSENT_LABEL = "I agree to send this file's code to Anthropic for this review."


def show_ai_panel(uploaded_file, analysis: dict) -> None:
    """Display the optional AI review of an analyzed file."""
    if analysis["error"] or analysis.get("is_empty"):
        return

    st.divider()
    st.subheader("AI review (optional)")

    if not live_ai_enabled():
        st.caption(
            "Real AI review is off in this version and nothing is sent to any AI "
            "service. It can be turned on when you run the app on your own computer: "
            "see the README. The demo below shows what it looks like."
        )
    elif not sdk_available():
        st.caption(
            "Real AI review needs the optional Anthropic SDK: see the README. "
            "Ruff keeps working without it."
        )
    else:
        _show_live_review(uploaded_file, analysis["issues"])


def show_demo(show_issues: Callable[[list], None]) -> None:
    """Display the demo: real Ruff results and hand-written AI examples."""
    st.divider()
    st.subheader("Demo")

    is_open = st.session_state.get(DEMO_OPEN_KEY, False)
    st.button(
        "Close demo" if is_open else "Try the demo",
        key="demo_toggle",
        on_click=_toggle_demo,
    )

    if not is_open:
        st.caption("See every feature on a sample file, for free and without a key.")
        return

    st.warning(DEMO_NOTICE)
    content = read_demo_source()
    source = content.decode("utf-8")

    st.markdown("#### Sample file")
    st.code(source, language="python", line_numbers=True)

    try:
        issues = analyze_bytes(content)
    except AnalysisError as error:
        st.error(f"The analysis could not be completed. {error}")
        return

    st.markdown("#### Ruff results (real analysis)")
    show_issues(issues)

    reviewer = DemoReviewer()
    st.markdown("#### Explanations (illustrative)")
    _show_review(reviewer.explain_issues(source, issues), source)
    st.markdown("#### Possible logic bugs (illustrative)")
    _show_review(reviewer.find_logic_bugs(source), source)


def _toggle_demo() -> None:
    """Runs before the page is redrawn, so the button shows the right label at once."""
    st.session_state[DEMO_OPEN_KEY] = not st.session_state.get(DEMO_OPEN_KEY, False)


def _show_live_review(uploaded_file, issues: list) -> None:
    """Ask for a key and consent, then make a real call when a button is clicked."""
    content = uploaded_file.getvalue()
    if len(content) > MAX_SOURCE_BYTES:
        st.warning(
            f"AI review is limited to {MAX_SOURCE_BYTES // 1024} KB of code for now, "
            "and this file is larger. Ruff results are not affected."
        )
        return
    source = content.decode("utf-8-sig")  # Ruff already accepted this file as UTF-8.
    selected_issues = select_issues(issues)

    api_key = st.text_input(
        "Anthropic API key",
        type="password",
        key="ai_api_key",
        autocomplete="off",
        help="Kept in memory for this session only. It is never saved to a file.",
    ).strip()
    model_id = st.selectbox(
        "Model",
        [model.model_id for model in AI_MODELS],
        format_func={model.model_id: model.label for model in AI_MODELS}.get,
        key="ai_model",
    )

    results = _results_for(uploaded_file.file_id)

    explain_cost = estimate_max_cost(
        build_explanation_message(source, selected_issues),
        EXPLANATION_SYSTEM_PROMPT,
        model_id,
    )
    logic_cost = estimate_max_cost(
        build_logic_bug_message(source), LOGIC_BUG_SYSTEM_PROMPT, model_id
    )
    st.warning(
        "**Before you continue:** the code of this file and its Ruff results will be "
        "sent to the Anthropic API. The file name is not sent. Anthropic may keep this "
        "data for up to 30 days, and does not use API data for training by default. "
        "Do not send passwords, keys or other secrets.\n\n"
        f"Estimated maximum cost on your API account: ${explain_cost:.3f} to explain "
        f"issues, ${logic_cost:.3f} to search for logic bugs. Only one request is made "
        "per click, and nothing is retried."
    )
    if len(issues) > MAX_ISSUES:
        st.caption(
            f"Only the {MAX_ISSUES} most severe of {len(issues)} issues will be "
            "explained."
        )

    # The key includes the file and model: changing either one asks for consent again.
    consent = st.checkbox(
        CONSENT_LABEL, key=f"ai_consent_{uploaded_file.file_id}_{model_id}"
    )
    can_send = consent and bool(api_key)

    explain_column, logic_column = st.columns(2)
    explain_clicked = explain_column.button(
        "Explain Ruff issues",
        key="ai_explain",
        disabled=(
            not can_send
            or not selected_issues
            or _is_done(results, model_id, EXPLAIN_ACTION)
        ),
    )
    logic_clicked = logic_column.button(
        "Search for logic bugs",
        key="ai_logic",
        disabled=not can_send or _is_done(results, model_id, LOGIC_ACTION),
    )

    # Checked again here: this is the last step before code leaves the computer.
    if can_send and (explain_clicked or logic_clicked):
        reviewer = ClaudeReviewer(api_key, model_id)
        with st.spinner("Waiting for Claude..."):
            if explain_clicked:
                results[model_id, EXPLAIN_ACTION] = _call(
                    lambda: reviewer.explain_issues(source, selected_issues)
                )
            else:
                results[model_id, LOGIC_ACTION] = _call(
                    lambda: reviewer.find_logic_bugs(source)
                )
        st.rerun()  # Updates the buttons above, which are drawn before the call.

    for action, title in (
        (EXPLAIN_ACTION, "Explanations from Claude"),
        (LOGIC_ACTION, "Possible logic bugs from Claude"),
    ):
        result = results.get((model_id, action))
        if isinstance(result, str):
            st.error(result)
        elif result is not None:
            st.markdown(f"#### {title}")
            _show_review(result, source, model_id)


def _results_for(file_id: str) -> dict:
    """Return the stored results of this upload, and forget those of any other one."""
    if st.session_state.get(RESULTS_FILE_KEY) != file_id:
        st.session_state[RESULTS_FILE_KEY] = file_id
        st.session_state[RESULTS_KEY] = {}
    return st.session_state[RESULTS_KEY]


def _is_done(results: dict, model_id: str, action: str) -> bool:
    """Return True if this review succeeded, so it is not paid for a second time."""
    result = results.get((model_id, action))
    return result is not None and not isinstance(result, str)


def _call(review: Callable) -> ExplanationReview | LogicReview | str:
    """Return the review, or a message that is safe to show if it failed."""
    try:
        return review()
    except AiReviewError as error:
        return str(error)


def _show_review(
    review: ExplanationReview | LogicReview,
    source: str,
    model_id: str | None = None,
) -> None:
    """Display a review, whichever reviewer produced it."""
    lines = source.splitlines()
    if isinstance(review, ExplanationReview):
        if not review.explanations:
            st.text("No explanations were returned.")
        for item in review.explanations:
            _show_explanation(item, lines)
    else:
        st.caption("These are unverified hypotheses: the code was not run.")
        if not review.bugs:
            st.text("No possible logic bugs were reported.")
        for item in review.bugs:
            _show_logic_bug(item, lines)

    if review.ignored_count:
        st.caption(
            f"{review.ignored_count} part(s) of the answer failed validation and "
            "were ignored."
        )
    if review.usage and model_id:
        st.caption(
            f"Used {review.usage.input_tokens} input and "
            f"{review.usage.output_tokens} output tokens: about "
            f"${actual_cost(review.usage, model_id):.4f}."
        )


def _show_explanation(item, lines: list[str]) -> None:
    with st.container(border=True):
        # rule_code is safe here: validation only keeps codes that Ruff really reported.
        st.caption(f"Line {item.line} · Rule {item.rule_code}")
        st.text(item.explanation)
        st.text(item.context)
        _show_fix(item.suggested_fix, item.line, lines)


def _show_logic_bug(item, lines: list[str]) -> None:
    with st.container(border=True):
        st.caption(f"Line {item.line} · Confidence: {item.confidence.value}")
        st.text(item.title)
        st.text(item.reasoning)
        _show_fix(item.suggested_fix, item.line, lines)


def _show_fix(suggested_fix: str, line: int, lines: list[str]) -> None:
    if not suggested_fix.strip():
        return
    st.caption(f"Current code (line {line})")
    st.code(lines[line - 1] if line <= len(lines) else "", language="python")
    st.caption("Suggested change: not applied, review it before using it")
    st.code(suggested_fix, language="python")
