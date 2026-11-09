"""Readiness for real calls to Claude, checked with the real SDK on a fake network.

Every request is answered by a local handler: nothing can reach the API, and nothing
is billed. Skipped when the optional SDK is not installed.
"""

import json
import logging
from pathlib import Path

import pytest

anthropic = pytest.importorskip("anthropic")
httpx2 = pytest.importorskip("httpx2")

from streamlit.testing.v1 import AppTest  # noqa: E402

from ai_bug_detector import ai_panel, claude_check  # noqa: E402
from ai_bug_detector.ai_models import (  # noqa: E402
    EXPLANATION_SCHEMA,
    LOGIC_BUG_SCHEMA,
    AiReviewError,
    Usage,
)
from ai_bug_detector.ai_review import (  # noqa: E402
    LIVE_AI_VARIABLE,
    MAX_OUTPUT_TOKENS,
    SPEND_LIMIT_VARIABLE,
    SPENDING,
    ClaudeReviewer,
    actual_cost,
)
from ai_bug_detector.docker_sandbox import SANDBOX_VARIABLE  # noqa: E402
from ai_bug_detector.fixes import FixOrigin, apply_fixes, fixes_from_review  # noqa: E402

FAKE_KEY = "fake-key-SECRET-for-tests-only"
SOURCE = "def total(values):\n    return sum(values) + 1\n"
APP_PATH = Path(__file__).resolve().parents[1] / "app.py"


def message_response(
    text='{"bugs": []}', stop_reason="end_turn", input_tokens=1000, output_tokens=500
):
    def handler(request):
        return httpx2.Response(
            200,
            json={
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-5-5",
                "content": [{"type": "text", "text": text}] if text else [],
                "stop_reason": stop_reason,
                "stop_sequence": None,
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                },
            },
        )

    return handler


def api_error(status, error_type, error_code=None):
    def handler(request):
        error = {"type": error_type, "message": "API message"}
        if error_code:
            error["details"] = {"error_code": error_code}
        return httpx2.Response(status, json={"type": "error", "error": error})

    return handler


def model_info(request):
    model_id = request.url.path.rsplit("/", 1)[-1]
    return httpx2.Response(
        200,
        json={
            "id": model_id,
            "type": "model",
            "display_name": model_id,
            "created_at": "2026-01-01T00:00:00Z",
            "capabilities": {
                "thinking": {
                    "types": {
                        "between_tools": {"supported": model_id.startswith("claude-s")},
                        "disabled": {"supported": model_id.startswith("claude-h")},
                    }
                }
            },
        },
    )


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    SPENDING.reset()
    monkeypatch.delenv(SPEND_LIMIT_VARIABLE, raising=False)
    monkeypatch.delenv(LIVE_AI_VARIABLE, raising=False)
    monkeypatch.delenv(SANDBOX_VARIABLE, raising=False)
    yield
    SPENDING.reset()


@pytest.fixture
def network(monkeypatch):
    """Route the SDK to local handlers. Return (requests, handlers by path)."""
    requests = []
    routes = {"messages": message_response()}

    def handle(request):
        requests.append(request)
        path = request.url.path
        if path.endswith("/count_tokens"):
            return httpx2.Response(200, json={"input_tokens": 1234})
        if path.startswith("/v1/models/"):
            return routes.get("models", model_info)(request)
        return routes["messages"](request)

    real_client = anthropic.Anthropic

    def client_on_fake_network(**kwargs):
        transport = httpx2.MockTransport(handle)
        return real_client(
            **kwargs, http_client=anthropic.DefaultHttpxClient(transport=transport)
        )

    monkeypatch.setattr(anthropic, "Anthropic", client_on_fake_network)
    return requests, routes


def body(request) -> dict:
    return json.loads(request.content)


# --- The request each model receives ---


