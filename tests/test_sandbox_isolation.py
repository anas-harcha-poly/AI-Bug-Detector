"""Attack simulations against the real sandbox, to check that every protection holds.

Each attack is a string of Python code sent to run_tests: it only ever runs inside a
container, never on Windows. Most attacks are written as pytest tests that pass when
the protection holds. The rest are checked from outside: run status and Docker state.

Run them on request, one container at a time:
    .venv\\Scripts\\python.exe -m pytest -m docker tests/test_sandbox_isolation.py
"""

import json
import subprocess
import textwrap
import threading
import time

import pytest

from ai_bug_detector import docker_sandbox
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

# Set on Windows before each run: it must never be visible inside the container.
SECRET = "sk-test-sentinel-must-not-leak-4b1d"

COMMON = """\
import errno
import os
import socket
import stat
import subprocess
import time

import pytest


def proc_status():
    with open("/proc/self/status") as file:
        return dict(line.split(":\\t", 1) for line in file.read().splitlines())


def mounts():
    with open("/proc/mounts") as file:
        return [line.split() for line in file]


def walk_files():
    for root, dirs, files in os.walk("/"):
        if root == "/":
            dirs[:] = [d for d in dirs if d not in ("proc", "sys")]
        for name in dirs + files:
            yield os.path.join(root, name)
"""


@pytest.fixture(autouse=True)
def ready_sandbox(monkeypatch):
    monkeypatch.setenv(SANDBOX_VARIABLE, "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    monkeypatch.setenv("AI_BUG_DETECTOR_ENABLE_LIVE_AI", "1")
    status = sandbox_status()
    if status is not SandboxStatus.READY:
        pytest.fail(f"The sandbox is not ready: {status}")
    yield
    assert leftover_containers() == [], "A test container was left behind."


def docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(find_docker()), *args], capture_output=True, check=True, timeout=30
    )


def leftover_containers() -> list[bytes]:
    return docker("ps", "--all", "--quiet", "--filter", f"label={LABEL}").stdout.split()


def run_attack(tests: str):
    code = "VALUE = 1\n"
    test_file = COMMON + "\n\n" + textwrap.dedent(tests)
    return run_tests("target.py", code.encode(), "test_attack.py", test_file.encode())


def assert_protections_hold(result, expected_tests: int):
    assert result.status is RunStatus.COMPLETED, result
    problems = [
        f"{test.name}: {test.outcome}\n{test.message}"
        for test in result.tests
        if test.outcome is not TestOutcome.PASSED
    ]
    assert problems == [], "\n\n".join(problems)
    assert len(result.tests) == expected_tests


# --- Network: no Internet, no local network, no host ---


def test_no_network_access_of_any_kind():
    result = run_attack(
        """
        TARGETS = [
            ("1.1.1.1", 443),         # Internet
            ("8.8.8.8", 53),          # Internet DNS
            ("192.168.65.254", 80),   # Docker Desktop's address for Windows
            ("172.17.0.1", 80),       # Docker's default bridge gateway
            ("192.168.1.1", 80),      # A typical home router
            ("10.0.0.1", 80),         # A private network
        ]


        @pytest.mark.parametrize(("host", "port"), TARGETS)
        def test_tcp_connection_fails(host, port):
            with socket.socket() as connection:
                connection.settimeout(2)
                with pytest.raises(OSError):
                    connection.connect((host, port))


        def test_udp_send_fails():
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
                with pytest.raises(OSError):
                    connection.sendto(b"x", ("8.8.8.8", 53))


        @pytest.mark.parametrize(
            "name",
            ["example.com", "host.docker.internal", "gateway.docker.internal"],
        )
        def test_name_resolution_fails(name):
            with pytest.raises(OSError):
                socket.getaddrinfo(name, 80)


        def test_only_the_loopback_interface_exists():
            assert sorted(os.listdir("/sys/class/net")) == ["lo"]
        """
    )

    assert_protections_hold(result, expected_tests=11)


# --- Files of this computer, secrets and the Docker socket ---


