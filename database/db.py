import aiosqlite
import json
import time
import logging
from typing import List, Optional, Dict, Any

logger = logging.getLogger(__name__)

# How much of a session's first user message to return as its sidebar title.
# Generous — the sidebar truncates visually — but bounded so a pasted wall of
# text does not travel with every session list.
PREVIEW_CHARS = 120


class DatabaseManager:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def _get_db(self) -> aiosqlite.Connection:
        """Get or create a persistent database connection."""
        if self._db is None:
            self._db = await aiosqlite.connect(self.db_path)
            self._db.row_factory = aiosqlite.Row
            await self._db.execute("PRAGMA foreign_keys = ON;")
            await self._db.execute("PRAGMA journal_mode = WAL;")
            await self._db.execute("PRAGMA synchronous = NORMAL;")
        return self._db

    async def initialize(self):
        """Create the schema if it is not already there.

        There is no migration path: the schema below is the only one this
        codebase has ever written, and a database that does not match it is not
        one this version can read.
        """
        db = await self._get_db()

        await db.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                created_at REAL NOT NULL,
                last_active REAL NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                message_type TEXT NOT NULL CHECK(message_type IN ('user', 'assistant', 'assistant_tool_call', 'tool')),
                role TEXT NOT NULL,
                content TEXT,
                tool_call_id TEXT,
                command TEXT,
                tool_name TEXT,
                timestamp REAL NOT NULL,
                FOREIGN KEY (session_id) REFERENCES sessions (id)
            )
        """)

        # Every message query filters by session_id and orders by
        # (timestamp, id) — history replay, the messages endpoint, and the
        # preview subquery above. Without this each is a full table scan.
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_session
                ON messages (session_id, timestamp, id)
        """)

        await db.commit()

    async def close(self):
        """Close the persistent database connection."""
        if self._db is not None:
            await self._db.close()
            self._db = None

    # ── Session CRUD ────────────────────────────────────────────────────

    async def create_session(self, session_id: str) -> bool:
        """Create a new session entry. Returns True on success."""
        try:
            now = time.time()
            db = await self._get_db()
            await db.execute(
                "INSERT INTO sessions (id, created_at, last_active) VALUES (?, ?, ?)",
                (session_id, now, now)
            )
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            # Session already exists — race condition guard
            logger.debug("Session %s already exists in DB", session_id[:8])
            return False
        except Exception as e:
            logger.error("Failed to create session %s: %s", session_id[:8], e)
            return False

    async def update_session_activity(self, session_id: str) -> bool:
        """Update the last active timestamp for a session."""
        try:
            now = time.time()
            db = await self._get_db()
            await db.execute(
                "UPDATE sessions SET last_active = ? WHERE id = ?",
                (now, session_id)
            )
            await db.commit()
            return True
        except Exception as e:
            logger.error("Failed to update activity for %s: %s", session_id[:8], e)
            return False

    async def destroy_session(self, session_id: str) -> bool:
        """Remove a session and its messages. Returns True if anything was removed."""
        try:
            db = await self._get_db()
            async with db.execute(
                "DELETE FROM messages WHERE session_id = ?", (session_id,)
            ) as cursor:
                msg_deleted = cursor.rowcount
            async with db.execute(
                "DELETE FROM sessions WHERE id = ?", (session_id,)
            ) as cursor:
                sess_deleted = cursor.rowcount
            await db.commit()
            return msg_deleted > 0 or sess_deleted > 0
        except Exception as e:
            logger.error("Failed to destroy session %s: %s", session_id[:8], e)
            return False

    async def clear_session(self, session_id: str) -> bool:
        """Clear messages for a specific session (keep the session row)."""
        try:
            db = await self._get_db()
            async with db.execute(
                "DELETE FROM messages WHERE session_id = ?", (session_id,)
            ) as cursor:
                count = cursor.rowcount
            await db.commit()
            return count > 0
        except Exception as e:
            logger.error("Failed to clear session %s: %s", session_id[:8], e)
            return False

    async def destroy_all_sessions(self) -> int:
        """Remove all sessions and messages. Returns total records destroyed."""
        try:
            db = await self._get_db()
            async with db.execute("SELECT COUNT(*) FROM messages") as cursor:
                msg_count = (await cursor.fetchone())[0]
            async with db.execute("DELETE FROM messages") as cursor:
                pass
            async with db.execute("DELETE FROM sessions") as cursor:
                sess_count = cursor.rowcount
            await db.commit()
            return msg_count + sess_count
        except Exception as e:
            logger.error("Failed to destroy all sessions: %s", e)
            return 0

    async def session_exists(self, session_id: str) -> bool:
        """Check if a session exists in the database."""
        try:
            db = await self._get_db()
            async with db.execute(
                "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
            ) as cursor:
                return (await cursor.fetchone()) is not None
        except Exception as e:
            logger.error("Failed to check session %s: %s", session_id[:8], e)
            return False

    async def get_all_sessions(self) -> List[Dict[str, Any]]:
        """Retrieve all sessions with their timestamps and a title preview.

        The preview is the session's first user message, truncated. It is
        computed here, in one query, because the alternative the sidebar used
        was a full `/sessions/{id}/messages` request per session — which now
        carries every tool result at up to MAX_OUTPUT_CHARS each, i.e. megabytes
        fetched and discarded to render a handful of one-line titles.

        `preview` is None for a session with no user message yet.
        """
        try:
            db = await self._get_db()
            async with db.execute(
                f"""
                SELECT s.id, s.created_at, s.last_active,
                       (SELECT substr(m.content, 1, {PREVIEW_CHARS})
                          FROM messages m
                         WHERE m.session_id = s.id AND m.message_type = 'user'
                         ORDER BY m.timestamp ASC, m.id ASC
                         LIMIT 1) AS preview
                  FROM sessions s
                 ORDER BY s.last_active DESC
                """
            ) as cursor:
                rows = await cursor.fetchall()
                return [
                    {
                        "id": row["id"],
                        "created_at": row["created_at"],
                        "last_active": row["last_active"],
                        "preview": row["preview"],
                    }
                    for row in rows
                ]
        except Exception as e:
            logger.error("Failed to get all sessions: %s", e)
            return []

    async def get_session_timestamps(self, session_id: str) -> Optional[Dict[str, float]]:
        """Retrieve created_at and last_active for a session."""
        try:
            db = await self._get_db()
            async with db.execute(
                "SELECT created_at, last_active FROM sessions WHERE id = ?",
                (session_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    return {
                        "created_at": row["created_at"],
                        "last_active": row["last_active"],
                    }
                return None
        except Exception as e:
            logger.error("Failed to get timestamps for %s: %s", session_id[:8], e)
            return None

    # ── Message CRUD ────────────────────────────────────────────────────

    async def save_message(
        self,
        session_id: str,
        message_type: str,
        role: str,
        content: str = "",
        tool_call_id: Optional[str] = None,
        command: Optional[str] = None,
        tool_name: Optional[str] = None,
    ) -> bool:
        """Save a single message. Returns True on success."""
        try:
            now = time.time()
            db = await self._get_db()
            await db.execute(
                """
                INSERT INTO messages (session_id, message_type, role, content, tool_call_id, command, tool_name, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (session_id, message_type, role, content, tool_call_id, command, tool_name, now),
            )
            await db.commit()
            return True
        except Exception as e:
            logger.error("Failed to save message: %s", e)
            return False

    async def batch_save_messages(
        self,
        session_id: str,
        messages: List[Dict[str, Any]],
    ) -> int:
        """Save multiple messages in a single transaction. Returns count saved."""
        try:
            db = await self._get_db()
            now = time.time()
            rows = []
            for msg in messages:
                message_type = msg.get("message_type", "assistant")
                role = msg["role"]
                content = msg.get("content", "")
                tool_call_id = msg.get("tool_call_id")
                command = msg.get("command")
                tool_name = msg.get("tool_name")
                rows.append((session_id, message_type, role, content, tool_call_id, command, tool_name, now))

            await db.executemany(
                """
                INSERT INTO messages (session_id, message_type, role, content, tool_call_id, command, tool_name, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            await db.commit()
            return len(rows)
        except Exception as e:
            logger.error("Failed to batch save messages: %s", e)
            return 0

    async def get_history(self, session_id: str) -> List[Dict[str, Any]]:
        """Retrieve history for a session, ordered by timestamp.

        Returns OpenAI-format messages suitable for feeding back into the LLM.
        Assistant messages include their tool_calls arrays; tool results are
        separate entries keyed by tool_call_id.

        Ordered by (timestamp, id): a turn is written as one batch and every row
        in it shares a single timestamp, so timestamp alone leaves the order
        within a turn up to the query planner. Getting it wrong puts a tool
        result ahead of the assistant message that requested it, which the API
        rejects outright — the id tiebreak is what keeps the turn intact.
        """
        try:
            db = await self._get_db()
            async with db.execute(
                "SELECT id, message_type, role, content, tool_call_id, command, tool_name, timestamp "
                "FROM messages WHERE session_id = ? ORDER BY timestamp ASC, id ASC",
                (session_id,),
            ) as cursor:
                rows = await cursor.fetchall()

            history: List[Dict[str, Any]] = []
            for row in rows:
                msg_type = row["message_type"]

                if msg_type == "user":
                    history.append({
                        "role": "user",
                        "content": row["content"] or "",
                    })

                elif msg_type == "assistant":
                    # Assistant message — may contain tool_calls or a final text response
                    entry: Dict[str, Any] = {
                        "role": "assistant",
                        "content": row["content"] or "",
                    }
                    if row["content"]:
                        try:
                            parsed = json.loads(row["content"])
                            # Only treat as tool_calls if it's an array of tool call objects
                            if isinstance(parsed, list) and all(
                                isinstance(tc, dict) and "function" in tc for tc in parsed
                            ):
                                entry["tool_calls"] = parsed
                                entry["content"] = ""  # Clear content when we have tool_calls
                        except (json.JSONDecodeError, TypeError):
                            pass  # Keep as plain text content
                    history.append(entry)

                elif msg_type == "assistant_tool_call":
                    # Stored as a JSON-encoded tool_calls array — reconstruct the assistant entry
                    try:
                        tool_calls = json.loads(row["content"])
                        history.append({
                            "role": "assistant",
                            "content": "",
                            "tool_calls": tool_calls,
                        })
                    except (json.JSONDecodeError, TypeError):
                        history.append({
                            "role": "assistant",
                            "content": row["content"] or "",
                        })

                elif msg_type == "tool":
                    history.append({
                        "role": "tool",
                        "tool_call_id": row["tool_call_id"] or "",
                        "content": row["content"] or "",
                    })

            return history

        except Exception as e:
            logger.error("Failed to get history for %s: %s", session_id[:8], e)
            return []

    async def get_session_messages(self, session_id: str) -> List[Dict[str, Any]]:
        """Get a session's full message list with all fields (for the /sessions/<id> endpoint).

        Ordered by (timestamp, id) for the same reason as get_history().
        """
        try:
            db = await self._get_db()
            async with db.execute(
                "SELECT id, message_type, role, content, tool_call_id, command, tool_name, timestamp "
                "FROM messages WHERE session_id = ? ORDER BY timestamp ASC, id ASC",
                (session_id,),
            ) as cursor:
                rows = await cursor.fetchall()

            return [
                {
                    "id": row["id"],
                    "message_type": row["message_type"],
                    "role": row["role"],
                    "content": row["content"],
                    "tool_call_id": row["tool_call_id"],
                    "command": row["command"],
                    "tool_name": row["tool_name"],
                    "timestamp": row["timestamp"],
                }
                for row in rows
            ]
        except Exception as e:
            logger.error("Failed to get messages for %s: %s", session_id[:8], e)
            return []
