"""Tests for the Docker sandbox, with a simulated Docker: no container is ever started.

The fake replaces subprocess.run and subprocess.Popen. It records every command and
imitates what docker.exe would do: print a result, hang until removed, flood the
output, or fail.
"""

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from streamlit.runtime.scriptrunner_utils.exceptions import (
    RerunException,
    StopException,
)
from streamlit.runtime.scriptrunner_utils.script_requests import RerunData

from ai_bug_detector import docker_sandbox, sandbox_models
from ai_bug_detector.docker_sandbox import (
    IMAGE,
    LABEL,
    MAX_FILE_BYTES,
    SANDBOX_VARIABLE,
    STARTED_LABEL,
    SandboxCleanupError,
    SandboxError,
    SandboxStatus,
    build_run_command,
    find_docker,
    remove_abandoned_containers,
    run_tests,
    sandbox_status,
    validate_files,
)
from ai_bug_detector.sandbox_models import (
    MAX_OUTPUT_BYTES,
    RESULT_MARKER,
    RunStatus,
    TestOutcome,
)

DOCKER = Path("C:/Program Files/Docker/docker.exe")
CODE = b"def add(a, b):\n    return a + b + 1\n"
TESTS = b"from calc import add\n\ndef test_add():\n    assert add(2, 2) == 4\n"


def runner_output(exit_code=1, outcome="failed") -> bytes:
    result = {
        "protocol": 1,
        "exit_code": exit_code,
        "tests": [
            {
                "name": "test_calc.py::test_add",
                "outcome": outcome,
                "duration": 0.01,
                "message": "assert 5 == 4",
            }
        ],
    }
    return RESULT_MARKER + json.dumps(result).encode() + b"\n"


# --- A simulated Docker ---


class FakeStream:
    """A pipe: fixed chunks, an endless flood, or nothing until the container stops."""

    def __init__(self, stopped, chunks=(), flood=None, blocks=False):
        self._stopped = stopped
        self._chunks = list(chunks)
        self._flood = flood
        self._blocks = blocks
        self.delivered = 0

    def read1(self, size):
        if self._chunks:
            chunk = self._chunks.pop(0)
        elif self._flood is not None and not self._stopped.is_set():
            chunk = self._flood
        else:
            if self._blocks:
                self._stopped.wait(10)
            return b""
        self.delivered += len(chunk)
        return chunk


class FakeStdin:
    def __init__(self):
        self.data = b""
        self.closed = False

    def write(self, data):
        self.data += data

    def close(self):
        self.closed = True


class FakeProcess:
    def __init__(
        self,
        command,
        stdout=b"",
        stderr=b"",
        returncode=0,
        hangs=False,
        flood=None,
        stderr_flood=None,
        interrupt=False,
    ):
        self.command = command
        self.stopped = threading.Event()
        self._hangs = hangs or flood is not None
        self._returncode = returncode
        self._interrupt = interrupt
        self.killed = False
        self.stdin = FakeStdin()
        self.stdout = FakeStream(
            self.stopped, [stdout] if stdout else [], flood, blocks=self._hangs
        )
        self.stderr = FakeStream(
            self.stopped, [stderr] if stderr else [], stderr_flood, blocks=self._hangs
        )
        if stderr_flood is not None:
            # The flood stops when the process ends on its own, a moment later.
            threading.Timer(0.2, self.stopped.set).start()

    @property
    def returncode(self):
        return self.poll()

    def poll(self):
        if self._interrupt:
            interruption = self._interrupt
            self._interrupt = False  # Like Ctrl+C: raised once.
            raise KeyboardInterrupt if interruption is True else interruption
        if self._hangs and not self.stopped.is_set():
            return None
        return 137 if self.killed else self._returncode

    def kill(self):
        self.killed = True
        self.stopped.set()

    def wait(self, timeout=None):
        self.stopped.wait(timeout)


