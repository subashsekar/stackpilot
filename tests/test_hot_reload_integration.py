"""Integration tests: real process hot reload on Windows/Linux."""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest

from stackpilot.config import HttpHealthCheck, Stack
from stackpilot.orchestrator import Orchestrator
from stackpilot.status import pid_is_alive


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_http(url: str, *, timeout: float = 20.0) -> bool:
    import urllib.error
    import urllib.request

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as resp:
                if 200 <= resp.status < 400:
                    return True
        except (urllib.error.URLError, TimeoutError, OSError):
            time.sleep(0.1)
    return False


def _force_kill_pids(pids: dict[str, int | None]) -> None:
    """Best-effort kill leftover service PIDs after a timed harness join."""

    from stackpilot.process_tree import signal_process_tree

    for pid in pids.values():
        if pid is None:
            continue
        try:
            if pid_is_alive(pid):
                signal_process_tree(int(pid), graceful=False)
        except Exception:
            pass


class _ReloadHarness:
    """Run Orchestrator in a thread and edit files against a live stack."""

    def __init__(self, project: Path, stack: Stack, *, debounce_s: float = 0.2):
        self.project = project
        self.stack = stack
        self.orch = Orchestrator(reload_debounce_s=debounce_s)
        self.printed: list[str] = []
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        import builtins

        real_print = builtins.print

        def capturing(*args, **kwargs):
            line = " ".join(str(a) for a in args)
            with self._lock:
                self.printed.append(line)
            real_print(*args, **kwargs)

        builtins.print = capturing  # type: ignore[assignment]

        def run() -> None:
            os.chdir(self.project)
            try:
                self.orch.run(self.stack, project_root=self.project)
            finally:
                builtins.print = real_print

        self._thread = threading.Thread(target=run, name="reload-orch", daemon=True)
        self._thread.start()

        deadline = time.time() + 40.0
        while time.time() < deadline:
            with self._lock:
                blob = "\n".join(self.printed)
            if "Watching for changes" in blob:
                wm = self.orch._watch_manager
                if wm is not None and wm.watched_services:
                    return
            if self._thread is not None and not self._thread.is_alive():
                raise RuntimeError(f"orchestrator exited early:\n{blob}")
            time.sleep(0.05)
        raise TimeoutError("watchers never became ready")

    def stop(self) -> None:
        leftover = self.pids()
        try:
            self.orch.stop()
        except Exception:
            pass
        if self._thread is not None:
            self._thread.join(timeout=20)
        # If shutdown outlived the join budget, force-kill children so later
        # tests do not collide on ports / hang the suite (CI exit 143).
        if self._thread is not None and self._thread.is_alive():
            runner = self.orch._runner
            if runner is not None and runner._manager is not None:
                try:
                    runner._manager.stop_all(timeout_s=0.1)
                except Exception:
                    pass
            _force_kill_pids(leftover)
            self._thread.join(timeout=5)
        else:
            _force_kill_pids(leftover)

    def blob(self) -> str:
        with self._lock:
            return "\n".join(self.printed)

    def reload_count(self) -> int:
        text = self.blob().lower()
        return text.count("reloading ") + text.count("reloaded")

    def pids(self) -> dict[str, int | None]:
        runner = self.orch._runner
        if runner is None or runner._manager is None:
            return {}
        out: dict[str, int | None] = {}
        for managed in runner._manager.services():
            out[managed.name] = managed.pid
        return out


