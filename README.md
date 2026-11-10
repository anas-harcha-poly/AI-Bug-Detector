# AI Bug Detector

**Find bugs in Python code with static analysis and Claude AI.**

**[Try the live app → ai-bug-detector-anas.streamlit.app](https://ai-bug-detector-anas.streamlit.app/)**

No account and no API key needed.

AI Bug Detector is a Python web application built with Streamlit. Upload a Python file:
[Ruff](https://docs.astral.sh/ruff/) reports errors and risky code, Claude AI explains
the issues and looks for possible logic bugs, and you choose which suggested fixes to
apply to a downloadable copy of your file. Your code is never run on the public site.

## Features

- **Static analysis with Ruff.** Errors, likely bugs and style issues, each with a
  severity, a line, a description and its Ruff rule, most severe first.
- **Real Claude AI review.** Claude Sonnet 5.5 explains each Ruff issue in plain words
  and looks for logic bugs that static analysis cannot see, such as a wrong condition
  or a missing edge case. Answers are validated before they are shown.
- **Interactive fixes.** Fixes come from Ruff and from Claude. Nothing is applied
  automatically: you accept each fix you want, and conflicting fixes are detected.
- **Preview of the changes.** See the changes before using them. Every result is
  checked to still be valid Python.
- **Download the fixed code.** Get a corrected copy of your file. The original is never
  changed.
- **Demo mode.** Try every feature on a sample file, for free and without sending
  anything. The demo's AI answers are hand-written examples, clearly labeled as such.

Logic bugs reported by Claude are **hypotheses**: the code is read, never executed, so
they are not verified. Suggested fixes, from Ruff's unsafe fixes or from Claude, can be
wrong. Review each one before using it.

## Public site and local version

| Feature | Public site | Local version |
|---|:---:|:---:|
| Ruff analysis | ✅ | ✅ |
| Claude AI review | ✅ No key needed | ✅ With your own API key |
| Interactive fixes, preview and download | ✅ | ✅ |
| Demo mode | ✅ | ✅ |
| Models | Sonnet 5.5, Haiku 5.5 | Sonnet 5.5, Haiku 5.5, Opus 5.5 |
| Running your pytest tests in Docker | ❌ | ✅ Optional |

On the public site, **visitors never need an Anthropic API key**: the site's key is kept
on the server, and is never sent to the browser or shown in any message.

## Protections on the public site

- **Consent first.** Nothing is sent to Anthropic until the visitor ticks a consent box
  for this file and model, then clicks a button. The file name is never sent.
- **No code execution.** Uploaded code is only read, never imported or run. Docker test
  runs are turned off on the public site, whatever the configuration says.
- **Shared budget in Upstash Redis.** Every call first reserves its maximum cost in a
  budget shared by all visitors and all servers. The check is a single atomic Lua
  script, so simultaneous requests cannot get around it. The real cost is recorded
  afterwards; when billing is uncertain, the maximum stays counted.
- **Spending caps.** A global budget that never resets by itself, and a daily cap. The
  spend limit of the Anthropic account remains the final safeguard.
- **Call limits.** A cap on calls per hour for the whole site and on calls running at the
  same time, with a message telling visitors when to try again. A small per-session cap
  prevents accidental overuse.
- **Fail closed.** If the budget database cannot be reached, paid AI is blocked. Ruff,
  the fixes and the demo keep working.
- **Untrusted content.** Code is sent to Claude as data, and every answer is validated
  and shown as plain text, never as Markdown or HTML. No AI model can fully guarantee
  that instructions hidden in code are ignored.

Anthropic may keep API data for up to 30 days and does not use it for training by
default ([details](https://platform.claude.com/docs/en/manage-claude/api-and-data-retention)).
Do not upload code that contains passwords, keys or other secrets.

## Tech stack

Python 3.12–3.14 · Streamlit · Ruff · Anthropic Python SDK (structured outputs) ·
Upstash Redis (REST API, Lua scripts) · Docker (local test sandbox) · pytest

## Run it locally

Requirements: Python 3.12, 3.13 or 3.14. The commands below are for Windows and call
the virtual environment's interpreter directly, so it does not need to be activated.

```powershell
py -3.14 -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\python.exe -m streamlit run app.py
```

Without any setting, the app runs Ruff, the fixes and the demo, and sends nothing to
any AI service.

### Claude AI with your own key

```powershell
.venv\Scripts\python.exe -m pip install -e ".[dev,ai]"
$env:AI_BUG_DETECTOR_ENABLE_LIVE_AI = "1"
.venv\Scripts\python.exe -m streamlit run app.py
```

Paste your Anthropic API key in the page. It stays in memory for the session and is
never written to a file. Before each call, the page shows its estimated maximum cost,
and after it, the real cost. A spending limit for the running app ($1.00 by default)
can be changed with `$env:AI_BUG_DETECTOR_MAX_SPEND_USD = "2"`.

To check that the models are available for your key, and measure the size of the
requests for a file, without creating any billed message:

```powershell
.venv\Scripts\python.exe -m ai_bug_detector.claude_check example.py
```

### Running your tests in Docker (local only)

You can run a pytest test file against your code in a temporary, isolated container:
no network, read-only file system, an unprivileged user, and limits on memory, CPU,
processes and time (30 seconds). The container is always removed afterwards. Nothing
runs until you tick a consent box. No isolation is perfect: only run code you trust.

Requirements: Docker Desktop with Linux containers.

```powershell
docker build -t ai-bug-detector-sandbox:1 sandbox
$env:AI_BUG_DETECTOR_ENABLE_SANDBOX = "1"
.venv\Scripts\python.exe -m streamlit run app.py
```

## Deploying the public version

Public mode is turned on explicitly. Set these values as Streamlit secrets (or
environment variables) on the server, never in the repository:

```toml
AI_BUG_DETECTOR_MODE = "public"
ANTHROPIC_API_KEY = "..."
UPSTASH_REDIS_REST_URL = "https://..."
UPSTASH_REDIS_REST_TOKEN = "..."
```

Also set `STREAMLIT_CLIENT_SHOW_ERROR_DETAILS=none`, and use a key from a dedicated
workspace with a spend limit in the Claude Console. Any other value of
`AI_BUG_DETECTOR_MODE` than `local` or `public` turns paid AI and Docker off. Without
the database, or without a budget set in it, paid AI stays off.

The owner manages the budget and limits from their own computer. Changes apply at once,
without redeploying:

```powershell
.venv\Scripts\python.exe -m ai_bug_detector.budget_admin status
.venv\Scripts\python.exe -m ai_bug_detector.budget_admin start --budget 10 --daily 1
.venv\Scripts\python.exe -m ai_bug_detector.budget_admin daily --usd 2
.venv\Scripts\python.exe -m ai_bug_detector.budget_admin limits --per-hour 10 --concurrent 2
.venv\Scripts\python.exe -m ai_bug_detector.budget_admin stop
.venv\Scripts\python.exe -m ai_bug_detector.budget_admin resume
```

The database URL and token are read from `UPSTASH_REDIS_REST_URL` and
`UPSTASH_REDIS_REST_TOKEN`, or typed without being shown. `start` begins a new budget
period at $0 and should only be run after adding credits: it asks for confirmation and
keeps the previous period in a history. If Anthropic refuses the key or the account
runs out of credit, paid AI stops for everyone until `resume` is run.

**Known limitation:** the limits are shared, not per visitor. One person can still use
up the calls of the hour or the daily cap, which blocks everyone for a while; the
money spent stays capped. Sessions, cookies and IP addresses cannot identify a visitor
reliably, so fair per-visitor quotas would require sign-in.

## How it works

**Ruff.** The code is passed to Ruff through standard input (rules `E`, `F`, `I`, `B`),
with a 30-second timeout. The project assigns its own severity:

| Severity | Meaning | Examples |
|---|---|---|
| `ERROR` | The code is invalid or will probably crash | Syntax error, undefined name (`F821`) |
| `WARNING` | Possible bug or suspicious code | Mutable default argument (`B006`), unused import (`F401`) |
| `INFO` | Style and readability | Unsorted imports (`I001`), comparison to `None` (`E711`) |

A failed analysis raises `AnalysisError`, so it can never be mistaken for code without
issues. Files stay in memory and are never saved to disk.

**Claude.** Each click makes exactly one request, never retried. A review accepts up to
30 KB of code and 20 Ruff issues, and asks for a JSON answer matching a schema. Line
numbers, rule codes and fix ranges are checked against the real file before anything
is shown.

**Fixes.** A fix replaces a range of lines of the original file. Fixes are checked
against a hash of the original, conflicts are rejected, and the result must parse as
valid Python. Fixes are parsed, never executed.

**Limitations.** Only UTF-8 files are supported. Static analysis cannot catch every bug,
and AI suggestions can be wrong.

## Development

```powershell
.venv\Scripts\python.exe -m pytest
.venv\Scripts\python.exe -m ruff check .
```

Tests never call Anthropic or Upstash: both are simulated, and Redis scripts run on
fakeredis. They never read `.streamlit/secrets.toml` and cannot open network
connections outside the computer (see `tests/conftest.py`). Tests that need real Docker
are marked `docker` and skipped by default; run them with `-m docker`.

| Python | Status |
|---|---|
| 3.12 | Tests pass |
| 3.13 | Dependencies resolve, tests not run |
| 3.14 | Tests pass |

## Project structure

```text
├── app.py                  Streamlit entry point
├── pyproject.toml          Metadata, dependencies and tool settings
├── .streamlit/config.toml  Light and dark theme
├── sandbox/                Docker image for local test runs
├── src/ai_bug_detector/
│   ├── ruff_analyzer.py    Runs Ruff and converts its findings
│   ├── models.py           Shared data: issues, severities, Ruff fixes
│   ├── report.py           Sorting and table rows
│   ├── ai_prompts.py       What is sent to Claude, and size limits
│   ├── ai_models.py        Answer schemas and validation
│   ├── ai_review.py        Demo and Claude reviewers, costs, API errors
│   ├── ai_panel.py         AI section of the page, consent, demo
│   ├── fixes.py            Applying accepted fixes safely
│   ├── fix_panel.py        Fix selection, preview and download
│   ├── deployment.py       Local or public mode, server-side secrets
│   ├── shared_budget.py    Shared budget and call limits in Redis
│   ├── redis_rest.py       Minimal HTTPS client for the Upstash REST API
│   ├── budget_admin.py     Owner's budget command
│   ├── claude_check.py     Free check of the API setup
│   ├── docker_sandbox.py   Isolated test runs (local only)
│   ├── sandbox_panel.py    Test run section of the page
│   └── demo/               Sample file and hand-written demo answers
└── tests/                  Automated tests
```
