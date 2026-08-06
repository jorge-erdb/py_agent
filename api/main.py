import uuid
import asyncio
import time
import json
import logging
from typing import AsyncGenerator, List, Literal, Optional
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import config
from agent.core import Agent, Tool
from agent.prompt import AGENT_NAME, AGENT_PERSONA, build_configured_prompt
from agent.tools import run_shell_command
from database.db import DatabaseManager

logger = logging.getLogger(__name__)

app = FastAPI()


def _parse_origins(value) -> list[str]:
    """Accept a JSON array from the config file or a comma-separated env var."""
    if isinstance(value, (list, tuple)):
        items = value
    else:
        items = str(value or "").split(",")
    return [str(item).strip() for item in items if str(item).strip()]


# Cross-origin access is off by default. main.py serves the frontend from this
# same origin, so the bundled UI never needs CORS — the only thing a wildcard
# enabled was *other* websites reaching an unauthenticated agent that runs shell
# commands. Set this only if you serve the frontend from somewhere else.
CORS_ORIGINS = _parse_origins(
    config.get("server", "cors_origins", "CORS_ORIGINS", "", cast=lambda v: v)
)

if CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    if "*" in CORS_ORIGINS:
        logger.warning(
            "CORS is set to '*' — any web page you visit can drive this agent, "
            "and there is no authentication. See SECURITY.md."
        )
    else:
        logger.info("CORS enabled for: %s", ", ".join(CORS_ORIGINS))
else:
    logger.info("CORS disabled — same-origin requests only")


# Configuration: environment > <repo>/config.json > default
# The agent.* settings are resolved in agent.prompt, alongside the prompt they
# configure; AGENT_NAME is re-exported here because the Agent also carries it.
LLM_BASE_URL = config.get("llm", "base_url", "LLM_BASE_URL", "http://localhost:8080/v1")
LLM_API_KEY = config.get("llm", "api_key", "LLM_API_KEY", "sk-no-key-needed")
LLM_MODEL = config.get("llm", "model", "LLM_MODEL", "local-model")
LLM_TIMEOUT = config.get("llm", "timeout", "LLM_TIMEOUT", 600, cast=int)

# How long a session may sit untouched before it is dropped from memory. This
# unloads, it never deletes: the conversation stays in SQLite and is rebuilt on
# the next request. Set to 0 to keep every session resident for the life of the
# process.
SESSION_IDLE_TTL = config.get(
    "sessions", "idle_ttl", "SESSION_IDLE_TTL", 1800, cast=int
)


# Built once, at import time — this module is imported during process startup,
# so the prompt is fixed for the life of the instance and every session created
# by it shares this exact string. Restart to pick up config changes.
SYSTEM_PROMPT = build_configured_prompt()
logger.info("System prompt built: %d chars", len(SYSTEM_PROMPT))
logger.debug("System prompt:\n%s", SYSTEM_PROMPT)