def test_no_access_to_windows_files_secrets_or_docker():
    result = run_attack(
        f"""
        SECRET = {SECRET!r}


        @pytest.mark.parametrize(
            "path",
            [
                "/mnt/c", "/mnt/wsl", "/mnt/host", "/host_mnt", "/run/desktop",
                "/var/run/docker.sock", "/run/docker.sock", "/Users",
            ],
        )
        def test_host_path_does_not_exist(path):
            assert not os.path.exists(path)


        def test_home_folder_is_empty():
            assert os.listdir("/home") == []


        def test_no_host_file_system_is_mounted():
            shared_types = {{"9p", "drvfs", "virtiofs", "cifs", "smb3", "nfs"}}
            for _, target, fs_type, *_ in mounts():
                assert fs_type not in shared_types, target
                assert not fs_type.startswith("fuse"), target


        def test_no_socket_file_anywhere():
            for path in walk_files():
                if os.path.islink(path):
                    continue
                try:
                    mode = os.stat(path).st_mode
                except OSError:
                    continue
                assert not stat.S_ISSOCK(mode), path
                assert "docker.sock" not in path


        def test_no_windows_user_name_in_any_path():
            for path in walk_files():
                assert "anas" not in path.lower(), path


        def test_secret_is_not_in_the_environment():
            text = "\\n".join(f"{{k}}={{v}}" for k, v in os.environ.items())
            assert SECRET not in text
            assert "ANTHROPIC" not in text
            assert "AI_BUG_DETECTOR" not in text


        def test_secret_is_not_in_the_first_process_environment():
            with open("/proc/1/environ", "rb") as file:
                assert SECRET.encode() not in file.read()


        def test_environment_only_has_known_variables():
            known = {{
                "PATH", "HOSTNAME", "HOME", "LANG", "PYTHON_VERSION",
                "PYTHON_SHA256", "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
                "PIP_NO_CACHE_DIR", "PIP_DISABLE_PIP_VERSION_CHECK",
                "PYTEST_CURRENT_TEST", "PYTEST_VERSION",
            }}
            assert set(os.environ) <= known, set(os.environ) - known
        """
    )

    assert_protections_hold(result, expected_tests=15)


# --- Privileges and file system ---


def test_no_privileges_and_a_read_only_file_system():
    result = run_attack(
        """
        def test_runs_as_nobody():
            assert (os.getuid(), os.geteuid()) == (65534, 65534)
            assert (os.getgid(), os.getegid()) == (65534, 65534)
            assert set(os.getgroups()) <= {65534}


        @pytest.mark.parametrize(
            "field", ["CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"]
        )
        def test_no_linux_capabilities(field):
            assert int(proc_status()[field], 16) == 0


        def test_privileges_can_never_be_gained():
            assert proc_status()["NoNewPrivs"].strip() == "1"


        def test_seccomp_filter_is_active():
            assert proc_status()["Seccomp"].strip() == "2"


        def test_cannot_become_root():
            with pytest.raises(PermissionError):
                os.setuid(0)


        def test_root_file_system_is_mounted_read_only():
            (root,) = [m for m in mounts() if m[1] == "/"]
            assert "ro" in root[3].split(",")


        @pytest.mark.parametrize(
            "path",
            ["/etc/passwd", "/opt/sandbox/runner.py", "/opt/sandbox/pytest.ini",
             "/usr/local/bin/python3"],
        )
        def test_system_files_cannot_be_changed(path):
            with pytest.raises(OSError):
                open(path, "a").close()


        @pytest.mark.parametrize(
            "folder", ["/", "/opt/sandbox", "/etc", "/usr", "/var/tmp", "/root"]
        )
        def test_no_file_can_be_created_outside_tmp(folder):
            with pytest.raises(OSError):
                open(os.path.join(folder, "created.txt"), "w").close()


        def test_only_tmp_is_writable():
            writable = set()
            for path in walk_files():
                # Links such as /dev/fd point into /proc: they hold no data.
                if os.path.islink(path):
                    continue
                if os.path.isdir(path) and os.access(path, os.W_OK):
                    writable.add(path)
            outside_tmp = {p for p in writable if not p.startswith("/tmp")}
            # Docker always mounts /dev/mqueue. It only holds message queues, and
            # the next test checks that none can be created.
            assert outside_tmp <= {"/dev/mqueue"}


        def test_no_message_queue_can_be_created():
            with pytest.raises(OSError):
                open("/dev/mqueue/stored-data", "w").close()


        def test_programs_cannot_be_run_from_tmp():
            with open("/bin/true", "rb") as source:
                program = source.read()
            with open("/tmp/program", "wb") as copy:
                copy.write(program)
            os.chmod("/tmp/program", 0o755)
            with pytest.raises(PermissionError):
                subprocess.run(["/tmp/program"])


        def test_tmp_space_is_limited():
            written = 0
            chunk = b"x" * (1024 * 1024)
            with pytest.raises(OSError) as error:
                with open("/tmp/filler", "wb") as file:
                    for _ in range(200):
                        file.write(chunk)
                        file.flush()
                        written += len(chunk)
            os.remove("/tmp/filler")
            assert error.value.errno == errno.ENOSPC
            assert written <= 64 * 1024 * 1024
        """
    )

    assert_protections_hold(result, expected_tests=24)


# --- Resource limits that the tested code can observe ---


