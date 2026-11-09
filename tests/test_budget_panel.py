"""The shared budget as visitors and the owner see it: the page, and budget_admin.

The real ClaudeReviewer is used, so each click goes through the reservation in the
fake database. Only the request to Anthropic is replaced: nothing is sent anywhere.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest
from fake_upstash import TOKEN, URL, FakeUpstash
from streamlit.testing.v1 import AppTest

from ai_bug_detector import ai_panel, budget_admin, redis_rest
from ai_bug_detector.ai_models import AiUnavailableError
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
from ai_bug_detector.shared_budget import (
    STATUS_CACHE,
    VISITOR_MESSAGES,
    Availability,
    SharedBudget,
)

APP_PATH = Path(__file__).resolve().parents[1] / "app.py"
SERVER_KEY = "server-key-SECRET-made-up-for-tests"
CODE = b"import os\n\nx = 1\nif x == None:\n    pass\n"
USED_UP = VISITOR_MESSAGES[Availability.BUDGET_USED_UP]
DAILY_USED_UP = VISITOR_MESSAGES[Availability.DAILY_USED_UP]
UNAVAILABLE = VISITOR_MESSAGES[Availability.UNAVAILABLE]


@pytest.fixture(autouse=True)
def site(monkeypatch) -> FakeUpstash:
    """A public site with a fake database, and Claude replaced by a fake answer."""
    fake = FakeUpstash()
    monkeypatch.setattr(redis_rest, "https_post", fake)
    monkeypatch.setenv(MODE_VARIABLE, "public")
    monkeypatch.setenv(STORE_URL_NAME, URL)
    monkeypatch.setenv(STORE_TOKEN_NAME, TOKEN)
    for name in (SERVER_KEY_NAME, LIVE_AI_VARIABLE):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(ai_panel, "sdk_available", lambda: True)
    fake.sent = []

    def fake_send(self, *args):
        fake.sent.append(self._api_key)
        if getattr(fake, "anthropic_error", None):
            raise fake.anthropic_error
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text='{"bugs": []}')],
            usage=SimpleNamespace(input_tokens=1000, output_tokens=500),
        )

    monkeypatch.setattr(ClaudeReviewer, "_send", fake_send)
    STATUS_CACHE.clear()
    PAID_AI_PAUSE.reset()
    SPENDING.reset()
    yield fake
    STATUS_CACHE.clear()
    PAID_AI_PAUSE.reset()
    SPENDING.reset()


def owner(fake: FakeUpstash) -> SharedBudget:
    """The budget as the owner's admin command sees it."""
    return SharedBudget(RedisRest(URL, TOKEN, transport=fake))


def page() -> AppTest:
    app = AppTest.from_file(str(APP_PATH), default_timeout=20)
    app.secrets[SERVER_KEY_NAME] = SERVER_KEY
    app.run()
    app.file_uploader[0].set_value(("code.py", CODE, "text/x-python")).run()
    app.button[0].click().run()
    next(b for b in app.checkbox if b.label == ai_panel.CONSENT_LABEL).check().run()
    return app


def texts(app: AppTest) -> str:
    groups = (app.caption, app.error, app.warning, app.info, app.markdown, app.text)
    return "\n".join(str(item.value) for group in groups for item in group)


def claude_is_shown_but_off(app: AppTest, message: str) -> None:
    assert [s.value for s in app.subheader if "AI review" in s.value]
    assert app.selectbox(key="ai_model")
    assert app.button(key="ai_logic").disabled
    assert app.button(key="ai_explain").disabled
    assert message in [i.value for i in app.info]


def free_features_work(app: AppTest) -> None:
    assert sorted(app.dataframe[0].value["Rule"]) == ["E711", "F401"]
    assert len([b for b in app.checkbox if b.label == "Accept this fix"]) == 2
    app.button(key="demo_toggle").click().run()
    assert "not real responses from Claude" in app.warning[-1].value
    assert not app.exception


# --- What visitors see ---


def test_a_click_is_paid_from_the_shared_budget(site):
    owner(site).start_period(4, 1)
    app = page()

    assert not app.button(key="ai_logic").disabled
    app.button(key="ai_logic").click().run()

    assert site.sent == [SERVER_KEY]
    status = owner(site).status()
    assert status.spent == 7_000  # 1000 input + 500 output tokens on Sonnet.
    assert (status.reserved, status.calls) == (0, 1)
    assert "No possible logic bugs were reported." in texts(app)


def test_when_the_budget_has_run_out_claude_stays_visible_but_off(site):
    budget = owner(site)
    budget.start_period(0.05, 0.05)  # Less than one review can cost.

    app = page()

    claude_is_shown_but_off(app, USED_UP)
    assert site.sent == []
    free_features_work(app)


def test_when_todays_budget_has_run_out_claude_stays_visible_but_off(site):
    budget = owner(site)
    budget.start_period(4, 0.5)
    budget.settle(budget.reserve(0.48), 0.48, certain=True)

    app = page()

    claude_is_shown_but_off(app, DAILY_USED_UP)
    free_features_work(app)


@pytest.mark.parametrize("problem", ["down", "not set up", "no budget yet"])
def test_storage_problems_turn_claude_off_but_not_ruff(site, monkeypatch, problem):
    if problem == "down":
        owner(site).start_period(4, 1)
        site.down = True
    elif problem == "not set up":
        monkeypatch.delenv(STORE_URL_NAME)

    app = page()

    claude_is_shown_but_off(app, UNAVAILABLE)
    assert site.sent == []
    free_features_work(app)


