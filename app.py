"""Streamlit entry point: this file only handles the user interface.

Streamlit runs this whole script again after every interaction, so anything that must
survive between two runs is kept in st.session_state.
"""

import streamlit as st

from ai_bug_detector import __version__
from ai_bug_detector.ai_panel import show_ai_panel, show_demo
from ai_bug_detector.models import Severity
from ai_bug_detector.report import count_by_severity, to_table_rows
from ai_bug_detector.ruff_analyzer import AnalysisError, analyze_bytes

MAX_UPLOAD_MB = 5
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
ANALYSIS_KEY = "analysis"


def analyze_upload(uploaded_file) -> dict:
    """Analyze an uploaded file, keeping it in memory: it is only read, never run."""
    analysis = {"file_id": uploaded_file.file_id, "issues": [], "error": None}

    # The browser also enforces this limit, but the server must not rely on it.
    if uploaded_file.size > MAX_UPLOAD_BYTES:
        analysis["error"] = f"The file is larger than {MAX_UPLOAD_MB} MB."
        return analysis

    content = uploaded_file.getvalue()
    analysis["is_empty"] = not content.strip()
    if analysis["is_empty"]:
        return analysis

    try:
        analysis["issues"] = analyze_bytes(content)
    except AnalysisError as error:
        analysis["error"] = str(error)
    return analysis


def show_analysis(analysis: dict) -> None:
    """Display the outcome of an analysis."""
    if analysis["error"]:
        st.error(f"The analysis could not be completed. {analysis['error']}")
    elif analysis.get("is_empty"):
        st.info("This file is empty, so there is nothing to analyze.")
    elif not analysis["issues"]:
        st.success("Ruff found no issues in this file.")
        st.caption("Static analysis cannot catch everything, such as logic errors.")
    else:
        show_issues(analysis["issues"])


def show_issues(issues: list) -> None:
    """Display the issue counters and the table of issues."""
    counts = count_by_severity(issues)
    total_column, error_column, warning_column, info_column = st.columns(4)
    total_column.metric("Total", len(issues), border=True)
    error_column.metric("Errors", counts[Severity.ERROR], border=True)
    warning_column.metric("Warnings", counts[Severity.WARNING], border=True)
    info_column.metric("Info", counts[Severity.INFO], border=True)

    st.dataframe(
        to_table_rows(issues),
        hide_index=True,
        width="stretch",
        column_config={
            "Severity": st.column_config.TextColumn(width="small"),
            "Line": st.column_config.NumberColumn(width="small", format="%d"),
            "Description": st.column_config.TextColumn(width="large"),
            "Rule": st.column_config.TextColumn(width="small"),
        },
    )


def main() -> None:
    st.set_page_config(
        page_title="AI Bug Detector",
        page_icon="🐞",
        layout="wide",
    )

    st.title("AI Bug Detector")
    st.caption(
        f"Find bugs in Python files with static analysis. "
        f"Your code is only read, never executed. Version {__version__}."
    )

    uploaded_file = st.file_uploader(
        "Python file",
        type="py",
        max_upload_size=MAX_UPLOAD_MB,
    )
    analyze_clicked = st.button(
        "Analyze",
        type="primary",
        disabled=uploaded_file is None,
    )

    # Each upload has its own id, even when two files share the same name. A result
    # from any other upload is out of date, so it must not stay on screen.
    analysis = st.session_state.get(ANALYSIS_KEY)
    if analysis and (
        uploaded_file is None or analysis["file_id"] != uploaded_file.file_id
    ):
        analysis = None

    if analyze_clicked and uploaded_file is not None:
        with st.spinner("Analyzing..."):
            analysis = analyze_upload(uploaded_file)

    st.session_state[ANALYSIS_KEY] = analysis

    if analysis:
        show_analysis(analysis)
        show_ai_panel(uploaded_file, analysis)

    show_demo(show_issues)


main()
