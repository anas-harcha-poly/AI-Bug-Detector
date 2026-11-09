"""Free checks to run before the first real call to Claude. Nothing here is billed.

Run it from the project folder, optionally with the small file you plan to test:
    .venv\\Scripts\\python.exe -m ai_bug_detector.claude_check [file.py]

1. For each model the app offers, the Models API confirms that it exists for this key
   and accepts the app's thinking setting. Free.
2. Token counting measures the exact input of the two requests the app would send
   for the file, and so their maximum cost. Free, but it sends the file's code to
   Anthropic, so the tool asks first.

The key is typed without being shown, used for these requests only, and never
printed or saved. No message is ever created, so nothing can be billed.
"""

import argparse
import getpass
import sys
from collections.abc import Callable
from pathlib import Path

from ai_bug_detector.ai_models import EXPLANATION_SCHEMA, LOGIC_BUG_SCHEMA, Usage
from ai_bug_detector.ai_prompts import (
    EXPLANATION_SYSTEM_PROMPT,
    LOGIC_BUG_SYSTEM_PROMPT,
    MAX_SOURCE_BYTES,
    build_explanation_message,
    build_logic_bug_message,
    select_issues,
)
from ai_bug_detector.ai_review import (
    _API_URL,
    AI_MODELS,
    MAX_OUTPUT_TOKENS,
    AiModel,
    actual_cost,
    build_request,
    read_demo_source,
)
from ai_bug_detector.ruff_analyzer import AnalysisError, analyze_bytes

_TIMEOUT_SECONDS = 30


def main(
    argv: list[str] | None = None,
    *,
    read_key: Callable[[str], str] = getpass.getpass,
    ask: Callable[[str], str] = input,
    out: Callable[[str], None] = print,
) -> int:
    """Run the checks. Return 0 when everything is ready, 1 otherwise."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("file", nargs="?", help="Python file to measure (UTF-8).")
    args = parser.parse_args(argv)

    try:
        name, source, issues = _read_file(args.file)
    except ValueError as error:
        out(f"Cannot use this file: {error}")
        return 1

    key = read_key("Anthropic API key (hidden while you type): ").strip()
    if not key:
        out("No key was given. Nothing was sent.")
        return 1
    try:
        import anthropic
    except ImportError:
        out("The Anthropic SDK is not installed. See the README.")
        return 1

    client = anthropic.Anthropic(
        api_key=key,
        base_url=_API_URL,  # Fixed: ANTHROPIC_BASE_URL cannot redirect the key.
        timeout=_TIMEOUT_SECONDS,
        max_retries=0,
    )

    out("Step 1: models (free)")
    problems = 0
    for model in AI_MODELS:
        try:
            info = client.models.retrieve(model.model_id)
        except anthropic.AuthenticationError:
            out("The API key was rejected. Check it in the Claude Console.")
            return 1
        except anthropic.NotFoundError:
            out(f"  {model.model_id}: NOT FOUND for this key")
            problems += 1
            continue
        except anthropic.APIError:
            out(f"  {model.model_id}: could not be checked (API or network error)")
            problems += 1
            continue
        note, supported = _thinking_note(info, model)
        out(f"  {model.model_id}: available{note}")
        problems += not supported

    out("")
    out(f"Step 2: token counting for {name} (free)")
    answer = ask(
        f"Send the code of {name} to Anthropic to count its tokens? Nothing is "
        "generated or billed. [y/N] "
    )
    if answer.strip().lower() not in ("y", "yes"):
        out("  Skipped: nothing was sent.")
    else:
        try:
            _count(client, source, issues, out)
        except anthropic.APIError:
            out("  Token counting failed (API or network error).")
            problems += 1

    out("")
    out("No message was created, so nothing was billed.")
    out("Ready for a first real test." if not problems else "Fix the problems above.")
    return 0 if not problems else 1


def _read_file(path: str | None) -> tuple[str, str, list]:
    if path is None:
        content, name = read_demo_source(), "the demo sample file"
    else:
        try:
            content, name = Path(path).read_bytes(), Path(path).name
        except OSError as error:
            raise ValueError(f"cannot read it ({error.strerror}).") from None
    if len(content) > MAX_SOURCE_BYTES:
        raise ValueError(f"it is larger than {MAX_SOURCE_BYTES // 1024} KB.")
    try:
        source = content.decode("utf-8-sig")
        issues = select_issues(analyze_bytes(content))
    except (UnicodeDecodeError, AnalysisError):
        raise ValueError("it is not valid UTF-8, or Ruff could not read it.") from None
    return name, source, issues


def _thinking_note(info, model: AiModel) -> tuple[str, bool]:
    """Check that the model accepts the thinking setting the app sends."""
    if model.thinking is None:
        return "", True
    setting = model.thinking["type"]
    try:
        types = info.to_dict()["capabilities"]["thinking"]["types"]
        supported = types[setting]["supported"]
    except (AttributeError, KeyError, TypeError):
        return f", thinking '{setting}' not reported", True
    if supported:
        return f", accepts thinking '{setting}'", True
    return f", does NOT accept thinking '{setting}'", False


def _count(client, source: str, issues: list, out: Callable[[str], None]) -> None:
    requests = (
        (
            "explain issues",
            EXPLANATION_SYSTEM_PROMPT,
            build_explanation_message(source, issues),
            EXPLANATION_SCHEMA,
            "low",
        ),
        (
            "logic bugs",
            LOGIC_BUG_SYSTEM_PROMPT,
            build_logic_bug_message(source),
            LOGIC_BUG_SCHEMA,
            "medium",
        ),
    )
    for model in AI_MODELS:
        for action, system, message, schema, effort in requests:
            request = build_request(model.model_id, system, message, schema, effort)
            tokens = client.messages.count_tokens(**request).input_tokens
            most = actual_cost(Usage(tokens, MAX_OUTPUT_TOKENS), model.model_id)
            out(
                f"  {model.model_id}, {action}: {tokens} input tokens, "
                f"at most ${most:.4f}"
            )


if __name__ == "__main__":
    sys.exit(main())
