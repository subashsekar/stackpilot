"""Redis infrastructure adapter."""

from __future__ import annotations

from pathlib import Path

from .base import (
    AdapterServiceSpec,
    FrameworkAdapter,
    compose_mentions,
    compose_text,
)
from .detect.ports import detect_infra_port


_REDIS_DIR_NAMES = frozenset({"redis", "cache"})


class RedisAdapter(FrameworkAdapter):
    """Detect Redis via ``redis.conf`` or compose services."""

    name = "Redis"
    priority = 80
    external = True

    def detect(self, path: Path) -> bool:
        directory = path.expanduser()
        if not directory.is_dir():
            return False

        if (directory / "redis.conf").is_file():
            return True

        if compose_mentions(directory, "image: redis", "redis:"):
            text = compose_text(directory).lower()
            if "redis" in text:
                return True

        if directory.name.lower() in _REDIS_DIR_NAMES:
            if compose_text(directory) or (directory / "Dockerfile").is_file():
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
        detected = detect_infra_port(directory, kind="redis")
        # Engine default only when conf/compose omit an explicit host port.
        port = detected if detected is not None else 6379
        return AdapterServiceSpec(
            framework=self.name,
            command="",
            uses_port=True,
            health="tcp",
            fixed_port=port,
            preferred_port=detected,
            external=True,
            external_type="redis",
        )
