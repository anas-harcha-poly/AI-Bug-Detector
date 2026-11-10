"""The optional AI part of the page: the demo, consent, and how results are displayed.

Text from the AI goes through st.text and st.code, which show it literally. Markdown
could turn an answer into links or external images, so it is never used for that text.
Suggested fixes are only displayed: nothing here can apply or run them.
"""

from collections.abc import Callable

import streamlit as st

from ai_bug_detector.ai_models import (
    AiReviewError,
    AiUnavailableError,
    ExplanationReview,
    LogicReview,
)
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
    PAID_AI_PAUSE,
    PUBLIC_MODELS,
    SPEND_LIMIT_VARIABLE,
    SPENDING,
    ClaudeReviewer,
    DemoReviewer,
    actual_cost,
    estimate_max_cost,
    live_ai_enabled,
    read_demo_source,
    sdk_available,
    spend_limit,
)
from ai_bug_detector.deployment import (
    Mode,
    deployment_mode,
    server_api_key,
    server_key_configured,
)
from ai_bug_detector.fix_panel import code_for_display, show_fix_panel
from ai_bug_detector.fixes import FixOrigin, split_lines
from ai_bug_detector.ruff_analyzer import AnalysisError, analyze_bytes
from ai_bug_detector.shared_budget import (
    VISITOR_MESSAGES,
    Availability,
    BudgetRefused,
    public_budget,
    public_status,
)

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
PRIVACY_TITLE = ":material/shield: Privacy Notice"
PRIVACY_NOTICE = (
    "Your code and its Ruff results are sent to Anthropic's API only after you give "
    "your consent below. The file name is not sent. Anthropic may keep this data for "
    "up to 30 days and does not use it for training by default. Do not include "
    "passwords, API keys or other sensitive information."
)
PUBLIC_UNAVAILABLE = VISITOR_MESSAGES[Availability.UNAVAILABLE]
# Visitors are never told about the owner's account: the same message is used.
PUBLIC_PAUSED = PUBLIC_UNAVAILABLE
# Public mode: an indicative limit per Streamlit session, against accidental overuse.
# It is NOT a protection against abuse: anyone can open a new session. The real
# limits are global, in shared_budget.py.
SESSION_CALL_LIMIT = 3
SESSION_CALLS_KEY = "ai_session_calls"
SESSION_LIMIT_MESSAGE = (
    f"This session has used its {SESSION_CALL_LIMIT} Claude AI reviews. Ruff, the "
    "fixes and the demo still work."
)
_ACCOUNT_STOP_REASON = (
    "Anthropic refused the site's key or account (credits, spend limit or key). "
    "Check the Claude Console, then resume."
)


def show_ai_panel(uploaded_file, analysis: dict) -> None:
    """Display the optional AI review of an analyzed file."""
    if analysis["error"] or analysis.get("is_empty"):
        return

    st.divider()
    st.subheader("AI review (optional)")

    mode = deployment_mode()
    if mode is Mode.PUBLIC:
        # Always shown: when Claude cannot be used, its buttons are disabled and a
        # message says why.
        _show_live_review(uploaded_file, analysis["issues"], public=True)
    elif mode is Mode.MISCONFIGURED:
        st.caption(
            "AI review is off: this site's configuration is not valid. Ruff, the "
            "fixes and the demo still work."
        )
    elif not live_ai_enabled():
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
        _show_live_review(uploaded_file, analysis["issues"], public=False)


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
    explanations = reviewer.explain_issues(source, issues)
    logic = reviewer.find_logic_bugs(source)
    st.markdown("#### Explanations (illustrative)")
    _show_review(explanations, source)
    st.markdown("#### Possible logic bugs (illustrative)")
    _show_review(logic, source)

    show_fix_panel(
        context="demo",
        file_name="sample_code.py",
        source=source,
        issues=issues,
        reviews=[(explanations, FixOrigin.DEMO), (logic, FixOrigin.DEMO)],
    )


def claude_reviews(file_id: str) -> list[ExplanationReview | LogicReview]:
    """Return Claude's finished reviews of this upload, for the model now selected.

    These are the reviews shown in the AI section: their fixes are offered below it.
    """
    paid_ai_possible = deployment_mode() is Mode.PUBLIC or live_ai_enabled()
    if not paid_ai_possible or st.session_state.get(RESULTS_FILE_KEY) != file_id:
        return []
    results = st.session_state.get(RESULTS_KEY, {})
    model_id = st.session_state.get("ai_model", AI_MODELS[0].model_id)
    reviews = (
        results.get((model_id, action)) for action in (EXPLAIN_ACTION, LOGIC_ACTION)
    )
    return [r for r in reviews if isinstance(r, ExplanationReview | LogicReview)]


