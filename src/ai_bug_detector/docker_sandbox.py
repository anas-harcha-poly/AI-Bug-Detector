"""Run pytest on uploaded files inside a temporary, isolated Docker container.

The only program this module starts on Windows is docker.exe. The uploaded files are
sent to the container through standard input: no folder of this computer is shared
with it. There is no fallback: if Docker is unavailable, nothing is run at all.

Stopping docker.exe does not stop the container: the container belongs to the Docker
engine. That is why every run ends with `docker rm -f`, whatever happened.
"""

import json
import keyword
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from enum import StrEnum
from pathlib import Path

from ai_bug_detector.sandbox_models import (
    MAX_OUTPUT_BYTES,
    RunStatus,
    SandboxOutputError,
    TestRunResult,
    parse_runner_output,
)

# Running uploaded code is off unless this is set, so a public deployment is safe.
SANDBOX_VARIABLE = "AI_BUG_DETECTOR_ENABLE_SANDBOX"

IMAGE = "ai-bug-detector-sandbox:1"
LABEL = "ai-bug-detector.sandbox"
STARTED_LABEL = "ai-bug-detector.sandbox.started"
PAYLOAD_PROTOCOL = 1

TIMEOUT_SECONDS = 30
MEMORY_LIMIT = "256m"
CPU_LIMIT = "1"
PROCESS_LIMIT = 64
TMP_SIZE = "64m"
MAX_FILE_BYTES = 256 * 1024

_DOCKER_COMMAND_TIMEOUT = 15  # For short commands such as `docker rm`.
_MAX_STDERR_BYTES = 16 * 1024
_READ_CHUNK_BYTES = 64 * 1024
# Docker's own exit codes: it could not create or start the container.
_DOCKER_FAILURE_EXIT_CODES = (125, 126, 127)

_MODULE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\.py")
# Names that would replace a module pytest or Python itself relies on.
_RESERVED_MODULES = {"conftest", "pytest", "_pytest", "pluggy", "py", "runner"}

_run_lock = threading.Lock()


class SandboxError(Exception):
    """The tests could not be run. The message is safe to show to the user."""


class SandboxCleanupError(SandboxError):
    """A test container could not be confirmed as removed."""


class SandboxStatus(StrEnum):
    """Whether this computer can run tests in the sandbox right now."""

    READY = "ready"
    DISABLED = "disabled"
    DOCKER_NOT_FOUND = "docker_not_found"
    ENGINE_UNAVAILABLE = "engine_unavailable"
    NOT_LINUX = "not_linux"
    IMAGE_MISSING = "image_missing"


STATUS_MESSAGES = {
    SandboxStatus.DISABLED: "Running tests is turned off on this server.",
    SandboxStatus.DOCKER_NOT_FOUND: "Docker was not found, so no test was run.",
    SandboxStatus.ENGINE_UNAVAILABLE: (
        "The Docker engine is not running, so no test was run."
    ),
    SandboxStatus.NOT_LINUX: ("Docker must use Linux containers, so no test was run."),
    SandboxStatus.IMAGE_MISSING: (
        f"The sandbox image {IMAGE} is missing, so no test was run. "
        f"Build it with: docker build -t {IMAGE} sandbox"
    ),
}


def sandbox_enabled() -> bool:
    """Return True only if running uploaded code was explicitly turned on."""
    return os.environ.get(SANDBOX_VARIABLE) == "1"


def find_docker() -> Path | None:
    """Return docker.exe from the absolute folders of PATH, or None.

    shutil.which() is not used: on Windows it also looks in the current folder, where
    a file named docker.exe could have been placed.
    """
    name = "docker.exe" if sys.platform == "win32" else "docker"
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        if not folder or not os.path.isabs(folder):
            continue
        candidate = Path(folder) / name
        if candidate.is_file():
            return candidate
    return None


