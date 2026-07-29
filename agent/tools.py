import asyncio
import logging
import shlex
import re

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# Simple whitelist – only the *command name* (no arguments) is checked.
# ----------------------------------------------------------------------
COMMAND_WHITELIST = [
    "cat",
    "find",
    "ls",
    "sed",
    "grep",
    "head",
    "tail",
    "wc",
    "diff",
    "stat",
    "file",
    "pwd",
    "echo",
    "which",
    "tree",
    "rg",
    "python3",
    "sqlite3",
]

# ----------------------------------------------------------------------
# Simple blacklist – patterns that are never allowed (checked on the full
# command line).  These are examples; you can add more if needed.
# ----------------------------------------------------------------------
BLACKLIST_PATTERNS = [
    r"\brm\b",          # any delete
    r"\bdd\b\s+if=",    # raw disk writes
    r"\bsudo\b",        # privilege escalation
    r"\bgit\b",         # VCS ops reserved for the human operator
]

# Maximum length of output that will be returned to the caller.
MAX_OUTPUT_CHARS = 6000

def _is_allowed(command: str) -> tuple[bool, str]:
    """
    Return (True, '') if the command is allowed, otherwise (False, error_msg).

    Whitelist check: only the first token (the command name) must be in
    COMMAND_WHITELIST.
    Blacklist check: the whole command string must not match any pattern.
    """
    if not command or not command.strip():
        return False, "Empty command."

    try:
        parts = shlex.split(command)
    except ValueError:
        return False, "Invalid command syntax."

    cmd_name = parts[0]

    # Whitelist – only the command name matters
    if cmd_name not in COMMAND_WHITELIST:
        return False, f"'{cmd_name}' command blocked."

    # Blacklist – scan the full command string
    for pat in BLACKLIST_PATTERNS:
        if re.search(pat, command):
            return False, f"'{command}' blocked."

    return True, ""

async def run_shell_command(command: str) -> str:
    """
    Executes a shell command and returns the output.
    Whitelisted commands are allowed; a simple blacklist protects against
    dangerous commands such as `rm -rf` or `dd if=/dev/zero`.
    No sandboxing, timeout or output truncation is applied.
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
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()

        output = stdout.decode()
        error = stderr.decode()

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