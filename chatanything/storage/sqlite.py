"""SQLite persistence layer.

A single :class:`sqlite3.Connection` is shared by four sub-stores:

* :class:`SqliteMemoryStore`        -- sessions, messages, threads, triggers,
  subscriptions and artifacts;
* :class:`SqliteModelConfigStore`   -- model backend configurations;
* :class:`SqliteMCPStore`           -- MCP servers and their discovered entries;
* :class:`SqliteSkillRegistry`      -- skill definitions.

Only the Python standard library is used.  All public store methods are
``async`` but execute synchronously under one re-entrant lock; the harness
itself is single-threaded asyncio code, so this keeps SQLite access simple and
correct without introducing an asyncio lock.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import replace
from typing import Any, ContextManager, Literal, Sequence, cast

from ..models import (
    Artifact,
    ContextReference,
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
    utc_now,
)


_Role = Literal["system", "user", "assistant", "tool"]

_SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY,
        kind TEXT,
        title TEXT DEFAULT '',
        goal TEXT DEFAULT '',
        status TEXT,
        thread_ids TEXT DEFAULT '[]',
        references_json TEXT DEFAULT '[]',
        parent_id TEXT,
        created_at REAL,
        updated_at REAL,
        last_activity REAL,
        steps INTEGER DEFAULT 0,
        metadata TEXT DEFAULT '{}'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sessions_updated_at ON sessions(updated_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS messages (
        session_id TEXT NOT NULL,
        position INTEGER NOT NULL,
        role TEXT,
        content TEXT DEFAULT '',
        name TEXT,
        tool_call_id TEXT,
        tool_calls TEXT DEFAULT '[]',
        created_at REAL,
        metadata TEXT DEFAULT '{}',
        PRIMARY KEY (session_id, position),
        FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS threads (
        id TEXT PRIMARY KEY,
        source TEXT,
        channel TEXT,
        title TEXT DEFAULT '',
        participants TEXT DEFAULT '[]',
        created_at REAL,
        updated_at REAL,
        references_json TEXT DEFAULT '[]',
        metadata TEXT DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS triggers (
        id TEXT PRIMARY KEY,
        text TEXT,
        source TEXT,
        channel TEXT,
        actor TEXT,
        thread_id TEXT,
        event_type TEXT,
        mentions TEXT DEFAULT '[]',
        metadata TEXT DEFAULT '{}',
        timestamp REAL,
        references_json TEXT DEFAULT '[]',
        priority INTEGER DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS subscriptions (
        worker_id TEXT NOT NULL,
        thread_id TEXT NOT NULL,
        session_id TEXT,
        created_at REAL,
        metadata TEXT DEFAULT '{}',
        PRIMARY KEY (worker_id, thread_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS artifacts (
        id TEXT PRIMARY KEY,
        kind TEXT,
        summary TEXT,
        content TEXT,
        references_json TEXT DEFAULT '[]',
        created_at REAL,
        metadata TEXT DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS model_configs (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        provider TEXT,
        model TEXT,
        base_url TEXT,
        api_key TEXT,
        params TEXT DEFAULT '{}',
        enabled INTEGER DEFAULT 1,
        is_default INTEGER DEFAULT 0,
        created_at REAL,
        updated_at REAL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mcp_servers (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        transport TEXT,
        command TEXT DEFAULT '',
        url TEXT DEFAULT '',
        args TEXT DEFAULT '[]',
        env TEXT DEFAULT '{}',
        enabled INTEGER DEFAULT 1,
        created_at REAL,
        updated_at REAL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mcp_entries (
        id TEXT PRIMARY KEY,
        server_id TEXT NOT NULL,
        kind TEXT,
        name TEXT,
        payload TEXT DEFAULT '{}',
        updated_at REAL,
        UNIQUE (server_id, kind, name),
        FOREIGN KEY (server_id) REFERENCES mcp_servers(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS skills (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        description TEXT DEFAULT '',
        path TEXT DEFAULT '',
        version TEXT DEFAULT '0.1.0',
        enabled INTEGER DEFAULT 1,
        tags TEXT DEFAULT '[]',
        metadata TEXT DEFAULT '{}',
        created_at REAL,
        updated_at REAL
    )
    """,
)


# -------------------------------------------------------------------------#
# JSON / row helpers
# -------------------------------------------------------------------------#


def _dumps(value: Any) -> str:
    """Serialize a JSON-compatible value without escaping non-ASCII text."""

    return json.dumps(value, ensure_ascii=False)


def _loads(text: str | None, default: Any) -> Any:
    """Deserialize JSON, returning ``default`` for empty or broken payloads."""

    if not text:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def _load_object(text: str | None) -> dict[str, Any]:
    value = _loads(text, {})
    return value if isinstance(value, dict) else {}


