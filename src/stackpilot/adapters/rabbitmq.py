"""RabbitMQ infrastructure adapter."""

from __future__ import annotations

from pathlib import Path

from .base import (
    AdapterServiceSpec,
    FrameworkAdapter,
    compose_text,
    read_text,
)
from .detect.ports import detect_infra_port


_RABBIT_DIR_NAMES = frozenset(
    {
        "rabbit",
        "rabbitmq",
        "amqp",
        "broker",
    }
)

_AMQP_URI_MARKERS = (
    "amqp://",
    "amqps://",
)


class RabbitMQAdapter(FrameworkAdapter):
    """Detect RabbitMQ via compose images, AMQP URIs, or common layouts."""

    name = "RabbitMQ"
    priority = 78
    external = True

    def detect(self, path: Path) -> bool:
        directory = path.expanduser()
        if not directory.is_dir():
            return False

        if (directory / "rabbitmq.conf").is_file():
            return True

        text = compose_text(directory).lower()
        if text and _compose_looks_like_rabbit(text):
            return True

        if directory.name.lower() in _RABBIT_DIR_NAMES:
            if (directory / "Dockerfile").is_file():
                return True
            if text:
                return True
            if _env_mentions_rabbit(directory):
                return True

        # Explicit amqp:// in a non-app directory is enough. Soft "rabbitmq"
        # text alone is not a detection signal.
        if _env_has_amqp_uri(directory) and not _looks_like_application(directory):
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
        detected = detect_infra_port(directory, kind="rabbitmq")
        # Engine default only when conf/compose omit an explicit host port.
        port = detected if detected is not None else 5672
        return AdapterServiceSpec(
            framework=self.name,
            command="",
            uses_port=True,
            health="tcp",
            fixed_port=port,
            preferred_port=detected,
            external=True,
            external_type="rabbitmq",
        )


def _compose_looks_like_rabbit(text: str) -> bool:
    """True for explicit RabbitMQ compose image/service keys (no soft guesses)."""

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("image:") and _compose_image_is_rabbit(stripped):
            return True
        if stripped.startswith("rabbitmq:") or stripped.startswith("rabbit:"):
            return True

    if any(uri in text for uri in _AMQP_URI_MARKERS):
        return True

    # Port + explicit rabbit/amqp token reinforces shared compose layouts.
    if "5672" in text and ("rabbitmq" in text or "amqp://" in text or "amqps://" in text):
        return True

    return False


def _compose_image_is_rabbit(image_line: str) -> bool:
    """True for official / common RabbitMQ images (rejects incidental names)."""

    _, _, rest = image_line.partition(":")
    image = rest.strip().strip("\"'")
    if not image:
        return False
    path = image
    if "/" in image and ("." in image.split("/", 1)[0] or ":" in image.split("/", 1)[0]):
        path = image.split("/", 1)[1]
    name = path.rsplit("/", 1)[-1].split(":", 1)[0].strip().lower()
    return name in {"rabbitmq", "rabbit"}


def _env_mentions_rabbit(directory: Path) -> bool:
    for name in (".env", ".env.local", ".env.development", ".env.dev"):
        path = directory / name
        if not path.is_file():
            continue
        text = read_text(path).lower()
        if any(uri in text for uri in _AMQP_URI_MARKERS):
            return True
        if "rabbitmq" in text:
            return True
    return False


def _env_has_amqp_uri(directory: Path) -> bool:
    """True only for explicit ``amqp://`` / ``amqps://`` connection strings."""

    for name in (".env", ".env.local", ".env.development", ".env.dev"):
        path = directory / name
        if not path.is_file():
            continue
        text = read_text(path).lower()
        if any(uri in text for uri in _AMQP_URI_MARKERS):
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
