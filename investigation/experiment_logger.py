# investigation/experiment_logger.py
"""Simple JSONL experiment logger.

If ``settings.PRISM_EXPERIMENT_LOGGING`` (environment variable
``PRISM_EXPERIMENT_LOGGING``) is truthy, each call writes a line to a
per‑incident log file under ``settings.data_dir / "experiments"``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from config.settings import settings
from config.logging import get_logger

logger = get_logger(__name__)

# Resolve the directory lazily. Importing this optional logger must never
# require filesystem write access in a production container.
_LOG_DIR: Path | None = None

def _log_dir() -> Path:
    global _LOG_DIR
    if _LOG_DIR is None:
        _LOG_DIR = settings.data_dir / "experiments"
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
    return _LOG_DIR


def _log_path(incident_id: str) -> Path:
    """Return the Path to the JSONL log file for *incident_id*."""
    return _log_dir() / f"{incident_id}.jsonl"


def log_iteration(info: Dict[str, Any]) -> None:
    """Append *info* as a JSON line to the incident's experiment log.

    Logging is disabled when ``settings.PRISM_EXPERIMENT_LOGGING`` is falsy.
    Any I/O errors are ignored so that logging never interferes with the
    investigation loop.
    """
    if not getattr(settings, "PRISM_EXPERIMENT_LOGGING", False):
        return
    try:
        line = json.dumps(info, ensure_ascii=False)
        with open(_log_path(info.get("incident_id", "unknown")), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as exc:
        logger.warning("experiment_log_write_failed", extra={"error": repr(exc)})

__all__ = ["log_iteration"]