def sandbox_status() -> SandboxStatus:
    """Check every requirement, without starting any container."""
    if not sandbox_enabled():
        return SandboxStatus.DISABLED
    docker = find_docker()
    if docker is None:
        return SandboxStatus.DOCKER_NOT_FOUND

    version = _docker(docker, "version", "--format", "{{.Server.Os}}")
    if version is None or version.returncode != 0:
        return SandboxStatus.ENGINE_UNAVAILABLE
    if version.stdout.strip() != b"linux":
        return SandboxStatus.NOT_LINUX

    image = _docker(docker, "image", "inspect", "--format", "{{.Id}}", IMAGE)
    if image is None or image.returncode != 0:
        return SandboxStatus.IMAGE_MISSING
    return SandboxStatus.READY


def validate_files(code_name: str, code: bytes, test_name: str, tests: bytes) -> None:
    """Raise SandboxError if the uploaded files cannot be run safely and reliably."""
    for name, content in ((code_name, code), (test_name, tests)):
        if len(content) > MAX_FILE_BYTES:
            raise SandboxError(f"{name} is larger than {MAX_FILE_BYTES // 1024} KB.")
        try:
            content.decode("utf-8")
        except UnicodeDecodeError:
            raise SandboxError(f"{name} is not valid UTF-8 text.") from None
        _check_module_name(name)

    if code_name == test_name:
        raise SandboxError("The code file and the test file must be different.")
    if _is_test_file(code_name):
        raise SandboxError(
            "The code file name must not look like a test file "
            "(test_*.py or *_test.py)."
        )
    if not test_name.startswith("test_"):
        raise SandboxError("The test file name must start with test_.")


def run_tests(
    code_name: str, code: bytes, test_name: str, tests: bytes
) -> TestRunResult:
    """Run the tests in a new container, then remove it. One run at a time.

    Every requirement is checked here too, so calling this function directly is as
    safe as going through the page.
    """
    if not sandbox_enabled():
        raise SandboxError(STATUS_MESSAGES[SandboxStatus.DISABLED])
    validate_files(code_name, code, test_name, tests)
    status = sandbox_status()
    if status is not SandboxStatus.READY:
        raise SandboxError(STATUS_MESSAGES[status])
    docker = find_docker()
    if docker is None:
        raise SandboxError(STATUS_MESSAGES[SandboxStatus.DOCKER_NOT_FOUND])

    if not _run_lock.acquire(blocking=False):
        raise SandboxError("Another test run is in progress. Try again in a moment.")
    try:
        remove_abandoned_containers(docker)
        payload = _build_payload(code_name, code, test_name, tests)
        return _run_container(docker, payload)
    finally:
        _run_lock.release()


def remove_abandoned_containers(docker: Path) -> int:
    """Remove sandbox containers older than any run could be.

    Return how many are confirmed gone. A run can be abandoned if the app was stopped
    in the middle of it. Recent containers are kept, because another window of the
    app could be using them.
    """
    listed = _list_sandbox_containers(docker)
    if listed is None:
        return 0

    oldest_allowed = time.time() - TIMEOUT_SECONDS - 60
    abandoned = [
        container_id
        for container_id, started in listed.items()
        if started is None or started < oldest_allowed
    ]
    if not abandoned:
        return 0

    _docker(docker, "rm", "--force", *abandoned)
    remaining = _list_sandbox_containers(docker)
    if remaining is None:
        return 0  # Unknown: never reported as removed.
    return sum(1 for container_id in abandoned if container_id not in remaining)


def _list_sandbox_containers(docker: Path) -> dict[str, float | None] | None:
    """Return {container id: start time} for this app's containers, or None."""
    listed = _docker(
        docker,
        "ps",
        "--all",
        "--filter",
        f"label={LABEL}",
        "--format",
        f'{{{{.ID}}}} {{{{.Label "{STARTED_LABEL}"}}}}',
    )
    if listed is None or listed.returncode != 0:
        return None
    containers = {}
    for line in listed.stdout.decode("utf-8", errors="replace").splitlines():
        parts = line.split()
        if parts:
            containers[parts[0]] = _to_float(parts[1]) if len(parts) > 1 else None
    return containers


