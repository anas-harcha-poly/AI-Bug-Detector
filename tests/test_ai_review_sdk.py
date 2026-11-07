"""Mode Claude simulated: the real SDK runs, but on a fake local network.

The fake network answers every request itself, so no request can reach the real API and
nothing is billed. These tests are skipped when the optional SDK is not installed.
"""

import json

import pytest

anthropic = pytest.importorskip("anthropic")
httpx2 = pytest.importorskip("httpx2")

from ai_bug_detector.ai_models import AiReviewError  # noqa: E402
from ai_bug_detector.ai_review import (  # noqa: E402
    LOGIC_BUG_SCHEMA,
    MAX_OUTPUT_TOKENS,
    ClaudeReviewer,
)

FAKE_KEY = "fake-key-for-tests-only"
SOURCE = "x = 1\n"


def success(request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(
        200,
        json={
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-5-5",
            "content": [{"type": "text", "text": '{"bugs": []}'}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
    )


def api_error(status: int, error_type: str, message: str = "API message"):
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            status,
            json={
                "type": "error",
                "error": {"type": error_type, "message": message},
            },
        )

    return handler


@pytest.fixture
def fake_network(monkeypatch):
    """Route the SDK to a handler, and return the list of requests it received."""
    requests = []
    state = {"handler": success}

    def transport_handler(request):
        requests.append(request)
        return state["handler"](request)

    real_client = anthropic.Anthropic

    def client_on_fake_network(**kwargs):
        transport = httpx2.MockTransport(transport_handler)
        return real_client(
            **kwargs, http_client=anthropic.DefaultHttpxClient(transport=transport)
        )

    monkeypatch.setattr(anthropic, "Anthropic", client_on_fake_network)
    return requests, state


def review(model_id="claude-sonnet-5-5"):
    return ClaudeReviewer(FAKE_KEY, model_id).find_logic_bugs(SOURCE)


def test_request_goes_to_the_official_api_with_the_expected_content(fake_network):
    requests, _ = fake_network

    result = review()

    assert result.bugs == []
    assert result.usage.input_tokens == 10
    assert len(requests) == 1

    request = requests[0]
    assert request.url.host == "api.anthropic.com"
    assert request.url.path == "/v1/messages"
    assert request.headers["authorization"] == f"Bearer {FAKE_KEY}"
    assert request.extensions["timeout"]["read"] == 120
    assert request.headers["x-stainless-retry-count"] == "0"

    body = json.loads(request.content)
    assert body["model"] == "claude-sonnet-5-5"
    assert body["max_tokens"] == MAX_OUTPUT_TOKENS
    assert body["output_config"]["format"]["schema"] == LOGIC_BUG_SCHEMA
    assert "1: x = 1" in body["messages"][0]["content"]
    assert "fallbacks" not in body
    assert "tools" not in body


def test_no_automatic_fallback_header_is_sent(fake_network):
    requests, _ = fake_network

    review()

    assert "fallback" not in requests[0].headers.get("anthropic-beta", "")


@pytest.mark.parametrize(
    "model_id", ["claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5"]
)
def test_selected_model_is_the_one_requested(fake_network, model_id):
    requests, _ = fake_network

    review(model_id)

    assert json.loads(requests[0].content)["model"] == model_id


def test_environment_cannot_redirect_the_key_elsewhere(fake_network, monkeypatch):
    requests, _ = fake_network
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://attacker.example")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key-from-environment")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "token-from-environment")

    review()

    assert requests[0].url.host == "api.anthropic.com"
    assert requests[0].headers["authorization"] == f"Bearer {FAKE_KEY}"
    assert "x-api-key" not in requests[0].headers


@pytest.mark.parametrize(
    ("status", "error_type", "expected"),
    [
        (401, "authentication_error", "key was rejected"),
        (402, "billing_error", "billing problem"),
        (403, "permission_error", "not allowed"),
        (404, "not_found_error", "not available"),
        (429, "rate_limit_error", "Too many requests"),
        (529, "overloaded_error", "overloaded"),
        (500, "api_error", "returned an error \\(500\\)"),
    ],
)
def test_api_errors_are_clear_and_never_retried(
    fake_network, status, error_type, expected
):
    requests, state = fake_network
    state["handler"] = api_error(status, error_type)

    with pytest.raises(AiReviewError, match=expected) as caught:
        review()

    assert len(requests) == 1  # One paid attempt at most: nothing was retried.
    assert "Nothing was retried" in str(caught.value)


def test_low_credit_balance_message_is_explained(fake_network):
    _, state = fake_network
    state["handler"] = api_error(
        400, "invalid_request_error", "Your credit balance is too low."
    )

    with pytest.raises(AiReviewError, match="billing problem"):
        review()


@pytest.mark.parametrize(
    ("network_error", "expected"),
    [
        (httpx2.ReadTimeout("slow"), "did not answer"),
        (httpx2.ConnectError("down"), "Cannot reach the API"),
    ],
)
def test_network_problems_are_clear_and_never_retried(
    fake_network, network_error, expected
):
    requests, state = fake_network

    def fail(request):
        raise network_error

    state["handler"] = fail

    with pytest.raises(AiReviewError, match=expected):
        review()

    assert len(requests) == 1


def test_key_never_appears_in_an_error(fake_network):
    _, state = fake_network
    state["handler"] = api_error(
        400, "invalid_request_error", f"Bad request with {FAKE_KEY} inside"
    )

    with pytest.raises(AiReviewError) as caught:
        review()

    assert FAKE_KEY not in str(caught.value)
    assert "[hidden]" in str(caught.value)
    # Nothing is chained, so a traceback cannot show the SDK's request details.
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__


def test_unexpected_error_details_are_not_shown(fake_network):
    _, state = fake_network

    def fail(request):
        raise RuntimeError(f"unexpected failure with {FAKE_KEY}")

    state["handler"] = fail

    with pytest.raises(AiReviewError) as caught:
        review()

    assert FAKE_KEY not in str(caught.value)
