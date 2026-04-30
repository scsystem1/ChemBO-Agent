from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

import pytest

pytest.importorskip("langchain_core")
pytest.importorskip("langgraph")

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from config.settings import Settings
from core.dataset_oracle import DatasetOracle
from core.graph import (
    _delta_best,
    _merge_llm_usage,
    _update_hypothesis_statuses,
    build_chembo_graph,
    compute_convergence_state,
)
from core.problem_loader import load_problem_file
from core.state import create_initial_state
from core.warm_start import (
    _build_coverage_guaranteed_doe_pool,
    _build_warm_start_candidate_search_tool,
    _normalize_llm_guidance,
    _select_warm_start_shortlist,
    _sort_warm_start_queue,
    interpret_warm_start_result,
    plan_warm_start,
    run_warm_start_postmortem,
)
from memory.memory_manager import MemoryManager
from pools.component_pools import candidate_to_key


class _GraphDummyLLM:
    def bind_tools(self, tools):
        del tools
        return self

    def invoke(self, messages):
        raise AssertionError(f"Unexpected direct LLM invocation: {messages}")


def _example_problem(name: str) -> dict:
    root = Path(__file__).resolve().parents[1]
    return load_problem_file(root / "examples" / f"{name}_problem.yaml")


def test_suzuki_problem_loads_with_dataset_oracle() -> None:
    problem = _example_problem("suzuki")

    oracle = DatasetOracle.from_problem_spec(problem)
    assert oracle is not None
    assert oracle.size == 5760
    assert oracle.feature_columns == (
        "Reactant_1_Name",
        "Reactant_2_Name",
        "Ligand_Short_Hand",
        "Reagent_1_Short_Hand",
        "Solvent_1_Short_Hand",
    )

    expected_domains = {
        "Reactant_1_Name": 7,
        "Reactant_2_Name": 4,
        "Ligand_Short_Hand": 12,
        "Reagent_1_Short_Hand": 8,
        "Solvent_1_Short_Hand": 6,
    }
    assert {name: len(values) for name, values in oracle.domain_values.items()} == expected_domains


def _sample_knowledge_cards() -> list[dict]:
    return [
        {
            "card_id": "kc_ligand",
            "text": "For ligand_SMILES, bulky electron-rich ligands are often productive starting points.",
            "card_type": "reagent_property",
            "confidence": 0.85,
            "targets": ["ligand_SMILES"],
            "actionable_for": ["warm_start", "hypothesis_generation"],
            "scope": "target",
            "status": "active",
            "evidence_refs": ["S01"],
            "source_type": "local_rag",
            "validation": {"used_count": 0, "supported_count": 0, "contradicted_count": 0, "last_used_iter": None},
        },
        {
            "card_id": "kc_temp",
            "text": "For temperature, moderate-to-high values are often needed to activate challenging coupling manifolds.",
            "card_type": "operating_window",
            "confidence": 0.62,
            "targets": ["temperature"],
            "actionable_for": ["warm_start"],
            "scope": "general",
            "status": "active",
            "evidence_refs": ["S02"],
            "source_type": "local_rag",
            "validation": {"used_count": 0, "supported_count": 0, "contradicted_count": 0, "last_used_iter": None},
        },
    ]


def _usage() -> dict[str, int | bool]:
    return {
        "calls": 1,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "estimated_calls": 0,
        "estimated": False,
    }


def _parse_warm_start_target(prompt: str) -> int:
    match = re.search(r'"warm_start_target"\s*:\s*(\d+)', prompt)
    return int(match.group(1)) if match else 0


