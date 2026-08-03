"""Central registry for framework adapters."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from .base import AdapterServiceSpec, FrameworkAdapter
from .celery import CeleryAdapter
from .django import DjangoAdapter
from .express import ExpressAdapter
from .fastapi import FastAPIAdapter
from .flask import FlaskAdapter
from .generic import GenericAdapter
from .mongodb import MongoDBAdapter
from .nestjs import NestJSAdapter
from .postgres import PostgresAdapter
from .rabbitmq import RabbitMQAdapter
from .redis import RedisAdapter


class AdapterRegistry:
    """
    Ordered collection of framework adapters.

    Matching walks adapters by ascending ``priority`` and returns the first
    positive ``detect()``. There are no framework if/elif chains at call sites.
    """

    def __init__(self) -> None:
        self._adapters: list[FrameworkAdapter] = []

    def register(self, adapter: FrameworkAdapter) -> None:
        """Register ``adapter``, replacing any prior adapter with the same name."""

        self._adapters = [item for item in self._adapters if item.name != adapter.name]
        self._adapters.append(adapter)
        self._adapters.sort(key=lambda item: (item.priority, item.name))

    def register_many(self, adapters: Iterable[FrameworkAdapter]) -> None:
        for adapter in adapters:
            self.register(adapter)

    def clear(self) -> None:
        self._adapters.clear()

    def all(self) -> tuple[FrameworkAdapter, ...]:
        return tuple(self._adapters)

    def get(self, name: str) -> FrameworkAdapter | None:
        for adapter in self._adapters:
            if adapter.name == name:
                return adapter
        return None

    def match(self, path: Path) -> FrameworkAdapter | None:
        """Return the first adapter that detects ``path``, or ``None``."""

        directory = path.expanduser()
        if not directory.is_dir():
            return None
        for adapter in self._adapters:
            if adapter.detect(directory):
                return adapter
        return None

    def detect_framework(self, path: Path) -> str | None:
        adapter = self.match(path)
        return None if adapter is None else adapter.name

    def generate_service(
        self,
        path: Path,
        *,
        port: int | None = None,
        framework: str | None = None,
    ) -> AdapterServiceSpec | None:
        """
        Build adapter metadata for ``path``.

        When ``framework`` is provided, that adapter is used directly
        (generator path). Otherwise the registry matches the directory.
        """

        if framework is not None:
            adapter = self.get(framework)
            if adapter is None:
                adapter = self.get("Generic")
            if adapter is None:
                return None
            return adapter.generate_service(path, port=port)

        adapter = self.match(path)
        if adapter is None:
            return None
        return adapter.generate_service(path, port=port)


def create_default_registry() -> AdapterRegistry:
    """Build a registry with all built-in adapters."""

    registry = AdapterRegistry()
    registry.register_many(
        [
            NestJSAdapter(),
            ExpressAdapter(),
            DjangoAdapter(),
            CeleryAdapter(),
            FastAPIAdapter(),
            FlaskAdapter(),
            PostgresAdapter(),
            MongoDBAdapter(),
            RabbitMQAdapter(),
            RedisAdapter(),
            GenericAdapter(),
        ]
    )
    return registry


# Process-wide default used by scanner and generator.
default_registry: AdapterRegistry = create_default_registry()
