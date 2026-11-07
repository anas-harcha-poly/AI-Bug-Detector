# AI Bug Detector

AI Python bug detection and code analysis tool.

> **Project status:** early development. Only the project foundation exists so far:
> the application displays a placeholder page and does not analyze code yet.

## Planned features

1. Static analysis of Python code with [Ruff](https://docs.astral.sh/ruff/), with no API key required.
2. Isolated test execution for unknown files with Docker.
3. AI-assisted explanations with the Anthropic API.

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

```powershell
.venv\Scripts\python.exe -m streamlit run app.py
```

## Development

```powershell
.venv\Scripts\python.exe -m pytest
.venv\Scripts\python.exe -m ruff check .
```

## Project structure

```text
├── app.py                  Streamlit entry point (user interface)
├── pyproject.toml          Project metadata, dependencies and tool settings
├── src/ai_bug_detector/    Application package
└── tests/                  Automated tests
```
