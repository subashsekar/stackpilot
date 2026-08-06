"""Regression tests for P0 release blockers (false healthy, circular UX, cleanup)."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import pytest

from stackpilot.config import HttpHealthCheck, ProcessHealthCheck, ServiceSpec, Stack, TcpHealthCheck
from stackpilot.dependency_graph import (
    CircularDependencyError,
    DependencyError,
    build_graph,
)
from stackpilot.diagnostics.errors import (
    format_circular_dependency_error,
    format_cleanup_failure,
    format_port_already_in_use,
)
from stackpilot.health import Health, HealthCheckTimeout, PortOwnershipError
from stackpilot.logger import Logger
from stackpilot.models import ServiceState
from stackpilot.orchestrator import Orchestrator
from stackpilot.port_detect import pid_tree_owns_port, pids_listening_on_port
from stackpilot.process_manager import ProcessManager
from stackpilot.runner import Runner
from stackpilot.runtime_control import (
    clear_runtime_session,
    stop_runtime_session,
)
from stackpilot.status import pid_is_alive, runtime_status_path, save_runtime_snapshot
from stackpilot.watch_manager import WatchManager


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def _sleep_cmd(seconds: float = 60.0) -> str:
    return f'{sys.executable} -c "import time; time.sleep({seconds})"'


def _http_server_script(path: Path, port: int, *, delay_s: float = 0.0) -> Path:
    path.write_text(
        "import time\n"
        "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
        f"PORT = {int(port)}\n"
        f"time.sleep({float(delay_s)})\n"
        "class H(BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        self.send_response(200)\n"
        "        self.end_headers()\n"
        "        self.wfile.write(b'ok')\n"
        "    def log_message(self, *a):\n"
        "        pass\n"
        "HTTPServer(('127.0.0.1', PORT), H).serve_forever()\n",
        encoding="utf-8",
    )
    return path


def _bind_runner(
    tmp_path: Path,
    specs: list[ServiceSpec],
) -> tuple[Runner, ProcessManager, Logger, WatchManager]:
    stack = Stack()
    for spec in specs:
        stack.service(
            name=spec.name,
            path=str(spec.path),
            command=spec.command,
            health_check=spec.health_check,
            depends_on=list(spec.depends_on),
            port=spec.port,
        )
    logger = Logger(
        tmp_path / "issues",
        service_names=[s.name for s in specs],
        auto_cleanup=False,
    )
    manager = ProcessManager(logger, stop_timeout_s=3.0)
    watch = WatchManager()
    runner = Runner(poll_interval_s=0.05)
    runner.bind(
        manager=manager,
        graph=build_graph(stack),
        watch_manager=watch,
        project_root=tmp_path,
        ordered=list(stack.services),
        logger=logger,
    )
    return runner, manager, logger, watch


def _start_foreign_http(port: int) -> tuple[HTTPServer, threading.Thread]:
    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"foreign")

        def log_message(self, *a):  # noqa: A003
            return

    srv = HTTPServer(("127.0.0.1", port), H)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    return srv, thread


# ---------------------------------------------------------------------------
# P0-1 — False healthy / foreign listener
# ---------------------------------------------------------------------------


class TestFalseHealthyDetection:
    def test_foreign_process_occupies_port_run_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        port = _free_port()
        srv, _ = _start_foreign_http(port)
        printed: list[str] = []
        monkeypatch.setattr(
            "builtins.print",
            lambda *a, **k: printed.append(" ".join(str(x) for x in a)),
        )
        try:
            script = _http_server_script(tmp_path / "api.py", port, delay_s=0.2)
            # Child will fail to bind; foreign /health still responds 200.
            stack = Stack()
            stack.service(
                name="api",
                path=str(tmp_path),
                command=f"{sys.executable} {script.name}",
                port=port,
                health_check=HttpHealthCheck(
                    url=f"http://127.0.0.1:{port}/",
                    timeout=3.0,
                    interval=0.1,
                    probe_timeout=0.5,
                ),
            )
            code = Orchestrator(poll_interval_s=0.05).run(
                stack, project_root=tmp_path
            )
            joined = "\n".join(printed)
            assert code == 1
            assert "Port already in use" in joined
            assert f'Service "api" requires {port}.' in joined
            assert "Current owner:" in joined
            assert "PID " in joined
            assert "stackpilot stop" in joined
            assert "healthy" not in joined.lower() or "Port already in use" in joined
            assert "Watching for changes" not in joined
        finally:
            srv.shutdown()

    def test_correct_process_owns_port_run_succeeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        port = _free_port()
        printed: list[str] = []
        monkeypatch.setattr(
            "builtins.print",
            lambda *a, **k: printed.append(" ".join(str(x) for x in a)),
        )
        script = _http_server_script(tmp_path / "api.py", port, delay_s=0.15)
        specs = [
            ServiceSpec(
                name="api",
                path=tmp_path,
                command=f"{sys.executable} {script.name}",
                port=port,
                health_check=HttpHealthCheck(
                    url=f"http://127.0.0.1:{port}/",
                    timeout=8.0,
                    interval=0.1,
                    probe_timeout=0.5,
                ),
            )
        ]
        runner, manager, logger, watch = _bind_runner(tmp_path, specs)
        try:
            assert runner.start_all(specs) is True
            joined = "\n".join(printed)
            assert "api healthy" in joined
            assert "Port already in use" not in joined
            owners = pids_listening_on_port(port)
            managed = manager.get("api")
            assert managed.pid is not None
            assert pid_tree_owns_port(managed.pid, port) is True
            assert owners  # listener present
        finally:
            manager.stop_all()
            watch.stop()
            runner.unbind()
            logger.close()

    def test_child_process_ownership_handled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Parent spawns a child that binds the port — tree ownership must pass."""

        port = _free_port()
        printed: list[str] = []
        monkeypatch.setattr(
            "builtins.print",
            lambda *a, **k: printed.append(" ".join(str(x) for x in a)),
        )
        parent = tmp_path / "parent.py"
        child = tmp_path / "child.py"
        _http_server_script(child, port, delay_s=0.05)
        parent.write_text(
            "import subprocess, sys, time\n"
            f"child = subprocess.Popen([sys.executable, r'{child}'])\n"
            "time.sleep(60)\n",
            encoding="utf-8",
        )
        specs = [
            ServiceSpec(
                name="api",
                path=tmp_path,
                command=f"{sys.executable} {parent.name}",
                port=port,
                health_check=HttpHealthCheck(
                    url=f"http://127.0.0.1:{port}/",
                    timeout=10.0,
                    interval=0.15,
                    probe_timeout=0.5,
                ),
            )
        ]
        runner, manager, logger, watch = _bind_runner(tmp_path, specs)
        try:
            assert runner.start_all(specs) is True
            assert "api healthy" in "\n".join(printed)
            managed = manager.get("api")
            assert managed.pid is not None
            assert pid_tree_owns_port(managed.pid, port) is True
        finally:
            manager.stop_all()
            watch.stop()
            runner.unbind()
            logger.close()

    def test_no_false_healthy_state_on_foreign_tcp(
        self, tmp_path: Path
    ) -> None:
        port = _free_port()
        # Foreign TCP listener.
        foreign = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        foreign.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        foreign.bind(("127.0.0.1", port))
        foreign.listen(1)
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=str(tmp_path),
        )
        try:
            with pytest.raises(PortOwnershipError) as exc:
                Health.wait_until_healthy(
                    "db",
                    TcpHealthCheck(
                        host="127.0.0.1",
                        port=port,
                        timeout=2.0,
                        interval=0.1,
                        probe_timeout=0.2,
                    ),
                    process=proc,
                )
            assert exc.value.port == port
            assert pid_tree_owns_port(proc.pid, port) is False
        finally:
            foreign.close()
            proc.kill()
            proc.wait(timeout=5)

    def test_format_port_already_in_use_copy(self) -> None:
        text = format_port_already_in_use(
            port=8080,
            service="api",
            owners=((14820, "python.exe"),),
        )
        assert "Problem: Port already in use" in text
        assert 'Service "api" requires 8080.' in text
        assert "Current owner:" in text
        assert "PID 14820" in text
        assert "python.exe" in text
        assert "stackpilot stop" in text
        assert "change the service port" in text


