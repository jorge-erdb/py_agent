import aiosqlite
import time
from typing import List, Optional, Dict, Any

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
        return self._db

    async def initialize(self):
        """Initialize the database schema."""
        db = await self._get_db()
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                created_at REAL,
                last_active REAL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                role TEXT,
                content TEXT,
                tool_call_id TEXT,
                command TEXT,
                tool_name TEXT,
                timestamp REAL,
                FOREIGN KEY (session_id) REFERENCES sessions (id)
            )
        """)
        await db.commit()

    async def close(self):
        """Close the persistent database connection."""
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def create_session(self, session_id: str) -> None:
        """Create a new session entry."""
        now = time.time()
        db = await self._get_db()
        await db.execute(
            "INSERT INTO sessions (id, created_at, last_active) VALUES (?, ?, ?)",
            (session_id, now, now)
        )
        await db.commit()

    async def update_session_activity(self, session_id: str) -> None:
        """Update the last active timestamp for a session."""
        now = time.time()
        db = await self._get_db()
        await db.execute(
            "UPDATE sessions SET last_active = ? WHERE id = ?",
            (now, session_id)
        )
        await db.commit()

    async def save_message(self, session_id: str, role: str, content: str, 
                           tool_call_id: Optional[str] = None, 
                           command: Optional[str] = None, 
                           tool_name: Optional[str] = None) -> None:
        """Save a message to the database."""
        now = time.time()
        db = await self._get_db()
        await db.execute(
            """
            INSERT INTO messages (session_id, role, content, tool_call_id, command, tool_name, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (session_id, role, content, tool_call_id, command, tool_name, now)
        )
        await db.commit()

    async def get_history(self, session_id: str) -> List[Dict[str, Any]]:
        """Retrieve history for a session, ordered by timestamp."""
        db = await self._get_db()
        async with db.execute(
            "SELECT id, role, content, tool_call_id, command, tool_name, timestamp FROM messages WHERE session_id = ? ORDER BY timestamp ASC",
            (session_id,)
        ) as cursor:
            rows = await cursor.fetchall()
            history = []
            for row in rows:
                msg = {
                    "id": row["id"],
                    "role": row["role"],
                    "content": row["content"],
                    "timestamp": row["timestamp"],
                }
                if row["tool_call_id"]:
                    msg["tool_call_id"] = row["tool_call_id"]
                if row["command"]:
                    msg["command"] = row["command"]
                if row["tool_name"]:
                    msg["tool_name"] = row["tool_name"]
                history.append(msg)
            return history

    async def clear_session(self, session_id: str) -> bool:
        """Clear messages for a specific session."""
        db = await self._get_db()
        async with db.execute("DELETE FROM messages WHERE session_id = ?", (session_id,)) as cursor:
            await db.commit()
            return cursor.rowcount > 0

    async def destroy_session(self, session_id: str) -> bool:
        """Remove a session and its messages."""
        db = await self._get_db()
        # Delete child rows first (messages), then parent (sessions)
        async with db.execute("DELETE FROM messages WHERE session_id = ?", (session_id,)) as cursor:
            pass
        async with db.execute("DELETE FROM sessions WHERE id = ?", (session_id,)) as cursor:
            await db.commit()
            return cursor.rowcount > 0

    async def destroy_all_sessions(self) -> int:
        """Remove all sessions and messages. Returns total records destroyed."""
        db = await self._get_db()
        # Count messages before deleting
        async with db.execute("SELECT COUNT(*) FROM messages") as cursor:
            msg_count = (await cursor.fetchone())[0]
        async with db.execute("DELETE FROM messages") as cursor:
            await db.commit()
        async with db.execute("DELETE FROM sessions") as cursor:
            session_count = cursor.rowcount
            await db.commit()
        return msg_count + session_count

    async def session_exists(self, session_id: str) -> bool:
        """Check if a session exists in the database."""
        db = await self._get_db()
        async with db.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)) as cursor:
            row = await cursor.fetchone()
            return row is not None

    async def get_all_sessions(self) -> List[Dict[str, Any]]:
        """Retrieve all sessions with their timestamps."""
        db = await self._get_db()
        async with db.execute("SELECT id, created_at, last_active FROM sessions") as cursor:
            rows = await cursor.fetchall()
            return [
                {
                    "id": row["id"],
                    "created_at": row["created_at"],
                    "last_active": row["last_active"],
                }
                for row in rows
            ]

    async def get_session_timestamps(self, session_id: str) -> Optional[Dict[str, float]]:
        """Retrieve created_at and last_active for a session."""
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
