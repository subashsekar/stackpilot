"""Port auto-detection for status / ps / dashboard display."""

from __future__ import annotations

from pathlib import Path

import pytest

from stackpilot import port_detect
from stackpilot.config import HttpHealthCheck, ServiceSpec, TcpHealthCheck
from stackpilot.models import configured_port
from stackpilot.port_detect import (
    parse_port_from_command,
    resolve_service_port,
    service_display_url,
)


def test_parse_port_from_uvicorn_flag() -> None:
    assert parse_port_from_command("uvicorn main:app --port 8001") == 8001
    assert parse_port_from_command("uvicorn main:app --port=9000") == 9000
    assert parse_port_from_command(["uvicorn", "main:app", "-p", "8080"]) == 8080


def test_parse_port_from_host_binding() -> None:
    assert parse_port_from_command("python manage.py runserver 0.0.0.0:8000") == 8000
    assert parse_port_from_command("flask run --host 127.0.0.1:5001") == 5001
    assert parse_port_from_command("something localhost:3000") == 3000


def test_parse_port_from_env_style() -> None:
    assert parse_port_from_command("PORT=8123 python app.py") == 8123
    assert parse_port_from_command("env PORT 7000 npm start") == 7000


def test_parse_port_missing() -> None:
    assert parse_port_from_command("python main.py") is None
    assert parse_port_from_command("") is None


def test_resolve_prefers_health_then_explicit_then_command(tmp_path: Path) -> None:
    assert (
        resolve_service_port(
            ServiceSpec(
                name="a",
                path=tmp_path,
                command="uvicorn main:app --port 8001",
                health_check=TcpHealthCheck(host="127.0.0.1", port=9000),
            )
        )
        == 9000
    )
    assert (
        resolve_service_port(
            ServiceSpec(
                name="b",
                path=tmp_path,
                command="uvicorn main:app --port 8001",
                port=8111,
            )
        )
        == 8111
    )
    assert (
        resolve_service_port(
            ServiceSpec(
                name="c",
                path=tmp_path,
                command="uvicorn main:app --port 8001",
            )
        )
        == 8001
    )
    assert (
        resolve_service_port(
            ServiceSpec(
                name="d",
                path=tmp_path,
                command="python main.py",
                health_check=HttpHealthCheck(url="http://127.0.0.1:8088/health"),
            )
        )
        == 8088
    )


def test_configured_port_uses_command_when_no_health(tmp_path: Path) -> None:
    assert (
        configured_port(
            ServiceSpec(
                name="gw",
                path=tmp_path,
                command="npm start -- --port 3000",
            )
        )
        == 3000
    )


def test_service_display_url_from_http_health(tmp_path: Path) -> None:
    assert (
        service_display_url(
            ServiceSpec(
                name="gateway",
                path=tmp_path,
                command="python manage.py runserver 0.0.0.0:8001",
                health_check=HttpHealthCheck(url="http://127.0.0.1:8001/health"),
            )
        )
        == "http://127.0.0.1:8001"
    )


def test_service_display_url_from_port(tmp_path: Path) -> None:
    assert (
        service_display_url(
            ServiceSpec(
                name="api",
                path=tmp_path,
                command="python app.py",
                port=9000,
            )
        )
        == "http://127.0.0.1:9000"
    )


# ---------------------------------------------------------------------------
# Port ownership — Linux process-group / foreign listener regressions
# ---------------------------------------------------------------------------


