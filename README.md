# AI Bug Detector

AI Python bug detection and code analysis tool.

> **Project status:** early development. You can upload a Python file in the web
> interface and see the issues found by Ruff. AI features are not available yet.

## Features

- Upload one Python file of up to 5 MB and analyze it with one click.
- See each issue's severity, line, description and Ruff rule, most severe first.
- Get a clear message when a file has no issues, is empty or cannot be analyzed.

## Planned features

1. Isolated test execution for unknown files with Docker.
2. AI-assisted explanations with the Anthropic API.

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

## Project structure

```text
├── .streamlit/config.toml  Visual theme
├── app.py                  Streamlit entry point (user interface)
├── pyproject.toml          Project metadata, dependencies and tool settings
├── src/ai_bug_detector/
│   ├── models.py           Shared data: Issue and Severity
│   ├── report.py           Sorts and prepares results for display
│   └── ruff_analyzer.py    Runs Ruff and converts its results into Issue objects
└── tests/                  Automated tests
```
