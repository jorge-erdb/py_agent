# Py Agent

A Python-based AI agent with a web interface that can reason, call tools, and execute shell commands. Built with FastAPI, vanilla JavaScript, and an OpenAI-compatible LLM backend.

## Features

- **Reasoning-loop agent** — The agent iteratively calls the LLM, executes tool outputs, and loops until a final answer is produced.
- **Tool-based architecture** — Register arbitrary functions as tools with metadata (name, description, parameters). Both sync and async functions are supported.
- **Shell command execution** — A built-in `run_shell_command` tool runs commands through a shell, gated by a command-name whitelist and a pattern blacklist. See [Security](#security).
- **Session management** — Per-session agent instances, persisted to SQLite and restored on restart. Sessions are kept indefinitely and are never expired: the database is meant to be a durable record the agent can search and return to, so an inactivity timeout would defeat its purpose. Prune deliberately via `POST /sessions/{id}` or `POST /destroy_all`.
- **Streaming responses** — Messages and tool results stream to the UI as NDJSON while the reasoning loop runs.
- **Web UI** — A chat interface with markdown rendering, collapsible tool outputs, and abort support. A sidebar lists past conversations with previews derived client-side; a settings dialog covers light/dark/system theming, tool-block defaults, and a custom log background stored in IndexedDB.

## Tech Stack

| Layer | Technology |
|-------|------------|
| Backend | Python 3.12+ · FastAPI · Uvicorn |
| Persistence | SQLite via aiosqlite |
| LLM Client | OpenAI SDK (compatible with any OpenAI-API server) |
| Frontend | Vanilla JS (ES modules) · Marked.js · DOMPurify — both vendored, no CDN |
| Styling | CSS variables · light/dark themes |

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

Settings resolve in three layers — **environment variable**, then **config
file**, then **built-in default**. Everything is optional.

The config file is `config.json` at the repository root. It is gitignored, so
API keys never touch git and local tweaks never show up in `git status`:

```bash
cp config.example.json config.json
chmod 600 config.json   # do this if it will hold a real API key — nothing checks for you
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
| `AGENT_INSTRUCTIONS` | `agent.instructions` | _(empty)_ | Free-form text appended after the built-in prompt sections, so it can override them |
| `SERVER_HOST` | `server.host` | `0.0.0.0` | Host to bind the server |
| `SERVER_PORT` | `server.port` | `8000` | Port to bind the server |

The system prompt is assembled once at process start from `agent.name`,
`agent.persona`, `agent.instructions` and built-in environment/shell-policy
sections, then shared by every session. Restart to pick up changes. Run
`python -m agent.prompt` to print the prompt this machine would boot with.

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

The container constrains the agent as follows:

| Control | Effect |
|---------|--------|
| Non-root user (uid 10001) | No write access to `/app` (root-owned) or any host mount |
| `cap_drop: ALL`, `no-new-privileges` | No capability escalation |
| `127.0.0.1:8000:8000` | Reachable only from this machine, never the network |
| `pids_limit`, `mem_limit` | Bounds process count and memory for runaway loops |
| Host mounts are `:ro` | The agent can read the paths you grant, never modify them |
| `agent-data` volume | Writable mount that keeps SQLite sessions across restarts |
| `tmpfs /tmp` | Scratch space, 64MB, discarded when the container stops |

Two caveats worth knowing rather than assuming:

- `read_only` is currently **false**, so the container's root filesystem is
  writable outside `/app`. The uid-10001 user is what limits the damage, not
  filesystem immutability.
- `pids_limit` caps process *count*, not CPU. The shell tool has no timeout of
  its own, so a single long-running command can still spin until you stop it.

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
├── config.example.json      # Template for config.json (gitignored)
├── requirements.txt         # Pinned Python dependencies
├── SECURITY.md              # Trust model and boundaries
├── Dockerfile               # Container image
├── docker-compose.yml       # Sandboxed run configuration
├── venv/                    # Virtual environment (not committed)
│
├── agent/                   # Agent core logic
│   ├── core.py              # Tool class + Agent with reasoning loop
│   ├── prompt.py            # System prompt assembly (runnable: python -m agent.prompt)
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
    ├── index.html           # Page shell
    ├── scripts/             # ES modules
    │   ├── app.js           # Entry point: session state + event wiring
    │   ├── api.js           # All backend calls
    │   ├── stream.js        # NDJSON reader
    │   ├── ui.js            # DOM rendering
    │   ├── history.js       # Conversation sidebar
    │   ├── settings.js      # Preferences dialog
    │   ├── background.js    # Custom log background (IndexedDB)
    │   └── config.js        # Endpoint + storage-key constants
    ├── styles/main.css      # Theme and layout
    └── vendor/              # marked.js, DOMPurify (vendored, not CDN)
```

## API Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/chat/stream` | Send a turn, stream the reasoning loop back |
| `POST` | `/new-session` | Create an empty session, return its id |
| `GET` | `/sessions` | List all sessions, newest first |
| `GET` | `/sessions/{id}/messages` | Full stored message list for one session |
| `POST` | `/sessions/{id}` | **Destroy** that session and its messages |
| `POST` | `/clear` | Clear one session's messages, keep the session |
| `POST` | `/destroy_all` | Destroy every session and message |
| `GET` | `/health` | Liveness probe |

Note the verb on `POST /sessions/{id}`: destroying a session is a POST, not a
DELETE.

Start the app with `python main.py` — that is where the lifespan hook and the
static mount are wired up.

### `POST /chat/stream`

The endpoint the web UI uses. Responds with `application/x-ndjson` — one JSON
object per line, emitted as the reasoning loop runs, so assistant messages and
tool results appear incrementally.

**Request body:**
```json
{
  "messages": [
    { "role": "user", "content": "List files in /tmp" }
  ],
  "session_id": "optional-session-id"
}
```

Omitting `session_id` creates a new session — but the response body carries only
messages, so there is no way to learn the generated id. Create the session up
front with `POST /new-session` if you intend to resume it, which is what the web
UI does.

Each line is an agent message (`role` of `assistant`, `tool`, or `system`).
Assistant messages may carry a `tool_calls` array; tool results carry
`tool_call_id`, `command` and `tool_name`. The stream ends with a sentinel:

```json
{"_final": true, "response": "Here are the files...", "history": [...]}
```

### `POST /clear`

Clears one session's messages and keeps the session row itself.

```json
{ "session_id": "the-session-id" }
```
```json
{"success": true}
```

`success: false` means nothing was cleared.

### `POST /destroy_all`

Destroys every session and every message in the database.

```json
{"destroyed": 42}
```

> ⚠️ **Irreversible.** There is no backup and no undo, and the endpoint is
> unauthenticated — see [SECURITY.md](SECURITY.md#data-loss). Back up
> `data/agent.db` if the history matters to you.

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
- `POST /destroy_all` **irreversibly** deletes all history, and the endpoint is
  unauthenticated. Back up `data/agent.db` if it matters to you.

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
