import pytest

from investigation.epistemic import ClaimLevel, HypothesisRecord, mark_inferred, require_confirmation


def test_agent_claims_default_to_inference():
    metadata = mark_inferred({"provenance": {"source_type": "llm"}})
    assert metadata["provenance"]["claim_level"] == ClaimLevel.INFERENCE.value
    assert metadata["provenance"]["confirmation_required"] is True


def test_confirmation_requires_external_authority():
    with pytest.raises(ValueError):
        require_confirmation("consensus")
    assert require_confirmation("engineer_confirmed") == "engineer_confirmed"


def test_hypothesis_tracks_contradiction_and_support():
    h = HypothesisRecord(id="h1", statement="database pool exhausted")
    h.add_support("e1")
    h.add_contradiction("e2")
    assert h.support == ["e1"]
    assert h.contradictions == ["e2"]
    assert not h.is_confirmed
