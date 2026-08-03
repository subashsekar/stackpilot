"""Release-quality coverage for Stackfile discovery, process trees, and public API."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from stackpilot import (
    HealthCheck,
    HttpHealthCheck,
    ProcessHealthCheck,
    Runner,
    ServiceSpec,
    Stack,
    TcpHealthCheck,
    parse_health_check,
)
from stackpilot.config import coerce_health_check
from stackpilot.discovery import (
    STACKFILE_NAME,
    discover_project,
    find_stackfile,
)
from stackpilot.logger import Logger
from stackpilot.models import ServiceState
from stackpilot.process_manager import ProcessManager, signal_process_tree
from stackpilot.utils import load_stack_from_stackfile, split_command


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        # OpenProcess / wait: use tasklist-style poll via os.kill(pid, 0) which
        # raises OSError when the process is gone on Windows as well.
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        # On Windows, os.kill(pid, 0) may succeed for zombies; double-check
        # with tasklist when available.
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                check=False,
            )
            out = (result.stdout or "").strip()
            return str(pid) in out and "No tasks" not in out
        except OSError:
            return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _wait_until(predicate, *, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


class TestStackfileDiscovery:
    def test_discovers_stackfile_not_stackpilot_py(self, tmp_path: Path) -> None:
        """Only Stackfile.py is discovered; stackpilot.py must not win."""

        (tmp_path / "stackpilot.py").write_text(
            "raise RuntimeError('shadow')\n",
            encoding="utf-8",
        )
        stackfile = tmp_path / STACKFILE_NAME
        stackfile.write_text(
            "from stackpilot import Stack\n\nstack = Stack()\nstack.run()\n",
            encoding="utf-8",
        )

        found = find_stackfile(tmp_path)
        assert found == stackfile.resolve()
        project = discover_project(tmp_path)
        assert project.stackfile == stackfile.resolve()
        assert project.root == tmp_path.resolve()

    def test_load_requires_no_config_flag(self, tmp_path: Path) -> None:
        """Loader works from path discovery alone (no -c argument)."""

        stackfile = tmp_path / STACKFILE_NAME
        stackfile.write_text(
            "from stackpilot import Stack\n"
            "\n"
            "stack = Stack()\n"
            "stack.service(name='demo', path='.', command='python -c \"pass\"')\n"
            "stack.run()\n",
            encoding="utf-8",
        )
        nested = tmp_path / "apps" / "api"
        nested.mkdir(parents=True)

        project = discover_project(nested)
        stack = load_stack_from_stackfile(project.stackfile)
        assert [s.name for s in stack.services] == ["demo"]
        assert stack.run_requested is True


class TestCommandParsing:
    def test_platform_aware_quoted_python_c(self) -> None:
        argv = split_command('python -c "print(1)"')
        assert argv[0] == sys.executable
        assert argv[1] == "-c"
        assert argv[2] == "print(1)"

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows argv parsing")
    def test_windows_paths_with_spaces(self) -> None:
        """Win32 CommandLineToArgvW keeps quoted path segments intact."""

        argv = split_command(r'"C:\Program Files\App\tool.exe" --flag "a b"')
        assert argv[0] == r"C:\Program Files\App\tool.exe"
        assert argv[1] == "--flag"
        assert argv[2] == "a b"

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX shlex escaping")
    def test_posix_keeps_backslash_escapes(self) -> None:
        argv = split_command(r"echo hello\ world")
        assert argv == ["echo", "hello world"]

    def test_never_uses_shlex_posix_true_on_windows(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: Windows must not call shlex.split(..., posix=True)."""

        if sys.platform != "win32":
            pytest.skip("Windows-only regression check")

        calls: list[object] = []

        real_split = __import__("shlex").split

        def tracking_split(s, **kwargs):  # noqa: ANN001
            calls.append(kwargs)
            return real_split(s, **kwargs)

        monkeypatch.setattr("shlex.split", tracking_split)
        argv = split_command('python -c "print(1)"')
        assert argv[2] == "print(1)"
        assert calls == []


