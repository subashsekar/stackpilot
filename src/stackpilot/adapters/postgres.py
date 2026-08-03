"""PostgreSQL infrastructure adapter."""

from __future__ import annotations

from pathlib import Path

from .base import (
    AdapterServiceSpec,
    FrameworkAdapter,
    compose_text,
)
from .detect.ports import detect_infra_port


_POSTGRES_DIR_NAMES = frozenset(
    {
        "postgres",
        "postgresql",
        "pgsql",
        "pg",
    }
)


class PostgresAdapter(FrameworkAdapter):
    """Detect PostgreSQL via compose services or common config layouts."""

    name = "PostgreSQL"
    priority = 70
    external = True

    def detect(self, path: Path) -> bool:
        directory = path.expanduser()
        if not directory.is_dir():
            return False

        if (directory / "postgresql.conf").is_file():
            return True

        text = compose_text(directory).lower()
        # Prefer service/image keys over a bare "postgres" substring so
        # unrelated mentions (env comments, app names) are less likely to match.
        if text and (
            "image: postgres" in text
            or "image:postgresql" in text
            or "postgres:" in text
            or "postgresql:" in text
        ):
            return True

        if directory.name.lower() in _POSTGRES_DIR_NAMES:
            if (directory / "Dockerfile").is_file():
                return True
            if text:
                return True
            if (directory / "init.sql").is_file() or (directory / "init").is_dir():
                return True

        return False

    def generate_service(
        self,
        path: Path,
        *,
        port: int | None = None,
    ) -> AdapterServiceSpec:
        directory = path.expanduser()
        _ = port
        detected = detect_infra_port(directory, kind="postgres")
        # Engine default only when conf/compose omit an explicit host port.
        port = detected if detected is not None else 5432
        return AdapterServiceSpec(
            framework=self.name,
            command="",
            uses_port=True,
            health="tcp",
            fixed_port=port,
            preferred_port=detected,
            external=True,
            external_type="postgresql",
        )
