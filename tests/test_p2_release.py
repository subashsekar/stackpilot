"""P2 release polish regressions (CLI UX, logs, parallel start, doctor, graph)."""

from __future__ import annotations

import io
import sys
import threading
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from stackpilot.cli import app
from stackpilot.config import ServiceSpec, Stack
from stackpilot.dashboard import ascii_fallback_dx, print_safe
from stackpilot.dependency_graph import DependencyGraph, build_graph
from stackpilot.diagnostics.errors import (
    format_corrupted_runtime,
    format_missing_stackfile,
    format_user_error,
)
from stackpilot.discovery import MISSING_STACKFILE_MESSAGE, STACKFILE_NAME
from stackpilot.doctor import CheckStatus, run_doctor
from stackpilot.graph_view import (
    format_architecture_report,
    format_startup_order,
)
from stackpilot.logger import Logger, detect_log_level, is_framework_info_line
from stackpilot.runner import Runner


runner = CliRunner()


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _check_by_name(report, name: str):
    matches = [c for c in report.checks if c.name == name]
    assert matches, f"No check named {name!r}. Have: {[c.name for c in report.checks]}"
    return matches[-1]


def _sleep_cmd(seconds: float = 30.0) -> str:
    return f'{sys.executable} -c "import time; time.sleep({seconds})"'


# ---------------------------------------------------------------------------
# P2-1 — CLI Problem / Reason / Suggested fix
# ---------------------------------------------------------------------------


class TestCliMessageContract:
    def test_user_error_shape(self) -> None:
        text = format_user_error(
            problem="Missing command",
            reason="command= is empty.",
            suggested_fix="Set command= in Stackfile.py.",
        )
        assert "Problem: Missing command" in text
        assert "Reason: command= is empty." in text
        assert "Suggested fix: Set command= in Stackfile.py." in text

    def test_missing_stackfile_message_contract(self) -> None:
        text = format_missing_stackfile()
        assert text == MISSING_STACKFILE_MESSAGE
        assert "Problem: No Stackfile.py found." in text
        assert "Reason:" in text
        assert "Suggested fix:" in text
        assert "stackpilot init" in text

    def test_missing_stackfile_cli(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["run"])
        assert result.exit_code == 1
        combined = (result.output or "") + (getattr(result, "stderr", None) or "")
        assert "Problem: No Stackfile.py found." in combined
        assert "Suggested fix:" in combined
        assert "stackpilot init" in combined

    def test_corrupted_runtime_message(self) -> None:
        text = format_corrupted_runtime(cleared=True)
        assert "Problem: Corrupted runtime status" in text
        assert "Reason:" in text
        assert "Suggested fix:" in text

    def test_corrupted_runtime_stop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write(
            tmp_path / STACKFILE_NAME,
            "from stackpilot import Stack\nstack = Stack()\n"
            'stack.service(name="a", path=".", command="true")\nstack.run()\n',
        )
        runtime = tmp_path / ".stackpilot" / "runtime.json"
        runtime.parent.mkdir(parents=True, exist_ok=True)
        runtime.write_text("{not-json", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["stop"])
        assert result.exit_code == 0
        combined = (result.output or "") + (getattr(result, "stderr", None) or "")
        assert "Problem: Corrupted runtime status" in combined
        assert "Suggested fix:" in combined


# ---------------------------------------------------------------------------
# P2-2 — Flask / Werkzeug log noise
# ---------------------------------------------------------------------------


