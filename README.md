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

Do not set this variable on a public deployment: code and keys from visitors would pass
through that server. It is off by default, so a public version only offers Ruff and the
demo.

### Cost

Using this project costs nothing without an API key. A real review is billed to the
owner of the key, through prepaid API credits. Before each call, the page shows an
estimated maximum cost, and after it, the actual cost.

| Model | Input / output, per million tokens |
|---|---|
| Claude Sonnet 5.5 (default) | $2 / $10 |
| Claude Opus 5.5 | $4 / $20 |
| Claude Haiku 5.5 | $0.10 / $0.50 |

To limit costs, a review accepts up to 30 KB of code, sends up to 20 Ruff issues (most
severe first), and generates up to 6,000 output tokens. Each click makes one request, and
failed requests are never retried automatically. Check the
[current prices](https://platform.claude.com/docs/en/about-claude/pricing) before use.

### Data and safety

- **Consent:** nothing is sent until you enter a key, accept for this file and model, and
  click a button. The file name is not sent.
- **Retention:** Anthropic may keep the data for up to 30 days and does not use API data
  for training by default. See its
  [data retention documentation](https://platform.claude.com/docs/en/manage-claude/api-and-data-retention).
  Do not send code containing secrets.
- **API key:** it stays in memory for the session. It is never written to a file. Do not
  commit it: `.env` and `.streamlit/secrets.toml` are ignored by Git.
- **Untrusted content:** analyzed code is sent as data, so instructions hidden in it should
  not be followed, though no AI model can guarantee this. Answers are validated and shown
  as plain text.
- **Fixes:** suggested fixes are only displayed. They are never applied or executed.
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
│   └── demo/               Sample file and hand-written illustrative examples
└── tests/                  Automated tests
```
