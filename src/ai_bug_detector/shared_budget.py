"""The AI budget and call limits of a public site, shared by every visitor and server.

They are kept in an external Redis database (Upstash REST API), so they survive
restarts and are the same for every server process. Amounts are whole millionths of
a dollar, so no rounding error can add up.

How a call is allowed and paid for:

1. Before anything is sent, one Lua script checks every global limit and, if all of
   them allow it, reserves the call. Redis runs a script as one step, so calls made
   at the same moment, even from several servers, can never get around a limit. The
   limits are: the budget, today's limit, the calls of the last hour, and the calls
   running at the same time. If one would be passed, or they cannot be checked,
   nothing is sent.
2. After the call, the reservation is replaced by the real cost. When it is not known
   whether the call was billed (a timeout, for example), its maximum stays counted.
3. A reservation that is never settled (a server crashed during the call) expires
   after RESERVATION_SECONDS: its maximum stays counted, and its place among the
   calls running at the same time is freed.

The global budget is never reset by time. Only the owner resets it, after buying
credits, with: python -m ai_bug_detector.budget_admin start
A daily limit, per UTC day, also keeps one day of abuse from using everything.
"""

import json
import math
import time
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from threading import Lock

from ai_bug_detector.ai_models import AiReviewError
from ai_bug_detector.deployment import (
    STORE_TOKEN_NAME,
    STORE_URL_NAME,
    server_setting,
)
from ai_bug_detector.redis_rest import RedisRest, StoreError

KEY_PREFIX = "ai-bug-detector:budget"
MICRO = 1_000_000  # Millionths of a dollar in one dollar.
# Longer than any call can take (2 minutes, then reading the answer): after this, a
# call that never reported back is counted as spent at its maximum, and no longer
# counts as running.
RESERVATION_SECONDS = 5 * 60
# Used until the owner sets other values with budget_admin limits.
DEFAULT_CALLS_PER_HOUR = 10
DEFAULT_CONCURRENT_CALLS = 2
HOUR_SECONDS = 3600
MAX_CALLS_PER_HOUR = 1000
MAX_CONCURRENT_CALLS = 20
_DAY_KEY_SECONDS = 8 * 24 * 3600
_HISTORY_LENGTH = 50
# Refuses a budget typed with an extra zero, for example. Raise it if really needed.
MAX_BUDGET_USD = 100.0
STATUS_CACHE_SECONDS = 10

# Lua scripts run inside Redis, each one as a single step. They only use what Redis
# Lua 5.1 offers: whole numbers, no division, no global variables.
_NUMBER = """
local function num(key, name)
  local value = redis.call('HGET', key, name)
  if not value then return 0 end
  local number = tonumber(value)
  -- Stops the whole script: damaged data blocks calls, never allows them.
  if not number then error('budget data is not valid') end
  return number
end
local function setting(name, default)
  if redis.call('HEXISTS', KEYS[1], name) == 0 then return tonumber(default) end
  return num(KEYS[1], name)
end
"""