# ---------------------------------------------------------------------------
# P0-2 — Circular dependency error precedence / UX
# ---------------------------------------------------------------------------


class TestCircularDependencyUX:
    def test_simple_cycle_labeled_not_configuration(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        printed: list[str] = []
        monkeypatch.setattr(
            "builtins.print",
            lambda *a, **k: printed.append(" ".join(str(x) for x in a)),
        )
        stack = Stack()
        stack.service(
            name="a", path=str(tmp_path), command=_sleep_cmd(1), depends_on=["b"]
        )
        stack.service(
            name="b", path=str(tmp_path), command=_sleep_cmd(1), depends_on=["a"]
        )
        code = Orchestrator(poll_interval_s=0.05).run(stack, project_root=tmp_path)
        joined = "\n".join(printed)
        assert code == 1
        assert "Circular dependency detected" in joined
        assert "Configuration error" not in joined
        assert "Add stack.service" not in joined
        assert "Remove one dependency to break the cycle." in joined

    def test_long_cycle_path_rendered(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        printed: list[str] = []
        monkeypatch.setattr(
            "builtins.print",
            lambda *a, **k: printed.append(" ".join(str(x) for x in a)),
        )
        names = ["gateway", "auth", "users", "billing", "gateway"]
        stack = Stack()
        stack.service(
            name="gateway",
            path=str(tmp_path),
            command=_sleep_cmd(1),
            depends_on=["billing"],
        )
        stack.service(
            name="auth",
            path=str(tmp_path),
            command=_sleep_cmd(1),
            depends_on=["gateway"],
        )
        stack.service(
            name="users",
            path=str(tmp_path),
            command=_sleep_cmd(1),
            depends_on=["auth"],
        )
        stack.service(
            name="billing",
            path=str(tmp_path),
            command=_sleep_cmd(1),
            depends_on=["users"],
        )
        code = Orchestrator(poll_interval_s=0.05).run(stack, project_root=tmp_path)
        joined = "\n".join(printed)
        assert code == 1
        assert "Circular dependency detected" in joined
        for name in ("gateway", "auth", "users", "billing"):
            assert name in joined
        assert " ↓" in joined or "↓" in joined

    def test_diamond_graph_orders_ok(self, tmp_path: Path) -> None:
        stack = Stack()
        stack.service(name="a", path=str(tmp_path), command=_sleep_cmd(1))
        stack.service(
            name="b", path=str(tmp_path), command=_sleep_cmd(1), depends_on=["a"]
        )
        stack.service(
            name="c", path=str(tmp_path), command=_sleep_cmd(1), depends_on=["a"]
        )
        stack.service(
            name="d",
            path=str(tmp_path),
            command=_sleep_cmd(1),
            depends_on=["b", "c"],
        )
        graph = build_graph(stack)
        order = list(graph.topological_order())
        assert order.index("a") < order.index("b")
        assert order.index("a") < order.index("c")
        assert order.index("b") < order.index("d")
        assert order.index("c") < order.index("d")

    def test_shared_dependency_orders_ok(self, tmp_path: Path) -> None:
        stack = Stack()
        stack.service(name="db", path=str(tmp_path), command=_sleep_cmd(1))
        stack.service(
            name="api", path=str(tmp_path), command=_sleep_cmd(1), depends_on=["db"]
        )
        stack.service(
            name="worker",
            path=str(tmp_path),
            command=_sleep_cmd(1),
            depends_on=["db"],
        )
        order = list(build_graph(stack).topological_order())
        assert order.index("db") < order.index("api")
        assert order.index("db") < order.index("worker")

    def test_disconnected_graph_orders_ok(self, tmp_path: Path) -> None:
        stack = Stack()
        stack.service(name="a", path=str(tmp_path), command=_sleep_cmd(1))
        stack.service(name="b", path=str(tmp_path), command=_sleep_cmd(1))
        order = list(build_graph(stack).topological_order())
        assert set(order) == {"a", "b"}

    def test_dependency_error_precedence_over_value_error(self) -> None:
        assert issubclass(DependencyError, ValueError)
        assert issubclass(CircularDependencyError, DependencyError)
        text = format_circular_dependency_error(
            ["gateway", "auth", "users", "gateway"]
        )
        assert text.startswith("Problem: Circular dependency detected")
        assert "gateway" in text
        assert "Suggested fix: Remove one dependency to break the cycle." in text


# ---------------------------------------------------------------------------
# P0-3 — Process cleanup / orphans / runtime.json
# ---------------------------------------------------------------------------


class TestProcessCleanup:
    def _spawn_n(
        self, tmp_path: Path, n: int
    ) -> tuple[Runner, ProcessManager, Logger, WatchManager, list[int]]:
        specs = [
            ServiceSpec(
                name=f"svc{i}",
                path=tmp_path,
                command=_sleep_cmd(60),
                health_check=ProcessHealthCheck(timeout=5.0, interval=0.05),
            )
            for i in range(n)
        ]
        runner, manager, logger, watch = _bind_runner(tmp_path, specs)
        assert runner.start_all(specs) is True
        pids = []
        for managed in manager.services():
            assert managed.pid is not None
            pids.append(managed.pid)
        return runner, manager, logger, watch, pids

    @pytest.mark.parametrize("n", [3, 10, 20])
    def test_stop_n_services_no_orphans(self, tmp_path: Path, n: int) -> None:
        runner, manager, logger, watch, pids = self._spawn_n(tmp_path, n)
        try:
            code = runner.shutdown(logger, force=False)
            assert code in {1, 130}
            for pid in pids:
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline and pid_is_alive(pid):
                    time.sleep(0.05)
                assert not pid_is_alive(pid), f"orphan pid {pid}"
            for managed in manager.services():
                assert managed.state in (ServiceState.STOPPED, ServiceState.FAILED)
        finally:
            manager.stop_all(timeout_s=0.1)
            watch.stop()
            runner.unbind()
            logger.close()

    def test_forced_kill_cleans_stubborn(self, tmp_path: Path) -> None:
        if sys.platform == "win32":
            cmd = _sleep_cmd(60)
        else:
            cmd = (
                f"{sys.executable} -c "
                "\"import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                'time.sleep(60)"'
            )
        specs = [
            ServiceSpec(
                name="stubborn",
                path=tmp_path,
                command=cmd,
                health_check=ProcessHealthCheck(timeout=5.0, interval=0.05),
            )
        ]
        runner, manager, logger, watch = _bind_runner(tmp_path, specs)
        try:
            assert runner.start_all(specs) is True
            pid = manager.get("stubborn").pid
            code = runner.shutdown(logger, force=True)
            assert code in {1, 130}
            if pid is not None:
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline and pid_is_alive(pid):
                    time.sleep(0.05)
                assert not pid_is_alive(pid)
        finally:
            manager.stop_all(timeout_s=0.1)
            watch.stop()
            runner.unbind()
            logger.close()

    def test_double_ctrl_c_completes_cleanup(self, tmp_path: Path) -> None:
        stack = Stack()
        stack.service(name="demo", path=str(tmp_path), command=_sleep_cmd(60))
        orch = Orchestrator(poll_interval_s=0.05)
        real_runner = Runner(poll_interval_s=0.05)
        logger = Logger(tmp_path / "issues", service_names=["demo"], auto_cleanup=False)
        manager = ProcessManager(logger)
        watch = WatchManager()
        real_runner.bind(
            manager=manager,
            graph=build_graph(stack),
            watch_manager=watch,
            project_root=tmp_path,
            ordered=list(stack.services),
            logger=logger,
        )
        managed = manager.start(list(stack.services)[0])
        pid = managed.pid
        try:
            orch._runner = real_runner
            orch._logger = logger
            orch._watch_manager = watch
            orch._cleanup_done = False

            calls = {"n": 0}
            original = real_runner.shutdown

            def flaky(logger_, *, force: bool = False) -> int:  # noqa: ANN001
                calls["n"] += 1
                if calls["n"] == 1:
                    raise KeyboardInterrupt
                return original(logger_, force=True)

            real_runner.shutdown = flaky  # type: ignore[method-assign]
            code = orch.stop()
            assert orch._cleanup_done is True
            assert code in {0, 1, 130}
            if pid is not None:
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline and pid_is_alive(pid):
                    time.sleep(0.05)
                assert not pid_is_alive(pid)
        finally:
            try:
                manager.stop_all(timeout_s=0.1)
            except Exception:
                pass
            try:
                watch.stop()
            except Exception:
                pass
            try:
                real_runner.unbind()
            except Exception:
                pass
            logger.close()
            if pid is not None and pid_is_alive(pid):
                from stackpilot.process_tree import signal_process_tree

                signal_process_tree(int(pid), graceful=False)

    def test_abnormal_exit_reaped_no_zombie(self, tmp_path: Path) -> None:
        logger = Logger(tmp_path / "logs", service_names=["svc"], auto_cleanup=False)
        manager = ProcessManager(logger, stop_timeout_s=3.0)
        spec = ServiceSpec(
            name="svc",
            path=tmp_path,
            command=f'{sys.executable} -c "raise SystemExit(1)"',
        )
        manager.start(spec)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            manager.reap_exited()
            if manager.get("svc").state in (ServiceState.STOPPED, ServiceState.FAILED):
                break
            time.sleep(0.05)
        assert manager.get("svc").state in (ServiceState.STOPPED, ServiceState.FAILED)
        assert manager.get("svc").process is None or manager.get("svc").process.poll() is not None
        logger.close()

    def test_no_orphan_listeners_after_stop(self, tmp_path: Path) -> None:
        port = _free_port()
        script = _http_server_script(tmp_path / "api.py", port, delay_s=0.05)
        specs = [
            ServiceSpec(
                name="api",
                path=tmp_path,
                command=f"{sys.executable} {script.name}",
                port=port,
                health_check=HttpHealthCheck(
                    url=f"http://127.0.0.1:{port}/",
                    timeout=8.0,
                    interval=0.1,
                ),
            )
        ]
        runner, manager, logger, watch = _bind_runner(tmp_path, specs)
        try:
            assert runner.start_all(specs) is True
            assert pids_listening_on_port(port)
            runner.shutdown(logger, force=True)
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and pids_listening_on_port(port):
                time.sleep(0.05)
            assert not pids_listening_on_port(port)
        finally:
            manager.stop_all(timeout_s=0.1)
            watch.stop()
            runner.unbind()
            logger.close()

    def test_runtime_json_cleaned_by_stop_session(self, tmp_path: Path) -> None:
        # Intentionally omit start_new_session so the child shares this
        # process group — stop_runtime_session must still terminate only
        # the recorded PID (not killpg the pytest group → exit 143 on CI).
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            cwd=str(tmp_path),
        )
        try:
            save_runtime_snapshot(
                tmp_path,
                {
                    "session_active": True,
                    "services": [
                        {
                            "name": "api",
                            "pid": proc.pid,
                            "port": 0,
                            "status": "RUNNING",
                            "command": "sleep",
                        }
                    ],
                },
            )
            result = stop_runtime_session(tmp_path, timeout_s=2.0)
            assert result.exit_code == 0
            assert "Stopped 1 service." in result.message
            assert "No orphan processes detected." in result.message
            assert not pid_is_alive(proc.pid)
            payload = runtime_status_path(tmp_path)
            assert payload.is_file()
            import json

            data = json.loads(payload.read_text(encoding="utf-8"))
            assert data.get("session_active") is False
            assert data.get("services") == []
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=3)

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX shared-PG regression")
    def test_stop_runtime_session_does_not_signal_pytest_group(
        self, tmp_path: Path
    ) -> None:
        """Shared process-group children must not take down the test runner."""

        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            cwd=str(tmp_path),
        )
        assert proc.pid is not None
        assert os.getpgid(proc.pid) == os.getpgid(os.getpid())
        try:
            save_runtime_snapshot(
                tmp_path,
                {
                    "session_active": True,
                    "services": [
                        {
                            "name": "api",
                            "pid": proc.pid,
                            "port": 0,
                            "status": "RUNNING",
                            "command": "sleep",
                        }
                    ],
                },
            )
            result = stop_runtime_session(tmp_path, timeout_s=2.0)
            assert result.exit_code == 0
            assert not pid_is_alive(proc.pid)
            # If killpg hit our group we would not reach these asserts.
            assert os.getpid() > 0
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=3)

    def test_stop_reports_remaining_pids_on_failure(self, tmp_path: Path) -> None:
        save_runtime_snapshot(
            tmp_path,
            {
                "session_active": True,
                "services": [
                    {
                        "name": "ghost",
                        "pid": 424242,
                        "status": "RUNNING",
                        "command": "x",
                    }
                ],
            },
        )
        with (
            patch("stackpilot.runtime_control.pid_is_alive", return_value=True),
            patch("stackpilot.runtime_control.signal_process_tree"),
            patch("stackpilot.runtime_control._wait_until_dead"),
        ):
            result = stop_runtime_session(tmp_path, force=True, timeout_s=0.05)
        assert result.exit_code == 1
        assert "Unable to terminate all services." in result.message
        assert "424242" in result.message
        assert "stackpilot stop --force" in result.message
        # runtime.json retained for retry
        import json

        data = json.loads(runtime_status_path(tmp_path).read_text(encoding="utf-8"))
        assert data.get("session_active") is True

    def test_cleanup_failure_format(self) -> None:
        text = format_cleanup_failure(remaining_pids=[111, 222])
        assert "Problem: Unable to terminate all services." in text
        assert "The following PIDs remain alive:" in text
        assert "111" in text and "222" in text

    def test_orchestrator_clears_runtime_on_stop(self, tmp_path: Path) -> None:
        stack = Stack()
        stack.service(
            name="demo",
            path=str(tmp_path),
            command=_sleep_cmd(0.3),
            health_check=ProcessHealthCheck(timeout=5.0, interval=0.05),
        )
        orch = Orchestrator(poll_interval_s=0.05)
        code = orch.run(stack, project_root=tmp_path)
        assert code in {0, 130}
        path = runtime_status_path(tmp_path)
        if path.is_file():
            import json

            data = json.loads(path.read_text(encoding="utf-8"))
            assert data.get("session_active") is False
            assert data.get("services") == []