class TestProcessGroupAndChildCleanup:
    def test_spawn_uses_process_group_isolation(self, tmp_path: Path) -> None:
        logger = Logger(tmp_path / "logs", service_names=["svc"])
        manager = ProcessManager(logger, stop_timeout_s=3.0)
        spec = ServiceSpec(
            name="svc",
            path=tmp_path,
            command='python -c "import time; time.sleep(30)"',
        )
        managed = manager.start(spec)
        assert managed.process is not None
        proc = managed.process
        assert proc.pid is not None

        if sys.platform != "win32":
            # start_new_session=True → child is its own session/group leader.
            assert os.getpgid(proc.pid) == proc.pid

        manager.stop("svc")
        assert managed.state == ServiceState.STOPPED
        assert not _pid_alive(proc.pid)
        logger.close()

    def test_stop_kills_grandchild_processes(self, tmp_path: Path) -> None:
        """Stopping a service must not leave orphan child processes."""

        # Isolate from pytest's console: Job Object / process-group teardown
        # can otherwise surface as KeyboardInterrupt in the test runner.
        child_pid_file = tmp_path / "child.pid"
        parent_spawn = tmp_path / "parent_spawn.py"
        parent_spawn.write_text(
            "import subprocess\n"
            "import sys\n"
            "import time\n"
            "from pathlib import Path\n"
            "\n"
            f"child_pid_file = Path(r'{child_pid_file}')\n"
            "child = subprocess.Popen(\n"
            "    [sys.executable, '-c', 'import time; time.sleep(120)']\n"
            ")\n"
            "child_pid_file.write_text(str(child.pid), encoding='utf-8')\n"
            "time.sleep(120)\n",
            encoding="utf-8",
        )

        runner = tmp_path / "tree_case.py"
        alive_block = (
            "def alive(pid: int) -> bool:\n"
            "    import subprocess as sp\n"
            "    r = sp.run(\n"
            "        ['tasklist', '/FI', f'PID eq {pid}', '/NH'],\n"
            "        capture_output=True, text=True, check=False,\n"
            "    )\n"
            "    out = (r.stdout or '').strip()\n"
            "    return str(pid) in out and 'No tasks' not in out\n"
            if sys.platform == "win32"
            else "def alive(pid: int) -> bool:\n"
            "    try:\n"
            "        os.kill(pid, 0)\n"
            "    except OSError:\n"
            "        return False\n"
            "    return True\n"
        )
        runner.write_text(
            "from __future__ import annotations\n"
            "import os\n"
            "import time\n"
            "from pathlib import Path\n"
            "\n"
            "from stackpilot.config import ServiceSpec\n"
            "from stackpilot.logger import Logger\n"
            "from stackpilot.process_manager import ProcessManager\n"
            "\n"
            f"tmp = Path(r'{tmp_path}')\n"
            f"child_pid_file = Path(r'{child_pid_file}')\n"
            "logger = Logger(tmp / 'logs', service_names=['parent'])\n"
            "manager = ProcessManager(logger, stop_timeout_s=5.0)\n"
            "spec = ServiceSpec(\n"
            "    name='parent',\n"
            "    path=tmp,\n"
            "    command='python parent_spawn.py',\n"
            ")\n"
            "managed = manager.start(spec)\n"
            "parent_pid = managed.pid\n"
            "deadline = time.monotonic() + 5\n"
            "while time.monotonic() < deadline and not child_pid_file.exists():\n"
            "    time.sleep(0.05)\n"
            "child_pid = int(child_pid_file.read_text(encoding='utf-8').strip())\n"
            "manager.stop('parent')\n"
            f"{alive_block}"
            "deadline = time.monotonic() + 5\n"
            "while time.monotonic() < deadline and (\n"
            "    alive(parent_pid) or alive(child_pid)\n"
            "):\n"
            "    time.sleep(0.05)\n"
            "assert not alive(parent_pid), parent_pid\n"
            "assert not alive(child_pid), child_pid\n"
            "logger.close()\n"
            "print('OK', flush=True)\n",
            encoding="utf-8",
        )

        env = {
            **os.environ,
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        }
        proc = subprocess.run(
            [sys.executable, str(runner)],
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            env=env,
        )
        assert proc.returncode == 0, proc.stdout + "\n" + proc.stderr
        assert "OK" in (proc.stdout or "")

    def test_signal_process_tree_force_is_safe_for_missing_pid(self) -> None:
        # Extremely unlikely live PID; should not raise.
        signal_process_tree(2_147_483_646, graceful=False)


class TestPublicApiExports:
    def test_public_imports(self) -> None:
        assert Stack is not None
        assert ServiceSpec is not None
        assert Runner is not None
        assert HealthCheck is not None
        assert ProcessHealthCheck is not None
        assert HttpHealthCheck is not None
        assert TcpHealthCheck is not None
        assert callable(parse_health_check)

    def test_parse_health_check_models_and_dicts(self) -> None:
        http = parse_health_check({"type": "http", "url": "http://127.0.0.1/health"})
        assert isinstance(http, HttpHealthCheck)
        assert http.url == "http://127.0.0.1/health"

        tcp = parse_health_check({"type": "tcp", "host": "127.0.0.1", "port": 5432})
        assert isinstance(tcp, TcpHealthCheck)
        assert tcp.port == 5432

        proc = coerce_health_check({"type": "process", "interval": 0.5})
        assert isinstance(proc, ProcessHealthCheck)
        assert proc.interval == 0.5

    def test_stack_service_accepts_typed_health_check(self, tmp_path: Path) -> None:
        stack = Stack()
        stack.service(
            name="api",
            path=tmp_path,
            command="python -c pass",
            health_check=HttpHealthCheck(
                url="http://127.0.0.1:9/health",
                interval=0.1,
                timeout=1.0,
            ),
        )
        assert isinstance(stack.services[0].health_check, HttpHealthCheck)

    def test_stack_run_as_import_does_not_start_runner(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CLI-style import: stack.run() only sets the flag."""

        calls: list[object] = []

        class FakeRunner:
            def run(self, stack, *, target=None):  # noqa: ANN001
                calls.append(stack)
                return 0

        monkeypatch.setattr("stackpilot.runner.Runner", FakeRunner)
        stack = Stack()
        stack.service(name="x", path=tmp_path, command="python -c pass")
        returned = stack.run()
        assert returned is stack
        assert stack.run_requested is True
        assert calls == []
