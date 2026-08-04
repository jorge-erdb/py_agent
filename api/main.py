import uuid
import asyncio
import time
import json
import logging
from typing import Optional, AsyncGenerator
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import List, Literal

import config
from agent.core import Agent, Tool
from agent.prompt import build_system_prompt
from agent.tools import run_shell_command
from database.db import DatabaseManager

logger = logging.getLogger(__name__)

app = FastAPI()


# Enable CORS for the frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Configuration: environment > ~/.config/py_agent/config.json > default
AGENT_NAME = config.get("agent", "name", "AGENT_NAME", "Kairo")
AGENT_PERSONA = config.get(
    "agent", "persona", "AGENT_PERSONA",
    "An advanced AI assistant designed to be genuinely helpful and rigorously honest. "
    "Your core purpose is to assist users in achieving their goals."
)
# Free-form operator text appended to the end of the system prompt, after the
# built-in sections, so it can override them.
AGENT_INSTRUCTIONS = config.get("agent", "instructions", "AGENT_INSTRUCTIONS", "")
LLM_BASE_URL = config.get("llm", "base_url", "LLM_BASE_URL", "http://localhost:8080/v1")
LLM_API_KEY = config.get("llm", "api_key", "LLM_API_KEY", "sk-no-key-needed")
LLM_MODEL = config.get("llm", "model", "LLM_MODEL", "local-model")
LLM_TIMEOUT = config.get("llm", "timeout", "LLM_TIMEOUT", 600, cast=int)


# Built once, at import time — this module is imported during process startup,
# so the prompt is fixed for the life of the instance and every session created
# by it shares this exact string. Restart to pick up config changes.
SYSTEM_PROMPT = build_system_prompt(
    AGENT_NAME,
    AGENT_PERSONA,
    extra_instructions=AGENT_INSTRUCTIONS,
)
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

    # ── Session lifecycle ──────────────────────────────────────────────

    async def get_or_create(self, session_id: Optional[str] = None) -> tuple[str, Agent, asyncio.Lock]:
        """Get existing session or create a new one. Returns (session_id, agent, lock).

        Protected by a global lock so that concurrent requests with the same
        stale session ID never produce duplicate agents.
        """
        async with self._global_lock:
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
        """Clear messages for a session and update timestamps."""
        async with self._global_lock:
            if session_id not in self.sessions:
                return False
            # Update in-memory timestamp
            self.session_timestamps[session_id] = time.time()
            # Persist the clear to DB
            if self.db:
                await self.db.clear_session(session_id)
                await self.db.update_session_activity(session_id)
            # Clear history in memory (keep system prompt)
            self.sessions[session_id].history = [self.sessions[session_id].history[0]]
            # DB rows are gone too, so nothing is persisted any more.
            self.persisted_counts[session_id] = 0
            return True

    async def destroy_all(self) -> int:
        """Destroy all sessions."""
        async with self._global_lock:
            count = 0
            if self.db:
                count = await self.db.destroy_all_sessions()
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
                del self.sessions[session_id]
                del self.session_timestamps[session_id]
                del self.session_locks[session_id]
                self.persisted_counts.pop(session_id, None)
                removed = True
            if self.db:
                await self.db.destroy_session(session_id)
            return removed

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
        # Fallback to in-memory only
        result = []
        for sid, ts in self.session_timestamps.items():
            result.append({"id": sid, "last_active": ts})
        return result


# ── Pydantic models ────────────────────────────────────────────────────

class Message(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: List[Message]
    session_id: Optional[str] = None


class ClearRequest(BaseModel):
    session_id: str


# ── Session manager instance (wired up in main.py lifespan) ───────────

session_manager: Optional[SessionManager] = None


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
                logger.info("Stream cancelled for session %s", session_id[:8])
                raise  # Re-raise to avoid partial history save

        # Persist only what this turn added — see persist_new_messages().
        await session_manager.persist_new_messages(session_id, agent)

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


@app.post("/sessions/{session_id}")
async def destroy_session(session_id: str):
    """Destroy a specific session."""
    if session_manager is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    success = await session_manager.destroy_session(session_id)
    return {"success": success}


@app.get("/health")
async def health_check():
    """Simple health check endpoint."""
    return {"status": "ok"}
