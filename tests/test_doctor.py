"""Tests for ``stackpilot doctor`` diagnostics."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest
from typer.testing import CliRunner

from stackpilot.cli import app
from stackpilot.config import ProcessHealthCheck, ServiceSpec, TcpHealthCheck
from stackpilot.discovery import STACKFILE_NAME
from stackpilot.doctor import CheckStatus, format_doctor_report, run_doctor
from stackpilot.diagnostics.health_check import validate_health_check
from stackpilot.diagnostics.ports import is_port_in_use
from stackpilot.diagnostics.summary import ascii_fallback_report

runner = CliRunner()


def _write_stackfile(directory: Path, body: str) -> Path:
    path = directory / STACKFILE_NAME
    path.write_text(body, encoding="utf-8")
    return path


def _valid_stackfile(
    directory: Path,
    *,
    service_path: str = ".",
    extra_services: str = "",
) -> Path:
    body = f"""\
from stackpilot import Stack

stack = Stack()
stack.service(
    name="demo",
    path={service_path!r},
    command="python -c \\"print('ok')\\"",
    health_check={{"type": "process", "interval": 0.5, "timeout": 5}},
)
{extra_services}
stack.run()
"""
    return _write_stackfile(directory, body)


def _check_by_name(report, name: str):
    matches = [c for c in report.checks if c.name == name]
    assert matches, f"No check named {name!r}. Have: {[c.name for c in report.checks]}"
    return matches[-1]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class TestMissingStackfile:
    def test_missing_stackfile_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        report = run_doctor(start=tmp_path)
        check = _check_by_name(report, "Stackfile.py exists")
        assert check.status == CheckStatus.FAIL
        assert report.ok is False
        assert report.error_count >= 1

        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 1
        assert "Stackfile.py exists" in result.output


class TestInvalidStackfile:
    def test_syntax_error_fails_import(self, tmp_path: Path) -> None:
        _write_stackfile(tmp_path, "this is not valid python !!!\n")
        report = run_doctor(start=tmp_path)
        assert _check_by_name(report, "Stackfile.py exists").status == CheckStatus.OK
        assert (
            _check_by_name(report, "Stackfile imports successfully").status
            == CheckStatus.FAIL
        )
        assert _check_by_name(report, "Stack object created").status == CheckStatus.FAIL
        assert report.ok is False

    def test_missing_stack_object(self, tmp_path: Path) -> None:
        _write_stackfile(
            tmp_path,
            "from stackpilot import Stack\n\nother = Stack()\n",
        )
        report = run_doctor(start=tmp_path)
        assert (
            _check_by_name(report, "Stackfile imports successfully").status
            == CheckStatus.OK
        )
        assert _check_by_name(report, "Stack object created").status == CheckStatus.FAIL


class TestMissingServiceDirectory:
    def test_missing_service_path(self, tmp_path: Path) -> None:
        _valid_stackfile(tmp_path, service_path="does_not_exist")
        report = run_doctor(start=tmp_path)
        check = _check_by_name(report, "Service paths exist")
        assert check.status == CheckStatus.FAIL
        assert "does_not_exist" in check.detail
        assert report.ok is False


class TestDuplicatePorts:
    def test_duplicate_tcp_ports(self, tmp_path: Path) -> None:
        port = _free_port()
        body = f"""\
from stackpilot import Stack

stack = Stack()
stack.service(
    name="a",
    path=".",
    command="python -c \\"print(1)\\"",
    health_check={{"type": "tcp", "host": "127.0.0.1", "port": {port}}},
)
stack.service(
    name="b",
    path=".",
    command="python -c \\"print(2)\\"",
    health_check={{"type": "tcp", "host": "127.0.0.1", "port": {port}}},
)
stack.run()
"""
        _write_stackfile(tmp_path, body)
        report = run_doctor(start=tmp_path)
        check = _check_by_name(report, "Duplicate ports")
        assert check.status == CheckStatus.FAIL
        assert str(port) in check.detail
        assert report.ok is False


class TestPortOccupied:
    def test_port_already_in_use(self, tmp_path: Path) -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = int(server.getsockname()[1])
        try:
            assert is_port_in_use(port, host="127.0.0.1") is True
            body = f"""\
from stackpilot import Stack

stack = Stack()
stack.service(
    name="api",
    path=".",
    command="python -c \\"print(1)\\"",
    health_check={{"type": "tcp", "host": "127.0.0.1", "port": {port}}},
)
stack.run()
"""
            _write_stackfile(tmp_path, body)
            report = run_doctor(start=tmp_path)
            check = _check_by_name(report, "Ports available")
            assert check.status == CheckStatus.WARN
            assert str(port) in check.detail
            assert report.error_count == 0
            assert report.ok is True
        finally:
            server.close()


class TestMissingDependency:
    def test_missing_dependency(self, tmp_path: Path) -> None:
        body = """\
from stackpilot import Stack

stack = Stack()
stack.service(
    name="api",
    path=".",
    command="python -c \\"print(1)\\"",
    depends_on=["redis"],
)
stack.run()
"""
        _write_stackfile(tmp_path, body)
        report = run_doctor(start=tmp_path)
        assert _check_by_name(report, "Missing dependencies").status == CheckStatus.FAIL
        assert _check_by_name(report, "Dependency graph").status == CheckStatus.FAIL
        assert report.ok is False


class TestCircularDependency:
    def test_circular_dependency(self, tmp_path: Path) -> None:
        body = """\
