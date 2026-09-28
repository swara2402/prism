# investigation/reliability_store.py
"""Per‑context agent reliability store.

Reliability is modeled as an exponential moving average (EMA) of binary
outcomes (correct/incorrect). The store persists to a JSON file in
``settings.data_dir`` (fallback if DB is not configured).

Values are clamped to the interval ``[0, 1]`` and a small decay factor
(``RELIABILITY_DECAY``) governs how quickly the EMA adapts.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, Optional, Tuple

from config.settings import settings
from config.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------
# Configuration defaults (can be overridden via Settings)
# ---------------------------------------------------------------
DEFAULT_RELIABILITY_DECAY = 0.9  # EMA decay (higher -> slower adaptation)
DEFAULT_RELIABILITY_PRIOR = 0.5  # Prior when no history exists

# ---------------------------------------------------------------
# Internal in‑memory cache – loaded lazily on first access
# ---------------------------------------------------------------
_RELIABILITY_CACHE: Dict[Tuple[str, str], float] = {}
_CACHE_LOADED = False


def _cache_path() -> Path:
    """Path to the JSON persistence file.

    ``settings.data_dir`` is guaranteed to exist (see ``config.settings``).
    """
    return settings.data_dir / "agent_reliability.json"


def _load_cache() -> None:
    global _CACHE_LOADED
    if _CACHE_LOADED:
        return
    path = _cache_path()
    if path.is_file():
        try:
            data = json.loads(path.read_text())
            for key, value in data.items():
                ctx, ag = key.split("::", 1)
                _RELIABILITY_CACHE[(ctx, ag)] = float(value)
        except Exception as exc:
            logger.warning("reliability_cache_load_failed", extra={"error": repr(exc)})
            _RELIABILITY_CACHE.clear()
    _CACHE_LOADED = True


def _persist_cache() -> None:
    path = _cache_path()
    try:
        os.makedirs(path.parent, exist_ok=True)
        data = {f"{ctx}::{ag}": val for (ctx, ag), val in _RELIABILITY_CACHE.items()}
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp_path.replace(path)
    except Exception as exc:
        logger.warning("reliability_cache_persist_failed", extra={"error": repr(exc)})


def reset_reliability_cache() -> None:
    """Clear the in-memory reliability cache so the store reloads from disk.

    Required for strict test / evaluation isolation — every scenario must
    start from the same (fresh) reliability state.
    """
    global _CACHE_LOADED
    _RELIABILITY_CACHE.clear()
    _CACHE_LOADED = False


def get_reliability(context: str, agent_name: str) -> float:
    """Return the stored reliability for ``agent_name`` in ``context``.

    If no entry exists, the default prior is returned.
    """
    _load_cache()
    ctx = context or "default"
    return _RELIABILITY_CACHE.get((ctx, agent_name), settings.agent_default_reliability)


def update_reliability(
    context: str,
    agent_name: str,
    is_correct: Optional[bool] = None,
    *,
    has_ground_truth: bool = True,
) -> None:
    """Update the EMA reliability for an agent ONLY when valid outcome / ground truth exists.

    Parameters
    ----------
    context: str
        Incident context / category (e.g. ``"network_incident"``).
    agent_name: str
        Name of agent.
    is_correct: bool
        Whether the agent matched confirmed ground truth outcome.
    has_ground_truth: bool
        Flag indicating if valid ground truth outcome is available. If False, update is skipped.
    """
    if not has_ground_truth or is_correct is None:
        # Priority 10: Do NOT update learned reliability when no valid ground truth outcome exists.
        return

    _load_cache()
    ctx = context or "default"
    key = (ctx, agent_name)
    prior = _RELIABILITY_CACHE.get(key, settings.agent_default_reliability)
    observation = 1.0 if is_correct else 0.0
    decay = getattr(settings, "agent_reliability_decay", DEFAULT_RELIABILITY_DECAY)
    updated = decay * prior + (1 - decay) * observation
    updated = max(0.0, min(1.0, updated))
    _RELIABILITY_CACHE[key] = updated
    _persist_cache()


__all__ = [
    "get_reliability",
    "update_reliability",
    "reset_reliability_cache",
]