"""MongoDB infrastructure adapter."""

from __future__ import annotations

from pathlib import Path

from .base import (
    AdapterServiceSpec,
    FrameworkAdapter,
    compose_text,
    read_text,
)
from .detect.ports import detect_infra_port


_MONGO_DIR_NAMES = frozenset(
    {
        "mongo",
        "mongodb",
        "mongo-db",
    }
)

_MONGO_URI_MARKERS = (
    "mongodb://",
    "mongodb+srv://",
)


class MongoDBAdapter(FrameworkAdapter):
    """Detect MongoDB via compose images, URIs, or common config layouts."""

    name = "MongoDB"
    priority = 75
    external = True

    def detect(self, path: Path) -> bool:
        directory = path.expanduser()
        if not directory.is_dir():
            return False

        if (directory / "mongod.conf").is_file():
            return True

        text = compose_text(directory).lower()
        if text and _compose_looks_like_mongo(text):
            return True

        if directory.name.lower() in _MONGO_DIR_NAMES:
            if (directory / "Dockerfile").is_file():
                return True
            if text:
                return True
            # Connection strings / mongod hints in local env files.
            if _env_mentions_mongo(directory):
                return True

        # Standalone compose/env at this directory without a mongo-named folder
        # still counts when image/service keys are explicit.
        if _env_mentions_mongo(directory) and (
            text or (directory / "mongod.conf").is_file()
        ):
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
        detected = detect_infra_port(directory, kind="mongodb")
        # Engine default only when conf/compose omit an explicit host port.
        port = detected if detected is not None else 27017
        return AdapterServiceSpec(
            framework=self.name,
            command="",
            uses_port=True,
            health="tcp",
            fixed_port=port,
            preferred_port=detected,
            external=True,
            external_type="mongodb",
        )


def _compose_looks_like_mongo(text: str) -> bool:
    """True for explicit MongoDB compose image/service keys (no soft guesses)."""

    markers = (
        "image: mongo",
        "image:mongo",
        "image: mongodb",
        "image:mongodb",
        "mongo:",
        "mongodb:",
        "mongod",
    )
    if any(marker in text for marker in markers):
        # Avoid matching unrelated "mongo" substrings inside app names by
        # requiring a compose image/service style token already checked above,
        # or an explicit URI / port 27017 binding.
        if "27017" in text:
            return True
        if any(uri in text for uri in _MONGO_URI_MARKERS):
            return True
        if "image: mongo" in text or "image:mongo" in text:
            return True
        if "image: mongodb" in text or "image:mongodb" in text:
            return True
        # ``mongo:`` / ``mongodb:`` service keys (YAML mapping).
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("mongo:") or stripped.startswith("mongodb:"):
                return True
            if stripped.startswith("image:") and "mongo" in stripped:
                return True
    return False


def _env_mentions_mongo(directory: Path) -> bool:
    for name in (".env", ".env.local", ".env.development", ".env.dev"):
        path = directory / name
        if not path.is_file():
            continue
        text = read_text(path).lower()
        if any(uri in text for uri in _MONGO_URI_MARKERS):
            return True
        if "mongod" in text or "mongodb" in text:
            return True
    return False