class FakeDocker:
    """Records every command, and answers like a Docker engine would."""

    def __init__(self):
        self.commands = []
        self.processes = []
        self.process_options = {}
        self.engine_os = b"linux"
        self.engine_available = True
        self.image_present = True
        self.oom_killed = False
        # Existing containers: {id or name: text of the start-time label}.
        self.containers = {}
        self.popen_error = None
        self.rm_fails = False
        self.inspect_answers = True
        self.listing_answers = True

    def run(self, command, **kwargs):
        assert isinstance(command, list) and "shell" not in kwargs
        self.commands.append(command)
        action = command[1]
        if action == "version":
            if not self.engine_available:
                return subprocess.CompletedProcess(command, 1, b"", b"no engine")
            return subprocess.CompletedProcess(command, 0, self.engine_os + b"\n", b"")
        if action == "image":
            return subprocess.CompletedProcess(command, 0 if self.image_present else 1)
        if action == "ps":
            if not self.listing_answers:
                return subprocess.CompletedProcess(command, 1, b"", b"engine stopped")
            lines = "".join(
                f"{key} {label}\n" for key, label in self.containers.items()
            )
            return subprocess.CompletedProcess(command, 0, lines.encode(), b"")
        if action == "inspect":
            answer = b"true\n" if self.oom_killed else b"false\n"
            return subprocess.CompletedProcess(command, 0, answer, b"")
        if action == "container":  # docker container inspect: does it still exist?
            if not self.inspect_answers:
                raise subprocess.TimeoutExpired(command, 15)
            name = command[-1]
            if name in self.containers:
                return subprocess.CompletedProcess(command, 0, b"id\n", b"")
            error = f"Error: No such container: {name}".encode()
            return subprocess.CompletedProcess(command, 1, b"", error)
        if action == "rm":
            names = set(command[3:])
            if self.rm_fails:
                return subprocess.CompletedProcess(command, 1, b"", b"rm failed")
            for name in names:
                self.containers.pop(name, None)
            for process in self.processes:
                if process.command[process.command.index("--name") + 1] in names:
                    process.stopped.set()  # Removing the container stops it.
            return subprocess.CompletedProcess(command, 0, b"", b"")
        raise AssertionError(f"Unexpected Docker command: {command}")

    def popen_with(self, **behavior):
        def popen(command, **kwargs):
            assert isinstance(command, list) and "shell" not in kwargs
            if self.popen_error:
                raise self.popen_error
            self.commands.append(command)
            self.process_options = kwargs
            process = FakeProcess(command, **behavior)
            self.processes.append(process)
            name = command[command.index("--name") + 1]
            self.containers[name] = f"{time.time():.0f}"
            return process

        return popen

    def removed(self) -> list[str]:
        return [name for c in self.commands if c[1] == "rm" for name in c[3:]]

    def run_name(self) -> str:
        command = self.processes[-1].command
        return command[command.index("--name") + 1]


@pytest.fixture
def docker(monkeypatch):
    fake = FakeDocker()
    monkeypatch.setenv(SANDBOX_VARIABLE, "1")
    monkeypatch.setattr(docker_sandbox, "find_docker", lambda: DOCKER)
    monkeypatch.setattr(subprocess, "run", fake.run)
    monkeypatch.setattr(subprocess, "Popen", fake.popen_with(stdout=runner_output()))
    return fake


def behave(monkeypatch, docker, **behavior):
    monkeypatch.setattr(subprocess, "Popen", docker.popen_with(**behavior))


def run():
    return run_tests("calc.py", CODE, "test_calc.py", TESTS)


def option(command: list[str], name: str) -> list[str]:
    """Return every value given to an option in a command."""
    return [command[i + 1] for i, part in enumerate(command) if part == name]


# --- Off by default, and nothing runs without Docker ---


@pytest.mark.parametrize("value", [None, "", "0", "true", "yes"])
def test_sandbox_is_off_without_the_exact_setting(monkeypatch, value):
    monkeypatch.delenv(SANDBOX_VARIABLE, raising=False)
    if value is not None:
        monkeypatch.setenv(SANDBOX_VARIABLE, value)

    assert sandbox_status() is SandboxStatus.DISABLED


def test_disabled_sandbox_runs_nothing(docker, monkeypatch):
    monkeypatch.delenv(SANDBOX_VARIABLE)

    with pytest.raises(SandboxError, match="turned off"):
        run()

    assert sandbox_status() is SandboxStatus.DISABLED
    assert docker.commands == []


def test_missing_docker_runs_nothing(docker, monkeypatch):
    monkeypatch.setattr(docker_sandbox, "find_docker", lambda: None)

    with pytest.raises(SandboxError, match="Docker was not found"):
        run()

    assert sandbox_status() is SandboxStatus.DOCKER_NOT_FOUND
    assert docker.commands == []