def _toggle_demo() -> None:
    """Runs before the page is redrawn, so the button shows the right label at once."""
    st.session_state[DEMO_OPEN_KEY] = not st.session_state.get(DEMO_OPEN_KEY, False)


def _show_live_review(uploaded_file, issues: list, *, public: bool) -> None:
    """Ask for consent, then make a real call when a button is clicked.

    Local mode: the user types their own key. Public mode: there is no key field at
    all; the site owner's key is read on the server at the moment of the call, and
    never stored anywhere a session can reach.
    """
    content = uploaded_file.getvalue()
    if len(content) > MAX_SOURCE_BYTES:
        st.warning(
            f"AI review is limited to {MAX_SOURCE_BYTES // 1024} KB of code for now, "
            "and this file is larger. Ruff results are not affected."
        )
        return
    source = content.decode("utf-8-sig")  # Ruff already accepted this file as UTF-8.
    selected_issues = select_issues(issues)

    if public:
        st.caption(
            "This site pays for AI reviews: you do not need an API key. Its budget "
            "is small and shared by all visitors, so reviews may become unavailable. "
            f"Each session can make up to {SESSION_CALL_LIMIT} reviews."
        )
        api_key, key_given = "", True
    else:
        api_key = st.text_input(
            "Anthropic API key",
            type="password",
            key="ai_api_key",
            autocomplete="off",
            help="Kept in memory for this session only. It is never saved to a file.",
        ).strip()
        key_given = bool(api_key)

    models = PUBLIC_MODELS if public else AI_MODELS
    model_ids = [model.model_id for model in models]
    model_id = st.selectbox(
        "Model",
        model_ids,
        format_func={model.model_id: model.label for model in models}.get,
        key="ai_model",
    )
    if model_id not in model_ids:
        model_id = model_ids[0]  # Never a model this mode does not offer.

    results = _results_for(uploaded_file.file_id)

    explain_cost = estimate_max_cost(
        build_explanation_message(source, selected_issues),
        EXPLANATION_SYSTEM_PROMPT,
        model_id,
    )
    logic_cost = estimate_max_cost(
        build_logic_bug_message(source), LOGIC_BUG_SYSTEM_PROMPT, model_id
    )
    if public:
        # Visitors do not pay: no amounts are shown. The costs above are still used
        # by every budget and limit check.
        _show_privacy_notice()
    else:
        st.warning(
            "**Before you continue:** the code of this file and its Ruff results will "
            "be sent to the Anthropic API. The file name is not sent. Anthropic may "
            "keep this data for up to 30 days, and does not use API data for training "
            "by default. Do not send passwords, keys or other secrets.\n\n"
            f"Estimated maximum cost on your API account: ${explain_cost:.3f} to "
            f"explain issues, ${logic_cost:.3f} to search for logic bugs. Only one "
            "request is made per click, and nothing is retried."
        )
    if len(issues) > MAX_ISSUES:
        st.caption(
            f"Only the {MAX_ISSUES} most severe of {len(issues)} issues will be "
            "explained."
        )

    if public:
        explain_refusal = _public_refusal(explain_cost)
        logic_refusal = _public_refusal(logic_cost)
        if explain_refusal or logic_refusal:
            st.info(explain_refusal or logic_refusal)
    else:
        explain_refusal, logic_refusal = _local_refusals(explain_cost, logic_cost)

    # The key includes the file and model: changing either one asks for consent again.
    consent = st.checkbox(
        CONSENT_LABEL, key=f"ai_consent_{uploaded_file.file_id}_{model_id}"
    )
    can_send = consent and key_given

    explain_column, logic_column = st.columns(2)
    explain_clicked = explain_column.button(
        "Explain Ruff issues",
        key="ai_explain",
        disabled=(
            not can_send
            or not selected_issues
            or explain_refusal is not None
            or _is_done(results, model_id, EXPLAIN_ACTION)
        ),
    )
    logic_clicked = logic_column.button(
        "Search for logic bugs",
        key="ai_logic",
        disabled=(
            not can_send
            or logic_refusal is not None
            or _is_done(results, model_id, LOGIC_ACTION)
        ),
    )

    # Checked again here: this is the last step before code leaves the computer.
    # The global limits are checked once more, atomically, by the reservation.
    blocked = explain_refusal if explain_clicked else logic_refusal
    if can_send and (explain_clicked or logic_clicked) and blocked is None:
        action = EXPLAIN_ACTION if explain_clicked else LOGIC_ACTION
        # Public mode: read on the server now, used for this call, then dropped.
        key = server_api_key() if public else api_key
        if not key:
            results[model_id, action] = PUBLIC_UNAVAILABLE
        else:
            reviewer = ClaudeReviewer(key, model_id)
            if explain_clicked:

                def review():
                    return reviewer.explain_issues(source, selected_issues)
            else:

                def review():
                    return reviewer.find_logic_bugs(source)

            if public:
                # Counted before the call: a click during the call starts a new run
                # of the page, which could stop this one before it counts.
                st.session_state[SESSION_CALLS_KEY] = _session_calls() + 1
            with st.spinner("Waiting for Claude..."):
                result, sent = _call(review, public)
            results[model_id, action] = result
            if public and not sent:
                # Refused before sending: this review does not count.
                st.session_state[SESSION_CALLS_KEY] = max(0, _session_calls() - 1)
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
            # The cost of each review is only shown to the owner of the key.
            _show_review(result, source, None if public else model_id)


