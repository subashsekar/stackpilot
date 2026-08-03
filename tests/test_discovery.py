from __future__ import annotations

from pathlib import Path

import pytest

from stackpilot.discovery import (
    MISSING_STACKFILE_MESSAGE,
    STACKFILE_NAME,
    StackfileNotFoundError,
    discover_project,
    find_stackfile,
)


def _write_stackfile(directory: Path, body: str | None = None) -> Path:
    path = directory / STACKFILE_NAME
    path.write_text(
        body
        or (
            "from stackpilot import Stack\n"
            "\n"
            "stack = Stack()\n"
            "stack.run()\n"
        ),
        encoding="utf-8",
    )
    return path


class TestProjectDiscovery:
    def test_finds_stackfile_in_current_directory(self, tmp_path: Path) -> None:
        stackfile = _write_stackfile(tmp_path)
        assert find_stackfile(tmp_path) == stackfile.resolve()

    def test_finds_stackfile_in_parent_directory(self, tmp_path: Path) -> None:
        stackfile = _write_stackfile(tmp_path)
        nested = tmp_path / "apps" / "gateway"
        nested.mkdir(parents=True)

        found = find_stackfile(nested)
        assert found == stackfile.resolve()

    def test_discover_project_returns_root_and_stackfile(self, tmp_path: Path) -> None:
        stackfile = _write_stackfile(tmp_path)
        nested = tmp_path / "services" / "auth"
        nested.mkdir(parents=True)

        project = discover_project(nested)
        assert project.root == tmp_path.resolve()
        assert project.stackfile == stackfile.resolve()

    def test_missing_stackfile_returns_none(self, tmp_path: Path) -> None:
        assert find_stackfile(tmp_path) is None

    def test_missing_stackfile_raises_friendly_error(self, tmp_path: Path) -> None:
        with pytest.raises(StackfileNotFoundError) as exc_info:
            discover_project(tmp_path)

        assert str(exc_info.value) == MISSING_STACKFILE_MESSAGE
        assert "stackpilot init" in str(exc_info.value)

    def test_prefers_nearest_stackfile(self, tmp_path: Path) -> None:
        _write_stackfile(tmp_path)
        child = tmp_path / "nested"
        child.mkdir()
        nearer = _write_stackfile(child)

        assert find_stackfile(child) == nearer.resolve()
