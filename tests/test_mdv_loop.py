import pytest

from investigation.state import InvestigationState
from investigation.metrics import shannon_entropy, agent_disagreement, evidence_coverage
from investigation.value_engine import compute_mdv, MDVWeights
from investigation.diagnostic_gain import compute_adg
from investigation.action_effectiveness import (
    get_expected_effectiveness,
    update_effectiveness,
    get_effectiveness_record,
)
from investigation.reliability_store import get_reliability, update_reliability
from investigation.tree import run_tree


# ---------------------------------------------------------------------------
# 1. MDV Tests
# ---------------------------------------------------------------------------

def test_mdv_component_normalization():
    action = {
        "agent_name": "log_analyzer",
        "discrimination_score": 0.8,
        "expected_uncertainty_reduction": 0.6,
        "evidence_value": 1.0,
        "reliability_score": 0.85,
        "execution_cost": 0.4,
    }
    mdv = compute_mdv(action)
    assert 0.0 <= mdv <= 1.0
    assert mdv > 0.5


def test_mdv_cost_penalty():
    cheap_action = {
        "agent_name": "rule_based_analyzer",
        "discrimination_score": 0.5,
        "expected_uncertainty_reduction": 0.5,
        "evidence_value": 1.0,
        "reliability_score": 0.8,
        "execution_cost": 0.1,
    }
    expensive_action = {
        "agent_name": "llm_analyzer",
        "discrimination_score": 0.5,
        "expected_uncertainty_reduction": 0.5,
        "evidence_value": 1.0,
        "reliability_score": 0.8,
        "execution_cost": 0.9,
    }
    mdv_cheap = compute_mdv(cheap_action)
    mdv_expensive = compute_mdv(expensive_action)
    assert mdv_cheap > mdv_expensive


def test_mdv_cost_modes():
    action = {
        "agent_name": "log_analyzer",
        "discrimination_score": 0.4,
        "expected_uncertainty_reduction": 0.4,
        "evidence_value": 1.0,
        "reliability_score": 0.8,
        "execution_cost": 0.5,
    }
    # Default (cost_efficiency): MDV = 0.3*0.4 + 0.3*0.4 + 0.2*1.0 + 0.1*0.8 + 0.1*(1 - 0.5)
    # = 0.12 + 0.12 + 0.20 + 0.08 + 0.05 = 0.57
    eff_weights = MDVWeights(cost_mode="cost_efficiency")
    mdv_eff = compute_mdv(action, weights=eff_weights)
    assert abs(mdv_eff - 0.57) < 1e-5

    # Penalty mode (cost_penalty): Benefit = 0.12 + 0.12 + 0.20 + 0.08 = 0.52
    # MDV = Benefit - 0.1 * 0.5 = 0.52 - 0.05 = 0.47
    pen_weights = MDVWeights(cost_mode="cost_penalty")
    mdv_pen = compute_mdv(action, weights=pen_weights)
    assert abs(mdv_pen - 0.47) < 1e-5

    # Validation check for invalid cost mode
    invalid_weights = MDVWeights(cost_mode="invalid_mode")
    with pytest.raises(ValueError, match="cost_mode must be one of"):
        invalid_weights.validate()



def test_mdv_custom_weight_injection():
    action = {
        "agent_name": "metric_analyzer",
        "discrimination_score": 1.0,
        "expected_uncertainty_reduction": 0.0,
        "evidence_value": 0.0,
        "reliability_score": 0.0,
        "execution_cost": 1.0,
    }
    weights = MDVWeights(
        discrimination=1.0,
        uncertainty_reduction=0.0,
        evidence_value=0.0,
        reliability=0.0,
        cost=0.0,
    )
    mdv = compute_mdv(action, weights=weights)
    assert abs(mdv - 1.0) < 1e-6


# ---------------------------------------------------------------------------
# 2. Metrics Tests
# ---------------------------------------------------------------------------

def test_shannon_entropy_normalized():
    # 2 equal hypotheses => max entropy = log2(2) = 1 => normalized = 1.0
    hyp_equal = {"db_failure": 0.5, "net_failure": 0.5}
    assert abs(shannon_entropy(hyp_equal) - 1.0) < 1e-5

    # Single hypothesis => zero entropy
    hyp_single = {"db_failure": 1.0}
    assert shannon_entropy(hyp_single) == 0.0

    # 4 equal hypotheses => normalized = 1.0
    hyp_four = {"h1": 0.25, "h2": 0.25, "h3": 0.25, "h4": 0.25}
    assert abs(shannon_entropy(hyp_four) - 1.0) < 1e-5


def test_agent_disagreement_actual_findings():
    state = InvestigationState()
    # 1 finding => 0 disagreement
    state.executed_actions = [{"result": {"root_cause_hint": "Database connection timeout"}}]
    assert agent_disagreement(state) == 0.0

    # 2 identical findings => 0 disagreement
    state.executed_actions = [
        {"result": {"root_cause_hint": "Database connection timeout"}},
        {"result": {"root_cause_hint": "Database connection timeout"}},
    ]
    assert agent_disagreement(state) == 0.0

    # 2 completely different findings => high disagreement
    state.executed_actions = [
        {"result": {"root_cause_hint": "Database connection timeout"}},
        {"result": {"root_cause_hint": "BGP router packet drop"}},
    ]
    disag = agent_disagreement(state)
    assert 0.0 < disag <= 1.0


