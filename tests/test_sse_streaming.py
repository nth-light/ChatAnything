"""Tests for token-level SSE streaming.

Covers the stream hub, the deterministic ``echo`` provider and the full HTTP
round-trip: ``POST /api/chat`` followed by ``GET /api/chat/stream``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
import time
import unittest
import urllib.error
import urllib.request

from chatanything.config import HarnessConfig
from chatanything.core.harness import Harness
from chatanything.models import Message
from chatanything.plugins.builtin.llm import EchoLLM
from chatanything.plugins.builtin.streaming import InMemoryStreamHub

STORAGE_PLUGIN = "chatanything.plugins.builtin.storage_sqlite:SqliteStoragePlugin"
TOOLS_PLUGIN = "chatanything.plugins.builtin.aicoding_tools:AICodingToolsPlugin"
WEB_PLUGIN = "chatanything.plugins.builtin.web_service:WebServicePlugin"


class StreamHubTestCase(unittest.TestCase):
    def test_publish_replay_and_terminal_event(self):
        hub = InMemoryStreamHub()
        hub.publish("trigger:t1", {"type": "start"})
        hub.publish("trigger:t1", {"type": "delta", "delta": "he"})
        hub.publish("trigger:t1", {"type": "delta", "delta": "llo"})

        subscription = hub.subscribe("trigger:t1")
        try:
            first = subscription.next_event(timeout=1.0)
            self.assertEqual(first["type"], "start")
            self.assertEqual(subscription.next_event(timeout=1.0)["delta"], "he")
            self.assertEqual(subscription.next_event(timeout=1.0)["delta"], "llo")

            hub.publish("trigger:t1", {"type": "done", "content": "hello"})
            done = subscription.next_event(timeout=1.0)
            self.assertEqual(done["type"], "done")
            self.assertEqual(done["content"], "hello")

            with self.assertRaises(TimeoutError):
                subscription.next_event(timeout=0.05)
        finally:
            subscription.close()

    def test_late_subscriber_receives_buffered_events(self):
        hub = InMemoryStreamHub()
        hub.publish("trigger:t2", {"type": "delta", "delta": "a"})
        hub.publish("trigger:t2", {"type": "done", "content": "a"})

        subscription = hub.subscribe("trigger:t2")
        try:
            events = [subscription.next_event(timeout=1.0) for _ in range(2)]
        finally:
            subscription.close()
        self.assertEqual([event["type"] for event in events], ["delta", "done"])


class EchoStreamTestCase(unittest.TestCase):
    def test_astream_yields_deltas_and_final_response(self):
        async def collect():
            llm = EchoLLM(chunk_size=3, stream_delay=0.0)
            return [
                chunk
                async for chunk in llm.astream([Message.user("hi there")], tools=[])
            ]

        chunks = asyncio.run(collect())
        text = "".join(chunk.delta for chunk in chunks)
        self.assertEqual(text, "Echo: hi there")
        self.assertTrue(len([chunk for chunk in chunks if chunk.delta]) > 1)
        final = chunks[-1]
        self.assertIsNotNone(final.response)
        self.assertEqual(final.response.content, text)
        self.assertIn("completion_tokens", final.response.usage)


class SseWebTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.workdir = tempfile.mkdtemp(prefix="chatanything-sse-")
        database = os.path.join(self.workdir, "chatanything.db")
        config = HarnessConfig(
            log_level=logging.WARNING,
            plugin_modules=[STORAGE_PLUGIN, TOOLS_PLUGIN, WEB_PLUGIN],
            plugin_config={
                "storage.sqlite": {"path": database},
                "environment.local": {"cwd": self.workdir, "allow_shell": True},
                "tools.aicoding": {"root": self.workdir},
                "web.http": {"host": "127.0.0.1", "port": 0},
                "llm.echo": {"stream_delay": 0.002, "chunk_size": 2},
            },
            defaults={},
        )
        self.harness = Harness(config)
        await self.harness.setup()
        await self.harness.start()

        record = self.harness.plugins.get("web.http")
        self.assertIsNotNone(record)
        port = record.plugin.bound_port
        self.base = f"http://127.0.0.1:{port}"

    async def asyncTearDown(self) -> None:
        try:
            await self.harness.stop()
        finally:
            shutil.rmtree(self.workdir, ignore_errors=True)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _post_chat(self, text: str) -> dict:
        data = json.dumps({"text": text}).encode("utf-8")
        request = urllib.request.Request(
            self.base + "/api/chat",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10.0) as response:
            return json.loads(response.read().decode("utf-8"))

    def _read_stream(self, trigger_id: str, timeout: float = 20.0) -> list[dict]:
        url = f"{self.base}/api/chat/stream?trigger_id={trigger_id}&timeout=15"
        request = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            self.assertEqual(
                response.headers.get("Content-Type", "").split(";")[0].strip(),
                "text/event-stream",
            )
            raw = response.read().decode("utf-8")

        events: list[dict] = []
        for block in raw.split("\n\n"):
            block = block.strip()
            if not block or block.startswith(":"):
                continue
            for line in block.splitlines():
                if line.startswith("data:"):
                    events.append(json.loads(line[5:].strip()))
        return events

    def _stream_events(self, trigger_id: str) -> list[dict]:
        started = time.monotonic()
        events = self._read_stream(trigger_id)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 20.0, "stream did not terminate in time")
        return events

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------
    async def test_chat_streams_incremental_deltas_until_done(self):
        payload = await asyncio.to_thread(self._post_chat, "请介绍一下 SSE 流式输出？")
        self.assertTrue(payload["accepted"])
        trigger_id = payload["trigger_id"]

        events = await asyncio.to_thread(self._stream_events, trigger_id)
        self.assertTrue(events)
        self.assertEqual(events[0]["type"], "start")

        deltas = [event["delta"] for event in events if event.get("type") == "delta"]
        self.assertGreater(len(deltas), 1, "expected multiple incremental chunks")
        self.assertEqual(events[-1]["type"], "done")

        streamed = "".join(deltas).strip()
        self.assertEqual(streamed, events[-1]["content"])
        self.assertTrue(streamed.startswith("Echo:"))

        await self.harness.wait_until_idle(timeout=10)

    async def test_ignored_trigger_terminates_the_stream(self):
        # Long enough for the slow-path filter to accept it, yet without any
        # task signal, so the Router heuristic decides to ignore it.
        text = "streaming context without any actionable keyword " * 3
        payload = await asyncio.to_thread(self._post_chat, text)
        self.assertTrue(payload["accepted"])
        trigger_id = payload["trigger_id"]

        events = await asyncio.to_thread(self._stream_events, trigger_id)
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(events[-1]["stopped_reason"], "ignored")
        self.assertTrue(events[-1]["reason"])

    async def test_stream_requires_a_trigger_id(self):
        def request():
            try:
                urllib.request.urlopen(self.base + "/api/chat/stream", timeout=5.0)
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read().decode("utf-8"))
            return 200, None

        status, body = await asyncio.to_thread(request)
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    async def test_unknown_trigger_times_out_with_error_event(self):
        def request():
            url = f"{self.base}/api/chat/stream?trigger_id=trigger_missing&timeout=0.4&heartbeat=0.2"
            with urllib.request.urlopen(url, timeout=10.0) as response:
                return json.loads(
                    response.read().decode("utf-8").split("data:", 1)[1].strip()
                )

        event = await asyncio.to_thread(request)
        self.assertEqual(event["type"], "error")
        self.assertEqual(event["stopped_reason"], "timeout")


if __name__ == "__main__":
    unittest.main()
