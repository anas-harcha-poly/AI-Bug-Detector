"""Smoke tests: check that the project is installed and the page can run."""

from importlib.metadata import version
from pathlib import Path

from streamlit.testing.v1 import AppTest

import ai_bug_detector

APP_PATH = Path(__file__).resolve().parents[1] / "app.py"


def test_package_version_matches_installed_metadata():
    assert ai_bug_detector.__version__ == version("ai-bug-detector")


def test_app_runs_without_exception():
    app = AppTest.from_file(str(APP_PATH)).run()

    assert not app.exception
    assert app.title[0].value == "AI Bug Detector"