from stackpilot import Stack

stack = Stack()
stack.service(
    name="a",
    path=".",
    command="python -c \\"print(1)\\"",
    depends_on=["b"],
)
stack.service(
    name="b",
    path=".",
    command="python -c \\"print(2)\\"",
    depends_on=["a"],
)
stack.run()
"""
        _write_stackfile(tmp_path, body)
        report = run_doctor(start=tmp_path)
        assert (
            _check_by_name(report, "Circular dependencies").status == CheckStatus.FAIL
        )
        assert _check_by_name(report, "Dependency graph").status == CheckStatus.FAIL
        assert report.ok is False


class TestInvalidHealthConfiguration:
    def test_invalid_timing_via_stackfile(self, tmp_path: Path) -> None:
        body = """\
from stackpilot import Stack

stack = Stack()
stack.service(
    name="api",
    path=".",
    command="python -c \\"print(1)\\"",
    health_check={"type": "process", "interval": -1, "timeout": 5},
)
stack.run()
"""
        _write_stackfile(tmp_path, body)
        report = run_doctor(start=tmp_path)
        check = _check_by_name(report, "Health check configuration")
        assert check.status == CheckStatus.FAIL
        assert "interval" in check.detail
        assert report.ok is False

    def test_invalid_tcp_port_on_spec(self) -> None:
        spec = ServiceSpec(
            name="bad",
            path=Path("."),
            command="python -c pass",
            health_check=TcpHealthCheck(host="127.0.0.1", port=99999),
        )
        problem = validate_health_check(spec)
        assert problem is not None
        assert "65535" in problem

    def test_invalid_http_scheme(self) -> None:
        from stackpilot.config import HttpHealthCheck

        spec = ServiceSpec(
            name="bad",
            path=Path("."),
            command="python -c pass",
            health_check=HttpHealthCheck(url="ftp://example.com/health"),
        )
        problem = validate_health_check(spec)
        assert problem is not None
        assert "http" in problem.lower()


class TestValidProject:
    def test_valid_project_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _valid_stackfile(tmp_path)
        monkeypatch.chdir(tmp_path)
        report = run_doctor(start=tmp_path)
        assert report.ok is True
        assert report.error_count == 0
        assert _check_by_name(report, "Stackfile.py exists").status == CheckStatus.OK
        assert (
            _check_by_name(report, "Stackfile imports successfully").status
            == CheckStatus.OK
        )
        assert _check_by_name(report, "Stack object created").status == CheckStatus.OK
        assert _check_by_name(report, "Service names unique").status == CheckStatus.OK
        assert _check_by_name(report, "Service paths exist").status == CheckStatus.OK
        assert _check_by_name(report, "Dependency graph").status == CheckStatus.OK
        assert (
            _check_by_name(report, "Health check configuration").status
            == CheckStatus.OK
        )

        text = format_doctor_report(report, color=False)
        assert "Environment" in text
        assert "Dependencies" in text
        assert "Ports" in text
        assert "Health Checks" in text
        assert "Configuration" in text
        assert "Checks Passed" in text
        assert "Warnings" in text
        assert "Errors" in text
        assert "Everything looks good." in text
        assert "stackpilot run" in text
        assert "✓" in text or "[OK]" in text

        # Section order matches Day 10 DX layout.
        env_i = text.index("Environment")
        dep_i = text.index("Dependencies")
        port_i = text.index("Ports")
        health_i = text.index("Health Checks")
        config_i = text.index("Configuration")
        assert env_i < dep_i < port_i < health_i < config_i

        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0
        assert "Everything looks good." in result.output


class TestExternalDependenciesDoctor:
    def test_unreachable_external_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        closed = _free_port()
        _write_stackfile(
            tmp_path,
            f"""\
from stackpilot import Stack

stack = Stack()
stack.external_dependency(
    name="postgres",
    type="postgresql",
    host="127.0.0.1",
    port={closed},
)
stack.service(
    name="auth",
    path=".",
    command="python -c \\"print('ok')\\"",
    depends_on=["postgres"],
)
stack.run()
""",
        )
        monkeypatch.chdir(tmp_path)
        report = run_doctor(start=tmp_path)
        check = _check_by_name(report, "External dependencies reachable")
        assert check.status == CheckStatus.FAIL
        assert report.ok is False


class TestSummaryFormatting:
    def test_errors_omit_everything_looks_good(self, tmp_path: Path) -> None:
        report = run_doctor(start=tmp_path)
        text = format_doctor_report(report, color=False)
        assert "Errors:" in text or "Errors" in text
        assert "Everything looks good." not in text

    def test_process_health_ok_helper(self) -> None:
        spec = ServiceSpec(
            name="ok",
            path=Path("."),
            command="python -c pass",
            health_check=ProcessHealthCheck(interval=0.5, timeout=5.0),
        )
        assert validate_health_check(spec) is None


def test_ascii_fallback_report_replaces_marks_and_arrows() -> None:
    raw = "✓ path ok\n✗ broken\nauth → ./missing"
    assert ascii_fallback_report(raw) == "[OK] path ok\n[FAIL] broken\nauth -> ./missing"
