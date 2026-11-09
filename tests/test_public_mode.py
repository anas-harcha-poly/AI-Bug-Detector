"""Tests for the public mode: the site owner's key, read on the server only.

No real API call is made: Claude is replaced by a fake reviewer, or the real SDK runs
on a fake local network. The key used here is a made-up value. The shared budget is
kept in a fake Upstash database (see fake_upstash.py), with $4 to spend.
"""

import subprocess
import time
from pathlib import Path

import pytest
from fake_upstash import TOKEN, URL, FakeUpstash
from streamlit.testing.v1 import AppTest

from ai_bug_detector import ai_panel, deployment, redis_rest
from ai_bug_detector.ai_models import (
    AiReviewError,
    AiUnavailableError,
    Confidence,
    ExplanationReview,
    LogicBug,
    LogicReview,
)
from ai_bug_detector.ai_review import (
    LIVE_AI_VARIABLE,
    PAID_AI_PAUSE,
    SPEND_LIMIT_VARIABLE,
    SPENDING,
    ClaudeReviewer,
    PaidAiPause,
    live_ai_enabled,
)
from ai_bug_detector.deployment import (
    MODE_VARIABLE,
    SERVER_KEY_NAME,
    STORE_TOKEN_NAME,
    STORE_URL_NAME,
    Mode,
    deployment_mode,
    is_public,
    server_api_key,
)
from ai_bug_detector.docker_sandbox import (
    SANDBOX_VARIABLE,
    SandboxError,
    SandboxStatus,
    run_tests,
    sandbox_enabled,
    sandbox_status,
)
from ai_bug_detector.shared_budget import STATUS_CACHE, SharedBudget

APP_PATH = Path(__file__).resolve().parents[1] / "app.py"
SERVER_KEY = "server-key-SECRET-made-up-for-tests"
CODE = b"import os\n\nx = 1\nif x == None:\n    pass\n"
SONNET, HAIKU, OPUS = "claude-sonnet-5-5", "claude-haiku-5-5", "claude-opus-5-5"


