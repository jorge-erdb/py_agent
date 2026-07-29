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
    "You are an advanced AI assistant designed to be genuinely helpful and rigorously honest. Your core purpose is to assist users in achieving their goals."
)
LLM_BASE_URL = config.get("llm", "base_url", "LLM_BASE_URL", "http://localhost:8080/v1")
LLM_API_KEY = config.get("llm", "api_key", "LLM_API_KEY", "sk-no-key-needed")
LLM_MODEL = config.get("llm", "model", "LLM_MODEL", "local-model")
LLM_TIMEOUT = config.get("llm", "timeout", "LLM_TIMEOUT", 600, cast=int)


class SessionManager:
    """Manages per-session agent instances to prevent race conditions."""

    def __init__(self, session_timeout: int = 1800, db: Optional[DatabaseManager] = None):
        self.sessions: dict[str, Agent] = {}
        self.session_timestamps: dict[str, float] = {}
        self.session_queues: dict[str, asyncio.Queue] = {}
        self.session_results: dict[str, asyncio.Future] = {}
        self.session_workers_running: dict[str, bool] = {}
        self.session_locks: dict[str, asyncio.Lock] = {}
        self.session_timeout = session_timeout  # seconds (default 30 min)
        self.db = db  # Optional database manager for persistence
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

    def _create_agent(self) -> Agent:
        """Create a new agent instance with tool registered."""
        agent = Agent(
            name=AGENT_NAME,
            persona=AGENT_PERSONA,
            base_url=LLM_BASE_URL,
            api_key=LLM_API_KEY,
            model=LLM_MODEL,
            timeout=LLM_TIMEOUT,
        )
        agent.register_tool(self._tool)
        return agent

    def _create_session(self) -> tuple[str, Agent]:
        """Create a new session and return (session_id, agent)."""
        session_id = str(uuid.uuid4())
        agent = self._create_agent()
        self.sessions[session_id] = agent
        self.session_timestamps[session_id] = time.time()
        self.session_locks[session_id] = asyncio.Lock()
        # Persist session to database
        if self.db:
            asyncio.create_task(self.db.create_session(session_id))
        logger.info("Created new session: %s", session_id[:8])
        return session_id, agent

    def clear(self, session_id: str) -> bool:
        """Clear a specific session's history. Returns True if session existed."""
        if session_id in self.sessions:
            agent = self.sessions[session_id]
            agent.history = [
                {"role": "system", "content": f"You are {agent.name}. {agent.persona}"}
            ]
            self.session_timestamps[session_id] = time.time()
            logger.info("Cleared session: %s", session_id[:8])
            # Clear in DB
            if self.db:
                asyncio.create_task(self.db.clear_session(session_id))
            return True
        logger.warning("Clear requested for non-existent session: %s", session_id)
        return False

    def destroy_all(self) -> int:
        """Destroy all sessions. Returns the number of sessions destroyed."""
        count = len(self.sessions)
        self.sessions.clear()
        self.session_timestamps.clear()
        self.session_locks.clear()
        logger.info("Destroyed %d sessions", count)
        # Destroy in DB
        if self.db:
            asyncio.create_task(self.db.destroy_all_sessions())
        return count

    def cleanup_expired(self):
        """Remove sessions that have been idle too long."""
        now = time.time()
        expired = [
            sid for sid, ts in self.session_timestamps.items()
            if now - ts > self.session_timeout
        ]
        for sid in expired:
            del self.sessions[sid]
            del self.session_timestamps[sid]
            self.session_locks.pop(sid, None)
            # Remove from DB
            if self.db:
                asyncio.create_task(self.db.destroy_session(sid))

    async def load_sessions(self):
        """Load all sessions from the database into memory on startup."""
        if not self.db:
            return
        sessions = await self.db.get_all_sessions()
        for session_info in sessions:
            sid = session_info["id"]
            # Skip if already loaded
            if sid in self.sessions:
                continue
            # Create agent and restore history
            agent = self._create_agent()
            self.sessions[sid] = agent
            self.session_timestamps[sid] = session_info["last_active"]
            self.session_locks[sid] = asyncio.Lock()
            # Load message history
            history = await self.db.get_history(sid)
            if history:
                agent.history.extend(history)
            logger.info("Restored session from DB: %s (%d messages)", sid[:8], len(history))

    async def get_or_create(self, session_id: Optional[str] = None) -> tuple[str, Agent, asyncio.Lock]:
        """Get existing session or create a new one. Returns (session_id, agent, lock)."""
        if session_id and session_id in self.sessions:
            # Update timestamp
            self.session_timestamps[session_id] = time.time()
            # Update activity in database
            if self.db:
                asyncio.create_task(self.db.update_session_activity(session_id))
            return session_id, self.sessions[session_id], self.session_locks[session_id]
        # Check if session exists in database but not in memory
        if session_id and self.db:
            exists = await self.db.session_exists(session_id)
            if exists:
                # Session exists in DB but not in memory — create agent and load history
                timestamps = await self.db.get_session_timestamps(session_id)
                agent = self._create_agent()
                if timestamps:
                    self.session_timestamps[session_id] = timestamps["last_active"]
                # Load history from DB
                history = await self.db.get_history(session_id)
                if history:
                    agent.history.extend(history)
                self.sessions[session_id] = agent
                self.session_locks[session_id] = asyncio.Lock()
                # Update activity
                await self.db.update_session_activity(session_id)
                logger.info("Loaded session from DB: %s", session_id[:8])
                return session_id, agent, self.session_locks[session_id]
        sid, agent = self._create_session()
        return sid, agent, self.session_locks[sid]

    async def cleanup_task(self):
        """Background task that periodically cleans up expired sessions."""
        while True:
            before = len(self.sessions)
            self.cleanup_expired()
            after = len(self.sessions)
            if before != after:
                logger.info("Cleaned up %d expired sessions", before - after)
            await asyncio.sleep(300)  # Check every 5 minutes


