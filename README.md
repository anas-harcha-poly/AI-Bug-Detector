# AI Bug Detector

AI Python bug detection and code analysis tool.

> **Project status:** early development. You can upload a Python file in the web
> interface, see the issues found by Ruff and try a demo of the AI review. The real AI
> review is optional and has only been tested with simulated API responses so far.

## Features

- Upload one Python file of up to 5 MB and analyze it with one click.
- See each issue's severity, line, description and Ruff rule, most severe first.
- Get a clear message when a file has no issues, is empty or cannot be analyzed.
- Try the AI review for free on a sample file, with no API key.
- Optionally, when running locally with your own API key, ask Claude to explain Ruff
  issues and look for possible logic bugs.

## Planned features

1. Isolated test execution for unknown files with Docker.

## Security

This project never executes unknown user code outside an isolated environment.
Until Docker isolation exists, user code is not executed at all.

## Requirements

Python 3.12, 3.13 or 3.14.

| Python | Status |
|---|---|
| 3.12 | Tests pass |
| 3.13 | Dependencies resolve, tests not run |
| 3.14 | Tests pass |

## Installation (Windows)

The commands call the virtual environment's interpreter directly, so the
environment does not need to be activated.

```powershell
py -3.14 -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

## Usage

Start the application, then open the address shown in the terminal:

```powershell
.venv\Scripts\python.exe -m streamlit run app.py
```

Analyze a UTF-8 Python file from the command line:

```powershell
.venv\Scripts\python.exe -c "from ai_bug_detector.ruff_analyzer import analyze_file; print(*analyze_file('example.py'), sep='\n')"
```

## How the analysis works

The code is sent to [Ruff](https://docs.astral.sh/ruff/) through standard input. Ruff
only reads it, and returns its findings as JSON. The project converts them into `Issue`
objects with a rule code, a message, a severity and a location. No API key is required.

Ruff reports almost every rule at the same severity, so the project assigns its own:

| Severity | Meaning | Examples |
|---|---|---|
| `ERROR` | The code is invalid or will probably crash | Syntax error, undefined name (`F821`) |
| `WARNING` | Possible bug or suspicious code | Mutable default argument (`B006`), unused import (`F401`) |
| `INFO` | Style and readability | Unsorted imports (`I001`), comparison to `None` (`E711`) |

The analyzer raises `AnalysisError` when an analysis cannot be completed, so a failure
can never be mistaken for code without issues. Uploaded files stay in memory and are
never saved to disk, imported or run.

Known limitations: only UTF-8 files are supported, and static analysis cannot catch every
bug, such as logic errors. An analysis stops after 30 seconds, which can happen with
unusual files such as a single very long comment line.

## Development

```powershell
.venv\Scripts\python.exe -m pytest
.venv\Scripts\python.exe -m ruff check .
```

## Optional AI review

Everything above works without an API key or the Anthropic SDK. The AI review has three
levels, and each one needs a deliberate step:

| Level | Requirements | What happens |
|---|---|---|
| Ruff | None | Local analysis. Nothing leaves your computer |
| Demo | None | Real Ruff results on a sample file, with **illustrative examples written by hand**. These are not real responses from Claude, and no API is called |
| Real AI review | SDK, server setting, your own API key, your consent | Your code is sent to the Anthropic API |

To turn on the real AI review on your own computer:

```powershell
.venv\Scripts\python.exe -m pip install -e ".[dev,ai]"
$env:AI_BUG_DETECTOR_ENABLE_LIVE_AI = "1"
.venv\Scripts\python.exe -m streamlit run app.py
```

This variable only works in local mode. On a public site, use the public mode below
instead: visitors must never type a key into a page hosted by someone else.

### Public mode

In public mode, visitors use the real AI review without an API key: it is paid with the
site owner's key, which only the server reads. Turn it on with these settings, as
Streamlit secrets (`.streamlit/secrets.toml` on the server, or the host's secrets page)
or as environment variables:

```toml
AI_BUG_DETECTOR_MODE = "public"
ANTHROPIC_API_KEY = "your key, only on the server"
UPSTASH_REDIS_REST_URL = "https://... (the shared budget database)"
UPSTASH_REDIS_REST_TOKEN = "its token, only on the server"
```

What changes in public mode:

- There is no API key field. The key is read on the server at the moment of a call, and
  is never put in the page, the session or an error message.
- Visitors still have to accept, for each file and model, that their code is sent to
  Anthropic before any button works.
- Claude Sonnet 5.5 is the default and Haiku 5.5 is offered. Opus 5.5 is not offered, and
  the server refuses it even if a request asks for it.
- Running tests with Docker is never available, whatever `AI_BUG_DETECTOR_ENABLE_SANDBOX`
  says, and no visitor code is run.
- Each call is paid from a shared budget (see below). When the budget, today's limit,
  the key, the SDK or the database is missing, Claude AI stays visible but its buttons
  are disabled, with a message. Ruff, the fixes and the demo always keep working.
- If Anthropic refuses the key, or the credits or the spend limit run out, Claude AI is
  stopped for everyone until the owner turns it back on.
- Any other value than `local` or `public` is treated as a configuration error: paid AI
  and Docker stay off.

Also hide error details from visitors with `STREAMLIT_CLIENT_SHOW_ERROR_DETAILS=none`,
and use a key from a dedicated workspace with a spend limit in the Claude Console.

### Shared AI budget (public mode)

The budget is kept in a Redis database reached over HTTPS, in the format of the Upstash
REST API. It is shared by every visitor and every copy of the app, and survives
restarts. Amounts are stored in millionths of a dollar.

- **Before a call**, a Lua script reserves its maximum cost. Redis runs it in one step,
  so simultaneous calls, even on several servers, can never share the same last dollars.
  If the budget or today's limit would be passed, nothing is sent.
- **After a call**, the real cost replaces the reservation. When it is not known whether
  the call was billed (a timeout, a server error, a missing usage), its maximum stays
  counted. A call that never reports back, for example after a crash, is counted at its
  maximum after 10 minutes.
- **If the database cannot be reached or is not set up**, paid AI is blocked.
- **The global budget never resets by itself.** The daily limit applies per UTC day.
- The Console spend limit and prepaid credits without automatic reload stay the last
  protection: this app only keeps its own count.

The owner manages the budget from their computer, with the database URL and token in
`UPSTASH_REDIS_REST_URL` and `UPSTASH_REDIS_REST_TOKEN` (or typed without being shown).
Changes apply at once, without deploying:

```powershell
.venv\Scripts\python.exe -m ai_bug_detector.budget_admin status
.venv\Scripts\python.exe -m ai_bug_detector.budget_admin start --budget 4 --daily 1
.venv\Scripts\python.exe -m ai_bug_detector.budget_admin daily --usd 0.5
.venv\Scripts\python.exe -m ai_bug_detector.budget_admin stop
.venv\Scripts\python.exe -m ai_bug_detector.budget_admin resume
```

`start` begins a new period with spending at $0: only run it after really adding
credits, and checking the balance in the Claude Console. It asks to type the amount
again, and keeps the previous period in a history.

**Known limit:** the budget is shared, not split per visitor. One person can still use
today's limit, which then blocks everyone until the next day. Sessions, cookies and IP
addresses cannot identify a visitor reliably. Fair per-visitor limits need sign-in.

`AI_BUG_DETECTOR_MAX_SPEND_USD` only applies in local mode.

### Cost

Using this project costs nothing without an API key. A real review is billed to the
owner of the key, through prepaid API credits. Before each call, the page shows an
estimated maximum cost, and after it, the actual cost.

| Model | Input / output, per million tokens |
|---|---|
| Claude Sonnet 5.5 (default) | $2 / $10 |
| Claude Opus 5.5 | $4 / $20 |
| Claude Haiku 5.5 | $0.10 / $0.50 |

To limit costs:

- A review accepts up to 30 KB of code, sends up to 20 Ruff issues (most severe first),
  and generates up to 6,000 output tokens, thinking included.
- Thinking is turned off on Sonnet 5.5 and Haiku 5.5. Opus 5.5 does not allow it, so it
  runs with low or medium effort.
- Each click makes one request, and failed requests are never retried automatically.
- The app refuses any call that could take its spending above a limit: $1.00 by default
  while it runs. Change it with `$env:AI_BUG_DETECTOR_MAX_SPEND_USD = "2"`. This limit
  resets when the app restarts, so also set a spend limit on a dedicated workspace in the
  Claude Console.

Check the [current prices](https://platform.claude.com/docs/en/about-claude/pricing)
before use.

### Free check before a first real call

This checks that the models exist for your key, and measures the exact size of the
requests for a file. It creates no message, so nothing is billed. The key is typed
without being shown, and is never printed or saved.

```powershell
.venv\Scripts\python.exe -m ai_bug_detector.claude_check first_test.py
```

### Data and safety

- **Consent:** nothing is sent until you accept for this file and model, and click a
  button (in local mode, you also enter your key). The file name is not sent.
- **Retention:** Anthropic may keep the data for up to 30 days and does not use API data
  for training by default. See its
  [data retention documentation](https://platform.claude.com/docs/en/manage-claude/api-and-data-retention).
  Do not send code containing secrets.
- **API key:** in local mode, it stays in memory for the session. In public mode, it stays
  on the server. It is never written to a file by the app. Do not commit it: `.env` and
  `.streamlit/secrets.toml` are ignored by Git.
- **Logs:** do not set `ANTHROPIC_LOG`. The SDK never logs the key, but its debug logs
  show the code that is sent.
- **Untrusted content:** analyzed code is sent as data, so instructions hidden in it should
  not be followed, though no AI model can guarantee this. Answers are validated and shown
  as plain text.
- **Fixes:** a suggested fix is only used if you accept it, to build a preview and a
  downloadable copy. Your file is never changed, and no fix is ever run.
- **Logic bugs:** these are unverified hypotheses, because the code is not run.

## Project structure

```text
├── .streamlit/config.toml  Visual theme
├── app.py                  Streamlit entry point (user interface)
├── pyproject.toml          Project metadata, dependencies and tool settings
├── src/ai_bug_detector/
│   ├── models.py           Shared data: Issue and Severity
│   ├── report.py           Sorts and prepares results for display
│   ├── ruff_analyzer.py    Runs Ruff and converts its results into Issue objects
│   ├── ai_models.py        AI review data, and validation of AI answers
│   ├── ai_prompts.py       What is sent to the AI, and the size limits
│   ├── ai_review.py        Demo and Claude reviewers, costs and API errors
│   ├── ai_panel.py         AI part of the page: demo, consent and results
│   ├── deployment.py       Local or public mode, and the server's API key
│   ├── redis_rest.py       Minimal HTTPS client for the Upstash REST API
│   ├── shared_budget.py    Shared AI budget of a public site, kept in Redis
│   ├── budget_admin.py     Owner's command to view, renew or stop that budget
│   └── demo/               Sample file and hand-written illustrative examples
└── tests/                  Automated tests
```
