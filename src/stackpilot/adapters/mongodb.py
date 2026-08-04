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

        # Explicit mongodb:// in a non-app directory (no framework entrypoint)
        # is enough. Soft "mongodb" text next to an unrelated compose file is not.
        if _env_has_mongo_uri(directory) and not _looks_like_application(directory):
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

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("image:") and _compose_image_is_mongo(stripped):
            return True
        # YAML service keys ``mongo:`` / ``mongodb:`` (not image tags).
        if stripped.startswith("mongo:") or stripped.startswith("mongodb:"):
            return True

    if any(uri in text for uri in _MONGO_URI_MARKERS):
        return True

    # Port + explicit mongo token reinforces shared compose layouts.
    if "27017" in text and ("mongodb" in text or "mongod" in text):
        return True

    return False


def _compose_image_is_mongo(image_line: str) -> bool:
    """
    True for official / common MongoDB images.

    Matches ``mongo``, ``mongodb``, ``library/mongo``, ``bitnami/mongodb``,
    and tagged variants. Rejects incidental names like ``mongolian-api``.
    """

    # ``image: bitnami/mongodb:6.0`` → ``bitnami/mongodb:6.0``
    _, _, rest = image_line.partition(":")
    image = rest.strip().strip("\"'")
    if not image:
        return False
    # Drop registry host if present (``docker.io/library/mongo:6``).
    path = image
    if "/" in image and ("." in image.split("/", 1)[0] or ":" in image.split("/", 1)[0]):
        path = image.split("/", 1)[1]
    # Final path segment without tag.
    name = path.rsplit("/", 1)[-1].split(":", 1)[0].strip().lower()
    return name in {"mongo", "mongodb"}


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


def _env_has_mongo_uri(directory: Path) -> bool:
    """True only for explicit ``mongodb://`` / ``mongodb+srv://`` connection strings."""

    for name in (".env", ".env.local", ".env.development", ".env.dev"):
        path = directory / name
        if not path.is_file():
            continue
        text = read_text(path).lower()
        if any(uri in text for uri in _MONGO_URI_MARKERS):
            return True
    return False


def _looks_like_application(directory: Path) -> bool:
    """True when the directory has a typical application entrypoint."""

    for relative in (
        "main.py",
        "app.py",
        "wsgi.py",
        "asgi.py",
        "manage.py",
        "server.py",
        "application.py",
        "package.json",
        "go.mod",
        "Cargo.toml",
    ):
        if (directory / relative).is_file():
            return True
    return False
