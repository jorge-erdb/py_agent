import asyncio
import logging
import os
import re
import shlex
import signal

import config

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# Allowed command names, checked at every *command position* in the line —
# the first word, and anything the shell will execute after `|`, `;`, `&&`,
# `$( )` and friends. See _command_positions().
#
# There is no blacklist. A blacklist has to match the raw text, which cannot
# tell an executed word from a piece of data: `\brm\b` refuses `cat rm.txt`
# and `grep git README.md` while doing nothing about
# `python3 -c "shutil.rmtree(...)"`. Denying by position instead of by
# spelling costs no capability and misfires on nothing.
#
# This is a guardrail against model error, not a security control. `python3`
# and `sqlite3` alone make it fully bypassable, deliberately — the operator's
# containment is the boundary. See SECURITY.md.
# ----------------------------------------------------------------------
COMMAND_WHITELIST = [
    # Inspecting files and directories
    "cat", "find", "ls", "head", "tail", "stat", "file", "tree", "du", "df",
    "wc", "diff", "cmp", "comm", "realpath", "basename", "dirname", "touch",
    "mkdir",
    # Searching and transforming text
    "grep", "rg", "sed", "awk", "sort", "uniq", "cut", "tr", "tac", "rev",
    "nl", "column", "paste", "fold", "jq",
    # Shell and environment basics
    "pwd", "echo", "printf", "which", "env", "date", "seq", "uname",
    "whoami", "id", "hostname", "ps", "sleep", "ps", "pgrep", "sleep",
    "kill", "source",
    # Checksums
    "md5sum", "sha256sum",
    # Interpreters and network. These can do anything the account can do;
    # they are here because they are genuinely useful, not because they
    # are safe.
    "python3", "sqlite3", "curl", "wget",
]

# Tokens after which the shell starts a *new* command. Redirection operators
# (`>`, `>>`, `<`, `&>`, `<<<`) are deliberately absent: what follows them is a
# filename, and treating `out.txt` in `echo hi > out.txt` as a command name
# would reject every redirect.
COMMAND_SEPARATORS = frozenset({
    "|", "||", "|&", "&&", ";", ";;", "&", "(", ")", "{", "}", "!", "\n",
})

# `FOO=bar cmd ...` — assignments precede the command name at a command
# position, so they are stepped over rather than mistaken for the command.
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# Maximum length of output that will be returned to the caller.
MAX_OUTPUT_CHARS = 6000

# Wall-clock limit for a single command. The agent holds its per-session lock
# for the whole of a turn, so a command that never returns — `curl` against a
# host that blackholes packets, a `find /` across a network mount — wedges that
# session permanently. Raise it if you legitimately run long scans.
COMMAND_TIMEOUT_SECONDS = config.get(
    "tools", "command_timeout", "COMMAND_TIMEOUT", 300, cast=int
)

def _command_positions(tokens: list[str]) -> list[str]:
    """Return the tokens the shell will treat as command names.

    That is the first word, plus whatever follows a separator in
    COMMAND_SEPARATORS. Everything else is data — arguments, redirect targets,
    search patterns — and is not a command no matter what it spells.
    """
    names: list[str] = []
    expect_command = True

    for token in tokens:
        if token in COMMAND_SEPARATORS:
            expect_command = True
            continue

        if not expect_command:
            continue

        # `$` is left as its own token by the lexer in `$(cmd)`; the real
        # command name is past the `(` that follows it.
        if token == "$" or _ASSIGNMENT.match(token):
            continue

        names.append(token)
        expect_command = False

    return names


