import asyncio
import logging
import os
from pathlib import Path
import signal

import config

logger = logging.getLogger(__name__)

# Maximum length of output that will be returned to the caller.
MAX_OUTPUT_CHARS = 24000

# Wall-clock limit for a single command. The agent holds its per-session lock
# for the whole of a turn, so a command that never returns — `curl` against a
# host that blackholes packets, a `find /` across a network mount — wedges that
# session permanently. Raise it if you legitimately run long scans.
COMMAND_TIMEOUT_SECONDS = config.get(
    "tools", "command_timeout", "COMMAND_TIMEOUT", 300, cast=int
)

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

    Commands are killed after COMMAND_TIMEOUT_SECONDS and output is truncated
    at MAX_OUTPUT_CHARS. There is no sandbox — only container/process-level
    containment provides security. See SECURITY.md.
    """
    if not command or not command.strip():
        logger.warning("Empty command received")
        return "Error: Empty command."

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


async def read_file(path: str) -> str:
    """Read a text file and return its contents.

    Uses Python's built-in open() — no shell execution, no subprocess.

    Binary files are detected by scanning the first 8 KiB for null bytes
    and rejected outright. Output is truncated at MAX_OUTPUT_CHARS.

    Args:
        path: Absolute or relative file path to read.

    Returns:
        File contents as a string, or an error message on failure.
    """
    if not path or not path.strip():
        logger.warning("Empty file path received")
        return "Error: Empty path."

    try:
        resolved = Path(path).resolve()
    except (OSError, ValueError) as e:
        logger.warning("Cannot resolve path %r: %s", path, e)
        return f"Error: cannot resolve path — {e}"

    # --- existence / type checks (fast, no content read) ---
    try:
        if not resolved.exists():
            return f"Error: file not found — {resolved}"
        if resolved.is_dir():
            return f"Error: path is a directory — {resolved}"
    except OSError as e:
        logger.warning("Cannot stat %r: %s", resolved, e)
        return f"Error: cannot access path — {e}"

    # --- binary detection on the first 8 KiB ---
    try:
        with open(resolved, "rb") as fh:
            header = fh.read(8192)
    except PermissionError:
        return f"Error: permission denied — {resolved}"
    except OSError as e:
        logger.warning("Cannot read %r: %s", resolved, e)
        return f"Error: cannot read file — {e}"

    if b"\x00" in header:
        # Check the rest of the file too for a more accurate report
        try:
            size = resolved.stat().st_size
            return (
                f"Error: binary file detected — {resolved} "
                f"({size:,} bytes). Use run_shell_command to inspect it."
            )
        except OSError:
            return f"Error: binary file detected — {resolved}"

    # --- read full text ---
    try:
        with open(resolved, encoding="utf-8", errors="replace") as fh:
            content = fh.read()
    except PermissionError:
        return f"Error: permission denied — {resolved}"
    except OSError as e:
        logger.warning("Cannot read %r: %s", resolved, e)
        return f"Error: cannot read file — {e}"

    result = content.rstrip("\n") or "File is empty."
    logger.info("Read %d chars from %s", len(content), resolved)

    # Truncate if needed.
    if len(result) > MAX_OUTPUT_CHARS:
        omitted = len(result) - MAX_OUTPUT_CHARS
        result = (
            result[:MAX_OUTPUT_CHARS]
            + f"\n... [truncated, {omitted:,} chars omitted — "
            f"use run_shell_command with head/tail/sed to explore portions]"
        )

    return result


async def write_file(path: str, content: str) -> str:
    """Write text content to a file atomically.

    Uses a temp file in the same directory followed by os.replace() so an
    interrupted write never corrupts the target.  Parent directories are
    created automatically.

    Args:
        path:   Absolute or relative file path to write.
        content: Text content to write.

    Returns:
        A confirmation message with resolved path and byte count, or an
        error string on failure.
    """
    if not path or not path.strip():
        logger.warning("Empty file path received")
        return "Error: Empty path."

    if content is None:
        logger.warning("None content received for write_file")
        return "Error: content must be a string."

    try:
        resolved = Path(path).resolve()
    except (OSError, ValueError) as e:
        logger.warning("Cannot resolve path %r: %s", path, e)
        return f"Error: cannot resolve path — {e}"

    # --- ensure parent directory exists ---
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning("Cannot create parent dir for %r: %s", resolved, e)
        return f"Error: cannot create parent directory — {e}"

    # --- write atomically via temp file + rename ---
    tmp_path = resolved.with_suffix(resolved.suffix + ".tmp")

    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(str(tmp_path), str(resolved))
    except PermissionError:
        # Clean up the temp file so it doesn't linger.
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        return f"Error: permission denied — {resolved}"
    except OSError as e:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        logger.warning("Cannot write %r: %s", resolved, e)
        return f"Error: cannot write file — {e}"

    byte_count = len(content.encode("utf-8"))
    logger.info("Wrote %d bytes to %s", byte_count, resolved)
    return f"Successfully wrote {byte_count:,} bytes to {resolved}"