@pytest.fixture
def fastapi_project(tmp_path: Path) -> tuple[Path, Path, int]:
    port = _free_port()
    root = tmp_path / "fastapi_proj"
    api = root / "api"
    _write(
        api / "main.py",
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "@app.get('/health')\n"
        "def health():\n"
        "    return {'ok': True}\n",
    )
    _write(
        root / "Stackfile.py",
        "from stackpilot import Stack, HttpHealthCheck\n"
        "stack = Stack()\n"
        "stack.service(\n"
        "    name='api',\n"
        f"    path='./api',\n"
        f"    command='python -m uvicorn main:app --reload --host 127.0.0.1 --port {port}',\n"
        f"    port={port},\n"
        "    reload=True,\n"
        f"    health_check=HttpHealthCheck(url='http://127.0.0.1:{port}/health'),\n"
        ")\n"
        "stack.run()\n",
    )
    return root, api / "main.py", port


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="Windows reload takeover + Observer validation",
)
def test_fastapi_reload_integration(fastapi_project) -> None:
    pytest.importorskip("fastapi")
    pytest.importorskip("uvicorn")
    root, target, port = fastapi_project
    from stackpilot.utils import load_stack_from_stackfile

    stack = load_stack_from_stackfile(root / "Stackfile.py")
    h = _ReloadHarness(root, stack)
    h.start()
    try:
        assert _wait_http(f"http://127.0.0.1:{port}/health")
        before = h.pids()
        assert before.get("api")
        original = target.read_text(encoding="utf-8")
        target.write_text(original + f"\n# reload {time.time()}\n", encoding="utf-8")
        deadline = time.time() + 15
        while time.time() < deadline:
            if "reloaded" in h.blob().lower() and h.pids().get("api") != before.get("api"):
                break
            time.sleep(0.1)
        assert "detected change" in h.blob().lower()
        assert "reloaded" in h.blob().lower()
        assert h.pids().get("api") != before.get("api")
        assert _wait_http(f"http://127.0.0.1:{port}/health")
        assert pid_is_alive(before["api"]) is False or before["api"] != h.pids().get("api")
    finally:
        old = h.pids().get("api")
        h.stop()
        if old:
            deadline = time.time() + 5
            while time.time() < deadline and pid_is_alive(old):
                time.sleep(0.1)
            assert not pid_is_alive(old)


def test_flask_reload_integration(tmp_path: Path) -> None:
    pytest.importorskip("flask")
    port = _free_port()
    root = tmp_path / "flask_proj"
    web = root / "web"
    target = web / "app.py"
    _write(
        target,
        "from flask import Flask\n"
        "app = Flask(__name__)\n"
        "@app.get('/')\n"
        "def index():\n"
        "    return 'ok'\n"
        "@app.get('/health')\n"
        "def health():\n"
        "    return 'ok'\n",
    )
    _write(
        root / "Stackfile.py",
        "from stackpilot import Stack, HttpHealthCheck\n"
        "stack = Stack()\n"
        "stack.service(\n"
        "    name='web',\n"
        "    path='./web',\n"
        f"    command='python -m flask --app app:app run --host 127.0.0.1 --port {port}',\n"
        f"    port={port},\n"
        "    reload=True,\n"
        f"    health_check=HttpHealthCheck(url='http://127.0.0.1:{port}/health'),\n"
        ")\n"
        "stack.run()\n",
    )
    from stackpilot.utils import load_stack_from_stackfile

    stack = load_stack_from_stackfile(root / "Stackfile.py")
    h = _ReloadHarness(root, stack)
    h.start()
    try:
        assert _wait_http(f"http://127.0.0.1:{port}/health")
        before = h.pids()["web"]
        target.write_text(target.read_text(encoding="utf-8") + f"\n# {time.time()}\n", encoding="utf-8")
        deadline = time.time() + 15
        while time.time() < deadline:
            if h.pids().get("web") != before and "reloaded" in h.blob().lower():
                break
            time.sleep(0.1)
        assert h.pids().get("web") != before
        assert _wait_http(f"http://127.0.0.1:{port}/health")
    finally:
        h.stop()


