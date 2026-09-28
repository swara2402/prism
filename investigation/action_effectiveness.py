"""Persistent EMA-based store for action effectiveness (expected uncertainty reduction).

The store holds a moving average of observed diagnostic gain for each
(incident_context, action_name) pair. Persistent storage is saved in
JSON format under ``settings.data_dir`` using atomic writes.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Dict, Tuple, Any

from config.settings import settings
from config.logging import get_logger
from investigation.diagnostic_gain import DiagnosticGainResult

logger = get_logger(__name__)

ALPHA = 0.8  # smoothing factor for EMA (higher -> retains prior history)
DEFAULT_EXPECTED_GAIN = 0.5

_EFFECTIVENESS_CACHE: Dict[Tuple[str, str], Dict[str, Any]] = {}
_CACHE_LOADED = False


def _store_path() -> Path:
    return settings.data_dir / "action_effectiveness.json"


def _load_cache() -> None:
    global _CACHE_LOADED
    if _CACHE_LOADED:
        return
    path = _store_path()
    if path.is_file():
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
            for key, rec in data.items():
                if "::" in key:
                    ctx, ag = key.split("::", 1)
                    if isinstance(rec, dict):
                        _EFFECTIVENESS_CACHE[(ctx, ag)] = {
                            "value": float(rec.get("value", DEFAULT_EXPECTED_GAIN)),
                            "update_count": int(rec.get("update_count", 1)),
                            "last_updated": float(rec.get("last_updated", time.time())),
                        }
                    else:
                        _EFFECTIVENESS_CACHE[(ctx, ag)] = {
                            "value": float(rec),
                            "update_count": 1,
                            "last_updated": time.time(),
                        }
        except Exception as exc:
            logger.warning("effectiveness_cache_load_failed", extra={"error": repr(exc)})
            _EFFECTIVENESS_CACHE.clear()
    _CACHE_LOADED = True


def _persist_cache() -> None:
    path = _store_path()
    try:
        os.makedirs(path.parent, exist_ok=True)
        data = {
            f"{ctx}::{ag}": rec for (ctx, ag), rec in _EFFECTIVENESS_CACHE.items()
        }
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp_path.replace(path)
    except Exception as exc:
        logger.warning("effectiveness_cache_persist_failed", extra={"error": repr(exc)})


def reset_effectiveness_cache() -> None:
    """Clear the in-memory effectiveness cache so the store reloads from disk.

    Required for strict test / evaluation isolation — every scenario must
    start from the same (fresh) effectiveness state.
    """
    global _CACHE_LOADED
    _EFFECTIVENESS_CACHE.clear()
    _CACHE_LOADED = False


def get_expected_effectiveness(context: str, action_name: str) -> float:
    """Return the expected uncertainty reduction for ``action_name`` in ``context``."""
    _load_cache()
    rec = _EFFECTIVENESS_CACHE.get((context, action_name))
    if rec is not None:
        return rec["value"]
    return DEFAULT_EXPECTED_GAIN


def get_effectiveness_record(context: str, action_name: str) -> Dict[str, Any]:
    """Return full effectiveness metadata record for ``action_name`` in ``context``."""
    _load_cache()
    rec = _EFFECTIVENESS_CACHE.get((context, action_name))
    if rec is not None:
        return dict(rec)
    return {
        "value": DEFAULT_EXPECTED_GAIN,
        "update_count": 0,
        "last_updated": 0.0,
    }


def update_effectiveness_from_adg(context: str, action_name: str, adg_result: DiagnosticGainResult) -> None:
    """Update effectiveness using the total gain extracted from ADG result."""
    gain = getattr(adg_result, "total_gain", 0.0)
    update_effectiveness(context, action_name, float(gain))


def update_effectiveness(context: str, action_name: str, observed_gain: float) -> None:
    """Update the EMA store with a new observed diagnostic gain."""
    _load_cache()
    key = (context, action_name)
    rec = _EFFECTIVENESS_CACHE.get(key)
    now = time.time()
    observed = max(0.0, min(1.0, float(observed_gain)))

    if rec is None:
        _EFFECTIVENESS_CACHE[key] = {
            "value": observed,
            "update_count": 1,
            "last_updated": now,
        }
    else:
        old_val = rec["value"]
        new_val = ALPHA * old_val + (1 - ALPHA) * observed
        new_val = max(0.0, min(1.0, new_val))
        rec["value"] = new_val
        rec["update_count"] += 1
        rec["last_updated"] = now

    _persist_cache()


__all__ = [
    "get_expected_effectiveness",
    "get_effectiveness_record",
    "update_effectiveness",
    "update_effectiveness_from_adg",
    "reset_effectiveness_cache",
]