"""Resolve service ports from config, commands, or live process sockets."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Sequence, Tuple, Union
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


def pids_listening_on_port(port: int) -> List[int]:
    """Return unique PIDs that currently hold a TCP LISTEN on ``port``."""

    port = int(port)
    if not (1 <= port <= 65535):
        return []
    try:
        if sys.platform == "win32":
            return _pids_listening_on_port_windows(port)
        return _pids_listening_on_port_posix(port)
    except Exception:
        return []


def process_name_for_pid(pid: int) -> Optional[str]:
    """
    Best-effort executable / process name for ``pid``.

    Returns a short label such as ``python.exe`` or ``python3`` when known,
    otherwise ``None``. Never raises.
    """

    if pid <= 0:
        return None
    try:
        if sys.platform == "win32":
            return _process_name_windows(pid)
        return _process_name_posix(pid)
    except Exception:
        return None


def describe_port_owners(port: int) -> List[Tuple[int, str]]:
    """
    Return ``(pid, label)`` pairs for processes listening on ``port``.

    ``label`` is the executable name when available, otherwise ``unknown``.
    """

    owners: List[Tuple[int, str]] = []
    for pid in pids_listening_on_port(port):
        name = process_name_for_pid(pid) or "unknown"
        owners.append((int(pid), name))
    return owners


def _process_name_windows(pid: int) -> Optional[str]:
    import ctypes
    from ctypes import wintypes

    TH32CS_SNAPPROCESS = 0x00000002
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID_HANDLE_VALUE or not snap:
        return None

    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    try:
        if not kernel32.Process32FirstW(snap, ctypes.byref(entry)):
            return None
        target = int(pid)
        while True:
            if int(entry.th32ProcessID) == target:
                name = str(entry.szExeFile or "").strip()
                return name or None
            if not kernel32.Process32NextW(snap, ctypes.byref(entry)):
                break
    finally:
        kernel32.CloseHandle(snap)
    return None


def _process_name_posix(pid: int) -> Optional[str]:
    if sys.platform.startswith("linux"):
        try:
            with open(f"/proc/{int(pid)}/comm", encoding="utf-8") as handle:
                name = handle.read().strip()
            if name:
                return name
        except OSError:
            pass
        try:
            target = os.readlink(f"/proc/{int(pid)}/exe")
            if target:
                return Path(target).name
        except OSError:
            pass

    completed = subprocess.run(
        ["ps", "-p", str(int(pid)), "-o", "comm="],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode == 0 and completed.stdout:
        name = completed.stdout.strip().splitlines()[0].strip()
        if name:
            return Path(name).name
    return None


def pid_tree_owns_port(root_pid: int, port: int) -> Optional[bool]:
    """
    Whether ``port`` is owned by ``root_pid`` or a descendant / process-group peer.

    Returns:
      ``True``  — a listener on ``port`` belongs to the StackPilot-spawned tree
      ``False`` — a foreign process owns the listen socket
      ``None``  — nothing is listening on ``port`` yet

    Ancestors of ``root_pid`` (test runners, shells, orchestrator parents) are
    never treated as part of the service tree — a parent-held listen socket is
    a foreign occupation even when the child shares the parent's process group.

    On macOS CI, port→PID backends can briefly mis-attribute a just-bound
    socket. We therefore also accept a positive match from
    :func:`listening_ports_for_pid` on the spawned tree before declaring
    foreign ownership.
    """

    if root_pid <= 0:
        return None

    tree = _process_tree_pids(root_pid)
    port_i = int(port)
    for pid in tree:
        try:
            if port_i in listening_ports_for_pid(int(pid)):
                return True
        except Exception:
            continue

    owners = pids_listening_on_port(port_i)
    if not owners:
        return None
    ancestors = _ancestor_pids(root_pid)
    for owner in owners:
        if owner in ancestors:
            continue
        if owner in tree:
            return True
    return False


def _process_tree_pids(root_pid: int) -> set[int]:
    """
    PIDs in the spawned tree: root, descendants, and (when safe) PG peers.

    POSIX process-group expansion is only applied when ``root_pid`` is the
    process-group leader (``getpgid(pid) == pid``). StackPilot spawns with
    ``start_new_session=True``, so managed children are leaders and workers
    that remain in the same group are correctly included.

    Expanding the group for a non-leader child (e.g. a raw ``Popen`` that
    inherits the pytest / shell process group) would incorrectly treat the
    parent process — and any foreign listener it holds — as part of the
    service tree, producing a false healthy state on Linux/macOS.
    """

    tree: set[int] = {int(root_pid)}
    try:
        tree.update(_descendant_pids(root_pid))
    except Exception:
        pass
    if sys.platform != "win32":
        try:
            pgid = os.getpgid(root_pid)
            # Only trust PG peers when this PID leads the group.
            if pgid == int(root_pid):
                tree.update(_pids_in_process_group(pgid))
        except (ProcessLookupError, PermissionError, OSError):
            pass
    return tree


def _ancestor_pids(root_pid: int) -> set[int]:
    """Best-effort parent chain of ``root_pid`` (exclusive of itself)."""

    ancestors: set[int] = set()
    try:
        pairs = (
            _windows_pid_ppid_pairs()
            if sys.platform == "win32"
            else _posix_pid_ppid_pairs()
        )
    except Exception:
        return ancestors
    parent_of = {pid: ppid for pid, ppid in pairs}
    current = int(root_pid)
    # Bound the walk so a corrupt / cyclic ppid map cannot loop forever.
    for _ in range(64):
        parent = parent_of.get(current)
        if parent is None or parent <= 0 or parent == current or parent in ancestors:
            break
        ancestors.add(parent)
        current = parent
    return ancestors


def _descendant_pids(root_pid: int) -> set[int]:
    """Breadth-first children of ``root_pid`` (best-effort, cross-platform)."""

    children_of = _parent_to_children_map()
    found: set[int] = set()
    queue = [int(root_pid)]
    while queue:
        current = queue.pop()
        for child in children_of.get(current, ()):
            if child in found or child == root_pid:
                continue
            found.add(child)
            queue.append(child)
    return found


def _parent_to_children_map() -> dict[int, list[int]]:
    mapping: dict[int, list[int]] = {}
    try:
        if sys.platform == "win32":
            pairs = _windows_pid_ppid_pairs()
        else:
            pairs = _posix_pid_ppid_pairs()
    except Exception:
        return mapping
    for pid, ppid in pairs:
        mapping.setdefault(ppid, []).append(pid)
    return mapping


def _windows_pid_ppid_pairs() -> list[tuple[int, int]]:
    """Return (pid, ppid) pairs via Toolhelp32 (no WMIC dependency)."""

    import ctypes
    from ctypes import wintypes

    TH32CS_SNAPPROCESS = 0x00000002
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID_HANDLE_VALUE or not snap:
        return []

    pairs: list[tuple[int, int]] = []
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    try:
        if not kernel32.Process32FirstW(snap, ctypes.byref(entry)):
            return []
        while True:
            pairs.append((int(entry.th32ProcessID), int(entry.th32ParentProcessID)))
            if not kernel32.Process32NextW(snap, ctypes.byref(entry)):
                break
    finally:
        kernel32.CloseHandle(snap)
    return pairs


def _posix_pid_ppid_pairs() -> list[tuple[int, int]]:
    pairs: list[tuple[int, int]] = []
    if sys.platform.startswith("linux"):
        try:
            for name in os.listdir("/proc"):
                if not name.isdigit():
                    continue
                try:
                    with open(f"/proc/{name}/stat", encoding="utf-8") as handle:
                        text = handle.read()
                except OSError:
                    continue
                # pid (comm) state ppid ...
                close = text.rfind(")")
                if close < 0:
                    continue
                rest = text[close + 2 :].split()
                if len(rest) < 2:
                    continue
                try:
                    pairs.append((int(name), int(rest[1])))
                except ValueError:
                    continue
            return pairs
        except OSError:
            pass
    completed = subprocess.run(
        ["ps", "-Ao", "pid=,ppid="],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0 or not completed.stdout:
        return pairs
    for line in completed.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            pairs.append((int(parts[0]), int(parts[1])))
        except ValueError:
            continue
    return pairs


def _pids_in_process_group(pgid: int) -> set[int]:
    found: set[int] = set()
    if pgid <= 0:
        return found
    if sys.platform.startswith("linux"):
        try:
            for name in os.listdir("/proc"):
                if not name.isdigit():
                    continue
                try:
                    with open(f"/proc/{name}/stat", encoding="utf-8") as handle:
                        text = handle.read()
                except OSError:
                    continue
                close = text.rfind(")")
                if close < 0:
                    continue
                rest = text[close + 2 :].split()
                # After state,ppid: pgrp is index 2 in rest (man proc_pid_stat)
                if len(rest) < 3:
                    continue
                try:
                    if int(rest[2]) == pgid:
                        found.add(int(name))
                except ValueError:
                    continue
            return found
        except OSError:
            pass
    completed = subprocess.run(
        ["ps", "-Ao", "pid=,pgid="],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0 or not completed.stdout:
        return found
    for line in completed.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            pid, group = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        if group == pgid:
            found.add(pid)
    return found


def _pids_listening_on_port_windows(port: int) -> List[int]:
    completed = subprocess.run(
        ["netstat", "-ano", "-p", "tcp"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if not completed.stdout:
        return []
    pids: set[int] = set()
    for line in completed.stdout.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        if parts[0].upper() != "TCP":
            continue
        state = parts[3].upper() if len(parts) >= 5 else ""
        if state not in {"LISTENING", "LISTEN"}:
            continue
        local = parts[1]
        local_port = _port_from_local_addr(local)
        if local_port != port:
            continue
        try:
            pids.add(int(parts[-1]))
        except ValueError:
            continue
    return sorted(pids)


def _pids_listening_on_port_posix(port: int) -> List[int]:
    """
    Resolve LISTEN owners for ``port`` on POSIX.

    Backends (first non-empty wins):
    1. optional ``psutil``
    2. ``lsof``
    3. Linux ``/proc/net/tcp{,6}`` + fd inode map
    4. ``ss -lptn``
    5. ``netstat -lptn`` / ``netstat -anv`` (macOS)
    """

    port = int(port)
    for resolver in (
        _pids_listening_on_port_psutil,
        _pids_listening_on_port_lsof,
        _pids_listening_on_port_linux_proc if sys.platform.startswith("linux") else None,
        _pids_listening_on_port_ss,
        _pids_listening_on_port_netstat_posix,
    ):
        if resolver is None:
            continue
        try:
            found = resolver(port)
        except Exception:
            continue
        if found:
            return sorted(set(int(pid) for pid in found if int(pid) > 0))
    return []


def _pids_listening_on_port_psutil(port: int) -> List[int]:
    try:
        import psutil  # type: ignore
    except ImportError:
        return []
    pids: set[int] = set()
    try:
        connections = psutil.net_connections(kind="tcp")
    except (psutil.AccessDenied, psutil.Error, OSError, AttributeError):
        return []
    for conn in connections:
        try:
            if conn.status != psutil.CONN_LISTEN:
                continue
            laddr = conn.laddr
            if not laddr:
                continue
            local_port = int(getattr(laddr, "port", None) or laddr[1])
            if local_port != port:
                continue
            if conn.pid:
                pid = int(conn.pid)
                if pid > 1:
                    pids.add(pid)
        except (TypeError, ValueError, AttributeError):
            continue
    return sorted(pids)


def _pids_listening_on_port_lsof(port: int) -> List[int]:
    try:
        completed = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{int(port)}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=0.5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []
    pids: set[int] = set()
    if completed.returncode != 0 or not completed.stdout:
        return []
    for line in completed.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[1])
        except ValueError:
            continue
        if pid > 1:
            pids.add(pid)
    return sorted(pids)


def _pids_listening_on_port_ss(port: int) -> List[int]:
    """Parse ``ss -lptn`` listeners (Linux; often available when lsof is not)."""

    try:
        completed = subprocess.run(
            ["ss", "-lptn"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=0.5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []
    if completed.returncode != 0 or not completed.stdout:
        return []
    pids: set[int] = set()
    # Example: LISTEN 0 128 127.0.0.1:8000 0.0.0.0:* users:(("python",pid=123,fd=3))
    target = int(port)
    for line in completed.stdout.splitlines():
        if "LISTEN" not in line.upper():
            continue
        # Local address is typically column 4 in `ss -lptn`.
        parts = line.split()
        local = parts[3] if len(parts) >= 4 else ""
        if _port_from_local_addr(local) != target:
            # IPv6 / atypical columns: scan tokens before users= only.
            before_users = line.split("users:", 1)[0]
            if not any(
                _port_from_local_addr(token) == target for token in before_users.split()
            ):
                continue
        for match in re.finditer(r"pid=(\d+)", line):
            try:
                pid = int(match.group(1))
            except ValueError:
                continue
            if pid > 1:
                pids.add(pid)
    return sorted(pids)


def _pids_listening_on_port_netstat_posix(port: int) -> List[int]:
    """Best-effort ``netstat`` parse for Linux/macOS when ss/lsof are absent."""

    if sys.platform == "darwin":
        # Linux-style -lptn is invalid on macOS; skip it to avoid timeout noise.
        candidates = (
            ["netstat", "-anv", "-p", "tcp"],
            ["netstat", "-an", "-p", "tcp"],
        )
    else:
        candidates = (
            ["netstat", "-lptn"],
            ["netstat", "-anv", "-p", "tcp"],
            ["netstat", "-an", "-p", "tcp"],
        )
    pids: set[int] = set()
    target = int(port)
    for argv in candidates:
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=0.5,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
        if not completed.stdout:
            continue
        for line in completed.stdout.splitlines():
            upper = line.upper()
            if "LISTEN" not in upper:
                continue
            # Exact host:port / host.port match only — never substring
            # (":1" must not match ":1234", ".80" must not match ".8080").
            if not any(_netstat_token_has_port(token, target) for token in line.split()):
                continue
            # Linux: ... LISTEN 1234/python
            match = re.search(r"\b(\d+)/(?:\S+)", line)
            if match:
                pid = int(match.group(1))
                if pid > 1:
                    pids.add(pid)
                continue
            # macOS netstat -anv: pid is a dedicated column near the end.
            # Prefer the token immediately after LISTEN-related state fields
            # when present; never treat the port number itself as a PID.
            pid = _macos_netstat_pid(line, target)
            if pid is not None and pid > 1:
                pids.add(pid)
        if pids:
            return sorted(pids)
    return sorted(pids)


def _macos_netstat_pid(line: str, port: int) -> Optional[int]:
    """
    Extract the process id from a macOS ``netstat -anv -p tcp`` LISTEN row.

    Columns vary by OS version; scanning digits from the right can pick up
    queue sizes or the port itself. Prefer ``pid/name`` never applies here —
    reject the listen port value and prefer the rightmost plausible pid.
    """

    parts = line.split()
    candidates: list[int] = []
    for token in parts:
        if not token.isdigit():
            continue
        value = int(token)
        if value <= 1 or value == int(port):
            continue
        # Ephemeral-looking / typical PID range; exclude common watermarks.
        if value in {131072, 65536, 32768, 16384}:
            continue
        candidates.append(value)
    if not candidates:
        return None
    # On modern macOS -anv, pid is usually the rightmost non-flag integer.
    return candidates[-1]


def _netstat_token_has_port(token: str, port: int) -> bool:
    """True when a netstat address token refers exactly to ``port``."""

    text = token.strip()
    if not text:
        return False
    parsed = _port_from_local_addr(text)
    if parsed == int(port):
        return True
    # macOS often prints ``*.8000`` / ``127.0.0.1.8000``.
    if "." in text and ":" not in text:
        tail = text.rsplit(".", 1)[-1]
        if tail.isdigit() and int(tail) == int(port):
            return True
    return False


def _pids_listening_on_port_linux_proc(port: int) -> List[int]:
    """Map listen inode(s) for ``port`` back to owning PIDs via /proc."""

    inodes: set[int] = set()
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            text = Path_read(table)
        except OSError:
            continue
        for line in text.splitlines()[1:]:
            parts = line.split()
            if len(parts) < 10:
                continue
            if parts[3] != "0A":
                continue
            local = parts[1]
            if ":" not in local:
                continue
            try:
                local_port = int(local.rsplit(":", 1)[1], 16)
            except ValueError:
                continue
            if local_port != port:
                continue
            try:
                inodes.add(int(parts[9]))
            except ValueError:
                continue
    if not inodes:
        return []

    pids: set[int] = set()
    try:
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            pid = int(name)
            owned = _socket_inodes_for_pid(pid)
            if owned & inodes:
                pids.add(pid)
    except OSError:
        return []
    return sorted(pids)


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
    try:
        completed = subprocess.run(
            ["lsof", "-nP", f"-p{int(pid)}", "-iTCP", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=0.5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        completed = None
    ports: set[int] = set()
    if completed is not None and completed.returncode == 0 and completed.stdout:
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
