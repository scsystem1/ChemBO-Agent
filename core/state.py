"""
ChemBO Agent State Definition
==============================
Central state schema for the ChemBO LangGraph.

REDUCER CONTRACT:
- messages: add_messages (LangGraph built-in)
- observations, performance_log, config_history, reconfig_history: append-only
- hypotheses: replace (new version carries status history)
- bo_config, effective_config, proposal_selected, current_proposal: replace
- proposal_shortlist: replace
- embedding_config: write-once after embedding_locked=True
- memory: replace (MemoryManager manages append/evict internally)
- convergence_state: replace (recomputed each iteration)
- best_result, best_candidate: conditional replace only on improvement
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Annotated, TypedDict

from langchain_core.messages import BaseMessage, SystemMessage
from langgraph.graph import add_messages


class CampaignPhase(str, Enum):
    INIT = "init"
    PARSING = "parsing"
    SELECTING_EMBEDDING = "selecting_embedding"
    HYPOTHESIZING = "hypothesizing"
    CONFIGURING = "configuring"
    WARM_STARTING = "warm_starting"
    RUNNING = "running"
    SELECTING_CANDIDATE = "selecting_candidate"
    AWAITING_HUMAN = "awaiting_human"
    INTERPRETING = "interpreting"
    REFLECTING = "reflecting"
    RECONFIGURING = "reconfiguring"
    SUMMARIZING = "summarizing"
    COMPLETED = "completed"


class NextAction(str, Enum):
    CONTINUE = "continue"
    RECONFIGURE = "reconfigure"
    STOP = "stop"


class ChemBOState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]

    phase: str
    iteration: int
    next_action: str

    problem_spec: dict[str, Any]
    knowledge_cards: list[dict[str, Any]]
    retrieval_artifacts: dict[str, Any]

    embedding_config: dict[str, Any]
    embedding_locked: bool

    bo_config: dict[str, Any]
    effective_config: dict[str, Any]

    hypotheses: list[dict[str, Any]]

    proposal_shortlist: list[dict[str, Any]]
    proposal_selected: dict[str, Any]
    current_proposal: dict[str, Any]
    warm_start_queue: list[dict[str, Any]]
    warm_start_target: int
    warm_start_active: bool

    observations: list[dict[str, Any]]

    best_result: float
    best_candidate: dict[str, Any]
    convergence_state: dict[str, Any]

    memory: dict[str, Any]

    reconfig_history: list[dict[str, Any]]
    last_reconfig_iteration: int
    total_reconfigs: int

    config_history: list[dict[str, Any]]
    performance_log: list[dict[str, Any]]
    llm_reasoning_log: list[str]
    llm_token_usage: dict[str, Any]
    last_llm_usage: dict[str, Any]
    campaign_summary: str
    final_summary: dict[str, Any]
    termination_reason: str
    tool_origin_node: str
    last_tool_payload: dict[str, Any]
    optimization_direction: str


def create_initial_state(
    problem_input: str | dict[str, Any],
    settings,
    problem_source_path: str | None = None,
) -> ChemBOState:
    """Create the initial state from raw text or a structured problem spec."""
    from core.problem_loader import normalize_problem_spec

    if isinstance(problem_input, dict):
        normalized = normalize_problem_spec(problem_input, problem_source_path)
        normalized.setdefault("budget", int(getattr(settings, "max_bo_iterations", 30)))
        problem_spec = _prepare_problem_spec(normalized)
    else:
        problem_spec = _prepare_problem_spec(
            {
                "raw_description": str(problem_input),
                "budget": int(getattr(settings, "max_bo_iterations", 30)),
            }
        )

    direction = str(problem_spec.get("optimization_direction") or "maximize").strip().lower()
    initial_best = float("-inf") if direction != "minimize" else float("inf")

    return ChemBOState(
        messages=[SystemMessage(content=_build_system_prompt())],
        phase=CampaignPhase.INIT.value,
        iteration=0,
        next_action="",
        problem_spec=problem_spec,
        knowledge_cards=[],
        retrieval_artifacts={},
        embedding_config={},
        embedding_locked=False,
        bo_config={},
        effective_config={},
        hypotheses=[],
        proposal_shortlist=[],
        proposal_selected={},
        current_proposal={},
        warm_start_queue=[],
        warm_start_target=0,
        warm_start_active=False,
        observations=[],
        best_result=initial_best,
        best_candidate={},
        convergence_state={},
        memory={"version": 2, "working": {}, "episodic": [], "semantic": {"nodes": [], "edges": []}},
        reconfig_history=[],
        last_reconfig_iteration=-999,
        total_reconfigs=0,
        config_history=[],
        performance_log=[],
        llm_reasoning_log=[],
        llm_token_usage={
            "calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "estimated_calls": 0,
            "by_node": {},
        },
        last_llm_usage={},
        campaign_summary="",
        final_summary={},
        termination_reason="",
        tool_origin_node="",
        last_tool_payload={},
        optimization_direction=direction,
    )


def _prepare_problem_spec(problem_spec: dict[str, Any]) -> dict[str, Any]:
    spec = dict(problem_spec)
    spec.setdefault("raw_description", str(spec.get("description") or ""))
    spec.setdefault("optimization_direction", "maximize")
    spec.setdefault("budget", 30)
    spec.setdefault("constraints", [])
    spec.setdefault("variables", [])

    prepared_variables = []
    for variable in spec.get("variables", []):
        if not isinstance(variable, dict):
            continue
        prepared = dict(variable)
        smiles_map = {str(k): str(v) for k, v in prepared.get("smiles_map", {}).items()}
        if "smiles" in prepared and "smiles_map" not in prepared:
            smiles_value = prepared.get("smiles")
            if isinstance(smiles_value, dict):
                smiles_map.update({str(k): str(v) for k, v in smiles_value.items()})
        for domain_entry in prepared.get("domain", []):
            if isinstance(domain_entry, dict):
                label = str(
                    domain_entry.get("label")
                    or domain_entry.get("name")
                    or domain_entry.get("value")
                    or ""
                ).strip()
                smiles = str(domain_entry.get("smiles") or "").strip()
                if label and smiles:
                    smiles_map[label] = smiles
            elif _variable_is_smiles_like(prepared):
                label = str(domain_entry)
                smiles_map.setdefault(label, label)
        if smiles_map:
            prepared["smiles_map"] = smiles_map
        prepared_variables.append(prepared)
    spec["variables"] = prepared_variables
    return spec


def _variable_is_smiles_like(variable: dict[str, Any]) -> bool:
    name = str(variable.get("name") or "").lower()
    description = str(variable.get("description") or "").lower()
    return "smiles" in name or "smiles" in description


def _build_system_prompt() -> str:
    return """LAYER 1 - IDENTITY