class FakeReviewer:
    """Stands in for ClaudeReviewer and records which key and model it was given."""

    created: list = []
    error: Exception | None = None

    def __init__(self, api_key, model_id):
        FakeReviewer.created.append((api_key, model_id))

    def explain_issues(self, source, issues):
        return self._answer(ExplanationReview([], 0))

    def find_logic_bugs(self, source):
        fix = LogicBug(4, "Comparison", "r", Confidence.LOW, "if x is None:", 4, 4)
        return self._answer(LogicReview([fix], 0))

    def _answer(self, review):
        if FakeReviewer.error is not None:
            raise FakeReviewer.error
        return review


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for name in (
        MODE_VARIABLE,
        SERVER_KEY_NAME,
        LIVE_AI_VARIABLE,
        SANDBOX_VARIABLE,
        SPEND_LIMIT_VARIABLE,
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(ai_panel, "ClaudeReviewer", FakeReviewer)
    monkeypatch.setattr(ai_panel, "sdk_available", lambda: True)
    FakeReviewer.created, FakeReviewer.error = [], None
    SPENDING.reset()
    PAID_AI_PAUSE.reset()
    STATUS_CACHE.clear()
    yield
    SPENDING.reset()
    PAID_AI_PAUSE.reset()
    STATUS_CACHE.clear()


@pytest.fixture(autouse=True)
def store(monkeypatch) -> FakeUpstash:
    """A shared budget of $4, $1 a day, in a fake database."""
    fake = FakeUpstash()
    monkeypatch.setattr(redis_rest, "https_post", fake)
    monkeypatch.setenv(STORE_URL_NAME, URL)
    monkeypatch.setenv(STORE_TOKEN_NAME, TOKEN)
    SharedBudget(redis_rest.RedisRest(URL, TOKEN)).start_period(4, 1)
    return fake


def make_app(secrets: dict | None = None) -> AppTest:
    app = AppTest.from_file(str(APP_PATH), default_timeout=20)
    for name, value in (secrets or {}).items():
        app.secrets[name] = value
    return app.run()


@pytest.fixture
def public_app(monkeypatch) -> AppTest:
    monkeypatch.setenv(MODE_VARIABLE, "public")
    return make_app({SERVER_KEY_NAME: SERVER_KEY})


def analyze(app: AppTest, content: bytes = CODE) -> None:
    app.file_uploader[0].set_value(("code.py", content, "text/x-python")).run()
    app.button[0].click().run()


def consent(app: AppTest) -> None:
    next(b for b in app.checkbox if b.label == ai_panel.CONSENT_LABEL).check().run()


def rendered(app: AppTest) -> str:
    """Everything the page sends to the browser, as text."""
    parts = []

    def walk(node):
        proto = getattr(node, "proto", None)
        if proto is not None:
            parts.append(str(proto))
        for child in getattr(node, "children", {}).values():
            walk(child)

    walk(app._tree)
    return "\n".join(parts)


def stored(app: AppTest) -> str:
    """Everything kept in the session, as text."""
    state = app.session_state
    return "\n".join(f"{key}={state[key]!r}" for key in state)


def texts(app: AppTest) -> str:
    groups = (app.caption, app.error, app.warning, app.info, app.markdown, app.text)
    return "\n".join(str(item.value) for group in groups for item in group)


def _has_button(app: AppTest, key: str) -> bool:
    return any(button.key == key for button in app.button)


# --- Choosing the mode ---


@pytest.mark.parametrize(
    ("value", "mode"),
    [
        (None, Mode.LOCAL),
        ("", Mode.LOCAL),
        ("local", Mode.LOCAL),
        ("LOCAL", Mode.LOCAL),
        ("public", Mode.PUBLIC),
        (" Public ", Mode.PUBLIC),
        ("prod", Mode.MISCONFIGURED),
        ("publik", Mode.MISCONFIGURED),
    ],
)
def test_mode_comes_from_one_explicit_setting(monkeypatch, value, mode):
    if value is not None:
        monkeypatch.setenv(MODE_VARIABLE, value)

    assert deployment_mode() is mode
    assert is_public() is (mode is not Mode.LOCAL)


def test_server_key_is_never_read_in_local_mode(monkeypatch):
    monkeypatch.setenv(SERVER_KEY_NAME, SERVER_KEY)

    assert server_api_key() is None


def test_server_key_is_read_from_the_environment_in_public_mode(monkeypatch):
    monkeypatch.setenv(MODE_VARIABLE, "public")
    monkeypatch.setenv(SERVER_KEY_NAME, f"  {SERVER_KEY}  ")

    assert server_api_key() == SERVER_KEY


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_server_key_counts_as_missing(monkeypatch, value):
    monkeypatch.setenv(MODE_VARIABLE, "public")
    monkeypatch.setenv(SERVER_KEY_NAME, value)

    assert server_api_key() is None


def test_misconfigured_mode_never_uses_a_key(monkeypatch):
    monkeypatch.setenv(MODE_VARIABLE, "prod")
    monkeypatch.setenv(SERVER_KEY_NAME, SERVER_KEY)

    assert server_api_key() is None


def test_missing_secrets_file_does_not_break_anything(monkeypatch):
    # Outside Streamlit, or without .streamlit/secrets.toml, secrets are just absent.
    assert deployment._secret(SERVER_KEY_NAME) is None


# --- The public page ---


def test_visitors_are_never_asked_for_a_key(public_app):
    analyze(public_app)

    assert not public_app.text_input
    assert "you do not need an API key" in texts(public_app)


def test_mode_and_key_can_come_from_streamlit_secrets():
    app = make_app({MODE_VARIABLE: "public", SERVER_KEY_NAME: SERVER_KEY})

    analyze(app)

    assert not app.text_input
    assert app.button(key="ai_logic")


def test_consent_is_still_required_before_any_call(public_app):
    analyze(public_app)

    assert public_app.button(key="ai_logic").disabled
    consent(public_app)
    assert not public_app.button(key="ai_logic").disabled
    assert FakeReviewer.created == []


def test_the_server_key_and_sonnet_are_used_on_click(public_app):
    analyze(public_app)
    consent(public_app)

    public_app.button(key="ai_logic").click().run()

    assert FakeReviewer.created == [(SERVER_KEY, SONNET)]


def test_the_key_never_reaches_the_page_or_the_session(public_app):
    analyze(public_app)
    consent(public_app)
    public_app.button(key="ai_logic").click().run()
    public_app.button(key="demo_toggle").click().run()

    page, session = rendered(public_app), stored(public_app)
    # The checks really look at the page and the session, not at empty text.
    assert "E711" in page and "if x is None:" in page
    assert "ai_model" in session
    assert SERVER_KEY not in page
    assert SERVER_KEY not in session
    assert "SECRET" not in page + session


def test_visitors_cannot_choose_opus(public_app):
    analyze(public_app)

    options = public_app.selectbox(key="ai_model").options
    assert len(options) == 2
    assert "Sonnet" in options[0]
    assert not any("Opus" in option for option in options)


def test_haiku_can_be_chosen(public_app):
    analyze(public_app)
    public_app.selectbox(key="ai_model").set_value(HAIKU).run()
    consent(public_app)

    public_app.button(key="ai_logic").click().run()

    assert FakeReviewer.created == [(SERVER_KEY, HAIKU)]


def test_server_refuses_opus_even_if_asked_directly(monkeypatch):
    monkeypatch.setenv(MODE_VARIABLE, "public")
    reviewer = ClaudeReviewer(SERVER_KEY, OPUS)

    def must_not_send(*args):
        raise AssertionError("Nothing may be sent.")

    monkeypatch.setattr(reviewer, "_send", must_not_send)

    with pytest.raises(AiReviewError, match="not available on this site"):
        reviewer.find_logic_bugs("x = 1\n")


def test_claude_fixes_are_offered_in_public_mode(public_app):
    analyze(public_app)
    consent(public_app)

    public_app.button(key="ai_logic").click().run()

    headers = [c.value for c in public_app.caption if c.value.startswith("Fix #")]
    assert any("Claude · line 4" in header for header in headers)


def test_the_local_live_setting_changes_nothing_in_public_mode(monkeypatch):
    monkeypatch.setenv(MODE_VARIABLE, "public")
    monkeypatch.setenv(LIVE_AI_VARIABLE, "1")
    app = make_app({SERVER_KEY_NAME: SERVER_KEY})

    analyze(app)

    assert not live_ai_enabled()
    assert not app.text_input


# --- Without a key, or without credit: free features keep working ---


def free_features_work(app: AppTest) -> None:
    assert sorted(app.dataframe[0].value["Rule"]) == ["E711", "F401"]
    assert len([b for b in app.checkbox if b.label == "Accept this fix"]) == 2
    app.button(key="demo_toggle").click().run()
    assert "not real responses from Claude" in app.warning[-1].value
    assert not app.exception


def test_without_a_server_key_paid_ai_is_off_and_the_rest_works(monkeypatch):
    monkeypatch.setenv(MODE_VARIABLE, "public")
    app = make_app()

    analyze(app)
    consent(app)

    assert ai_panel.PUBLIC_UNAVAILABLE in texts(app)
    assert not app.text_input
    assert app.button(key="ai_logic").disabled  # Still shown, but disabled.
    free_features_work(app)


def test_without_the_sdk_paid_ai_is_off_and_the_rest_works(monkeypatch):
    monkeypatch.setenv(MODE_VARIABLE, "public")
    monkeypatch.setattr(ai_panel, "sdk_available", lambda: False)
    app = make_app({SERVER_KEY_NAME: SERVER_KEY})

    analyze(app)

    consent(app)

    assert ai_panel.PUBLIC_UNAVAILABLE in texts(app)
    assert app.button(key="ai_logic").disabled
    free_features_work(app)


def test_an_account_problem_pauses_paid_ai_for_everyone(public_app):
    FakeReviewer.error = AiUnavailableError(
        "The API key was rejected. Check it and try again. Nothing was retried."
    )
    analyze(public_app)
    consent(public_app)

    public_app.button(key="ai_logic").click().run()

    # Visitors see a neutral message, never details about the owner's account.
    assert ai_panel.PUBLIC_PAUSED in public_app.error[0].value
    assert "key was rejected" not in texts(public_app)
    assert PAID_AI_PAUSE.is_paused()

    other_visitor = make_app({SERVER_KEY_NAME: SERVER_KEY})
    analyze(other_visitor)
    consent(other_visitor)
    assert other_visitor.button(key="ai_logic").disabled
    assert ai_panel.PUBLIC_PAUSED in texts(other_visitor)
    free_features_work(other_visitor)


def test_a_passing_problem_does_not_pause_paid_ai(public_app):
    FakeReviewer.error = AiReviewError("Claude is overloaded right now. Try later.")
    analyze(public_app)
    consent(public_app)

    public_app.button(key="ai_logic").click().run()

    assert "overloaded" in public_app.error[0].value
    assert not PAID_AI_PAUSE.is_paused()


def test_the_pause_ends_by_itself():
    pause = PaidAiPause()

    pause.pause(seconds=0.05)
    assert pause.is_paused()
    time.sleep(0.1)

    assert not pause.is_paused()


# --- Docker test runs are never possible on a public site ---


@pytest.mark.parametrize("mode", ["public", "prod"])
def test_docker_runs_stay_off_whatever_the_setting(monkeypatch, mode):
    monkeypatch.setenv(MODE_VARIABLE, mode)
    monkeypatch.setenv(SANDBOX_VARIABLE, "1")

    def no_docker(*args, **kwargs):
        raise AssertionError("Docker must not be called on a public site.")

    monkeypatch.setattr(subprocess, "run", no_docker)
    monkeypatch.setattr(subprocess, "Popen", no_docker)

    assert not sandbox_enabled()
    assert sandbox_status() is SandboxStatus.DISABLED
    with pytest.raises(SandboxError, match="turned off"):
        run_tests("calc.py", b"x = 1\n", "test_calc.py", b"def test_a():\n    pass\n")


def test_public_page_explains_that_tests_cannot_run(monkeypatch):
    monkeypatch.setenv(MODE_VARIABLE, "public")
    monkeypatch.setenv(SANDBOX_VARIABLE, "1")
    app = make_app({SERVER_KEY_NAME: SERVER_KEY})

    analyze(app)

    assert "not available on this public site" in texts(app)
    assert len(app.file_uploader) == 1  # No test file uploader.


# --- A setting that is not valid is the most restrictive ---


def test_misconfigured_site_offers_no_paid_ai_and_no_key_field(monkeypatch):
    monkeypatch.setenv(MODE_VARIABLE, "prod")
    monkeypatch.setenv(LIVE_AI_VARIABLE, "1")
    app = make_app({SERVER_KEY_NAME: SERVER_KEY})

    analyze(app)

    assert "configuration is not valid" in texts(app)
    assert not app.text_input
    assert not _has_button(app, "ai_logic")
    free_features_work(app)


# --- Local mode is unchanged ---


def test_local_mode_still_uses_the_typed_key_never_the_server_one(monkeypatch):
    monkeypatch.setenv(LIVE_AI_VARIABLE, "1")
    monkeypatch.setenv(SERVER_KEY_NAME, SERVER_KEY)
    app = make_app({SERVER_KEY_NAME: SERVER_KEY})
    analyze(app)

    app.text_input(key="ai_api_key").set_value("my-own-key").run()
    consent(app)
    app.button(key="ai_logic").click().run()

    assert FakeReviewer.created == [("my-own-key", SONNET)]


def test_local_mode_still_offers_opus(monkeypatch):
    monkeypatch.setenv(LIVE_AI_VARIABLE, "1")
    app = make_app()

    analyze(app)

    assert any("Opus" in option for option in app.selectbox(key="ai_model").options)


# --- With the real SDK on a fake network ---


def test_real_sdk_sends_the_server_key_and_never_shows_it(public_app, monkeypatch):
    anthropic = pytest.importorskip("anthropic")
    httpx2 = pytest.importorskip("httpx2")
    requests = []

    def handle(request):
        requests.append(request)
        message = f"Invalid key {SERVER_KEY}"  # An error that repeats the key.
        return httpx2.Response(
            401,
            json={
                "type": "error",
                "error": {"type": "authentication_error", "message": message},
            },
        )

    real_client = anthropic.Anthropic

    def on_fake_network(**kwargs):
        transport = httpx2.MockTransport(handle)
        return real_client(
            **kwargs, http_client=anthropic.DefaultHttpxClient(transport=transport)
        )

    monkeypatch.setattr(anthropic, "Anthropic", on_fake_network)
    monkeypatch.setattr(ai_panel, "ClaudeReviewer", ClaudeReviewer)
    analyze(public_app)
    consent(public_app)

    public_app.button(key="ai_logic").click().run()

    assert requests[0].headers["authorization"] == f"Bearer {SERVER_KEY}"
    assert ai_panel.PUBLIC_PAUSED in public_app.error[0].value
    assert SERVER_KEY not in rendered(public_app)
    assert SERVER_KEY not in stored(public_app)
    assert PAID_AI_PAUSE.is_paused()


def test_an_unknown_api_error_never_shows_the_key(public_app, monkeypatch):
    anthropic = pytest.importorskip("anthropic")
    httpx2 = pytest.importorskip("httpx2")

    def handle(request):
        message = f"Something odd happened for {SERVER_KEY}"
        return httpx2.Response(
            418,
            json={"type": "error", "error": {"type": "odd_error", "message": message}},
        )

    real_client = anthropic.Anthropic

    def on_fake_network(**kwargs):
        transport = httpx2.MockTransport(handle)
        return real_client(
            **kwargs, http_client=anthropic.DefaultHttpxClient(transport=transport)
        )

    monkeypatch.setattr(anthropic, "Anthropic", on_fake_network)
    monkeypatch.setattr(ai_panel, "ClaudeReviewer", ClaudeReviewer)
    analyze(public_app)
    consent(public_app)

    public_app.button(key="ai_logic").click().run()

    assert "Something odd happened for [hidden]" in public_app.error[0].value
    assert SERVER_KEY not in rendered(public_app)
    assert not PAID_AI_PAUSE.is_paused()  # Not an account problem.