def _invoke_tool_loop_factory():
    def _fake_invoke_tool_loop(llm, state, prompt, tool_map, max_turns=6, **kwargs):
        del llm, max_turns, kwargs
        messages = [HumanMessage(content=prompt)]

        if "Generate 3-5 high-value hypotheses" in prompt:
            messages.append(
                AIMessage(
                    content=json.dumps(
                        {
                            "hypotheses": [
                                {
                                    "id": "H1",
                                    "text": "Coverage-first warm starts should reveal productive discrete regions early.",
                                    "mechanism": "Initialization should balance chemistry priors with broad categorical coverage.",
                                    "testable_prediction": "Strong ligands should appear without collapsing diversity.",
                                    "confidence": "medium",
                                    "status": "active",
                                }
                            ],
                            "working_memory_focus": "Use knowledge cards to balance coverage and exploitation.",
                        }
                    )
                )
            )
            return messages, "", _usage()

        if "Design deterministic guidance for the warm-start planner." in prompt:
            search_payload = tool_map["warm_start_candidate_search"].invoke(
                {
                    "objective": "dataset-grounded coverage-first warm start",
                    "preferences": [{"variable": "solvent_SMILES", "preferred_values": ["CC(N(C)C)=O"], "weight": 0.6}],
                    "must_include": {},
                    "max_results": 4,
                }
            )
            messages.append(
                ToolMessage(
                    content=search_payload,
                    name="warm_start_candidate_search",
                    tool_call_id="warm-start-search-1",
                )
            )
            target = _parse_warm_start_target(prompt)
            messages.append(
                AIMessage(
                    content=json.dumps(
                        {
                            "strategy_summary": "Seed a few chemically strong points, then preserve broad categorical coverage.",
                            "selected_indices": [0, 1, 1, 999],
                            "preferred_patterns": [
                                {
                                    "variable": "solvent_SMILES",
                                    "preferred_values": ["CC(N(C)C)=O", "CCCC#N"],
                                    "weight": 0.8,
                                    "reason": "Polar aprotic solvents are strong initial bets.",
                                    "knowledge_card_ids": [],
                                }
                            ],
                            "avoided_patterns": [],
                            "category_targets": {
                                "exploration": max(1, int(round(target * 0.40))),
                                "balanced": max(1, int(round(target * 0.35))),
                                "exploitation": max(1, target - max(1, int(round(target * 0.40))) - max(1, int(round(target * 0.35)))),
                            },
                            "priority_indices": [0, 2, 3],
                        }
                    )
                )
            )
            return messages, "", _usage()

        if "Select the single best embedding method" in prompt:
            messages.append(
                AIMessage(
                    content=json.dumps(
                        {
                            "method": "one_hot",
                            "params": {},
                            "rationale": "Simple discrete baseline.",
                            "confidence": 0.9,
                        }
                    )
                )
            )
            return messages, "", _usage()

        if "Configure the BoTorch surrogate" in prompt:
            messages.append(
                AIMessage(
                    content=json.dumps(
                        {
                            "surrogate_model": "gp",
                            "surrogate_params": {},
                            "kernel_config": {"key": "matern52", "params": {}, "rationale": "Stable default."},
                            "acquisition_function": "log_ei",
                            "af_params": {},
                            "rationale": "Use the default BO stack after warm start.",
                            "confidence": 0.85,
                        }
                    )
                )
            )
            return messages, "", _usage()

        raise AssertionError(f"Unhandled prompt in fake tool loop:\n{prompt}")

    return _fake_invoke_tool_loop


def _direct_extract_last_json(messages):
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            return json.loads(message.content)
    return None


def _state_messages_identity(messages):
    return messages


def _updated_campaign_summary_stub(state, messages):
    del state
    return "\n".join(getattr(message, "content", "") for message in messages if getattr(message, "content", ""))


def _attach_llm_usage_stub(update, state, node_name, usage):
    del state, node_name
    update["last_llm_usage"] = dict(usage)


def _memory_manager_from_state(state: dict, settings: Settings) -> MemoryManager:
    return MemoryManager.from_dict(
        state.get("memory", {}),
        capacity=settings.episodic_memory_capacity,
        node_budgets=getattr(settings, "memory_node_budgets", {}),
        consolidation_every_n=int(getattr(settings, "memory_consolidation_every_n", 5)),
        enable_llm_consolidation=bool(getattr(settings, "memory_llm_consolidation_enabled", True)),
        llm_cooldown_iters=int(getattr(settings, "memory_llm_consolidation_cooldown_iters", 5)),
        episode_keep_recent=int(getattr(settings, "memory_episode_keep_recent", 24)),
        episode_keep_salient=int(getattr(settings, "memory_episode_keep_salient", 96)),
    )


