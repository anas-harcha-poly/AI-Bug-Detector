"""Tests for the call limits of a public site: per hour, at the same time, per session.

The global limits live in Redis, simulated by fakeredis running the real Lua scripts
behind a fake Upstash REST API. Claude is never called: only its last step, the
request, is replaced by a fake answer.
"""

import json
import socket
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
import streamlit as st
from fake_upstash import TOKEN, URL, FakeUpstash
from streamlit import config
from streamlit.errors import StreamlitSecretNotFoundError
from streamlit.testing.v1 import AppTest

from ai_bug_detector import ai_panel, budget_admin, deployment, redis_rest
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
from ai_bug_detector.redis_rest import RedisRest, StoreError
from ai_bug_detector.shared_budget import (
    HOUR_SECONDS,
    KEY_PREFIX,
    RESERVATION_SECONDS,
    STATUS_CACHE,
    Availability,
    BudgetRefused,
    SharedBudget,
    visitor_message,
)

APP_PATH = Path(__file__).resolve().parents[1] / "app.py"
SERVER_KEY = "server-key-SECRET-made-up-for-tests"
CODE = b"import os\n\nx = 1\nif x == None:\n    pass\n"
NOON = 1_800_000_000 - (1_800_000_000 % 86400) + 12 * 3600  # Noon UTC, some day.
SONNET, HAIKU, OPUS = "claude-sonnet-5-5", "claude-haiku-5-5", "claude-opus-5-5"


class Clock:
    def __init__(self) -> None:
        self.now = float(NOON)

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def fake() -> FakeUpstash:
    return FakeUpstash()


@pytest.fixture
def clock() -> Clock:
    return Clock()


def server(fake: FakeUpstash, clock=None) -> SharedBudget:
    """One copy of the app: its own memory, the same database."""
    store = RedisRest(URL, TOKEN, transport=fake)
    return SharedBudget(store) if clock is None else SharedBudget(store, clock=clock)


@pytest.fixture
def budget(fake, clock) -> SharedBudget:
    budget = server(fake, clock)
    budget.start_period(4, 4)  # Plenty of money: only the call limits matter here.
    return budget


def refusal(budget: SharedBudget) -> BudgetRefused:
    with pytest.raises(BudgetRefused) as caught:
        budget.reserve(0.01)
    return caught.value


# --- Calls per hour, on the whole site ---


def test_the_eleventh_call_of_the_hour_is_refused(budget, clock):
    first_call = clock.now
    for _ in range(10):
        budget.settle(budget.reserve(0.01), 0.001, certain=True)
        clock.now += 60

    refused = refusal(budget)

    assert refused.availability is Availability.HOURLY_LIMIT
    assert refused.retry_at == first_call + HOUR_SECONDS
    assert budget.status().reserved == 0  # Nothing was reserved for it.


def test_the_hour_is_a_sliding_window(budget, clock):
    first_call = clock.now
    for _ in range(10):
        budget.settle(budget.reserve(0.01), 0.001, certain=True)
        clock.now += 60

    clock.now = first_call + HOUR_SECONDS  # The first call leaves the window.
    budget.reserve(0.01)

    assert refusal(budget).retry_at == first_call + 60 + HOUR_SECONDS


def test_the_message_says_when_to_try_again():
    message = visitor_message(
        Availability.HOURLY_LIMIT, retry_at=NOON + 12 * 60, now=NOON
    )

    assert "Try again after 12:12 UTC (in about 12 minutes)" in message
    assert "Ruff, the fixes and the demo still work." in message


def test_failed_and_refused_by_api_calls_still_count_in_the_hour(budget):
    for _ in range(10):
        budget.settle(budget.reserve(0.01), 0, certain=True)  # Not billed.

    assert refusal(budget).availability is Availability.HOURLY_LIMIT


# --- Calls at the same time ---


def test_a_third_simultaneous_call_is_refused_until_one_finishes(budget):
    first = budget.reserve(0.01)
    budget.reserve(0.01)

    refused = refusal(budget)
    assert refused.availability is Availability.BUSY
    assert refused.retry_at is None  # Not known: no time is promised.
    assert "Try again in a minute or two" in str(refused)

    budget.settle(first, 0.001, certain=True)
    budget.reserve(0.01)