def _load_list(text: str | None) -> list[Any]:
    value = _loads(text, [])
    return value if isinstance(value, list) else []


def _enum_value(value: Any) -> Any:
    """Return the plain value of an enum member, or the value itself."""

    return getattr(value, "value", value)


def _as_float(value: Any, default: float = 0.0) -> float:
    return float(value) if value is not None else default


def _as_int(value: Any, default: int = 0) -> int:
    return int(value) if value is not None else default


def _tool_calls_to_json(tool_calls: Sequence[ToolCall]) -> str:
    return _dumps(
        [
            {"name": call.name, "arguments": call.arguments, "id": call.id}
            for call in tool_calls
        ]
    )


def _message_from_row(row: sqlite3.Row) -> Message:
    payload = _load_list(row["tool_calls"])
    tool_calls: list[ToolCall] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        call = ToolCall(
            name=str(item.get("name", "")),
            arguments=dict(item.get("arguments") or {}),
        )
        if item.get("id"):
            call.id = str(item["id"])
        tool_calls.append(call)
    return Message(
        role=cast(_Role, row["role"]),
        content=row["content"] or "",
        name=row["name"],
        tool_call_id=row["tool_call_id"],
        tool_calls=tool_calls,
        created_at=_as_float(row["created_at"]),
        metadata=_load_object(row["metadata"]),
    )


def _session_from_row(row: sqlite3.Row) -> Session:
    return Session(
        id=str(row["id"]),
        kind=SessionKind(row["kind"]),
        title=row["title"] or "",
        goal=row["goal"] or "",
        status=SessionStatus(row["status"]),
        thread_ids=[str(item) for item in _load_list(row["thread_ids"])],
        references=[str(item) for item in _load_list(row["references_json"])],
        parent_id=row["parent_id"],
        created_at=_as_float(row["created_at"]),
        updated_at=_as_float(row["updated_at"]),
        last_activity=_as_float(row["last_activity"]),
        steps=_as_int(row["steps"]),
        metadata=_load_object(row["metadata"]),
    )


def _thread_from_row(row: sqlite3.Row) -> Thread:
    return Thread(
        id=str(row["id"]),
        source=row["source"] or "",
        channel=row["channel"] or "",
        title=row["title"] or "",
        participants=[str(item) for item in _load_list(row["participants"])],
        created_at=_as_float(row["created_at"]),
        updated_at=_as_float(row["updated_at"]),
        references=[str(item) for item in _load_list(row["references_json"])],
        metadata=_load_object(row["metadata"]),
    )


def _trigger_from_row(row: sqlite3.Row) -> Trigger:
    return Trigger(
        text=row["text"] or "",
        source=row["source"] or "",
        channel=row["channel"] or "",
        actor=row["actor"],
        thread_id=row["thread_id"],
        event_type=row["event_type"] or "",
        mentions=tuple(str(item) for item in _load_list(row["mentions"])),
        metadata=_load_object(row["metadata"]),
        id=str(row["id"]),
        timestamp=_as_float(row["timestamp"]),
        references=[str(item) for item in _load_list(row["references_json"])],
        priority=_as_int(row["priority"]),
    )


def _subscription_from_row(row: sqlite3.Row) -> ThreadSubscription:
    return ThreadSubscription(
        worker_id=str(row["worker_id"]),
        thread_id=str(row["thread_id"]),
        session_id=row["session_id"] or "",
        created_at=_as_float(row["created_at"]),
        metadata=_load_object(row["metadata"]),
    )


def _artifact_from_row(row: sqlite3.Row) -> Artifact:
    return Artifact(
        id=str(row["id"]),
        kind=row["kind"] or "",
        summary=row["summary"] or "",
        content=row["content"] or "",
        references=[str(item) for item in _load_list(row["references_json"])],
        created_at=_as_float(row["created_at"]),
        metadata=_load_object(row["metadata"]),
    )


def _model_config_from_row(row: sqlite3.Row) -> ModelConfig:
    return ModelConfig(
        name=row["name"],
        provider=row["provider"] or "",
        model=row["model"] or "",
        base_url=row["base_url"] or "",
        api_key=row["api_key"] or "",
        params=_load_object(row["params"]),
        enabled=bool(row["enabled"]),
        is_default=bool(row["is_default"]),
        id=str(row["id"]),
        created_at=_as_float(row["created_at"]),
        updated_at=_as_float(row["updated_at"]),
    )


