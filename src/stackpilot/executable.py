"""Shared executable resolution for Doctor, Runner, and Sync.

Windows ``CreateProcess`` (``subprocess`` with ``shell=False``) cannot find
bare names like ``npm`` even when ``npm.cmd`` is on ``PATH``. Resolving via
``shutil.which`` (PATHEXT-aware) to an absolute path — including ``.cmd`` /
``.bat`` — makes the same argv work for validation probes and real spawns
without ``shell=True``.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Mapping, Optional, Sequence


def resolve_executable(
    executable: str,
    *,
    cwd: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Optional[str]:
    """
    Resolve ``executable`` to an absolute filesystem path, or ``None``.

    Lookup order:

    1. Absolute path that exists as a file
    2. Relative path (contains ``/`` or ``\\``) against process CWD, then ``cwd``
    3. ``PATH`` search via ``shutil.which`` (uses ``env["PATH"]`` when given;
       on Windows this applies ``PATHEXT``, so ``npm`` → ``npm.cmd``)
    4. Bare filename sitting directly under ``cwd``
    """

    token = (executable or "").strip()
    if not token:
        return None

    candidate = Path(token)
    if candidate.is_absolute():
        return str(candidate) if candidate.is_file() else None

    root: Optional[Path] = None
    if cwd is not None:
        try:
            root = Path(cwd).expanduser().resolve()
        except OSError:
            root = Path(cwd)

    if "/" in token or "\\" in token:
        # Prefer service ``cwd`` first so relative tokens like
        # ``.venv/Scripts/python.exe`` bind to the service tree — not a
        # coincidental match under the StackPilot process working directory.
        if root is not None:
            under = root / candidate
            if under.is_file():
                try:
                    return str(under.resolve())
                except OSError:
                    return str(under)
        if candidate.is_file():
            try:
                return str(candidate.resolve())
            except OSError:
                return str(candidate)
        return None

    search_path = None
    if env is not None and env.get("PATH") is not None:
        search_path = env["PATH"]
    which = shutil.which(token, path=search_path)
    if which is not None:
        return which

    if root is not None:
        local = root / token
        if local.is_file():
            try:
                return str(local.resolve())
            except OSError:
                return str(local)
    return None


def is_launchable(
    resolved: str,
    *,
    probe_args: Sequence[str] = ("--version",),
) -> bool:
    """
    Return True when the OS allows starting ``resolved`` with ``shell=False``.

    A successful start (any exit code) or a timeout means the binary is
    launchable. ``OSError`` (including permission denied / AppLocker
    ``WinError 4551`` / missing image) means it is not.
    """

    if not resolved:
        return False
    try:
        subprocess.run(
            [resolved, *probe_args],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
        return True
    except subprocess.TimeoutExpired:
        return True
    except OSError:
        return False


def cli_is_runnable(
    name: str,
    *,
    cwd: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
) -> bool:
    """
    Return True when ``name`` resolves on ``PATH`` (or under ``cwd``) and
    the OS allows launching it with ``shell=False``.
    """

    resolved = resolve_executable(name, cwd=cwd, env=env)
    if resolved is None:
        return False
    return is_launchable(resolved)