def test_a_budget_used_up_between_drawing_and_clicking_is_refused(site):
    budget = owner(site)
    budget.start_period(0.2, 0.2)
    app = page()  # The page was drawn while the budget was open.
    assert not app.button(key="ai_logic").disabled

    # Visitors on another server spend the rest. This server still remembers the
    # status it read a few seconds ago, so its page still offers the button.
    remembered = STATUS_CACHE._value
    budget.settle(budget.reserve(0.15), 0.15, certain=True)
    STATUS_CACHE._value = remembered
    app.button(key="ai_logic").click().run()

    assert site.sent == []  # The atomic reservation refused it.
    assert USED_UP in [e.value for e in app.error]
    assert app.button(key="ai_logic").disabled


def test_an_account_problem_stops_claude_on_every_server(site):
    owner(site).start_period(4, 1)
    site.anthropic_error = AiUnavailableError("The API key was rejected.")
    app = page()

    app.button(key="ai_logic").click().run()

    assert "key was rejected" not in texts(app)
    status = owner(site).status()
    assert status.availability is Availability.STOPPED
    assert "Claude Console" in status.stop_reason

    # Another server: nothing in common with the first one except the database.
    PAID_AI_PAUSE.reset()
    STATUS_CACHE.clear()
    claude_is_shown_but_off(page(), UNAVAILABLE)


def test_the_owner_turns_claude_back_on_without_deploying(site, capsys):
    budget = owner(site)
    budget.start_period(4, 1)
    budget.stop("test")
    claude_is_shown_but_off(page(), UNAVAILABLE)

    code = budget_admin.main(["resume"], budget=budget, ask=lambda _: "RESUME")
    STATUS_CACHE.clear()  # Otherwise, the page sees the change within seconds.

    assert code == 0
    assert not page().button(key="ai_logic").disabled


def test_local_mode_never_touches_the_shared_budget(site, monkeypatch):
    monkeypatch.delenv(MODE_VARIABLE)
    monkeypatch.setenv(LIVE_AI_VARIABLE, "1")
    app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
    app.file_uploader[0].set_value(("code.py", CODE, "text/x-python")).run()
    app.button[0].click().run()
    app.text_input(key="ai_api_key").set_value("my-own-key").run()
    next(b for b in app.checkbox if b.label == ai_panel.CONSENT_LABEL).check().run()

    app.button(key="ai_logic").click().run()

    assert site.sent == ["my-own-key"]
    assert site.requests == []  # The database was never contacted.
    assert SPENDING.spent > 0  # The local in-memory limit is used, as before.


# --- The owner's admin command ---


def test_status_shows_the_budget_and_never_the_secrets(site, capsys, monkeypatch):
    budget = owner(site)
    budget.start_period(4, 1)
    budget.settle(budget.reserve(0.1), 0.0123, certain=True)
    monkeypatch.setenv(STORE_URL_NAME, URL)
    monkeypatch.setenv(STORE_TOKEN_NAME, TOKEN)

    assert budget_admin.main(["status"]) == 0  # Connects with the variables.

    out = capsys.readouterr().out
    assert "Budget: $4.0000" in out
    assert "Spent (billed): $0.0123" in out
    assert "Today (UTC): $0.0123 of $1.0000" in out
    assert TOKEN not in out and URL not in out


def test_status_before_any_budget(site, capsys):
    assert budget_admin.main(["status"], budget=owner(site)) == 0
    assert "No budget has been set yet" in capsys.readouterr().out


def test_starting_a_period_needs_the_amount_typed_again(site, capsys):
    budget = owner(site)
    budget.start_period(1, 1)
    budget.settle(budget.reserve(0.5), 0.5, certain=True)

    code = budget_admin.main(
        ["start", "--budget", "4", "--daily", "1"], budget=budget, ask=lambda _: "40"
    )

    assert code == 1
    assert "Nothing was changed." in capsys.readouterr().out
    assert budget.status().spent == 500_000


def test_starting_a_period_resets_spending_and_turns_claude_on(site, capsys):
    budget = owner(site)
    budget.start_period(1, 1)
    budget.settle(budget.reserve(0.5), 0.5, certain=True)
    budget.stop("Credits ran out.")

    code = budget_admin.main(
        ["start", "--budget", "4", "--daily", "1"], budget=budget, ask=lambda _: "4"
    )

    assert code == 0
    out = capsys.readouterr().out
    assert "Only do this after you have really added credits" in out
    status = budget.status()
    assert (status.availability, status.spent, status.limit, status.period) == (
        Availability.OPEN,
        0,
        4_000_000,
        2,
    )


def test_resume_needs_a_confirmation(site, capsys):
    budget = owner(site)
    budget.start_period(4, 1)
    budget.stop("test")

    assert budget_admin.main(["resume"], budget=budget, ask=lambda _: "yes") == 1
    assert budget.status().availability is Availability.STOPPED


def test_stop_and_daily_limit(site, capsys):
    budget = owner(site)
    budget.start_period(4, 1)

    assert budget_admin.main(["daily", "--usd", "0.5"], budget=budget) == 0
    assert budget.status().daily_limit == 500_000
    assert budget_admin.main(["daily", "--usd", "5"], budget=budget) == 1  # > $4.
    assert budget_admin.main(["stop"], budget=budget) == 0
    assert budget.status().availability is Availability.STOPPED


def test_admin_errors_never_show_the_secrets(site, capsys):
    site.down = True

    assert budget_admin.main(["status"], budget=owner(site)) == 1

    captured = capsys.readouterr()
    assert "storage could not be reached" in captured.err
    assert TOKEN not in captured.err + captured.out
    assert URL not in captured.err + captured.out