def test_a_crashed_call_frees_its_place_after_a_while(budget, clock):
    budget.reserve(0.01)
    budget.reserve(0.01)  # Both servers crash: never settled.
    assert refusal(budget).availability is Availability.BUSY

    clock.now += RESERVATION_SECONDS + 1

    budget.reserve(0.01)
    assert budget.status().uncertain == 20_000  # Their maximum stays counted.


# --- Many visitors at once, on several servers ---


def run_at_once(count: int, attempt) -> tuple[list, list]:
    accepted, refused = [], []
    start = threading.Barrier(count)

    def visitor(number: int) -> None:
        start.wait()
        try:
            accepted.append(attempt(number))
        except BudgetRefused as error:
            refused.append(error.availability)

    threads = [threading.Thread(target=visitor, args=(n,)) for n in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return accepted, refused


def test_simultaneous_clicks_never_pass_the_concurrency_limit(fake, clock, budget):
    servers = [server(fake, clock) for _ in range(3)]

    accepted, refused = run_at_once(30, lambda n: servers[n % 3].reserve(0.01))

    assert len(accepted) == 2
    assert set(refused) == {Availability.BUSY}
    assert budget.status().in_flight == 2


def test_simultaneous_clicks_never_pass_the_hourly_limit(fake, clock, budget):
    budget.set_call_limits(concurrent=20)
    servers = [server(fake, clock) for _ in range(3)]

    accepted, refused = run_at_once(30, lambda n: servers[n % 3].reserve(0.01))

    assert len(accepted) == 10
    assert set(refused) == {Availability.HOURLY_LIMIT}


def test_simultaneous_reviews_through_the_real_reviewer(fake, budget, monkeypatch):
    """Answers take time, so calls really overlap: at most 2 are ever sent at once."""
    running, most, lock = [0], [0], threading.Lock()
    release = threading.Event()

    def slow_send(self, *args):
        with lock:
            running[0] += 1
            most[0] = max(most[0], running[0])
        release.wait(2)
        with lock:
            running[0] -= 1
        return answer()

    monkeypatch.setattr(ClaudeReviewer, "_send", slow_send)

    def review(number: int):
        if number == 0:
            threading.Timer(0.3, release.set).start()
        reviewer = ClaudeReviewer("k", SONNET, budget=server(fake))
        return reviewer.find_logic_bugs("x = 1\n")

    accepted, refused = run_at_once(12, review)

    assert most[0] <= 2
    assert len(accepted) == 2
    assert set(refused) == {Availability.BUSY}


# --- Restarts and storage problems ---


def test_the_limits_survive_a_restart(fake, clock, budget):
    for _ in range(10):
        budget.settle(budget.reserve(0.01), 0.001, certain=True)
    del budget  # Every server restarts: nothing is left in memory.

    assert refusal(server(fake, clock)).availability is Availability.HOURLY_LIMIT


def test_storage_down_blocks_new_calls(fake, budget):
    fake.down = True

    assert refusal(budget).availability is Availability.UNAVAILABLE
    assert budget.status().availability is Availability.UNAVAILABLE


def test_storage_errors_block_new_calls(fake, budget, monkeypatch):
    def broken(*args):
        return 400, b'{"error": "ERR unknown command"}'

    assert refusal(server(broken)).availability is Availability.UNAVAILABLE


def test_damaged_limits_block_new_calls(fake, budget):
    fake.redis.hset(f"{KEY_PREFIX}:state", "calls_per_hour", "ten")

    assert refusal(budget).availability is Availability.UNAVAILABLE


def test_reading_the_status_changes_nothing(fake, budget, clock):
    budget.reserve(0.01)
    clock.now += RESERVATION_SECONDS + HOUR_SECONDS  # Old data, still stored.
    before = {key: fake.redis.dump(key) for key in fake.redis.keys()}

    status = budget.status()

    assert (status.in_flight, status.calls_last_hour) == (0, 0)
    assert {key: fake.redis.dump(key) for key in fake.redis.keys()} == before


# --- Settings: the existing period is never touched ---


def test_an_existing_period_gets_the_default_limits(fake, clock, budget):
    assert b"calls_per_hour" not in fake.redis.hgetall(f"{KEY_PREFIX}:state")

    status = budget.status()

    assert (status.calls_per_hour, status.concurrent_calls) == (10, 2)


def test_changing_the_limits_keeps_the_budget_and_spending(fake, clock, budget):
    budget.settle(budget.reserve(0.3), 0.25, certain=True)
    before = budget.status()

    assert budget.set_call_limits(per_hour=20, concurrent=3)

    after = budget.status()
    assert (after.calls_per_hour, after.concurrent_calls) == (20, 3)
    for name in ("limit", "daily_limit", "spent", "period", "started_at", "calls"):
        assert getattr(after, name) == getattr(before, name)


@pytest.mark.parametrize(
    ("per_hour", "concurrent"), [(0, None), (None, 0), (5000, None), (None, None)]
)
def test_impossible_limits_are_refused(budget, per_hour, concurrent):
    with pytest.raises(ValueError):
        budget.set_call_limits(per_hour, concurrent)


def test_a_new_period_keeps_the_call_limits(budget):
    budget.set_call_limits(per_hour=5, concurrent=1)

    budget.start_period(4, 1)

    assert budget.status().calls_per_hour == 5


def test_admin_limits_command(fake, budget, capsys):
    code = budget_admin.main(
        ["limits", "--per-hour", "20", "--concurrent", "3"], budget=budget
    )

    assert code == 0
    out = capsys.readouterr().out
    assert "Calls in the last hour (whole site): 0 of 20" in out
    assert "Calls running now: 0 of 3" in out
    assert "Budget: $4.0000" in out
    assert budget_admin.main(["limits", "--per-hour", "0"], budget=budget) == 1


# --- The page ---


@pytest.fixture
def site(monkeypatch, fake) -> FakeUpstash:
    monkeypatch.setattr(redis_rest, "https_post", fake)
    monkeypatch.setenv(MODE_VARIABLE, "public")
    monkeypatch.setenv(STORE_URL_NAME, URL)
    monkeypatch.setenv(STORE_TOKEN_NAME, TOKEN)
    for name in (SERVER_KEY_NAME, LIVE_AI_VARIABLE):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(ai_panel, "sdk_available", lambda: True)
    fake.sent = []

    def fake_send(self, *args):
        fake.sent.append(self._model_id)
        return answer()

    monkeypatch.setattr(ClaudeReviewer, "_send", fake_send)
    STATUS_CACHE.clear()
    PAID_AI_PAUSE.reset()
    SPENDING.reset()
    server(fake).start_period(4, 1)
    yield fake
    STATUS_CACHE.clear()
    PAID_AI_PAUSE.reset()
    SPENDING.reset()


def answer():
    return SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text='{"bugs": [], "explanations": []}')],
        usage=SimpleNamespace(input_tokens=1000, output_tokens=500),
    )