def _mcp_server_from_row(row: sqlite3.Row) -> MCPServerConfig:
    return MCPServerConfig(
        name=row["name"],
        transport=row["transport"] or "",
        command=row["command"] or "",
        url=row["url"] or "",
        args=[str(item) for item in _load_list(row["args"])],
        env={str(key): str(value) for key, value in _load_object(row["env"]).items()},
        enabled=bool(row["enabled"]),
        id=str(row["id"]),
        created_at=_as_float(row["created_at"]),
        updated_at=_as_float(row["updated_at"]),
    )


def _mcp_entry_from_row(row: sqlite3.Row) -> MCPEntry:
    return MCPEntry(
        server_id=str(row["server_id"]),
        kind=row["kind"] or "",
        name=row["name"] or "",
        payload=_load_object(row["payload"]),
        id=str(row["id"]),
        updated_at=_as_float(row["updated_at"]),
    )


def _skill_from_row(row: sqlite3.Row) -> SkillConfig:
    return SkillConfig(
        name=row["name"],
        description=row["description"] or "",
        path=row["path"] or "",
        version=row["version"] or "0.1.0",
        enabled=bool(row["enabled"]),
        tags=[str(item) for item in _load_list(row["tags"])],
        metadata=_load_object(row["metadata"]),
        id=str(row["id"]),
        created_at=_as_float(row["created_at"]),
        updated_at=_as_float(row["updated_at"]),
    )


# -------------------------------------------------------------------------#
# Database
# -------------------------------------------------------------------------#


class SqliteDatabase:
    """Owns one connection plus the four stores bound to it.

    Use :meth:`initialize` once to create the schema, and :meth:`close` when
    the owning plugin is uninstalled.  ``close`` is idempotent.
    """

    def __init__(
        self,
        path: str,
        *,
        busy_timeout: float = 5.0,
        journal_mode: str = "WAL",
    ) -> None:
        self.path = path
        self.busy_timeout = busy_timeout
        self.journal_mode = journal_mode
        self._lock: ContextManager[bool] = threading.RLock()
        self._connection: sqlite3.Connection | None = None
        self.memory = SqliteMemoryStore(self)
        self.model_configs = SqliteModelConfigStore(self)
        self.mcp = SqliteMCPStore(self)
        self.skills = SqliteSkillRegistry(self)

    @property
    def lock(self) -> ContextManager[bool]:
        """The re-entrant lock guarding every database access."""

        return self._lock

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("SqliteDatabase.initialize() must be called before use")
        return self._connection

    def initialize(self) -> None:
        """Open the connection, apply pragmas and create tables (idempotent)."""

        with self._lock:
            if self._connection is not None:
                return
            connection = sqlite3.connect(
                self.path,
                check_same_thread=False,
                timeout=self.busy_timeout,
            )
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute(
                    f"PRAGMA busy_timeout = {int(self.busy_timeout * 1000)}"
                )
                mode = self.journal_mode.strip().upper()
                if self.path != ":memory:" and mode.isalnum():
                    try:
                        connection.execute(f"PRAGMA journal_mode = {mode}")
                    except sqlite3.DatabaseError:
                        pass
                for statement in _SCHEMA:
                    connection.execute(statement)
                connection.commit()
            except Exception:
                connection.close()
                raise
            self._connection = connection

    def close(self) -> None:
        """Commit and close the connection; safe to call repeatedly."""

        with self._lock:
            if self._connection is None:
                return
            connection = self._connection
            self._connection = None
            try:
                connection.commit()
            except sqlite3.DatabaseError:
                pass
            connection.close()

    def execute(
        self, sql: str, parameters: Sequence[Any] = ()
    ) -> sqlite3.Cursor:
        with self._lock:
            return self.connection.execute(sql, tuple(parameters))

    def fetchone(
        self, sql: str, parameters: Sequence[Any] = ()
    ) -> sqlite3.Row | None:
        with self._lock:
            cursor = self.connection.execute(sql, tuple(parameters))
            try:
                return cursor.fetchone()
            finally:
                cursor.close()

    def fetchall(
        self, sql: str, parameters: Sequence[Any] = ()
    ) -> list[sqlite3.Row]:
        with self._lock:
            cursor = self.connection.execute(sql, tuple(parameters))
            try:
                return list(cursor.fetchall())
            finally:
                cursor.close()

    def commit(self) -> None:
        with self._lock:
            self.connection.commit()


# -------------------------------------------------------------------------#
# Memory store
# -------------------------------------------------------------------------#


