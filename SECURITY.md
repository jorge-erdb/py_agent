# Security Policy

This document describes the trust model behind py_agent and where its boundaries
lie. Read it before exposing the agent to anything you care about.

## Trust Model

py_agent runs an LLM-driven agent that executes shell commands on the machine
hosting it. It runs **within the security boundary of the user account that
started it**, and treats everything that account can reach as inside that
boundary.

There is deliberately **no sandbox**. Containing the agent — with a container, a
VM, a dedicated user account, or nothing at all — is the operator's decision. The
repository ships a Docker Compose setup that provides a reasonable default
containment, but using it is optional and it is not a supported security
guarantee.

If you can write to the files py_agent reads — its config, its database, the
directories it inspects — you can influence its behaviour. That is expected, not
a vulnerability.

## Shell Tool — No Whitelist

`agent/tools.py` executes commands directly through a full shell (`/bin/sh`).
There is **no whitelist, no blacklist, and no filtering**. The tool runs whatever
command the agent sends it: pipes, redirection, `;`, `&&`, `$(...)`, everything.
The only constraints are the user account's permissions and whatever containment
the operator has put in place (e.g. Docker).

There is **no timeout and no resource limit** on command execution at the tool
level. The shell tool documentation notes a 300-second kill signal for runaway
commands, but this is a courtesy to prevent session wedging — not a security
control.

Treat any filesystem the agent can reach as reachable by whoever is talking to it.

## Network Exposure

`SERVER_HOST` defaults to `0.0.0.0`, which binds every interface. The server logs
a warning at startup when this is in effect.

There is **no authentication on any endpoint**, and CORS is set to `*` when
enabled. Anyone who can reach the port can run commands through the agent, and
any web page you visit can issue requests to it on localhost.

Set `SERVER_HOST=127.0.0.1` unless you specifically intend network access. Never
expose this to the public internet.

## Prompt Injection

The agent will act on text it reads. A file, a filename, a database row, or a web
page containing instructions can redirect it. **This cannot be prevented** while
the agent has both tool access and the ability to read untrusted input.

Do not point the agent at content you do not trust.

## Data Loss

`POST /destroy_all` destroys every session and every message in the database. This
is **irreversible and by design** — the endpoint exists so the operator can wipe
everything deliberately.

`POST /clear` with `{"session_id": "..."}` clears one session's messages while
keeping the session row. It is also irreversible for that session — there is no
undo.

Understand what that means before calling them:

- **There is no backup and no undo.** Nothing is dumped before the delete.
- **Deleted rows are not recoverable from the database file.** SQLite is typically
  built with `secure_delete` enabled, which zeroes freed pages instead of merely
  unlinking them. Forensic carving of the file will return nothing.
- Conversation history is the only copy. If you want it to survive, back up
  `data/agent.db` yourself — `sqlite3 data/agent.db ".backup data/agent.db.bak"`.

## Secrets

- `config.json` at the repository root may hold real API keys. **Nothing checks
  its permissions for you** — a fresh copy of `config.example.json` is created
  `-rw-r--r--`, readable by every account on the machine. If you put a real key
  in it, run:

  ```bash
  chmod 600 config.json
  ```

  The file is gitignored and dockerignored, so it is never committed and never
  baked into an image. Under compose, configure the container through the
  `environment:` block instead — environment variables outrank the file.
- If you mount the directory containing this repository into the container
  read-only (the default compose file mounts `${HOME}/Projects`), then
  `config.json` is inside that mount and the agent can `cat` its own API key.
  Keep the checkout outside any mounted path, or accept that the key is
  readable by the model.
- Conversation history — including full command output — is stored unencrypted in
  `data/agent.db`. Anything the agent reads may be persisted there.
- The agent can read any file its user account can read, and repeat the contents
  into the chat window.

## In Scope

Reports that demonstrate a boundary crossing py_agent itself grants:

- Remote code execution reachable without any prior local access
- Escape from the shipped Docker configuration to the host
- Secrets leaking to a destination other than the configured LLM endpoint
- Vulnerabilities in a pinned dependency that are reachable through this code

## Out Of Scope

- **Sandbox behaviour of the shell tool.** There is intentionally no sandbox; the
  shell tool executes commands directly.
- **Prompt injection**, and any consequence of the model acting on untrusted input.
- **Network exposure of a py_agent instance**, including the `0.0.0.0` default and
  the absence of authentication — both are documented here.
- **Malicious or unexpected model output.**
- Anything requiring the ability to modify local state the agent trusts: its
  config file, its database, or files in directories it has been pointed at.
- Resource exhaustion or denial of service caused by operator-supplied config or
  by the model looping.
- **Irreversible history deletion through `POST /destroy_all` or `POST /clear`.**
  These are intended behaviour, documented under [Data Loss](#data-loss). Note that
  the endpoints are unauthenticated, so anyone who can reach the port can invoke
  them — that is a consequence of the documented lack of authentication, not a
  separate issue.
- Third-party API keys the operator supplied.

## Reporting

Open an issue at https://github.com/jorge-erdb/py_agent/issues.

Please avoid filing reports that only demonstrate documented behaviour from the
Out Of Scope list. The most useful reports show a reproducible boundary crossing
with concrete impact, against current `main`.