def visitor() -> AppTest:
    """A new visitor: a new Streamlit session."""
    app = AppTest.from_file(str(APP_PATH), default_timeout=20)
    app.secrets[SERVER_KEY_NAME] = SERVER_KEY
    app.run()
    app.file_uploader[0].set_value(("code.py", CODE, "text/x-python")).run()
    app.button[0].click().run()
    agree(app)
    return app


def agree(app: AppTest) -> None:
    next(b for b in app.checkbox if b.label == ai_panel.CONSENT_LABEL).check().run()


def choose(app: AppTest, model_id: str) -> None:
    app.selectbox(key="ai_model").set_value(model_id).run()
    agree(app)  # Consent is asked again for each model.


def infos(app: AppTest) -> list[str]:
    return [item.value for item in app.info]


def claude_is_shown_but_off(app: AppTest) -> None:
    assert [s.value for s in app.subheader if "AI review" in s.value]
    assert app.button(key="ai_explain").disabled
    assert app.button(key="ai_logic").disabled
    assert not app.exception


def test_a_session_can_make_three_reviews(site):
    app = visitor()
    app.button(key="ai_explain").click().run()
    app.button(key="ai_logic").click().run()
    choose(app, HAIKU)
    app.button(key="ai_explain").click().run()

    assert site.sent == [SONNET, SONNET, HAIKU]
    claude_is_shown_but_off(app)
    assert ai_panel.SESSION_LIMIT_MESSAGE in infos(app)
    assert sorted(app.dataframe[0].value["Rule"]) == ["E711", "F401"]  # Ruff.