class TestFrameworkLogNoise:
    @pytest.mark.parametrize(
        "line,expected",
        [
            (" * Serving Flask app 'app:app'", "INFO"),
            (" * Running on http://127.0.0.1:8000", "INFO"),
            (" * Debug mode: off", "INFO"),
            (
                "WARNING: This is a development server. Do not use it in a production deployment.",
                "WARN",
            ),
            ("Press CTRL+C to quit", "INFO"),
        ],
    )
    def test_framework_info_detected(self, line: str, expected: str) -> None:
        assert is_framework_info_line(line)
        level, _ = detect_log_level(line, default="ERROR")
        assert level == expected
        assert level != "ERROR"

    def test_stderr_flask_banner_is_info_not_error(self, tmp_path: Path) -> None:
        lines: list[str] = []
        logger = Logger(
            tmp_path / "issues",
            service_names=["web"],
            print_fn=lines.append,
            color=False,
            auto_cleanup=False,
        )
        logger.stderr("web", " * Serving Flask app 'app:app'")
        logger.close()
        assert " INFO " in lines[0]
        assert " ERROR " not in lines[0]
        assert list((tmp_path / "issues").glob("*.issue")) == []

    def test_real_error_still_error(self, tmp_path: Path) -> None:
        lines: list[str] = []
        logger = Logger(
            tmp_path / "issues",
            service_names=["web"],
            print_fn=lines.append,
            color=False,
            auto_cleanup=False,
        )
        logger.stderr("web", "Connection refused")
        logger.close()
        assert " ERROR " in lines[0]


# ---------------------------------------------------------------------------
# P2-3 — Parallel startup waves
# ---------------------------------------------------------------------------


def _chain_and_independents(tmp_path: Path) -> tuple[Runner, list[ServiceSpec]]:
    specs = [
        ServiceSpec(name="user", path=tmp_path, command="true"),
        ServiceSpec(name="auth", path=tmp_path, command="true", depends_on=["user"]),
        ServiceSpec(
            name="gateway", path=tmp_path, command="true", depends_on=["auth"]
        ),
        ServiceSpec(name="analytics", path=tmp_path, command="true"),
        ServiceSpec(name="notification", path=tmp_path, command="true"),
        ServiceSpec(name="email", path=tmp_path, command="true"),
    ]
    graph = DependencyGraph.from_services(specs)
    runner_obj = Runner(logs_dir=tmp_path / "logs")
    runner_obj._graph = graph
    return runner_obj, specs


class TestParallelStartup:
    def test_startup_waves_respect_dependency_order(self, tmp_path: Path) -> None:
        runner_obj, specs = _chain_and_independents(tmp_path)
        names = [s.name for s in specs]
        waves = runner_obj._startup_waves(names)

        flat_index = {n: i for i, wave in enumerate(waves) for n in wave}
        assert flat_index["user"] < flat_index["auth"] < flat_index["gateway"]
        first = set(waves[0])
        assert "user" in first
        assert {"analytics", "notification", "email"} <= first
        assert "gateway" not in first

    def test_start_wave_runs_independents_concurrently(self, tmp_path: Path) -> None:
        runner_obj = Runner(logs_dir=tmp_path / "logs")
        specs = [
            ServiceSpec(name=n, path=tmp_path, command="true")
            for n in ("a", "b", "c", "d")
        ]
        # All four starts must rendezvous; sequential starts would time out here.
        rendezvous = threading.Barrier(len(specs), timeout=2.0)
        peak = {"active": 0, "max": 0, "lock": threading.Lock()}

        def fake_start(spec: ServiceSpec) -> bool:
            rendezvous.wait()
            with peak["lock"]:
                peak["active"] += 1
                peak["max"] = max(peak["max"], peak["active"])
            time.sleep(0.15)
            with peak["lock"]:
                peak["active"] -= 1
            return True

        runner_obj.start = fake_start  # type: ignore[method-assign]
        started, failed = runner_obj._start_wave(specs)

        assert started == ["a", "b", "c", "d"]
        assert failed == []
        assert peak["max"] >= 3

    def test_parallel_startup_faster_than_sequential(self, tmp_path: Path) -> None:
        runner_obj = Runner(logs_dir=tmp_path / "logs")
        start_s = 0.08

        def measure(n: int, *, parallel: bool) -> float:
            specs = [
                ServiceSpec(name=f"s{i}", path=tmp_path, command="true")
                for i in range(n)
            ]
            graph = DependencyGraph.from_services(specs)
            runner_obj._graph = graph

            def fake_start(spec: ServiceSpec) -> bool:
                time.sleep(start_s)
                return True

            runner_obj.start = fake_start  # type: ignore[method-assign]
            began = time.monotonic()
            if parallel:
                for wave in runner_obj._startup_waves([s.name for s in specs]):
                    wave_specs = [s for s in specs if s.name in wave]
                    runner_obj._start_wave(wave_specs)
            else:
                for spec in specs:
                    runner_obj.start(spec)
            return time.monotonic() - began

        parallel_t = measure(4, parallel=True)
        sequential_t = measure(4, parallel=False)
        # Relative to the sequential baseline measured on this machine / load.
        assert parallel_t < sequential_t * 0.7


