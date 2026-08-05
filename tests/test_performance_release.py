"""Automated performance / leak regressions for v0.1.0."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

from stackpilot.config import ServiceSpec, Stack
from stackpilot.dependency_graph import build_graph
from stackpilot.logger import Logger
from stackpilot.process_manager import ProcessManager
from stackpilot.runner import Runner
from stackpilot.status import RuntimeStatus
from stackpilot.watch_manager import WatchManager


def _sleep_cmd(seconds: float = 30.0) -> str:
    return f'{sys.executable} -c "import time; time.sleep({seconds})"'


class TestPerformanceBudgets:
    def test_graph_generation_scale(self) -> None:
        stack = Stack()
        prev = None
        for i in range(50):
            name = f"svc{i}"
            deps = [prev] if prev else []
            stack.service(
                name=name,
                path=".",
                command="true",
                depends_on=deps,
            )
            prev = name
        began = time.perf_counter()
        graph = build_graph(stack)
        from stackpilot.graph_view import format_architecture_report

        report = format_architecture_report(graph, unicode=False)
        elapsed = time.perf_counter() - began
        assert "svc0" in report
        assert "svc49" in report
        assert "Applications" in report
        assert elapsed < 2.0

    def test_runtime_persist_budget(self, tmp_path: Path) -> None:
        status = RuntimeStatus(project_root=tmp_path)
        specs = [
            ServiceSpec(name=f"s{i}", path=tmp_path, command="true")
            for i in range(25)
        ]
        status.register_specs(specs)
        began = time.perf_counter()
        for _ in range(20):
            status.persist(force=True)
        elapsed = time.perf_counter() - began
        assert elapsed < 2.0
        assert (tmp_path / ".stackpilot" / "runtime.json").is_file()

    def test_startup_shutdown_budget(self, tmp_path: Path) -> None:
        stack = Stack()
        for i in range(3):
            stack.service(
                name=f"s{i}",
                path=str(tmp_path),
                command=_sleep_cmd(60),
            )
        logger = Logger(tmp_path / "issues", service_names=["s0", "s1", "s2"], auto_cleanup=False)
        manager = ProcessManager(logger)
        watch = WatchManager()
        runner = Runner(poll_interval_s=0.05)
        ordered = list(stack.services)
        runner.bind(
            manager=manager,
            graph=build_graph(stack),
            watch_manager=watch,
            project_root=tmp_path,
            ordered=ordered,
            logger=logger,
        )
        try:
            t0 = time.perf_counter()
            assert runner.start_all(ordered)
            startup = time.perf_counter() - t0
            t1 = time.perf_counter()
            code = runner.shutdown(logger)
            shutdown = time.perf_counter() - t1
            assert code == 130
            assert startup < 10.0
            assert shutdown < 15.0
        finally:
            try:
                manager.stop_all(timeout_s=0.1)
            except Exception:
                pass
            watch.stop()
            runner.unbind()
            logger.close()

    def test_reload_stress_no_double_reload(self, tmp_path: Path) -> None:
        logger = Logger(tmp_path / "issues", service_names=["demo"], auto_cleanup=False)
        manager = ProcessManager(logger)
        watch = WatchManager()
        runner = Runner(poll_interval_s=0.05)
        spec = ServiceSpec(
            name="demo",
            path=tmp_path,
            command=_sleep_cmd(60),
            reload=True,
        )
        runner.bind(
            manager=manager,
            graph=None,
            watch_manager=watch,
            project_root=tmp_path,
            ordered=[spec],
            logger=logger,
        )
        manager.start(spec)
        restarts = {"n": 0}

        def counting(manager_, spec_):  # noqa: ANN001
            restarts["n"] += 1
            time.sleep(0.05)
            return True

        runner._restart_with_health = counting  # type: ignore[method-assign]
        threads = [
            threading.Thread(target=runner.on_reload, args=("demo", ["a.py"]))
            for _ in range(8)
        ]
        try:
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)
            # Overlapping reloads for the same service are coalesced to one in-flight
            # restart plus at most one follow-up (never one-per-event).
            assert 1 <= restarts["n"] <= 2
            runner.begin_shutdown()
            runner.shutdown(logger)
        finally:
            try:
                manager.stop_all(timeout_s=0.1)
            except Exception:
                pass
            watch.stop()
            runner.unbind()
            logger.close()


class TestLeakDetection:
    def test_no_thread_leak_after_shutdown(self, tmp_path: Path) -> None:
        before = {t.name for t in threading.enumerate()}
        logger = Logger(tmp_path / "issues", service_names=["demo"], auto_cleanup=False)
        manager = ProcessManager(logger)
        watch = WatchManager()
        runner = Runner(poll_interval_s=0.05)
        spec = ServiceSpec(name="demo", path=tmp_path, command=_sleep_cmd(60))
        runner.bind(
            manager=manager,
            graph=None,
            watch_manager=watch,
            project_root=tmp_path,
            ordered=[spec],
            logger=logger,
        )
        manager.start(spec)
        try:
            time.sleep(0.2)
            runner.shutdown(logger)
        finally:
            try:
                manager.stop_all(timeout_s=0.1)
            except Exception:
                pass
            watch.stop()
            runner.unbind()
            logger.close()
        time.sleep(0.3)
        after = {t.name for t in threading.enumerate()}
        leaked = [
            name
            for name in after - before
            if name.startswith("stackpilot-")
        ]
        assert leaked == []

    def test_no_process_leak_after_shutdown(self, tmp_path: Path) -> None:
        logger = Logger(tmp_path / "issues", service_names=["demo"], auto_cleanup=False)
        manager = ProcessManager(logger)
        watch = WatchManager()
        runner = Runner(poll_interval_s=0.05)
        spec = ServiceSpec(name="demo", path=tmp_path, command=_sleep_cmd(60))
        runner.bind(
            manager=manager,
            graph=None,
            watch_manager=watch,
            project_root=tmp_path,
            ordered=[spec],
            logger=logger,
        )
        managed = manager.start(spec)
        pid = managed.pid
        assert pid is not None
        try:
            runner.shutdown(logger)
        finally:
            try:
                manager.stop_all(timeout_s=0.1)
            except Exception:
                pass
            watch.stop()
            runner.unbind()
            logger.close()
        time.sleep(0.2)
        alive = True
        try:
            import os

            os.kill(pid, 0)
        except OSError:
            alive = False
        if sys.platform == "win32" and alive:
            # Confirm via tasklist when os.kill is inconclusive.
            import subprocess

            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                check=False,
            )
            alive = str(pid) in (out.stdout or "") and "No tasks" not in (out.stdout or "")
        assert alive is False

    def test_watchers_inactive_after_shutdown(self, tmp_path: Path) -> None:
        svc = tmp_path / "api"
        svc.mkdir()
        watch = WatchManager(debounce_s=0.05)
        calls: list[str] = []

        def on_change(name: str, paths=()) -> None:  # noqa: ANN001
            calls.append(name)

        spec = ServiceSpec(
            name="api",
            path=svc,
            command="true",
            reload=True,
        )
        watch.start([spec], on_change, project_root=tmp_path)
        try:
            assert watch.watched_services
        finally:
            watch.stop()
        assert list(watch.watched_services) == []
        # Post-stop notifications must not be delivered via manager callback list.
        assert watch._on_change is None
