from __future__ import annotations

import importlib.util
import os
import shlex
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import IO, Iterator, List, Mapping, Optional, Sequence, TypeVar, Union

from .config import Stack
from .discovery import STACKFILE_NAME

T = TypeVar("T")

_PYTHON_LAUNCHERS = frozenset({"python", "python3", "py"})


def split_command(
    command: Union[str, Sequence[str]],
    *,
    cwd: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
) -> List[str]:
    """
    Turn a Stackfile command into an argv list for ``subprocess.Popen``.

    Never intended for ``shell=True``. String commands use platform-aware
    parsing: Win32 ``CommandLineToArgvW`` on Windows, POSIX ``shlex`` elsewhere.

    When ``cwd`` is provided, bare ``python`` / ``python3`` / ``py`` resolve
    to the service-local venv (when present) so the child matches
    ``cd <cwd> && python ...``. Without ``cwd``, they fall back to
    ``sys.executable`` (CLI / doctor convenience).
    """

    if cwd is not None:
        from .launch_env import resolve_service_argv

        return resolve_service_argv(command, cwd=Path(cwd), env=env)

    if isinstance(command, (list, tuple)):
        argv = [str(part) for part in command]
    else:
        text = str(command).strip()
        if not text:
            raise ValueError("Service command is empty")
        argv = _split_command_text(text)

    if not argv:
        raise ValueError("Service command is empty")

    if argv[0] in _PYTHON_LAUNCHERS:
        argv = [sys.executable, *argv[1:]]
    return argv


def _split_command_text(text: str) -> List[str]:
    """Platform-aware argv split for a command string."""

    if os.name == "nt":
        return _split_windows_command(text)
    return shlex.split(text, posix=True)


def _split_windows_command(text: str) -> List[str]:
    """
    Split a Windows command line the same way CreateProcess does.

    Uses ``CommandLineToArgvW`` so quotes are stripped correctly and paths
    with spaces survive — unlike ``shlex.split(..., posix=False)``, which
    leaves quote characters in tokens.
    """

    import ctypes
    from ctypes import wintypes

    command_line_to_argv_w = ctypes.windll.shell32.CommandLineToArgvW
    command_line_to_argv_w.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_int),
    ]
    command_line_to_argv_w.restype = ctypes.POINTER(wintypes.LPWSTR)

    argc = ctypes.c_int(0)
    argv_ptr = command_line_to_argv_w(text, ctypes.byref(argc))
    if not argv_ptr:
        raise ValueError(f"Unable to parse Windows command: {text!r}")

    try:
        return [argv_ptr[i] for i in range(argc.value)]
    finally:
        ctypes.windll.kernel32.LocalFree(argv_ptr)


def sanitize_service_name(name: str) -> str:
    """Make a service name safe for filesystem paths."""

    out_chars: list[str] = []
    for ch in name:
        if ch.isalnum() or ch in ("-", "_", "."):
            out_chars.append(ch)
        else:
            out_chars.append("_")
    sanitized = "".join(out_chars).strip("_")
    return sanitized or "service"


def safe_echo(
    message: str,
    *,
    err: bool = False,
    fg: Optional[str] = None,
    ascii_fallback: Optional[str] = None,
) -> None:
    """Print ``message``, falling back when the active console encoding cannot."""

    import typer

    encoding = (sys.stderr if err else sys.stdout).encoding or "utf-8"
    try:
        message.encode(encoding)
    except UnicodeEncodeError:
        message = ascii_fallback or message.encode(encoding, errors="replace").decode(
            encoding
        )

    try:
        if fg is None:
            typer.echo(message, err=err)
        else:
            typer.secho(message, err=err, fg=fg)
    except UnicodeEncodeError:
        fallback = ascii_fallback or message.encode(encoding, errors="replace").decode(
            encoding
        )
        if fg is None:
            typer.echo(fallback, err=err)
        else:
            typer.secho(fallback, err=err, fg=fg)


def iter_text_lines(stream: IO[str]) -> Iterator[str]:
    """Iterate over a text stream line-by-line (no buffering tricks)."""

    while True:
        line = stream.readline()
        if line == "":
            return
        yield line.rstrip("\r\n")


@contextmanager
def prepend_sys_path(path: Path) -> Iterator[None]:
    sys.path.insert(0, str(path))
    try:
        yield
    finally:
        if sys.path and sys.path[0] == str(path):
            sys.path.pop(0)
        else:
            sys.path = [p for p in sys.path if p != str(path)]


def _load_module_from_path(module_path: Path) -> ModuleType:
    # Unique module name avoids colliding with the installed ``stackpilot`` package.
    unique_name = f"_stackpilot_stackfile_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(unique_name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from {module_path!s}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[call-arg]
    return module


def load_stack_from_stackfile(config_path: Path) -> Stack:
    """
    Load developer configuration from ``Stackfile.py`` and return ``stack``.

    Expected module-level variable: ``stack = Stack()``.

    Service paths are resolved relative to the Stackfile directory so
    ``stackpilot run`` works from any project subdirectory.

    No ``-c`` / ``--config`` flag is required: discovery locates the nearest
    ``Stackfile.py`` automatically.
    """

    config_path = config_path.expanduser().resolve()
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    project_root = config_path.parent

    with prepend_sys_path(project_root):
        module = _load_module_from_path(config_path)

    stack_obj: Optional[object] = getattr(module, "stack", None)
    if stack_obj is None:
        raise AttributeError(
            f"`{config_path.name}` must define a module-level variable named `stack`."
        )
    if not isinstance(stack_obj, Stack):
        raise TypeError(
            f"`stack` in `{config_path.name}` must be an instance of `stackpilot.Stack`."
        )

    return materialize_stack_for_project(stack_obj, project_root)


def materialize_stack_for_project(stack: Stack, project_root: Path) -> Stack:
    """Return a new Stack whose service paths are absolute under ``project_root``."""

    resolved = Stack()
    for spec in stack.services:
        path = spec.path if spec.path.is_absolute() else (project_root / spec.path)
        resolved.service(
            name=spec.name,
            path=path.resolve(),
            command=spec.command,
            depends_on=spec.depends_on,
            health_check=spec.health_check,
            reload=spec.reload,
            reload_dirs=spec.reload_dirs,
            restart_dependents=spec.restart_dependents,
            port=spec.port,
        )
    for dep in stack.external_dependencies:
        resolved.external_dependency(
            name=dep.name,
            type=dep.type,
            host=dep.host,
            port=dep.port,
            health_check=dep.health_check,
        )
    if stack.run_requested:
        resolved.run()
    return resolved


def default_stackfile_path(project_root: Path) -> Path:
    return project_root.expanduser().resolve() / STACKFILE_NAME
