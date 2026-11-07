"""Execution environment decoupled from compute and persisted state.

The default environment is local and shell access is off unless explicitly
enabled -- a safe, honest default for a harness that may be driven by
untrusted external events.
"""

from __future__ import annotations

import asyncio
import platform
import sys
import time
from pathlib import Path

from ...errors import EnvironmentExecutionError
from ...models import ExecResult
from ...registry import Capability
from ..base import Plugin, PluginContext, PluginMeta
from ..interfaces import EnvironmentProvider


class LocalEnvironment:
    """Run commands on the local machine, optionally within a fixed workspace."""

    name = "local"

    def __init__(self, *, cwd: str | None = None, allow_shell: bool = False) -> None:
        self.cwd = cwd or str(Path.cwd())
        self.allow_shell = allow_shell

    async def describe(self) -> dict[str, object]:
        return {
            "name": self.name,
            "cwd": self.cwd,
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "allow_shell": self.allow_shell,
        }

    async def execute(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout: float = 30.0,
    ) -> ExecResult:
        if not self.allow_shell:
            raise EnvironmentExecutionError(
                "local shell execution is disabled; set allow_shell=True explicitly"
            )
        started = time.time()
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=cwd or self.cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise EnvironmentExecutionError(f"command timed out after {timeout}s: {command}")
        return ExecResult(
            command=command,
            exit_code=process.returncode or 0,
            stdout=stdout.decode(errors="replace"),
            stderr=stderr.decode(errors="replace"),
            duration=time.time() - started,
            metadata={"cwd": cwd or self.cwd},
        )


class LocalEnvironmentPlugin(Plugin):
    meta = PluginMeta(
        name="environment.local",
        version="0.1.0",
        description="Local execution environment (shell disabled by default).",
        provides=("environment",),
        priority=0,
    )

    async def install(self, context: PluginContext) -> None:
        environment = LocalEnvironment(
            cwd=context.setting("cwd", None),
            allow_shell=bool(context.setting("allow_shell", False)),
        )
        context.container.register(
            Capability.ENVIRONMENT,
            environment.name,
            environment,
            plugin=self.meta.name,
            priority=self.meta.priority,
        )


_ = EnvironmentProvider
