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

Settings resolve in three layers — **environment variable**, then **user config
file**, then **built-in default**. Everything is optional.

The config file lives outside the repository at
`~/.config/py_agent/config.json` (or `$XDG_CONFIG_HOME/py_agent/config.json`), so
API keys never touch git and local tweaks never show up in `git status`:

```bash
mkdir -p ~/.config/py_agent
cp config.example.json ~/.config/py_agent/config.json
chmod 600 ~/.config/py_agent/config.json   # if it will hold a real API key
```

A malformed or unreadable config file is logged and ignored rather than fatal.

### Using a cloud provider

The client is the OpenAI SDK, so any OpenAI-compatible endpoint works — just set
`base_url`, `model`, and `api_key`:

```json
{
  "llm": {
    "base_url": "https://openrouter.ai/api/v1",
    "model": "anthropic/claude-sonnet-4",
    "api_key": "sk-or-..."
  }
}
```

`config.example.json` includes ready-made blocks for llama.cpp, Ollama, OpenAI,
OpenRouter, Groq, and Together.

### Settings

| Environment variable | Config file key | Default | Description |
|----------------------|-----------------|---------|-------------|
| `LLM_BASE_URL` | `llm.base_url` | `http://localhost:8080/v1` | OpenAI-compatible API URL |
| `LLM_API_KEY` | `llm.api_key` | `sk-no-key-needed` | API key for the LLM server |
| `LLM_MODEL` | `llm.model` | `local-model` | Model name sent to the LLM |
| `LLM_TIMEOUT` | `llm.timeout` | `600` | Request timeout in **seconds** (10 min) |
| `AGENT_NAME` | `agent.name` | `Kairo` | Agent name used in system prompt |
| `AGENT_PERSONA` | `agent.persona` | _(default persona)_ | System prompt description |
| `SERVER_HOST` | `server.host` | `0.0.0.0` | Host to bind the server |
| `SERVER_PORT` | `server.port` | `8000` | Port to bind the server |

## Running

```bash
python main.py
```

The server starts on the configured host and port (default `http://0.0.0.0:8000`). Visit `http://localhost:8000` in your browser to access both the chat UI and the API endpoints. The frontend and backend are served from a single process.

> **Note:** the default `SERVER_HOST` of `0.0.0.0` binds every network interface, exposing the agent — and its shell tool — to anyone who can reach the port, with no authentication. The server logs a warning when this is in effect. Set `SERVER_HOST=127.0.0.1` unless you specifically want remote access. See [SECURITY.md](SECURITY.md).

## Running with Docker

Optional. Running natively works fine — the container mainly buys you a sandbox,
which matters because the agent executes shell commands.

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
├── config.py                # Layered config: env > user file > default
├── config.example.json      # Template for ~/.config/py_agent/config.json
├── requirements.txt         # Pinned Python dependencies
├── SECURITY.md              # Trust model and boundaries
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

Deletes chat history. The destructive scope must be stated explicitly — a request
that specifies neither target is rejected rather than assumed to mean "everything".

**Clear one session** — removes that session's messages, keeps the session itself:

```json
{ "session_id": "the-session-id" }
```
```json
{"status": "history cleared", "cleared": true}
```

An unknown `session_id` is a no-op: `{"status": "session not found", "cleared": false}`.

**Destroy everything** — every session and every message in the database:

```json
{ "all": true }
```
```json
{"status": "all sessions destroyed", "cleared": true}
```

> ⚠️ `all: true` is **irreversible**. There is no backup and no undo — see
> [SECURITY.md](SECURITY.md#data-loss). If both fields are supplied, `all` wins.

**Neither field** — `{}`, `{"session_id": null}`, or `{"all": false}` — returns
**HTTP 400** and deletes nothing:

```json
{"detail": "Either session_id or all=true must be provided."}
```

## Security

This project lets a language model run shell commands on the machine hosting it.
**[SECURITY.md](SECURITY.md) is the authoritative document** — read it before
exposing the agent to anything you care about. In brief:

- `agent/tools.py` gates commands through a whitelist and blacklist. This is a
  guardrail against model error, **not a security boundary** — commands run through
  a full shell, and whitelisted interpreters like `python3` can do anything.
- There is **no sandbox**, no timeout, and no resource limit. Containing the agent
  is your decision; the [Docker setup](#running-with-docker) is one reasonable way.
- There is **no authentication** on any endpoint, and CORS is `*`.
- Prompt injection is possible and cannot be prevented. Don't point the agent at
  content you don't trust.
- `POST /clear` with `all: true` **irreversibly** deletes all history, and the
  endpoint is unauthenticated. Back up `data/agent.db` if it matters to you.

Practical minimum: set `SERVER_HOST=127.0.0.1`, and never expose this to the public
internet.

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