def build_run_command(docker: Path, name: str) -> list[str]:
    """Return the `docker run` command, with every isolation and limit."""
    return [
        str(docker),
        "run",
        "--name",
        name,
        "--label",
        f"{LABEL}=1",
        "--label",
        f"{STARTED_LABEL}={time.time():.0f}",
        "--interactive",  # The files arrive through standard input.
        "--pull",
        "never",  # Never download anything: only the image built locally is used.
        "--runtime",
        "runc",  # Explicit: another runtime could give access to the GPU.
        "--network",
        "none",
        "--read-only",
        "--tmpfs",
        f"/tmp:rw,nosuid,nodev,noexec,size={TMP_SIZE}",
        "--ipc",
        "none",  # No /dev/shm: Docker would add a second, writable 64 MB space.
        "--user",
        "65534:65534",  # The "nobody" user, not root.
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--memory",
        MEMORY_LIMIT,
        "--memory-swap",
        MEMORY_LIMIT,  # Same value as the memory limit: no extra swap.
        "--cpus",
        CPU_LIMIT,
        "--pids-limit",
        str(PROCESS_LIMIT),
        "--ulimit",
        "nofile=256:256",
        "--ulimit",
        "msgqueue=0",  # Docker always mounts /dev/mqueue: let it store nothing.
        "--init",  # A small init process stops leftover child processes.
        "--log-driver",
        "none",  # No unlimited logs kept by Docker.
        "--workdir",
        "/tmp",
        "--hostname",
        "sandbox",
        IMAGE,
    ]


def _check_module_name(name: str) -> None:
    stem = name.removesuffix(".py")
    if (
        not _MODULE_NAME.fullmatch(name)
        or keyword.iskeyword(stem)
        or stem.lower() in _RESERVED_MODULES
        or stem in sys.stdlib_module_names
    ):
        raise SandboxError(
            f"{name} cannot be used: the name must be a simple Python module name "
            "that does not replace a standard or pytest module."
        )


def _is_test_file(name: str) -> bool:
    return name.startswith("test_") or name.endswith("_test.py")


def _build_payload(code_name: str, code: bytes, test_name: str, tests: bytes) -> bytes:
    return json.dumps(
        {
            "protocol": PAYLOAD_PROTOCOL,
            "code_file": {"name": code_name, "content": code.decode("utf-8")},
            "test_file": {"name": test_name, "content": tests.decode("utf-8")},
        }
    ).encode("utf-8")


