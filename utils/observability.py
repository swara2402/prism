"""Small, dependency-free observability primitives for PRISM investigations."""
from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional


@dataclass
class InvestigationEvent:
    name: str
    status: str
    duration_ms: float
    attributes: Dict[str, Any] = field(default_factory=dict)


@dataclass
class InvestigationTrace:
    investigation_id: str
    tenant_id: Optional[str]
    events: List[InvestigationEvent] = field(default_factory=list)

    def add(self, name: str, status: str, duration_ms: float, **attributes: Any) -> None:
        # Never accept secrets as trace attributes.
        redacted = {k: v for k, v in attributes.items() if k.lower() not in {"password", "token", "api_key", "authorization", "cookie"}}
        self.events.append(InvestigationEvent(name, status, round(duration_ms, 2), redacted))

    def summary(self) -> Dict[str, Any]:
        return {
            "investigation_id": self.investigation_id,
            "tenant_id": self.tenant_id,
            "event_count": len(self.events),
            "failed_events": sum(e.status in {"failed", "timeout"} for e in self.events),
            "duration_ms": round(sum(e.duration_ms for e in self.events), 2),
            "events": [e.__dict__ for e in self.events],
        }


@contextmanager
def timed(trace: InvestigationTrace, name: str, **attributes: Any) -> Iterator[None]:
    started = time.perf_counter()
    status = "ok"
    try:
        yield
    except TimeoutError:
        status = "timeout"
        raise
    except Exception:
        status = "failed"
        raise
    finally:
        trace.add(name, status, (time.perf_counter() - started) * 1000, **attributes)
