"""Launch-environment fidelity and startup failure diagnostics.

StackPilot must spawn each service the way a developer would::

    cd <service.path> && <service.command>

This module builds the child environment (cwd-local venv, inherited vars,
optional ``.env``), resolves bare ``python`` launchers against the service
directory, and compares the actual spawn plan to the expected developer
plan for diagnostics. It never invents project-specific paths.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple, Union

from .adapters.detect.venv import (
    _venv_python_path,
    detect_venv_dir,
    resolve_python_executable,
)
from .config import ExternalDependency, ServiceSpec
from .executable import resolve_executable
from .utils import _PYTHON_LAUNCHERS, _split_command_text

# Env keys compared when reporting launch differences (nothing else).
_COMPARE_KEYS = ("PYTHONPATH", "VIRTUAL_ENV", "PATH")

_DOTENV_LINE = re.compile(
    r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$"
)


@dataclass(frozen=True, slots=True)
class LaunchPlan:
    """Resolved spawn plan for one service (actual or expected)."""

    service: str
    cwd: Path
    argv: Tuple[str, ...]
    env: Mapping[str, str]
    command: str

    @property
    def executable(self) -> str:
        return self.argv[0] if self.argv else ""

    def env_snapshot(self) -> Dict[str, str]:
        """Return only the diagnostic env keys (plus missing as empty)."""

        out: Dict[str, str] = {}
        for key in _COMPARE_KEYS:
            out[key] = self.env.get(key, "")
        return out


@dataclass(frozen=True, slots=True)
class EnvDifference:
    """One env key that differs between StackPilot and the expected plan."""

    key: str
    actual: str
    expected: str


@dataclass(frozen=True, slots=True)
class LaunchComparison:
    """Side-by-side launch comparison; ``differences`` is empty when aligned."""

    actual: LaunchPlan
    expected: LaunchPlan
    differences: Tuple[EnvDifference, ...] = field(default_factory=tuple)
    argv_differs: bool = False
    cwd_differs: bool = False

    @property
    def has_differences(self) -> bool:
        return bool(self.differences) or self.argv_differs or self.cwd_differs


@dataclass(frozen=True, slots=True)
class TracebackSummary:
    """Parsed exception details from a Python traceback."""

    exception_type: Optional[str] = None
    exception_message: Optional[str] = None
    file_line: Optional[str] = None


def resolve_service_argv(
    command: Union[str, Sequence[str]],
    *,
    cwd: Path,
    env: Optional[Mapping[str, str]] = None,
) -> List[str]:
    """
    Build argv for ``Popen`` with service-local Python resolution.

    Bare ``python`` / ``python3`` / ``py`` resolve to the service (or
    monorepo-root) venv interpreter when one exists under ``cwd``, otherwise
    to ``PATH`` lookup using ``env``, and finally to ``sys.executable``.
    Other bare names (``npm``, ``pnpm``, …) resolve via the shared
    ``resolve_executable`` helper (PATHEXT / ``.cmd`` on Windows) so
    ``CreateProcess`` receives an absolute path — the same logic Doctor uses.
    Relative path executables are absolutized against ``cwd`` when they exist.
    """

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
        argv = [_resolve_python_launcher(cwd, env=env), *argv[1:]]
    else:
        argv = [_resolve_path_executable(argv[0], cwd, env=env), *argv[1:]]

    # Windows: uvicorn --reload / Django runserver autoreload are taken over
    # by WatchManager so StackPilot can restart one service without killing
    # the whole console stack.
    from .watcher import should_takeover_native_reload, strip_native_reload_argv

    if should_takeover_native_reload(argv):
        argv = strip_native_reload_argv(argv)
    return argv


def build_child_env(
    service_path: Path,
    *,
    base: Optional[Mapping[str, str]] = None,
    services: Sequence[ServiceSpec] = (),
    external_dependencies: Sequence[ExternalDependency] = (),
    env: Optional[Mapping[str, str]] = None,
    env_file: Optional[Union[str, Path]] = None,
) -> Dict[str, str]:
    """
    Build the environment for a service subprocess.

    Starts from ``base`` (default: ``os.environ``), layers service ``.env``
    files without overriding existing keys, applies an explicit ``env_file``
    then ``env={}`` from the Stackfile (those override), and when a local
    venv is present sets ``VIRTUAL_ENV`` and prepends the venv scripts/bin
    directory to ``PATH``. When stack topology is provided, injects service
    discovery env vars so frontends and sibling services can connect without
    hardcoded URLs.

    When the service declares ``port=`` in the Stackfile, also sets ``PORT`` /
    framework listen env keys (without overriding values already present from
    the parent environment or ``.env``) so Node / Flask apps bind the same
    port StackPilot health-checks.

    Never mutates the caller's ``base`` mapping or ``os.environ``.
    """

    root = Path(service_path).expanduser().resolve()
    child: Dict[str, str] = dict(base if base is not None else os.environ)

    # Force line-oriented logs when stdout/stderr are pipes (not a TTY).
    child["PYTHONUNBUFFERED"] = "1"
    child.setdefault("PYTHONIOENCODING", "utf-8")

    for path in _dotenv_files(root):
        for key, value in _parse_dotenv(path).items():
            child.setdefault(key, value)

    # Explicit Stackfile env_file / env win over auto-discovered dotenv and
    # the inherited parent environment for those keys only.
    file_path = env_file
    mapping_env: Mapping[str, str] = env or {}
    if file_path is None and not mapping_env:
        matched = _matching_service_spec(root, services=services)
        if matched is not None:
            file_path = matched.env_file
            mapping_env = matched.env

    if file_path is not None:
        resolved_file = _resolve_env_file(root, file_path)
        if resolved_file is not None:
            child.update(_parse_dotenv(resolved_file))

    if mapping_env:
        child.update({str(k): str(v) for k, v in mapping_env.items()})

    venv = detect_venv_dir(root)
    if venv is not None:
        venv_resolved = venv.resolve()
        child["VIRTUAL_ENV"] = str(venv_resolved)
        bin_dir = _venv_bin_dir(venv_resolved)
        if bin_dir is not None:
            current = child.get("PATH", "")
            prefix = str(bin_dir)
            parts = current.split(os.pathsep) if current else []
            if not parts or Path(parts[0]).resolve() != bin_dir.resolve():
                child["PATH"] = prefix + (os.pathsep + current if current else "")

    _inject_listen_port_env(child, root, services=services)
    _inject_service_connection_env(
        child,
        services=services,
        external_dependencies=external_dependencies,
    )
    return child


def _matching_service_spec(
    service_path: Path,
    *,
    services: Sequence[ServiceSpec],
) -> Optional[ServiceSpec]:
    try:
        resolved = service_path.expanduser().resolve()
    except OSError:
        return None
    for spec in services:
        try:
            if Path(spec.path).expanduser().resolve() == resolved:
                return spec
        except OSError:
            continue
    return None


def _resolve_env_file(service_root: Path, env_file: Union[str, Path]) -> Optional[Path]:
    """Resolve ``env_file`` against the service directory; ``None`` if missing."""

    raw = Path(env_file).expanduser()
    path = raw if raw.is_absolute() else (service_root / raw)
    try:
        resolved = path.resolve()
    except OSError:
        return None
    if resolved.is_file():
        return resolved
    return None


def _inject_listen_port_env(
    env: Dict[str, str],
    service_path: Path,
    *,
    services: Sequence[ServiceSpec],
) -> None:
    """Align framework listen env with the Stackfile ``port=`` for this cwd."""

    try:
        resolved = service_path.expanduser().resolve()
    except OSError:
        return
    for spec in services:
        try:
            if Path(spec.path).expanduser().resolve() != resolved:
                continue
        except OSError:
            continue
        if spec.port is None:
            return
        port = str(int(spec.port))
        for key in (
            "PORT",
            "APP_PORT",
            "HTTP_PORT",
            "SERVER_PORT",
            "UVICORN_PORT",
            "FLASK_RUN_PORT",
        ):
            env.setdefault(key, port)
        return

def expected_launch_plan(
    spec: ServiceSpec,
    *,
    base_env: Optional[Mapping[str, str]] = None,
    services: Sequence[ServiceSpec] = (),
    external_dependencies: Sequence[ExternalDependency] = (),
) -> LaunchPlan:
    """
    Launch plan a developer would get from ``cd <path> && <command>`` with
    a local venv activated (when present) and service ``.env`` loaded.
    """

    cwd = Path(spec.path).expanduser().resolve()
    env = build_child_env(
        cwd,
        base=base_env,
        services=services,
        external_dependencies=external_dependencies,
        env=spec.env,
        env_file=spec.env_file,
    )
    argv = tuple(resolve_service_argv(spec.command, cwd=cwd, env=env))
    return LaunchPlan(
        service=spec.name,
        cwd=cwd,
        argv=argv,
        env=env,
        command=str(spec.command),
    )


def actual_launch_plan(
    spec: ServiceSpec,
    *,
    argv: Sequence[str],
    cwd: Path,
    env: Mapping[str, str],
) -> LaunchPlan:
    """Wrap the argv/cwd/env StackPilot actually used for spawn."""

    return LaunchPlan(
        service=spec.name,
        cwd=Path(cwd).expanduser().resolve(),
        argv=tuple(str(a) for a in argv),
        env=dict(env),
        command=str(spec.command),
    )


def compare_launch_plans(actual: LaunchPlan, expected: LaunchPlan) -> LaunchComparison:
    """
    Compare StackPilot launch vs expected developer launch.

    Reports differences only (cwd, argv/executable, and selected env keys).
    Does not modify anything.
    """

    diffs: List[EnvDifference] = []
    actual_snap = actual.env_snapshot()
    expected_snap = expected.env_snapshot()
    for key in _COMPARE_KEYS:
        a_val = actual_snap.get(key, "")
        e_val = expected_snap.get(key, "")
        if key == "PATH":
            a_val = _path_prefix(a_val)
            e_val = _path_prefix(e_val)
        if a_val != e_val:
            diffs.append(EnvDifference(key=key, actual=a_val, expected=e_val))

    return LaunchComparison(
        actual=actual,
        expected=expected,
        differences=tuple(diffs),
        argv_differs=list(actual.argv) != list(expected.argv),
        cwd_differs=actual.cwd != expected.cwd,
    )


def format_env_differences(comparison: LaunchComparison) -> str:
    """Human-readable difference block; empty string when aligned."""

    if not comparison.has_differences:
        return "(none)"

    lines: List[str] = []
    if comparison.cwd_differs:
        lines.append(
            f"cwd: actual={comparison.actual.cwd} expected={comparison.expected.cwd}"
        )
    if comparison.argv_differs:
        lines.append(
            "executable/argv: "
            f"actual={_format_argv(comparison.actual.argv)} "
            f"expected={_format_argv(comparison.expected.argv)}"
        )
    for diff in comparison.differences:
        lines.append(
            f"{diff.key}: actual={_display_env(diff.actual)!r} "
            f"expected={_display_env(diff.expected)!r}"
        )
    return "\n".join(lines)


def infer_likely_cause(
    summary: Optional[TracebackSummary],
    comparison: Optional[LaunchComparison],
) -> str:
    """Heuristic likely-cause line for startup failure diagnostics."""

    hints: List[str] = []
    exc = (summary.exception_type or "") if summary else ""
    msg = (summary.exception_message or "") if summary else ""

    if comparison is not None and comparison.cwd_differs:
        hints.append("Working directory mismatch")
    if comparison is not None:
        for diff in comparison.differences:
            if diff.key == "PYTHONPATH":
                hints.append("Missing or mismatched PYTHONPATH")
            elif diff.key == "VIRTUAL_ENV":
                hints.append("Virtual environment not applied")
            elif diff.key == "PATH":
                hints.append("PATH mismatch (interpreter / scripts)")
        if comparison.argv_differs:
            hints.append("Python executable mismatch")

    if exc == "ModuleNotFoundError" or "No module named" in msg:
        if "Missing or mismatched PYTHONPATH" not in hints:
            hints.append("Missing PYTHONPATH")
        if "Python executable mismatch" not in hints and "Virtual environment not applied" not in hints:
            hints.append("Wrong Python interpreter / environment")
    elif exc in {"SyntaxError", "IndentationError"} or "SyntaxError" in msg:
        hints.append("Invalid Python syntax in application code")
    elif "Cannot find module" in msg or "MODULE_NOT_FOUND" in msg:
        hints.append("Missing Node.js dependency or wrong working directory")
    elif exc.endswith("Error") and exc not in {"", "Error"}:
        hints.append(f"{exc} raised during application startup")

    if not hints:
        hints.append("Application process exited during startup")

    # Deduplicate while preserving order.
    seen: set[str] = set()
    ordered: List[str] = []
    for hint in hints:
        if hint in seen:
            continue
        seen.add(hint)
        ordered.append(hint)
    return ", or ".join(ordered)


def suggest_startup_fix(
    summary: Optional[TracebackSummary],
    comparison: Optional[LaunchComparison],
) -> str:
    """Actionable Suggested fix line for application startup failures."""

    cause = infer_likely_cause(summary, comparison).lower()
    if "pythonpath" in cause or "module named" in (
        (summary.exception_message or "") if summary else ""
    ).lower():
        return (
            "Install the missing package in the service venv, or fix PYTHONPATH / "
            "imports, then re-run."
        )
    if "syntax" in cause:
        return "Fix the syntax error in the reported file, then re-run."
    if "node" in cause or "module_not_found" in cause:
        return "Run npm/pnpm/yarn install in the service directory, then re-run."
    if "working directory" in cause:
        return "Fix path= in Stackfile.py so it points at the service directory."
    if "virtual environment" in cause or "interpreter" in cause:
        return "Activate or recreate the service venv, install dependencies, then re-run."
    if summary and summary.file_line:
        return f"Inspect {summary.file_line}, fix the application error, then re-run."
    return (
        "Inspect Application Output above (and stackpilot issues <service>), "
        "fix the application error, then re-run."
    )


def format_startup_failure_report(
    *,
    service: str,
    cwd: Path,
    command: str,
    python_executable: str,
    comparison: Optional[LaunchComparison] = None,
    summary: Optional[TracebackSummary] = None,
    application_output: Optional[str] = None,
) -> str:
    """
    Build the developer-facing application failure block.

    Format::

        Application Output

        <real traceback / stderr>

        Problem: ...
        Reason: ...
        Suggested fix: ...

    Never includes a StackPilot traceback. Application output is preserved.
    """

    del python_executable  # retained for call-site compatibility

    output = (application_output or "").strip()
    if not output and summary is not None:
        bits: List[str] = []
        if summary.exception_type:
            line = summary.exception_type
            if summary.exception_message:
                line = f"{summary.exception_type}: {summary.exception_message}"
            bits.append(line)
        elif summary.exception_message:
            bits.append(summary.exception_message)
        if summary.file_line:
            bits.append(f"  at {summary.file_line}")
        output = "\n".join(bits)
    if not output:
        output = "(no application output captured)"

    reason = infer_likely_cause(summary, comparison)
    if summary and summary.exception_type:
        detail = summary.exception_type
        if summary.exception_message:
            detail = f"{summary.exception_type}: {summary.exception_message}"
        if summary.file_line:
            detail = f"{detail} ({summary.file_line})"
        if reason and detail.lower() not in reason.lower():
            reason = f"{detail}. {reason}"
        else:
            reason = detail

    suggested = suggest_startup_fix(summary, comparison)
    extra = [
        f"Command: {command}",
        f"Working directory: {cwd}",
    ]
    if comparison is not None and comparison.has_differences:
        env_block = format_env_differences(comparison)
        if env_block and env_block != "(none)":
            extra.append(f"Environment differences: {env_block.splitlines()[0]}")

    lines = [
        "",
        "Application Output",
        "",
        output,
        "",
        "Problem: Application startup failed",
        f"Affected service: {service}",
        f"Reason: {reason}",
        f"Suggested fix: {suggested}",
        "",
        *extra,
    ]
    return "\n".join(lines)


def _resolve_python_launcher(
    cwd: Path,
    *,
    env: Optional[Mapping[str, str]] = None,
) -> str:
    root = Path(cwd).expanduser().resolve()
    local = resolve_python_executable(root)
    if local not in _PYTHON_LAUNCHERS:
        return _resolve_path_executable(local, root, env=env)

    # Parent/monorepo venv: resolve_python_executable returns bare "python"
    # for Stackfile safety; still bind the absolute interpreter at spawn.
    venv = detect_venv_dir(root)
    if venv is not None:
        exe = _venv_python_path(venv)
        if exe is not None and exe.is_file():
            return str(exe.resolve())

    found = resolve_executable("python", cwd=root, env=env)
    if found:
        return found
    return sys.executable


def _resolve_path_executable(
    token: str,
    cwd: Path,
    *,
    env: Optional[Mapping[str, str]] = None,
) -> str:
    """
    Resolve ``token`` to an absolute executable path when possible.

    Uses the shared ``resolve_executable`` so Doctor validation and Runner
    spawn agree (including Windows ``npm.cmd`` / ``pnpm.cmd`` via PATHEXT).
    Unresolved bare names are returned unchanged so ``CreateProcess`` still
    raises ``FileNotFoundError`` with a useful filename.
    """

    found = resolve_executable(token, cwd=cwd, env=env)
    return found if found is not None else token


def _venv_bin_dir(venv: Path) -> Optional[Path]:
    if os.name == "nt" or sys.platform.startswith("win"):
        scripts = venv / "Scripts"
        if scripts.is_dir():
            return scripts
    unix = venv / "bin"
    if unix.is_dir():
        return unix
    scripts = venv / "Scripts"
    if scripts.is_dir():
        return scripts
    return None


def _dotenv_files(root: Path) -> List[Path]:
    files: List[Path] = []
    for name in (".env", ".env.local", ".env.development", ".env.dev"):
        path = root / name
        if path.is_file():
            files.append(path)
    try:
        for child in sorted(root.iterdir()):
            if child.is_file() and child.name.startswith(".env.") and child not in files:
                files.append(child)
    except OSError:
        pass
    return files


def _parse_dotenv(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _DOTENV_LINE.match(line)
        if not match:
            continue
        key = match.group(1)
        value = match.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values


def _inject_service_connection_env(
    env: Dict[str, str],
    *,
    services: Sequence[ServiceSpec],
    external_dependencies: Sequence[ExternalDependency],
) -> None:
    """Expose discovered stack endpoints via stable env vars."""

    for spec in services:
        if spec.port is None:
            continue
        token = _service_env_token(spec.name)
        host = "127.0.0.1"
        port = str(int(spec.port))
        url = f"http://{host}:{port}"
        env[f"STACKPILOT_{token}_HOST"] = host
        env[f"STACKPILOT_{token}_PORT"] = port
        env[f"STACKPILOT_{token}_URL"] = url
        for prefix in (
            "VITE",
            "NEXT_PUBLIC",
            "REACT_APP",
            "NUXT_PUBLIC",
            "PUBLIC",
            "EXPO_PUBLIC",
        ):
            env[f"{prefix}_{token}_URL"] = url

    for dep in external_dependencies:
        token = _service_env_token(dep.name)
        env[f"STACKPILOT_{token}_HOST"] = str(dep.host).strip() or "127.0.0.1"
        env[f"STACKPILOT_{token}_PORT"] = str(int(dep.port))


def _service_env_token(name: str) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "_", str(name).strip().upper())
    text = re.sub(r"_+", "_", text).strip("_")
    return text or "SERVICE"


def _path_prefix(path_value: str, *, parts: int = 2) -> str:
    if not path_value:
        return ""
    chunks = path_value.split(os.pathsep)
    return os.pathsep.join(chunks[:parts])


def _format_argv(argv: Sequence[str]) -> str:
    return " ".join(argv)


def _display_env(value: str) -> str:
    if not value:
        return ""
    if len(value) > 120:
        return value[:117] + "..."
    return value
