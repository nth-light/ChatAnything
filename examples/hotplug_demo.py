"""Hot-plug walkthrough: load, disable, enable, reload and unload at runtime.

Run with::

    python examples/hotplug_demo.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chatanything import Harness, HarnessConfig  # noqa: E402
from chatanything.plugins.interfaces import ToolContext  # noqa: E402
from chatanything.registry import Capability  # noqa: E402

PLUGIN_DIR = Path(__file__).parent / "plugins" / "hotplug_greet"


def print_status(harness: Harness) -> None:
    payload = [status.to_dict() for status in harness.plugins.status()]
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


async def main() -> None:
    harness = Harness(HarnessConfig(log_level=40, plugin_resource_monitoring=True))
    await harness.setup()
    await harness.start()

    name = "example.hotplug_greet"

    # 1. Hot load from a local package directory (manifest + code).
    record = await harness.plugins.load(PLUGIN_DIR)
    print(f"\nloaded: {record.meta.name}@{record.meta.version} state={record.state.value}")

    # 2. The capability is immediately resolvable and usable.
    tool = harness.container.resolve(Capability.TOOL, "hotplug_greet")
    greeting = await tool.invoke({"name": "Ada"}, ToolContext(runtime=harness.runtime))
    print("tool says:", greeting)

    # 3. Live monitoring: state, capabilities, timings, CPU and memory.
    print_status(harness)

    # 4. Disable withdraws capabilities but keeps the plugin loaded.
    await harness.plugins.disable(name)
    print(
        "\nafter disable:",
        harness.plugins.status_of(name).state.value,
        "tool registered:",
        harness.container.contains(Capability.TOOL, "hotplug_greet"),
    )

    # 5. Enable restores the previous capability set and default selections.
    await harness.plugins.enable(name)
    print(
        "after enable:",
        harness.plugins.status_of(name).state.value,
        "tool registered:",
        harness.container.contains(Capability.TOOL, "hotplug_greet"),
    )

    # 6. Reload creates a fresh instance/generation with a clean module namespace.
    new_record = await harness.plugins.reload(name)
    print(f"after reload: generation={new_record.generation}")

    # 7. Unload removes code, capabilities, hooks and sandbox namespace.
    await harness.plugins.unload(name)
    print("after unload:", harness.plugins.installed_names())

    await harness.stop()


if __name__ == "__main__":
    asyncio.run(main())