def _run_to_first_interrupt(monkeypatch, problem_spec: dict, *, cards: list[dict]) -> dict:
    monkeypatch.setattr("core.graph._create_llm", lambda settings, enable_thinking_override=None: _GraphDummyLLM())
    monkeypatch.setattr(
        "core.graph.run_knowledge_augmentation",
        lambda problem_spec, settings: (
            {
                "target_family": problem_spec.get("reaction_type", ""),
                "knowledge_profile": "generic_fallback",
                "coverage_level": "partial",
                "source_health_summary": {"local_rag": "ok"},
            },
            {"cards": cards, "build_summary": {"coverage_level": "partial", "cards_active": len(cards)}},
            {"card_count": len(cards)},
        ),
    )
    monkeypatch.setattr("core.graph._invoke_tool_loop", _invoke_tool_loop_factory())

    settings = Settings(
        initial_doe_size=20,
        max_bo_iterations=40,
        human_input_mode="dataset_auto",
    )
    settings.knowledge_enabled = True
    graph = build_chembo_graph(settings)
    initial_state = create_initial_state(problem_spec, settings)
    config = {"configurable": {"thread_id": f"test-{uuid.uuid4().hex[:8]}"}}
    list(graph.stream(initial_state, config=config, stream_mode="updates"))
    return graph.get_state(config).values


def _toy_variables() -> list[dict]:
    return [
        {"name": "ligand", "type": "categorical", "domain": ["A", "B", "C", "D"]},
        {"name": "solvent", "type": "categorical", "domain": ["S1", "S2"]},
        {"name": "temperature", "type": "continuous", "domain": [60.0, 120.0]},
    ]


def _toy_pool() -> list[dict]:
    return [
        {"ligand": "A", "solvent": "S1", "temperature": 60.0},
        {"ligand": "B", "solvent": "S1", "temperature": 70.0},
        {"ligand": "C", "solvent": "S2", "temperature": 80.0},
        {"ligand": "D", "solvent": "S2", "temperature": 90.0},
        {"ligand": "A", "solvent": "S2", "temperature": 100.0},
        {"ligand": "B", "solvent": "S2", "temperature": 110.0},
    ]


def test_build_coverage_guaranteed_doe_pool_dataset_backed_covers_all_discrete_values() -> None:
    problem_spec = _example_problem("dar")
    oracle = DatasetOracle.from_problem_spec(problem_spec)
    assert oracle is not None

    pool_a = _build_coverage_guaranteed_doe_pool(
        problem_spec["variables"],
        pool_size=80,
        seed=7,
        observed_keys=set(),
        hard_constraints=[],
        candidate_pool=list(oracle.candidates),
    )
    pool_b = _build_coverage_guaranteed_doe_pool(
        problem_spec["variables"],
        pool_size=80,
        seed=7,
        observed_keys=set(),
        hard_constraints=[],
        candidate_pool=list(oracle.candidates),
    )

    assert pool_a == pool_b
    assert len(pool_a) == 80
    assert len({candidate_to_key(candidate) for candidate in pool_a}) == 80
    assert all(oracle.candidate_exists(candidate) for candidate in pool_a)

    for variable in problem_spec["variables"]:
        if variable.get("type") == "continuous":
            continue
        selected_values = {str(candidate.get(variable["name"], "")) for candidate in pool_a}
        assert set(map(str, variable.get("domain", []))) <= selected_values


def test_build_coverage_guaranteed_doe_pool_mixed_space_without_dataset_covers_discrete_values() -> None:
    variables = _toy_variables()
    pool = _build_coverage_guaranteed_doe_pool(
        variables,
        pool_size=12,
        seed=11,
        observed_keys=set(),
        hard_constraints=[],
        candidate_pool=None,
    )

    assert len(pool) == 12
    assert len({candidate_to_key(candidate) for candidate in pool}) == 12
    assert {"A", "B", "C", "D"} <= {candidate["ligand"] for candidate in pool}
    assert {"S1", "S2"} <= {candidate["solvent"] for candidate in pool}
    assert all(60.0 <= float(candidate["temperature"]) <= 120.0 for candidate in pool)


def test_settings_default_initial_doe_size_is_20() -> None:
    assert Settings().initial_doe_size == 20


def test_delta_best_supports_fast_interpretation_digest() -> None:
    assert _delta_best(40.0, 55.5, "maximize") == 15.5
    assert _delta_best(40.0, 35.0, "minimize") == 5.0
    assert _delta_best(None, 35.0, "maximize") is None
    assert _delta_best(40.0, None, "maximize") is None