def test_evidence_coverage():
    state = InvestigationState(
        metadata={
            "evidence_present": {"logs": True, "metrics": True},
            "required_evidence": ["logs", "metrics", "traces", "topology"],
        }
    )
    cov = evidence_coverage(state)
    assert abs(cov - 0.5) < 1e-5


# ---------------------------------------------------------------------------
# 3. ADG Tests
# ---------------------------------------------------------------------------

def test_adg_directional_gains():
    prev = InvestigationState(
        hypotheses={"db": 0.5, "net": 0.5},
        metadata={"evidence_present": {"logs": True}},
    )
    prev.recompute_metrics()

    cur = InvestigationState(
        hypotheses={"db": 0.9, "net": 0.1},
        metadata={"evidence_present": {"logs": True, "metrics": True}},
    )
    cur.recompute_metrics()

    adg = compute_adg(prev, cur)
    assert adg.total_gain > 0.0
    assert "uncertainty" in adg.component_gains
    assert "coverage" in adg.component_gains


def test_adg_missing_metric_renormalization():
    # Only uncertainty is present
    class DummyState:
        def __init__(self, entropy):
            self.hypothesis_uncertainty = entropy

    prev = DummyState(0.8)
    cur = DummyState(0.2)

    adg = compute_adg(prev, cur)
    # Gain in uncertainty = 0.6, active weight renormalizes to 1.0 => total gain = 0.6
    assert abs(adg.total_gain - 0.6) < 1e-5


# ---------------------------------------------------------------------------
# 4. Learning & Context Separation Tests
# ---------------------------------------------------------------------------

def test_action_effectiveness_ema_and_context_separation():
    import investigation.action_effectiveness as ae
    ae._EFFECTIVENESS_CACHE.clear()
    ae._CACHE_LOADED = True

    ctx_net = "network_incident"
    ctx_db = "database_incident"
    agent = "trace_analyzer"

    update_effectiveness(ctx_net, agent, 0.9)
    update_effectiveness(ctx_db, agent, 0.2)

    eff_net = get_expected_effectiveness(ctx_net, agent)
    eff_db = get_expected_effectiveness(ctx_db, agent)

    assert eff_net > eff_db



def test_reliability_ground_truth_requirement():
    ctx = "test_context"
    agent = "log_analyzer"
    initial_rel = get_reliability(ctx, agent)

    # Calling update_reliability without ground_truth should NOT mutate reliability
    update_reliability(ctx, agent, is_correct=True, has_ground_truth=False)
    assert get_reliability(ctx, agent) == initial_rel

    # Calling with ground truth should update reliability
    update_reliability(ctx, agent, is_correct=True, has_ground_truth=True)
    updated_rel = get_reliability(ctx, agent)
    assert updated_rel != initial_rel


# ---------------------------------------------------------------------------
# 5. Persistence Tests
# ---------------------------------------------------------------------------

def test_persistence_atomic_and_corruption_safety(tmp_path, monkeypatch):
    import investigation.action_effectiveness as ae

    eff_path = tmp_path / "action_effectiveness.json"
    monkeypatch.setattr(ae, "_store_path", lambda: eff_path)
    ae._CACHE_LOADED = False
    ae._EFFECTIVENESS_CACHE.clear()

    # Write data
    update_effectiveness("context_a", "agent_x", 0.75)
    rec = get_effectiveness_record("context_a", "agent_x")
    assert abs(rec["value"] - 0.75) < 1e-5

    # Corrupt file
    eff_path.write_text("NOT_VALID_JSON{{{")

    # Reset cache and load should handle corrupted file without crash
    ae._CACHE_LOADED = False
    ae._EFFECTIVENESS_CACHE.clear()

    eff_val = get_expected_effectiveness("context_a", "agent_x")
    assert eff_val == ae.DEFAULT_EXPECTED_GAIN



# ---------------------------------------------------------------------------
# 6. Iterative Core Loop Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_tree_single_action_loop():
    context = {
        "incident_id": "INC-TEST-100",
        "incident_type": "network_incident",
        "logs": ["Network timeout connecting to host DB"],
        "metrics": {"latency_ms": 450.0},
        "hypotheses": {"database_failure": 0.5, "network_failure": 0.5},
    }

    result = await run_tree("INC-TEST-100", "network_incident", context)
    assert result.findings
    assert len(result.agents_used) >= 1
    # Verify repeat protection: non-repeatable agents are executed at most once
    non_repeatable_used = [a for a in result.agents_used if a not in ("historical_analyzer", "knowledge_graph_analyzer")]
    assert len(non_repeatable_used) == len(set(non_repeatable_used))

