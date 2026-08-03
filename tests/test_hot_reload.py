"""Unit tests for Hot Reload Engine (ignore, debounce, watcher, runner)."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

from stackpilot.config import ServiceSpec, Stack
from stackpilot.dependency_graph import build_graph
from stackpilot.health import Health
from stackpilot.ignore import IgnoreMatcher, default_ignore_patterns, load_stackpilotignore
from stackpilot.launch_env import resolve_service_argv
from stackpilot.logger import Logger
from stackpilot.models import ServiceState
from stackpilot.process_manager import ProcessManager
from stackpilot.runner import Runner
from stackpilot.watch_manager import WatchManager, resolve_reload_dirs
from stackpilot.watcher import (
    ServiceWatcher,
    has_native_reload,
    has_uvicorn_reload_flag,
    should_takeover_native_reload,
    strip_native_reload_argv,
)

# ---------------------------------------------------------------------------
# Native reload detection
# ---------------------------------------------------------------------------


def test_native_reload_detects_uvicorn_reload() -> None:
    assert has_native_reload("uvicorn app:app --reload --port 8000")
    assert has_native_reload(["uvicorn", "app:app", "--reload"])
    assert has_uvicorn_reload_flag("uvicorn app:app --reload --port 8000")


def test_native_reload_detects_flask_debug() -> None:
    assert has_native_reload("flask --debug run")
    assert has_native_reload("flask run --debug --port 5000")
    assert not has_uvicorn_reload_flag("flask --debug run")


def test_native_reload_detects_django_runserver() -> None:
    assert has_native_reload("python manage.py runserver 0.0.0.0:8000")
    assert has_native_reload("uv run python manage.py runserver 0.0.0.0:8001")
    assert has_native_reload(["python", "-m", "django", "runserver", "8000"])
    assert not has_native_reload("python manage.py runserver --noreload 8000")


def test_native_reload_false_for_plain_commands() -> None:
    assert not has_native_reload("uvicorn app:app --port 8000")
    assert not has_native_reload('python -c "print(1)"')
    assert not has_native_reload("flask run")


def test_strip_native_reload_argv_removes_reload_flags() -> None:
    assert strip_native_reload_argv(
        ["python", "-m", "uvicorn", "app:app", "--reload", "--port", "8000"]
    ) == ["python", "-m", "uvicorn", "app:app", "--port", "8000"]
    assert strip_native_reload_argv(
        ["uvicorn", "app:app", "--reload-dir", "src", "--host", "0.0.0.0"]
    ) == ["uvicorn", "app:app", "--host", "0.0.0.0"]
    assert strip_native_reload_argv(
        ["uvicorn", "app:app", "--reload-delay=0.5", "--port", "1"]
    ) == ["uvicorn", "app:app", "--port", "1"]


def test_strip_native_reload_argv_adds_django_noreload() -> None:
    assert strip_native_reload_argv(
        ["python", "manage.py", "runserver", "0.0.0.0:8000"]
    ) == ["python", "manage.py", "runserver", "0.0.0.0:8000", "--noreload"]
    assert strip_native_reload_argv(
        ["python", "manage.py", "runserver", "--noreload", "8000"]
    ) == ["python", "manage.py", "runserver", "--noreload", "8000"]


def test_should_takeover_native_reload_windows_only() -> None:
    cmd = "uvicorn app:app --reload"
    django = "python manage.py runserver 0.0.0.0:8000"
    if sys.platform == "win32":
        assert should_takeover_native_reload(cmd) is True
        assert should_takeover_native_reload(django) is True
    else:
        assert should_takeover_native_reload(cmd) is False
        assert should_takeover_native_reload(django) is False
    assert should_takeover_native_reload("uvicorn app:app") is False
    assert should_takeover_native_reload("flask --debug run") is False
    assert should_takeover_native_reload(
        "python manage.py runserver --noreload 8000"
    ) is False


# ---------------------------------------------------------------------------
# Ignore rules
# ---------------------------------------------------------------------------


def test_default_ignores_common_paths(tmp_path: Path) -> None:
    matcher = IgnoreMatcher(tmp_path, load_ignore_file=False)

    assert matcher.ignored(tmp_path / ".git" / "config")
    assert matcher.ignored(tmp_path / "__pycache__" / "x.pyc")
    assert matcher.ignored(tmp_path / "node_modules" / "pkg" / "index.js")
    assert matcher.ignored(tmp_path / ".venv" / "bin" / "python")
    assert matcher.ignored(tmp_path / ".stackpilot" / "logs" / "a.log")
    assert matcher.ignored(tmp_path / "dist" / "out.js")
    assert matcher.ignored(tmp_path / "build" / "lib")
    assert matcher.ignored(tmp_path / "app.pyc")
    assert matcher.ignored(tmp_path / "app.pyo")
    assert matcher.ignored(tmp_path / "server.log")
    assert not matcher.ignored(tmp_path / "app.py")
    assert not matcher.ignored(tmp_path / "src" / "main.py")


def test_stackpilotignore_gitignore_patterns(tmp_path: Path) -> None:
    (tmp_path / ".stackpilotignore").write_text(
        "# comment\n"
        "*.tmp\n"
        "secrets/\n"
        "!secrets/keep.txt\n",
        encoding="utf-8",
    )
    assert "*.tmp" in load_stackpilotignore(tmp_path)

    matcher = IgnoreMatcher(tmp_path)
    assert matcher.ignored(tmp_path / "foo.tmp")
    assert matcher.ignored(tmp_path / "secrets" / "token")
    assert not matcher.ignored(tmp_path / "secrets" / "keep.txt")
    assert not matcher.ignored(tmp_path / "app.py")


def test_default_ignore_patterns_nonempty() -> None:
    patterns = default_ignore_patterns()
    assert ".git/" in patterns or ".git" in patterns
    assert "*.pyc" in patterns


# ---------------------------------------------------------------------------
# Debounce + ignored files via ServiceWatcher handler
# ---------------------------------------------------------------------------


def test_debounce_collapses_bursts_to_single_restart(tmp_path: Path) -> None:
    events: list[str] = []
    done = threading.Event()

    def on_change(name: str, paths) -> None:
        events.append(name)
        done.set()

    watcher = ServiceWatcher(
        "api",
        [tmp_path],
        on_change,
        debounce_s=0.15,
        ignore=IgnoreMatcher(tmp_path, load_ignore_file=False),
    )
    # Do not start the Observer — drive the handler directly.
    target = tmp_path / "app.py"
    target.write_text("v1", encoding="utf-8")

    for _ in range(5):
        watcher.notify_for_tests(target)
        time.sleep(0.02)

    assert done.wait(timeout=2.0)
    time.sleep(0.25)
    assert events == ["api"]
    assert watcher.handler.fire_count == 1
    watcher.stop()


def test_forget_tree_clears_descendant_signatures_cross_platform(tmp_path: Path) -> None:
    """Directory delete must drop child signatures on every OS (no hardcoded seps)."""

    events: list[str] = []
    nested = tmp_path / "pkg" / "nested"
    nested.mkdir(parents=True)
    child = nested / "mod.py"
    child.write_text("x = 1\n", encoding="utf-8")

    watcher = ServiceWatcher(
        "api",
        [tmp_path],
        lambda name, paths: events.append(name),
        debounce_s=0.05,
        ignore=IgnoreMatcher(tmp_path, load_ignore_file=False),
    )
    watcher.handler.prime(tmp_path)
    child_key = str(child.resolve())
    assert child_key in watcher.handler._signatures

    watcher.handler._consider(str(nested), event_type="deleted", is_directory=True)
    assert child_key not in watcher.handler._signatures
    assert str(nested.resolve()) not in watcher.handler._signatures
    watcher.stop()


def test_ignored_files_do_not_restart(tmp_path: Path) -> None:
    events: list[str] = []

    watcher = ServiceWatcher(
        "api",
        [tmp_path],
        lambda name, paths: events.append(name),
        debounce_s=0.05,
        ignore=IgnoreMatcher(tmp_path, load_ignore_file=False),
    )

    junk = tmp_path / "__pycache__"
    junk.mkdir()
    (junk / "mod.pyc").write_bytes(b"x")
    watcher.notify_for_tests(junk / "mod.pyc")
    watcher.notify_for_tests(tmp_path / "server.log")
    time.sleep(0.2)

    assert events == []
    assert watcher.handler.fire_count == 0
    watcher.stop()


def test_single_restart_on_modify(tmp_path: Path) -> None:
    events: list[str] = []
    done = threading.Event()

    def on_change(name: str, paths) -> None:
        events.append(name)
        done.set()

    watcher = ServiceWatcher(
        "api",
        [tmp_path],
        on_change,
        debounce_s=0.05,
        ignore=IgnoreMatcher(tmp_path, load_ignore_file=False),
    )
    path = tmp_path / "main.py"
    path.write_text("print(1)\n", encoding="utf-8")
    watcher.notify_for_tests(path)

    assert done.wait(timeout=2.0)
    assert events == ["api"]
    watcher.stop()


def test_no_restart_for_unchanged_file_event_after_prime(tmp_path: Path) -> None:
    events: list[str] = []

    watcher = ServiceWatcher(
        "api",
        [tmp_path],
        lambda name, paths: events.append(name),
        debounce_s=0.05,
        ignore=IgnoreMatcher(tmp_path, load_ignore_file=False),
    )
    path = tmp_path / "main.py"
    path.write_text("print(1)\n", encoding="utf-8")
    watcher.handler.prime(tmp_path)

    watcher.notify_for_tests(path, event_type="modified")
    time.sleep(0.2)

    assert events == []
    assert watcher.handler.fire_count == 0
    watcher.stop()


def test_open_event_does_not_restart(tmp_path: Path) -> None:
    events: list[str] = []

    watcher = ServiceWatcher(
        "api",
        [tmp_path],
        lambda name, paths: events.append(name),
        debounce_s=0.05,
        ignore=IgnoreMatcher(tmp_path, load_ignore_file=False),
    )
    path = tmp_path / "main.py"
    path.write_text("print(1)\n", encoding="utf-8")

    watcher.notify_for_tests(path, event_type="opened")
    time.sleep(0.2)

    assert events == []
    assert watcher.handler.fire_count == 0
    watcher.stop()


# ---------------------------------------------------------------------------
# WatchManager
# ---------------------------------------------------------------------------


def test_watch_manager_skips_native_reload(tmp_path: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("Windows takes over uvicorn --reload instead of skipping")

    called: list[str] = []
    logs: list[str] = []

    specs = [
        ServiceSpec(
            name="api",
            path=tmp_path,
            command="uvicorn app:app --reload",
            reload=True,
        )
    ]
    wm = WatchManager(debounce_s=0.05, log=logs.append)
    wm.start(specs, lambda name, paths: called.append(name), project_root=tmp_path)

    assert wm.watched_services == ()
    assert any("Native reload enabled" in line for line in logs)
    assert called == []
    wm.stop()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows reload takeover only")
def test_watch_manager_windows_takeover_uvicorn_reload(tmp_path: Path) -> None:
    logs: list[str] = []
    specs = [
        ServiceSpec(
            name="ai_service",
            path=tmp_path,
            command="python -m uvicorn app.main:app --reload --port 8004",
            reload=False,
        )
    ]
    wm = WatchManager(debounce_s=0.05, log=logs.append)
    wm.start(specs, lambda _name, _paths: None, project_root=tmp_path)

    assert list(wm.watched_services) == ["ai_service"]
    assert any("StackPilot reload enabled" in line for line in logs)
    wm.stop()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows reload takeover only")
def test_resolve_service_argv_strips_reload_on_windows(tmp_path: Path) -> None:
    argv = resolve_service_argv(
        "python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8004",
        cwd=tmp_path,
        env={},
    )
    assert "--reload" not in argv
    assert "--port" in argv
    assert "8004" in argv


@pytest.mark.skipif(sys.platform != "win32", reason="Windows reload takeover only")
def test_resolve_service_argv_adds_django_noreload_on_windows(tmp_path: Path) -> None:
    argv = resolve_service_argv(
        "python manage.py runserver 0.0.0.0:8001",
        cwd=tmp_path,
        env={},
    )
    assert "--noreload" in argv
    assert "0.0.0.0:8001" in argv


@pytest.mark.skipif(sys.platform != "win32", reason="Windows reload takeover only")
def test_watch_manager_takes_over_django_runserver_without_reload_flag(
    tmp_path: Path,
) -> None:
    logs: list[str] = []
    wm = WatchManager(log=logs.append)
    specs = [
        ServiceSpec(
            name="gateway",
            path=tmp_path,
            command="python manage.py runserver 0.0.0.0:8001",
            reload=False,
        ),
    ]
    wm.start(specs, lambda *_: None, project_root=tmp_path)
    assert list(wm.watched_services) == ["gateway"]
    assert any("StackPilot reload enabled" in line for line in logs)
    wm.stop()


def test_watch_manager_starts_watcher_for_reload_true(tmp_path: Path) -> None:
    events: list[str] = []
    done = threading.Event()

    def on_change(name: str, paths) -> None:
        events.append(name)
        done.set()

    specs = [
        ServiceSpec(
            name="api",
            path=tmp_path,
            command='python -c "pass"',
            reload=True,
        ),
        ServiceSpec(
            name="worker",
            path=tmp_path,
            command='python -c "pass"',
            reload=False,
        ),
    ]
    wm = WatchManager(debounce_s=0.05, log=lambda _m: None)
    wm.start(specs, on_change, project_root=tmp_path)

    assert list(wm.watched_services) == ["api"]
    watcher = wm.get_watcher("api")
    assert watcher is not None
    path = tmp_path / "x.py"
    path.write_text("x", encoding="utf-8")
    watcher.notify_for_tests(path)

    assert done.wait(timeout=2.0)
    assert events == ["api"]
    wm.stop()


def test_resolve_reload_dirs_defaults_to_service_path(tmp_path: Path) -> None:
    spec = ServiceSpec(name="api", path=tmp_path, command="true", reload=True)
    assert resolve_reload_dirs(spec) == [tmp_path.resolve()]

    sub = tmp_path / "src"
    sub.mkdir()
    spec2 = ServiceSpec(
        name="api",
        path=tmp_path,
        command="true",
        reload=True,
        reload_dirs=("src",),
    )
    assert resolve_reload_dirs(spec2) == [sub.resolve()]


# ---------------------------------------------------------------------------
# Runner integration: health after restart, failure isolation
# ---------------------------------------------------------------------------


def test_health_check_after_restart(tmp_path: Path) -> None:
    lines: list[str] = []
    logger = Logger(tmp_path / "logs", service_names=["api"], print_fn=lines.append)
    manager = ProcessManager(logger, stop_timeout_s=2.0)

    marker = tmp_path / "boot_count.txt"
    marker.write_text("0", encoding="utf-8")
    script = tmp_path / "svc.py"
    script.write_text(
        "import time\n"
        "from pathlib import Path\n"
        "p = Path('boot_count.txt')\n"
        "n = int(p.read_text().strip() or '0') + 1\n"
        "p.write_text(str(n))\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )

    spec = ServiceSpec(
        name="api",
        path=tmp_path,
        command="python svc.py",
        health_check={"type": "process", "interval": 0.05, "timeout": 5},
        reload=True,
    )
    managed = manager.start(spec)
    elapsed = Health.wait_until_healthy(
        "api", spec.health_check, process=managed.process
    )
    assert elapsed >= 0

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and marker.read_text(encoding="utf-8").strip() == "0":
        time.sleep(0.05)
    assert marker.read_text(encoding="utf-8").strip() == "1"
    old_pid = managed.pid

    runner = Runner(logs_dir=tmp_path / "logs")
    runner._manager = manager
    runner._reload_locks = {"api": threading.Lock()}
    ok = runner._restart_with_health(manager, spec)
    assert ok is True
    assert manager.get("api").state == ServiceState.RUNNING
    assert manager.get("api").pid != old_pid

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and marker.read_text(encoding="utf-8").strip() != "2":
        time.sleep(0.05)
    assert marker.read_text(encoding="utf-8").strip() == "2"

    manager.stop("api")
    logger.close()


def test_restart_failure_isolation(tmp_path: Path, monkeypatch) -> None:
    printed: list[str] = []
    monkeypatch.setattr(
        "builtins.print",
        lambda *args, **kwargs: printed.append(" ".join(str(a) for a in args)),
    )

    logger = Logger(tmp_path / "logs", service_names=["ok", "bad"])
    manager = ProcessManager(logger, stop_timeout_s=2.0)

    ok_spec = ServiceSpec(
        name="ok",
        path=tmp_path,
        command='python -c "import time; time.sleep(30)"',
        health_check={"type": "process", "interval": 0.05, "timeout": 5},
    )
    bad_spec = ServiceSpec(
        name="bad",
        path=tmp_path,
        command='python -c "import time; time.sleep(30)"',
        health_check={
            "type": "http",
            "url": "http://127.0.0.1:1/health",
            "interval": 0.05,
            "timeout": 0.2,
        },
        reload=True,
    )

    manager.start(ok_spec)
    manager.start(
        ServiceSpec(
            name="bad",
            path=tmp_path,
            command='python -c "import time; time.sleep(30)"',
            health_check={"type": "process", "interval": 0.05, "timeout": 5},
            reload=True,
        )
    )

    # Swap in a health check that will fail after restart.
    managed_bad = manager.get("bad")
    managed_bad.spec = bad_spec

    runner = Runner(logs_dir=tmp_path / "logs")
    runner._manager = manager
    runner._reload_locks = {"ok": threading.Lock(), "bad": threading.Lock()}

    ok = runner._restart_with_health(manager, bad_spec)
    assert ok is False
    assert manager.get("ok").state == ServiceState.RUNNING
    assert any("failed health check after reload" in line for line in printed)

    manager.stop_all()
    logger.close()


def test_runner_launches_watch_manager_and_reloads(
    tmp_path: Path, monkeypatch
) -> None:
    printed: list[str] = []
    monkeypatch.setattr(
        "builtins.print",
        lambda *args, **kwargs: printed.append(" ".join(str(a) for a in args)),
    )
    monkeypatch.chdir(tmp_path)
    (tmp_path / "Stackfile.py").write_text(
        "from stackpilot import Stack\nstack = Stack()\n",
        encoding="utf-8",
    )

    script = tmp_path / "svc.py"
    script.write_text(
        "import time\n"
        "print('up', flush=True)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )

    stack = Stack()
    stack.service(
        name="api",
        path=tmp_path,
        command="python svc.py",
        health_check={"type": "process", "interval": 0.05, "timeout": 5},
        reload=True,
    )
    stack.service(
        name="sidecar",
        path=tmp_path,
        command='python -c "import time; time.sleep(30)"',
        health_check={"type": "process", "interval": 0.05, "timeout": 5},
    )

    runner = Runner(
        logs_dir=tmp_path / "logs",
        poll_interval_s=0.05,
        reload_debounce_s=0.05,
    )

    original_monitor = runner.monitor

    def monitor_then_reload() -> int:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if runner._watch_manager and runner._watch_manager.get_watcher("api"):
                break
            time.sleep(0.05)
        wm = runner._watch_manager
        assert wm is not None
        watcher = wm.get_watcher("api")
        assert watcher is not None
        assert runner._manager is not None
        old_pid = runner._manager.get("api").pid

        touch = tmp_path / "touch.py"
        touch.write_text("changed-1", encoding="utf-8")
        watcher.handler._signatures.pop(str(touch.resolve()), None)
        watcher.notify_for_tests(touch, event_type="modified")

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if any("reloaded" in line for line in printed):
                break
            time.sleep(0.05)

        assert runner._manager.get("api").pid != old_pid
        assert runner._manager.get("sidecar").state == ServiceState.RUNNING
        raise KeyboardInterrupt

    monkeypatch.setattr(runner, "monitor", monitor_then_reload)

    code = runner.run(stack)
    assert code == 130
    assert any("Starting application services..." in line for line in printed)
    assert any("Watching for changes..." in line for line in printed)
    assert any("Reloading api..." in line for line in printed)
    assert any("reloaded" in line for line in printed)
    assert any(
        "WARNING StackPilot detected changes in" in line and "touch.py" in line
        for line in printed
    )
    del original_monitor


def test_service_spec_reload_fields_roundtrip(tmp_path: Path) -> None:
    stack = Stack()
    stack.service(
        name="api",
        path=tmp_path,
        command="python main.py",
        reload=True,
        reload_dirs=["src", "lib"],
        restart_dependents=True,
    )
    spec = stack.services[0]
    assert spec.reload is True
    assert spec.reload_dirs == ("src", "lib")
    assert spec.restart_dependents is True

    graph = build_graph(stack.services)
    assert graph.specs["api"].reload is True


def test_dependents_helper_order() -> None:
    stack = Stack()
    stack.service(name="db", path=".", command="true")
    stack.service(name="api", path=".", command="true", depends_on=["db"])
    stack.service(name="web", path=".", command="true", depends_on=["api"])
    graph = build_graph(stack.services)
    assert graph.dependents("db") == ["api", "web"]
    assert graph.dependents("api") == ["web"]
    assert graph.dependents("web") == []
