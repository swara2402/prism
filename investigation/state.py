from dataclasses import dataclass, field
from typing import Dict, List, Any, Optional


@dataclass
class InvestigationState:
    """Central representation of the current diagnostic investigation.

    This object is intended to be passed between the orchestration loop,
    the marginal‑diagnostic‑value engine, and the consensus engine.
    """

    hypotheses: Dict[str, float] = field(default_factory=dict)
    hypothesis_uncertainty: float = 0.0
    agent_disagreement: float = 0.0
    evidence_coverage: float = 0.0
    causal_consistency: Optional[float] = None
    current_confidence: float = 0.0
    executed_actions: List[Dict[str, Any]] = field(default_factory=list)
    remaining_actions: List[Dict[str, Any]] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def recompute_metrics(self) -> None:
        """Recompute all state metrics from current state attributes and metadata."""
        from investigation.metrics import compute_metrics
        m = compute_metrics(self)
        self.hypothesis_uncertainty = m.get("entropy") or 0.0
        self.agent_disagreement = m.get("hypothesis_disagreement") or 0.0
        self.evidence_coverage = m.get("evidence_coverage") or 0.0
        cs = m.get("causal_consistency")
        self.causal_consistency = cs

        if self.hypotheses:
            total = sum(self.hypotheses.values())
            if total > 0:
                self.current_confidence = max(self.hypotheses.values()) / total
            else:
                self.current_confidence = 0.0
        else:
            self.current_confidence = 0.0

    def update_from_finding(self, finding: Dict[str, Any], context: Dict[str, Any] | None = None) -> None:
        """Update diagnostic state from a newly executed agent finding."""
        context = context or {}
        findings_list = self.metadata.setdefault("findings", [])
        findings_list.append(finding)

        # Update evidence presence map
        evidence_map = self.metadata.setdefault("evidence_present", {})
        if context:
            for k in ["logs", "metrics", "traces", "topology"]:
                if context.get(k):
                    evidence_map[k] = True
        ev = finding.get("evidence", {}) or {}
        if ev:
            for k in ["logs", "metrics", "traces", "topology"]:
                if ev.get(k):
                    evidence_map[k] = True

        # Update hypothesis distribution dynamically
        hint = finding.get("root_cause_hint") or finding.get("description") or ""
        conf = float(finding.get("confidence", 0.5))

        if hint:
            hint_norm = hint.lower().strip()
            # Check for existing matching hypothesis or update
            found_match = False
            for h in list(self.hypotheses.keys()):
                if h.lower().strip() in hint_norm or hint_norm in h.lower().strip():
                    self.hypotheses[h] += conf
                    found_match = True
                    break
            if not found_match:
                self.hypotheses[hint] = conf

        # Normalize hypothesis probabilities
        if self.hypotheses:
            tot = sum(self.hypotheses.values())
            if tot > 0:
                for k in self.hypotheses:
                    self.hypotheses[k] = self.hypotheses[k] / tot

        self.recompute_metrics()

    def update_remaining_actions(self, actions: List[Dict[str, Any]]) -> None:
        """Update remaining action pool."""
        self.remaining_actions = list(actions)

    def pop_action(self, agent_name: str, result_payload: Dict[str, Any] | None = None) -> Dict[str, Any] | None:
        """Remove an action from remaining_actions and record it in executed_actions."""
        match_idx = None
        target_act = None
        for idx, act in enumerate(self.remaining_actions):
            if act.get("agent_name") == agent_name:
                match_idx = idx
                target_act = act
                break
        if match_idx is not None:
            self.remaining_actions.pop(match_idx)
        else:
            target_act = {"agent_name": agent_name}

        executed_entry = dict(target_act)
        if result_payload:
            executed_entry["result"] = result_payload
        self.executed_actions.append(executed_entry)
        return target_act


