"""Regression tests for shared executable resolution (Doctor / Runner / Sync)."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Dict

import pytest

from stackpilot.config import ServiceSpec
from stackpilot.diagnostics.service_check import _command_problem
from stackpilot.executable import cli_is_runnable, is_launchable, resolve_executable
from stackpilot.launch_env import build_child_env, resolve_service_argv


def _windows() -> bool:
    return os.name == "nt" or sys.platform.startswith("win")


def _make_batch_or_script(directory: Path, name: str, body: str) -> Path:
    """
    Create a PATH-visible shim.

    On Windows write ``name.cmd`` (CreateProcess needs the extension via
    PATHEXT / which). On POSIX write an executable ``name`` script.
    """

    directory.mkdir(parents=True, exist_ok=True)
    if _windows():
        path = directory / f"{name}.cmd"
        path.write_text(f"@echo off\r\n{body}\r\n", encoding="utf-8")
    else:
        path = directory / name
        path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _path_env(bin_dir: Path, *, base: Dict[str, str] | None = None) -> Dict[str, str]:
    env = dict(base if base is not None else os.environ)
    prefix = str(bin_dir)
    current = env.get("PATH", "")
    env["PATH"] = prefix + (os.pathsep + current if current else "")
    return env


class TestResolveExecutable:
    def test_python_executable_absolute(self) -> None:
        found = resolve_executable(sys.executable)
        assert found is not None
        assert Path(found).resolve() == Path(sys.executable).resolve()
        assert Path(found).is_file()

    def test_python_via_resolve_service_argv(self, tmp_path: Path) -> None:
        env = build_child_env(tmp_path)
        argv = resolve_service_argv("python -c pass", cwd=tmp_path, env=env)
        assert Path(argv[0]).is_file()
        assert Path(argv[0]).resolve() == Path(sys.executable).resolve() or (
            Path(argv[0]).name.lower().startswith("python")
        )

    def test_npm_cmd_on_path(self, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bin"
        _make_batch_or_script(bin_dir, "npm", "echo 1.0.0")
        env = _path_env(bin_dir)
        found = resolve_executable("npm", cwd=tmp_path, env=env)
        assert found is not None
        assert Path(found).is_file()
        if _windows():
            assert found.lower().endswith(".cmd")
        argv = resolve_service_argv("npm run dev", cwd=tmp_path, env=env)
        assert Path(argv[0]).resolve() == Path(found).resolve()

    def test_pnpm_cmd_on_path(self, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bin"
        _make_batch_or_script(bin_dir, "pnpm", "echo 9.0.0")
        env = _path_env(bin_dir)
        found = resolve_executable("pnpm", cwd=tmp_path, env=env)
        assert found is not None
        if _windows():
            assert Path(found).name.lower() in {"pnpm.cmd", "pnpm.bat"}
        argv = resolve_service_argv("pnpm run start", cwd=tmp_path, env=env)
        assert argv[0] == found

    @pytest.mark.parametrize("name", ["yarn", "bun", "npx"])
    def test_node_shims_on_path(self, tmp_path: Path, name: str) -> None:
        bin_dir = tmp_path / "bin"
        _make_batch_or_script(bin_dir, name, "echo ok")
        env = _path_env(bin_dir)
        found = resolve_executable(name, cwd=tmp_path, env=env)
        assert found is not None
        assert Path(found).is_file()

    def test_missing_executable(self, tmp_path: Path) -> None:
        env = _path_env(tmp_path / "empty")
        (tmp_path / "empty").mkdir()
        assert resolve_executable("definitely-missing-stackpilot-bin", env=env) is None

    def test_windows_path_lookup_prefers_env_path(self, tmp_path: Path) -> None:
        first = tmp_path / "first"
        second = tmp_path / "second"
        _make_batch_or_script(first, "tool", "echo first")
        _make_batch_or_script(second, "tool", "echo second")
        env = dict(os.environ)
        env["PATH"] = str(first) + os.pathsep + str(second)
        found = resolve_executable("tool", env=env)
        assert found is not None
        resolved = str(Path(found).resolve()).lower()
        assert str(first.resolve()).lower() in resolved


class TestLaunchableAndCli:
    def test_permission_denied_not_launchable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / ("blocked.cmd" if _windows() else "blocked")
        target.write_text("echo hi\n", encoding="utf-8")
        if not _windows():
            target.chmod(0o755)

        def _raise(*_a, **_k):
            raise PermissionError("denied")

        monkeypatch.setattr(
            "stackpilot.executable.subprocess.run",
            _raise,
        )
        assert is_launchable(str(target)) is False
        assert cli_is_runnable("blocked", cwd=tmp_path, env=_path_env(tmp_path)) is False

    def test_missing_cli_not_runnable(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        assert cli_is_runnable("no-such-cli-xyz", env=_path_env(empty)) is False

    def test_runnable_shim(self, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bin"
        _make_batch_or_script(bin_dir, "uv", "echo 0.0.0")
        assert cli_is_runnable("uv", env=_path_env(bin_dir)) is True


class TestDoctorRunnerParity:
    def test_doctor_and_runner_resolve_npm_identically(self, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bin"
        _make_batch_or_script(bin_dir, "npm", "echo 1.2.3")
        service = tmp_path / "app"
        service.mkdir()
        env = _path_env(bin_dir)

        argv = resolve_service_argv("npm run dev", cwd=service, env=env)
        assert Path(argv[0]).is_file()

        # Doctor uses the same resolver stack with a patched child env.
        original_environ = os.environ.copy()
        try:
            os.environ.clear()
            os.environ.update(env)
            spec = ServiceSpec(name="web", path=service, command="npm run dev")
            assert _command_problem(spec) is None
        finally:
            os.environ.clear()
            os.environ.update(original_environ)

    def test_doctor_fails_missing_executable(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        service = tmp_path / "app"
        service.mkdir()
        original = os.environ.copy()
        try:
            os.environ.clear()
            os.environ.update(_path_env(empty, base={}))
            # Keep SystemRoot on Windows so Path / Python internals work.
            if _windows():
                for key in ("SystemRoot", "SYSTEMROOT", "WINDIR", "COMSPEC"):
                    if key in original:
                        os.environ[key] = original[key]
            spec = ServiceSpec(
                name="web",
                path=service,
                command="missing-bin-xyz run",
            )
            problem = _command_problem(spec)
            assert problem is not None
            assert "executable not found" in problem
        finally:
            os.environ.clear()
            os.environ.update(original)

    def test_doctor_fails_permission_denied(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bin_dir = tmp_path / "bin"
        _make_batch_or_script(bin_dir, "npm", "echo 1")
        service = tmp_path / "app"
        service.mkdir()

        monkeypatch.setattr(
            "stackpilot.diagnostics.service_check.is_launchable",
            lambda *_a, **_k: False,
        )
        original = os.environ.copy()
        try:
            os.environ.clear()
            os.environ.update(_path_env(bin_dir))
            spec = ServiceSpec(name="web", path=service, command="npm run dev")
            problem = _command_problem(spec)
            assert problem is not None
            assert "blocked by OS policy" in problem
        finally:
            os.environ.clear()
            os.environ.update(original)

    def test_spawn_argv_uses_resolved_cmd_not_bare_name(
        self, tmp_path: Path
    ) -> None:
        """Bare npm fails CreateProcess on Windows; resolved .cmd must be used."""

        bin_dir = tmp_path / "bin"
        _make_batch_or_script(bin_dir, "npm", "echo SPAWN_OK")
        env = _path_env(bin_dir)
        argv = resolve_service_argv("npm -v", cwd=tmp_path, env=env)
        assert argv[0] != "npm"
        assert Path(argv[0]).is_file()
        proc = subprocess.run(
            argv,
            cwd=str(tmp_path),
            env=env,
            capture_output=True,
            text=True,
            shell=False,
            check=False,
        )
        assert proc.returncode == 0
        assert "SPAWN_OK" in (proc.stdout or "") or "SPAWN_OK" in (proc.stderr or "")


class TestInstallResolution:
    def test_editable_python_resolves(self) -> None:
        """Current interpreter (editable install host) must resolve and launch."""

        found = resolve_executable(sys.executable)
        assert found is not None
        assert is_launchable(found, probe_args=["-c", "pass"])
        # Shared Sync helper must agree for a PATH python name when present.
        which_py = resolve_executable("python")
        if which_py is not None:
            assert Path(which_py).is_file()

    def test_wheel_metadata_console_script_name(self, tmp_path: Path) -> None:
        """
        Wheel install regression: built wheel still exposes stackpilot entry.

        Full clean-venv install is covered in test_packaging_regression; here we
        only assert the resolver can target the built console-script payload name
        once the wheel exists on disk.
        """

        repo = Path(__file__).resolve().parents[1]
        dist = tmp_path / "dist"
        dist.mkdir()
        proc = subprocess.run(
            [sys.executable, "-m", "build", "--wheel", "--outdir", str(dist)],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            pytest.skip("build failed: " + (proc.stderr or proc.stdout or "")[:300])
        wheels = list(dist.glob("*.whl"))
        assert wheels
        with zipfile.ZipFile(wheels[0]) as zf:
            ep = next(n for n in zf.namelist() if n.endswith(".dist-info/entry_points.txt"))
            text = zf.read(ep).decode("utf-8")
            assert "stackpilot=stackpilot.cli:main" in text.replace(" ", "")

        # After a normal install the console script is found via the shared resolver.
        installed = resolve_executable("stackpilot")
        if installed is None:
            pytest.skip("stackpilot console script not on PATH in this environment")
        assert Path(installed).is_file()