You are ChemBO Agent, an expert AI system for chemical reaction optimization using Bayesian Optimization.
You operate as a single cognitive core augmented by specialized tools.

Core beliefs:
- LLM constructs priors; probabilistic models update posteriors; the agent evolves across iterations.
- Every decision must have scientific justification tied to the specific problem.
- Honest uncertainty: if unsure, prefer conservative baselines and say so.

LAYER 2 - OUTPUT DISCIPLINE
- When a node requires JSON, respond with ONLY valid JSON.
- When prose is requested, be concise and evidence-based.
- Every recommendation must state:
  (a) what you chose
  (b) why
  (c) confidence (0.0-1.0)
- Cite evidence sources when possible:
  [KB:<source>], [OBS:iterN], [RULE:Rn], [HYPOTHESIS:Hn], [CONFIG:vN]

LAYER 3 - TOOL PROTOCOL
- embedding_method_advisor: call only when selecting the initial embedding. Embedding is locked afterward.
- surrogate_model_selector: use when configuring or reconfiguring the BO engine.
- af_selector: use when configuring or reconfiguring the acquisition strategy.
- bo_runner: use for BO shortlist generation after warm start.
- hypothesis_generator: use at campaign start and on major reconfiguration.
- result_interpreter: use after each observed result.

LAYER 4 - WORKFLOW
1. Parse problem
2. Select embedding (lock)
3. Generate hypotheses
4. Configure surrogate + kernel + acquisition
5. Warm start
6. Iterate: shortlist -> select candidate -> observe -> interpret -> reflect
7. Summarize the campaign
"""
