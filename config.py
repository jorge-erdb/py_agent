"""Layered configuration.

Values are resolved in this order, first match wins:

    1. Environment variable      (LLM_BASE_URL=... python main.py)
    2. User config file          (~/.config/py_agent/config.json)
    3. Built-in default

The user config file lives outside the repository, so API keys never land in
git and local tweaks never show up in `git status`. Environment variables win
over the file so container runtimes can override without touching the host.
"""

import json
import logging
import os
import stat
from pathlib import Path

logger = logging.getLogger(__name__)


def config_path() -> Path:
    """Location of the user config file, honouring XDG_CONFIG_HOME."""
    base = os.getenv("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "py_agent" / "config.json"


def _warn_if_readable_by_others(path: Path) -> None:
    """Config files holding an API key should not be world/group readable."""
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    if mode & (stat.S_IRGRP | stat.S_IROTH):
        logger.warning(
            "%s is readable by other users. If it contains an API key, run: chmod 600 %s",
            path, path,
        )


def _load() -> dict:
    """Read the user config file. Missing or malformed files are non-fatal."""
    path = config_path()
    if not path.is_file():
        return {}
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as e:
        logger.warning("Ignoring %s — invalid JSON: %s", path, e)
        return {}
    except OSError as e:
        logger.warning("Ignoring %s — %s", path, e)
        return {}

    if not isinstance(data, dict):
        logger.warning("Ignoring %s — expected a JSON object at the top level", path)
        return {}

    if isinstance(data.get("llm"), dict) and data["llm"].get("api_key"):
        _warn_if_readable_by_others(path)

    logger.info("Loaded config from %s", path)
    return data


_FILE = _load()


def get(section: str, key: str, env: str, default, cast=str):
    """Resolve one setting: environment, then config file, then default.

    Args:
        section: Top-level object in the config file, e.g. "llm".
        key: Key within that section.
        env: Environment variable that overrides both.
        default: Value used when neither source supplies one.
        cast: Applied to the resolved value; falls back to `default` on failure.
    """
    raw = os.getenv(env)
    if raw is None:
        raw = _FILE.get(section, {}).get(key) if isinstance(_FILE.get(section), dict) else None
    if raw is None:
        return default

    try:
        return cast(raw)
    except (TypeError, ValueError):
        logger.warning("Bad value for %s.%s (%r) — using default %r", section, key, raw, default)
        return default
