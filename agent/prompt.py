"""System prompt construction.

The prompt is assembled once per process (see api.main.SYSTEM_PROMPT) and the
same string is handed to every Agent the instance creates. Nothing here is
per-session or per-turn: anything that varies between sessions belongs in the
conversation, not in history[0].

Sections are built independently and joined with blank lines, so an empty
section simply drops out rather than leaving a stray heading behind.
"""

import platform
import re
import sys
from pathlib import Path

import config
from agent.tools import (
    COMMAND_TIMEOUT_SECONDS,
    COMMAND_WHITELIST,
    MAX_OUTPUT_CHARS,
)

# Resolved here rather than in api.main so that the prompt and everything that
# configures it live together — api.main imports these, and `python -m
# agent.prompt` gets the same values without a circular import.
AGENT_NAME = config.get("agent", "name", "AGENT_NAME", "Pygent")
AGENT_PERSONA = config.get(
    "agent", "persona", "AGENT_PERSONA",
    "An AI assistant designed to be helpful."
)
# Free-form operator text appended to the end of the system prompt, after the
# built-in sections, so it can override them.
AGENT_INSTRUCTIONS = config.get("agent", "instructions", "AGENT_INSTRUCTIONS", "")


def _in_container() -> bool:
    """Same probe main.py uses for its bind-address warning."""
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


def _identity(name: str, persona: str) -> str:
    """Who the agent is. `persona` is operator-supplied and used verbatim."""
    lines = [f"You are {name}."]
    persona = (persona or "").strip()
    if persona:
        lines.append(persona)
    return "\n\n".join(lines)


def _environment() -> str:
    """Facts about the machine that are fixed for the life of the process."""
    facts = [
        f"- Operating system: {platform.system()} {platform.release()} ({platform.machine()})",
        f"- Python: {platform.python_version()}",
        f"- Working directory: {Path.cwd()}",
    ]
    if _in_container():
        facts.append(
            "- You are running inside a container. The filesystem you can see is "
            "the container's, not the operator's host."
        )
    return "# Environment\n\n" + "\n".join(facts)


def _shell_policy() -> str:
    """The run_shell_command rules.

    The tool's JSON schema tells the model the tool exists; it does not tell it
    which commands will actually survive the whitelist. Without this the model
    burns turns discovering that `rm` and `git` are refused.
    """
    allowed = ", ".join(f"`{c}`" for c in COMMAND_WHITELIST)
    return f"""# Shell access

You have one tool, `run_shell_command`. It is filtered, not sandboxed:

- Only these command names are permitted: {allowed}
- The check applies to every command in the line, not just the first — each \
stage of a pipe, and anything after `;`, `&&` or inside `$(...)`, must also be \
on that list. A permitted command's *arguments* are unrestricted, so \
`grep git README.md` is fine.
- Backticks are refused. Use `$(...)` for command substitution.
- If you need something not on the list, say so and let the operator run it \
rather than guessing at a workaround.
- Output is truncated at {MAX_OUTPUT_CHARS} characters. Narrow the command \
rather than paging through a truncated dump.
- A command that runs longer than {COMMAND_TIMEOUT_SECONDS} seconds is killed \
and returns no output at all. Scope expensive searches to a directory rather \
than starting from `/`.

Do not attempt commands outside the permitted list — they fail and cost a turn. \
If a task genuinely needs one, say so and let the operator run it."""


def _conduct() -> str:
    """How to behave. Static guidance, deliberately short."""
    return """# Conduct

- Prefer reading the system over guessing about it. One targeted command beats \
a paragraph of speculation.
- Report what the command actually returned. If it failed or returned nothing, \
say that rather than describing what you expected.
- State uncertainty plainly instead of hedging every sentence.
- Answer at the length the question deserves."""


def build_system_prompt(
    name: str,
    persona: str,
    *,
    include_environment: bool = True,
    include_shell_policy: bool = True,
    extra_instructions: str = "",
) -> str:
    """Assemble the system prompt.

    Args:
        name: Agent name.
        persona: Operator-supplied description, inserted verbatim.
        include_environment: Emit the host-facts section.
        include_shell_policy: Emit the run_shell_command rules. Set False when
            the agent is created without the shell tool registered.
        extra_instructions: Operator text appended last, so it can override
            anything above it.

    Returns:
        A single string suitable for the system message.
    """
    sections = [_identity(name, persona)]

    if include_environment:
        sections.append(_environment())
    if include_shell_policy:
        sections.append(_shell_policy())

    sections.append(_conduct())

    extra = (extra_instructions or "").strip()
    if extra:
        sections.append(f"# Additional instructions\n\n{extra}")

    return "\n\n".join(s for s in sections if s.strip())


def build_configured_prompt() -> str:
    """Build the prompt from the resolved configuration.

    This is what the server boots with; call it instead of build_system_prompt()
    unless you are deliberately overriding the configured identity.
    """
    return build_system_prompt(
        AGENT_NAME,
        AGENT_PERSONA,
        extra_instructions=AGENT_INSTRUCTIONS,
    )


if __name__ == "__main__":
    # `python -m agent.prompt` prints the prompt this machine would boot with,
    # using the same config resolution the server does.
    sys.stdout.write(build_configured_prompt() + "\n")
