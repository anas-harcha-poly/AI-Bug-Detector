"""The AI section as visitors see it: a privacy notice, and no amounts in dollars.

Costs are still computed and checked in the background: the shared budget (a fake
Upstash database here) keeps recording them. Claude is never called.
"""

import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from fake_upstash import TOKEN, URL, FakeUpstash
from streamlit.testing.v1 import AppTest

from ai_bug_detector import ai_panel, redis_rest
from ai_bug_detector.ai_review import (
    LIVE_AI_VARIABLE,
    PAID_AI_PAUSE,
    SPENDING,
    ClaudeReviewer,
)
from ai_bug_detector.deployment import (
    MODE_VARIABLE,
    SERVER_KEY_NAME,
    STORE_TOKEN_NAME,
    STORE_URL_NAME,
)
from ai_bug_detector.redis_rest import RedisRest
from ai_bug_detector.shared_budget import STATUS_CACHE, SharedBudget

APP_PATH = Path(__file__).resolve().parents[1] / "app.py"
SERVER_KEY = "server-key-SECRET-made-up-for-tests"
CODE = b"import os\n\nx = 1\nif x == None:\n    pass\n"
DOLLARS = re.compile(r"\$\s?\d")


@pytest.fixture(autouse=True)
def site(monkeypatch) -> FakeUpstash:
    fake = FakeUpstash()
    monkeypatch.setattr(redis_rest, "https_post", fake)
    monkeypatch.setenv(STORE_URL_NAME, URL)
    monkeypatch.setenv(STORE_TOKEN_NAME, TOKEN)
    for name in (MODE_VARIABLE, SERVER_KEY_NAME, LIVE_AI_VARIABLE):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(ai_panel, "sdk_available", lambda: True)

    def fake_send(self, *args):
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text='{"bugs": []}')],
            usage=SimpleNamespace(input_tokens=1000, output_tokens=500),
        )

    monkeypatch.setattr(ClaudeReviewer, "_send", fake_send)
    SharedBudget(RedisRest(URL, TOKEN)).start_period(4, 0.5)
    STATUS_CACHE.clear()
    PAID_AI_PAUSE.reset()
    SPENDING.reset()
    yield fake
    STATUS_CACHE.clear()
    PAID_AI_PAUSE.reset()
    SPENDING.reset()


def open_page(monkeypatch, *, public: bool) -> AppTest:
    if public:
        monkeypatch.setenv(MODE_VARIABLE, "public")
    else:
        monkeypatch.setenv(LIVE_AI_VARIABLE, "1")
    app = AppTest.from_file(str(APP_PATH), default_timeout=20)
    if public:
        app.secrets[SERVER_KEY_NAME] = SERVER_KEY
    app.run()
    app.file_uploader[0].set_value(("code.py", CODE, "text/x-python")).run()
    app.button[0].click().run()
    if not public:
        app.text_input(key="ai_api_key").set_value("my-own-key").run()
    return app


def agree(app: AppTest) -> None:
    next(b for b in app.checkbox if b.label == ai_panel.CONSENT_LABEL).check().run()


def everything_shown(app: AppTest) -> str:
    """All the text the page sends to the browser."""
    parts = []

    def walk(node):
        proto = getattr(node, "proto", None)
        if proto is not None:
            parts.append(str(proto))
        for child in getattr(node, "children", {}).values():
            walk(child)

    walk(app._tree)
    return "\n".join(parts)


# --- Public mode ---


def test_visitors_see_a_privacy_notice_instead_of_the_warning(monkeypatch):
    app = open_page(monkeypatch, public=True)

    assert f"**{ai_panel.PRIVACY_TITLE}**" in [m.value for m in app.markdown]
    assert ai_panel.PRIVACY_NOTICE in [c.value for c in app.caption]
    assert not [w for w in app.warning if "Before you continue" in w.value]
    notice = ai_panel.PRIVACY_NOTICE
    assert "only after you give your consent" in notice
    assert "passwords, API keys or other sensitive information" in notice


def test_visitors_never_see_amounts_in_dollars(monkeypatch, site):
    app = open_page(monkeypatch, public=True)
    assert "E711" in everything_shown(app)  # The check below really reads the page.
    assert not DOLLARS.search(everything_shown(app))

    agree(app)
    app.button(key="ai_logic").click().run()

    page = everything_shown(app)
    assert "No possible logic bugs were reported." in page
    assert not DOLLARS.search(page)
    assert "output tokens" not in page


def test_costs_are_still_counted_in_the_background(monkeypatch, site):
    app = open_page(monkeypatch, public=True)
    agree(app)

    app.button(key="ai_logic").click().run()

    status = SharedBudget(RedisRest(URL, TOKEN)).status()
    assert status.spent == 7_000  # 1000 input + 500 output tokens on Sonnet.
    assert (status.calls, status.calls_last_hour, status.reserved) == (1, 1, 0)


def test_consent_is_still_required_in_public_mode(monkeypatch):
    app = open_page(monkeypatch, public=True)

    assert app.button(key="ai_logic").disabled
    agree(app)
    assert not app.button(key="ai_logic").disabled


def test_unavailable_messages_are_still_shown(monkeypatch, site):
    SharedBudget(RedisRest(URL, TOKEN)).start_period(0.01, 0.01)  # Too small.

    app = open_page(monkeypatch, public=True)

    assert "the AI budget of this demo has run out" in app.info[0].value
    assert app.button(key="ai_logic").disabled
    assert not DOLLARS.search(everything_shown(app))


# --- Local mode: the developer still sees every cost ---


def test_local_mode_keeps_the_warning_and_the_costs(monkeypatch):
    app = open_page(monkeypatch, public=False)

    warning = next(w.value for w in app.warning if "Before you continue" in w.value)
    assert re.search(r"Estimated maximum cost on your API account: \$\d", warning)
    assert any(c.value.startswith("Spending limit") for c in app.caption)
    assert ai_panel.PRIVACY_NOTICE not in [c.value for c in app.caption]


def test_local_mode_shows_the_cost_of_each_review(monkeypatch):
    app = open_page(monkeypatch, public=False)
    agree(app)

    app.button(key="ai_logic").click().run()

    assert any(
        "1000 input and 500 output tokens: about $0.0070" in c.value
        for c in app.caption
    )