@pytest.mark.parametrize(
    ("model_id", "thinking"),
    [
        ("claude-sonnet-5-5", {"type": "between_tools"}),
        ("claude-haiku-5-5", {"type": "disabled"}),
        ("claude-opus-5-5", None),  # Cannot be turned off on this model.
    ],
)
def test_each_model_gets_its_thinking_setting(network, model_id, thinking):
    requests, _ = network

    ClaudeReviewer(FAKE_KEY, model_id).find_logic_bugs(SOURCE)

    sent = body(requests[0])
    assert sent.get("thinking") == thinking
    assert sent["model"] == model_id
    assert sent["max_tokens"] == MAX_OUTPUT_TOKENS
    assert sent["output_config"]["effort"] == "medium"
    assert sent["output_config"]["format"]["schema"] == LOGIC_BUG_SCHEMA


def test_explanations_use_low_effort_and_their_schema(network):
    requests, routes = network
    routes["messages"] = message_response('{"explanations": []}')

    ClaudeReviewer(FAKE_KEY, "claude-sonnet-5-5").explain_issues(SOURCE, [])

    config = body(requests[0])["output_config"]
    assert config["effort"] == "low"
    assert config["format"]["schema"] == EXPLANATION_SCHEMA


def test_schema_sent_asks_for_the_lines_to_replace(network):
    requests, _ = network

    ClaudeReviewer(FAKE_KEY, "claude-sonnet-5-5").find_logic_bugs(SOURCE)

    item = body(requests[0])["output_config"]["format"]["schema"]["properties"]["bugs"][
        "items"
    ]
    assert {"fix_start_line", "fix_end_line"} <= set(item["required"])
    assert item["additionalProperties"] is False


def test_an_answer_with_a_range_becomes_an_applicable_fix(network):
    _, routes = network
    answer = {
        "bugs": [
            {
                "line": 2,
                "title": "Off by one",
                "reasoning": "total([]) returns 1.",
                "confidence": "high",
                "suggested_fix": "    return sum(values)",
                "fix_start_line": 2,
                "fix_end_line": 2,
            }
        ]
    }
    routes["messages"] = message_response(json.dumps(answer))

    review = ClaudeReviewer(FAKE_KEY, "claude-sonnet-5-5").find_logic_bugs(SOURCE)
    (fix,), unusable = fixes_from_review(SOURCE, review, FixOrigin.CLAUDE)

    assert unusable == []
    assert apply_fixes(SOURCE, [fix]).corrected == (
        "def total(values):\n    return sum(values)\n"
    )


# --- Clear messages for account problems ---


@pytest.mark.parametrize(
    ("status", "error_type", "error_code", "expected"),
    [
        (
            429,
            "rate_limit_error",
            "enforced_spend_limit_reached",
            "spending limit of this API account",
        ),
        (400, "invalid_request_error", "organization_on_hold", "on hold"),
        (429, "rate_limit_error", "ramp_limit_exceeded", "cannot send this much"),
        (413, "request_too_large", None, "too large for the API"),
        (429, "rate_limit_error", None, "Too many requests"),
    ],
)
def test_account_problems_get_a_clear_message(
    network, status, error_type, error_code, expected
):
    requests, routes = network
    routes["messages"] = api_error(status, error_type, error_code)

    with pytest.raises(AiReviewError, match=expected):
        ClaudeReviewer(FAKE_KEY, "claude-sonnet-5-5").find_logic_bugs(SOURCE)

    assert len(requests) == 1  # Never retried.


# --- Spending ---


def test_the_real_cost_of_a_call_is_recorded(network):
    ClaudeReviewer(FAKE_KEY, "claude-sonnet-5-5").find_logic_bugs(SOURCE)

    expected = actual_cost(Usage(1000, 500), "claude-sonnet-5-5")
    assert SPENDING.spent == pytest.approx(expected)
    assert SPENDING.reserved == 0


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
def test_a_billed_answer_that_cannot_be_used_is_still_recorded(network, stop_reason):
    _, routes = network
    routes["messages"] = message_response("", stop_reason)

    with pytest.raises(AiReviewError):
        ClaudeReviewer(FAKE_KEY, "claude-sonnet-5-5").find_logic_bugs(SOURCE)

    assert SPENDING.spent == pytest.approx(
        actual_cost(Usage(1000, 500), "claude-sonnet-5-5")
    )


