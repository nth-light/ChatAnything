"""End-to-end tests for the HTTP service and REST API.

HTTP calls are executed through ``asyncio.to_thread`` so the server thread can
call back into the harness event loop without deadlocking.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
import unittest
import urllib.error
import urllib.request
from typing import Any

from chatanything.config import HarnessConfig
from chatanything.core.harness import Harness

STORAGE_PLUGIN = "chatanything.plugins.builtin.storage_sqlite:SqliteStoragePlugin"
TOOLS_PLUGIN = "chatanything.plugins.builtin.aicoding_tools:AICodingToolsPlugin"
WEB_PLUGIN = "chatanything.plugins.builtin.web_service:WebServicePlugin"


class WebServiceTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.workdir = tempfile.mkdtemp(prefix="chatanything-web-")
        self.database = os.path.join(self.workdir, "chatanything.db")
        config = HarnessConfig(
            log_level=logging.WARNING,
            plugin_modules=[STORAGE_PLUGIN, TOOLS_PLUGIN, WEB_PLUGIN],
            plugin_config={
                "storage.sqlite": {"path": self.database},
                "environment.local": {"cwd": self.workdir, "allow_shell": True},
                "tools.aicoding": {"root": self.workdir},
                "web.http": {"host": "127.0.0.1", "port": 0},
            },
            defaults={},
        )
        self.harness = Harness(config)
        await self.harness.setup()
        await self.harness.start()

        record = self.harness.plugins.get("web.http")
        self.assertIsNotNone(record)
        port = record.plugin.bound_port
        self.assertIsInstance(port, int)
        self.base = f"http://127.0.0.1:{port}"

    async def asyncTearDown(self) -> None:
        harness = getattr(self, "harness", None)
        workdir = getattr(self, "workdir", None)
        try:
            if harness is not None:
                await harness.stop()
        finally:
            if workdir:
                shutil.rmtree(workdir, ignore_errors=True)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _perform_request(self, method: str, path: str, body: Any) -> tuple[int, Any]:
        data: bytes | None = None
        headers: dict[str, str] = {}
        if body is not None:
            if isinstance(body, (bytes, bytearray)):
                data = bytes(body)
            elif isinstance(body, str):
                data = body.encode("utf-8")
            else:
                data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(
            self.base + path, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=10.0) as response:
                status = response.getcode()
                raw = response.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            raw = exc.read()

        text = raw.decode("utf-8", errors="replace")
        if not text:
            return status, None
        try:
            return status, json.loads(text)
        except json.JSONDecodeError:
            return status, text

    async def request(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        return await asyncio.to_thread(self._perform_request, method, path, body)

    # ------------------------------------------------------------------
    # Static UI
    # ------------------------------------------------------------------
    async def test_static_ui_is_served(self):
        status, payload = await self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIsInstance(payload, str)
        self.assertIn("ChatAnything", payload)
        self.assertIn("#models", payload)

        status, payload = await self.request("GET", "/app.js")
        self.assertEqual(status, 200)
        self.assertIsInstance(payload, str)
        self.assertTrue(payload.strip())

        status, payload = await self.request("GET", "/style.css")
        self.assertEqual(status, 200)
        self.assertIsInstance(payload, str)
        self.assertTrue(payload.strip())

        status, _ = await self.request("GET", "/missing-file.html")
        self.assertEqual(status, 404)

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------
    async def test_health_reports_state_capabilities_and_tools(self):
        status, payload = await self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertIsInstance(payload, dict)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["state"], "running")

        capabilities = payload["capabilities"]
        for slot in ("memory", "config_store", "mcp", "skill"):
            self.assertIn(slot, capabilities)
            self.assertIn("sqlite", capabilities[slot])

        tools = payload["tools"]
        for name in ("web_search", "file_edit", "run_command"):
            self.assertIn(name, tools)

    # ------------------------------------------------------------------
    # Model config CRUD
    # ------------------------------------------------------------------
    async def test_model_crud_default_and_error_handling(self):
        created_status, created = await self.request(
            "POST",
            "/api/models",
            {
                "name": "gpt-test",
                "provider": "openai",
                "model": "gpt-4o",
                "params": {"temperature": 0.1},
                "is_default": True,
            },
        )
        self.assertEqual(created_status, 201)
        self.assertEqual(created["name"], "gpt-test")
        config_id = created["id"]

        status, listing = await self.request("GET", "/api/models")
        self.assertEqual(status, 200)
        self.assertIsInstance(listing, list)
        self.assertIn("gpt-test", [item["name"] for item in listing])

        status, detail = await self.request("GET", f"/api/models/{config_id}")
        self.assertEqual(status, 200)
        self.assertEqual(detail["model"], "gpt-4o")
        self.assertEqual(detail["params"], {"temperature": 0.1})

        status, updated = await self.request(
            "PUT", f"/api/models/{config_id}", {"model": "gpt-4o-mini"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(updated["model"], "gpt-4o-mini")

        status, selected = await self.request(
            "POST", f"/api/models/{config_id}/default", {}
        )
        self.assertEqual(status, 200)
        self.assertEqual(selected["id"], config_id)
        self.assertTrue(selected["is_default"])

        status, listing = await self.request("GET", "/api/models")
        self.assertEqual(status, 200)
        defaults = [item for item in listing if item["is_default"]]
        self.assertEqual(len(defaults), 1)
        self.assertEqual(defaults[0]["id"], config_id)

        status, deleted = await self.request("DELETE", f"/api/models/{config_id}")
        self.assertEqual(status, 200)
        self.assertTrue(deleted["deleted"])
        status, _ = await self.request("GET", f"/api/models/{config_id}")
        self.assertEqual(status, 404)

        status, error = await self.request("POST", "/api/models", "{not valid json")
        self.assertEqual(status, 400)
        self.assertIsInstance(error, dict)
        self.assertIn("error", error)

        status, error = await self.request("POST", "/api/models", {"model": "missing-name"})
        self.assertEqual(status, 400)
        self.assertIn("error", error)

        status, error = await self.request("GET", "/api/does-not-exist")
        self.assertEqual(status, 404)
        self.assertIn("error", error)

    # ------------------------------------------------------------------
    # MCP + skills
    # ------------------------------------------------------------------
    async def test_mcp_and_skill_crud(self):
        status, server = await self.request(
            "POST",
            "/api/mcp/servers",
            {"name": "test-server", "transport": "stdio", "command": "echo"},
        )
        self.assertEqual(status, 201)
        server_id = server["id"]

        status, entry = await self.request(
            "PUT",
            f"/api/mcp/servers/{server_id}/entries",
            {"kind": "tool", "name": "search", "payload": {"description": "search docs"}},
        )
        self.assertEqual(status, 200)
        self.assertEqual(entry["kind"], "tool")
        self.assertEqual(entry["name"], "search")

        status, entries = await self.request(
            "GET", f"/api/mcp/servers/{server_id}/entries"
        )
        self.assertEqual(status, 200)
        self.assertEqual([item["name"] for item in entries], ["search"])

        status, filtered = await self.request(
            "GET", f"/api/mcp/servers/{server_id}/entries?kind=tool"
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(filtered), 1)

        status, deleted = await self.request(
            "DELETE", f"/api/mcp/servers/{server_id}/entries/tool/search"
        )
        self.assertEqual(status, 200)
        self.assertTrue(deleted["deleted"])
        status, entries = await self.request(
            "GET", f"/api/mcp/servers/{server_id}/entries"
        )
        self.assertEqual(status, 200)
        self.assertEqual(entries, [])

        status, deleted = await self.request("DELETE", f"/api/mcp/servers/{server_id}")
        self.assertEqual(status, 200)
        self.assertTrue(deleted["deleted"])
        status, _ = await self.request("GET", f"/api/mcp/servers/{server_id}")
        self.assertEqual(status, 404)
        # The entries route stays empty after the server (and its entries) is gone.
        status, entries = await self.request(
            "GET", f"/api/mcp/servers/{server_id}/entries"
        )
        self.assertEqual(status, 200)
        self.assertEqual(entries, [])

        status, skill = await self.request(
            "POST",
            "/api/skills",
            {"name": "lint", "description": "old description", "tags": ["code"]},
        )
        self.assertEqual(status, 201)
        skill_id = skill["id"]

        status, skills = await self.request("GET", "/api/skills")
        self.assertEqual(status, 200)
        self.assertIn("lint", [item["name"] for item in skills])

        status, updated = await self.request(
            "PUT", f"/api/skills/{skill_id}", {"description": "new description"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(updated["description"], "new description")

        status, detail = await self.request("GET", f"/api/skills/{skill_id}")
        self.assertEqual(status, 200)
        self.assertEqual(detail["description"], "new description")

        status, deleted = await self.request("DELETE", f"/api/skills/{skill_id}")
        self.assertEqual(status, 200)
        self.assertTrue(deleted["deleted"])
        status, _ = await self.request("GET", f"/api/skills/{skill_id}")
        self.assertEqual(status, 404)

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------
    async def test_tool_listing_invocation_and_errors(self):
        status, tools = await self.request("GET", "/api/tools")
        self.assertEqual(status, 200)
        names = [item["name"] for item in tools]
        for name in ("web_search", "file_edit", "run_command"):
            self.assertIn(name, names)
        for item in tools:
            self.assertEqual(item["parameters"]["type"], "object")

        status, write_result = await self.request(
            "POST",
            "/api/tools/file_edit/invoke",
            {
                "arguments": {
                    "operation": "write",
                    "path": "notes/a.txt",
                    "content": "hello from tool",
                }
            },
        )
        self.assertEqual(status, 200)
        self.assertFalse(write_result["is_error"])
        self.assertIn("Wrote", write_result["content"])

        status, read_result = await self.request(
            "POST",
            "/api/tools/file_edit/invoke",
            {"arguments": {"operation": "read", "path": "notes/a.txt"}},
        )
        self.assertEqual(status, 200)
        self.assertFalse(read_result["is_error"])
        self.assertEqual(read_result["content"], "hello from tool")

        status, outside_result = await self.request(
            "POST",
            "/api/tools/file_edit/invoke",
            {"arguments": {"operation": "write", "path": "../outside.txt", "content": "x"}},
        )
        self.assertEqual(status, 200)
        self.assertTrue(outside_result["is_error"])

        status, _ = await self.request(
            "POST", "/api/tools/does_not_exist/invoke", {"arguments": {}}
        )
        self.assertEqual(status, 404)

    # ------------------------------------------------------------------
    # Chat
    # ------------------------------------------------------------------
    async def test_chat_submission_is_persisted_and_sessions_are_listed(self):
        status, payload = await self.request(
            "POST", "/api/chat", {"text": "hello from test", "actor": "tester"}
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["accepted"])
        trigger_id = payload["trigger_id"]
        self.assertTrue(trigger_id)

        await self.harness.wait_until_idle(timeout=15)

        trigger = await self.harness.runtime.memory.get_trigger(trigger_id)
        self.assertIsNotNone(trigger)
        self.assertEqual(trigger.text, "hello from test")
        self.assertEqual(trigger.actor, "tester")
        self.assertEqual(trigger.source, "web")

        status, sessions = await self.request("GET", "/api/sessions")
        self.assertEqual(status, 200)
        self.assertIsInstance(sessions, list)
        self.assertTrue(sessions)
        for session in sessions:
            self.assertIn("id", session)
            self.assertIsInstance(session["message_count"], int)


if __name__ == "__main__":
    unittest.main()
