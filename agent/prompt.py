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

from agent.tools import BLACKLIST_PATTERNS, COMMAND_WHITELIST, MAX_OUTPUT_CHARS


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


def _describe_pattern(pattern: str) -> str:
    """Render a blacklist regex as something readable in a prompt.

    Strips the word-boundary and escape noise so `\\bsudo\\b` reads as `sudo`
    and `\\bdd\\b\\s+if=` reads as `dd if=`. Patterns that survive as
    regex-looking text are still worth showing — the point is that this list
    stays generated from the code that enforces it.
    """
    readable = re.sub(r"\\s\+?", " ", pattern)
    return re.sub(r"\\b|\\", "", readable).strip()


def _shell_policy() -> str:
    """The run_shell_command rules.

    The tool's JSON schema tells the model the tool exists; it does not tell it
    which commands will actually survive the whitelist. Without this the model
    burns turns discovering that `rm` and `git` are refused.
    """
    allowed = ", ".join(f"`{c}`" for c in COMMAND_WHITELIST)
    blocked = ", ".join(f"`{_describe_pattern(p)}`" for p in BLACKLIST_PATTERNS)
    return f"""# Shell access

You have one tool, `run_shell_command`. It is filtered, not sandboxed:

- Only these command names are permitted: {allowed}
- Only the first token is checked against that list, so pipes and redirection \
inside a permitted command still run.
- These are always refused, anywhere in the command line: {blocked}
- Output is truncated at {MAX_OUTPUT_CHARS} characters. Narrow the command \
rather than paging through a truncated dump.

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


if __name__ == "__main__":
    # `python -m agent.prompt` prints the prompt this machine would boot with.
    sys.stdout.write(build_system_prompt("Kairo", "An AI assistant.") + "\n")