# KEYS: state, in-flight deadlines, in-flight amounts, today, recent calls
# ARGV: reservation id, amount, now (ms), deadline (ms), seconds to keep today's data,
#       default calls per hour, default calls at the same time, start of the hour (ms)
_RESERVE = (
    _NUMBER
    + """
local stale = redis.call('ZRANGEBYSCORE', KEYS[2], '-inf', ARGV[3])
for _, id in ipairs(stale) do
  local amount = tonumber(redis.call('HGET', KEYS[3], id) or '0') or 0
  redis.call('HINCRBY', KEYS[1], 'reserved', -amount)
  redis.call('HINCRBY', KEYS[1], 'uncertain', amount)
  redis.call('HDEL', KEYS[3], id)
  redis.call('ZREM', KEYS[2], id)
end
redis.call('ZREMRANGEBYSCORE', KEYS[5], '-inf', ARGV[8])
if redis.call('HEXISTS', KEYS[1], 'limit') == 0 then return {'not_configured'} end
if redis.call('HGET', KEYS[1], 'status') ~= 'open' then return {'stopped'} end
local amount = tonumber(ARGV[2])
local used = num(KEYS[1], 'spent') + num(KEYS[1], 'uncertain')
  + num(KEYS[1], 'reserved')
if used + amount > num(KEYS[1], 'limit') then return {'budget_used_up'} end
if num(KEYS[4], 'committed') + amount > num(KEYS[1], 'daily_limit') then
  return {'daily_used_up'}
end
if redis.call('ZCARD', KEYS[5]) >= setting('calls_per_hour', ARGV[6]) then
  local oldest = redis.call('ZRANGE', KEYS[5], 0, 0, 'WITHSCORES')
  return {'hourly_limit', oldest[2] or ''}
end
if redis.call('ZCARD', KEYS[2]) >= setting('concurrent_calls', ARGV[7]) then
  return {'busy'}
end
redis.call('HINCRBY', KEYS[1], 'reserved', amount)
redis.call('HINCRBY', KEYS[1], 'calls', 1)
redis.call('ZADD', KEYS[2], ARGV[4], ARGV[1])
redis.call('HSET', KEYS[3], ARGV[1], ARGV[2])
redis.call('HINCRBY', KEYS[4], 'committed', amount)
redis.call('EXPIRE', KEYS[4], ARGV[5])
redis.call('ZADD', KEYS[5], ARGV[3], ARGV[1])
redis.call('EXPIRE', KEYS[5], 7200)
return {'ok'}
"""
)

# KEYS: state, in-flight deadlines, in-flight amounts, the day of the reservation
# ARGV: reservation id, real cost, 'spent' or 'uncertain'
# A finished call frees its place among running calls, but still counts in the hour.
_SETTLE = """
local reserved = redis.call('HGET', KEYS[3], ARGV[1])
if not reserved then return 'unknown' end
reserved = tonumber(reserved)
local cost = tonumber(ARGV[2])
redis.call('HDEL', KEYS[3], ARGV[1])
redis.call('ZREM', KEYS[2], ARGV[1])
redis.call('HINCRBY', KEYS[1], 'reserved', -reserved)
redis.call('HINCRBY', KEYS[1], ARGV[3], cost)
if redis.call('EXISTS', KEYS[4]) == 1 then
  redis.call('HINCRBY', KEYS[4], 'committed', cost - reserved)
end
return 'ok'
"""

# KEYS: state, today, in-flight deadlines, recent calls
# ARGV: now (ms), start of the hour (ms). Reads only: nothing is changed.
_STATUS = """
local window = '(' .. ARGV[2]
local oldest = redis.call('ZRANGEBYSCORE', KEYS[4], window, '+inf', 'WITHSCORES',
  'LIMIT', 0, 1)
return {redis.call('HGETALL', KEYS[1]),
        redis.call('HGET', KEYS[2], 'committed') or '0',
        redis.call('ZCOUNT', KEYS[3], '(' .. ARGV[1], '+inf'),
        redis.call('ZCOUNT', KEYS[4], window, '+inf'),
        oldest[2] or ''}
"""

# KEYS: state. ARGV: budget, daily limit, start time.
# Calls still in flight keep their reservation: they belong to the new period. The
# call limits are kept as they are.
_START = """
local previous = redis.call('HGETALL', KEYS[1])
redis.call('HSET', KEYS[1], 'limit', ARGV[1], 'daily_limit', ARGV[2],
  'spent', 0, 'uncertain', 0, 'calls', 0, 'status', 'open', 'stop_reason', '',
  'started_at', ARGV[3])
redis.call('HINCRBY', KEYS[1], 'period', 1)
return previous
"""

# KEYS: state. ARGV: field, value, ... Changes nothing if no budget was ever set.
_UPDATE = """
if redis.call('HEXISTS', KEYS[1], 'limit') == 0 then return 0 end
for i = 1, #ARGV, 2 do
  redis.call('HSET', KEYS[1], ARGV[i], ARGV[i + 1])
end
return 1
"""


