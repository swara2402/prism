"""Agent capability registry for PRISM.

Defines each diagnostic action (agent) with its capabilities, evidence requirements,
hypothesis discrimination pairs, execution cost, latency, and a default reliability
prior. Learned context‑specific reliability will be stored separately (see
action_effectiveness module).
"""

from dataclasses import dataclass, field
from typing import List, Tuple


@dataclass(frozen=True)
class AgentCapability:
    """Static description of an agent's capabilities.

    The fields are intentionally simple and immutable; learning updates are
    stored elsewhere.
    """

    agent_name: str
    supported_evidence: List[str] = field(default_factory=list)
    supported_hypotheses: List[str] = field(default_factory=list)
    discriminates_between: List[Tuple[str, str]] = field(default_factory=list)
    execution_cost: float = 0.0  # normalized 0‑1 for MDV
    expected_latency: float = 0.0  # seconds (raw)
    repeatable: bool = False  # whether the action may be executed multiple times
    default_reliability: float = 0.5  # prior reliability (0‑1)


# Registry of agents used by the selector.
AGENT_REGISTRY: List[AgentCapability] = [
    AgentCapability(
        agent_name="rule_based_analyzer",
        supported_evidence=["logs"],
        supported_hypotheses=["database_failure", "network_failure", "application_failure", "configuration_error"],
        discriminates_between=[
            ("database_failure", "application_failure"),
            ("network_failure", "configuration_error"),
        ],
        execution_cost=0.1,
        expected_latency=0.2,
        repeatable=False,
        default_reliability=0.70,
    ),
    AgentCapability(
        agent_name="log_analyzer",
        supported_evidence=["logs"],
        supported_hypotheses=["database_failure", "network_failure", "application_failure", "memory_leak"],
        discriminates_between=[
            ("database_failure", "application_failure"),
            ("network_failure", "application_failure"),
            ("application_failure", "memory_leak"),
        ],
        execution_cost=0.4,
        expected_latency=1.2,
        repeatable=False,
        default_reliability=0.80,
    ),
    AgentCapability(
        agent_name="metric_analyzer",
        supported_evidence=["metrics"],
        supported_hypotheses=["database_failure", "network_failure", "resource_exhaustion", "cpu_spike"],
        discriminates_between=[
            ("database_failure", "network_failure"),
            ("resource_exhaustion", "cpu_spike"),
        ],
        execution_cost=0.3,
        expected_latency=0.8,
        repeatable=False,
        default_reliability=0.85,
    ),
    AgentCapability(
        agent_name="trace_analyzer",
        supported_evidence=["traces", "logs"],
        supported_hypotheses=["database_failure", "network_failure", "downstream_timeout"],
        discriminates_between=[
            ("database_failure", "network_failure"),
            ("network_failure", "downstream_timeout"),
        ],
        execution_cost=0.6,
        expected_latency=2.0,
        repeatable=False,
        default_reliability=0.85,
    ),
    AgentCapability(
        agent_name="topology_analyzer",
        supported_evidence=["topology"],
        supported_hypotheses=["network_failure", "cascading_failure", "single_point_of_failure"],
        discriminates_between=[
            ("network_failure", "cascading_failure"),
            ("cascading_failure", "single_point_of_failure"),
        ],
        execution_cost=0.3,
        expected_latency=0.9,
        repeatable=False,
        default_reliability=0.75,
    ),
    AgentCapability(
        agent_name="historical_analyzer",
        supported_evidence=["history", "logs", "affected_services"],
        supported_hypotheses=["database_failure", "network_failure", "application_failure", "recurring_bug"],
        discriminates_between=[
            ("application_failure", "recurring_bug"),
            ("database_failure", "network_failure"),
        ],
        execution_cost=0.2,
        expected_latency=0.5,
        repeatable=True,
        default_reliability=0.70,
    ),
    AgentCapability(
        agent_name="knowledge_graph_analyzer",
        supported_evidence=["kg", "affected_services"],
        supported_hypotheses=["dependency_failure", "configuration_error", "database_failure"],
        discriminates_between=[
            ("dependency_failure", "configuration_error"),
            ("database_failure", "dependency_failure"),
        ],
        execution_cost=0.2,
        expected_latency=0.6,
        repeatable=True,
        default_reliability=0.75,
    ),
    AgentCapability(
        agent_name="llm_analyzer",
        supported_evidence=["logs", "metrics", "traces", "topology"],
        supported_hypotheses=["database_failure", "network_failure", "application_failure", "complex_root_cause"],
        discriminates_between=[
            ("application_failure", "complex_root_cause"),
            ("database_failure", "network_failure"),
        ],
        execution_cost=0.9,
        expected_latency=4.5,
        repeatable=False,
        default_reliability=0.90,
    ),
]

