"""Platform helpers for process-group isolation and tree cleanup."""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
from ctypes import wintypes
from typing import Any, Optional


def spawn_kwargs() -> dict[str, Any]:
    """
    Extra ``Popen`` kwargs so children are isolated from StackPilot Ctrl+C.

    Windows: ``CREATE_NEW_PROCESS_GROUP``
    POSIX: ``start_new_session=True``
    """

    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def signal_process_tree(pid: int, *, graceful: bool) -> None:
    """
    Signal a process and its descendants.

    On Unix the child was started with ``start_new_session=True``, so the
    process group is killed with ``killpg``. On Windows prefer a Job Object
    (see ``WindowsProcessJob``); this function is the fallback using
    ``taskkill /T`` (never console control events).
    """

    if sys.platform == "win32":
        _signal_windows_tree(pid, graceful=graceful)
    else:
        _signal_posix_tree(pid, graceful=graceful)


def _signal_posix_tree(pid: int, *, graceful: bool) -> None:
    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, PermissionError, OSError):
        return

    sig = signal.SIGTERM if graceful else signal.SIGKILL
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def _signal_windows_tree(pid: int, *, graceful: bool) -> None:
    """
    Windows fallback when no Job Object is available.

    Avoid ``CTRL_BREAK_EVENT`` here: on a shared console it can raise
    ``KeyboardInterrupt`` in the StackPilot / pytest process. Prefer
    ``TerminateProcess`` via ``taskkill`` (and Job Objects in ProcessManager).
    """

    del graceful  # Windows fallback has no safe console-signal graceful path.
    try:
        subprocess.run(
            ["taskkill", "/T", "/PID", str(pid)],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        pass
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        try:
            os.kill(pid, signal.SIGTERM)
        except (OSError, AttributeError, ValueError):
            pass


# ---------------------------------------------------------------------------
# Windows Job Objects — kill the whole tree even after the root exits
# ---------------------------------------------------------------------------

if sys.platform == "win32":
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _JobObjectExtendedLimitInformation = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_TERMINATE = 0x0001
    _PROCESS_ASSIGN = _PROCESS_SET_QUOTA | _PROCESS_TERMINATE

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
            ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL


class WindowsProcessJob:
    """
    A Windows Job Object that owns a service process tree.

    Child processes inherit job membership. Closing / terminating the job
    kills every remaining member — no orphans after the root exits.
    """

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError("WindowsProcessJob is only available on Windows")
        handle = _kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise OSError("CreateJobObjectW failed")

        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = _kernel32.SetInformationJobObject(
            handle,
            _JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            _kernel32.CloseHandle(handle)
            raise OSError("SetInformationJobObject failed")

        self._handle: Optional[int] = handle

    def assign(self, pid: int) -> None:
        if self._handle is None:
            return
        process = _kernel32.OpenProcess(_PROCESS_ASSIGN, False, pid)
        if not process:
            raise OSError(f"OpenProcess failed for pid={pid}")
        try:
            if not _kernel32.AssignProcessToJobObject(self._handle, process):
                raise OSError(f"AssignProcessToJobObject failed for pid={pid}")
        finally:
            _kernel32.CloseHandle(process)

    def terminate(self) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            _kernel32.TerminateJobObject(handle, 1)
        finally:
            _kernel32.CloseHandle(handle)
            self._handle = None

    def close(self) -> None:
        """Close the job (``KILL_ON_JOB_CLOSE`` terminates remaining members)."""

        handle = self._handle
        if handle is None:
            return
        _kernel32.CloseHandle(handle)
        self._handle = None