class SessionManager:
    """Manages per-session agent instances to prevent race conditions.

    Uses a single global lock for session lookup/creation so that two
    concurrent requests with the same (possibly stale) session ID never
    produce duplicate agents.  All DB writes are synchronous — no more
    fire-and-forget silent failures.
    """

    def __init__(self, db: Optional[DatabaseManager] = None):
        self.sessions: dict[str, Agent] = {}
        self.session_timestamps: dict[str, float] = {}
        self.session_locks: dict[str, asyncio.Lock] = {}
        # How many of each agent's history entries are already in the DB.
        # history[0] is the system prompt and is never stored, so this doubles
        # as an offset into history[1:] — see persist_new_messages().
        self.persisted_counts: dict[str, int] = {}
        self.db = db

        # Global lock protects get_or_create from duplicate creation races.
        self._global_lock = asyncio.Lock()

        # Shared tool configuration (registered per session)
        self._tool = Tool(
            name="run_shell_command",
            description="Executes a shell command on the local system and returns the output.",
            func=run_shell_command,
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command to run."}
                },
                "required": ["command"],
            }
        )

    # ── Helpers ────────────────────────────────────────────────────────

    def _create_agent(self) -> Agent:
        """Create a new agent instance with tool registered."""
        agent = Agent(
            name=AGENT_NAME,
            persona=AGENT_PERSONA,
            base_url=LLM_BASE_URL,
            api_key=LLM_API_KEY,
            model=LLM_MODEL,
            timeout=LLM_TIMEOUT,
            system_prompt=SYSTEM_PROMPT,
        )
        agent.register_tool(self._tool)
        return agent

    async def _unload_idle(self, exclude: Optional[str] = None) -> int:
        """Drop idle sessions from memory. **Nothing is deleted.**

        The in-memory Agent is a cache: `get_or_create` can rebuild one from
        SQLite whenever it is asked for. Without this the four dicts only ever
        shrink on an explicit destroy, so every session opened since boot stays
        resident — each holding a full history and, more to the point, an
        `AsyncOpenAI` client with its own connection pool that nothing closes.

        `session_timestamps` is deliberately left intact. It is the record of
        what exists and when it was last touched, it costs a float per session,
        and `get_or_create` needs nothing more to bring one back.

        Caller must hold `_global_lock`.

        Args:
            exclude: Session to leave resident — the one being served, which
                would otherwise be unloaded and reloaded in the same call.

        Returns:
            How many sessions were unloaded.
        """
        if SESSION_IDLE_TTL <= 0:
            return 0

        cutoff = time.time() - SESSION_IDLE_TTL
        stale = []

        for sid, agent in self.sessions.items():
            if sid == exclude:
                continue
            if self.session_timestamps.get(sid, 0.0) > cutoff:
                continue
            # A held lock means a stream is in flight on this session.
            lock = self.session_locks.get(sid)
            if lock is not None and lock.locked():
                continue
            # A cancelled stream skips persist_new_messages, leaving history
            # in memory that never reached SQLite. Unloading would be the only
            # thing that could actually lose data, so don't — it will be
            # written on the session's next turn.
            if self.persisted_counts.get(sid, 0) != len(agent.history) - 1:
                logger.debug(
                    "Keeping idle session %s resident — unpersisted history", sid[:8]
                )
                continue
            stale.append(sid)

        for sid in stale:
            agent = self.sessions.pop(sid)
            self.session_locks.pop(sid, None)
            self.persisted_counts.pop(sid, None)
            try:
                await agent.client.close()
            except Exception as e:
                # A pool that will not close is not a reason to keep the
                # session in memory.
                logger.warning("Error closing LLM client for %s: %s", sid[:8], e)

        if stale:
            logger.info(
                "Unloaded %d idle session(s) from memory; %d still resident",
                len(stale), len(self.sessions),
            )
        return len(stale)

    # ── Session lifecycle ──────────────────────────────────────────────

    async def get_or_create(self, session_id: Optional[str] = None) -> tuple[str, Agent, asyncio.Lock]:
        """Get existing session or create a new one. Returns (session_id, agent, lock).

        Protected by a global lock so that concurrent requests with the same
        stale session ID never produce duplicate agents.
        """
        async with self._global_lock:
            # Reclaim anything that has gone idle. Done here rather than on a
            # background task so there is no second source of mutation for
            # these dicts — the global lock is already held.
            await self._unload_idle(exclude=session_id)

            # 1. Already in memory — just bump the timestamp
            if session_id and session_id in self.sessions:
                self.session_timestamps[session_id] = time.time()
                return session_id, self.sessions[session_id], self.session_locks[session_id]

            # 2. Exists in DB but not in memory — load it
            if session_id and self.db:
                exists = await self.db.session_exists(session_id)
                if exists:
                    agent = self._create_agent()
                    timestamps = await self.db.get_session_timestamps(session_id)
                    if timestamps:
                        self.session_timestamps[session_id] = timestamps["last_active"]
                    # Load history from DB (full OpenAI format with tool_calls)
                    history = await self.db.get_history(session_id)
                    if history:
                        agent.history = [agent.history[0]] + history  # keep system prompt at top
                    # Everything just loaded is by definition already stored.
                    self.persisted_counts[session_id] = len(history)
                    self.sessions[session_id] = agent
                    self.session_locks[session_id] = asyncio.Lock()
                    return session_id, agent, self.session_locks[session_id]

            # 3. Brand new session — create in memory and DB
            if not session_id:
                session_id = str(uuid.uuid4())

            agent = self._create_agent()
            self.sessions[session_id] = agent
            self.session_timestamps[session_id] = time.time()
            self.session_locks[session_id] = asyncio.Lock()
            self.persisted_counts[session_id] = 0

            # Persist to DB — this is synchronous, not fire-and-forget
            if self.db:
                created = await self.db.create_session(session_id)
                if not created:
                    logger.warning("Session %s was created in memory but already exists in DB", session_id[:8])
                else:
                    logger.info("Created new session %s in DB", session_id[:8])

            return session_id, agent, self.session_locks[session_id]

    async def clear(self, session_id: str) -> bool:
        """Clear a session's messages, resident in memory or not.

        `load_sessions()` only populates `session_timestamps`; `self.sessions`
        is filled lazily by `get_or_create` on first use. Gating on memory
        residency therefore made Clear a silent no-op on exactly the sessions
        restored from disk after a restart — the rows survived untouched and
        the conversation reappeared on the next reload.

        Returns False only when the session exists in neither memory nor DB.
        """
        async with self._global_lock:
            resident = session_id in self.sessions

            known = resident
            if not known and self.db:
                known = await self.db.session_exists(session_id)
            if not known:
                return False

            self.session_timestamps[session_id] = time.time()

            if self.db:
                # The row count is not the signal here — clearing a session
                # that had no messages yet is still a success.
                await self.db.clear_session(session_id)
                await self.db.update_session_activity(session_id)

            if resident:
                agent = self.sessions[session_id]
                agent.history = [agent.history[0]]  # keep the system prompt
                # DB rows are gone too, so nothing is persisted any more.
                self.persisted_counts[session_id] = 0
            else:
                # Nothing to trim in memory, but make sure no stale offset
                # survives to desync the next load.
                self.persisted_counts.pop(session_id, None)

            return True

    @staticmethod
    async def _close_client(session_id: str, agent: Agent) -> None:
        """Release an agent's LLM client and its connection pool."""
        try:
            await agent.client.close()
        except Exception as e:
            logger.warning("Error closing LLM client for %s: %s", session_id[:8], e)

    async def destroy_all(self) -> int:
        """Destroy all sessions."""
        async with self._global_lock:
            count = 0
            if self.db:
                count = await self.db.destroy_all_sessions()
            for sid, agent in self.sessions.items():
                await self._close_client(sid, agent)
            self.sessions.clear()
            self.session_timestamps.clear()
            self.session_locks.clear()
            self.persisted_counts.clear()
            return count

    async def destroy_session(self, session_id: str) -> bool:
        """Destroy a specific session."""
        async with self._global_lock:
            removed = False
            if session_id in self.sessions:
                await self._close_client(session_id, self.sessions.pop(session_id))
                del self.session_timestamps[session_id]
                del self.session_locks[session_id]
                self.persisted_counts.pop(session_id, None)
                removed = True
            if self.db:
                await self.db.destroy_session(session_id)
            return removed

    async def shutdown(self) -> None:
        """Release every resident session's LLM client.

        Called from the lifespan teardown. Without it, uvicorn's shutdown leaves
        httpx connection pools to be reclaimed by the garbage collector, which
        logs unclosed-session warnings on the way out.
        """
        async with self._global_lock:
            for sid, agent in self.sessions.items():
                await self._close_client(sid, agent)
            self.sessions.clear()
            self.session_locks.clear()
            self.persisted_counts.clear()

    async def persist_new_messages(self, session_id: str, agent: Agent) -> int:
        """Write only the history entries added since the last save.

        The agent accumulates the whole conversation in memory, so saving
        `history[1:]` wholesale re-inserts every earlier turn and grows the
        table quadratically. `persisted_counts` records how much of `history`
        is already on disk; everything past that offset is what is new.

        Returns the number of rows written.
        """
        if not self.db:
            return 0

        already = self.persisted_counts.get(session_id, 0)
        pending = agent.history[1 + already:]  # skip the system prompt
        if not pending:
            return 0

        saved_msgs = []
        for msg in pending:
            role = msg.get("role", "assistant")

            if role == "user":
                saved_msgs.append({
                    "message_type": "user",
                    "role": "user",
                    "content": msg.get("content", ""),
                })
            elif role == "tool":
                saved_msgs.append({
                    "message_type": "tool",
                    "role": "tool",
                    "content": msg.get("content", ""),
                    "tool_call_id": msg.get("tool_call_id"),
                    "command": msg.get("command"),
                    "tool_name": msg.get("tool_name"),
                })
            elif role == "assistant" and msg.get("tool_calls"):
                # Assistant issued tool calls — store the call array as JSON
                saved_msgs.append({
                    "message_type": "assistant_tool_call",
                    "role": "assistant",
                    "content": json.dumps(msg["tool_calls"]),
                    "tool_call_id": msg.get("tool_call_id"),
                    "command": msg.get("command"),
                    "tool_name": msg.get("tool_name"),
                })
            elif role == "assistant":
                saved_msgs.append({
                    "message_type": "assistant",
                    "role": "assistant",
                    "content": msg.get("content", "") or "",
                    "tool_call_id": msg.get("tool_call_id"),
                    "command": msg.get("command"),
                    "tool_name": msg.get("tool_name"),
                })

        if saved_msgs:
            written = await self.db.batch_save_messages(session_id, saved_msgs)
            if written == 0:
                # The write failed — leave the offset alone so these messages
                # are retried on the next turn rather than silently dropped.
                logger.error(
                    "Failed to persist %d messages for session %s — will retry",
                    len(saved_msgs), session_id[:8],
                )
                return 0

        # Advance past everything consumed, including any entry no branch above
        # matched — the offset tracks position in history, not rows written.
        self.persisted_counts[session_id] = len(agent.history) - 1
        await self.db.update_session_activity(session_id)
        return len(saved_msgs)

    async def load_sessions(self):
        """Load all sessions from DB into memory on startup."""
        async with self._global_lock:
            if not self.db:
                return
            sessions = await self.db.get_all_sessions()
            for sess in sessions:
                sid = sess["id"]
                # Create a placeholder agent (no history yet) — it will be
                # fully loaded on first use via get_or_create
                timestamps = await self.db.get_session_timestamps(sid)
                if timestamps:
                    self.session_timestamps[sid] = timestamps["last_active"]
                else:
                    self.session_timestamps[sid] = sess["created_at"]

    async def get_sessions(self) -> List[dict]:
        """Return all sessions (from memory + DB)."""
        # Always refresh from DB to catch any sessions that might exist there
        # but weren't loaded (e.g., after a crash where in-memory state was lost)
        if self.db:
            return await self.db.get_all_sessions()
        # Fallback to in-memory only. No preview is available without the DB —
        # the shape stays the same so the frontend needs no special case.
        result = []
        for sid, ts in self.session_timestamps.items():
            result.append({"id": sid, "last_active": ts, "preview": None})
        return result