class _ContainerRun:
    """The docker.exe process of one run and the threads talking to it."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.process = None
        self.threads: list[threading.Thread] = []


def _run_container(docker: Path, payload: bytes) -> TestRunResult:
    """Run one container, then remove it whatever happens, and check the removal."""
    run = _ContainerRun(f"ai-bug-detector-sandbox-{uuid.uuid4().hex}")
    try:
        result = _execute(docker, run, payload)
    except BaseException as error:  # Also time limits and interruptions.
        if not _stop_and_remove(docker, run):
            note = _cleanup_failure_message(run.name)
            if isinstance(error, SandboxError):
                raise SandboxCleanupError(f"{error} {note}") from None
            error.add_note(note)  # For example KeyboardInterrupt: keep raising it.
        raise
    if not _stop_and_remove(docker, run):
        raise SandboxCleanupError(_cleanup_failure_message(run.name))
    return result


def _execute(docker: Path, run: _ContainerRun, payload: bytes) -> TestRunResult:
    stdout = bytearray()
    stderr = bytearray()
    output_overflow = threading.Event()
    timed_out = False
    try:
        run.process = subprocess.Popen(
            build_run_command(docker, run.name),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError:
        raise SandboxError("Docker could not be started.") from None

    process = run.process
    run.threads = [
        _start(_write_payload, process.stdin, payload),
        _start(
            _read_limited, process.stdout, stdout, MAX_OUTPUT_BYTES, output_overflow
        ),
        _start(_read_limited, process.stderr, stderr, _MAX_STDERR_BYTES, None),
    ]

    deadline = time.monotonic() + TIMEOUT_SECONDS
    while process.poll() is None:
        if output_overflow.is_set():
            break
        if time.monotonic() >= deadline:
            timed_out = True
            break
        output_overflow.wait(0.05)

    if output_overflow.is_set():
        return TestRunResult(RunStatus.OUTPUT_LIMIT)
    if timed_out:
        return TestRunResult(RunStatus.TIMED_OUT)
    if _was_killed_for_memory(docker, run.name):
        return TestRunResult(RunStatus.MEMORY_LIMIT)

    _join(run.threads)
    if process.returncode in _DOCKER_FAILURE_EXIT_CODES:
        raise SandboxError(
            "Docker could not start the test container: " + _safe_text(stderr)
        )
    try:
        return parse_runner_output(bytes(stdout))
    except SandboxOutputError as error:
        raise SandboxError(str(error)) from None


def _stop_and_remove(docker: Path, run: _ContainerRun) -> bool:
    """Remove the container, then stop docker.exe. Return True only if confirmed."""
    removed = _remove_container(docker, run.name)
    process = run.process
    if process is not None and process.poll() is None:
        process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    _join(run.threads)
    return removed


def _remove_container(docker: Path, name: str) -> bool:
    """Remove a container. Return True only if Docker confirms it no longer exists.

    `docker rm` can fail, for example if the engine stopped. So the result is checked
    afterwards, and an engine that does not answer never counts as a success.
    """
    _docker(docker, "rm", "--force", name)
    check = _docker(docker, "container", "inspect", "--format", "{{.Id}}", name)
    return (
        check is not None
        and check.returncode != 0
        and b"no such" in check.stderr.lower()
    )


def _cleanup_failure_message(name: str) -> str:
    return (
        f"The test container {name} could not be confirmed as removed. Check with "
        f"`docker ps -a --filter label={LABEL}` and remove it with "
        f"`docker rm -f {name}`."
    )


def _was_killed_for_memory(docker: Path, name: str) -> bool:
    result = _docker(docker, "inspect", "--format", "{{.State.OOMKilled}}", name)
    return result is not None and result.stdout.strip() == b"true"


def _write_payload(stream, payload: bytes) -> None:
    try:
        stream.write(payload)
        stream.close()
    except (BrokenPipeError, OSError, ValueError):
        pass  # The container stopped early: its result will say why.


def _read_limited(stream, sink: bytearray, limit: int, overflow) -> None:
    """Keep at most `limit` bytes, and drop the rest. Past it, signal an overflow.

    The limit applies while the container runs, so a flood of output never fills
    the memory of this computer. The pipe is still drained after the limit: if it
    were left full, docker.exe would block and `docker rm` could time out.
    """
    try:
        while chunk := stream.read1(_READ_CHUNK_BYTES):
            room = limit - len(sink)
            if room > 0:
                sink.extend(chunk[:room])
            if len(chunk) > room and overflow is not None:
                overflow.set()
    except (OSError, ValueError):
        pass


def _start(target, *args) -> threading.Thread:
    thread = threading.Thread(target=target, args=args, daemon=True)
    thread.start()
    return thread


def _join(threads: list[threading.Thread]) -> None:
    for thread in threads:
        thread.join(timeout=5)


def _docker(docker: Path, *args: str) -> subprocess.CompletedProcess | None:
    """Run a short Docker command. Return None if it could not run in time."""
    try:
        return subprocess.run(
            [str(docker), *args],
            capture_output=True,
            timeout=_DOCKER_COMMAND_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _safe_text(data: bytearray, limit: int = 300) -> str:
    text = bytes(data).decode("utf-8", errors="replace")
    text = "".join(c if c.isprintable() else " " for c in text)
    return " ".join(text.split())[:limit] or "no details."


def _to_float(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        return None