class SqliteMemoryStore:
    """Durable :class:`~chatanything.plugins.interfaces.MemoryStore`."""

    name = "sqlite"

    def __init__(self, database: SqliteDatabase) -> None:
        self._db = database

    # -- sessions -----------------------------------------------------------

    async def save_session(self, session: Session) -> None:
        db = self._db
        with db.lock:
            db.execute(
                """
                INSERT INTO sessions (
                    id, kind, title, goal, status, thread_ids, references_json,
                    parent_id, created_at, updated_at, last_activity, steps, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    kind = excluded.kind,
                    title = excluded.title,
                    goal = excluded.goal,
                    status = excluded.status,
                    thread_ids = excluded.thread_ids,
                    references_json = excluded.references_json,
                    parent_id = excluded.parent_id,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at,
                    last_activity = excluded.last_activity,
                    steps = excluded.steps,
                    metadata = excluded.metadata
                """,
                (
                    session.id,
                    session.kind.value,
                    session.title,
                    session.goal,
                    session.status.value,
                    _dumps(list(session.thread_ids)),
                    _dumps(list(session.references)),
                    session.parent_id,
                    session.created_at,
                    session.updated_at,
                    session.last_activity,
                    session.steps,
                    _dumps(session.metadata),
                ),
            )
            db.execute(
                "DELETE FROM messages WHERE session_id = ? AND position >= ?",
                (session.id, len(session.messages)),
            )
            for position, message in enumerate(session.messages):
                db.execute(
                    """
                    INSERT OR REPLACE INTO messages (
                        session_id, position, role, content, name, tool_call_id,
                        tool_calls, created_at, metadata
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session.id,
                        position,
                        message.role,
                        message.content,
                        message.name,
                        message.tool_call_id,
                        _tool_calls_to_json(message.tool_calls),
                        message.created_at,
                        _dumps(message.metadata),
                    ),
                )
            db.commit()

    async def get_session(self, session_id: str) -> Session | None:
        db = self._db
        with db.lock:
            row = db.fetchone("SELECT * FROM sessions WHERE id = ?", (session_id,))
            if row is None:
                return None
            session = _session_from_row(row)
            session.messages = self._load_messages(session_id)
            return session

    async def list_sessions(
        self,
        *,
        kind: str | None = None,
        status: str | None = None,
    ) -> list[Session]:
        db = self._db
        with db.lock:
            clauses: list[str] = []
            parameters: list[Any] = []
            if kind is not None:
                clauses.append("kind = ?")
                parameters.append(_enum_value(kind))
            if status is not None:
                clauses.append("status = ?")
                parameters.append(_enum_value(status))
            sql = "SELECT * FROM sessions"
            if clauses:
                sql += " WHERE " + " AND ".join(clauses)
            sql += " ORDER BY updated_at DESC"
            rows = db.fetchall(sql, tuple(parameters))
            sessions: list[Session] = []
            for row in rows:
                session = _session_from_row(row)
                session.messages = self._load_messages(session.id)
                sessions.append(session)
            return sessions

    async def list_messages(
        self, session_id: str, *, limit: int = 100, offset: int = 0
    ) -> list[Message]:
        db = self._db
        with db.lock:
            rows = db.fetchall(
                """
                SELECT * FROM messages
                WHERE session_id = ?
                ORDER BY position ASC
                LIMIT ? OFFSET ?
                """,
                (session_id, max(0, limit), max(0, offset)),
            )
            return [_message_from_row(row) for row in rows]

    async def append_message(self, session_id: str, message: Message) -> None:
        db = self._db
        with db.lock:
            if db.fetchone("SELECT 1 FROM sessions WHERE id = ?", (session_id,)) is None:
                return
            count_row = db.fetchone(
                "SELECT COUNT(*) AS total FROM messages WHERE session_id = ?",
                (session_id,),
            )
            position = _as_int(count_row["total"]) if count_row is not None else 0
            db.execute(
                """
                INSERT OR REPLACE INTO messages (
                    session_id, position, role, content, name, tool_call_id,
                    tool_calls, created_at, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    position,
                    message.role,
                    message.content,
                    message.name,
                    message.tool_call_id,
                    _tool_calls_to_json(message.tool_calls),
                    message.created_at,
                    _dumps(message.metadata),
                ),
            )
            activity = message.created_at if message.created_at else utc_now()
            db.execute(
                "UPDATE sessions SET updated_at = ?, last_activity = ? WHERE id = ?",
                (activity, activity, session_id),
            )
            db.commit()

    def _load_messages(self, session_id: str) -> list[Message]:
        rows = self._db.fetchall(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY position ASC",
            (session_id,),
        )
        return [_message_from_row(row) for row in rows]

    # -- threads ------------------------------------------------------------

    async def save_thread(self, thread: Thread) -> None:
        db = self._db
        with db.lock:
            db.execute(
                """
                INSERT OR REPLACE INTO threads (
                    id, source, channel, title, participants, created_at,
                    updated_at, references_json, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    thread.id,
                    thread.source,
                    thread.channel,
                    thread.title,
                    _dumps(list(thread.participants)),
                    thread.created_at,
                    thread.updated_at,
                    _dumps(list(thread.references)),
                    _dumps(thread.metadata),
                ),
            )
            db.commit()

    async def get_thread(self, thread_id: str) -> Thread | None:
        db = self._db
        with db.lock:
            row = db.fetchone("SELECT * FROM threads WHERE id = ?", (thread_id,))
            return _thread_from_row(row) if row is not None else None

    async def list_threads(self) -> list[Thread]:
        db = self._db
        with db.lock:
            rows = db.fetchall("SELECT * FROM threads ORDER BY created_at ASC")
            return [_thread_from_row(row) for row in rows]

    async def remove_thread(self, thread_id: str) -> None:
        db = self._db
        with db.lock:
            db.execute("DELETE FROM threads WHERE id = ?", (thread_id,))
            db.commit()

    # -- triggers -----------------------------------------------------------

    async def save_trigger(self, trigger: Trigger) -> None:
        db = self._db
        with db.lock:
            db.execute(
                """
                INSERT OR REPLACE INTO triggers (
                    id, text, source, channel, actor, thread_id, event_type,
                    mentions, metadata, timestamp, references_json, priority
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trigger.id,
                    trigger.text,
                    trigger.source,
                    trigger.channel,
                    trigger.actor,
                    trigger.thread_id,
                    trigger.event_type,
                    _dumps(list(trigger.mentions)),
                    _dumps(trigger.metadata),
                    trigger.timestamp,
                    _dumps(list(trigger.references)),
                    trigger.priority,
                ),
            )
            db.commit()

    async def get_trigger(self, trigger_id: str) -> Trigger | None:
        db = self._db
        with db.lock:
            row = db.fetchone("SELECT * FROM triggers WHERE id = ?", (trigger_id,))
            return _trigger_from_row(row) if row is not None else None

    async def list_triggers(self, *, thread_id: str | None = None) -> list[Trigger]:
        db = self._db
        with db.lock:
            if thread_id is None:
                rows = db.fetchall("SELECT * FROM triggers ORDER BY timestamp ASC")
            else:
                rows = db.fetchall(
                    "SELECT * FROM triggers WHERE thread_id = ? ORDER BY timestamp ASC",
                    (thread_id,),
                )
            return [_trigger_from_row(row) for row in rows]

    async def remove_trigger(self, trigger_id: str) -> None:
        db = self._db
        with db.lock:
            db.execute("DELETE FROM triggers WHERE id = ?", (trigger_id,))
            db.commit()

    # -- subscriptions ------------------------------------------------------

    async def save_subscription(self, subscription: ThreadSubscription) -> None:
        db = self._db
        with db.lock:
            db.execute(
                """
                INSERT OR REPLACE INTO subscriptions (
                    worker_id, thread_id, session_id, created_at, metadata
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    subscription.worker_id,
                    subscription.thread_id,
                    subscription.session_id,
                    subscription.created_at,
                    _dumps(subscription.metadata),
                ),
            )
            db.commit()

    async def list_subscriptions(
        self,
        *,
        thread_id: str | None = None,
        worker_id: str | None = None,
    ) -> list[ThreadSubscription]:
        db = self._db
        with db.lock:
            clauses: list[str] = []
            parameters: list[Any] = []
            if thread_id is not None:
                clauses.append("thread_id = ?")
                parameters.append(thread_id)
            if worker_id is not None:
                clauses.append("worker_id = ?")
                parameters.append(worker_id)
            sql = "SELECT * FROM subscriptions"
            if clauses:
                sql += " WHERE " + " AND ".join(clauses)
            sql += " ORDER BY created_at ASC"
            rows = db.fetchall(sql, tuple(parameters))
            return [_subscription_from_row(row) for row in rows]

    async def remove_subscription(self, worker_id: str, thread_id: str) -> None:
        db = self._db
        with db.lock:
            db.execute(
                "DELETE FROM subscriptions WHERE worker_id = ? AND thread_id = ?",
                (worker_id, thread_id),
            )
            db.commit()

    # -- artifacts ----------------------------------------------------------

    async def save_artifact(self, artifact: Artifact) -> None:
        db = self._db
        with db.lock:
            db.execute(
                """
                INSERT OR REPLACE INTO artifacts (
                    id, kind, summary, content, references_json, created_at, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact.id,
                    artifact.kind,
                    artifact.summary,
                    artifact.content,
                    _dumps(list(artifact.references)),
                    artifact.created_at,
                    _dumps(artifact.metadata),
                ),
            )
            db.commit()

    async def get_artifact(self, artifact_id: str) -> Artifact | None:
        db = self._db
        with db.lock:
            row = db.fetchone("SELECT * FROM artifacts WHERE id = ?", (artifact_id,))
            return _artifact_from_row(row) if row is not None else None

    async def list_artifacts(self, *, kind: str | None = None) -> list[Artifact]:
        db = self._db
        with db.lock:
            if kind is None:
                rows = db.fetchall("SELECT * FROM artifacts ORDER BY created_at ASC")
            else:
                rows = db.fetchall(
                    "SELECT * FROM artifacts WHERE kind = ? ORDER BY created_at ASC",
                    (kind,),
                )
            return [_artifact_from_row(row) for row in rows]

    async def remove_artifact(self, artifact_id: str) -> None:
        db = self._db
        with db.lock:
            db.execute("DELETE FROM artifacts WHERE id = ?", (artifact_id,))
            db.commit()

    # -- chain of recall ----------------------------------------------------

    async def resolve_reference(self, reference_id: str) -> ContextReference | None:
        """Resolve ``session:...`` / ``thread:...`` / ``trigger:...`` / ``artifact:...``."""

        kind, _, raw_id = reference_id.partition(":")
        if not raw_id:
            return None

        if kind == "trigger":
            trigger = await self.get_trigger(raw_id)
            if trigger is None:
                return None
            return ContextReference(
                id=trigger.id,
                kind="trigger",
                summary=f"{trigger.brief(120)} ({trigger.timestamp:.0f})",
                references=list(trigger.references),
            )

        if kind == "thread":
            thread = await self.get_thread(raw_id)
            if thread is None:
                return None
            return ContextReference(
                id=thread.id,
                kind="thread",
                summary=(
                    f"thread '{thread.title}' source={thread.source} "
                    f"channel={thread.channel} participants={','.join(thread.participants)}"
                ),
                references=list(thread.references),
            )

        if kind == "session":
            session = await self.get_session(raw_id)
            if session is None:
                return None
            return ContextReference(
                id=session.id,
                kind="session",
                summary=(
                    f"session {session.kind.value}/{session.status.value} "
                    f"goal={session.goal[:100]}"
                ),
                references=list(session.references),
            )

        if kind == "artifact":
            artifact = await self.get_artifact(raw_id)
            if artifact is None:
                return None
            return ContextReference(
                id=artifact.id,
                kind=artifact.kind,
                summary=artifact.summary,
                references=list(artifact.references),
            )

        return None

    def snapshot(self) -> dict[str, Any]:
        """Row counts for dashboards and watchdogs (synchronous by design)."""

        db = self._db

        def count(table: str) -> int:
            row = db.fetchone(f"SELECT COUNT(*) AS total FROM {table}")
            return _as_int(row["total"]) if row is not None else 0

        with db.lock:
            return {
                "sessions": count("sessions"),
                "threads": count("threads"),
                "triggers": count("triggers"),
                "artifacts": count("artifacts"),
                "subscriptions": count("subscriptions"),
            }


