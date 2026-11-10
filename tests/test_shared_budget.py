"""Tests for the shared AI budget of a public site, kept in Redis.

Redis is simulated by fakeredis, which runs the real Lua scripts, behind a fake
Upstash REST API (fake_upstash.py). No network, no real database, no Anthropic call.
"""

import json
import threading
import time
from types import SimpleNamespace

import pytest
from fake_upstash import TOKEN, URL, FakeUpstash

from ai_bug_detector import redis_rest
from ai_bug_detector.ai_models import AiReviewError, Usage
from ai_bug_detector.ai_prompts import LOGIC_BUG_SYSTEM_PROMPT, build_logic_bug_message
from ai_bug_detector.ai_review import (
    ClaudeReviewer,
    actual_cost,
    estimate_max_cost,
)
from ai_bug_detector.deployment import MODE_VARIABLE, STORE_TOKEN_NAME, STORE_URL_NAME
from ai_bug_detector.redis_rest import RedisRest, StoreError
from ai_bug_detector.shared_budget import (
    KEY_PREFIX,
    RESERVATION_SECONDS,
    Availability,
    BudgetRefused,
    SharedBudget,
    public_budget,
    to_micro,
)

DAY = 24 * 3600
NOON = 1_800_000_000.0 + 12 * 3600 - (1_800_000_000 % DAY)  # Noon UTC, some day.
SOURCE = "def f(x):\n    return x / 0\n"
SONNET = "claude-sonnet-5-5"


class Clock:
    def __init__(self) -> None:
        self.now = NOON

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def fake() -> FakeUpstash:
    return FakeUpstash()


@pytest.fixture
def clock() -> Clock:
    return Clock()


def app_copy(fake: FakeUpstash, clock: Clock) -> SharedBudget:
    """One copy of the app: its own objects in memory, the same database."""
    return SharedBudget(RedisRest(URL, TOKEN, transport=fake), clock=clock)


@pytest.fixture
def budget(fake, clock) -> SharedBudget:
    budget = app_copy(fake, clock)
    budget.start_period(4, 1)
    return budget


def stored(fake: FakeUpstash) -> dict:
    raw = fake.redis.hgetall(f"{KEY_PREFIX}:state")
    return {k.decode(): v.decode() for k, v in raw.items()}


# --- The REST client ---


def test_commands_are_sent_like_the_upstash_rest_api(fake):
    store = RedisRest(URL, TOKEN, transport=fake)

    assert store.command("SET", "greeting", "hello") == "OK"
    assert store.command("GET", "greeting") == "hello"
    assert fake.requests[0] == (URL, TOKEN, ["SET", "greeting", "hello"])


def test_the_real_transport_sends_the_token_as_a_bearer_header(monkeypatch):
    seen = {}

    class Answer:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, size):
            return b'{"result": "PONG"}'

    def fake_open(request, timeout):
        seen.update(request=request, timeout=timeout)
        return Answer()

    monkeypatch.setattr(redis_rest._OPENER, "open", fake_open)

    assert RedisRest(URL, TOKEN).command("PING") == "PONG"
    request = seen["request"]
    assert request.get_header("Authorization") == f"Bearer {TOKEN}"
    assert json.loads(request.data) == ["PING"]
    assert request.get_method() == "POST"
    assert seen["timeout"] == redis_rest.TIMEOUT_SECONDS


def test_redirects_are_never_followed():
    handlers = [type(h) for h in redis_rest._OPENER.handlers]
    assert redis_rest._NoRedirect in handlers
    assert redis_rest._NoRedirect().redirect_request() is None


@pytest.mark.parametrize(
    ("url", "token"),
    [
        ("http://fake-budget.example.test", TOKEN),  # Not encrypted.
        ("fake-budget.example.test", TOKEN),
        (URL, "  "),
    ],
)
def test_unsafe_or_incomplete_settings_are_refused(url, token):
    with pytest.raises(StoreError, match="not valid"):
        RedisRest(url, token)


@pytest.mark.parametrize(
    "transport",
    [
        lambda *a: (401, b'{"error": "Unauthorized"}'),
        lambda *a: (400, b'{"error": "ERR something"}'),
        lambda *a: (200, b"not json"),
        lambda *a: (200, b'{"other": 1}'),
        lambda *a: (_ for _ in ()).throw(OSError(f"cannot reach {URL}")),
    ],
)
def test_store_errors_never_show_the_url_or_token(transport):
    store = RedisRest(URL, TOKEN, transport=transport)

    with pytest.raises(StoreError) as caught:
        store.command("GET", "x")

    assert TOKEN not in str(caught.value) and URL not in str(caught.value)
    error = caught.value
    assert error.__cause__ is None
    assert error.__context__ is None or error.__suppress_context__
    assert TOKEN not in repr(store) and URL not in repr(store)