def _is_allowed(command: str) -> tuple[bool, str]:
    """
    Return (True, '') if the command is allowed, otherwise (False, error_msg).

    Every command position must name something in COMMAND_WHITELIST, so
    `ls | rm -rf /` is refused on `rm` even though `ls` opens the line, while
    `grep git README.md` runs — `git` there is a search string, not a command.
    """
    if not command or not command.strip():
        return False, "Empty command."

    # Backticks are the one construct the lexer will not break apart: it
    # yields "`rm" as a single token, hiding the command inside it. `$(...)`
    # is the modern spelling and tokenizes correctly, so nothing is lost.
    if "`" in command:
        return False, "Backticks are not supported — use $(...) instead."

    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return False, "Invalid command syntax."

    names = _command_positions(tokens)
    if not names:
        return False, "No command found."

    for name in names:
        if name not in COMMAND_WHITELIST:
            return False, f"'{name}' command blocked. Ask the user to execute."

    return True, ""

async def _kill_process_group(process: asyncio.subprocess.Process) -> None:
    """Kill a timed-out command and everything it spawned.

    `create_subprocess_shell` runs the command under `/bin/sh`, and a pipeline
    or a backgrounded child produces further processes. Signalling `process`
    alone would kill the shell and orphan the rest — which keeps holding the
    stdout pipe. `start_new_session=True` puts the whole lot in one process
    group so it can be signalled as a unit.

    SIGTERM first so the command can clean up, SIGKILL for anything that
    ignores it.
    """
    try:
        pgid = os.getpgid(process.pid)
    except ProcessLookupError:
        return  # already gone

    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return
        try:
            # Also reaps the process, so it does not linger as a zombie.
            await asyncio.wait_for(process.wait(), timeout=2)
            return
        except asyncio.TimeoutError:
            continue

    logger.error("Process group %d survived SIGKILL", pgid)


async def run_shell_command(command: str) -> str:
    """
    Executes a shell command and returns the output.

    Whitelisted commands are allowed; a simple blacklist refuses patterns such
    as `rm` and `sudo`. Commands are killed after COMMAND_TIMEOUT_SECONDS and
    output is truncated at MAX_OUTPUT_CHARS.

    None of that is a sandbox. Only the first token is checked against the
    whitelist, so anything reachable through a permitted command still runs.
    The container is the actual boundary — see SECURITY.md.
    """
    if not command or not command.strip():
        logger.warning("Empty command received")
        return "Error: Empty command."

    allowed, msg = _is_allowed(command)
    if not allowed:
        logger.warning("Command blocked: %s", msg)
        # The “graceful” message now contains the actual reason
        return f"Error: {msg}"

    try:
        logger.info("Executing command: %s", command)
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Own process group, so a timeout can take down the whole pipeline
            # rather than just the shell that fronts it.
            start_new_session=True,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=COMMAND_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            await _kill_process_group(process)
            logger.warning(
                "Command timed out after %ds: %s", COMMAND_TIMEOUT_SECONDS, command
            )
            # Reported as a normal tool result, not raised: the model should see
            # the timeout and narrow the command rather than the turn dying.
            return (
                f"Error: command exceeded the {COMMAND_TIMEOUT_SECONDS}s time limit "
                "and was killed. No output was captured. Narrow the command or "
                "restrict it to a smaller path."
            )

        output = stdout.decode(errors="replace")
        error = stderr.decode(errors="replace")

        if process.returncode == 0:
            logger.info("Command succeeded: %s", command)
            result = output.rstrip("\n") or "Command executed successfully (no output)."
        else:
            logger.warning("Command failed (exit %d): %s", process.returncode, error)
            result = f"Error (exit code {process.returncode}): {error.rstrip(chr(10))}"

        # Truncate result if it exceeds the allowed size.
        if len(result) > MAX_OUTPUT_CHARS:
            truncate_msg = f"\n... [truncated, {len(result) - MAX_OUTPUT_CHARS} chars omitted — narrow your command]"
            result = result[:MAX_OUTPUT_CHARS] + truncate_msg

        return result

    except Exception as e:
        logger.error("Command execution error: %s", e)
        result = f"Failed to execute command: {str(e)}"
        if len(result) > MAX_OUTPUT_CHARS:
            truncate_msg = f"\n... [truncated, {len(result) - MAX_OUTPUT_CHARS} chars omitted — narrow your command]"
            result = result[:MAX_OUTPUT_CHARS] + truncate_msg
        return result

# async def [read tool using 'cat -n']