"""Functional tests with the real sandbox: a real container runs our own harmless files.

Skipped by default. Run them with:
    .venv\\Scripts\\python.exe -m pytest -m docker
When requested, they fail instead of being skipped if the sandbox is not ready, because
a skipped test proves nothing. The attack simulations come in a later step.
"""

import subprocess

import pytest

from ai_bug_detector.docker_sandbox import (
    LABEL,
    SANDBOX_VARIABLE,
    SandboxStatus,
    find_docker,
    run_tests,
    sandbox_status,
)
from ai_bug_detector.sandbox_models import RunStatus, TestOutcome

pytestmark = pytest.mark.docker

CODE = b"""\
def add(a, b):
    return a + b


def average(values):
    return sum(values) / (len(values) + 1)  # Deliberate bug.


def greet(name):
    return f"Bonjour, {name} !"
"""


@pytest.fixture(autouse=True)
def ready_sandbox(monkeypatch):
    monkeypatch.setenv(SANDBOX_VARIABLE, "1")
    status = sandbox_status()
    if status is not SandboxStatus.READY:
        pytest.fail(f"The sandbox is not ready: {status}")
    yield
    assert leftover_containers() == [], "A test container was left behind."


def leftover_containers() -> list[str]:
    listed = subprocess.run(
        [str(find_docker()), "ps", "--all", "--quiet", "--filter", f"label={LABEL}"],
        capture_output=True,
        check=True,
    )
    return listed.stdout.split()


def run(tests: str, code: bytes = CODE):
    return run_tests("calc.py", code, "test_calc.py", tests.encode())


def outcomes(result) -> dict[str, TestOutcome]:
    return {test.name.split("::")[-1]: test.outcome for test in result.tests}


def test_wrong_result_is_detected_and_right_result_passes():
    result = run(
        "from calc import add, average\n\n"
        "def test_add():\n    assert add(2, 2) == 4\n\n"
        "def test_average():\n    assert average([10, 20]) == 15\n"
    )

    assert result.status is RunStatus.COMPLETED
    assert outcomes(result) == {
        "test_add": TestOutcome.PASSED,
        "test_average": TestOutcome.FAILED,
    }
    failure = next(t for t in result.tests if t.outcome is TestOutcome.FAILED)
    assert "10.0 == 15" in failure.message


def test_every_kind_of_outcome_is_reported():
    result = run(
        "import pytest\nfrom calc import add\n\n"
        "@pytest.mark.skip(reason='not ready')\ndef test_skip():\n    pass\n\n"
        "@pytest.mark.xfail\ndef test_xfail():\n    assert add(1, 1) == 3\n\n"
        "@pytest.mark.xfail\ndef test_xpass():\n    assert add(1, 1) == 2\n\n"
        "@pytest.fixture\ndef broken():\n    raise RuntimeError('setup failed')\n\n"
        "def test_error(broken):\n    pass\n"
    )

    assert outcomes(result) == {
        "test_skip": TestOutcome.SKIPPED,
        "test_xfail": TestOutcome.XFAILED,
        "test_xpass": TestOutcome.XPASSED,
        "test_error": TestOutcome.ERROR,
    }
    skipped = next(t for t in result.tests if t.outcome is TestOutcome.SKIPPED)
    assert skipped.message == "Skipped: not ready"  # pytest's own wording.


def test_syntax_error_in_the_code_is_a_pytest_error():
    result = run(
        "from calc import add\n\ndef test_add():\n    pass\n", code=b"def (:\n"
    )

    assert result.status is RunStatus.PYTEST_ERROR
    assert result.count(TestOutcome.ERROR) == 1
    assert "SyntaxError" in result.tests[0].message


def test_file_without_tests_is_reported():
    assert run("from calc import add\n").status is RunStatus.NO_TESTS


def test_accented_text_survives_the_round_trip():
    result = run(
        "from calc import greet\n\n"
        "def test_greet():\n    assert greet('Anaïs') == 'Bonjour, Anaïs.'\n"
    )

    assert "Bonjour, Anaïs !" in result.tests[0].message


def test_container_has_the_expected_settings():
    # Harmless reads only: the attack simulations come in the next step.
    result = run(
        "import os, sys\n\n"
        "def test_plugins_are_not_loaded_automatically():\n"
        "    assert os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] == '1'\n\n"
        "def test_runs_as_nobody():\n"
        "    assert (os.getuid(), os.getgid()) == (65534, 65534)\n\n"
        "def test_files_are_in_tmp():\n"
        "    assert os.path.dirname(__file__) == '/tmp/work'\n\n"
        "def test_tmp_is_writable():\n"
        "    with open('/tmp/scratch.txt', 'w') as f:\n"
        "        f.write('ok')\n\n"
        "def test_runner_files_are_read_only():\n"
        "    for name in ('runner.py', 'pytest.ini'):\n"
        "        st = os.stat('/opt/sandbox/' + name)\n"
        "        assert (st.st_uid, st.st_mode & 0o777) == (0, 0o444)\n\n"
        "def test_no_bytecode_is_written():\n"
        "    assert sys.dont_write_bytecode\n\n"
        "def test_python_version():\n"
        "    assert sys.version_info[:2] == (3, 14)\n"
    )

    assert result.status is RunStatus.COMPLETED
    failures = [t for t in result.tests if t.outcome is not TestOutcome.PASSED]
    assert failures == [], failures
    assert len(result.tests) == 7
