# Py Agent

A Python-based AI agent with a web interface that can reason, call tools, and execute shell commands. Built with FastAPI, vanilla JavaScript, and an OpenAI-compatible LLM backend.

## Features

- **Reasoning-loop agent** — The agent iteratively calls the LLM, executes tool outputs, and loops until a final answer is produced.
- **Tool-based architecture** — Register arbitrary functions as tools with metadata (name, description, parameters). Both sync and async functions are supported.
- **Shell command execution** — A built-in `run_shell_command` tool runs commands through a shell, gated by a command-name whitelist and a pattern blacklist. See [Security](#security).
- **Session management** — Per-session agent instances, persisted to SQLite and restored on restart, with automatic cleanup after 30 minutes of inactivity.
- **Streaming responses** — Messages and tool results stream to the UI as NDJSON while the reasoning loop runs.
- **Web UI** — A glassmorphism-styled chat interface with markdown rendering, collapsible tool outputs, timestamps, and abort support.

## Tech Stack

| Layer | Technology |
|-------|------------|
| Backend | Python 3.12+ · FastAPI · Uvicorn |
| Persistence | SQLite via aiosqlite |
| LLM Client | OpenAI SDK (compatible with any OpenAI-API server) |
| Frontend | Vanilla JS · Marked.js · DOMPurify |
| Styling | CSS variables · Glassmorphism |

## Prerequisites

- Python 3.12+
- An OpenAI-compatible LLM server running on `http://localhost:8080` (e.g., [llama.cpp](https://github.com/ggerganov/llama.cpp), [Ollama](https://ollama.com), or similar)

## Installation

```bash
# Create and activate the virtual environment
python -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

## Configuration

All settings use sensible defaults — override them with environment variables, or copy
`.env.example` to `.env` and edit it:

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_BASE_URL` | `http://localhost:8080/v1` | OpenAI-compatible API URL |
| `LLM_API_KEY` | `sk-no-key-needed` | API key for the LLM server |
| `LLM_MODEL` | `local-model` | Model name sent to the LLM |
| `LLM_TIMEOUT` | `600` | Request timeout in **seconds** (default 10 min) |
| `AGENT_NAME` | `Kairo` | Agent name used in system prompt |
| `AGENT_PERSONA` | _(default persona)_ | System prompt description |
| `SERVER_HOST` | `0.0.0.0` | Host to bind the server |
| `SERVER_PORT` | `8000` | Port to bind the server |

## Running

```bash
python main.py
```

The server starts on the configured host and port (default `http://0.0.0.0:8000`). Visit `http://localhost:8000` in your browser to access both the chat UI and the API endpoints. The frontend and backend are served from a single process.

> **Note:** the default `SERVER_HOST` of `0.0.0.0` binds to every network interface, exposing the agent — and its shell tool — to anyone on your network. Set `SERVER_HOST=127.0.0.1` unless you specifically want remote access. See [Security](#security).

## Running with Docker (recommended)

Because the agent executes shell commands, running it in a container is the
recommended setup — it turns the whitelist from an advisory guardrail into an
enforced boundary.

```bash
docker compose up -d --build
```

Then visit `http://localhost:8000`.

The container is locked down by default:

| Control | Effect |
|---------|--------|
| `read_only: true` | Root filesystem is immutable — nothing the model runs can persist itself |
| Non-root user (uid 10001) | No write access to `/app` or any host mount |
| `cap_drop: ALL`, `no-new-privileges` | No capability escalation |
| `127.0.0.1:8000:8000` | Reachable only from this machine, never the network |
| `pids_limit`, `mem_limit` | Bounds runaway loops — the shell tool has no timeout of its own |
| `agent-data` volume | The only writable mount; keeps SQLite sessions across restarts |

**Reaching your LLM server.** Inside the container, `localhost` is the container
itself. The compose file therefore points `LLM_BASE_URL` at
`http://host.docker.internal:8080/v1` and defines a `host-gateway` alias so that
resolves on Linux. If your LLM runs somewhere else, override `LLM_BASE_URL`.

**Giving the agent host files.** By default it can read `~/Projects` at
`/host/Projects`, read-only. Edit the `volumes:` block in `docker-compose.yml` to
add or remove paths. Mount only what you need — every path you add is one a
prompt-injected model could read back to whoever is chatting with it.

## Project Structure

```
py_agent/
├── main.py                  # Entry point — launches uvicorn + serves frontend
├── requirements.txt         # Pinned Python dependencies
├── .env.example             # Configuration template
├── Dockerfile               # Container image
├── docker-compose.yml       # Sandboxed run configuration
├── venv/                    # Virtual environment (not committed)
│
├── agent/                   # Agent core logic
│   ├── core.py              # Tool class + Agent with reasoning loop
│   └── tools.py             # Shell tool + whitelist/blacklist gate
│
├── api/                     # FastAPI backend
│   └── main.py              # REST API + session management
│
├── database/                # Persistence layer
│   └── db.py                # aiosqlite session + message storage
│
├── data/                    # Runtime SQLite database (not committed)
│
└── frontend/                # Static web UI
    ├── index.html           # Chat interface
    ├── script.js            # Frontend logic
    ├── style.css            # Glassmorphism theme
    └── assets/              # Background images
```

## API Endpoints

### `POST /chat/stream`

The endpoint the web UI uses. Same request body as `/chat`, but responds with
`application/x-ndjson` — one JSON object per line, emitted as the reasoning loop
runs, so assistant messages and tool results appear incrementally.

The stream ends with a `{"_session_id": "..."}` line, which the client stores to
resume the session across page reloads.

### `POST /chat`

Send a message and receive the agent's response.

**Request body:**
```json
{
  "messages": [
    { "role": "user", "content": "List files in /tmp" }
  ],
  "session_id": "optional-session-id"
}
```

**Response:**
```json
{
  "response": "Here are the files...",
  "history": [...],
  "session_id": "generated-uuid"
}
```

### `POST /clear`

Clear chat history for a specific session, or destroy all sessions if no `session_id` is provided.

**Request body:**
```json
{
  "session_id": "optional-session-id"
}
```

**Response (with session_id):**
```json
{"status": "history cleared", "cleared": true}
```

**Response (without session_id):**
```json
{"status": "all sessions destroyed", "cleared": true}
```

## Security

This project lets a language model run shell commands on the machine hosting it.
Understand the following before exposing it to anything:

**What protects you.** `agent/tools.py` gates every command through two checks:

- A **whitelist** of allowed command names (`cat`, `ls`, `grep`, `find`, `python3`,
  `sqlite3`, and similar read-oriented tools). Only the first token is checked.
- A **blacklist** of patterns matched against the full command string, rejecting
  `rm`, `sudo`, `dd if=`, and `git`.

Output is truncated at 6,000 characters.

**What does not protect you.** These checks are a guardrail against an LLM making a
careless mistake — not a security boundary against a determined attacker:

- Commands run through a **full shell**, so pipes, redirection, `;`, and `$(...)`
  are all live. A whitelisted command name is no guarantee the rest of the line is
  harmless.
- Whitelisted interpreters (`python3`, `sqlite3`) can do essentially anything.
- There is **no sandboxing, no timeout, and no resource limit**. Commands run as the
  user who started the server, with that user's full filesystem access.
- `/chat` and `/chat/stream` have **no authentication**, and CORS is set to `*`.

**Recommendations.**

- **Use the [Docker setup](#running-with-docker-recommended).** It enforces most of
  what the whitelist only suggests: immutable root filesystem, non-root user, no
  capabilities, loopback-only port, and read-only host mounts.
- If running natively, bind to `127.0.0.1` (`SERVER_HOST=127.0.0.1`) rather than the
  `0.0.0.0` default.
- Never expose this to the public internet or an untrusted network.
- Treat the filesystem the agent can reach as compromised-by-design, and mount only
  what it genuinely needs.
- Tighten `COMMAND_WHITELIST` in `agent/tools.py` to the minimum your use case needs.

## Extending the Agent

### Adding a New Tool

Tools are registered on the agent instance:

```python
from agent.core import Tool

async def my_tool(query: str) -> str:
    # ... your logic here
    return result

tool = Tool(
    name="my_tool",
    description="What this tool does.",
    func=my_tool,
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The search query."}
        },
        "required": ["query"],
    }
)

agent.register_tool(tool)
```

## License

MIT — see [LICENSE](LICENSE) file.