@pytest.mark.parametrize(
    ("budget", "expected_target"),
    [
        (35, 17),
        (40, 20),
        (50, 20),
        (1, 1),
    ],
)
def test_plan_warm_start_respects_budget_caps(budget: int, expected_target: int) -> None:
    settings = Settings(initial_doe_size=20, max_bo_iterations=max(budget, 1))
    problem_spec = _example_problem("dar")
    problem_spec["budget"] = budget
    state = create_initial_state(problem_spec, settings)
    state["knowledge_deck"] = {"cards": _sample_knowledge_cards(), "build_summary": {"coverage_level": "partial"}}

    updates = plan_warm_start(
        state,
        settings,
        _GraphDummyLLM(),
        invoke_tool_loop=_invoke_tool_loop_factory(),
        extract_last_json=_direct_extract_last_json,
        state_messages=_state_messages_identity,
        updated_campaign_summary=_updated_campaign_summary_stub,
        attach_llm_usage=_attach_llm_usage_stub,
    )

    assert updates["warm_start_target"] == expected_target
    assert len(updates["warm_start_queue"]) == expected_target


def test_plan_warm_start_is_deterministic_and_covers_discrete_domains() -> None:
    settings = Settings(initial_doe_size=20, max_bo_iterations=40)
    problem_spec = _example_problem("dar")
    state = create_initial_state(problem_spec, settings)
    state["knowledge_deck"] = {"cards": _sample_knowledge_cards(), "build_summary": {"coverage_level": "partial"}}

    first = plan_warm_start(
        state,
        settings,
        _GraphDummyLLM(),
        invoke_tool_loop=_invoke_tool_loop_factory(),
        extract_last_json=_direct_extract_last_json,
        state_messages=_state_messages_identity,
        updated_campaign_summary=_updated_campaign_summary_stub,
        attach_llm_usage=_attach_llm_usage_stub,
    )
    second = plan_warm_start(
        state,
        settings,
        _GraphDummyLLM(),
        invoke_tool_loop=_invoke_tool_loop_factory(),
        extract_last_json=_direct_extract_last_json,
        state_messages=_state_messages_identity,
        updated_campaign_summary=_updated_campaign_summary_stub,
        attach_llm_usage=_attach_llm_usage_stub,
    )

    assert first["warm_start_queue"] == second["warm_start_queue"]
    categories = [item["warm_start_category"] for item in first["warm_start_queue"]]
    assert set(categories) <= {"anchor", "contrast", "wildcard"}

    for variable in problem_spec["variables"]:
        if variable.get("type") == "continuous":
            continue
        selected_values = {str(item["candidate"].get(variable["name"], "")) for item in first["warm_start_queue"]}
        assert set(map(str, variable.get("domain", []))) <= selected_values


def test_normalize_llm_guidance_limits_and_deduplicates_selected_indices() -> None:
    guidance = _normalize_llm_guidance(
        {
            "strategy_summary": "test",
            "selected_indices": [2, 2, 999, -1, 3, 4],
            "preferred_patterns": [],
            "avoided_patterns": [],
            "category_targets": {"exploration": 4, "balanced": 3, "exploitation": 3},
            "priority_indices": [0, 0, 5, 999],
        },
        target=10,
        valid_card_ids=set(),
        doe_pool_size=6,
    )

    assert guidance["selected_indices"] == [2, 3, 4]
    assert guidance["priority_indices"] == [0, 5]


def test_select_warm_start_shortlist_preserves_coverage_with_selected_indices() -> None:
    shortlist = _select_warm_start_shortlist(
        doe_pool=_toy_pool(),
        variables=_toy_variables(),
        target=4,
        knowledge_cards=[],
        llm_guidance={
            "strategy_summary": "test",
            "selected_indices": [0, 4],
            "preferred_patterns": [],
            "avoided_patterns": [],
            "category_targets": {"exploration": 2, "balanced": 1, "exploitation": 1},
            "priority_indices": [],
        },
    )
    assert len(shortlist) == 4
    assert {"A", "B", "C", "D"} <= {item["candidate"]["ligand"] for item in shortlist}
    assert {"S1", "S2"} <= {item["candidate"]["solvent"] for item in shortlist}