# -------------------------------------------------------------------------#
# Model config store
# -------------------------------------------------------------------------#


class SqliteModelConfigStore:
    """Durable :class:`~chatanything.plugins.interfaces.ModelConfigStore`."""

    name = "sqlite"

    def __init__(self, database: SqliteDatabase) -> None:
        self._db = database

    async def save_model_config(self, config: ModelConfig) -> ModelConfig:
        db = self._db
        with db.lock:
            clash = db.fetchone(
                "SELECT id FROM model_configs WHERE name = ?", (config.name,)
            )
            if clash is not None and str(clash["id"]) != config.id:
                raise ValueError(f"model config name already exists: {config.name}")
            existing = db.fetchone(
                "SELECT created_at FROM model_configs WHERE id = ?", (config.id,)
            )
            now = utc_now()
            created_at = (
                _as_float(existing["created_at"], config.created_at)
                if existing is not None
                else config.created_at
            )
            if config.is_default:
                db.execute(
                    "UPDATE model_configs SET is_default = 0 WHERE id <> ?",
                    (config.id,),
                )
            db.execute(
                """
                INSERT INTO model_configs (
                    id, name, provider, model, base_url, api_key, params,
                    enabled, is_default, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    provider = excluded.provider,
                    model = excluded.model,
                    base_url = excluded.base_url,
                    api_key = excluded.api_key,
                    params = excluded.params,
                    enabled = excluded.enabled,
                    is_default = excluded.is_default,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at
                """,
                (
                    config.id,
                    config.name,
                    config.provider,
                    config.model,
                    config.base_url,
                    config.api_key,
                    _dumps(config.params),
                    int(config.enabled),
                    int(config.is_default),
                    created_at,
                    now,
                ),
            )
            db.commit()
            return replace(config, created_at=created_at, updated_at=now)

    async def get_model_config(self, config_id: str) -> ModelConfig | None:
        db = self._db
        with db.lock:
            row = db.fetchone(
                "SELECT * FROM model_configs WHERE id = ?", (config_id,)
            )
            return _model_config_from_row(row) if row is not None else None

    async def list_model_configs(
        self, *, enabled: bool | None = None
    ) -> list[ModelConfig]:
        db = self._db
        with db.lock:
            if enabled is None:
                rows = db.fetchall(
                    "SELECT * FROM model_configs ORDER BY created_at ASC"
                )
            else:
                rows = db.fetchall(
                    "SELECT * FROM model_configs WHERE enabled = ? "
                    "ORDER BY created_at ASC",
                    (int(enabled),),
                )
            return [_model_config_from_row(row) for row in rows]

    async def delete_model_config(self, config_id: str) -> bool:
        db = self._db
        with db.lock:
            cursor = db.execute(
                "DELETE FROM model_configs WHERE id = ?", (config_id,)
            )
            deleted = cursor.rowcount > 0
            db.commit()
            return deleted

    async def set_default_model(self, config_id: str) -> None:
        db = self._db
        with db.lock:
            if db.fetchone(
                "SELECT 1 FROM model_configs WHERE id = ?", (config_id,)
            ) is None:
                raise ValueError(f"model config not found: {config_id}")
            db.execute("UPDATE model_configs SET is_default = 0")
            db.execute(
                "UPDATE model_configs SET is_default = 1, updated_at = ? WHERE id = ?",
                (utc_now(), config_id),
            )
            db.commit()

    async def get_default_model(self) -> ModelConfig | None:
        db = self._db
        with db.lock:
            row = db.fetchone(
                "SELECT * FROM model_configs WHERE is_default = 1 "
                "ORDER BY updated_at DESC LIMIT 1"
            )
            return _model_config_from_row(row) if row is not None else None