# ---------------------------------------------------------------------------
# P2-4 — Runtime cleanup verification
# ---------------------------------------------------------------------------


class TestRuntimeCleanup:
    def test_verify_cleanup_after_shutdown(self, tmp_path: Path) -> None:
        from stackpilot.logger import Logger as SPLogger
        from stackpilot.process_manager import ProcessManager
        from stackpilot.watch_manager import WatchManager

        logger = SPLogger(
            tmp_path / "issues", service_names=["demo"], auto_cleanup=False
        )
        manager = ProcessManager(logger)
        watch = WatchManager()
        runner_obj = Runner(poll_interval_s=0.05)
        spec = ServiceSpec(name="demo", path=tmp_path, command=_sleep_cmd(60))
        runner_obj.bind(
            manager=manager,
            graph=None,
            watch_manager=watch,
            project_root=tmp_path,
            ordered=[spec],
            logger=logger,
        )
        manager.start(spec)
        runner_obj.shutdown(logger)
        watch.stop()
        report = runner_obj.verify_cleanup()
        runner_obj.unbind()
        logger.close()
        assert report["orphan_pids"] == []
        assert report["alive_pump_threads"] == 0
        assert report["watched_services"] == []
        assert report["ok"] is True


# ---------------------------------------------------------------------------
# P2-5 — Graph improvements
# ---------------------------------------------------------------------------


class TestGraphPolish:
    def test_startup_order_section(self) -> None:
        stack = Stack()
        stack.service(name="db_proxy", path=".", command="true")
        stack.service(
            name="api", path=".", command="true", depends_on=["db_proxy"]
        )
        stack.service(name="web", path=".", command="true", depends_on=["api"])
        graph = build_graph(stack)
        report = format_architecture_report(graph, unicode=True)
        assert "Startup order:" in report
        assert "db_proxy" in report
        order = format_startup_order(graph)
        assert "db_proxy" in order and "api" in order and "web" in order
        assert order.index("db_proxy") < order.index("api") < order.index("web")

    def test_cycle_highlighting(self) -> None:
        stack = Stack()
        stack.service(name="a", path=".", command="true", depends_on=["b"])
        stack.service(name="b", path=".", command="true", depends_on=["a"])
        graph = build_graph(stack)
        cycle = ("a", "b", "a")
        report = format_architecture_report(graph, unicode=True, cycle=cycle)
        assert "CYCLE" in report or "⚠" in report
        assert "Circular Dependencies" in report
        assert "a" in report and "b" in report

    def test_ascii_and_cp1252_safe(self) -> None:
        stack = Stack()
        stack.external_dependency(
            name="postgres", type="postgresql", host="127.0.0.1", port=5432
        )
        stack.service(
            name="api",
            path=".",
            command="uvicorn api:app",
            depends_on=["postgres"],
            port=8000,
        )
        graph = build_graph(stack)
        ascii_report = format_architecture_report(graph, unicode=False)
        assert "Startup order:" in ascii_report
        assert "🟢" not in ascii_report
        assert "├──" not in ascii_report

        class _Cp1252(io.TextIOBase):
            encoding = "cp1252"

            def __init__(self) -> None:
                self.chunks: list[str] = []

            def write(self, s: str) -> int:  # type: ignore[override]
                s.encode("cp1252")
                self.chunks.append(s)
                return len(s)

            def flush(self) -> None:
                return None

        sink = _Cp1252()

        def emit(message: str) -> None:
            sink.write(message)
            sink.write("\n")

        unicode_report = format_architecture_report(graph, unicode=True)
        print_safe(
            unicode_report,
            ascii_fallback=ascii_fallback_dx(unicode_report),
            print_fn=emit,
        )
        joined = "".join(sink.chunks)
        assert "🟢" not in joined
        assert "api" in joined


