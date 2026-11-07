"""Streamlit entry point: this file only handles the user interface."""

import streamlit as st

from ai_bug_detector import __version__

st.set_page_config(page_title="AI Bug Detector")

st.title("AI Bug Detector")
st.caption(f"Version {__version__}")
st.info("Project foundation in place. Code analysis is not available yet.")