def test_plan_warm_start_without_selected_indices_still_builds_valid_queue() -> None:
    settings = Settings(initial_doe_size=20, max_bo_iterations=40)
    problem_spec = _example_problem("dar")
    state = create_initial_state(problem_spec, settings)
    state["knowledge_deck"] = {"cards": [], "build_summary": {"coverage_level": "gap"}}

    def _invoke_tool_loop_no_direct(llm, state, prompt, tool_map, max_turns=6, **kwargs):
        del llm, state, tool_map, max_turns, kwargs
        return [
            HumanMessage(content=prompt),
            AIMessage(
                content=json.dumps(
                    {
                        "strategy_summary": "No direct seeds; rely on deterministic coverage-first planning.",
                        "selected_indices": [],
                        "preferred_patterns": [],
                        "avoided_patterns": [],
                        "category_targets": {"exploration": 8, "balanced": 7, "exploitation": 5},
                        "priority_indices": [],
                    }
                )
            ),
        ], "", _usage()

    updates = plan_warm_start(
        state,
        settings,
        _GraphDummyLLM(),
        invoke_tool_loop=_invoke_tool_loop_no_direct,
        extract_last_json=_direct_extract_last_json,
        state_messages=_state_messages_identity,
        updated_campaign_summary=_updated_campaign_summary_stub,
        attach_llm_usage=_attach_llm_usage_stub,
    )

    assert len(updates["warm_start_queue"]) == 20
    for variable in problem_spec["variables"]:
        if variable.get("type") == "continuous":
            continue
        selected_values = {str(item["candidate"].get(variable["name"], "")) for item in updates["warm_start_queue"]}
        assert set(map(str, variable.get("domain", []))) <= selected_values


@pytest.mark.parametrize("problem_name", ["dar", "ocm"])
def test_graph_warm_start_smoke_uses_deterministic_queue(problem_name: str, monkeypatch) -> None:
    state = _run_to_first_interrupt(
        monkeypatch,
        _example_problem(problem_name),
        cards=_sample_knowledge_cards(),
    )
    oracle = DatasetOracle.from_problem_spec(state["problem_spec"])

    assert "kb_context" not in state
    assert "kb_priors" not in state
    assert state["knowledge_deck"]["cards"]
    assert "retrieval_artifacts" not in state
    assert len(state["warm_start_queue"]) == 20
    assert state["warm_start_active"] is True
    assert state["proposal_selected"]["selection_source"] == "warm_start_queue"
    assert oracle is not None
    assert oracle.candidate_exists(state["proposal_selected"]["candidate"])
    categories = [item["warm_start_category"] for item in state["warm_start_queue"]]
    assert set(categories) <= {"anchor", "contrast", "wildcard"}


def test_interpret_warm_start_result_stays_lightweight() -> None:
    settings = Settings(initial_doe_size=20, max_bo_iterations=40)
    problem_spec = _example_problem("dar")
    state = create_initial_state(problem_spec, settings)
    state["observations"] = [
        {
            "iteration": 1,
            "candidate": {
                "base_SMILES": problem_spec["variables"][0]["domain"][0],
                "ligand_SMILES": problem_spec["variables"][1]["domain"][0],
                "solvent_SMILES": problem_spec["variables"][2]["domain"][0],
                "concentration": problem_spec["variables"][3]["domain"][0],
                "temperature": problem_spec["variables"][4]["domain"][0],
            },
            "result": 42.0,
            "metadata": {"selection_source": "warm_start_queue", "best_before_result": 10.0},
        }
    ]
    memory_manager = _memory_manager_from_state(state, settings)
    semantic_before = len(memory_manager.to_dict()["semantic"]["nodes"])

    updates = interpret_warm_start_result(
        state,
        settings,
        _GraphDummyLLM(),
        memory_manager=memory_manager,
        build_context_messages=lambda state, **kwargs: ([HumanMessage(content="context")], "", {"system": 0, "campaign_summary": 0, "recent_messages": 0, "prompt": 0}),
        invoke_llm_with_tracking=lambda llm, messages: (_GraphDummyLLM().invoke(messages), _usage()),
        extract_json_from_response=lambda text: json.loads(text),
        message_text=lambda message: message.content,
        state_messages=_state_messages_identity,
        updated_campaign_summary=_updated_campaign_summary_stub,
        attach_llm_usage=_attach_llm_usage_stub,
    )

    assert len(updates["memory"]["semantic"]["nodes"]) == semantic_before
    assert "[interpret_results:warm_start_light]" in updates["llm_reasoning_log"][-2]
    assert "last_llm_usage" not in updates
    assert "hypotheses" not in updates