@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        (
            lambda d: setattr(d, "engine_available", False),
            SandboxStatus.ENGINE_UNAVAILABLE,
        ),
        (lambda d: setattr(d, "engine_os", b"windows"), SandboxStatus.NOT_LINUX),
        (lambda d: setattr(d, "image_present", False), SandboxStatus.IMAGE_MISSING),
        (lambda d: None, SandboxStatus.READY),
    ],
)
def test_status_checks_every_requirement_without_starting_a_container(
    docker, setup, expected
):
    setup(docker)

    assert sandbox_status() is expected
    assert docker.processes == []


def test_engine_that_does_not_answer_in_time_is_unavailable(docker, monkeypatch):
    def too_slow(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 15)

    monkeypatch.setattr(subprocess, "run", too_slow)

    assert sandbox_status() is SandboxStatus.ENGINE_UNAVAILABLE


# --- Finding docker.exe ---

DOCKER_NAME = "docker.exe" if sys.platform == "win32" else "docker"


def test_docker_is_found_in_an_absolute_path_folder(tmp_path, monkeypatch):
    (tmp_path / DOCKER_NAME).write_bytes(b"")
    monkeypatch.setenv("PATH", str(tmp_path))

    assert find_docker() == tmp_path / DOCKER_NAME


def test_docker_in_the_current_folder_is_never_used(tmp_path, monkeypatch):
    (tmp_path / DOCKER_NAME).write_bytes(b"")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", "")

    assert find_docker() is None


def test_relative_path_folders_are_ignored(tmp_path, monkeypatch):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / DOCKER_NAME).write_bytes(b"")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", "bin")

    assert find_docker() is None


def test_a_folder_named_like_docker_is_not_a_program(tmp_path, monkeypatch):
    (tmp_path / DOCKER_NAME).mkdir()
    monkeypatch.setenv("PATH", str(tmp_path))

    assert find_docker() is None


# --- Uploaded files ---


def test_valid_files_are_accepted():
    validate_files("calc.py", CODE, "test_calc.py", TESTS)


def test_files_exactly_at_the_size_limit_are_accepted():
    content = b"#" * MAX_FILE_BYTES

    validate_files("calc.py", content, "test_calc.py", content)


@pytest.mark.parametrize("which", ["code", "tests"])
def test_files_over_the_size_limit_are_refused(which):
    big = b"#" * (MAX_FILE_BYTES + 1)
    code, tests = (big, TESTS) if which == "code" else (CODE, big)

    with pytest.raises(SandboxError, match="larger than 256 KB"):
        validate_files("calc.py", code, "test_calc.py", tests)


def test_non_utf8_file_is_refused():
    with pytest.raises(SandboxError, match="not valid UTF-8"):
        validate_files("calc.py", b"\xff\xfe", "test_calc.py", TESTS)


@pytest.mark.parametrize(
    "name",
    [
        "my calc.py",
        "../calc.py",
        "dir/calc.py",
        "calc.txt",
        "calc",
        "1calc.py",
        "calc.PY",
        "class.py",
        "conftest.py",
        "pytest.py",
        "_pytest.py",
        "pluggy.py",
        "Runner.py",
        "random.py",
        "os.py",
        "json.py",
    ],
)
def test_unsafe_or_shadowing_code_file_names_are_refused(name):
    with pytest.raises(SandboxError, match="cannot be used"):
        validate_files(name, CODE, "test_calc.py", TESTS)


@pytest.mark.parametrize("name", ["test_calc.py", "calc_test.py"])
def test_code_file_must_not_look_like_a_test_file(name):
    with pytest.raises(SandboxError, match="must not look like a test file"):
        validate_files(name, CODE, "test_other.py", TESTS)


def test_test_file_must_start_with_test():
    with pytest.raises(SandboxError, match="must start with test_"):
        validate_files("calc.py", CODE, "checks.py", TESTS)


def test_the_two_files_must_be_different():
    with pytest.raises(SandboxError, match="must be different"):
        validate_files("test_calc.py", CODE, "test_calc.py", TESTS)


def test_invalid_files_never_reach_docker(docker):
    with pytest.raises(SandboxError):
        run_tests("os.py", CODE, "test_calc.py", TESTS)

    assert docker.commands == []


# --- The docker run command ---


@pytest.fixture
def command():
    return build_run_command(DOCKER, "ai-bug-detector-sandbox-test")


