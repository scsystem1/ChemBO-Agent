from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

import pytest

pytest.importorskip("langchain_core")
pytest.importorskip("langgraph")

from langchain_core.messages import AIMessage, HumanMessage

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
            "source_type": "llm_internal_prior",
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
            "source_type": "llm_internal_prior",
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


def _parse_direct_target(prompt: str) -> int:
    match = re.search(r"Return exactly\s+(\d+)\s+", prompt)
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

        if "Select high-value direct warm-start experiments" in prompt:
            target = _parse_direct_target(prompt)
            problem_spec = state.get("problem_spec", {})
            oracle = DatasetOracle.from_problem_spec(problem_spec)
            if oracle is not None and str(problem_spec.get("reaction_type", "")).upper() != "OCM":
                observed = {
                    candidate_to_key(item.get("candidate", {}))
                    for item in state.get("observations", [])
                    if item.get("candidate")
                }
                selections = [
                    {
                        "variables": dict(candidate),
                        "reasoning": f"High-value direct warm-start seed {index + 1}.",
                        "hypothesis_alignment": "Tests a chemically plausible early region.",
                        "information_value": "Runs before coverage fill.",
                        "concerns": "",
                        "confidence": 0.8,
                    }
                    for index, candidate in enumerate(
                        candidate for candidate in oracle.candidates if candidate_to_key(candidate) not in observed
                    )
                    if index < target
                ]
            else:
                from core.autobo_engine import _build_pure_reasoning_space_spec

                spec = _build_pure_reasoning_space_spec(state)
                default_selection = dict((spec or {}).get("default_response", {}))
                default_selection["reasoning"] = "Use the default encoded-domain warm-start seed."
                selections = [dict(default_selection) for _ in range(target)]
            messages.append(
                AIMessage(
                    content=json.dumps(
                        {
                            "strategy_summary": "Choose high-value direct warm-start points before deterministic coverage fill.",
                            "selections": selections,
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
        "core.graph.write_initial_priors",
        lambda problem_spec, settings, llm, invoke_json: (cards, {"needs_evidence": [], "llm_usage": {}}),
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
    expected_direct = (expected_target + 1) // 2
    assert [item["warm_start_category"] for item in updates["warm_start_queue"][:expected_direct]] == [
        "llm_direct"
    ] * expected_direct
    assert [item["warm_start_category"] for item in updates["warm_start_queue"][expected_direct:]] == [
        "coverage"
    ] * (expected_target - expected_direct)


def test_plan_warm_start_direct_half_first_then_coverage_is_deterministic() -> None:
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
    assert categories[:10] == ["llm_direct"] * 10
    assert categories[10:] == ["coverage"] * 10
    assert len({candidate_to_key(item["candidate"]) for item in first["warm_start_queue"]}) == 20

    for variable in problem_spec["variables"]:
        if variable.get("type") == "continuous":
            continue
        selected_values = {str(item["candidate"].get(variable["name"], "")) for item in first["warm_start_queue"]}
        assert set(map(str, variable.get("domain", []))) <= selected_values


def test_plan_warm_start_retries_invalid_direct_points_then_coverage_fills() -> None:
    settings = Settings(initial_doe_size=4, max_bo_iterations=40)
    problem_spec = _example_problem("dar")
    state = create_initial_state(problem_spec, settings)
    state["knowledge_deck"] = {"cards": [], "build_summary": {"coverage_level": "gap"}}
    oracle = DatasetOracle.from_problem_spec(problem_spec)
    assert oracle is not None
    calls = {"direct": 0}

    def _invoke_tool_loop_invalid_then_sparse(llm, state, prompt, tool_map, max_turns=6, **kwargs):
        del llm, state, tool_map, max_turns, kwargs
        if "Select high-value direct warm-start experiments" not in prompt:
            raise AssertionError(f"Unexpected prompt:\n{prompt}")
        calls["direct"] += 1
        if calls["direct"] == 1:
            selections = [
                {
                    "variables": {column: "not-a-real-level" for column in oracle.feature_columns},
                    "reasoning": "Invalid point that should trigger validation feedback.",
                    "confidence": 0.2,
                }
            ]
        else:
            selections = [
                {
                    "variables": dict(oracle.candidates[0]),
                    "reasoning": "Valid replacement direct seed after validation feedback.",
                    "confidence": 0.8,
                }
            ]
        return [
            HumanMessage(content=prompt),
            AIMessage(
                content=json.dumps(
                    {
                        "strategy_summary": "Retry invalid direct warm-start points.",
                        "selections": selections,
                    }
                )
            ),
        ], "", _usage()

    updates = plan_warm_start(
        state,
        settings,
        _GraphDummyLLM(),
        invoke_tool_loop=_invoke_tool_loop_invalid_then_sparse,
        extract_last_json=_direct_extract_last_json,
        state_messages=_state_messages_identity,
        updated_campaign_summary=_updated_campaign_summary_stub,
        attach_llm_usage=_attach_llm_usage_stub,
    )

    assert calls["direct"] == 2
    assert len(updates["warm_start_queue"]) == 4
    assert [item["warm_start_category"] for item in updates["warm_start_queue"]].count("llm_direct") == 1
    assert [item["warm_start_category"] for item in updates["warm_start_queue"]].count("coverage") == 3
    assert len({candidate_to_key(item["candidate"]) for item in updates["warm_start_queue"]}) == 4


@pytest.mark.parametrize("problem_name", ["dar", "ocm"])
def test_graph_warm_start_smoke_uses_llm_direct_queue(problem_name: str, monkeypatch) -> None:
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
    assert categories[0] == "llm_direct"
    assert set(categories) <= {"llm_direct", "coverage"}


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