class Availability(StrEnum):
    OPEN = "open"
    UNAVAILABLE = "unavailable"  # Storage down or not set up, or no budget set.
    STOPPED = "stopped"  # Turned off by the owner, or after an account problem.
    BUDGET_USED_UP = "budget_used_up"
    DAILY_USED_UP = "daily_used_up"
    HOURLY_LIMIT = "hourly_limit"  # Too many calls on the whole site this hour.
    BUSY = "busy"  # Too many calls running at the same time.


_STILL_WORKING = "Ruff, the fixes and the demo still work."
VISITOR_MESSAGES = {
    Availability.UNAVAILABLE: (
        f"Claude AI is temporarily unavailable on this site. {_STILL_WORKING}"
    ),
    Availability.STOPPED: (
        f"Claude AI is temporarily unavailable on this site. {_STILL_WORKING}"
    ),
    Availability.BUDGET_USED_UP: (
        "Claude AI is temporarily unavailable: the AI budget of this demo has run "
        f"out. {_STILL_WORKING}"
    ),
    Availability.DAILY_USED_UP: (
        "Claude AI is temporarily unavailable: today's AI budget has run out. It "
        f"should be back tomorrow (UTC). {_STILL_WORKING}"
    ),
    Availability.HOURLY_LIMIT: (
        "Claude AI is temporarily unavailable: this site has reached its limit of AI "
        f"reviews for the last hour. Try again later. {_STILL_WORKING}"
    ),
    Availability.BUSY: (
        "Claude AI is busy with other visitors' reviews right now. Try again in a "
        f"minute or two. {_STILL_WORKING}"
    ),
}


def visitor_message(
    availability: Availability, retry_at: float | None = None, now: float | None = None
) -> str:
    """The message for visitors. With retry_at (epoch seconds), says when to retry.

    retry_at is only given for the hourly limit: it is known exactly, from the time
    of the oldest call of the last hour.
    """
    if availability is not Availability.HOURLY_LIMIT or retry_at is None:
        return VISITOR_MESSAGES[availability]
    now = time.time() if now is None else now
    minutes = max(1, math.ceil((retry_at - now) / 60))
    at = datetime.fromtimestamp(retry_at, UTC).strftime("%H:%M")
    unit = "minute" if minutes == 1 else "minutes"
    return (
        "Claude AI is temporarily unavailable: this site has reached its limit of AI "
        f"reviews for the last hour. Try again after {at} UTC (in about {minutes} "
        f"{unit}). {_STILL_WORKING}"
    )


class BudgetRefused(AiReviewError):
    """The call was not sent: a limit does not allow it, or cannot be checked."""

    def __init__(self, availability: Availability, retry_at: float | None = None):
        super().__init__(visitor_message(availability, retry_at))
        self.availability = availability
        self.retry_at = retry_at