def test_django_reload_integration(tmp_path: Path) -> None:
    pytest.importorskip("django")
    port = _free_port()
    root = tmp_path / "django_proj"
    web = root / "web"
    # Minimal manage.py + settings + urls
    _write(
        web / "manage.py",
        "#!/usr/bin/env python\n"
        "import os, sys\n"
        "def main():\n"
        "    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'project.settings')\n"
        "    from django.core.management import execute_from_command_line\n"
        "    execute_from_command_line(sys.argv)\n"
        "if __name__ == '__main__':\n"
        "    main()\n",
    )
    _write(
        web / "project" / "__init__.py",
        "",
    )
    _write(
        web / "project" / "settings.py",
        "SECRET_KEY='x'\n"
        "DEBUG=True\n"
        "ALLOWED_HOSTS=['*']\n"
        "ROOT_URLCONF='project.urls'\n"
        "MIDDLEWARE=[]\n"
        "INSTALLED_APPS=[]\n"
        "DATABASES={'default':{'ENGINE':'django.db.backends.sqlite3','NAME':':memory:'}}\n"
        "USE_TZ=True\n"
        "STATIC_URL='/static/'\n",
    )
    urls = web / "project" / "urls.py"
    _write(
        urls,
        "from django.http import JsonResponse\n"
        "from django.urls import path\n"
        "def health(_request):\n"
        "    return JsonResponse({'ok': True})\n"
        "urlpatterns = [path('', health), path('health', health)]\n",
    )
    _write(
        root / "Stackfile.py",
        "from stackpilot import Stack, HttpHealthCheck\n"
        "stack = Stack()\n"
        "stack.service(\n"
        "    name='web',\n"
        "    path='./web',\n"
        f"    command='python manage.py runserver 127.0.0.1:{port}',\n"
        f"    port={port},\n"
        "    reload=True,\n"
        f"    health_check=HttpHealthCheck(url='http://127.0.0.1:{port}/health'),\n"
        ")\n"
        "stack.run()\n",
    )
    from stackpilot.utils import load_stack_from_stackfile

    stack = load_stack_from_stackfile(root / "Stackfile.py")
    h = _ReloadHarness(root, stack)
    h.start()
    try:
        assert _wait_http(f"http://127.0.0.1:{port}/health", timeout=30)
        before = h.pids()["web"]
        urls.write_text(
            urls.read_text(encoding="utf-8") + f"\n# {time.time()}\n",
            encoding="utf-8",
        )
        deadline = time.time() + 20
        while time.time() < deadline:
            if h.pids().get("web") != before and "reloaded" in h.blob().lower():
                break
            time.sleep(0.1)
        assert h.pids().get("web") != before
        assert _wait_http(f"http://127.0.0.1:{port}/health", timeout=20)
    finally:
        h.stop()


def test_rapid_saves_single_reload(tmp_path: Path) -> None:
    port = _free_port()
    root = tmp_path / "rapid"
    svc = root / "svc"
    target = svc / "main.py"
    _write(
        target,
        "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
        "class H(BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        self.send_response(200); self.end_headers(); self.wfile.write(b'ok')\n"
        "    def log_message(self, *a): pass\n"
        f"HTTPServer(('127.0.0.1', {port}), H).serve_forever()\n",
    )
    stack = Stack()
    stack.service(
        name="api",
        path=str(svc),
        command=f"{sys.executable} main.py",
        port=port,
        reload=True,
        health_check=HttpHealthCheck(
            url=f"http://127.0.0.1:{port}/",
            timeout=10.0,
            interval=0.1,
            probe_timeout=1.0,
        ),
    )
    h = _ReloadHarness(root, stack, debounce_s=0.35)
    h.start()
    try:
        assert _wait_http(f"http://127.0.0.1:{port}/")
        before = h.pids()["api"]
        for i in range(8):
            target.write_text(target.read_text(encoding="utf-8") + f"# {i}\n", encoding="utf-8")
            time.sleep(0.03)
        time.sleep(2.5)
        # Exactly one reload cycle for the burst (may print Reloading + reloaded).
        assert h.blob().lower().count("reloading api") == 1
        assert h.pids().get("api") != before
        assert _wait_http(f"http://127.0.0.1:{port}/")
    finally:
        h.stop()


def test_observer_survives_multiple_reloads(tmp_path: Path) -> None:
    port = _free_port()
    root = tmp_path / "multi"
    svc = root / "svc"
    target = svc / "main.py"
    _write(
        target,
        "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
        "class H(BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        self.send_response(200); self.end_headers(); self.wfile.write(b'ok')\n"
        "    def log_message(self, *a): pass\n"
        f"HTTPServer(('127.0.0.1', {port}), H).serve_forever()\n",
    )
    stack = Stack()
    stack.service(
        name="api",
        path=str(svc),
        command=f"{sys.executable} main.py",
        port=port,
        reload=True,
        health_check=HttpHealthCheck(
            url=f"http://127.0.0.1:{port}/",
            timeout=10.0,
            interval=0.1,
            probe_timeout=1.0,
        ),
    )
    h = _ReloadHarness(root, stack, debounce_s=0.2)
    h.start()
    try:
        assert _wait_http(f"http://127.0.0.1:{port}/")
        pids = []
        for i in range(3):
            before = h.pids()["api"]
            pids.append(before)
            target.write_text(
                target.read_text(encoding="utf-8") + f"# round-{i}\n",
                encoding="utf-8",
            )
            deadline = time.time() + 12
            while time.time() < deadline:
                if h.pids().get("api") != before:
                    break
                time.sleep(0.1)
            assert h.pids().get("api") != before
            time.sleep(0.6)
        assert len(set(pids)) == 3
        wm = h.orch._watch_manager
        assert wm is not None
        watcher = wm.get_watcher("api")
        assert watcher is not None
        assert watcher.handler.fire_count >= 3
    finally:
        h.stop()