def test_run_warm_start_postmortem_only_uses_warm_start_observations_and_updates_memory() -> None:
    settings = Settings(initial_doe_size=20, max_bo_iterations=40)
    problem_spec = _example_problem("dar")
    state = create_initial_state(problem_spec, settings)
    state["hypotheses"] = [{"id": "H1", "text": "Hotter starts help.", "status": "active"}]
    state["iteration"] = 3
    state["observations"] = [
        {
            "iteration": 1,
            "candidate": {"temperature": "90"},
            "result": 10.0,
            "metadata": {"selection_source": "warm_start_queue"},
        },
        {
            "iteration": 2,
            "candidate": {"temperature": "120"},
            "result": 25.0,
            "metadata": {"selection_source": "warm_start_queue"},
        },
        {
            "iteration": 3,
            "candidate": {"temperature": "105"},
            "result": 22.0,
            "metadata": {"selection_source": "autobo"},
        },
    ]
    captured_prompt = {"text": ""}
    memory_manager = _memory_manager_from_state(state, settings)

    def _invoke_llm_with_tracking(llm, messages):
        del llm
        captured_prompt["text"] = messages[-1].content
        return (
            AIMessage(
                content=json.dumps(
                    {
                        "batch_interpretation": "Warm-start established that hotter conditions outperform colder ones.",
                        "supported_hypotheses": ["H1"],
                        "refuted_hypotheses": [],
                        "key_patterns": ["High temperature is favorable."],
                        "semantic_rules": [
                            {
                                "rule_type": "chemical_effect",
                                "statement": "Higher temperature improved early DAR performance.",
                                "variables": ["temperature"],
                                "conditions": {},
                                "confidence": 0.72,
                            }
                        ],
                    }
                )
            ),
            _usage(),
        )

    payload = run_warm_start_postmortem(
        state,
        settings,
        _GraphDummyLLM(),
        None,
        memory_manager=memory_manager,
        build_context_messages=lambda state, **kwargs: ([HumanMessage(content="context")], "", {"system": 0, "campaign_summary": 0, "recent_messages": 0, "prompt": 0}),
        invoke_llm_with_tracking=_invoke_llm_with_tracking,
        extract_json_from_response=lambda text: json.loads(text),
        message_text=lambda message: message.content,
        compute_convergence_state=compute_convergence_state,
        update_hypothesis_statuses=_update_hypothesis_statuses,
        merge_llm_usage=_merge_llm_usage,
    )

    assert '"selection_source":"warm_start_queue"' in captured_prompt["text"]
    assert '"selection_source":"autobo"' not in captured_prompt["text"]
    assert payload["batch_interpretation"].startswith("Warm-start established")
    assert payload["hypotheses"][0]["status"] == "supported"
    assert payload["added_rule_count"] == 1
    assert payload["memory"]["semantic"]["nodes"]


def test_warm_start_candidate_search_tool_returns_dataset_backed_candidates() -> None:
    problem_spec = _example_problem("dar")
    oracle = DatasetOracle.from_problem_spec(problem_spec)
    assert oracle is not None
    tool = _build_warm_start_candidate_search_tool(
        variables=problem_spec["variables"],
        observed_keys=set(),
        hard_constraints=[],
        oracle=oracle,
        seed=0,
    )
    payload = json.loads(
        tool.invoke(
            {
                "objective": "dataset-grounded warm start",
                "preferences": [{"variable": "solvent_SMILES", "preferred_values": ["CC(N(C)C)=O"], "weight": 1.0}],
                "must_include": {"base_SMILES": problem_spec["variables"][0]["domain"][0]},
                "max_results": 5,
            }
        )
    )

    assert payload["status"] == "success"
    assert payload["candidates"]
    assert all(oracle.candidate_exists(item["candidate"]) for item in payload["candidates"])