def test_process_cpu_and_file_limits():
    result = run_attack(
        """
        def test_process_count_is_limited():
            children = []
            try:
                with pytest.raises(OSError):
                    for _ in range(200):
                        pid = os.fork()
                        if pid == 0:
                            time.sleep(10)
                            os._exit(0)
                        children.append(pid)
            finally:
                for pid in children:
                    os.kill(pid, 9)
                    os.waitpid(pid, 0)
            assert len(children) < 64


        def test_only_one_cpu_can_be_used():
            # Four busy processes for 2 seconds: about 8 s of CPU without a limit.
            children = []
            for _ in range(4):
                pid = os.fork()
                if pid == 0:
                    end = time.monotonic() + 2
                    while time.monotonic() < end:
                        pass
                    os._exit(0)
                children.append(pid)
            for pid in children:
                os.waitpid(pid, 0)
            times = os.times()
            assert times.children_user + times.children_system < 3


        def test_open_files_are_limited():
            files = []
            try:
                with pytest.raises(OSError) as error:
                    for _ in range(1000):
                        files.append(open("/dev/null"))
            finally:
                for file in files:
                    file.close()
            assert error.value.errno == errno.EMFILE
            assert len(files) < 256


        def test_memory_below_the_limit_is_allowed():
            data = b"x" * (120 * 1024 * 1024)
            assert len(data) == 120 * 1024 * 1024
        """
    )

    assert_protections_hold(result, expected_tests=4)


# --- Limits enforced from outside: the run is stopped and reported ---


def test_too_much_memory_stops_the_run():
    result = run_attack(
        """
        def test_allocate_too_much():
            data = b"x" * (400 * 1024 * 1024)
            assert data
        """
    )

    assert result.status is RunStatus.MEMORY_LIMIT


def test_infinite_loop_is_stopped_at_the_time_limit(monkeypatch):
    monkeypatch.setattr(docker_sandbox, "TIMEOUT_SECONDS", 5)
    start = time.monotonic()

    result = run_attack(
        """
        def test_never_ends():
            while True:
                pass
        """
    )

    assert result.status is RunStatus.TIMED_OUT
    assert time.monotonic() - start < 25


def test_output_flood_after_the_result_is_stopped():
    start = time.monotonic()

    result = run_attack(
        """
        import atexit


        def flood():
            while True:
                os.write(1, b"x" * 65536)


        def test_register_flood():
            atexit.register(flood)
        """
    )

    assert result.status is RunStatus.OUTPUT_LIMIT
    assert time.monotonic() - start < 25


def test_background_process_does_not_keep_the_container_alive():
    start = time.monotonic()

    result = run_attack(
        """
        def test_leave_a_process_behind():
            subprocess.Popen(["sleep", "600"], start_new_session=True)
        """
    )

    assert_protections_hold(result, expected_tests=1)
    assert time.monotonic() - start < 20


def test_pytest_failure_and_syntax_error_also_clean_up():
    failing = run_attack("def test_fails():\n    assert False\n")
    broken = run_attack("def test_broken(:\n")

    assert failing.count(TestOutcome.FAILED) == 1
    assert broken.status is RunStatus.PYTEST_ERROR


# --- The limits Docker really applied, read from outside while a run is going on ---


def test_docker_applied_every_setting(monkeypatch):
    monkeypatch.setattr(docker_sandbox, "TIMEOUT_SECONDS", 20)
    results = []
    attack = threading.Thread(
        target=lambda: results.append(
            run_attack("def test_wait():\n    time.sleep(6)\n")
        )
    )
    attack.start()

    config = None
    deadline = time.monotonic() + 15
    while config is None and time.monotonic() < deadline:
        ids = leftover_containers()
        if ids:
            inspected = docker("inspect", ids[0].decode()).stdout
            config = json.loads(inspected)[0]
        else:
            time.sleep(0.2)
    attack.join()

    assert config is not None, "The container was never seen running."
    host = config["HostConfig"]
    assert host["NetworkMode"] == "none"
    assert host["ReadonlyRootfs"] is True
    assert host["Privileged"] is False
    assert host["CapDrop"] == ["ALL"]
    assert not host["CapAdd"]
    assert "no-new-privileges" in host["SecurityOpt"]
    assert host["Memory"] == 256 * 1024 * 1024
    assert host["MemorySwap"] == 256 * 1024 * 1024
    assert host["NanoCpus"] == 1_000_000_000
    assert host["PidsLimit"] == 64
    assert not host["Binds"]
    assert not host.get("Mounts")
    assert host["IpcMode"] == "none"
    assert not host["PortBindings"]
    assert not host["DeviceRequests"]  # No GPU.
    assert host["Runtime"] == "runc"
    assert host["LogConfig"]["Type"] == "none"
    assert host["Init"] is True
    assert config["Config"]["User"] == "65534:65534"
    assert config["Mounts"] == []
    assert results[0].status is RunStatus.COMPLETED