def test_pid_tree_does_not_claim_ancestor_listener(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Parent-held listen sockets must never count as owned by a child service.

    On Ubuntu CI the pytest process owns the foreign TCP socket while the
    dummy service is a raw Popen child sharing that process group. Including
    non-leader PG peers previously made ``pid_tree_owns_port`` return True.
    """

    child_pid = 4242
    parent_pid = 1000
    port = 55555

    monkeypatch.setattr(
        port_detect,
        "pids_listening_on_port",
        lambda _port: [parent_pid],
    )
    monkeypatch.setattr(
        port_detect,
        "_process_tree_pids",
        lambda _root: {child_pid, parent_pid},  # buggy expansion would include parent
    )
    monkeypatch.setattr(
        port_detect,
        "_ancestor_pids",
        lambda _root: {parent_pid},
    )
    assert port_detect.pid_tree_owns_port(child_pid, port) is False


def test_process_tree_skips_pg_peers_when_not_group_leader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only session/group leaders expand to process-group peers (POSIX)."""

    root = 7777
    peer = 8888

    monkeypatch.setattr(port_detect.sys, "platform", "linux")
    monkeypatch.setattr(port_detect, "_descendant_pids", lambda _pid: set())
    # Windows runners have no os.getpgid; create the attribute for this test.
    monkeypatch.setattr(port_detect.os, "getpgid", lambda pid: 1111, raising=False)
    monkeypatch.setattr(
        port_detect,
        "_pids_in_process_group",
        lambda _pgid: {root, peer, 1111},
    )
    tree = port_detect._process_tree_pids(root)
    assert tree == {root}
    assert peer not in tree


def test_process_tree_includes_pg_peers_when_group_leader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = 7777
    worker = 8888

    monkeypatch.setattr(port_detect.sys, "platform", "linux")
    monkeypatch.setattr(port_detect, "_descendant_pids", lambda _pid: set())
    monkeypatch.setattr(port_detect.os, "getpgid", lambda pid: pid, raising=False)
    monkeypatch.setattr(
        port_detect,
        "_pids_in_process_group",
        lambda _pgid: {root, worker},
    )
    tree = port_detect._process_tree_pids(root)
    assert tree == {root, worker}


def test_foreign_tcp_listener_raises_port_ownership_error() -> None:
    """End-to-end: foreign LISTEN + unrelated child => PortOwnershipError."""

    import socket
    import subprocess
    import sys
    import time

    from stackpilot.config import TcpHealthCheck
    from stackpilot.health import Health, PortOwnershipError

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = int(probe.getsockname()[1])
    probe.close()

    foreign = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    foreign.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    foreign.bind(("127.0.0.1", port))
    foreign.listen(1)
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
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
        assert port_detect.pid_tree_owns_port(proc.pid, port) is False
    finally:
        foreign.close()
        proc.kill()
        proc.wait(timeout=5)
        time.sleep(0.05)


def test_pids_listening_backends_tolerate_missing_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ss/netstat/lsof failures must not abort; empty backends return []."""

    monkeypatch.setattr(port_detect.sys, "platform", "linux")

    def boom(_port: int):
        raise OSError("unavailable")

    monkeypatch.setattr(port_detect, "_pids_listening_on_port_psutil", boom)
    monkeypatch.setattr(port_detect, "_pids_listening_on_port_lsof", boom)
    monkeypatch.setattr(port_detect, "_pids_listening_on_port_linux_proc", lambda _p: [])
    monkeypatch.setattr(port_detect, "_pids_listening_on_port_ss", boom)
    monkeypatch.setattr(port_detect, "_pids_listening_on_port_netstat_posix", boom)
    assert port_detect._pids_listening_on_port_posix(18080) == []


def test_ss_parser_rejects_port_number_prefixes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``:1`` must not match ``:1234`` / ``]:18080`` (CI false foreign owners)."""

    import subprocess

    sample = (
        "State Recv-Q Send-Q Local Address:Port Peer Address:Port Process\n"
        "LISTEN 0 128 127.0.0.1:1234 0.0.0.0:* users:((\"python\",pid=111,fd=3))\n"
        "LISTEN 0 128 [::]:18080 [::]:* users:((\"node\",pid=222,fd=4))\n"
        "LISTEN 0 128 127.0.0.1:1 0.0.0.0:* users:((\"sshd\",pid=333,fd=5))\n"
    )

    class _Completed:
        returncode = 0
        stdout = sample

    original = subprocess.run

    def fake_run(argv, **kwargs):
        if argv and argv[0] == "ss":
            return _Completed()
        return original(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert port_detect._pids_listening_on_port_ss(1) == [333]
    assert port_detect._pids_listening_on_port_ss(1234) == [111]
    assert port_detect._pids_listening_on_port_ss(18080) == [222]
    assert port_detect._pids_listening_on_port_ss(8) == []
    assert port_detect._pids_listening_on_port_ss(80) == []


def test_netstat_parser_rejects_port_number_prefixes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Substring ``:1`` / ``.80`` must not claim unrelated LISTEN rows."""

    import subprocess

    sample = (
        "Proto Recv-Q Send-Q Local Address Foreign Address State PID/Program\n"
        "tcp 0 0 127.0.0.1:1234 0.0.0.0:* LISTEN 111/python\n"
        "tcp 0 0 0.0.0.0:8080 0.0.0.0:* LISTEN 222/node\n"
        "tcp 0 0 127.0.0.1:1 0.0.0.0:* LISTEN 333/sshd\n"
        "tcp4 0 0 127.0.0.1.8000 *.* LISTEN 444\n"
    )

    class _Completed:
        returncode = 0
        stdout = sample

    original = subprocess.run

    def fake_run(argv, **kwargs):
        if argv and argv[0] == "netstat":
            return _Completed()
        return original(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert port_detect._pids_listening_on_port_netstat_posix(1) == [333]
    assert port_detect._pids_listening_on_port_netstat_posix(1234) == [111]
    assert port_detect._pids_listening_on_port_netstat_posix(8080) == [222]
    assert port_detect._pids_listening_on_port_netstat_posix(8000) == [444]
    assert port_detect._pids_listening_on_port_netstat_posix(8) == []
    assert port_detect._pids_listening_on_port_netstat_posix(80) == []


def test_netstat_token_has_exact_port_only() -> None:
    assert port_detect._netstat_token_has_port("127.0.0.1:1", 1)
    assert not port_detect._netstat_token_has_port("127.0.0.1:1234", 1)
    assert not port_detect._netstat_token_has_port("0.0.0.0:8080", 80)
    assert port_detect._netstat_token_has_port("127.0.0.1.8000", 8000)
    assert not port_detect._netstat_token_has_port("127.0.0.1.8000", 80)