# ── Pydantic models ────────────────────────────────────────────────────

class Message(BaseModel):
    # Only "user" is accepted. `chat_stream` copies this straight into
    # agent.history, so a client posting role="system" would rewrite the
    # agent's instructions mid-conversation — and there is no auth in front of
    # this endpoint. Pydantic rejects anything else with a 422 before the
    # handler runs. The frontend only ever sends "user" (api.js:34).
    role: Literal["user"]
    content: str


class ChatRequest(BaseModel):
    messages: List[Message]
    session_id: Optional[str] = None


class ClearRequest(BaseModel):
    session_id: str


# ── Session manager instance (wired up in main.py lifespan) ───────────

session_manager: Optional[SessionManager] = None


async def _persist_turn(session_id: str, agent: Agent) -> None:
    """Write a finished turn to SQLite, surviving a client disconnect.

    A disconnect tears this generator down mid-await, which would cancel an
    ordinary `await` on the write along with it. `shield` lets the write run to
    completion regardless. Waiting on it explicitly afterwards matters just as
    much: it keeps the caller — and therefore the per-session lock it is holding
    — from unwinding before the rows land, so a fast follow-up request cannot
    start a turn that races this one's bookkeeping.
    """
    writer = asyncio.create_task(
        session_manager.persist_new_messages(session_id, agent)
    )
    try:
        await asyncio.shield(writer)
    except asyncio.CancelledError:
        await asyncio.wait({writer})
        raise