def to_micro(dollars: float) -> int:
    """Dollars to whole millionths, rounded up: a cost is never under-counted."""
    return max(0, -(-round(dollars * MICRO * 1000) // 1000))


def to_dollars(micro: int) -> float:
    return micro / MICRO


@dataclass(frozen=True)
class Reservation:
    reservation_id: str
    amount: int  # Millionths of a dollar.
    day: str


@dataclass(frozen=True)
class BudgetStatus:
    """The budget and call limits as stored. Amounts in millionths of a dollar."""

    availability: Availability
    limit: int = 0
    daily_limit: int = 0
    spent: int = 0
    uncertain: int = 0
    reserved: int = 0
    today: int = 0
    calls: int = 0
    in_flight: int = 0
    period: int = 0
    started_at: str = ""
    stop_reason: str = ""
    calls_per_hour: int = DEFAULT_CALLS_PER_HOUR
    concurrent_calls: int = DEFAULT_CONCURRENT_CALLS
    calls_last_hour: int = 0
    oldest_call_ms: int = 0  # Start of the oldest call of the last hour.

    @property
    def used(self) -> int:
        return self.spent + self.uncertain + self.reserved

    @property
    def remaining(self) -> int:
        """What a new call may still reserve, given both money limits."""
        return max(0, min(self.limit - self.used, self.daily_limit - self.today))

    @property
    def retry_at(self) -> float | None:
        """When the hourly limit allows a call again (epoch seconds), if reached."""
        if self.calls_last_hour < self.calls_per_hour or not self.oldest_call_ms:
            return None
        return self.oldest_call_ms / 1000 + HOUR_SECONDS

    def refusal_for(self, cost_usd: float) -> Availability | None:
        """Why a call of this maximum cost would be refused now, or None.

        Checked in the same order as the reservation script.
        """
        if self.availability is not Availability.OPEN:
            return self.availability
        amount = to_micro(cost_usd)
        if self.used + amount > self.limit:
            return Availability.BUDGET_USED_UP
        if self.today + amount > self.daily_limit:
            return Availability.DAILY_USED_UP
        if self.calls_last_hour >= self.calls_per_hour:
            return Availability.HOURLY_LIMIT
        if self.in_flight >= self.concurrent_calls:
            return Availability.BUSY
        return None

    def message_for(self, cost_usd: float) -> str | None:
        """The visitor message for refusal_for, or None if the call is allowed."""
        availability = self.refusal_for(cost_usd)
        if availability is None:
            return None
        return visitor_message(availability, self.retry_at)


UNAVAILABLE_STATUS = BudgetStatus(Availability.UNAVAILABLE)


class SharedBudget:
    """The budget in Redis. Every method is safe to call from many threads."""

    def __init__(self, store: RedisRest, clock=time.time, prefix: str = KEY_PREFIX):
        self._store = store
        self._clock = clock
        self._state = f"{prefix}:state"
        self._deadlines = f"{prefix}:in-flight"
        self._amounts = f"{prefix}:in-flight-amounts"
        self._recent = f"{prefix}:recent-calls"
        self._history = f"{prefix}:history"
        self._day_prefix = f"{prefix}:day:"

    # --- Used by the app ---

    def reserve(self, cost_usd: float) -> Reservation:
        """Check every limit and reserve the call at once, or raise BudgetRefused."""
        amount = to_micro(cost_usd)
        if amount <= 0:
            raise BudgetRefused(Availability.UNAVAILABLE)
        now = self._clock()
        now_ms = int(now * 1000)
        reservation = Reservation(uuid.uuid4().hex, amount, self._day(now))
        try:
            answer = self._eval(
                _RESERVE,
                [
                    self._state,
                    self._deadlines,
                    self._amounts,
                    self._day_key(reservation),
                    self._recent,
                ],
                [
                    reservation.reservation_id,
                    amount,
                    now_ms,
                    int((now + RESERVATION_SECONDS) * 1000),
                    _DAY_KEY_SECONDS,
                    DEFAULT_CALLS_PER_HOUR,
                    DEFAULT_CONCURRENT_CALLS,
                    now_ms - HOUR_SECONDS * 1000,
                ],
            )
            if not isinstance(answer, list) or not answer:
                raise StoreError("The storage gave an answer that is not valid.")
            outcome = _text(answer[0])
        except StoreError:
            outcome, answer = "error", []
        finally:
            STATUS_CACHE.clear()
        if outcome == "ok":
            return reservation
        retry_at = None
        if outcome == "hourly_limit":
            retry_at = _retry_time(answer[1] if len(answer) > 1 else "")
        raise BudgetRefused(_REFUSALS.get(outcome, Availability.UNAVAILABLE), retry_at)

    def settle(self, reservation: Reservation, cost_usd: float, *, certain: bool):
        """Replace a reservation by the real cost. Never raises.

        If this fails, the reservation expires later and its maximum stays counted.
        """
        try:
            self._eval(
                _SETTLE,
                [
                    self._state,
                    self._deadlines,
                    self._amounts,
                    self._day_key(reservation),
                ],
                [
                    reservation.reservation_id,
                    to_micro(cost_usd),
                    "spent" if certain else "uncertain",
                ],
            )
        except StoreError:
            pass
        finally:
            STATUS_CACHE.clear()

    def status(self) -> BudgetStatus:
        """Read the budget. Never raises: a failure means paid AI is unavailable."""
        try:
            return self.checked_status()
        except (StoreError, TypeError, ValueError, IndexError):
            return UNAVAILABLE_STATUS

    def stop(self, reason: str) -> bool:
        """Turn paid AI off for everyone until the owner turns it back on."""
        return self._update("status", "stopped", "stop_reason", reason[:200])

    # --- Used by the owner, through budget_admin. These raise StoreError. ---

    def start_period(self, budget_usd: float, daily_usd: float) -> dict:
        """Start a new budget period, and return the previous one."""
        _check_limits(budget_usd, daily_usd)
        started = datetime.fromtimestamp(self._clock(), UTC).isoformat(
            timespec="seconds"
        )
        answer = self._eval(
            _START,
            [self._state],
            [to_micro(budget_usd), to_micro(daily_usd), started],
        )
        previous = _pairs(answer)
        if previous:
            record = json.dumps(previous, sort_keys=True)
            self._store.command("LPUSH", self._history, record)
            self._store.command("LTRIM", self._history, 0, _HISTORY_LENGTH - 1)
        STATUS_CACHE.clear()
        return previous

    def set_daily_limit(self, daily_usd: float) -> bool:
        current = self.status()
        _check_limits(to_dollars(current.limit) or MAX_BUDGET_USD, daily_usd)
        return self._update("daily_limit", to_micro(daily_usd))

    def set_call_limits(
        self, per_hour: int | None = None, concurrent: int | None = None
    ) -> bool:
        """Change the call limits only: the budget and spending are not touched."""
        pairs = []
        if per_hour is not None:
            if not 1 <= per_hour <= MAX_CALLS_PER_HOUR:
                raise ValueError(
                    f"Calls per hour must be between 1 and {MAX_CALLS_PER_HOUR}."
                )
            pairs += ["calls_per_hour", per_hour]
        if concurrent is not None:
            if not 1 <= concurrent <= MAX_CONCURRENT_CALLS:
                raise ValueError(
                    "Calls at the same time must be between 1 and "
                    f"{MAX_CONCURRENT_CALLS}."
                )
            pairs += ["concurrent_calls", concurrent]
        if not pairs:
            raise ValueError("Give --per-hour, --concurrent or both.")
        return self._update(*pairs)

    def resume(self) -> bool:
        return self._update("status", "open", "stop_reason", "")

    def checked_status(self) -> BudgetStatus:
        """Like status(), but raises StoreError: the owner needs to know why."""
        now = self._clock()
        now_ms = int(now * 1000)
        answer = self._eval(
            _STATUS,
            [self._state, self._day_key_for(now), self._deadlines, self._recent],
            [now_ms, now_ms - HOUR_SECONDS * 1000],
        )
        return _parse_status(answer)

    # --- Details ---

    def _update(self, *pairs) -> bool:
        try:
            changed = self._eval(_UPDATE, [self._state], list(pairs))
        except StoreError:
            return False
        finally:
            STATUS_CACHE.clear()
        return changed == 1

    def _eval(self, script: str, keys: list[str], args: list) -> object:
        return self._store.command("EVAL", script, len(keys), *keys, *args)

    def _day(self, now: float) -> str:
        return datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%d")

    def _day_key(self, reservation: Reservation) -> str:
        return self._day_prefix + reservation.day

    def _day_key_for(self, now: float) -> str:
        return self._day_prefix + self._day(now)


_REFUSALS = {
    "not_configured": Availability.UNAVAILABLE,
    "stopped": Availability.STOPPED,
    "budget_used_up": Availability.BUDGET_USED_UP,
    "daily_used_up": Availability.DAILY_USED_UP,
    "hourly_limit": Availability.HOURLY_LIMIT,
    "busy": Availability.BUSY,
}


def _retry_time(oldest_ms) -> float | None:
    """When the oldest call of the hour leaves it, or None if that is not known."""
    try:
        return int(float(_text(oldest_ms))) / 1000 + HOUR_SECONDS
    except (TypeError, ValueError):
        return None


def _check_limits(budget_usd: float, daily_usd: float) -> None:
    if not 0 < budget_usd <= MAX_BUDGET_USD:
        raise ValueError(
            f"The budget must be more than $0 and at most ${MAX_BUDGET_USD}."
        )
    if not 0 < daily_usd <= budget_usd:
        raise ValueError("The daily limit must be more than $0 and at most the budget.")


def _text(value) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def _pairs(flat) -> dict[str, str]:
    """A Redis HGETALL answer, [name, value, name, value...], as a dict."""
    if not isinstance(flat, list) or len(flat) % 2:
        raise ValueError("Not a list of pairs.")
    return {_text(flat[i]): _text(flat[i + 1]) for i in range(0, len(flat), 2)}


def _parse_status(answer) -> BudgetStatus:
    state = _pairs(answer[0])
    today, in_flight = int(_text(answer[1])), int(answer[2])
    calls_last_hour = int(answer[3])
    oldest = _text(answer[4])
    oldest_call_ms = int(float(oldest)) if oldest else 0
    if "limit" not in state:
        return BudgetStatus(Availability.UNAVAILABLE, today=today, in_flight=in_flight)

    def number(name: str, default: int = 0) -> int:
        return int(state.get(name, default))

    status = BudgetStatus(
        Availability.OPEN,
        limit=number("limit"),
        daily_limit=number("daily_limit"),
        spent=number("spent"),
        uncertain=number("uncertain"),
        reserved=number("reserved"),
        today=today,
        calls=number("calls"),
        in_flight=in_flight,
        period=number("period"),
        started_at=state.get("started_at", ""),
        stop_reason=state.get("stop_reason", ""),
        calls_per_hour=number("calls_per_hour", DEFAULT_CALLS_PER_HOUR),
        concurrent_calls=number("concurrent_calls", DEFAULT_CONCURRENT_CALLS),
        calls_last_hour=calls_last_hour,
        oldest_call_ms=oldest_call_ms,
    )
    if state.get("status") != "open":
        availability = Availability.STOPPED
    elif status.used >= status.limit:
        availability = Availability.BUDGET_USED_UP
    elif today >= status.daily_limit:
        availability = Availability.DAILY_USED_UP
    else:
        return status
    return replace(status, availability=availability)


class _StatusCache:
    """Keeps the last status a few seconds, so each page rerun does not call Redis.

    Only used to draw the page: a call is always checked by the atomic reservation.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._value: tuple[float, BudgetStatus] | None = None

    def get(self, budget: SharedBudget) -> BudgetStatus:
        now = time.monotonic()
        with self._lock:
            if self._value and now - self._value[0] < STATUS_CACHE_SECONDS:
                return self._value[1]
        status = budget.status()
        with self._lock:
            self._value = (now, status)
        return status

    def clear(self) -> None:
        with self._lock:
            self._value = None


STATUS_CACHE = _StatusCache()


def public_budget() -> SharedBudget | None:
    """The budget of this public site, or None if the storage is not set up."""
    url = server_setting(STORE_URL_NAME)
    token = server_setting(STORE_TOKEN_NAME)
    if url is None or token is None:
        return None
    try:
        return SharedBudget(RedisRest(url, token))
    except StoreError:
        return None


def public_status() -> BudgetStatus:
    budget = public_budget()
    return UNAVAILABLE_STATUS if budget is None else STATUS_CACHE.get(budget)