def _show_privacy_notice() -> None:
    """A discreet notice, drawn with native components that follow the theme."""
    with st.container(border=True):
        st.markdown(f"**{PRIVACY_TITLE}**")
        st.caption(PRIVACY_NOTICE)


def _public_refusal(cost: float) -> str | None:
    """Why a review of this maximum cost cannot start on this site, or None."""
    if not server_key_configured() or not sdk_available() or PAID_AI_PAUSE.is_paused():
        return PUBLIC_UNAVAILABLE
    if _session_calls() >= SESSION_CALL_LIMIT:
        return SESSION_LIMIT_MESSAGE
    return public_status().message_for(cost)


def _session_calls() -> int:
    return st.session_state.get(SESSION_CALLS_KEY, 0)


def _local_refusals(explain_cost: float, logic_cost: float) -> tuple:
    """Local mode: the in-memory limit of this process, shown to its owner."""
    limit = spend_limit()
    remaining = max(0.0, limit - SPENDING.spent - SPENDING.reserved)
    st.caption(
        f"Spending limit of this app while it runs: ${limit:.2f}, of which "
        f"${SPENDING.spent:.3f} is already used. Set {SPEND_LIMIT_VARIABLE} to "
        "change it."
    )
    if min(explain_cost, logic_cost) > remaining:
        st.caption(
            "A review could go over the spending limit, so it cannot be started."
        )
    message = "Over the spending limit."
    return (
        message if explain_cost > remaining else None,
        message if logic_cost > remaining else None,
    )


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


def _call(
    review: Callable, public: bool
) -> tuple[ExplanationReview | LogicReview | str, bool]:
    """Return the review, or a message that is safe to show if it failed.

    Also returns False when nothing was sent, because a limit refused the call.
    """
    try:
        return review(), True
    except BudgetRefused as error:
        return str(error), False
    except AiUnavailableError as error:
        if not public:
            return str(error), True  # The user's own account: they can act on it.
        # The site owner's account: stop paid AI for everyone, on every server,
        # until the owner checks the account and resumes it. Visitors are not
        # given details about the owner's account.
        PAID_AI_PAUSE.pause()  # This server, even if the storage cannot be reached.
        budget = public_budget()
        if budget is not None:
            budget.stop(_ACCOUNT_STOP_REASON)
        return PUBLIC_PAUSED, True
    except AiReviewError as error:
        return str(error), True


def _show_review(
    review: ExplanationReview | LogicReview,
    source: str,
    model_id: str | None = None,
) -> None:
    """Display a review, whichever reviewer produced it."""
    lines = split_lines(source)  # Numbered like Python and Ruff number them.
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
        _show_fix(item, lines)


def _show_logic_bug(item, lines: list[str]) -> None:
    with st.container(border=True):
        st.caption(f"Line {item.line} · Confidence: {item.confidence.value}")
        st.text(item.title)
        st.text(item.reasoning)
        _show_fix(item, lines)


def _show_fix(item, lines: list[str]) -> None:
    """Show every line the suggestion replaces, then the suggestion itself."""
    if not item.suggested_fix.strip():
        return
    start, end = item.fix_start_line, item.fix_end_line
    if start is None and end is None:
        start = end = item.line  # An older answer: it replaces the reported line.
    if start is None or end is None or not 1 <= start <= end <= len(lines):
        st.caption(
            "The lines this suggestion replaces are missing or not valid: see "
            "Suggested fixes below."
        )
    else:
        label = f"line {start}" if start == end else f"lines {start}–{end}"
        st.caption(f"Current code ({label})")
        st.code(code_for_display("".join(lines[start - 1 : end])), language="python")
    st.caption("Suggested change: not applied, review it before using it")
    st.code(item.suggested_fix, language="python")
