"""Functional tests for the SQLite persistence layer.

Every test gets its own temporary database file; the database is closed
before the temporary directory is removed so Windows does not keep ``-wal``
or ``-shm`` files locked.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace

from chatanything.models import (
    Artifact,
    MCPServerConfig,
    MCPEntry,
    Message,
    ModelConfig,
    Session,
    SessionKind,
    SessionStatus,
    SkillConfig,
    Thread,
    ThreadSubscription,
    ToolCall,
    Trigger,
    new_id,
)
from chatanything.storage import SqliteDatabase


class SqliteStorageTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "chatanything-test.db")
        self.db = self._open()
        self.addCleanup(self._close)

    def _open(self) -> SqliteDatabase:
        database = SqliteDatabase(self.path)
        database.initialize()
        return database

    def _close(self) -> None:
        database = getattr(self, "db", None)
        if database is not None:
            database.close()
            self.db = None

    def _reopen(self) -> SqliteDatabase:
        self._close()
        self.db = self._open()
        return self.db

    # ------------------------------------------------------------------
    # Model configs
    # ------------------------------------------------------------------
    async def test_model_config_crud_default_and_reopen(self):
        store = self.db.model_configs
        first = ModelConfig(
            name="alpha",
            provider="openai",
            model="gpt-4o",
            base_url="https://api.example/v1",
            api_key="sk-test",
            params={"temperature": 0.2, "nested": [1, 2], "flag": True},
        )
        saved = await store.save_model_config(first)
        self.assertEqual(saved.id, first.id)
        self.assertEqual(saved.name, "alpha")
        self.assertEqual(saved.params, {"temperature": 0.2, "nested": [1, 2], "flag": True})

        second = await store.save_model_config(
            ModelConfig(name="beta", provider="local", model="gpt-4o-mini")
        )
        self.assertEqual(second.name, "beta")

        listed = await store.list_model_configs()
        self.assertEqual({item.name for item in listed}, {"alpha", "beta"})
        self.assertEqual(len(listed), 2)

        fetched = await store.get_model_config(first.id)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.model, "gpt-4o")
        self.assertEqual(fetched.base_url, "https://api.example/v1")

        updated_input = replace(saved, model="gpt-4o-2024", params={"temperature": 0.0})
        updated = await store.save_model_config(updated_input)
        self.assertEqual(updated.id, saved.id)
        self.assertEqual(updated.model, "gpt-4o-2024")
        self.assertEqual(updated.params, {"temperature": 0.0})
        self.assertEqual(updated.created_at, saved.created_at)

        # Name uniqueness is enforced across different ids.
        with self.assertRaises(ValueError):
            await store.save_model_config(ModelConfig(name="alpha", model="other"))

        await store.set_default_model(first.id)
        default = await store.get_default_model()
        self.assertIsNotNone(default)
        self.assertEqual(default.id, first.id)

        await store.set_default_model(second.id)
        after_switch = await store.list_model_configs()
        defaults = [item for item in after_switch if item.is_default]
        self.assertEqual(len(defaults), 1)
        self.assertEqual(defaults[0].id, second.id)
        self.assertEqual((await store.get_default_model()).id, second.id)

        with self.assertRaises(ValueError):
            await store.set_default_model("model_missing")

        # Everything survives a close/reopen cycle.
        store = self._reopen().model_configs
        persisted = await store.get_model_config(first.id)
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted.model, "gpt-4o-2024")
        self.assertEqual(persisted.params, {"temperature": 0.0})
        self.assertEqual(persisted.created_at, saved.created_at)
        self.assertEqual((await store.get_default_model()).id, second.id)

        self.assertTrue(await store.delete_model_config(first.id))
        self.assertFalse(await store.delete_model_config(first.id))
        self.assertIsNone(await store.get_model_config(first.id))
        self.assertTrue(await store.delete_model_config(second.id))
        self.assertEqual(await store.list_model_configs(), [])

        # Deletion is durable too.
        store = self._reopen().model_configs
        self.assertEqual(await store.list_model_configs(), [])
        self.assertIsNone(await store.get_default_model())

    # ------------------------------------------------------------------
    # MCP
    # ------------------------------------------------------------------
    async def test_mcp_servers_entries_upsert_cascade_and_reopen(self):
        store = self.db.mcp
        server = MCPServerConfig(
            name="local-server",
            transport="stdio",
            command="npx",
            args=["-y", "server"],
            env={"TOKEN": "secret"},
        )
        saved = await store.save_server(server)
        self.assertEqual(saved.name, "local-server")
        self.assertEqual(saved.env, {"TOKEN": "secret"})
        self.assertEqual(saved.args, ["-y", "server"])

        first_entry = await store.upsert_entry(
            MCPEntry(
                server_id=saved.id,
                kind="tool",
                name="search",
                payload={"description": "first", "version": 1},
            )
        )
        replacement = await store.upsert_entry(
            MCPEntry(
                server_id=saved.id,
                kind="tool",
                name="search",
                payload={"description": "second", "version": 2},
            )
        )
        self.assertEqual(replacement.id, first_entry.id)
        self.assertEqual(
            replacement.payload, {"description": "second", "version": 2}
        )

        resource = await store.upsert_entry(
            MCPEntry(server_id=saved.id, kind="resource", name="docs", payload={"uri": "x"})
        )
        prompt = await store.upsert_entry(
            MCPEntry(server_id=saved.id, kind="prompt", name="greet", payload={})
        )
        self.assertNotEqual(resource.id, prompt.id)

        tool_entries = await store.list_entries(saved.id, kind="tool")
        self.assertEqual(len(tool_entries), 1)
        self.assertEqual(tool_entries[0].id, first_entry.id)
        self.assertEqual(tool_entries[0].payload["version"], 2)

        all_entries = await store.list_entries(saved.id)
        self.assertEqual(
            {(entry.kind, entry.name) for entry in all_entries},
            {("tool", "search"), ("resource", "docs"), ("prompt", "greet")},
        )

        self.assertEqual([item.id for item in await store.list_servers()], [saved.id])
        self.assertEqual((await store.get_server(saved.id)).name, "local-server")

        # Reopen keeps servers and entries.
        store = self._reopen().mcp
        reopened_entries = await store.list_entries(saved.id)
        self.assertEqual(len(reopened_entries), 3)
        self.assertEqual(reopened_entries[0].id in {first_entry.id, resource.id, prompt.id}, True)

        self.assertTrue(await store.delete_entry(saved.id, "prompt", "greet"))
        self.assertFalse(await store.delete_entry(saved.id, "prompt", "greet"))
        self.assertEqual(len(await store.list_entries(saved.id, kind="prompt")), 0)

        self.assertTrue(await store.delete_server(saved.id))
        self.assertFalse(await store.delete_server(saved.id))
        self.assertIsNone(await store.get_server(saved.id))
        self.assertEqual(await store.list_entries(saved.id), [])
        count_row = self.db.fetchone("SELECT COUNT(*) AS total FROM mcp_entries")
        self.assertEqual(int(count_row["total"]), 0)

        store = self._reopen().mcp
        self.assertEqual(await store.list_servers(), [])
        self.assertEqual(await store.list_entries(saved.id), [])

    # ------------------------------------------------------------------
    # Skills
    # ------------------------------------------------------------------
    async def test_skill_crud_filters_and_reopen(self):
        store = self.db.skills
        enabled = SkillConfig(
            name="code-review",
            description="review code",
            path="skills/review",
            version="1.2.0",
            tags=["code", "quality"],
            metadata={"owner": "team"},
        )
        saved = await store.save_skill(enabled)
        self.assertEqual(saved.name, "code-review")
        self.assertEqual(saved.tags, ["code", "quality"])
        self.assertEqual(saved.metadata, {"owner": "team"})

        disabled = await store.save_skill(
            SkillConfig(name="legacy", description="old", enabled=False, tags=["legacy"])
        )
        self.assertFalse(disabled.enabled)

        self.assertEqual(len(await store.list_skills()), 2)
        enabled_only = await store.list_skills(enabled=True)
        self.assertEqual([item.name for item in enabled_only], ["code-review"])
        disabled_only = await store.list_skills(enabled=False)
        self.assertEqual([item.name for item in disabled_only], ["legacy"])

        tagged = await store.list_skills(tag="quality")
        self.assertEqual([item.id for item in tagged], [saved.id])
        self.assertEqual(await store.list_skills(tag="missing"), [])

        fetched = await store.get_skill(saved.id)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.version, "1.2.0")
        self.assertEqual(fetched.path, "skills/review")

        with self.assertRaises(ValueError):
            await store.save_skill(SkillConfig(name="code-review", description="conflict"))

        store = self._reopen().skills
        self.assertEqual(len(await store.list_skills()), 2)
        persisted = await store.get_skill(saved.id)
        self.assertEqual(persisted.description, "review code")
        self.assertEqual(persisted.tags, ["code", "quality"])

        self.assertTrue(await store.delete_skill(saved.id))
        self.assertFalse(await store.delete_skill(saved.id))
        self.assertIsNone(await store.get_skill(saved.id))
        self.assertEqual([item.name for item in await store.list_skills()], ["legacy"])

        store = self._reopen().skills
        self.assertEqual([item.name for item in await store.list_skills()], ["legacy"])

    # ------------------------------------------------------------------
    # Sessions / messages
    # ------------------------------------------------------------------
    async def test_session_messages_roundtrip_and_pagination(self):
        store = self.db.memory
        call = ToolCall(name="read_file", arguments={"path": "a.txt"}, id="call_1")
        messages = [
            Message.system("system prompt", tag="boot"),
            Message.user("hello from user", channel="console"),
            Message.assistant("calling read_file", tool_calls=[call]),
            Message.tool("file contents", call_id="call_1", name="read_file"),
        ]
        session = Session(
            id=new_id("session"),
            kind=SessionKind.WORKER,
            title="Worker session",
            goal="read a file",
            status=SessionStatus.RUNNING,
            thread_ids=["thread_demo"],
            references=["thread:thread_demo", "trigger:trigger_demo"],
            parent_id="session_router",
            steps=2,
            metadata={"nested": {"a": [1, True, None]}, "label": "测试"},
            messages=messages,
        )
        await store.save_session(session)

        restored = await store.get_session(session.id)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.kind, SessionKind.WORKER)
        self.assertEqual(restored.status, SessionStatus.RUNNING)
        self.assertEqual(restored.thread_ids, ["thread_demo"])
        self.assertEqual(restored.references, ["thread:thread_demo", "trigger:trigger_demo"])
        self.assertEqual(restored.parent_id, "session_router")
        self.assertEqual(restored.steps, 2)
        self.assertEqual(restored.metadata, {"nested": {"a": [1, True, None]}, "label": "测试"})
        self.assertEqual(len(restored.messages), 4)
        self.assertEqual([item.role for item in restored.messages], ["system", "user", "assistant", "tool"])
        self.assertEqual(restored.messages[0].metadata, {"tag": "boot"})
        self.assertEqual(restored.messages[2].tool_calls[0].name, "read_file")
        self.assertEqual(restored.messages[2].tool_calls[0].arguments, {"path": "a.txt"})
        self.assertEqual(restored.messages[2].tool_calls[0].id, "call_1")
        self.assertEqual(restored.messages[3].tool_call_id, "call_1")
        self.assertEqual(restored.messages[3].name, "read_file")

        listed_sessions = await store.list_sessions(kind="worker")
        self.assertEqual([item.id for item in listed_sessions], [session.id])

        page_one = await store.list_messages(session.id, limit=2, offset=0)
        self.assertEqual([item.content for item in page_one], ["system prompt", "hello from user"])
        page_two = await store.list_messages(session.id, limit=2, offset=2)
        self.assertEqual([item.role for item in page_two], ["assistant", "tool"])
        tail = await store.list_messages(session.id, limit=10, offset=3)
        self.assertEqual(len(tail), 1)
        self.assertEqual(tail[0].role, "tool")

        appended = Message.user("appended message")
        await store.append_message(session.id, appended)
        all_messages = await store.list_messages(session.id, limit=100)
        self.assertEqual(len(all_messages), 5)
        self.assertEqual(all_messages[-1].content, "appended message")
        refreshed = await store.get_session(session.id)
        self.assertEqual(len(refreshed.messages), 5)

        # Reopen keeps the full history and pagination order.
        store = self._reopen().memory
        reopened = await store.get_session(session.id)
        self.assertEqual(len(reopened.messages), 5)
        self.assertEqual(reopened.messages[-1].content, "appended message")
        page = await store.list_messages(session.id, limit=2, offset=2)
        self.assertEqual([item.role for item in page], ["assistant", "tool"])

    # ------------------------------------------------------------------
    # Chain of recall
    # ------------------------------------------------------------------
    async def test_context_references_and_snapshot(self):
        store = self.db.memory
        thread = Thread(
            id="thread_ctx",
            source="console",
            channel="console",
            title="Context thread",
            participants=["alice"],
            references=["trigger:trig_ctx"],
            metadata={"key": "thread"},
        )
        trigger = Trigger(
            text="context event",
            id="trig_ctx",
            source="console",
            channel="console",
            actor="alice",
            thread_id=thread.id,
            references=["artifact:art_ctx"],
            metadata={"key": "trigger"},
        )
        artifact = Artifact(
            id="art_ctx",
            kind="note",
            summary="spilled notes",
            content="long content",
            references=[],
            metadata={"key": "artifact"},
        )
        session = Session(
            id="session_ctx",
            kind=SessionKind.ROUTER,
            goal="resolve context",
            references=["thread:thread_ctx"],
        )
        await store.save_thread(thread)
        await store.save_trigger(trigger)
        await store.save_artifact(artifact)
        await store.save_session(session)
        await store.save_subscription(
            ThreadSubscription(worker_id="worker_1", thread_id=thread.id, session_id=session.id)
        )

        thread_ref = await store.resolve_reference("thread:thread_ctx")
        self.assertIsNotNone(thread_ref)
        self.assertEqual(thread_ref.kind, "thread")
        self.assertEqual(thread_ref.id, thread.id)
        self.assertEqual(thread_ref.references, ["trigger:trig_ctx"])

        trigger_ref = await store.resolve_reference("trigger:trig_ctx")
        self.assertIsNotNone(trigger_ref)
        self.assertEqual(trigger_ref.kind, "trigger")
        self.assertEqual(trigger_ref.id, trigger.id)
        self.assertEqual(trigger_ref.references, ["artifact:art_ctx"])

        artifact_ref = await store.resolve_reference("artifact:art_ctx")
        self.assertIsNotNone(artifact_ref)
        self.assertEqual(artifact_ref.kind, "note")
        self.assertEqual(artifact_ref.id, artifact.id)
        self.assertEqual(artifact_ref.summary, "spilled notes")
        self.assertEqual(artifact_ref.references, [])

        session_ref = await store.resolve_reference("session:session_ctx")
        self.assertIsNotNone(session_ref)
        self.assertEqual(session_ref.kind, "session")
        self.assertEqual(session_ref.id, session.id)
        self.assertEqual(session_ref.references, ["thread:thread_ctx"])

        self.assertIsNone(await store.resolve_reference("thread:missing"))
        self.assertIsNone(await store.resolve_reference("unknown:session_ctx"))
        self.assertIsNone(await store.resolve_reference("no-prefix"))

        snapshot = store.snapshot()
        self.assertEqual(snapshot["sessions"], 1)
        self.assertEqual(snapshot["threads"], 1)
        self.assertEqual(snapshot["triggers"], 1)
        self.assertEqual(snapshot["artifacts"], 1)
        self.assertEqual(snapshot["subscriptions"], 1)

    # ------------------------------------------------------------------
    # Persistence across every domain
    # ------------------------------------------------------------------
    async def test_all_domains_persist_across_reopen(self):
        memory = self.db.memory
        session = Session(
            id="session_persist",
            kind=SessionKind.WATCHDOG,
            status=SessionStatus.CLOSED,
            goal="check",
            messages=[Message.user("persisted"), Message.assistant("done")],
        )
        thread = Thread(id="thread_persist", source="web", channel="web", title="T")
        trigger = Trigger(text="event", id="trigger_persist", thread_id=thread.id)
        artifact = Artifact(id="artifact_persist", kind="log", summary="s", content="c")
        await memory.save_session(session)
        await memory.save_thread(thread)
        await memory.save_trigger(trigger)
        await memory.save_artifact(artifact)

        model = await self.db.model_configs.save_model_config(
            ModelConfig(name="persist-model", model="m", params={"x": 1}, is_default=True)
        )
        mcp_server = await self.db.mcp.save_server(MCPServerConfig(name="persist-server"))
        await self.db.mcp.upsert_entry(
            MCPEntry(server_id=mcp_server.id, kind="tool", name="t", payload={"p": True})
        )
        skill = await self.db.skills.save_skill(
            SkillConfig(name="persist-skill", tags=["persisted"])
        )

        memory = self._reopen().memory
        self.assertEqual((await memory.get_session(session.id)).messages[0].content, "persisted")
        self.assertEqual((await memory.get_thread(thread.id)).title, "T")
        self.assertEqual((await memory.get_trigger(trigger.id)).text, "event")
        self.assertEqual((await memory.get_artifact(artifact.id)).content, "c")

        self.assertEqual(
            (await self.db.model_configs.get_model_config(model.id)).params, {"x": 1}
        )
        self.assertEqual((await self.db.model_configs.get_default_model()).id, model.id)
        self.assertEqual((await self.db.mcp.get_server(mcp_server.id)).name, "persist-server")
        entries = await self.db.mcp.list_entries(mcp_server.id)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].payload, {"p": True})
        self.assertEqual((await self.db.skills.get_skill(skill.id)).tags, ["persisted"])

        snapshot = memory.snapshot()
        self.assertEqual(snapshot["sessions"], 1)
        self.assertEqual(snapshot["threads"], 1)
        self.assertEqual(snapshot["triggers"], 1)
        self.assertEqual(snapshot["artifacts"], 1)


if __name__ == "__main__":
    unittest.main()
