"""Ignore rules for the Hot Reload Engine (defaults + ``.stackpilotignore``)."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import List, Optional, Sequence

STACKPILOTIGNORE_NAME = ".stackpilotignore"

# Directory / path segment names always skipped.
DEFAULT_IGNORE_NAMES = frozenset(
    {
        ".git",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        "node_modules",
        ".venv",
        ".logs",
        ".stackpilot",
        "dist",
        "build",
    }
)

# Filename globs always skipped.
DEFAULT_IGNORE_GLOBS = ("*.pyc", "*.pyo", "*.log")


def default_ignore_patterns() -> List[str]:
    """Return built-in ignore patterns in gitignore style."""

    patterns: List[str] = []
    for name in sorted(DEFAULT_IGNORE_NAMES):
        patterns.append(f"{name}/")
        patterns.append(name)
    patterns.extend(DEFAULT_IGNORE_GLOBS)
    return patterns


def load_stackpilotignore(root: Path) -> List[str]:
    """Load gitignore-style patterns from ``root/.stackpilotignore`` if present."""

    path = root.expanduser().resolve() / STACKPILOTIGNORE_NAME
    if not path.is_file():
        return []

    patterns: List[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        patterns.append(line)
    return patterns


class IgnoreMatcher:
    """
    Match filesystem paths against default and ``.stackpilotignore`` patterns.

    Pattern semantics follow a practical gitignore subset:
    - ``#`` comments and blank lines are ignored when loading from file
    - trailing ``/`` matches directories only
    - ``*`` matches within a single path segment
    - ``**`` matches across directories
    - a pattern with no ``/`` matches in any directory
    - a leading ``/`` anchors to the watch root
    - ``!`` negates a previous match
    """

    def __init__(
        self,
        root: Path,
        *,
        extra_patterns: Sequence[str] | None = None,
        load_ignore_file: bool = True,
    ) -> None:
        self._root = root.expanduser().resolve()
        patterns: List[str] = default_ignore_patterns()
        if load_ignore_file:
            patterns.extend(load_stackpilotignore(self._root))
        if extra_patterns:
            patterns.extend(extra_patterns)
        self._rules = [_IgnoreRule.parse(p) for p in patterns if p.strip()]

    @property
    def root(self) -> Path:
        return self._root

    def ignored(self, path: Path | str) -> bool:
        """Return True when ``path`` (file or directory) should be ignored."""

        target = Path(path)
        try:
            if not target.is_absolute():
                target = (self._root / target).resolve()
            else:
                target = target.resolve()
        except OSError:
            target = Path(os.path.normpath(str(path)))

        try:
            relative = target.relative_to(self._root)
        except ValueError:
            # Outside the watch root — treat as not ignored by these rules.
            return False

        rel = relative.as_posix()
        if rel in ("", "."):
            return False

        # Also check each path prefix (parent directories).
        parts = rel.split("/")
        candidates = ["/".join(parts[: i + 1]) for i in range(len(parts))]

        ignored = False
        for candidate in candidates:
            is_dir = candidate != rel or _looks_like_dir(target, rel, candidate)
            for rule in self._rules:
                matched = rule.matches(candidate, is_dir=is_dir)
                if matched is None:
                    continue
                ignored = matched
        return ignored


def _looks_like_dir(target: Path, full_rel: str, candidate: str) -> bool:
    if candidate != full_rel:
        return True
    try:
        return target.is_dir()
    except OSError:
        return False


class _IgnoreRule:
    __slots__ = ("negated", "dir_only", "anchored", "regex")

    def __init__(
        self,
        *,
        negated: bool,
        dir_only: bool,
        anchored: bool,
        regex: re.Pattern[str],
    ) -> None:
        self.negated = negated
        self.dir_only = dir_only
        self.anchored = anchored
        self.regex = regex

    @classmethod
    def parse(cls, pattern: str) -> "_IgnoreRule":
        text = pattern.strip()
        negated = text.startswith("!")
        if negated:
            text = text[1:]

        dir_only = text.endswith("/")
        if dir_only:
            text = text[:-1]

        anchored = text.startswith("/")
        if anchored:
            text = text[1:]

        # Unanchored patterns with a slash still match from root (gitignore).
        if "/" in text.rstrip("/"):
            anchored = True

        regex = re.compile(_glob_to_regex(text, anchored=anchored))
        return cls(
            negated=negated,
            dir_only=dir_only,
            anchored=anchored,
            regex=regex,
        )

    def matches(self, relative_posix: str, *, is_dir: bool) -> Optional[bool]:
        """
        Return True/False when this rule applies, or None when it does not match.

        True means ignore; False means un-ignore (negation).
        """

        if self.dir_only and not is_dir:
            return None

        path = relative_posix.strip("/")
        if not path:
            return None

        if self.regex.search(path) is None:
            return None
        return not self.negated


def _glob_to_regex(pattern: str, *, anchored: bool) -> str:
    """Convert a gitignore-style glob to a regex matching a relative posix path."""

    i = 0
    out: List[str] = []
    n = len(pattern)

    while i < n:
        ch = pattern[i]
        if ch == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                # ** or **/
                if i + 2 < n and pattern[i + 2] == "/":
                    out.append("(?:.*/)?")
                    i += 3
                else:
                    out.append(".*")
                    i += 2
            else:
                out.append("[^/]*")
                i += 1
        elif ch == "?":
            out.append("[^/]")
            i += 1
        elif ch == "/":
            out.append("/")
            i += 1
        elif ch == "[":
            j = i + 1
            if j < n and pattern[j] in ("!", "^"):
                j += 1
            while j < n and pattern[j] != "]":
                j += 1
            if j >= n:
                out.append(re.escape(ch))
                i += 1
            else:
                out.append(pattern[i : j + 1])
                i = j + 1
        else:
            out.append(re.escape(ch))
            i += 1

    body = "".join(out)
    if anchored:
        return rf"^{body}(?:/.*)?$"
    # Match the pattern as a full segment anywhere in the path.
    return rf"(?:^|/)" + body + rf"(?:/.*)?$"
