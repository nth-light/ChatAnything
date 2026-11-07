"""Web application layer: static UI plus REST API."""

from __future__ import annotations

from .routes import WebApplication
from .server import HttpServer

__all__ = ["WebApplication", "HttpServer"]
