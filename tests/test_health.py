"""Tests for the Health Check Engine (HTTP, TCP, process, timeout, gating)."""

from __future__ import annotations

import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from stackpilot.config import Stack
from stackpilot.health import Health, HealthCheckError, HealthCheckTimeout
from stackpilot.http_checker import check_http
from stackpilot.process_checker import check_process
from stackpilot.runner import Runner
from stackpilot.tcp_checker import check_tcp


class _DelayedHTTPHandler(BaseHTTPRequestHandler):
    """Respond 200 on /health only after ``ready_after`` monotonic time."""

    ready_after: float = 0.0

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
        if self.path.rstrip("/") != "/health":
            self.send_response(404)
            self.end_headers()
            return
        if time.monotonic() < self.ready_after:
            self.send_response(503)
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


def _serve_http_until(*, ready_after_s: float, stop: threading.Event) -> str:
    """Start a daemon HTTP server; return its base URL."""

    handler = type(
        "Handler",
        (_DelayedHTTPHandler,),
        {"ready_after": time.monotonic() + ready_after_s},
    )
    server = HTTPServer(("127.0.0.1", 0), handler)
    host, port = server.server_address[:2]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def _shutdown() -> None:
        # Bound wait so a leaked Event cannot pin a helper thread forever.
        stop.wait(timeout=120.0)
        try:
            server.shutdown()
        except Exception:
            pass

    threading.Thread(target=_shutdown, daemon=True).start()
    return f"http://127.0.0.1:{port}"


def _listen_tcp_after(*, delay_s: float, stop: threading.Event) -> tuple[str, int]:
    """Reserve a port, open the listener after ``delay_s``, return host/port."""

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    host, port = probe.getsockname()
    probe.close()

    def _open() -> None:
        time.sleep(delay_s)
        if stop.is_set():
            return
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((host, port))
        srv.listen(1)
        srv.settimeout(0.5)
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
                conn.close()
            except socket.timeout:
                continue
            except OSError:
                break
        srv.close()

    threading.Thread(target=_open, daemon=True).start()
    return host, port


def test_http_checker_becomes_healthy_after_delay() -> None:
    stop = threading.Event()
    try:
        base = _serve_http_until(ready_after_s=5.0, stop=stop)
        url = f"{base}/health"
        assert check_http(url) is False
        elapsed = Health.wait_until_healthy(
            "api",
            {"type": "http", "url": url, "interval": 0.25, "timeout": 15},
        )
        assert elapsed >= 4.5
        assert check_http(url) is True
    finally:
        stop.set()


def test_tcp_checker_opens_after_delay() -> None:
    stop = threading.Event()
    try:
        host, port = _listen_tcp_after(delay_s=0.5, stop=stop)
        assert check_tcp(host, port, connect_timeout=0.2) is False
        elapsed = Health.wait_until_healthy(
            "postgres",
            {
                "type": "tcp",
                "host": host,
                "port": port,
                "interval": 0.1,
                "timeout": 5,
            },
        )
        assert elapsed >= 0.4
        assert check_tcp(host, port) is True
    finally:
        stop.set()


def test_process_checker_dummy_alive(tmp_path: Path) -> None:
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=str(tmp_path),
    )
    try:
        assert check_process(proc) is True
        elapsed = Health.wait_until_healthy(
            "worker",
            {"type": "process", "interval": 0.05, "timeout": 2},
            process=proc,
        )
        assert elapsed < 1.0
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_process_checker_dead_is_unhealthy() -> None:
    proc = subprocess.Popen([sys.executable, "-c", "raise SystemExit(0)"])
    proc.wait(timeout=5)
    assert check_process(proc) is False
    assert check_process(None) is False


def test_health_timeout_raises() -> None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    closed_port = int(probe.getsockname()[1])
    probe.close()
    with pytest.raises(HealthCheckTimeout) as exc:
        Health.wait_until_healthy(
            "auth",
            {
                "type": "tcp",
                "host": "127.0.0.1",
                "port": closed_port,
                "interval": 0.05,
                "timeout": 0.25,
            },
        )
    assert exc.value.name == "auth"