# Global session manager (database initialized in lifespan)
session_manager: SessionManager = None  # type: ignore  # Initialized in lifespan


class Message(BaseModel):
    role: Literal['system', 'user', 'assistant', 'tool']
    content: str


class ChatRequest(BaseModel):
    messages: List[Message]
    session_id: Optional[str] = None


class ChatResponse(BaseModel):
    response: str
    history: List[dict]
    session_id: str


class ClearRequest(BaseModel):
    session_id: Optional[str] = None
    all: bool = False


@app.post("/chat/stream")
async def chat_stream(request: ChatRequest) -> StreamingResponse:
    """Stream each agent message as it's generated (NDJSON format).

    The last message is a special {"_session_id": "..."} that the client
    uses to persist the session across page reloads. The agent yields a
    {"_final": ...} sentinel before that to signal completion.
    """
    session_id, agent, lock = await session_manager.get_or_create(request.session_id)
    logger.info("Chat request for session: %s", session_id[:8])

    messages_dict = [m.model_dump() for m in request.messages]

    async def message_generator() -> AsyncGenerator[str, None]:
        """Yield each message as a JSON line."""
        async with lock:
            # Save user messages to DB before processing
            if session_manager.db:
                for msg in messages_dict:
                    if msg["role"] == "user":
                        await session_manager.db.save_message(
                            session_id=session_id,
                            role=msg["role"],
                            content=msg["content"],
                        )

            async for msg in agent.process_messages(messages_dict):
                if msg.get("_final"):
                    # Sentinel — don't send to client, just marks end
                    continue
                # Save assistant/tool messages to DB
                if session_manager.db and msg.get("role") in ("assistant", "tool"):
                    await session_manager.db.save_message(
                        session_id=session_id,
                        role=msg["role"],
                        content=msg.get("content", ""),
                        tool_call_id=msg.get("tool_call_id"),
                        command=msg.get("command"),
                        tool_name=msg.get("tool_name"),
                    )
                yield json.dumps(msg) + "\n"
            yield json.dumps({"_session_id": session_id}) + "\n"

    return StreamingResponse(message_generator(), media_type="application/x-ndjson")


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    # Legacy endpoint — collects all yielded messages into a full response
    session_id, agent, lock = await session_manager.get_or_create(request.session_id)
    logger.info("Chat request for session: %s", session_id[:8])

    messages_dict = [m.model_dump() for m in request.messages]
    final_response = None
    final_history = None
    async with lock:
        # Save user messages to DB before processing
        if session_manager.db:
            for msg in messages_dict:
                if msg["role"] == "user":
                    await session_manager.db.save_message(
                        session_id=session_id,
                        role=msg["role"],
                        content=msg["content"],
                    )

        async for msg in agent.process_messages(messages_dict):
            if msg.get("_final"):
                final_response = msg["response"]
                final_history = msg["history"]
                # Save assistant/tool messages to DB
                if session_manager.db:
                    for hist_msg in final_history:
                        if hist_msg.get("role") in ("assistant", "tool"):
                            await session_manager.db.save_message(
                                session_id=session_id,
                                role=hist_msg["role"],
                                content=hist_msg.get("content", ""),
                                tool_call_id=hist_msg.get("tool_call_id"),
                                command=hist_msg.get("command"),
                                tool_name=hist_msg.get("tool_name"),
                            )
                break
    return ChatResponse(response=final_response, history=final_history, session_id=session_id)


@app.post("/clear")
async def clear_history(request: ClearRequest):
    if request.all:
        count = session_manager.destroy_all()
        logger.info("Destroyed %d sessions (all=True)", count)
        
        return {"status": "all sessions destroyed", "cleared": True}

    elif request.session_id:
        existed = session_manager.clear(request.session_id)
        if not existed:
            return {"status": "session not found", "cleared": False}
        return {"status": "history cleared", "cleared": True}

    else:
        raise HTTPException(status_code=400, detail="Either session_id or all=true must be provided.")
        