# ---------------------------------------------------------------------------
# P2-6 — Doctor expansions
# ---------------------------------------------------------------------------


class TestDoctorP2:
    def test_runtime_integrity_and_orphans(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write(
            tmp_path / STACKFILE_NAME,
            "from stackpilot import Stack\nstack = Stack()\n"
            'stack.service(name="demo", path=".", command="python -c \\"pass\\"")\n'
            "stack.run()\n",
        )
        monkeypatch.chdir(tmp_path)
        report = run_doctor(start=tmp_path)
        assert _check_by_name(report, "Runtime status integrity").status == CheckStatus.OK
        assert (
            _check_by_name(report, "Orphan StackPilot processes").status
            == CheckStatus.OK
        )
        assert "No orphan processes detected." in _check_by_name(
            report, "Orphan StackPilot processes"
        ).detail

        runtime = tmp_path / ".stackpilot" / "runtime.json"
        runtime.parent.mkdir(parents=True, exist_ok=True)
        runtime.write_text("{bad", encoding="utf-8")
        report2 = run_doctor(start=tmp_path)
        assert (
            _check_by_name(report2, "Runtime status integrity").status
            == CheckStatus.FAIL
        )

    def test_env_file_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        body = """\
from stackpilot import Stack

stack = Stack()
stack.service(
    name="demo",
    path=".",
    command="python -c \\"pass\\"",
    env_file=".env.missing",
)
stack.run()
"""
        _write(tmp_path / STACKFILE_NAME, body)
        monkeypatch.chdir(tmp_path)
        report = run_doctor(start=tmp_path)
        check = _check_by_name(report, "Env files readable")
        assert check.status == CheckStatus.FAIL
        assert "missing" in check.detail.lower()

    def test_permissions_ok(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write(
            tmp_path / STACKFILE_NAME,
            "from stackpilot import Stack\nstack = Stack()\n"
            'stack.service(name="demo", path=".", command="python -c \\"pass\\"")\n'
            "stack.run()\n",
        )
        monkeypatch.chdir(tmp_path)
        report = run_doctor(start=tmp_path)
        assert (
            _check_by_name(report, "Project artifact permissions").status
            == CheckStatus.OK
        )


# ---------------------------------------------------------------------------
# P2-8 — Performance budgets (parallel start)
# ---------------------------------------------------------------------------


class TestParallelStartBudget:
    def test_independent_services_start_budget(self, tmp_path: Path) -> None:
        from stackpilot.logger import Logger as SPLogger
        from stackpilot.process_manager import ProcessManager
        from stackpilot.watch_manager import WatchManager

        stack = Stack()
        for i in range(4):
            stack.service(
                name=f"s{i}",
                path=str(tmp_path),
                command=_sleep_cmd(60),
            )
        logger = SPLogger(
            tmp_path / "issues",
            service_names=[f"s{i}" for i in range(4)],
            auto_cleanup=False,
        )
        manager = ProcessManager(logger)
        watch = WatchManager()
        runner_obj = Runner(poll_interval_s=0.05)
        ordered = list(stack.services)
        graph = build_graph(stack)
        runner_obj.bind(
            manager=manager,
            graph=graph,
            watch_manager=watch,
            project_root=tmp_path,
            ordered=ordered,
            logger=logger,
        )
        t0 = time.perf_counter()
        assert runner_obj.start_all(ordered)
        startup = time.perf_counter() - t0
        code = runner_obj.shutdown(logger)
        watch.stop()
        cleanup = runner_obj.verify_cleanup()
        runner_obj.unbind()
        logger.close()
        assert code == 130
        assert startup < 8.0
        assert cleanup["ok"] is True