# -------------------------------------------------------------------------#
# MCP store
# -------------------------------------------------------------------------#


class SqliteMCPStore:
    """Durable :class:`~chatanything.plugins.interfaces.MCPStore`."""

    name = "sqlite"

    def __init__(self, database: SqliteDatabase) -> None:
        self._db = database

    async def save_server(self, server: MCPServerConfig) -> MCPServerConfig:
        db = self._db
        with db.lock:
            clash = db.fetchone(
                "SELECT id FROM mcp_servers WHERE name = ?", (server.name,)
            )
            if clash is not None and str(clash["id"]) != server.id:
                raise ValueError(f"mcp server name already exists: {server.name}")
            existing = db.fetchone(
                "SELECT created_at FROM mcp_servers WHERE id = ?", (server.id,)
            )
            now = utc_now()
            created_at = (
                _as_float(existing["created_at"], server.created_at)
                if existing is not None
                else server.created_at
            )
            db.execute(
                """
                INSERT INTO mcp_servers (
                    id, name, transport, command, url, args, env, enabled,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    transport = excluded.transport,
                    command = excluded.command,
                    url = excluded.url,
                    args = excluded.args,
                    env = excluded.env,
                    enabled = excluded.enabled,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at
                """,
                (
                    server.id,
                    server.name,
                    server.transport,
                    server.command,
                    server.url,
                    _dumps(list(server.args)),
                    _dumps(dict(server.env)),
                    int(server.enabled),
                    created_at,
                    now,
                ),
            )
            db.commit()
            return replace(server, created_at=created_at, updated_at=now)

    async def get_server(self, server_id: str) -> MCPServerConfig | None:
        db = self._db
        with db.lock:
            row = db.fetchone("SELECT * FROM mcp_servers WHERE id = ?", (server_id,))
            return _mcp_server_from_row(row) if row is not None else None

    async def list_servers(self, *, enabled: bool | None = None) -> list[MCPServerConfig]:
        db = self._db
        with db.lock:
            if enabled is None:
                rows = db.fetchall("SELECT * FROM mcp_servers ORDER BY created_at ASC")
            else:
                rows = db.fetchall(
                    "SELECT * FROM mcp_servers WHERE enabled = ? "
                    "ORDER BY created_at ASC",
                    (int(enabled),),
                )
            return [_mcp_server_from_row(row) for row in rows]

    async def delete_server(self, server_id: str) -> bool:
        db = self._db
        with db.lock:
            cursor = db.execute("DELETE FROM mcp_servers WHERE id = ?", (server_id,))
            deleted = cursor.rowcount > 0
            db.commit()
            return deleted

    async def upsert_entry(self, entry: MCPEntry) -> MCPEntry:
        db = self._db
        with db.lock:
            if db.fetchone(
                "SELECT 1 FROM mcp_servers WHERE id = ?", (entry.server_id,)
            ) is None:
                raise ValueError(f"mcp server not found: {entry.server_id}")
            existing = db.fetchone(
                "SELECT id FROM mcp_entries WHERE server_id = ? AND kind = ? AND name = ?",
                (entry.server_id, entry.kind, entry.name),
            )
            now = utc_now()
            if existing is not None:
                entry_id = str(existing["id"])
                db.execute(
                    "UPDATE mcp_entries SET payload = ?, updated_at = ? WHERE id = ?",
                    (_dumps(entry.payload), now, entry_id),
                )
                result = replace(entry, id=entry_id, updated_at=now)
            else:
                db.execute(
                    """
                    INSERT INTO mcp_entries (
                        id, server_id, kind, name, payload, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        entry.id,
                        entry.server_id,
                        entry.kind,
                        entry.name,
                        _dumps(entry.payload),
                        now,
                    ),
                )
                result = replace(entry, updated_at=now)
            db.commit()
            return result

    async def list_entries(
        self, server_id: str, *, kind: str | None = None
    ) -> list[MCPEntry]:
        db = self._db
        with db.lock:
            if kind is None:
                rows = db.fetchall(
                    "SELECT * FROM mcp_entries WHERE server_id = ? "
                    "ORDER BY kind ASC, name ASC",
                    (server_id,),
                )
            else:
                rows = db.fetchall(
                    "SELECT * FROM mcp_entries WHERE server_id = ? AND kind = ? "
                    "ORDER BY kind ASC, name ASC",
                    (server_id, kind),
                )
            return [_mcp_entry_from_row(row) for row in rows]

    async def delete_entry(self, server_id: str, kind: str, name: str) -> bool:
        db = self._db
        with db.lock:
            cursor = db.execute(
                "DELETE FROM mcp_entries WHERE server_id = ? AND kind = ? AND name = ?",
                (server_id, kind, name),
            )
            deleted = cursor.rowcount > 0
            db.commit()
            return deleted


# -------------------------------------------------------------------------#
# Skill registry
# -------------------------------------------------------------------------#


class SqliteSkillRegistry:
    """Durable :class:`~chatanything.plugins.interfaces.SkillRegistry`."""

    name = "sqlite"

    def __init__(self, database: SqliteDatabase) -> None:
        self._db = database

    async def save_skill(self, skill: SkillConfig) -> SkillConfig:
        db = self._db
        with db.lock:
            clash = db.fetchone(
                "SELECT id FROM skills WHERE name = ?", (skill.name,)
            )
            if clash is not None and str(clash["id"]) != skill.id:
                raise ValueError(f"skill name already exists: {skill.name}")
            existing = db.fetchone(
                "SELECT created_at FROM skills WHERE id = ?", (skill.id,)
            )
            now = utc_now()
            created_at = (
                _as_float(existing["created_at"], skill.created_at)
                if existing is not None
                else skill.created_at
            )
            db.execute(
                """
                INSERT INTO skills (
                    id, name, description, path, version, enabled, tags,
                    metadata, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    description = excluded.description,
                    path = excluded.path,
                    version = excluded.version,
                    enabled = excluded.enabled,
                    tags = excluded.tags,
                    metadata = excluded.metadata,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at
                """,
                (
                    skill.id,
                    skill.name,
                    skill.description,
                    skill.path,
                    skill.version,
                    int(skill.enabled),
                    _dumps(list(skill.tags)),
                    _dumps(skill.metadata),
                    created_at,
                    now,
                ),
            )
            db.commit()
            return replace(skill, created_at=created_at, updated_at=now)

    async def get_skill(self, skill_id: str) -> SkillConfig | None:
        db = self._db
        with db.lock:
            row = db.fetchone("SELECT * FROM skills WHERE id = ?", (skill_id,))
            return _skill_from_row(row) if row is not None else None

    async def list_skills(
        self, *, enabled: bool | None = None, tag: str | None = None
    ) -> list[SkillConfig]:
        db = self._db
        with db.lock:
            if enabled is None:
                rows = db.fetchall("SELECT * FROM skills ORDER BY created_at ASC")
            else:
                rows = db.fetchall(
                    "SELECT * FROM skills WHERE enabled = ? ORDER BY created_at ASC",
                    (int(enabled),),
                )
            skills = [_skill_from_row(row) for row in rows]
        if tag is not None:
            skills = [skill for skill in skills if tag in skill.tags]
        return skills

    async def delete_skill(self, skill_id: str) -> bool:
        db = self._db
        with db.lock:
            cursor = db.execute("DELETE FROM skills WHERE id = ?", (skill_id,))
            deleted = cursor.rowcount > 0
            db.commit()
            return deleted
