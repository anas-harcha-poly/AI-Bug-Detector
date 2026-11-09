"""The two ways to get an AI review: illustrative examples, or a real call to Claude.

Both have the same methods, so the page displays their results the same way. The
Anthropic SDK is only imported when a real call is made, so the free mode never needs
it.
"""

import importlib.util
import json
import math
import os
import threading
import time
from dataclasses import dataclass, replace
from importlib import resources
from typing import Protocol

from ai_bug_detector.ai_models import (
    EXPLANATION_SCHEMA,
    LOGIC_BUG_SCHEMA,
    AiReviewError,
    AiUnavailableError,
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
from ai_bug_detector.deployment import is_public
from ai_bug_detector.fixes import split_lines
from ai_bug_detector.models import Issue
from ai_bug_detector.shared_budget import (
    Availability,
    BudgetRefused,
    public_budget,
)

# Real AI is off unless this is set, so a public deployment is safe by default.
LIVE_AI_VARIABLE = "AI_BUG_DETECTOR_ENABLE_LIVE_AI"
# Local mode: most this app may spend on Claude while it runs, in dollars. A public
# site uses the shared budget of shared_budget.py instead.
SPEND_LIMIT_VARIABLE = "AI_BUG_DETECTOR_MAX_SPEND_USD"
DEFAULT_SPEND_LIMIT_USD = 1.00

# Limits the length of each answer, and so the cost of each call.
MAX_OUTPUT_TOKENS = 6000
_TIMEOUT_SECONDS = 120
# Fixed, so a variable such as ANTHROPIC_BASE_URL cannot send the key somewhere else.
_API_URL = "https://api.anthropic.com"
# Rough but cautious: code averages more than 2 characters per token.
_CHARACTERS_PER_TOKEN = 2

# The cause of an error, when the API gives one (error.details.error_code). Checked
# first: for example, a spending limit comes as a rate limit error, but waiting does
# not help.
_ERROR_CODE_MESSAGES = {
    "enforced_spend_limit_reached": (
        "The spending limit of this API account has been reached. Raise it in the "
        "Claude Console, or wait until it resets."
    ),
    "organization_on_hold": "This API account is on hold. Check it in the Console.",
    "ramp_limit_exceeded": (
        "This API account cannot send this much yet. Try again later."
    ),
}
_STATUS_ERROR_MESSAGES = {
    "authentication_error": "The API key was rejected. Check it and try again.",
    "permission_error": "This API key is not allowed to use this model.",
    "billing_error": "The API account has a billing problem. Check your credits.",
    "not_found_error": "This model is not available. Try another one.",
    "rate_limit_error": "Too many requests. Wait a little, then try again.",
    "overloaded_error": "Claude is overloaded right now. Try again later.",
    "request_too_large": "The request is too large for the API.",
}


@dataclass(frozen=True)
class AiModel:
    """A model the user can choose, with its price in dollars per million tokens."""

    model_id: str
    label: str
    input_price: float
    output_price: float
    # Thinking is billed as output and counts toward MAX_OUTPUT_TOKENS. It is turned
    # off where the model allows it, for a lower and more predictable cost.
    thinking: dict | None = None


# The first one is the default.
AI_MODELS = (
    AiModel(
        "claude-sonnet-5-5",
        "Claude Sonnet 5.5 (balanced)",
        2.00,
        10.00,
        # Turns off up-front thinking. Without tools, there is no other thinking.
        {"type": "between_tools"},
    ),
    # Thinking cannot be turned off on this model: low effort keeps it short.
    AiModel("claude-opus-5-5", "Claude Opus 5.5 (most capable)", 4.00, 20.00),
    AiModel(
        "claude-haiku-5-5",
        "Claude Haiku 5.5 (lowest cost)",
        0.10,
        0.50,
        {"type": "disabled"},
    ),
)
_MODELS_BY_ID = {model.model_id: model for model in AI_MODELS}

# Offered to visitors of a public site, paid by the site owner: Opus is left out to
# keep costs under control. The first one is the default.
PUBLIC_MODELS = tuple(
    model for model in AI_MODELS if model.model_id != "claude-opus-5-5"
)
PUBLIC_MODEL_IDS = frozenset(model.model_id for model in PUBLIC_MODELS)

# How long paid AI stays paused on a public site after an account problem.
PAUSE_SECONDS = 15 * 60


class SpendTracker:
    """Local mode: what this app has spent on Claude since it started.

    Shared by every session of this process only, and lost when it stops: that is
    why a public site uses the shared budget in Redis instead.

    Before a call, its maximum cost is reserved; afterwards, the reservation is
    replaced by the real cost. A call that could go over the limit is never sent.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.spent = 0.0
        self.reserved = 0.0

    def reserve(self, amount: float, limit: float) -> bool:
        with self._lock:
            if self.spent + self.reserved + amount > limit:
                return False
            self.reserved += amount
            return True

    def settle(self, reserved: float, cost: float) -> None:
        with self._lock:
            self.reserved = max(0.0, self.reserved - reserved)
            self.spent += cost

    def reset(self) -> None:
        with self._lock:
            self.spent = self.reserved = 0.0


SPENDING = SpendTracker()


def spend_limit() -> float:
    """Return the spending limit in dollars. A setting that is not valid means 0."""
    value = os.environ.get(SPEND_LIMIT_VARIABLE)
    if value is None or not value.strip():
        return DEFAULT_SPEND_LIMIT_USD
    try:
        limit = float(value)
    except ValueError:
        return 0.0
    return limit if math.isfinite(limit) and limit >= 0 else 0.0


class Budget(Protocol):
    """Where the cost of calls is reserved, then recorded."""

    def reserve(self, cost_usd: float) -> object:
        """Reserve a maximum cost, or raise AiReviewError: nothing is sent then."""

    def settle(self, reservation: object, cost_usd: float, *, certain: bool) -> None:
        """Replace the reservation by the real cost (certain=False: maybe billed)."""


class LocalBudget:
    """Local mode: the in-memory limit of this process, see SPEND_LIMIT_VARIABLE."""

    def reserve(self, cost_usd: float) -> float:
        limit = spend_limit()
        if not SPENDING.reserve(cost_usd, limit):
            raise AiReviewError(
                f"This call could cost up to ${cost_usd:.3f}, which would go over "
                f"this app's spending limit of ${limit:.2f} (${SPENDING.spent:.3f} "
                f"already spent). Nothing was sent. See {SPEND_LIMIT_VARIABLE} in "
                "the README."
            )
        return cost_usd

    def settle(self, reservation: float, cost_usd: float, *, certain: bool) -> None:
        SPENDING.settle(reservation, cost_usd)


def default_budget() -> Budget:
    """The local limit, or on a public site the shared budget in Redis."""
    if not is_public():
        return LocalBudget()
    budget = public_budget()
    if budget is None:
        # Public mode without its storage: paid AI is blocked, never unlimited.
        raise BudgetRefused(Availability.UNAVAILABLE)
    return budget


def build_request(
    model_id: str, system: str, message: str, schema: dict, effort: str
) -> dict:
    """The request sent to Claude, without max_tokens. Token counting uses it too."""
    request = {
        "model": model_id,
        "system": system,
        "messages": [{"role": "user", "content": message}],
        "output_config": {
            "effort": effort,
            "format": {"type": "json_schema", "schema": schema},
        },
    }
    thinking = _MODELS_BY_ID[model_id].thinking
    if thinking is not None:
        request["thinking"] = thinking
    return request


class Reviewer(Protocol):
    """Anything able to produce an AI review."""

    def explain_issues(self, source: str, issues: list[Issue]) -> ExplanationReview: ...

    def find_logic_bugs(self, source: str) -> LogicReview: ...


def live_ai_enabled() -> bool:
    """Local mode only: True if real AI with the user's own key was turned on."""
    return not is_public() and os.environ.get(LIVE_AI_VARIABLE) == "1"


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
        return parse_logic_bugs(self._responses, len(split_lines(source)))


def read_demo_source() -> bytes:
    """Return the demo code. Its .txt name keeps it out of the project's own linting."""
    return _read_demo_file("sample_code.txt").encode("utf-8")


def _read_demo_file(name: str) -> str:
    return resources.files("ai_bug_detector.demo").joinpath(name).read_text("utf-8")


class ClaudeReviewer:
    """Sends code to Claude. Each method makes one paid call, and never retries."""

    def __init__(
        self, api_key: str, model_id: str, budget: Budget | None = None
    ) -> None:
        self._api_key = api_key
        self._model_id = model_id
        self._budget = budget

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
        line_count = len(split_lines(source))
        return replace(parse_logic_bugs(data, line_count), usage=usage)

    def _ask(
        self, system: str, message: str, schema: dict, effort: str
    ) -> tuple[object, Usage]:
        """Make one call within the budget, and record what it cost.

        The maximum cost is reserved first: if the budget refuses, nothing is sent.
        Afterwards, the real cost is recorded. When it is not known whether the call
        was billed, its maximum stays counted, to stay on the safe side.
        """
        if is_public() and self._model_id not in PUBLIC_MODEL_IDS:
            # Checked on the server: a visitor cannot pick a model the page hides.
            raise AiReviewError("This model is not available on this site.")
        budget = self._budget or default_budget()
        maximum = estimate_max_cost(message, system, self._model_id)
        reservation = budget.reserve(maximum)
        cost, certain = maximum, False  # Until the outcome is known.
        try:
            response = self._send(system, message, schema, effort)
        except AiReviewError as error:
            if not error.maybe_billed:
                cost, certain = 0.0, True  # Refused by the API: not billed.
            raise
        else:
            # Recorded even when the answer is unusable: a cut or refused answer
            # is billed too. Without usage, the maximum stays counted.
            usage = _usage(response)
            if usage is not None:
                cost, certain = actual_cost(usage, self._model_id), True
        finally:
            budget.settle(reservation, cost, certain=certain)
        return _read_response(response)

    def _send(self, system: str, message: str, schema: dict, effort: str) -> object:
        """Make the API call. Errors never carry the key or SDK request details."""
        try:
            import anthropic
        except ImportError:
            raise AiReviewError(
                "The Anthropic SDK is not installed. See the README."
            ) from None

        maybe_billed = True  # Unless the API clearly refused the request.
        try:
            client = anthropic.Anthropic(
                api_key=self._api_key,
                base_url=_API_URL,
                timeout=_TIMEOUT_SECONDS,
                max_retries=0,  # The SDK retries by default: each try could be billed.
            )
            return client.messages.create(
                max_tokens=MAX_OUTPUT_TOKENS,
                **build_request(self._model_id, system, message, schema, effort),
            )
        except anthropic.APITimeoutError:
            error_message = f"Claude did not answer within {_TIMEOUT_SECONDS} seconds."
        except anthropic.APIConnectionError:
            error_message = "Cannot reach the API. Check your internet connection."
        except anthropic.APIStatusError as error:
            error_message = self._status_error_message(error)
            # A refused request (4xx) or an overloaded API (529) is not billed. For
            # another server error, the answer may have been generated: counted.
            maybe_billed = error.status_code >= 500 and error.status_code != 529
            if _is_account_problem(error):
                raise AiUnavailableError(
                    f"{error_message} Nothing was retried."
                ) from None
        except Exception:
            # Any other error could contain request details, so none of it is shown.
            error_message = "The request could not be completed."
        failure = AiReviewError(f"{error_message} Nothing was retried.")
        failure.maybe_billed = maybe_billed
        raise failure from None

    def _status_error_message(self, error) -> str:
        known_message = _ERROR_CODE_MESSAGES.get(
            getattr(error, "error_code", None)
        ) or _STATUS_ERROR_MESSAGES.get(error.type)
        if known_message:
            return known_message
        details = str(error.message).replace(self._api_key, "[hidden]")
        if "credit balance" in details.lower():
            return _STATUS_ERROR_MESSAGES["billing_error"]
        return f"The API returned an error ({error.status_code}): {details[:300]}"


_ACCOUNT_ERROR_TYPES = {"authentication_error", "permission_error", "billing_error"}
_ACCOUNT_ERROR_CODES = {
    "enforced_spend_limit_reached",
    "organization_on_hold",
    "ramp_limit_exceeded",
}


def _is_account_problem(error) -> bool:
    """True when the account itself cannot be used: retrying soon will not help."""
    details = str(getattr(error, "message", "")).lower()
    return (
        error.type in _ACCOUNT_ERROR_TYPES
        or getattr(error, "error_code", None) in _ACCOUNT_ERROR_CODES
        or "credit balance" in details
    )


class PaidAiPause:
    """Pauses paid AI for everyone on a public site after an account problem.

    Shared by all sessions of this server process, like the spending tracker.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._until = 0.0

    def pause(self, seconds: float = PAUSE_SECONDS) -> None:
        with self._lock:
            self._until = max(self._until, time.monotonic() + seconds)

    def is_paused(self) -> bool:
        with self._lock:
            return time.monotonic() < self._until

    def reset(self) -> None:
        with self._lock:
            self._until = 0.0


PAID_AI_PAUSE = PaidAiPause()


def _usage(response) -> Usage | None:
    """Return the billed tokens of a response, or None if it does not say."""
    try:
        return Usage(
            int(response.usage.input_tokens), int(response.usage.output_tokens)
        )
    except (AttributeError, TypeError, ValueError):
        return None


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

    return data, _usage(response) or Usage(0, 0)