# ── API Routes ─────────────────────────────────────────────────────────

@app.post("/new-session")
async def new_session():
    """Create a fresh session. Returns the new session_id."""
    if session_manager is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    session_id, _, _ = await session_manager.get_or_create()
    return {"session_id": session_id}


@app.get("/sessions")
async def list_sessions():
    """List all sessions."""
    if session_manager is None:
        raise HTTPException(status_code=503, detail="Service not initialized")
    sessions = await session_manager.get_sessions()
    return sessions


@app.get("/sessions/{session_id}/messages")
async def get_session_messages(session_id: str):
    """Get full message history for a session (for the frontend chat log)."""
    if session_manager is None or not session_manager.db:
        raise HTTPException(status_code=503, detail="Service not initialized")

    # Check DB exists
    exists = await session_manager.db.session_exists(session_id)
    if not exists:
        raise HTTPException(status_code=404, detail="Session not found")

    messages = await session_manager.db.get_session_messages(session_id)
    return {"session_id": session_id, "messages": messages}


@app.post("/chat/stream")
async def chat_stream(request: ChatRequest):
    """Stream a conversation with the agent.

    Yields NDJSON-encoded messages as they arrive, plus a final sentinel
    message containing the complete history for frontend state sync.
    """
    if session_manager is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    # Get or create the session — protected by global lock to prevent races
    session_id, agent, lock = await session_manager.get_or_create(request.session_id)

    async def event_generator() -> AsyncGenerator[str, None]:
        """Yield NDJSON-encoded messages from the agent's reasoning loop."""
        new_messages = [{"role": m.role, "content": m.content} for m in request.messages]

        # Use per-session lock to prevent concurrent processing
        async with lock:
            try:
                async for msg in agent.process_messages(new_messages):
                    yield json.dumps(msg) + "\n"
            except asyncio.CancelledError:
                # Stop, or the browser going away. Everything this turn produced
                # is already on the user's screen, so discarding it would make
                # Stop look like it silently threw the work away.
                #
                # It cannot be saved as-is, though: a stream cut between an
                # assistant tool_calls message and its results leaves history in
                # a state the API refuses. Trim back to the last complete turn,
                # then save that — in memory as well as on disk, or the live
                # session keeps the invalid tail and fails on its next turn.
                removed = agent.trim_incomplete_tail()
                logger.info(
                    "Stream cancelled for session %s — keeping %d entries, dropped %d incomplete",
                    session_id[:8], len(agent.history) - 1, removed,
                )
                raise
            finally:
                # Runs on the cancellation path too, before the exception
                # propagates — see _persist_turn().
                await _persist_turn(session_id, agent)

    return StreamingResponse(
        event_generator(),
        media_type="application/x-ndjson",
    )


@app.post("/clear")
async def clear_history(request: ClearRequest):
    """Clear a specific session's history."""
    if session_manager is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    success = await session_manager.clear(request.session_id)
    return {"success": success}


@app.post("/destroy_all")
async def destroy_all_sessions():
    """Destroy all sessions."""
    if session_manager is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    count = await session_manager.destroy_all()
    return {"destroyed": count}


@app.delete("/sessions/{session_id}")
async def destroy_session(session_id: str):
    """Destroy a specific session and all of its messages. Irreversible."""
    if session_manager is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    success = await session_manager.destroy_session(session_id)
    return {"success": success}


@app.get("/health")
async def health_check():
    """Simple health check endpoint."""
    return {"status": "ok"}
