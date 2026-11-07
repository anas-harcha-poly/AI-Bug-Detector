"""The two ways to get an AI review: illustrative examples, or a real call to Claude.

Both have the same methods, so the page displays their results the same way. The
Anthropic SDK is only imported when a real call is made, so the free mode never needs
it.
"""

import importlib.util
import json
import os
from dataclasses import dataclass, replace
from importlib import resources
from typing import Protocol

from ai_bug_detector.ai_models import (
    EXPLANATION_SCHEMA,
    LOGIC_BUG_SCHEMA,
    AiReviewError,
    ExplanationReview,
    LogicReview,
    Usage,
    parse_explanations,
    parse_logic_bugs,
)
from ai_bug_detector.ai_prompts import (
    EXPLANATION_SYSTEM_PROMPT,
    LOGIC_BUG_SYSTEM_PROMPT,
    build_explanation_message,
    build_logic_bug_message,
)
from ai_bug_detector.models import Issue

# Real AI is off unless this is set, so a public deployment is safe by default.
LIVE_AI_VARIABLE = "AI_BUG_DETECTOR_ENABLE_LIVE_AI"

# Limits the length of each answer, and so the cost of each call.
MAX_OUTPUT_TOKENS = 6000
_TIMEOUT_SECONDS = 120
# Fixed, so a variable such as ANTHROPIC_BASE_URL cannot send the key somewhere else.
_API_URL = "https://api.anthropic.com"
# Rough but cautious: code averages more than 2 characters per token.
_CHARACTERS_PER_TOKEN = 2

_STATUS_ERROR_MESSAGES = {
    "authentication_error": "The API key was rejected. Check it and try again.",
    "permission_error": "This API key is not allowed to use this model.",
    "billing_error": "The API account has a billing problem. Check your credits.",
    "not_found_error": "This model is not available. Try another one.",
    "rate_limit_error": "Too many requests. Wait a little, then try again.",
    "overloaded_error": "Claude is overloaded right now. Try again later.",
}


@dataclass(frozen=True)
class AiModel:
    """A model the user can choose, with its price in dollars per million tokens."""

    model_id: str
    label: str
    input_price: float
    output_price: float


# The first one is the default.
AI_MODELS = (
    AiModel("claude-sonnet-5-5", "Claude Sonnet 5.5 (balanced)", 2.00, 10.00),
    AiModel("claude-opus-5-5", "Claude Opus 5.5 (most capable)", 4.00, 20.00),
    AiModel("claude-haiku-5-5", "Claude Haiku 5.5 (lowest cost)", 0.10, 0.50),
)
_MODELS_BY_ID = {model.model_id: model for model in AI_MODELS}


class Reviewer(Protocol):
    """Anything able to produce an AI review."""

    def explain_issues(self, source: str, issues: list[Issue]) -> ExplanationReview: ...

    def find_logic_bugs(self, source: str) -> LogicReview: ...


def live_ai_enabled() -> bool:
    """Return True only if real AI calls were explicitly turned on for this server."""
    return os.environ.get(LIVE_AI_VARIABLE) == "1"


def sdk_available() -> bool:
    """Return True if the optional Anthropic SDK is installed, without importing it."""
    return importlib.util.find_spec("anthropic") is not None