def test_amounts_are_rounded_up_to_a_millionth_of_a_dollar():
    assert to_micro(0.09) == 90_000
    assert to_micro(0.0000001) == 1
    assert to_micro(0) == 0


# --- Reserving and settling ---


def test_nothing_can_be_reserved_before_a_budget_is_set(fake, clock):
    budget = app_copy(fake, clock)

    with pytest.raises(BudgetRefused) as caught:
        budget.reserve(0.05)

    assert caught.value.availability is Availability.UNAVAILABLE
    assert budget.status().availability is Availability.UNAVAILABLE


def test_a_call_reserves_its_maximum_then_records_its_real_cost(budget):
    reservation = budget.reserve(0.09)
    during = budget.status()
    budget.settle(reservation, 0.0123, certain=True)
    after = budget.status()

    assert (during.reserved, during.today, during.in_flight) == (90_000, 90_000, 1)
    assert (after.spent, after.reserved, after.today) == (12_300, 0, 12_300)
    assert after.calls == 1


def test_a_call_whose_billing_is_unknown_stays_counted(budget):
    reservation = budget.reserve(0.09)

    budget.settle(reservation, 0.09, certain=False)

    status = budget.status()
    assert (status.spent, status.uncertain, status.reserved) == (0, 90_000, 0)


def test_settling_twice_counts_once(budget):
    reservation = budget.reserve(0.09)

    budget.settle(reservation, 0.01, certain=True)
    budget.settle(reservation, 0.01, certain=True)

    assert budget.status().spent == 10_000


def test_a_call_that_never_reports_back_is_counted_at_its_maximum(budget, clock):
    budget.reserve(0.09)  # The server crashes: no settle.

    clock.now += RESERVATION_SECONDS + 1
    budget.reserve(0.01)  # Any later reservation tidies up.

    status = budget.status()
    assert status.uncertain == 90_000
    assert status.reserved == 10_000


def test_a_late_report_after_expiry_changes_nothing(budget, clock):
    reservation = budget.reserve(0.09)
    clock.now += RESERVATION_SECONDS + 1
    budget.reserve(0.01)

    budget.settle(reservation, 0.001, certain=True)

    assert budget.status().uncertain == 90_000  # The safe side is kept.


# --- Limits ---


def test_the_budget_cannot_be_passed(fake, clock):
    budget = app_copy(fake, clock)
    budget.start_period(0.2, 0.2)
    budget.reserve(0.09)
    budget.reserve(0.09)

    with pytest.raises(BudgetRefused) as caught:
        budget.reserve(0.09)

    assert caught.value.availability is Availability.BUDGET_USED_UP
    assert "budget of this demo has run out" in str(caught.value)
    assert budget.status().reserved == 180_000


def test_the_global_budget_is_never_reset_by_a_new_day(fake, clock):
    budget = app_copy(fake, clock)
    budget.start_period(0.1, 0.1)
    budget.settle(budget.reserve(0.1), 0.1, certain=True)

    for _ in range(3):
        clock.now += DAY
        with pytest.raises(BudgetRefused) as caught:
            budget.reserve(0.01)
        assert caught.value.availability is Availability.BUDGET_USED_UP
    assert budget.status().availability is Availability.BUDGET_USED_UP


def test_the_daily_limit_starts_again_each_utc_day(budget, clock):
    for _ in range(10):
        budget.settle(budget.reserve(0.1), 0.1, certain=True)

    with pytest.raises(BudgetRefused) as caught:
        budget.reserve(0.05)
    assert caught.value.availability is Availability.DAILY_USED_UP
    assert "tomorrow" in str(caught.value)

    clock.now += DAY
    budget.reserve(0.05)  # A new day: allowed again, within the $4.
    assert budget.status().spent == 1_000_000


def test_a_status_shows_why_a_call_would_be_refused(budget):
    status = budget.status()

    assert status.refusal_for(0.5) is None
    assert status.refusal_for(1.5) is Availability.DAILY_USED_UP
    assert status.refusal_for(4.5) is Availability.BUDGET_USED_UP


# --- Many visitors, many copies of the app, restarts ---