def test_ctrl_c_exits_clean_after_reload(tmp_path: Path) -> None:
    port = _free_port()
    root = tmp_path / "ctrlc"
    svc = root / "svc"
    target = svc / "main.py"
    _write(
        target,
        "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
        "class H(BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        self.send_response(200); self.end_headers(); self.wfile.write(b'ok')\n"
        "    def log_message(self, *a): pass\n"
        f"HTTPServer(('127.0.0.1', {port}), H).serve_forever()\n",
    )
    stack = Stack()
    stack.service(
        name="api",
        path=str(svc),
        command=f"{sys.executable} main.py",
        port=port,
        reload=True,
        health_check=HttpHealthCheck(
            url=f"http://127.0.0.1:{port}/",
            timeout=10.0,
            interval=0.1,
            probe_timeout=1.0,
        ),
    )
    h = _ReloadHarness(root, stack)
    after: int | None = None
    h.start()
    try:
        assert _wait_http(f"http://127.0.0.1:{port}/")
        before = h.pids()["api"]
        target.write_text(target.read_text(encoding="utf-8") + "# x\n", encoding="utf-8")
        deadline = time.time() + 12
        while time.time() < deadline and h.pids().get("api") == before:
            time.sleep(0.1)
        after = h.pids().get("api")
        assert after and after != before
    finally:
        h.stop()
        if after:
            assert not pid_is_alive(after)


def test_only_changed_service_reloads(tmp_path: Path) -> None:
    port_a = _free_port()
    port_b = _free_port()
    root = tmp_path / "multi_svc"
    for name, port in (("a", port_a), ("b", port_b)):
        svc = root / name
        _write(
            svc / "main.py",
            "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
            "class H(BaseHTTPRequestHandler):\n"
            "    def do_GET(self):\n"
            "        self.send_response(200); self.end_headers(); self.wfile.write(b'ok')\n"
            "    def log_message(self, *a): pass\n"
            f"HTTPServer(('127.0.0.1', {port}), H).serve_forever()\n",
        )
    stack = Stack()
    stack.service(
        name="a",
        path=str(root / "a"),
        command=f"{sys.executable} main.py",
        port=port_a,
        reload=True,
        health_check=HttpHealthCheck(
            url=f"http://127.0.0.1:{port_a}/",
            timeout=10.0,
            interval=0.1,
            probe_timeout=1.0,
        ),
    )
    stack.service(
        name="b",
        path=str(root / "b"),
        command=f"{sys.executable} main.py",
        port=port_b,
        reload=True,
        health_check=HttpHealthCheck(
            url=f"http://127.0.0.1:{port_b}/",
            timeout=10.0,
            interval=0.1,
            probe_timeout=1.0,
        ),
    )
    h = _ReloadHarness(root, stack)
    h.start()
    try:
        assert _wait_http(f"http://127.0.0.1:{port_a}/")
        assert _wait_http(f"http://127.0.0.1:{port_b}/")
        before = h.pids()
        target = root / "a" / "main.py"
        target.write_text(target.read_text(encoding="utf-8") + "# only-a\n", encoding="utf-8")
        deadline = time.time() + 12
        while time.time() < deadline and h.pids().get("a") == before.get("a"):
            time.sleep(0.1)
        after = h.pids()
        assert after.get("a") != before.get("a")
        assert after.get("b") == before.get("b")
        assert "reloading b" not in h.blob().lower()
    finally:
        h.stop()