def test_a_failed_request_costs_nothing_and_frees_its_reservation(network):
    _, routes = network
    routes["messages"] = api_error(529, "overloaded_error")

    with pytest.raises(AiReviewError, match="overloaded"):
        ClaudeReviewer(FAKE_KEY, "claude-sonnet-5-5").find_logic_bugs(SOURCE)

    assert (SPENDING.spent, SPENDING.reserved) == (0, 0)


def test_a_call_that_could_exceed_the_limit_is_never_sent(network, monkeypatch):
    requests, _ = network
    monkeypatch.setenv(SPEND_LIMIT_VARIABLE, "0.001")

    with pytest.raises(AiReviewError, match="spending limit of \\$0.00"):
        ClaudeReviewer(FAKE_KEY, "claude-sonnet-5-5").find_logic_bugs(SOURCE)

    assert requests == []


def test_the_limit_counts_every_call(network, monkeypatch):
    requests, routes = network
    monkeypatch.setenv(SPEND_LIMIT_VARIABLE, "0.10")
    # Each answer uses the whole output allowance: about $0.06 with Sonnet.
    routes["messages"] = message_response(output_tokens=MAX_OUTPUT_TOKENS)
    reviewer = ClaudeReviewer(FAKE_KEY, "claude-sonnet-5-5")

    reviewer.find_logic_bugs(SOURCE)
    with pytest.raises(AiReviewError, match="spending limit"):
        reviewer.find_logic_bugs(SOURCE)

    assert len(requests) == 1


# --- The key never shows ---


def test_the_key_is_not_in_the_sdk_logs(network, caplog):
    _, routes = network
    routes["messages"] = api_error(401, "authentication_error")
    caplog.set_level(logging.DEBUG)

    with pytest.raises(AiReviewError) as caught:
        ClaudeReviewer(FAKE_KEY, "claude-sonnet-5-5").find_logic_bugs(SOURCE)

    assert caplog.text  # The SDK did log the request...
    assert FAKE_KEY not in caplog.text  # ...but never the key.
    assert FAKE_KEY not in str(caught.value)


# --- The free check tool ---


def run_check(argv=(), answer="y", key=FAKE_KEY):
    lines = []
    code = claude_check.main(
        list(argv),
        read_key=lambda prompt: key,
        ask=lambda prompt: answer,
        out=lines.append,
    )
    return code, "\n".join(lines)


def test_check_tool_confirms_models_and_counts_tokens_for_free(network):
    requests, _ = network

    code, output = run_check()

    assert code == 0
    for model_id in ("claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5"):
        assert f"{model_id}: available" in output
    assert "accepts thinking 'between_tools'" in output
    assert "accepts thinking 'disabled'" in output
    assert output.count("1234 input tokens") == 6
    assert "nothing was billed" in output
    paths = {request.url.path for request in requests}
    assert "/v1/messages" not in paths  # No message is ever created.
    assert paths <= {
        "/v1/models/claude-sonnet-5-5",
        "/v1/models/claude-opus-5-5",
        "/v1/models/claude-haiku-5-5",
        "/v1/messages/count_tokens",
    }


def test_check_tool_counts_the_exact_request_the_app_sends(network):
    requests, _ = network

    run_check()

    counted = [body(r) for r in requests if r.url.path.endswith("/count_tokens")]
    sonnet = [c for c in counted if c["model"] == "claude-sonnet-5-5"]
    assert {c["output_config"]["effort"] for c in sonnet} == {"low", "medium"}
    assert all(c["thinking"] == {"type": "between_tools"} for c in sonnet)
    assert all("max_tokens" not in c for c in counted)


def test_check_tool_never_prints_the_key(network):
    _, output = run_check()

    assert FAKE_KEY not in output