def test_simultaneous_calls_never_go_over_the_budget(fake, clock):
    setup = app_copy(fake, clock)
    setup.start_period(1, 1)
    setup.set_call_limits(per_hour=1000, concurrent=20)  # Only money limits here.
    copies = [app_copy(fake, clock) for _ in range(3)]  # Three servers.
    accepted, refused = [], []
    start = threading.Barrier(40)

    def visitor(number: int) -> None:
        start.wait()
        try:
            accepted.append(copies[number % 3].reserve(0.09))
        except BudgetRefused:
            refused.append(number)

    threads = [threading.Thread(target=visitor, args=(n,)) for n in range(40)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(accepted) == 11  # $1 / $0.09
    assert len(refused) == 29
    assert setup.status().reserved == 11 * 90_000 <= 1_000_000


def test_simultaneous_reviews_never_go_over_the_budget(fake, clock, monkeypatch):
    """Through the real reviewer: calls overlap while their answers are awaited."""
    budget = app_copy(fake, clock)
    budget.start_period(0.5, 0.5)
    budget.set_call_limits(per_hour=1000, concurrent=20)  # Only money limits here.
    maximum = estimate_max_cost(
        build_logic_bug_message(SOURCE), LOGIC_BUG_SYSTEM_PROMPT, SONNET
    )
    sent, refused = [], []
    lock = threading.Lock()

    def slow_send(self, *args):
        with lock:
            sent.append(1)
        time.sleep(0.05)  # Other visitors click meanwhile.
        return answer()

    monkeypatch.setattr(ClaudeReviewer, "_send", slow_send)

    def visitor() -> None:
        try:
            ClaudeReviewer("k", SONNET, budget=app_copy(fake, clock)).find_logic_bugs(
                SOURCE
            )
        except BudgetRefused:
            refused.append(1)

    threads = [threading.Thread(target=visitor) for _ in range(25)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(sent) == int(0.5 // maximum)
    assert len(sent) + len(refused) == 25
    status = budget.status()
    assert status.reserved == 0
    assert status.spent == len(sent) * to_micro(actual_cost(Usage(1000, 500), SONNET))


def test_the_budget_survives_a_restart(fake, clock):
    before = app_copy(fake, clock)
    before.start_period(4, 1)
    before.settle(before.reserve(0.3), 0.25, certain=True)
    del before  # The app stops: everything in its memory is lost.

    after = app_copy(fake, clock)

    assert after.status().spent == 250_000
    assert after.status().period == 1


def test_a_new_period_only_starts_when_the_owner_asks(fake, clock):
    budget = app_copy(fake, clock)
    budget.start_period(0.1, 0.1)
    budget.settle(budget.reserve(0.1), 0.1, certain=True)
    clock.now += 30 * DAY
    assert budget.status().availability is Availability.BUDGET_USED_UP

    previous = budget.start_period(4, 1)

    assert previous["spent"] == "100000"
    status = budget.status()
    assert (status.availability, status.spent, status.limit, status.period) == (
        Availability.OPEN,
        0,
        4_000_000,
        2,
    )
    history = fake.redis.lrange(f"{KEY_PREFIX}:history", 0, -1)
    assert json.loads(history[0])["spent"] == "100000"


def test_calls_in_progress_stay_counted_in_a_new_period(budget):
    reservation = budget.reserve(0.09)

    budget.start_period(4, 1)
    assert budget.status().reserved == 90_000
    budget.settle(reservation, 0.02, certain=True)

    status = budget.status()
    assert (status.reserved, status.spent) == (0, 20_000)


@pytest.mark.parametrize(
    ("budget_usd", "daily_usd"), [(0, 0), (4, 5), (1000, 1), (4, 0)]
)
def test_impossible_limits_are_refused(fake, clock, budget_usd, daily_usd):
    with pytest.raises(ValueError):
        app_copy(fake, clock).start_period(budget_usd, daily_usd)
    assert stored(fake) == {}


# --- Storage problems: paid AI is blocked, never unlimited ---


def test_storage_down_blocks_calls(budget, fake):
    fake.down = True

    with pytest.raises(BudgetRefused) as caught:
        budget.reserve(0.01)

    assert caught.value.availability is Availability.UNAVAILABLE
    assert budget.status().availability is Availability.UNAVAILABLE


def test_storage_down_after_a_call_keeps_its_maximum_counted(budget, fake, clock):
    reservation = budget.reserve(0.09)
    fake.down = True
    budget.settle(reservation, 0.01, certain=True)  # Lost: never raises.
    fake.down = False

    clock.now += RESERVATION_SECONDS + 1
    budget.reserve(0.01)

    assert budget.status().uncertain == 90_000


def test_a_wrong_token_blocks_calls(fake, clock):
    budget = SharedBudget(RedisRest(URL, "wrong-token", transport=fake), clock=clock)

    with pytest.raises(BudgetRefused):
        budget.reserve(0.01)


def test_damaged_data_blocks_calls(budget, fake):
    fake.redis.hset(f"{KEY_PREFIX}:state", "spent", "a lot")

    with pytest.raises(BudgetRefused) as caught:
        budget.reserve(0.01)

    assert caught.value.availability is Availability.UNAVAILABLE


def test_a_review_is_never_sent_when_the_budget_refuses(fake, clock, monkeypatch):
    fake.down = True

    def must_not_send(*args):
        raise AssertionError("Nothing may be sent.")

    monkeypatch.setattr(ClaudeReviewer, "_send", must_not_send)

    with pytest.raises(BudgetRefused):
        ClaudeReviewer("k", SONNET, budget=app_copy(fake, clock)).find_logic_bugs(
            SOURCE
        )


def test_public_mode_without_storage_settings_blocks_paid_ai(monkeypatch):
    monkeypatch.setenv(MODE_VARIABLE, "public")
    monkeypatch.delenv(STORE_URL_NAME, raising=False)
    monkeypatch.delenv(STORE_TOKEN_NAME, raising=False)

    def must_not_send(*args):
        raise AssertionError("Nothing may be sent.")

    monkeypatch.setattr(ClaudeReviewer, "_send", must_not_send)

    assert public_budget() is None
    with pytest.raises(BudgetRefused):
        ClaudeReviewer("k", SONNET).find_logic_bugs(SOURCE)


def test_storage_settings_are_ignored_in_local_mode(monkeypatch):
    monkeypatch.delenv(MODE_VARIABLE, raising=False)
    monkeypatch.setenv(STORE_URL_NAME, URL)
    monkeypatch.setenv(STORE_TOKEN_NAME, TOKEN)

    assert public_budget() is None


# --- What a call costs, depending on how it ends ---


def answer(usage=True):
    return SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text='{"bugs": []}')],
        usage=SimpleNamespace(input_tokens=1000, output_tokens=500) if usage else None,
    )