def test_http_healthy_when_port_ownership_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    When listener PID mapping returns None, still accept a successful HTTP probe.

    CI hosts intermittently fail /proc|ss|lsof attribution; skipping probes on
    None previously caused false health timeouts while the endpoint was up.
    """

    stop = threading.Event()
    try:
        base = _serve_http_until(ready_after_s=0.0, stop=stop)
        url = f"{base}/health"
        monkeypatch.setattr(
            "stackpilot.health.pid_tree_owns_port",
            lambda *_a, **_k: None,
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
        )
        try:
            elapsed = Health.wait_until_healthy(
                "api",
                {"type": "http", "url": url, "interval": 0.05, "timeout": 3},
                process=proc,
            )
            assert elapsed >= 0.0
            assert check_http(url) is True
        finally:
            proc.kill()
            proc.wait(timeout=5)
    finally:
        stop.set()


def test_dispatch_unknown_type_raises() -> None:
    with pytest.raises(HealthCheckError, match="Unknown health check type"):
        Health.dispatch({"type": "magic"})


def test_runner_waits_for_http_before_dependent(
    tmp_path: Path, monkeypatch
) -> None:
    printed: list[str] = []
    monkeypatch.setattr(
        "builtins.print",
        lambda *args, **kwargs: printed.append(" ".join(str(a) for a in args)),
    )

    # Bind a free port, then have the managed service listen on it after a delay
    # so health must wait for *our* process (not a foreign listener).
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = int(probe.getsockname()[1])
    probe.close()
    url = f"http://127.0.0.1:{port}/health"
    marker = tmp_path / "auth_started"
    api_script = tmp_path / "api_server.py"
    api_script.write_text(
        "import time\n"
        "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
        f"PORT = {port}\n"
        "time.sleep(0.8)\n"
        "class H(BaseHTTPRequestHandler):\n"
        "    hits = 0\n"
        "    def do_GET(self):\n"
        "        H.hits += 1\n"
        "        self.send_response(200); self.end_headers(); self.wfile.write(b'ok')\n"
        "    def log_message(self, *a):\n"
        "        pass\n"
        "print('api up', flush=True)\n"
        "srv = HTTPServer(('127.0.0.1', PORT), H)\n"
        "srv.timeout = 0.5\n"
        "deadline = time.time() + 4.0\n"
        "while time.time() < deadline:\n"
        "    srv.handle_request()\n",
        encoding="utf-8",
    )

    stack = Stack()
    stack.service(
        name="api",
        path=tmp_path,
        command=f"{sys.executable} api_server.py",
        health_check={
            "type": "http",
            "url": url,
            "interval": 0.1,
            "timeout": 8,
        },
    )
    stack.service(
        name="auth",
        path=tmp_path,
        command=(
            f'python -c "from pathlib import Path; '
            f'Path(r\'{marker}\').write_text(\'ok\'); '
            f'import time; time.sleep(0.3)"'
        ),
        depends_on=["api"],
        health_check={"type": "process", "interval": 0.05, "timeout": 5},
    )

    assert not marker.exists()
    code = Runner(logs_dir=tmp_path / "logs", poll_interval_s=0.05).run(stack)
    assert code == 0
    assert marker.exists()

    start_api = next(i for i, line in enumerate(printed) if line == "Starting api...")
    healthy_api = next(
        i for i, line in enumerate(printed) if "api healthy" in line
    )
    start_auth = next(
        i for i, line in enumerate(printed) if line == "Starting auth..."
    )
    assert start_api < healthy_api < start_auth
    assert any("Waiting for api..." in line for line in printed)
    assert any("Starting application services..." in line for line in printed)
    assert any("Press Ctrl+C to stop." in line for line in printed)
    # No reload=True on these services — do not claim a file watcher is active.
    assert not any("Watching for changes..." in line for line in printed)


def test_runner_aborts_on_health_timeout_and_stops_started(
    tmp_path: Path, monkeypatch
) -> None:
    printed: list[str] = []
    monkeypatch.setattr(
        "builtins.print",
        lambda *args, **kwargs: printed.append(" ".join(str(a) for a in args)),
    )

    # Use a just-released ephemeral port — never hardcode port 1.
    # On macOS CI, launchd/PID 1 is often reported as owning TCP/1, which
    # raises PortOwnershipError instead of the health-timeout path.
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    closed_port = int(probe.getsockname()[1])
    probe.close()

    marker = tmp_path / "dependent_started"
    stack = Stack()
    stack.service(
        name="postgres",
        path=tmp_path,
        command='python -c "import time; time.sleep(30)"',
        health_check={
            "type": "tcp",
            "host": "127.0.0.1",
            "port": closed_port,
            "interval": 0.05,
            "timeout": 0.3,
        },
    )
    stack.service(
        name="auth",
        path=tmp_path,
        command=(
            f'python -c "from pathlib import Path; '
            f'Path(r\'{marker}\').write_text(\'ok\'); '
            f'import time; time.sleep(1)"'
        ),
        depends_on=["postgres"],
    )

    code = Runner(logs_dir=tmp_path / "logs", poll_interval_s=0.05).run(stack)

    assert code == 1
    assert not marker.exists()
    assert any("postgres failed health check" in line for line in printed)
    assert "Startup aborted." in printed
    assert not any(line == "Starting auth..." for line in printed)
    assert not any("Watching for changes..." in line for line in printed)


def test_runner_default_process_health_messages(
    tmp_path: Path, monkeypatch
) -> None:
    printed: list[str] = []
    monkeypatch.setattr(
        "builtins.print",
        lambda *args, **kwargs: printed.append(" ".join(str(a) for a in args)),
    )

    stack = Stack()
    stack.service(
        name="redis",
        path=tmp_path,
        command='python -c "import time; time.sleep(0.2)"',
    )

    code = Runner(logs_dir=tmp_path / "logs", poll_interval_s=0.05).run(stack)
    assert code == 0
    assert "Starting redis..." in printed
    assert "Waiting for redis..." in printed
    assert any("redis healthy" in line for line in printed)