def estimate_max_cost(message: str, system_prompt: str, model_id: str) -> float:
    """Return a cautious maximum cost in dollars, computed locally before any call."""
    characters = len(message) + len(system_prompt)
    return _cost(
        Usage(characters // _CHARACTERS_PER_TOKEN, MAX_OUTPUT_TOKENS), model_id
    )


def actual_cost(usage: Usage, model_id: str) -> float:
    """Return the cost in dollars of a finished call."""
    return _cost(usage, model_id)


def _cost(usage: Usage, model_id: str) -> float:
    model = _MODELS_BY_ID[model_id]
    return (
        usage.input_tokens * model.input_price
        + usage.output_tokens * model.output_price
    ) / 1_000_000


class DemoReviewer:
    """Returns examples written by hand for the demo file. Nothing is sent anywhere."""

    def __init__(self) -> None:
        self._responses = json.loads(_read_demo_file("illustrative_responses.json"))

    def explain_issues(self, source: str, issues: list[Issue]) -> ExplanationReview:
        return parse_explanations(self._responses, issues)

    def find_logic_bugs(self, source: str) -> LogicReview:
        return parse_logic_bugs(self._responses, len(source.splitlines()))


def read_demo_source() -> bytes:
    """Return the demo code. Its .txt name keeps it out of the project's own linting."""
    return _read_demo_file("sample_code.txt").encode("utf-8")


def _read_demo_file(name: str) -> str:
    return resources.files("ai_bug_detector.demo").joinpath(name).read_text("utf-8")


class ClaudeReviewer:
    """Sends code to Claude. Each method makes one paid call, and never retries."""

    def __init__(self, api_key: str, model_id: str) -> None:
        self._api_key = api_key
        self._model_id = model_id

    def explain_issues(self, source: str, issues: list[Issue]) -> ExplanationReview:
        data, usage = self._ask(
            EXPLANATION_SYSTEM_PROMPT,
            build_explanation_message(source, issues),
            EXPLANATION_SCHEMA,
            effort="low",
        )
        return replace(parse_explanations(data, issues), usage=usage)

    def find_logic_bugs(self, source: str) -> LogicReview:
        data, usage = self._ask(
            LOGIC_BUG_SYSTEM_PROMPT,
            build_logic_bug_message(source),
            LOGIC_BUG_SCHEMA,
            effort="medium",
        )
        return replace(parse_logic_bugs(data, len(source.splitlines())), usage=usage)

    def _ask(
        self, system: str, message: str, schema: dict, effort: str
    ) -> tuple[object, Usage]:
        return _read_response(self._send(system, message, schema, effort))

    def _send(self, system: str, message: str, schema: dict, effort: str) -> object:
        """Make the API call. Errors never carry the key or SDK request details."""
        try:
            import anthropic
        except ImportError:
            raise AiReviewError(
                "The Anthropic SDK is not installed. See the README."
            ) from None

        try:
            client = anthropic.Anthropic(
                api_key=self._api_key,
                base_url=_API_URL,
                timeout=_TIMEOUT_SECONDS,
                max_retries=0,  # The SDK retries by default: each try could be billed.
            )
            return client.messages.create(
                model=self._model_id,
                max_tokens=MAX_OUTPUT_TOKENS,
                system=system,
                messages=[{"role": "user", "content": message}],
                output_config={
                    "effort": effort,
                    "format": {"type": "json_schema", "schema": schema},
                },
            )
        except anthropic.APITimeoutError:
            error_message = f"Claude did not answer within {_TIMEOUT_SECONDS} seconds."
        except anthropic.APIConnectionError:
            error_message = "Cannot reach the API. Check your internet connection."
        except anthropic.APIStatusError as error:
            error_message = self._status_error_message(error)
        except Exception:
            # Any other error could contain request details, so none of it is shown.
            error_message = "The request could not be completed."
        raise AiReviewError(f"{error_message} Nothing was retried.") from None

    def _status_error_message(self, error) -> str:
        known_message = _STATUS_ERROR_MESSAGES.get(error.type)
        if known_message:
            return known_message
        details = str(error.message).replace(self._api_key, "[hidden]")
        if "credit balance" in details.lower():
            return _STATUS_ERROR_MESSAGES["billing_error"]
        return f"The API returned an error ({error.status_code}): {details[:300]}"


def _read_response(response) -> tuple[object, Usage]:
    """Return the JSON data and usage of a response, after checking how it ended."""
    if response.stop_reason == "refusal":
        raise AiReviewError(
            "Claude declined to review this file. This call may still be billed. "
            "Nothing was retried."
        )
    if response.stop_reason == "max_tokens":
        raise AiReviewError(
            "The answer reached the length limit and was cut off. This call is billed. "
            "Nothing was retried."
        )

    text = next((b.text for b in response.content if b.type == "text"), None)
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        raise AiReviewError("The AI answer was not valid JSON.") from None

    usage = Usage(response.usage.input_tokens, response.usage.output_tokens)
    return data, usage