def test_command_starts_with_docker_and_ends_with_the_image(command):
    assert command[0] == str(DOCKER)
    assert command[1] == "run"
    assert command[-1] == IMAGE


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("--network", "none"),
        ("--user", "65534:65534"),
        ("--cap-drop", "ALL"),
        ("--security-opt", "no-new-privileges"),
        ("--memory", "256m"),
        ("--memory-swap", "256m"),
        ("--cpus", "1"),
        ("--pids-limit", "64"),
        ("--ulimit", "nofile=256:256"),
        ("--ulimit", "msgqueue=0"),
        ("--pull", "never"),
        ("--runtime", "runc"),
        ("--ipc", "none"),
        ("--log-driver", "none"),
        ("--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=64m"),
        ("--name", "ai-bug-detector-sandbox-test"),
        ("--label", f"{LABEL}=1"),
    ],
)
def test_command_sets_each_protection(command, name, value):
    assert value in option(command, name)


@pytest.mark.parametrize("flag", ["--read-only", "--init", "--interactive"])
def test_command_sets_each_flag(command, flag):
    assert flag in command


@pytest.mark.parametrize(
    "forbidden",
    [
        "-v",
        "--volume",
        "--mount",
        "-p",
        "--publish",
        "--privileged",
        "--gpus",
        "--device",
        "-e",
        "--env",
        "--env-file",
        "--cap-add",
        "--tty",
        "-t",
    ],
)
def test_command_never_shares_files_ports_devices_or_secrets(command, forbidden):
    assert forbidden not in command


def test_command_records_when_the_container_started(command):
    (started,) = [v for v in option(command, "--label") if v.startswith(STARTED_LABEL)]

    assert abs(float(started.split("=")[1]) - time.time()) < 5


# --- A normal run ---


def test_run_returns_the_validated_result(docker):
    result = run()

    assert result.status is RunStatus.COMPLETED
    assert result.count(TestOutcome.FAILED) == 1


def test_files_are_sent_through_standard_input(docker):
    run()

    payload = json.loads(docker.processes[0].stdin.data)
    assert payload == {
        "protocol": 1,
        "code_file": {"name": "calc.py", "content": CODE.decode()},
        "test_file": {"name": "test_calc.py", "content": TESTS.decode()},
    }
    assert docker.processes[0].stdin.closed


def test_uploaded_code_is_never_run_on_windows(docker):
    run()

    for command in docker.commands:
        assert command[0] == str(DOCKER)
        assert sys.executable not in command
        assert not any("calc" in part for part in command)
    assert "shell" not in docker.process_options


def test_each_run_uses_a_new_container_name(docker):
    run()
    run()

    names = [p.command[p.command.index("--name") + 1] for p in docker.processes]
    assert len(set(names)) == 2


def test_result_exactly_at_the_output_limit_is_read(docker, monkeypatch):
    end = runner_output()
    output = b"x" * (MAX_OUTPUT_BYTES - len(end) - 1) + b"\n" + end
    behave(monkeypatch, docker, stdout=output)

    assert run().status is RunStatus.COMPLETED


# --- The container is removed in every case ---


def test_container_is_removed_after_a_normal_run(docker):
    run()

    assert docker.run_name() in docker.removed()


def test_time_limit_stops_the_run_and_removes_the_container(docker, monkeypatch):
    monkeypatch.setattr(docker_sandbox, "TIMEOUT_SECONDS", 0.3)
    behave(monkeypatch, docker, hangs=True)
    start = time.monotonic()

    result = run()

    assert result.status is RunStatus.TIMED_OUT
    assert time.monotonic() - start < 5
    assert docker.run_name() in docker.removed()
    assert docker.processes[0].stopped.is_set()


def test_output_flood_is_stopped_while_it_happens(docker, monkeypatch):
    behave(monkeypatch, docker, flood=b"x" * 65536)
    start = time.monotonic()

    result = run()

    assert result.status is RunStatus.OUTPUT_LIMIT
    assert time.monotonic() - start < 5
    assert docker.run_name() in docker.removed()


class ChunkStream:
    def __init__(self, chunks: int, size: int = 65536):
        self.left = chunks
        self.size = size

    def read1(self, size):
        if not self.left:
            return b""
        self.left -= 1
        return b"x" * self.size


