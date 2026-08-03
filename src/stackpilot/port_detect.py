"""Resolve service ports from config, commands, or live process sockets."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from typing import List, Optional, Sequence, Union
from urllib.parse import urlparse

from .config import HttpHealthCheck, ServiceSpec, TcpHealthCheck

# --port 8000 / -p 8000 / --port=8000
_PORT_FLAG_RE = re.compile(
    r"(?:--port|-p)(?:=|\s+)(\d{1,5})\b",
    re.IGNORECASE,
)
# 0.0.0.0:8000 / 127.0.0.1:8000 / localhost:8000
_HOST_PORT_RE = re.compile(
    r"(?:0\.0\.0\.0|127\.0\.0\.1|localhost):(\d{1,5})\b",
    re.IGNORECASE,
)
# PORT=8000 / PORT 8000
_ENV_PORT_RE = re.compile(r"\bPORT(?:=|\s+)(\d{1,5})\b", re.IGNORECASE)


def resolve_service_port(
    spec: ServiceSpec,
    *,
    pid: Optional[int] = None,
) -> Optional[int]:
    """
    Best-effort port for DX display.

    Order:
    1. Live listening TCP port for ``pid`` (when the process is up)
    2. TCP/HTTP health-check port
    3. Explicit ``spec.port``
    4. Port parsed from the launch command
    """

    if pid is not None:
        live = listening_ports_for_pid(pid)
        if live:
            return live[0]

    health_port = port_from_health_check(spec)
    if health_port is not None:
        return health_port

    if spec.port is not None:
        return int(spec.port)

    return parse_port_from_command(spec.command)


def service_display_url(spec: ServiceSpec) -> Optional[str]:
    """
    Best-effort browser URL for a service (scheme://host:port).

    Prefers the HTTP health-check origin; otherwise ``http://127.0.0.1:<port>``.
    """

    health = spec.health_check
    if isinstance(health, HttpHealthCheck):
        text = (health.url or "").strip()
        if text:
            try:
                parsed = urlparse(text)
            except ValueError:
                parsed = None
            if parsed is not None and parsed.scheme and parsed.hostname:
                host = "127.0.0.1" if parsed.hostname in {"0.0.0.0", "::"} else parsed.hostname
                if parsed.port is not None:
                    return f"{parsed.scheme}://{host}:{parsed.port}"
                return f"{parsed.scheme}://{host}"

    port = resolve_service_port(spec)
    if port is None:
        return None
    return f"http://127.0.0.1:{int(port)}"


def port_from_health_check(spec: ServiceSpec) -> Optional[int]:
    health = spec.health_check
    if isinstance(health, TcpHealthCheck):
        return int(health.port)
    if isinstance(health, HttpHealthCheck):
        try:
            parsed = urlparse(health.url.strip())
        except ValueError:
            return None
        return int(parsed.port) if parsed.port is not None else None
    return None


def parse_port_from_command(command: Union[str, Sequence[str]]) -> Optional[int]:
    """Extract a listen port from common CLI patterns."""

    if isinstance(command, (list, tuple)):
        text = " ".join(str(part) for part in command)
    else:
        text = str(command or "")
    if not text.strip():
        return None

    for pattern in (_PORT_FLAG_RE, _HOST_PORT_RE, _ENV_PORT_RE):
        match = pattern.search(text)
        if match:
            port = int(match.group(1))
            if 1 <= port <= 65535:
                return port
    return None


def listening_ports_for_pid(pid: int) -> List[int]:
    """Return sorted unique local TCP listen ports owned by ``pid``."""

    if pid <= 0:
        return []
    try:
        if sys.platform == "win32":
            return _listening_ports_windows(pid)
        return _listening_ports_posix(pid)
    except Exception:
        return []


def _listening_ports_windows(pid: int) -> List[int]:
    # netstat -ano: Proto Local Address Foreign Address State PID
    completed = subprocess.run(
        ["netstat", "-ano", "-p", "tcp"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode not in (0, None) and not completed.stdout:
        return []

    ports: set[int] = set()
    target = str(int(pid))
    for line in completed.stdout.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        # TCP  0.0.0.0:8000  0.0.0.0:0  LISTENING  1234
        if parts[0].upper() != "TCP":
            continue
        state = parts[3].upper() if len(parts) >= 5 else ""
        if state not in {"LISTENING", "LISTEN"}:
            continue
        if parts[-1] != target:
            continue
        local = parts[1]
        port = _port_from_local_addr(local)
        if port is not None:
            ports.add(port)
    return sorted(ports)


def _listening_ports_posix(pid: int) -> List[int]:
    # Prefer lsof when available.
    completed = subprocess.run(
        ["lsof", "-nP", f"-p{int(pid)}", "-iTCP", "-sTCP:LISTEN"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    ports: set[int] = set()
    if completed.returncode == 0 and completed.stdout:
        for line in completed.stdout.splitlines()[1:]:
            # COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME
            # python  123 ... TCP *:8000 (LISTEN)
            if "(LISTEN)" not in line.upper() and "LISTEN" not in line.upper():
                continue
            match = re.search(r":(\d{1,5})(?:\s|\()", line)
            if match:
                port = int(match.group(1))
                if 1 <= port <= 65535:
                    ports.add(port)
        if ports:
            return sorted(ports)

    # Linux fallback via /proc/net/tcp(+6) + inode ownership.
    if sys.platform.startswith("linux"):
        return _listening_ports_linux_proc(pid)
    return sorted(ports)


def _listening_ports_linux_proc(pid: int) -> List[int]:
    inodes = _socket_inodes_for_pid(pid)
    if not inodes:
        return []
    ports: set[int] = set()
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            text = Path_read(table)
        except OSError:
            continue
        for line in text.splitlines()[1:]:
            parts = line.split()
            if len(parts) < 10:
                continue
            # local_address state ... inode
            # 0100007F:1F90 0A000000:0000 0A ...
            if parts[3] != "0A":  # TCP_LISTEN
                continue
            try:
                inode = int(parts[9])
            except ValueError:
                continue
            if inode not in inodes:
                continue
            local = parts[1]
            if ":" not in local:
                continue
            try:
                port = int(local.rsplit(":", 1)[1], 16)
            except ValueError:
                continue
            if 1 <= port <= 65535:
                ports.add(port)
    return sorted(ports)


def _socket_inodes_for_pid(pid: int) -> set[int]:
    fd_dir = f"/proc/{int(pid)}/fd"
    inodes: set[int] = set()
    try:
        for name in os.listdir(fd_dir):
            try:
                target = os.readlink(os.path.join(fd_dir, name))
            except OSError:
                continue
            if target.startswith("socket:["):
                try:
                    inodes.add(int(target[8:-1]))
                except ValueError:
                    continue
    except OSError:
        return set()
    return inodes


def Path_read(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read()


def _port_from_local_addr(local: str) -> Optional[int]:
    # 127.0.0.1:8000 or [::1]:8000 or 0.0.0.0:8000
    if local.startswith("["):
        match = re.search(r"\]:(\d{1,5})$", local)
        if not match:
            return None
        port = int(match.group(1))
    else:
        if ":" not in local:
            return None
        try:
            port = int(local.rsplit(":", 1)[1])
        except ValueError:
            return None
    if 1 <= port <= 65535:
        return port
    return None
