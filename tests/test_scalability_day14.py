"""Day 14 scalability hardening regressions."""

from __future__ import annotations

import io
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from stackpilot.config import ServiceSpec
from stackpilot.dashboard import ascii_fallback_dx, print_safe
from stackpilot.dependency_graph import DependencyGraph
from stackpilot.logger import Logger
from stackpilot.models import ManagedService, ServiceState
from stackpilot.process_manager import ProcessManager
from stackpilot.runner import Runner
from stackpilot.status import (
    RUNTIME_FLUSH_INTERVAL_S,
    RuntimeStatus,
)


# ---------------------------------------------------------------------------
# FIX 1 — Unicode-safe printing
# ---------------------------------------------------------------------------


def test_print_safe_never_raises_on_ascii_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    class _AsciiOut(io.TextIOBase):
        encoding = "ascii"

        def __init__(self) -> None:
            self.chunks: list[str] = []

        def write(self, s: str) -> int:  # type: ignore[override]
            s.encode("ascii")  # raise if non-ascii reaches the sink
            self.chunks.append(s)
            return len(s)

        def flush(self) -> None:
            return None

    sink = _AsciiOut()

    def emit(message: str) -> None:
        sink.write(message)
        sink.write("\n")

    # Must not raise UnicodeEncodeError on Windows-like ascii consoles.
    print_safe("✓ gateway healthy", ascii_fallback="+ gateway healthy", print_fn=emit)
    print_safe("❌ boom", ascii_fallback="X boom", print_fn=emit)
    # External-validation failure mark (cp1252 cannot encode ✗).
    fail = "✗ PostgreSQL is not reachable."
    print_safe(fail, ascii_fallback=ascii_fallback_dx(fail), print_fn=emit)
    joined = "".join(sink.chunks)
    assert "✓" not in joined
    assert "❌" not in joined
    assert "✗" not in joined
    assert "+ gateway healthy" in joined
    assert "X boom" in joined
    assert "X PostgreSQL is not reachable." in joined


def test_print_safe_passes_unicode_when_encoding_allows() -> None:
    seen: list[str] = []
    print_safe("✓ ok", ascii_fallback="+ ok", print_fn=seen.append)
    assert seen == ["✓ ok"]


def test_logger_console_uses_print_safe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def boom(message: str) -> None:
        if "✓" in message or "⚠" in message:
            raise UnicodeEncodeError("ascii", message, 0, 1, "ordinal not in range")
        calls.append(message)

    logger = Logger(
        tmp_path / "issues",
        service_names=["api"],
        print_fn=boom,
        color=False,
    )
    # Force a unicode mark into the formatted line via message body.
    logger.stdout("api", "ready ✓")
    logger.close()
    assert calls
    assert "✓" not in calls[-1]
    assert "+" in calls[-1] or "ready" in calls[-1]


# ---------------------------------------------------------------------------
# FIX 2 — runtime.json write throttling
# ---------------------------------------------------------------------------


def test_runtime_persist_throttles_unchanged_polls(tmp_path: Path) -> None:
    status = RuntimeStatus(project_root=tmp_path)
    status.register("api", status=ServiceState.RUNNING, command="python -c pass")
    status.mark_stack_started()

    status.persist(force=True)
    assert status._persist_writes == 1

    # Unchanged fingerprints within the flush window must not hit disk.
    for _ in range(20):
        status.persist()
    assert status._persist_writes == 1

    # After the flush interval, an unchanged snapshot may refresh uptime.
    time.sleep(RUNTIME_FLUSH_INTERVAL_S + 0.05)
    status.persist()
    assert status._persist_writes == 2


def test_runtime_persist_writes_immediately_on_state_change(tmp_path: Path) -> None:
    status = RuntimeStatus(project_root=tmp_path)
    status.register("api", status=ServiceState.STOPPED, command="python -c pass")
    status.mark_stack_started()
    status.persist(force=True)
    baseline = status._persist_writes

    managed = ManagedService(
        spec=ServiceSpec(name="api", path=tmp_path, command="python -c pass"),
        state=ServiceState.RUNNING,
        pid=4242,
    )
    managed.mark_started()
    status.sync_managed(managed)
    assert status._persist_writes == baseline + 1

    # Same state again within interval — no extra write.
    status.sync_managed(managed)
    assert status._persist_writes == baseline + 1


def test_runtime_persist_force_preserves_final_state(tmp_path: Path) -> None:
    status = RuntimeStatus(project_root=tmp_path)
    status.register("api", status=ServiceState.RUNNING, command="python -c pass")
    status.mark_stack_started()
    status.persist(force=True)
    status.mark_session_ended()
    path = tmp_path / ".stackpilot" / "runtime.json"
    text = path.read_text(encoding="utf-8")
    assert '"session_active": false' in text
    assert '"status": "stopped"' in text


def test_runtime_write_reduction_vs_unthrottled(tmp_path: Path) -> None:
    """Simulate ~4 Hz monitor polls for 3s; throttled writes stay near flush rate."""

    status = RuntimeStatus(project_root=tmp_path)
    status.register("api", status=ServiceState.RUNNING, command="python -c pass")
    status.mark_stack_started()
    status.persist(force=True)

    polls = 0
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        status.persist()
        polls += 1
        time.sleep(0.25)

    # Unthrottled would write ~polls times; throttled ≈ 3s / 1.5s + initial.
    assert polls >= 10
    assert status._persist_writes <= 5
    assert status._persist_writes < polls // 2