def test_output_kept_in_memory_never_exceeds_the_limit():
    stream = ChunkStream(chunks=160)  # 10 MB
    kept = bytearray()
    overflow = threading.Event()

    docker_sandbox._read_limited(stream, kept, MAX_OUTPUT_BYTES, overflow)

    assert len(kept) == MAX_OUTPUT_BYTES
    assert overflow.is_set()
    assert stream.left == 0  # Drained to the end, so docker.exe never blocks.


def test_output_exactly_at_the_limit_is_not_an_overflow():
    kept = bytearray()
    overflow = threading.Event()

    docker_sandbox._read_limited(
        ChunkStream(chunks=16), kept, MAX_OUTPUT_BYTES, overflow
    )

    assert len(kept) == MAX_OUTPUT_BYTES
    assert not overflow.is_set()


def test_error_output_flood_is_drained_without_breaking_the_run(docker, monkeypatch):
    behave(
        monkeypatch,
        docker,
        stdout=runner_output(),
        stderr_flood=b"e" * 65536,
        hangs=True,
    )

    result = run()

    assert result.status is RunStatus.COMPLETED
    assert docker.run_name() in docker.removed()


def test_memory_limit_is_reported_and_the_container_removed(docker, monkeypatch):
    docker.oom_killed = True
    behave(monkeypatch, docker, returncode=137)

    assert run().status is RunStatus.MEMORY_LIMIT
    assert docker.run_name() in docker.removed()


def test_docker_failure_is_explained_and_the_container_removed(docker, monkeypatch):
    behave(
        monkeypatch,
        docker,
        returncode=125,
        stderr=b"Unable to find image\x1b[31m locally\n",
    )

    with pytest.raises(SandboxError, match="could not start") as caught:
        run()

    assert "\x1b" not in str(caught.value)
    assert "Unable to find image" in str(caught.value)
    assert docker.run_name() in docker.removed()


@pytest.mark.parametrize(
    "stdout",
    [
        pytest.param(b"", id="no-output"),
        pytest.param(b"garbage\n", id="garbage"),
        pytest.param(RESULT_MARKER + b"{broken\n", id="broken-result"),
    ],
)
def test_invalid_output_is_an_error_and_the_container_removed(
    docker, monkeypatch, stdout
):
    behave(monkeypatch, docker, stdout=stdout, returncode=0)

    with pytest.raises(SandboxError):
        run()

    assert docker.run_name() in docker.removed()


def test_docker_that_cannot_start_is_an_error(docker):
    docker.popen_error = FileNotFoundError("docker.exe")

    with pytest.raises(SandboxError, match="could not be started"):
        run()

    assert any(c[1] == "rm" for c in docker.commands)


def test_interruption_still_removes_the_container(docker, monkeypatch):
    behave(monkeypatch, docker, interrupt=True)

    with pytest.raises(KeyboardInterrupt):
        run()

    assert docker.run_name() in docker.removed()


@pytest.mark.parametrize(
    "interruption",
    [
        pytest.param(StopException(), id="streamlit-stop"),
        pytest.param(RerunException(RerunData()), id="streamlit-rerun"),
    ],
)
def test_streamlit_interruption_still_removes_the_container(
    docker, monkeypatch, interruption
):
    behave(monkeypatch, docker, interrupt=interruption)

    with pytest.raises(type(interruption)):
        run()

    assert docker.run_name() in docker.removed()
    assert docker.containers == {}


# --- One run at a time ---


def test_a_second_run_is_refused_while_one_is_in_progress(docker, monkeypatch):
    monkeypatch.setattr(docker_sandbox, "TIMEOUT_SECONDS", 1)
    behave(monkeypatch, docker, hangs=True)
    first = threading.Thread(target=run)
    first.start()
    while not docker.processes:
        time.sleep(0.01)

    with pytest.raises(SandboxError, match="Another test run"):
        run()

    first.join()
    assert len(docker.processes) == 1


def test_lock_is_released_after_an_error(docker, monkeypatch):
    behave(monkeypatch, docker, returncode=125)
    with pytest.raises(SandboxError):
        run()

    behave(monkeypatch, docker, stdout=runner_output())
    assert run().status is RunStatus.COMPLETED


# --- Abandoned containers ---


