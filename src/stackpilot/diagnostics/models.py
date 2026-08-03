"""Shared types for StackPilot doctor diagnostics."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import List, Optional, Sequence

from ..config import Stack
from ..discovery import ProjectContext


class CheckStatus(str, Enum):
    OK = "ok"
    FAIL = "fail"
    WARN = "warn"


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    name: str
    status: CheckStatus
    detail: str
    fix: Optional[str] = None


@dataclass(frozen=True, slots=True)
class DoctorReport:
    checks: Sequence[DoctorCheck]

    @property
    def ok(self) -> bool:
        return all(c.status != CheckStatus.FAIL for c in self.checks)

    @property
    def passed_count(self) -> int:
        return sum(1 for c in self.checks if c.status == CheckStatus.OK)

    @property
    def warning_count(self) -> int:
        return sum(1 for c in self.checks if c.status == CheckStatus.WARN)

    @property
    def error_count(self) -> int:
        return sum(1 for c in self.checks if c.status == CheckStatus.FAIL)


@dataclass
class DiagnosticContext:
    """Mutable accumulator shared across diagnostic modules."""

    origin: Path
    checks: List[DoctorCheck] = field(default_factory=list)
    project: Optional[ProjectContext] = None
    stack: Optional[Stack] = None

    def add(self, check: DoctorCheck) -> None:
        self.checks.append(check)
