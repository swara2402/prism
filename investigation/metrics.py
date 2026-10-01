from typing import Dict, Any, List, Optional
import math
from utils.text import jaccard_similarity, tokenize


def shannon_entropy(hypotheses: Dict[str, float]) -> float:
    """Calculate Shannon entropy normalized to [0, 1].

    Divides raw Shannon entropy by log2(N) where N is the number of hypotheses.
    """
    if not hypotheses:
        return 0.0
    # Hypothesis weights must form a valid non-negative distribution.
    # Negative/NaN/inf scores are invalid evidence, not probabilities.
    clean = []
    for raw in hypotheses.values():
        try:
            p = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(p) and p > 0:
            clean.append(p)
    total = sum(clean)
    if total <= 0:
        return 0.0
    probs = [p / total for p in clean]
    num_h = len(probs)
    if num_h <= 1:
        return 0.0
    raw_entropy = -sum(p * math.log2(p) for p in probs)
    max_entropy = math.log2(num_h)
    norm = raw_entropy / max_entropy if max_entropy > 0 else 0.0
    return max(0.0, min(1.0, norm))


def agent_disagreement(state: Any) -> float:
    """Calculate agent disagreement from actual differences between executed agent findings.

    Returns pairwise average Jaccard distance between root cause hints / finding descriptions.
    Normalized to [0, 1].
    """
    findings: List[Dict[str, Any]] = []
    if hasattr(state, "executed_actions") and state.executed_actions:
        for act in state.executed_actions:
            if isinstance(act, dict) and "result" in act and isinstance(act["result"], dict):
                findings.append(act["result"])
            elif isinstance(act, dict) and ("agent_name" in act or "root_cause_hint" in act):
                findings.append(act)
    if not findings and hasattr(state, "metadata") and isinstance(state.metadata, dict):
        findings = state.metadata.get("findings", [])

    hints = [f.get("root_cause_hint") or f.get("description") or "" for f in findings if f]
    hints = [h.strip().lower() for h in hints if h.strip()]
    if len(hints) < 2:
        return 0.0

    distances = []
    for i in range(len(hints)):
        tokens_i = set(tokenize(hints[i]))
        for j in range(i + 1, len(hints)):
            tokens_j = set(tokenize(hints[j]))
            if not tokens_i or not tokens_j:
                dist = 1.0 if hints[i] != hints[j] else 0.0
            else:
                sim = jaccard_similarity(tokens_i, tokens_j)
                dist = 1.0 - sim
            distances.append(dist)

    if not distances:
        return 0.0
    avg_dist = sum(distances) / len(distances)
    return max(0.0, min(1.0, avg_dist))


CONTEXT_EVIDENCE_MAP: Dict[str, List[str]] = {
    "high_latency": ["metrics", "traces", "logs"],
    "database_error": ["logs", "metrics"],
    "cascading_failure": ["topology", "traces", "metrics"],
    "memory_leak": ["metrics", "logs"],
    "network_partition": ["topology", "traces", "metrics"],
    "cpu_spike": ["metrics", "logs"],
    "default": ["logs", "metrics", "traces", "topology"],
}


def get_required_evidence(
    incident_type: Optional[str] = None,
    hypotheses: Optional[Dict[str, float]] = None,
) -> List[str]:
    """Derive required evidence types dynamically based on incident context and hypotheses.

    1. Match incident_type against CONTEXT_EVIDENCE_MAP if known.
    2. Query AGENT_REGISTRY to include evidence required by agents targeting top hypotheses.
    3. Return a deduplicated list of required evidence keys.
    """
    req_set = set()
    inc_type = (incident_type or "").lower().strip()
    if inc_type in CONTEXT_EVIDENCE_MAP:
        req_set.update(CONTEXT_EVIDENCE_MAP[inc_type])
    else:
        req_set.update(CONTEXT_EVIDENCE_MAP["default"])

    if hypotheses:
        top_hyps = [
            h.lower().strip()
            for h, p in sorted(hypotheses.items(), key=lambda kv: kv[1], reverse=True)[:2]
            if p > 0.0
        ]
        if top_hyps:
            try:
                from orchestrator.agent_registry import AGENT_REGISTRY

                for cap in AGENT_REGISTRY:
                    supported = [sh.lower().strip() for sh in cap.supported_hypotheses]
                    if any(th in supported for th in top_hyps):
                        req_set.update(cap.supported_evidence)
            except ImportError:
                pass

    return sorted(list(req_set))


def evidence_coverage(state: Any) -> float:
    """Return proportion of required evidence keys that are present.

    Evidence requirements are context- and hypothesis-aware when not explicitly overridden
    in state metadata['required_evidence'].
    """
    metadata = getattr(state, "metadata", {}) or {}
    evidence_map = metadata.get("evidence_present", {})

    if "required_evidence" in metadata:
        required = metadata["required_evidence"]
    else:
        inc_type = metadata.get("incident_type")
        hyps = getattr(state, "hypotheses", {}) or {}
        required = get_required_evidence(inc_type, hyps)

    if not required:
        return 0.0

    present_count = 0
    for key in required:
        if evidence_map.get(key, False):
            present_count += 1
    return max(0.0, min(1.0, present_count / len(required)))


def causal_consistency(state: Any) -> Optional[float]:
    """Calculate causal consistency score measuring alignment between findings/hypotheses and causal topology.

    Returns:
    - float in [0.0, 1.0]: when a causal graph or findings allow computing topological consistency.
    - None: when no causal graph or findings exist, preserving unmeasurability without false 0.0 penalties.
    """
    metadata = getattr(state, "metadata", {}) or {}

    # 1. Direct metadata override or pre-computed score
    causal_score = metadata.get("causal_consistency")
    if causal_score is not None:
        try:
            return max(0.0, min(1.0, float(causal_score)))
        except (ValueError, TypeError):
            pass

    # 2. Causal graph object in metadata
    cg_obj = metadata.get("causal_graph")
    if cg_obj is not None and hasattr(cg_obj, "find_root_causes"):
        try:
            candidates = cg_obj.find_root_causes(top_k=3)
            if candidates:
                top_score = candidates[0].get("score") or candidates[0].get("confidence") or 0.5
                return max(0.0, min(1.0, float(top_score)))
        except Exception:
            pass

    # 3. Build graph from findings
    findings = []
    if hasattr(state, "executed_actions") and state.executed_actions:
        for act in state.executed_actions:
            if isinstance(act, dict) and "result" in act and isinstance(act["result"], dict):
                findings.append(act["result"])
    if not findings and metadata:
        findings = metadata.get("findings", [])

    if not findings:
        return None

    try:
        from causal_graph.engine import CausalGraphBuilder

        builder = CausalGraphBuilder()
        cg = builder.build_from_findings(findings)
        if cg.graph.number_of_nodes() == 0:
            return None
        candidates = cg.find_root_causes(top_k=3)
        if not candidates:
            return 0.5
        top_score = candidates[0].get("score", 0.5)
        return max(0.0, min(1.0, float(top_score)))
    except Exception:
        return None


def compute_metrics(state: Any) -> Dict[str, Optional[float]]:
    """Compute all state metrics and return a dict."""
    return {
        "entropy": shannon_entropy(getattr(state, "hypotheses", {})),
        "evidence_coverage": evidence_coverage(state),
        "hypothesis_disagreement": agent_disagreement(state),
        "causal_consistency": causal_consistency(state),
    }


