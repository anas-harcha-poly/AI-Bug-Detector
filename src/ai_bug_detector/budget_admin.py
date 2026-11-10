"""Manage the shared AI budget of the public site, from the owner's computer.

    python -m ai_bug_detector.budget_admin status
    python -m ai_bug_detector.budget_admin start --budget 4 --daily 1
    python -m ai_bug_detector.budget_admin daily --usd 0.5
    python -m ai_bug_detector.budget_admin limits --per-hour 10 --concurrent 2
    python -m ai_bug_detector.budget_admin stop
    python -m ai_bug_detector.budget_admin resume

The site reads the budget from Redis at each call, so a change applies at once,
without changing the code or deploying again. The database URL and token are read
from UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN, or typed without being
shown. They are never printed. This makes no call to Anthropic.
"""

import argparse
import getpass
import os
import sys

from ai_bug_detector.deployment import STORE_TOKEN_NAME, STORE_URL_NAME
from ai_bug_detector.redis_rest import RedisRest, StoreError
from ai_bug_detector.shared_budget import (
    Availability,
    BudgetStatus,
    SharedBudget,
    to_dollars,
)

START_WARNING = """\
Starting a new period sets the amount spent back to $0.
Only do this after you have really added credits, and checked the balance and the
spend limit of the workspace in the Claude Console. Calls in progress stay counted.
It also turns Claude AI back on if it was stopped.
"""
RESUME_WARNING = """\
Before turning Claude AI back on, check the credits, the spend limit and the key in
the Claude Console.
"""


def _money(micro: int) -> str:
    return f"${to_dollars(micro):.4f}"


def describe(status: BudgetStatus) -> str:
    if status.availability is Availability.UNAVAILABLE and not status.limit:
        return "No budget has been set yet: paid AI is off. Use the start command."
    lines = [
        f"State: {status.availability.value}",
        f"Period {status.period}, started {status.started_at or 'unknown'}",
        f"Budget: {_money(status.limit)}",
        f"  Spent (billed): {_money(status.spent)}",
        f"  Counted at maximum (billing unknown): {_money(status.uncertain)}",
        f"  Reserved by calls in progress: {_money(status.reserved)} "
        f"({status.in_flight} call(s))",
        f"  Left: {_money(max(0, status.limit - status.used))}",
        f"Today (UTC): {_money(status.today)} of {_money(status.daily_limit)}",
        f"Calls this period: {status.calls}",
        f"Calls in the last hour (whole site): {status.calls_last_hour} of "
        f"{status.calls_per_hour}",
        f"Calls running now: {status.in_flight} of {status.concurrent_calls}",
    ]
    if status.stop_reason:
        lines.append(f"Stopped because: {status.stop_reason}")
    return "\n".join(lines)


def connect() -> SharedBudget:
    url = os.environ.get(STORE_URL_NAME) or getpass.getpass("Database REST URL: ")
    token = os.environ.get(STORE_TOKEN_NAME) or getpass.getpass("Database token: ")
    return SharedBudget(RedisRest(url, token))


def main(argv=None, *, budget: SharedBudget | None = None, ask=input) -> int:
    parser = argparse.ArgumentParser(prog="python -m ai_bug_detector.budget_admin")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="Show the budget.")
    start = commands.add_parser("start", help="Start a new budget period.")
    start.add_argument("--budget", type=float, required=True, help="In dollars.")
    start.add_argument("--daily", type=float, required=True, help="In dollars.")
    daily = commands.add_parser("daily", help="Change the daily limit only.")
    daily.add_argument("--usd", type=float, required=True)
    limits = commands.add_parser(
        "limits", help="Change the call limits only (not the budget)."
    )
    limits.add_argument("--per-hour", type=int, help="Calls per hour, whole site.")
    limits.add_argument("--concurrent", type=int, help="Calls at the same time.")
    commands.add_parser("stop", help="Turn paid AI off for every visitor.")
    commands.add_parser("resume", help="Turn paid AI back on.")
    args = parser.parse_args(argv)

    try:
        budget = budget or connect()
        return _run(args, budget, ask)
    except StoreError as error:
        print(f"Error: {error}", file=sys.stderr)  # Never contains the URL or token.
        return 1
    except ValueError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


def _run(args, budget: SharedBudget, ask) -> int:
    if args.command == "status":
        print(describe(budget.checked_status()))
        return 0

    if args.command == "stop":
        if not budget.stop("Stopped by the owner."):
            print("No budget has been set yet: paid AI is already off.")
            return 1
        print("Paid AI is now off for every visitor.")
        return 0

    if args.command == "daily":
        if not budget.set_daily_limit(args.usd):
            print("Not changed: no budget has been set yet, or storage failed.")
            return 1
        print(f"Daily limit set to ${args.usd:.2f}.")
        return 0

    if args.command == "limits":
        if not budget.set_call_limits(args.per_hour, args.concurrent):
            print("Not changed: no budget has been set yet, or storage failed.")
            return 1
        print(describe(budget.checked_status()))
        return 0

    if args.command == "resume":
        print(RESUME_WARNING)
        if ask("Type RESUME to turn Claude AI back on: ").strip() != "RESUME":
            print("Nothing was changed.")
            return 1
        if not budget.resume():
            print("Not changed: no budget has been set yet, or storage failed.")
            return 1
        print(describe(budget.checked_status()))
        return 0

    # start
    print("Current state:")
    print(describe(budget.checked_status()))
    print()
    print(START_WARNING)
    typed = ask(f"Type the new budget again ({args.budget:g}) to confirm: ").strip()
    try:
        confirmed = float(typed) == args.budget
    except ValueError:
        confirmed = False
    if not confirmed:
        print("Nothing was changed.")
        return 1
    budget.start_period(args.budget, args.daily)
    print("New period started. The previous one was saved in the history.")
    print(describe(budget.checked_status()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