def test_check_tool_sends_no_code_without_permission(network):
    requests, _ = network

    code, output = run_check(answer="n")

    assert code == 0
    assert "Skipped: nothing was sent" in output
    assert not any(r.url.path.endswith("/count_tokens") for r in requests)


def test_check_tool_reports_a_missing_model(network):
    _, routes = network

    def missing_opus(request):
        if request.url.path.endswith("claude-opus-5-5"):
            return api_error(404, "not_found_error")(request)
        return model_info(request)

    routes["models"] = missing_opus

    code, output = run_check(answer="n")

    assert code == 1
    assert "claude-opus-5-5: NOT FOUND" in output


def test_check_tool_reports_an_unsupported_thinking_setting(network):
    _, routes = network

    def no_between_tools(request):
        response = model_info(request)
        data = json.loads(response.content)
        data["capabilities"]["thinking"]["types"]["between_tools"]["supported"] = False
        return httpx2.Response(200, json=data)

    routes["models"] = no_between_tools

    code, output = run_check(answer="n")

    assert code == 1
    assert "does NOT accept thinking 'between_tools'" in output


def test_check_tool_stops_on_a_rejected_key(network):
    requests, routes = network
    routes["models"] = api_error(401, "authentication_error")

    code, output = run_check()

    assert code == 1
    assert "key was rejected" in output
    assert len(requests) == 1


def test_check_tool_needs_a_key(network):
    requests, _ = network

    code, output = run_check(key="   ")

    assert code == 1
    assert requests == []


def test_check_tool_measures_a_chosen_file(network, tmp_path):
    path = tmp_path / "first_test.py"
    path.write_text("def f(x):\n    return x + 1\n", encoding="utf-8")

    code, output = run_check([str(path)])

    assert code == 0
    assert "first_test.py" in output


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (b"x = 1\n" + b"#" * (30 * 1024), "larger than 30 KB"),
        (b"x = '\xe9'\n", "not valid UTF-8"),
    ],
)
def test_check_tool_refuses_unsuitable_files(network, tmp_path, content, reason):
    requests, _ = network
    path = tmp_path / "file.py"
    path.write_bytes(content)

    code, output = run_check([str(path)])

    assert code == 1
    assert reason in output
    assert requests == []


# --- Ruff, the demo and fixes keep working when Claude is unavailable ---


@pytest.mark.parametrize(
    ("status", "error_type", "error_code", "message"),
    [
        (400, "invalid_request_error", None, "returned an error"),
        (402, "billing_error", None, "billing problem"),
        (429, "rate_limit_error", "enforced_spend_limit_reached", "spending limit"),
        (529, "overloaded_error", None, "overloaded"),
    ],
)
def test_free_features_keep_working_when_claude_fails(
    network, monkeypatch, status, error_type, error_code, message
):
    _, routes = network
    routes["messages"] = api_error(status, error_type, error_code)
    monkeypatch.setenv(LIVE_AI_VARIABLE, "1")
    monkeypatch.setattr(ai_panel, "sdk_available", lambda: True)
    app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
    code = b"import os\n\nx = 1\nif x == None:\n    pass\n"
    app.file_uploader[0].set_value(("code.py", code, "text/x-python")).run()
    app.button[0].click().run()
    app.text_input(key="ai_api_key").set_value(FAKE_KEY).run()
    next(b for b in app.checkbox if b.label == ai_panel.CONSENT_LABEL).check().run()

    app.button(key="ai_logic").click().run()

    assert message in app.error[0].value
    assert FAKE_KEY not in app.error[0].value
    assert not app.exception
    assert sorted(app.dataframe[0].value["Rule"]) == ["E711", "F401"]  # Ruff.
    fix_boxes = [b for b in app.checkbox if b.label == "Accept this fix"]
    assert len(fix_boxes) == 2  # Ruff's fixes are still offered.
    app.button(key="demo_toggle").click().run()
    assert "not real responses from Claude" in app.warning[-1].value
