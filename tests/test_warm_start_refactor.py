from __future__ import annotations

import json
import re
import uuid
from collections import Counter
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
    RegionSpec,
    _candidates_matching_region,
    _farthest_first_sample,
    _filter_and_rank_contrast_variables,
    _generate_contrast_candidates,
    _normalize_region_guidance,
    _sort_warm_start_queue,
    interpret_warm_start_result,
    plan_warm_start,
    run_warm_start_postmortem,
)
from memory.memory_manager import MemoryManager
from pools.component_pools import build_doe_pool, candidate_to_key


class _GraphDummyLLM:
    def bind_tools(self, tools):
        del tools
        return self

    def invoke(self, messages):
        raise AssertionError(f"Unexpected direct LLM invocation: {messages}")


def _example_problem(name: str) -> dict:
    root = Path(__file__).resolve().parents[1]
    return load_problem_file(root / "examples" / f"{name}_problem.yaml")


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


def _slot_order(value: str) -> int:
    return {"anchor": 0, "contrast": 1, "wildcard": 2}[value]


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
                                    "text": "Knowledge-guided starts should quickly separate promising regions.",
                                    "mechanism": "The initialization should cover both precedent-favored and diverse settings.",
                                    "testable_prediction": "Early runs will reveal whether precedent-like conditions dominate.",
                                    "confidence": "medium",
                                    "status": "active",
                                }
                            ],
                            "working_memory_focus": "Use knowledge cards to balance exploration and exploitation.",
                        }
                    )
                )
            )
            return messages, "", _usage()

        if "You are designing the initial experimental campaign for chemical reaction optimization." in prompt:
            search_payload = tool_map["warm_start_candidate_search"].invoke(
                {
                    "objective": "dataset-grounded deterministic warm start",
                    "preferences": [],
                    "must_include": {"temperature": [105, 120]} if "temperature" in prompt else {},
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
                            "strategy_summary": "Use region-level chemistry guidance, then add contrasting controls.",
                            "anchor_regions": [
                                {
                                    "name": "knowledge-guided high-temperature region",
                                    "filter": {"temperature": [105, 120]} if "kc_temp" in prompt else {},
                                    "quota": max(1, round(target * 0.3)),
                                    "priority": 1,
                                    "reason": "Moderate-to-high temperatures are a strong starting region.",
                                    "knowledge_card_ids": ["kc_temp"] if "kc_temp" in prompt else [],
                                },
                                {
                                    "name": "ligand-coverage region",
                                    "filter": {"ligand_SMILES": [state["problem_spec"]["variables"][1]["domain"][0]]}
                                    if len(state["problem_spec"].get("variables", [])) >= 2 and state["problem_spec"]["variables"][1]["name"] == "ligand_SMILES"
                                    else {},
                                    "quota": 1,
                                    "priority": 2,
                                    "reason": "Use one anchor to stabilize ligand precedent coverage.",
                                    "knowledge_card_ids": ["kc_ligand"] if "kc_ligand" in prompt else [],
                                },
                            ],
                            "wildcard_regions": [
                                {
                                    "name": "broad wildcard coverage region",
                                    "filter": {},
                                    "quota": max(1, round(target * 0.3)),
                                    "priority": 1,
                                    "reason": "Reserve space for broader diversity across the feasible pool.",
                                    "knowledge_card_ids": [],
                                }
                            ],
                            "contrast_variable_priority": ["temperature", "ligand_SMILES", "solvent_SMILES"],
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
        initial_doe_size=10,
        max_bo_iterations=35,
        human_input_mode="dataset_auto",
    )
    graph = build_chembo_graph(settings)
    initial_state = create_initial_state(problem_spec, settings)
    config = {"configurable": {"thread_id": f"test-{uuid.uuid4().hex[:8]}"}}
    list(graph.stream(initial_state, config=config, stream_mode="updates"))
    return graph.get_state(config).values


def test_build_doe_pool_dataset_backed_is_deterministic_and_covers_top_values() -> None:
    problem_spec = _example_problem("dar")
    oracle = DatasetOracle.from_problem_spec(problem_spec)
    assert oracle is not None

    pool_a = build_doe_pool(
        problem_spec["variables"],
        pool_size=20,
        seed=7,
        observed_keys=set(),
        hard_constraints=[],
        candidate_pool=list(oracle.candidates),
    )
    pool_b = build_doe_pool(
        problem_spec["variables"],
        pool_size=20,
        seed=7,
        observed_keys=set(),
        hard_constraints=[],
        candidate_pool=list(oracle.candidates),
    )

    assert pool_a == pool_b
    assert len(pool_a) == 20
    assert len({candidate_to_key(candidate) for candidate in pool_a}) == 20
    assert all(oracle.candidate_exists(candidate) for candidate in pool_a)

    for variable in problem_spec["variables"]:
        if variable.get("type") == "continuous":
            continue
        name = variable["name"]
        counts = Counter(candidate.get(name) for candidate in oracle.candidates)
        top_values = [
            value
            for value, _count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        ][: min(len(variable.get("domain", [])), 4)]
        selected_values = {candidate.get(name) for candidate in pool_a}
        assert set(top_values) <= selected_values


def test_build_doe_pool_mixed_space_without_dataset_is_unique_and_bounded() -> None:
    variables = [
        {"name": "ligand", "type": "categorical", "domain": ["A", "B", "C", "D"]},
        {"name": "temperature", "type": "continuous", "domain": [60.0, 120.0]},
    ]
    pool_a = build_doe_pool(variables, pool_size=12, seed=11)
    pool_b = build_doe_pool(variables, pool_size=12, seed=11)

    assert pool_a == pool_b
    assert len(pool_a) == 12
    assert len({candidate_to_key(candidate) for candidate in pool_a}) == 12
    assert all(candidate["ligand"] in {"A", "B", "C", "D"} for candidate in pool_a)
    assert all(60.0 <= float(candidate["temperature"]) <= 120.0 for candidate in pool_a)


def test_settings_default_initial_doe_size_is_10() -> None:
    assert Settings().initial_doe_size == 10


def test_delta_best_supports_fast_interpretation_digest() -> None:
    assert _delta_best(40.0, 55.5, "maximize") == 15.5
    assert _delta_best(40.0, 35.0, "minimize") == 5.0
    assert _delta_best(None, 35.0, "maximize") is None
    assert _delta_best(40.0, None, "maximize") is None


@pytest.mark.parametrize(
    ("budget", "expected_target"),
    [
        (35, 17),
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


def test_plan_warm_start_is_deterministic_and_pairs_anchor_and_contrast() -> None:
    settings = Settings(initial_doe_size=10, max_bo_iterations=35)
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
    if "contrast" in categories:
        first_contrast = categories.index("contrast")
        assert "anchor" in categories[:first_contrast]
    wildcard_positions = [index for index, category in enumerate(categories) if category == "wildcard"]
    contrast_positions = [index for index, category in enumerate(categories) if category == "contrast"]
    if wildcard_positions and contrast_positions:
        assert min(wildcard_positions) >= min(contrast_positions)


def test_plan_warm_start_consumes_deck_text_without_prior_metadata() -> None:
    settings = Settings(initial_doe_size=10, max_bo_iterations=35)
    problem_spec = _example_problem("dar")
    state = create_initial_state(problem_spec, settings)
    state["knowledge_state"] = {
        "target_family": "DAR",
        "knowledge_profile": "homogeneous_cross_coupling",
        "coverage_level": "partial",
        "source_health_summary": {"local_rag": "ok"},
    }
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

    assert updates["warm_start_queue"]
    assert any(item["warm_start_card_refs"] for item in updates["warm_start_queue"])
    assert all("applied_prior_ids" not in item for item in updates["warm_start_queue"])
    assert "knowledge_serving_stats" not in updates
    assert set(item["warm_start_category"] for item in updates["warm_start_queue"]) <= {"anchor", "contrast", "wildcard"}


def test_plan_warm_start_changes_when_knowledge_cards_text_changes() -> None:
    settings = Settings(initial_doe_size=10, max_bo_iterations=35)
    problem_spec = _example_problem("dar")

    state_with_cards = create_initial_state(problem_spec, settings)
    state_with_cards["knowledge_deck"] = {"cards": _sample_knowledge_cards(), "build_summary": {"coverage_level": "partial"}}
    updates_with_cards = plan_warm_start(
        state_with_cards,
        settings,
        _GraphDummyLLM(),
        invoke_tool_loop=_invoke_tool_loop_factory(),
        extract_last_json=_direct_extract_last_json,
        state_messages=_state_messages_identity,
        updated_campaign_summary=_updated_campaign_summary_stub,
        attach_llm_usage=_attach_llm_usage_stub,
    )

    state_without_cards = create_initial_state(problem_spec, settings)
    state_without_cards["knowledge_deck"] = {"cards": [], "build_summary": {"coverage_level": "gap"}}
    updates_without_cards = plan_warm_start(
        state_without_cards,
        settings,
        _GraphDummyLLM(),
        invoke_tool_loop=_invoke_tool_loop_factory(),
        extract_last_json=_direct_extract_last_json,
        state_messages=_state_messages_identity,
        updated_campaign_summary=_updated_campaign_summary_stub,
        attach_llm_usage=_attach_llm_usage_stub,
    )

    queue_with_cards = [candidate_to_key(item["candidate"]) for item in updates_with_cards["warm_start_queue"]]
    queue_without_cards = [candidate_to_key(item["candidate"]) for item in updates_without_cards["warm_start_queue"]]
    assert queue_with_cards != queue_without_cards
    assert any(item["warm_start_card_refs"] for item in updates_with_cards["warm_start_queue"])
    assert all(not item["warm_start_card_refs"] for item in updates_without_cards["warm_start_queue"])


def test_plan_warm_start_falls_back_to_doe_pool_when_regions_missing() -> None:
    settings = Settings(initial_doe_size=10, max_bo_iterations=35)
    problem_spec = _example_problem("dar")
    state = create_initial_state(problem_spec, settings)
    state["knowledge_deck"] = {"cards": _sample_knowledge_cards(), "build_summary": {"coverage_level": "partial"}}

    def _invoke_tool_loop_no_regions(llm, state, prompt, tool_map, max_turns=6, **kwargs):
        del llm, state, tool_map, max_turns, kwargs
        return [HumanMessage(content=prompt), AIMessage(content=json.dumps({"strategy_summary": "legacy-fallback"}))], "", _usage()

    updates = plan_warm_start(
        state,
        settings,
        _GraphDummyLLM(),
        invoke_tool_loop=_invoke_tool_loop_no_regions,
        extract_last_json=_direct_extract_last_json,
        state_messages=_state_messages_identity,
        updated_campaign_summary=_updated_campaign_summary_stub,
        attach_llm_usage=_attach_llm_usage_stub,
    )

    assert updates["warm_start_queue"]
    assert "doe_pool_fallback" in updates["llm_reasoning_log"][-1]
    assert set(item["warm_start_category"] for item in updates["warm_start_queue"]) <= {"anchor", "contrast", "wildcard"}


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
    assert len(state["warm_start_queue"]) == 10
    assert state["warm_start_active"] is True
    assert state["proposal_selected"]["selection_source"] == "warm_start_queue"
    assert oracle is not None
    assert oracle.candidate_exists(state["proposal_selected"]["candidate"])
    categories = [item["warm_start_category"] for item in state["warm_start_queue"]]
    assert set(categories) <= {"anchor", "contrast", "wildcard"}
    if "contrast" in categories:
        assert categories.index("contrast") > categories.index("anchor")


def test_interpret_warm_start_result_stays_lightweight() -> None:
    settings = Settings(initial_doe_size=10, max_bo_iterations=35)
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
    settings = Settings(initial_doe_size=10, max_bo_iterations=35)
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


def _simple_region_variables() -> list[dict]:
    return [
        {"name": "ligand", "type": "categorical", "domain": ["A", "B", "C"]},
        {"name": "solvent", "type": "categorical", "domain": ["S1", "S2"]},
        {"name": "temperature", "type": "continuous", "domain": [60.0, 120.0]},
    ]


def _simple_region_pool() -> list[dict]:
    return [
        {"ligand": "A", "solvent": "S1", "temperature": "60"},
        {"ligand": "A", "solvent": "S1", "temperature": "90"},
        {"ligand": "A", "solvent": "S2", "temperature": "90"},
        {"ligand": "B", "solvent": "S1", "temperature": "90"},
        {"ligand": "B", "solvent": "S2", "temperature": "120"},
        {"ligand": "C", "solvent": "S2", "temperature": "120"},
    ]


def test_empty_filter_matches_all_candidates() -> None:
    region = RegionSpec(
        name="all",
        slot_type="wildcard",
        filters={},
        quota=3,
        priority=1,
        reason="",
        knowledge_card_ids=[],
    )
    matched = _candidates_matching_region(region, _simple_region_pool(), _simple_region_variables(), set())
    assert len(matched) == len(_simple_region_pool())


def test_region_filter_supports_categorical_and_continuous_ranges() -> None:
    region = RegionSpec(
        name="focused",
        slot_type="anchor",
        filters={"ligand": ["A", "B"], "temperature": [85.0, 95.0]},
        quota=2,
        priority=1,
        reason="",
        knowledge_card_ids=[],
    )
    matched = _candidates_matching_region(region, _simple_region_pool(), _simple_region_variables(), set())
    assert {candidate_to_key(candidate) for candidate in matched} == {
        candidate_to_key({"ligand": "A", "solvent": "S1", "temperature": "90"}),
        candidate_to_key({"ligand": "A", "solvent": "S2", "temperature": "90"}),
        candidate_to_key({"ligand": "B", "solvent": "S1", "temperature": "90"}),
    }


def test_normalize_region_guidance_clips_filters_and_can_zero_low_priority_regions() -> None:
    payload = {
        "strategy_summary": "test",
        "anchor_regions": [
            {
                "name": "valid",
                "filter": {"ligand": ["A", "Z"], "temperature": [50.0, 95.0], "unknown": ["x"]},
                "quota": 3,
                "priority": 1,
                "knowledge_card_ids": ["kc_1", "bad"],
            },
            {
                "name": "overflow",
                "filter": {"ligand": ["B"]},
                "quota": 2,
                "priority": 9,
                "knowledge_card_ids": [],
            },
        ],
        "wildcard_regions": [],
        "contrast_variable_priority": ["temperature", "unknown"],
    }
    guidance = _normalize_region_guidance(
        payload,
        target=4,
        valid_card_ids={"kc_1"},
        variables=_simple_region_variables(),
    )
    anchors = guidance["anchor_regions"]
    assert anchors[0].filters == {"ligand": ["A"], "temperature": [60.0, 95.0]}
    assert anchors[0].knowledge_card_ids == ["kc_1"]
    assert sum(region.quota for region in anchors) == 1
    assert anchors[1].quota == 0
    assert guidance["contrast_variable_priority"] == ["temperature"]


def test_farthest_first_sample_is_deterministic() -> None:
    sampled_a = _farthest_first_sample(_simple_region_pool(), _simple_region_variables(), 3)
    sampled_b = _farthest_first_sample(_simple_region_pool(), _simple_region_variables(), 3)
    assert sampled_a == sampled_b
    assert len(sampled_a) == 3
    assert len({candidate_to_key(candidate) for candidate in sampled_a}) == 3


def test_contrast_variables_preserve_requested_order_after_support_filtering() -> None:
    ranked = _filter_and_rank_contrast_variables(
        ["solvent", "temperature"],
        _simple_region_pool(),
        _simple_region_variables(),
    )
    assert ranked[:2] == ["solvent", "temperature"]
    assert "solvent" in ranked


def test_generate_contrast_candidates_prefers_precise_controls() -> None:
    anchor = {"ligand": "A", "solvent": "S1", "temperature": "90"}
    contrasts = _generate_contrast_candidates(
        anchor_candidates=[anchor],
        contrast_var_priority=["temperature", "solvent"],
        full_pool=_simple_region_pool(),
        variables=_simple_region_variables(),
        n_contrast=1,
        excluded_keys={candidate_to_key(anchor)},
    )
    assert len(contrasts) == 1
    assert contrasts[0]["candidate"] == {"ligand": "A", "solvent": "S1", "temperature": "60"}


def test_sort_warm_start_queue_pairs_anchor_then_contrast_then_wildcard() -> None:
    shortlist = [
        {
            "candidate": {"ligand": "A", "solvent": "S1", "temperature": "90"},
            "warm_start_category": "anchor",
            "warm_start_rationale": "",
            "warm_start_card_refs": [],
            "warm_start_index": -1,
            "_warm_start_anchor_key": candidate_to_key({"ligand": "A", "solvent": "S1", "temperature": "90"}),
        },
        {
            "candidate": {"ligand": "A", "solvent": "S1", "temperature": "60"},
            "warm_start_category": "contrast",
            "warm_start_rationale": "",
            "warm_start_card_refs": [],
            "warm_start_index": -1,
            "_warm_start_pair_anchor_key": candidate_to_key({"ligand": "A", "solvent": "S1", "temperature": "90"}),
            "_warm_start_pair_rank": 0,
        },
        {
            "candidate": {"ligand": "C", "solvent": "S2", "temperature": "120"},
            "warm_start_category": "wildcard",
            "warm_start_rationale": "",
            "warm_start_card_refs": [],
            "warm_start_index": -1,
        },
    ]
    ordered = _sort_warm_start_queue(shortlist, _simple_region_variables())
    assert [item["warm_start_category"] for item in ordered] == ["anchor", "contrast", "wildcard"]
    assert all(not any(key.startswith("_warm_start_") for key in item) for item in ordered)
