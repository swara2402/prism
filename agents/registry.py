"""
agents.registry
===============

Agent registry.  Centralizes agent instantiation and discovery so the
orchestrator can pick them up dynamically.  New agents should be
registered here (or via :func:`register_agent` at runtime).
"""
from __future__ import annotations

from typing import Dict, List, Type

from agents.base import BaseAgent
from agents.historical_analyzer import HistoricalAnalyzerAgent
from agents.knowledge_graph_analyzer import KnowledgeGraphAnalyzerAgent
from agents.llm_analyzer import LLMAnalyzerAgent
from agents.log_analyzer import LogAnalyzerAgent
from agents.metric_analyzer import MetricAnalyzerAgent
from agents.rule_based_analyzer import RuleBasedAnalyzerAgent
from agents.topology_analyzer import TopologyAnalyzerAgent
from agents.trace_analyzer import TraceAnalyzerAgent


_REGISTRY: Dict[str, Type[BaseAgent]] = {}


def register_agent(cls: Type[BaseAgent]) -> Type[BaseAgent]:
    """Register an agent class.  Usable as a decorator."""
    if not cls.name:
        raise ValueError(f"Agent {cls.__name__} must define a non-empty `name`.")
    _REGISTRY[cls.name] = cls
    return cls


def get_agent_class(name: str) -> Type[BaseAgent] | None:
    return _REGISTRY.get(name)


def list_agent_classes() -> List[Type[BaseAgent]]:
    return list(_REGISTRY.values())


def list_agent_names() -> List[str]:
    return list(_REGISTRY.keys())


# Register built-in agents
for _cls in (
    RuleBasedAnalyzerAgent,
    LogAnalyzerAgent,
    MetricAnalyzerAgent,
    TraceAnalyzerAgent,
    TopologyAnalyzerAgent,
    HistoricalAnalyzerAgent,
    KnowledgeGraphAnalyzerAgent,
    LLMAnalyzerAgent,
):
    register_agent(_cls)


def instantiate_all() -> Dict[str, BaseAgent]:
    """Instantiate one of every registered agent."""
    return {name: cls() for name, cls in _REGISTRY.items()}
