"""Command-line entry point for the ChatAnything web UI.

Run with ``python -m chatanything.webapp`` or, after installation,
``chatanything-web``.  The harness is assembled from three plugins only:
SQLite storage, AI coding tools and the HTTP web service, so the server starts
without any third-party dependencies.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from ..config import HarnessConfig
from ..core.harness import Harness

_LOG_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chatanything-web",
        description="Start the ChatAnything web UI and REST API.",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address for the HTTP server (default: 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8790,
        help="Listen port; 0 picks a free port (default: 8790).",
    )
    parser.add_argument(
        "--db",
        default="./chatanything.db",
        help="Path to the SQLite database file (default: ./chatanything.db).",
    )
    parser.add_argument(
        "--workspace",
        default=None,
        help="Workspace root for tools and the local environment (default: cwd).",
    )
    parser.add_argument(
        "--log-level",
        choices=sorted(_LOG_LEVELS),
        default="INFO",
        help="Logging level (default: INFO).",
    )
    return parser


def build_config(args: argparse.Namespace) -> HarnessConfig:
    """Translate parsed CLI arguments into a harness configuration."""

    db_path = Path(args.db).expanduser().resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    workspace = (
        Path(args.workspace).expanduser().resolve()
        if args.workspace
        else Path.cwd().resolve()
    )
    return HarnessConfig(
        agent_name="ChatAnything Web",
        log_level=_LOG_LEVELS[args.log_level],
        plugin_modules=[
            "chatanything.plugins.builtin.storage_sqlite:SqliteStoragePlugin",
            "chatanything.plugins.builtin.aicoding_tools:AICodingToolsPlugin",
            "chatanything.plugins.builtin.web_service:WebServicePlugin",
        ],
        defaults={
            "memory": "sqlite",
            "config_store": "sqlite",
            "mcp": "sqlite",
            "skill": "sqlite",
        },
        plugin_config={
            "storage.sqlite": {"path": str(db_path)},
            "environment.local": {"cwd": str(workspace), "allow_shell": True},
            "tools.aicoding": {"root": str(workspace)},
            "web.http": {"host": args.host, "port": args.port},
        },
    )


async def run(args: argparse.Namespace) -> None:
    """Set up the harness, serve until interrupted, then shut down."""

    config = build_config(args)
    db_path = config.plugin_config["storage.sqlite"]["path"]
    workspace = config.plugin_config["tools.aicoding"]["root"]

    harness = Harness(config)
    await harness.setup()
    await harness.start()

    record = harness.plugins.get("web.http")
    if record is None:
        raise RuntimeError("web.http plugin is not installed")
    port = record.plugin.bound_port
    if not port:
        raise RuntimeError("web.http plugin did not bind a port")

    print(f"ChatAnything Web UI: http://{args.host}:{port}", flush=True)
    print(f"Database: {db_path}", flush=True)
    print(f"Workspace: {workspace}", flush=True)

    try:
        while True:
            await asyncio.sleep(1)
    finally:
        await harness.stop()


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, run the server and return a process exit code."""

    args = _build_parser().parse_args(argv)
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        return 0
    except Exception:  # noqa: BLE001 - report the failure and exit non-zero
        logging.exception("failed to start the ChatAnything web UI")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
