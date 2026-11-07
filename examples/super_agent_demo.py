"""Super-agent style demo: Router batching, isolated Workers, thread fast path.

Run::

    python examples/super_agent_demo.py

What to look for:

* two different conversations are batched into one Router cycle;
* each conversation gets an isolated Worker session;
* a follow-up message on an already-subscribed thread bypasses the Router and
  goes straight to the existing Worker (fast path);
* reports are delivered through a channel plugin (memory channel here).
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chatanything import Harness, HarnessConfig, LifecyclePoint


async def main() -> None:
    config = HarnessConfig(
        agent_name="ChatAnything",
        default_channel="memory",
        batch_window_seconds=0.05,
        log_level=logging.WARNING,
    )
    async with Harness(config) as harness:
        # Two independent threads arriving at nearly the same time.
        await harness.submit(
            "请分析当前方案的主要风险，并给出缓解建议。",
            actor="alice",
            channel="memory",
            thread_id="thread-alpha",
        )
        await harness.submit(
            "请检查构建失败的原因，并给出下一步动作。",
            actor="bob",
            channel="memory",
            thread_id="thread-beta",
        )
        await harness.wait_until_idle(timeout=15)

        # Follow-up on alpha: the Worker already subscribed to this thread.
        await harness.submit(
            "继续补充第二个风险的细节。",
            actor="alice",
            channel="memory",
            thread_id="thread-alpha",
        )
        await harness.wait_until_idle(timeout=15)

        channel = harness.container.resolve(
            __import__("chatanything").Capability.CHANNEL, "memory"
        )
        print("=== delivered messages ===")
        for message in channel.messages:
            print(f"[{message.kind}] thread={message.thread_id}: {message.text}")

        counts = Counter(event.point.value for event in harness.lifecycle.history)
        ticks = [
            event.get("batch_size")
            for event in harness.lifecycle.history
            if event.point == LifecyclePoint.HARNESS_TICK
        ]
        fast_path = [
            event for event in harness.lifecycle.history
            if event.point == LifecyclePoint.TRIGGER_ROUTED and event.get("fast_path")
        ]
        print("\n=== runtime observations ===")
        print("router batch sizes:", ticks)
        print("fast-path deliveries:", len(fast_path))
        print("lifecycle event counts:", dict(counts))
        print("worker sessions:", len(await harness.runtime.memory.list_sessions(kind="worker")))
        print(
            "thread subscriptions:",
            len(await harness.runtime.memory.list_subscriptions(thread_id="thread-alpha")),
        )


if __name__ == "__main__":
    asyncio.run(main())
