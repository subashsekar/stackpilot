"""Framework adapters for StackPilot service discovery and generation."""

from __future__ import annotations

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
from .registry import AdapterRegistry, create_default_registry, default_registry

__all__ = [
    "AdapterRegistry",
    "AdapterServiceSpec",
    "CeleryAdapter",
    "DjangoAdapter",
    "ExpressAdapter",
    "FastAPIAdapter",
    "FlaskAdapter",
    "FrameworkAdapter",
    "GenericAdapter",
    "MongoDBAdapter",
    "NestJSAdapter",
    "PostgresAdapter",
    "RabbitMQAdapter",
    "RedisAdapter",
    "create_default_registry",
    "default_registry",
]
