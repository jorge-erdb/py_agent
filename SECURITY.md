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

## What The Command Whitelist Is And Is Not

`agent/tools.py` checks each command against a whitelist of command names and a
blacklist of patterns. **This is a guardrail against model error, not a security
control.**

It does not contain a determined attacker, and it is not intended to:

- Commands run through a **full shell**. Pipes, redirection, `;`, `&&`, and
  `$(...)` are all live. Only the *first token* is checked against the whitelist,
  so the remainder of the line is unconstrained.
- Whitelisted interpreters — `python3`, `sqlite3` — can do essentially anything
  the user account can do, including writing files and opening sockets.
- There is **no timeout and no resource limit** on command execution.
- Blacklist patterns are string matches and can be evaded through ordinary shell
  quoting and expansion.

Treat any filesystem the agent can reach as reachable by whoever is talking to it.

## Network Exposure

`SERVER_HOST` defaults to `0.0.0.0`, which binds every interface. The server logs
a warning at startup when this is in effect.

There is **no authentication on any endpoint**, and CORS is set to `*`. Anyone who
can reach the port can run commands through the agent, and any web page you visit
can issue requests to it on localhost.

Set `SERVER_HOST=127.0.0.1` unless you specifically intend network access. Never
expose this to the public internet.

## Prompt Injection

The agent will act on text it reads. A file, a filename, a database row, or a web
page containing instructions can redirect it. **This cannot be prevented** while
the agent has both tool access and the ability to read untrusted input.

Do not point the agent at content you do not trust, and do not assume the
whitelist limits what injected instructions can accomplish.

## Data Loss

`POST /clear` with `{"all": true}` deletes every session and every message in the
database. This is **irreversible and by design** — the endpoint exists so the
operator can wipe everything deliberately.

Understand what that means before calling it:

- **There is no backup and no undo.** Nothing is dumped before the delete.
- **Deleted rows are not recoverable from the database file.** SQLite is typically
  built with `secure_delete` enabled, which zeroes freed pages instead of merely
  unlinking them. Forensic carving of the file will return nothing.
- Conversation history is the only copy. If you want it to survive, back up
  `data/agent.db` yourself — `sqlite3 data/agent.db ".backup data/agent.db.bak"`.

The endpoint requires the destructive scope to be stated explicitly (`all: true`);
a request specifying neither a `session_id` nor `all` is rejected with HTTP 400 and
deletes nothing. This exists because the endpoint is unauthenticated: anyone who
can reach the port, and any script or client that POSTs to `/clear` to check
whether the service is alive, can otherwise destroy the database. Treat `/clear`
as destructive in any tooling that touches this API.

## Secrets

- `~/.config/py_agent/config.json` may hold real API keys. Run
  `chmod 600` on it; the server warns at startup if it is group- or
  world-readable.
- Conversation history — including full command output — is stored unencrypted in
  `data/agent.db`. Anything the agent reads may be persisted there.
- The agent can read any file its user account can read, and repeat the contents
  into the chat window.

## In Scope

Reports that demonstrate a boundary crossing py_agent itself grants:

- Remote code execution reachable without any prior local access
- The whitelist being bypassed in a way that also bypasses the operator's intent
  (e.g. a parsing flaw that admits a command the operator explicitly denied)
- Escape from the shipped Docker configuration to the host
- Secrets leaking to a destination other than the configured LLM endpoint
- Vulnerabilities in a pinned dependency that are reachable through this code

## Out Of Scope

- **Sandbox behaviour of the shell tool.** There is intentionally no sandbox; the
  whitelist is documented above as advisory.
- **Prompt injection**, and any consequence of the model acting on untrusted input.
- **Network exposure of a py_agent instance**, including the `0.0.0.0` default and
  the absence of authentication — both are documented here.
- **Malicious or unexpected model output.**
- Anything requiring the ability to modify local state the agent trusts: its
  config file, its database, or files in directories it has been pointed at.
- Resource exhaustion or denial of service caused by operator-supplied config or
  by the model looping.
- **Irreversible history deletion through `POST /clear` with `all: true`.** This is
  intended behaviour, documented under [Data Loss](#data-loss). Note that the
  endpoint is unauthenticated, so anyone who can reach the port can invoke it — that
  is a consequence of the documented lack of authentication, not a separate issue.
- Third-party API keys the operator supplied.

## Reporting

Open an issue at https://github.com/jorge-erdb/py_agent/issues.

Please avoid filing reports that only demonstrate documented behaviour from the
Out Of Scope list. The most useful reports show a reproducible boundary crossing
with concrete impact, against current `main`.