def test_the_session_limit_is_not_an_identity(site):
    first = visitor()
    first.button(key="ai_explain").click().run()
    first.button(key="ai_logic").click().run()
    choose(first, HAIKU)
    first.button(key="ai_explain").click().run()

    # The same person can simply open a new session: only the global limits,
    # kept in Redis, still apply. This is why the session limit is only indicative.
    second = visitor()
    assert not second.button(key="ai_logic").disabled


def test_a_call_refused_by_a_limit_does_not_use_the_session(site):
    budget = server(site)
    budget.set_call_limits(per_hour=1)
    app = visitor()
    remembered = STATUS_CACHE._value  # This server still sees the hour as free.
    budget.settle(budget.reserve(0.01), 0.001, certain=True)  # Another visitor.
    STATUS_CACHE._value = remembered

    app.button(key="ai_logic").click().run()

    assert site.sent == []
    assert app.session_state[ai_panel.SESSION_CALLS_KEY] == 0
    assert "Try again after" in app.error[0].value


def test_when_the_hourly_limit_is_reached_visitors_see_when_to_retry(site):
    server(site).set_call_limits(per_hour=2)
    visitor().button(key="ai_logic").click().run()
    visitor().button(key="ai_logic").click().run()

    third = visitor()

    claude_is_shown_but_off(third)
    message = infos(third)[0]
    assert "limit of AI reviews for the last hour" in message
    assert "Try again after" in message and "UTC" in message
    assert site.sent == [SONNET, SONNET]
    third.button(key="demo_toggle").click().run()
    assert "not real responses from Claude" in third.warning[-1].value


def test_when_two_calls_are_running_visitors_are_asked_to_wait(site):
    budget = server(site)
    budget.reserve(0.05)
    budget.reserve(0.05)  # Two reviews still waiting for Claude.

    app = visitor()

    claude_is_shown_but_off(app)
    assert ai_panel.SESSION_LIMIT_MESSAGE not in infos(app)
    assert "busy with other visitors' reviews" in infos(app)[0]


def test_redis_down_turns_claude_off_but_not_ruff(site):
    site.down = True

    app = visitor()

    claude_is_shown_but_off(app)
    assert "temporarily unavailable on this site" in infos(app)[0]
    assert sorted(app.dataframe[0].value["Rule"]) == ["E711", "F401"]
    assert len([b for b in app.checkbox if b.label == "Accept this fix"]) == 2


def test_local_mode_has_no_session_or_global_call_limits(site, monkeypatch):
    monkeypatch.delenv(MODE_VARIABLE)
    monkeypatch.setenv(LIVE_AI_VARIABLE, "1")
    site.requests.clear()
    app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
    app.file_uploader[0].set_value(("code.py", CODE, "text/x-python")).run()
    app.button[0].click().run()
    app.text_input(key="ai_api_key").set_value("my-own-key").run()
    agree(app)

    app.button(key="ai_explain").click().run()
    app.button(key="ai_logic").click().run()
    choose(app, OPUS)
    app.button(key="ai_explain").click().run()

    assert not app.button(key="ai_logic").disabled  # A 4th review is possible.
    app.button(key="ai_logic").click().run()
    assert site.sent == [SONNET, SONNET, OPUS, OPUS]
    assert site.requests == []  # Redis is never contacted in local mode.


# --- The protections of conftest.py ---


def test_tests_never_read_the_real_secrets_file():
    paths = config.get_option("secrets.files")

    assert all("no-secrets-" in path for path in paths)
    assert not Path(paths[0]).exists()
    with pytest.raises(StreamlitSecretNotFoundError):  # The real file is not read.
        st.secrets.get(SERVER_KEY_NAME)
    assert deployment._secret(SERVER_KEY_NAME) is None


def test_tests_cannot_reach_the_network():
    with pytest.raises(OSError):
        socket.create_connection(("192.0.2.1", 443), timeout=1)
    with pytest.raises(OSError):
        socket.getaddrinfo("upstash.io", 443)


def test_a_forgotten_fake_fails_instead_of_reaching_upstash():
    store = RedisRest("https://real-database.upstash.io", "token")

    with pytest.raises(StoreError, match="could not be reached"):
        store.command("PING")


def test_messages_never_contain_the_store_secrets(site):
    site.down = True
    app = visitor()

    shown = json.dumps([str(e.value) for e in (*app.info, *app.error, *app.caption)])
    assert TOKEN not in shown and URL not in shown