# ---------------------------------------------------------------------------
# FIX 3 — pump thread cleanup
# ---------------------------------------------------------------------------


def test_pump_threads_pruned_after_stop(tmp_path: Path) -> None:
    logger = Logger(tmp_path / "logs", service_names=["demo"], print_fn=lambda s: None)
    manager = ProcessManager(logger, stop_timeout_s=2.0)
    spec = ServiceSpec(
        name="demo",
        path=tmp_path,
        command=f'{sys.executable} -c "import time; time.sleep(0.2)"',
    )

    for _ in range(8):
        manager.start(spec)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not manager.all_finished():
            manager.reap_exited()
            time.sleep(0.05)
        manager.reap_exited()
        manager.wait_for_output("demo", timeout_s=1.0)
        manager._prune_completed_threads()

    alive = [t for t in manager._threads if t.is_alive()]
    assert alive == []
    assert len(manager._threads) <= 2
    logger.close()


def test_pump_thread_list_bounded_across_restarts(tmp_path: Path) -> None:
    logger = Logger(tmp_path / "logs", service_names=["svc"], print_fn=lambda s: None)
    manager = ProcessManager(logger, stop_timeout_s=2.0)
    spec = ServiceSpec(
        name="svc",
        path=tmp_path,
        command=f'{sys.executable} -c "import time; time.sleep(30)"',
    )

    for _ in range(6):
        manager.start(spec)
        manager.stop("svc")

    manager._prune_completed_threads()
    assert all(not t.is_alive() for t in manager._threads)
    assert len(manager._threads) == 0
    assert manager._pump_threads == {}
    logger.close()


# ---------------------------------------------------------------------------
# FIX 4 — parallel shutdown waves
# ---------------------------------------------------------------------------


def _chain_and_independents(tmp_path: Path) -> tuple[Runner, list[str]]:
    """Gateway→Auth→User chain plus three independent leaves."""

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
    runner = Runner(logs_dir=tmp_path / "logs")
    runner._graph = graph
    return runner, [s.name for s in specs]


def test_shutdown_waves_respect_dependency_order(tmp_path: Path) -> None:
    runner, _ = _chain_and_independents(tmp_path)
    # Reverse start-order display list (dependents first), matching shutdown().
    names = ["gateway", "email", "notification", "analytics", "auth", "user"]
    waves = runner._shutdown_waves(names)

    assert waves[0][0] == "gateway" or "gateway" in waves[0]
    # Independents share the first wave with gateway.
    first = set(waves[0])
    assert "gateway" in first
    assert {"analytics", "notification", "email"} <= first
    # Auth only after gateway; user only after auth.
    flat_index = {n: i for i, wave in enumerate(waves) for n in wave}
    assert flat_index["gateway"] < flat_index["auth"] < flat_index["user"]


def test_stop_wave_runs_independents_concurrently(tmp_path: Path) -> None:
    runner = Runner(logs_dir=tmp_path / "logs")
    manager = MagicMock()
    barrier = {"active": 0, "max": 0, "lock": threading.Lock()}

    def fake_stop(name: str):
        with barrier["lock"]:
            barrier["active"] += 1
            barrier["max"] = max(barrier["max"], barrier["active"])
        time.sleep(0.15)
        with barrier["lock"]:
            barrier["active"] -= 1
        managed = MagicMock()
        managed.name = name
        managed.pid = None
        managed.status = ServiceState.STOPPED
        managed.state = ServiceState.STOPPED
        managed.port = None
        managed.started_at = None
        managed.uptime = None
        managed.exit_code = 0
        managed.spec = ServiceSpec(name=name, path=tmp_path, command="true")
        return managed

    manager.stop.side_effect = fake_stop
    began = time.monotonic()
    stopped, failed = runner._stop_wave(manager, ["a", "b", "c", "d"])
    elapsed = time.monotonic() - began

    assert stopped == ["a", "b", "c", "d"]
    assert failed == []
    assert barrier["max"] >= 3
    # Sequential would be ~0.6s; parallel should finish near one sleep.
    assert elapsed < 0.45


def test_shutdown_timing_scales_with_parallelism(tmp_path: Path) -> None:
    """Independent N-service wave: parallel ~O(1 sleep), sequential ~O(N)."""

    runner = Runner(logs_dir=tmp_path / "logs")
    stop_s = 0.05

    def measure(n: int) -> float:
        manager = MagicMock()

        def fake_stop(name: str):
            time.sleep(stop_s)
            managed = MagicMock()
            managed.name = name
            managed.pid = None
            managed.status = ServiceState.STOPPED
            managed.state = ServiceState.STOPPED
            managed.port = None
            managed.started_at = None
            managed.uptime = None
            managed.exit_code = 0
            managed.spec = ServiceSpec(name=name, path=tmp_path, command="true")
            return managed

        manager.stop.side_effect = fake_stop
        names = [f"s{i}" for i in range(n)]
        began = time.monotonic()
        runner._stop_wave(manager, names)
        return time.monotonic() - began

    t5 = measure(5)
    t10 = measure(10)
    t20 = measure(20)
    t50 = measure(50)

    # Parallel waves stay near stop_s even as N grows (thread-pool bound).
    assert t5 < 0.35
    assert t10 < 0.40
    assert t20 < 0.55
    assert t50 < 0.90
    # Sequential baseline would be n * stop_s; require clear win at N=50.
    sequential_50 = 50 * stop_s
    assert t50 < sequential_50 * 0.5
