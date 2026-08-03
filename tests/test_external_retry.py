"""P1 regression: external dependency retries with delayed / failing TCP peers."""

from __future__ import annotations

import socket
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, List, Tuple
from unittest.mock import patch

import pytest

from stackpilot.config import ExternalDependency, ServiceSpec, Stack, TcpHealthCheck
from stackpilot.dependency_graph import build_graph
from stackpilot.external_validation import (
    ExternalDependencyError,
    format_external_unavailable,
    validate_external_dependencies,
    wait_for_external_dependency,
)
from stackpilot.health import HealthCheckTimeout


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def _tcp_listener(host: str = "127.0.0.1") -> Iterator[Tuple[str, int]]:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, 0))
    srv.listen(5)
    port = int(srv.getsockname()[1])
    stop = threading.Event()

    def _accept_loop() -> None:
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                conn.close()
            except OSError:
                pass

    thread = threading.Thread(target=_accept_loop, daemon=True)
    thread.start()
    try:
        yield host, port
    finally:
        stop.set()
        try:
            srv.close()
        except OSError:
            pass
        thread.join(timeout=2.0)


@contextmanager
def _delayed_tcp_listener(
    delay_s: float,
    host: str = "127.0.0.1",
) -> Iterator[Tuple[str, int]]:
    """Bind immediately but only accept after ``delay_s`` (simulates slow boot)."""

    port = _free_port()
    stop = threading.Event()
    ready = threading.Event()

    def _run() -> None:
        time.sleep(delay_s)
        if stop.is_set():
            return
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind((host, port))
            srv.listen(5)
            ready.set()
            srv.settimeout(0.2)
            while not stop.is_set():
                try:
                    conn, _ = srv.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                try:
                    conn.close()
                except OSError:
                    pass
        finally:
            try:
                srv.close()
            except OSError:
                pass

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    try:
        yield host, port
    finally:
        stop.set()
        thread.join(timeout=3.0)


class TestExternalRetryProgress:
    def test_progress_messages_and_connected(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with _tcp_listener() as (host, port):
            stack = Stack()
            stack.external_dependency(
                name="postgres",
                type="postgresql",
                host=host,
                port=port,
                retries=3,
                retry_delay=0.01,
            )
            stack.service(
                name="auth",
                path=".",
                command="true",
                depends_on=["postgres"],
            )
            graph = build_graph(stack)
            validate_external_dependencies(
                graph,
                ordered_services=graph.ordered_specs(),
            )
        out = capsys.readouterr().out
        assert "Checking PostgreSQL..." in out
        assert "Attempt 1/3..." in out
        assert "Connected." in out
        assert f"✓ PostgreSQL ({host}:{port})" in out

    def test_delayed_startup_succeeds_with_retries(self) -> None:
        with _delayed_tcp_listener(0.35) as (host, port):
            dep = ExternalDependency(
                name="postgres",
                type="postgresql",
                host=host,
                port=port,
                retries=8,
                retry_delay=0.1,
                retry_backoff="fixed",
                health_check=TcpHealthCheck(
                    host=host,
                    port=port,
                    timeout=3.0,
                    interval=0.1,
                    probe_timeout=0.2,
                ),
            )
            elapsed = wait_for_external_dependency(dep)
        assert elapsed >= 0.3

    def test_permanent_failure_reports_details(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        port = _free_port()
        stack = Stack()
        stack.external_dependency(
            name="postgres",
            type="postgresql",
            host="127.0.0.1",
            port=port,
            retries=3,
            retry_delay=0.01,
            health_check=TcpHealthCheck(
                host="127.0.0.1",
                port=port,
                timeout=0.5,
                interval=0.01,
                probe_timeout=0.05,
            ),
        )
        stack.service(
            name="auth",
            path=".",
            command="true",
            depends_on=["postgres"],
        )
        graph = build_graph(stack)
        with pytest.raises(ExternalDependencyError) as exc_info:
            validate_external_dependencies(
                graph,
                ordered_services=graph.ordered_specs(),
            )
        message = str(exc_info.value)
        assert "Problem: Dependency unavailable" in message
        assert "Host: 127.0.0.1" in message
        assert f"Port: {port}" in message
        assert "Elapsed:" in message
        assert "Attempts:" in message
        assert "Suggested fix:" in message
        assert "Startup aborted." in message
        out = capsys.readouterr().out
        assert "Attempt 1/3..." in out
        assert "Attempt 3/3..." in out

    def test_exponential_backoff_increases_sleep(self) -> None:
        sleeps: List[float] = []
        dep = ExternalDependency(
            name="redis",
            type="redis",
            host="127.0.0.1",
            port=_free_port(),
            retries=4,
            retry_delay=0.1,
            retry_backoff="exponential",
            health_check=TcpHealthCheck(
                host="127.0.0.1",
                port=1,
                timeout=5.0,
                interval=0.1,
                probe_timeout=0.01,
            ),
        )

        def fake_check(_dep: ExternalDependency) -> bool:
            return False

        with patch(
            "stackpilot.external_validation.check_external_dependency",
            side_effect=fake_check,
        ):
            with pytest.raises(HealthCheckTimeout):
                wait_for_external_dependency(
                    dep,
                    sleep=sleeps.append,
                    clock=time.monotonic,
                )
        # Three sleeps between 4 attempts: 0.1, 0.2, 0.4
        assert len(sleeps) == 3
        assert sleeps[0] == pytest.approx(0.1)
        assert sleeps[1] == pytest.approx(0.2)
        assert sleeps[2] == pytest.approx(0.4)

    def test_format_includes_next_action(self) -> None:
        dep = ExternalDependency(
            name="postgres",
            type="postgresql",
            host="127.0.0.1",
            port=5432,
        )
        text = format_external_unavailable(
            dep,
            dependents=["auth"],
            elapsed_s=2.5,
            attempts=5,
        )
        assert "Checking PostgreSQL..." in text
        assert "Host: 127.0.0.1" in text
        assert "Port: 5432" in text
        assert "Elapsed: 2.5s" in text
        assert "Attempts: 5/5" in text
        assert "Suggested fix:" in text
        assert "- auth" in text


class TestExternalDefaults:
    def test_default_retries_and_timeout(self) -> None:
        dep = ExternalDependency(
            name="postgres",
            type="postgresql",
            host="127.0.0.1",
            port=5432,
        )
        assert dep.retries == 5
        assert dep.retry_delay == 0.5
        assert dep.retry_backoff == "fixed"
        assert isinstance(dep.health_check, TcpHealthCheck)
        assert dep.health_check.timeout == 10.0