def test_old_and_unlabeled_containers_are_removed_recent_ones_kept(docker):
    now = time.time()
    docker.containers = {
        "old111": f"{now - 3600:.0f}",
        "recent222": f"{now - 5:.0f}",
        "nolabel333": "",
        "garbage444": "not-a-number",
    }

    removed = remove_abandoned_containers(DOCKER)

    assert removed == 3
    assert set(docker.removed()) == {"old111", "nolabel333", "garbage444"}
    assert set(docker.containers) == {"recent222"}


def test_listing_only_looks_at_sandbox_containers(docker):
    remove_abandoned_containers(DOCKER)

    (listing,) = [c for c in docker.commands if c[1] == "ps"]
    assert f"label={LABEL}" in option(listing, "--filter")


def test_nothing_is_removed_when_there_is_nothing_abandoned(docker):
    assert remove_abandoned_containers(DOCKER) == 0
    assert docker.removed() == []


def test_failed_removal_of_abandoned_containers_is_not_counted(docker):
    docker.containers = {"old111": "0"}
    docker.rm_fails = True

    assert remove_abandoned_containers(DOCKER) == 0
    assert "old111" in docker.containers


def test_removal_that_cannot_be_checked_is_not_counted(docker, monkeypatch):
    docker.containers = {"old111": "0"}
    real_run = docker.run

    def listing_fails_after_rm(command, **kwargs):
        if command[1] == "rm":
            docker.listing_answers = False
        return real_run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", listing_fails_after_rm)

    assert remove_abandoned_containers(DOCKER) == 0


def test_abandoned_containers_are_removed_before_each_run(docker):
    docker.containers = {"old111": "0"}

    run()

    order = [c[1] for c in docker.commands]
    assert order.index("ps") < order.index("run")
    assert "old111" in docker.removed()


# --- A failed removal is never reported as a success ---


def test_container_that_cannot_be_removed_is_reported(docker):
    docker.rm_fails = True

    with pytest.raises(SandboxCleanupError) as caught:
        run()

    name = docker.run_name()
    assert name in docker.containers  # Still there, and the user is told so.
    assert f"docker rm -f {name}" in str(caught.value)
    assert f"label={LABEL}" in str(caught.value)


def test_engine_that_cannot_confirm_the_removal_is_reported(docker):
    docker.inspect_answers = False

    with pytest.raises(SandboxCleanupError, match="could not be confirmed"):
        run()


def test_failed_removal_after_a_time_limit_is_reported(docker, monkeypatch):
    monkeypatch.setattr(docker_sandbox, "TIMEOUT_SECONDS", 0.3)
    behave(monkeypatch, docker, hangs=True)
    docker.rm_fails = True

    with pytest.raises(SandboxCleanupError):
        run()

    assert docker.processes[0].killed  # docker.exe is still stopped.


def test_failed_removal_after_an_error_keeps_both_messages(docker, monkeypatch):
    behave(monkeypatch, docker, returncode=125, stderr=b"No such image")
    docker.rm_fails = True

    with pytest.raises(SandboxCleanupError) as caught:
        run()

    assert "could not start" in str(caught.value)
    assert "could not be confirmed as removed" in str(caught.value)


def test_failed_removal_after_an_interruption_keeps_the_interruption(
    docker, monkeypatch
):
    behave(monkeypatch, docker, interrupt=True)
    docker.rm_fails = True

    with pytest.raises(KeyboardInterrupt) as caught:
        run()

    assert any("could not be confirmed" in note for note in caught.value.__notes__)


def test_lock_is_released_after_a_failed_removal(docker):
    docker.rm_fails = True
    with pytest.raises(SandboxCleanupError):
        run()

    docker.rm_fails = False
    assert run().status is RunStatus.COMPLETED


# --- run_tests checks every requirement, even when called directly ---


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        (lambda d: setattr(d, "engine_available", False), "engine is not running"),
        (lambda d: setattr(d, "engine_os", b"windows"), "Linux containers"),
        (lambda d: setattr(d, "image_present", False), "docker build -t"),
    ],
)
def test_run_is_refused_when_a_requirement_is_missing(docker, setup, message):
    setup(docker)

    with pytest.raises(SandboxError, match=message):
        run()

    assert docker.processes == []
    assert not any(c[1] in ("run", "ps", "rm") for c in docker.commands)


# --- Compatibility with sandbox_models ---


def test_output_limit_is_the_one_used_to_validate_results():
    assert docker_sandbox.MAX_OUTPUT_BYTES is sandbox_models.MAX_OUTPUT_BYTES