def failure(maybe_billed: bool) -> AiReviewError:
    error = AiReviewError("It failed. Nothing was retried.")
    error.maybe_billed = maybe_billed
    return error


@pytest.mark.parametrize(
    ("outcome", "spent", "uncertain"),
    [
        ("answer", "real", 0),
        ("answer without usage", 0, "maximum"),
        ("refused by the API", 0, 0),
        ("maybe billed", 0, "maximum"),
    ],
)
def test_each_outcome_is_recorded_on_the_safe_side(
    budget, monkeypatch, outcome, spent, uncertain
):
    def send(self, *args):
        if outcome == "answer":
            return answer()
        if outcome == "answer without usage":
            return answer(usage=False)
        raise failure(maybe_billed=outcome == "maybe billed")

    monkeypatch.setattr(ClaudeReviewer, "_send", send)
    maximum = to_micro(
        estimate_max_cost(
            build_logic_bug_message(SOURCE), LOGIC_BUG_SYSTEM_PROMPT, SONNET
        )
    )
    real = to_micro(actual_cost(Usage(1000, 500), SONNET))

    try:
        ClaudeReviewer("k", SONNET, budget=budget).find_logic_bugs(SOURCE)
    except AiReviewError:
        pass

    expected = {"real": real, "maximum": maximum, 0: 0}
    status = budget.status()
    assert (status.spent, status.uncertain) == (expected[spent], expected[uncertain])
    assert status.reserved == 0


@pytest.mark.parametrize(
    ("status_code", "error_type", "maybe_billed"),
    [
        (400, "invalid_request_error", False),
        (401, "authentication_error", False),
        (429, "rate_limit_error", False),
        (529, "overloaded_error", False),
        (500, "api_error", True),
    ],
)
def test_the_real_sdk_errors_are_classified(
    monkeypatch, status_code, error_type, maybe_billed
):
    anthropic = pytest.importorskip("anthropic")
    httpx2 = pytest.importorskip("httpx2")

    def handle(request):
        body = {"type": "error", "error": {"type": error_type, "message": "no"}}
        return httpx2.Response(status_code, json=body)

    _route_sdk(monkeypatch, anthropic, httpx2, handle)

    with pytest.raises(AiReviewError) as caught:
        ClaudeReviewer("k", SONNET)._send("s", "m", {"type": "object"}, "low")

    assert caught.value.maybe_billed is maybe_billed


def test_a_timeout_may_have_been_billed(monkeypatch):
    anthropic = pytest.importorskip("anthropic")
    httpx2 = pytest.importorskip("httpx2")

    def handle(request):
        raise httpx2.ReadTimeout("too slow", request=request)

    _route_sdk(monkeypatch, anthropic, httpx2, handle)

    with pytest.raises(AiReviewError) as caught:
        ClaudeReviewer("k", SONNET)._send("s", "m", {"type": "object"}, "low")

    assert caught.value.maybe_billed


def _route_sdk(monkeypatch, anthropic, httpx2, handle) -> None:
    real_client = anthropic.Anthropic

    def on_fake_network(**kwargs):
        transport = httpx2.MockTransport(handle)
        return real_client(
            **kwargs, http_client=anthropic.DefaultHttpxClient(transport=transport)
        )

    monkeypatch.setattr(anthropic, "Anthropic", on_fake_network)
