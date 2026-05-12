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
    _extract_json_from_response,
    _extract_last_json,
    _merge_llm_usage,
    _rank_and_filter_cards,
    _update_hypothesis_statuses,
    build_chembo_graph,
    compute_convergence_state,
)
from core.problem_loader import load_problem_file
from core.state import create_initial_state
from core.warm_start import (
    _build_random_warm_start_pool,
    _extract_partial_direct_selection_payloads,
    interpret_warm_start_result,
    plan_warm_start,
    run_warm_start_postmortem,
)
from knowledge.prior_writer import write_initial_priors
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
                        "information_value": "Runs before random fill.",
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
                            "strategy_summary": "Choose high-value direct warm-start points before seeded random fill.",
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
    monkeypatch.setattr(
        "core.graph._create_llm",
        lambda settings, enable_thinking_override=None, **kwargs: _GraphDummyLLM(),
    )
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


def test_build_random_warm_start_pool_dataset_backed_is_seeded_and_excludes_observed() -> None:
    problem_spec = _example_problem("dar")
    oracle = DatasetOracle.from_problem_spec(problem_spec)
    assert oracle is not None
    observed_keys = {candidate_to_key(dict(oracle.candidates[0])), candidate_to_key(dict(oracle.candidates[1]))}

    pool_a = _build_random_warm_start_pool(
        problem_spec["variables"],
        pool_size=25,
        seed=7,
        observed_keys=observed_keys,
        hard_constraints=[],
        candidate_pool=list(oracle.candidates),
    )
    pool_b = _build_random_warm_start_pool(
        problem_spec["variables"],
        pool_size=25,
        seed=7,
        observed_keys=observed_keys,
        hard_constraints=[],
        candidate_pool=list(oracle.candidates),
    )
    pool_c = _build_random_warm_start_pool(
        problem_spec["variables"],
        pool_size=25,
        seed=8,
        observed_keys=observed_keys,
        hard_constraints=[],
        candidate_pool=list(oracle.candidates),
    )

    assert pool_a == pool_b
    assert pool_a != pool_c
    assert len(pool_a) == 25
    assert len({candidate_to_key(candidate) for candidate in pool_a}) == 25
    assert all(oracle.candidate_exists(candidate) for candidate in pool_a)
    assert not {candidate_to_key(candidate) for candidate in pool_a} & observed_keys


def test_build_random_warm_start_pool_mixed_space_without_dataset_is_seeded_and_bounded() -> None:
    variables = _toy_variables()
    pool_a = _build_random_warm_start_pool(
        variables,
        pool_size=12,
        seed=11,
        observed_keys=set(),
        hard_constraints=[],
        candidate_pool=None,
    )
    pool_b = _build_random_warm_start_pool(
        variables,
        pool_size=12,
        seed=11,
        observed_keys=set(),
        hard_constraints=[],
        candidate_pool=None,
    )
    pool_c = _build_random_warm_start_pool(
        variables,
        pool_size=12,
        seed=12,
        observed_keys=set(),
        hard_constraints=[],
        candidate_pool=None,
    )

    assert pool_a == pool_b
    assert pool_a != pool_c
    assert len(pool_a) == 12
    assert len({candidate_to_key(candidate) for candidate in pool_a}) == 12
    assert all(candidate["ligand"] in {"A", "B", "C", "D"} for candidate in pool_a)
    assert all(candidate["solvent"] in {"S1", "S2"} for candidate in pool_a)
    assert all(60.0 <= float(candidate["temperature"]) <= 120.0 for candidate in pool_a)


def test_build_random_warm_start_pool_excludes_initial_selected_candidates() -> None:
    initial_selected = [_toy_pool()[0], _toy_pool()[1]]
    pool = _build_random_warm_start_pool(
        _toy_variables(),
        pool_size=4,
        seed=13,
        observed_keys=set(),
        hard_constraints=[],
        candidate_pool=_toy_pool(),
        initial_selected=initial_selected,
    )

    pool_keys = {candidate_to_key(candidate) for candidate in pool}
    initial_keys = {candidate_to_key(candidate) for candidate in initial_selected}
    assert len(pool) == 4
    assert not pool_keys & initial_keys


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
        "random"
    ] * (expected_target - expected_direct)


def test_plan_warm_start_direct_half_first_then_random_is_deterministic() -> None:
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
    assert categories[10:] == ["random"] * 10
    assert len({candidate_to_key(item["candidate"]) for item in first["warm_start_queue"]}) == 20


def test_plan_warm_start_retries_invalid_direct_points_then_random_fills() -> None:
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
    assert [item["warm_start_category"] for item in updates["warm_start_queue"]].count("random") == 3
    assert len({candidate_to_key(item["candidate"]) for item in updates["warm_start_queue"]}) == 4


def test_plan_warm_start_repairs_unparseable_direct_response() -> None:
    settings = Settings(initial_doe_size=4, max_bo_iterations=40)
    problem_spec = _example_problem("dar")
    state = create_initial_state(problem_spec, settings)
    state["knowledge_deck"] = {"cards": [], "build_summary": {"coverage_level": "gap"}}
    oracle = DatasetOracle.from_problem_spec(problem_spec)
    assert oracle is not None
    calls = {"direct": 0, "repair": 0}

    def _invoke_tool_loop_needs_repair(llm, state, prompt, tool_map, max_turns=6, **kwargs):
        del llm, state, tool_map, max_turns, kwargs
        if "Select high-value direct warm-start experiments" not in prompt:
            raise AssertionError(f"Unexpected prompt:\n{prompt}")
        if "Return strict JSON only. Do not include prose" in prompt:
            calls["repair"] += 1
            content = json.dumps(
                {
                    "strategy_summary": "Repair the draft into strict JSON.",
                    "selections": [
                        {
                            "variables": dict(oracle.candidates[0]),
                            "reasoning": "Recovered direct warm-start seed 1.",
                            "confidence": 0.8,
                        },
                        {
                            "variables": dict(oracle.candidates[1]),
                            "reasoning": "Recovered direct warm-start seed 2.",
                            "confidence": 0.75,
                        },
                    ],
                }
            )
        else:
            calls["direct"] += 1
            content = (
                "I will think step by step before giving JSON.\n"
                '{"strategy_summary":"Draft","selections":[{"variables":{"broken":"response"}}'
            )
        return [HumanMessage(content=prompt), AIMessage(content=content)], "", _usage()

    updates = plan_warm_start(
        state,
        settings,
        _GraphDummyLLM(),
        invoke_tool_loop=_invoke_tool_loop_needs_repair,
        extract_last_json=_extract_last_json,
        state_messages=_state_messages_identity,
        updated_campaign_summary=_updated_campaign_summary_stub,
        attach_llm_usage=_attach_llm_usage_stub,
    )

    assert calls["direct"] == 1
    assert calls["repair"] == 1
    assert len(updates["warm_start_queue"]) == 4
    assert [item["warm_start_category"] for item in updates["warm_start_queue"]].count("llm_direct") == 2
    assert len({candidate_to_key(item["candidate"]) for item in updates["warm_start_queue"]}) == 4


def test_extract_json_from_response_handles_embedded_prose_and_trailing_text() -> None:
    text = """
    Some reasoning before the answer.

    ```json
    {"strategy_summary": "Recovered", "selections": [{"variables": {"temperature": "120"}}]}
    ```

    Extra commentary after the code block.
    """

    parsed = _extract_json_from_response(text)

    assert parsed is not None
    assert parsed["strategy_summary"] == "Recovered"


def test_initial_prior_writer_requires_four_hypotheses_and_warm_start_actionability() -> None:
    problem = _example_problem("ocm")
    raw_cards = [
        {
            "text": "Methane activation and oxygen availability jointly control coupling versus complete oxidation selectivity.",
            "card_type": "mechanism",
            "scope": "target",
            "confidence": 0.58,
            "targets": [],
            "actionable_for": ["select_candidate"],
        },
        {
            "text": "Primary redox metals tune lattice oxygen reactivity and therefore methane activation rates.",
            "card_type": "reagent_property",
            "scope": "target",
            "confidence": 0.55,
            "targets": ["M1"],
            "actionable_for": ["select_candidate"],
        },
        {
            "text": "Alkali promoters can increase surface basicity and suppress nonselective oxidation pathways.",
            "card_type": "reagent_property",
            "scope": "target",
            "confidence": 0.55,
            "targets": ["M2"],
            "actionable_for": ["select_candidate"],
        },
        {
            "text": "Intermediate oxygen availability should avoid both conversion starvation and excessive total oxidation.",
            "card_type": "operating_window",
            "scope": "target",
            "confidence": 0.55,
            "targets": ["O2_flow"],
            "actionable_for": ["select_candidate"],
        },
        {
            "text": "Very high operating severity can promote complete oxidation and mobile promoter loss.",
            "card_type": "failure_mode",
            "scope": "target",
            "confidence": 0.55,
            "targets": ["Temp"],
            "actionable_for": ["select_candidate"],
        },
        {
            "text": "Support identity can interact with alkali promoters through acid base site density.",
            "card_type": "interaction",
            "scope": "target",
            "confidence": 0.55,
            "targets": ["Support", "M2"],
            "actionable_for": ["select_candidate"],
        },
        {
            "text": "Methane rich feeds should improve C2 plus selectivity when oxygen activation remains sufficient.",
            "card_type": "hypothesis",
            "scope": "target",
            "confidence": 0.55,
            "targets": [],
            "testable_prediction": "Methane rich warm starts outperform oxygen rich conditions for similar catalysts.",
            "actionable_for": ["select_candidate"],
        },
        {
            "text": "Sodium tungstate manganese catalysts should outperform support only controls in early warm starts.",
            "card_type": "hypothesis",
            "scope": "target",
            "confidence": 0.55,
            "targets": [],
            "testable_prediction": "Mn Na W catalysts beat blank or support only rows at matched conditions.",
            "actionable_for": ["select_candidate"],
        },
        {
            "text": "Silica supported tungstate systems should reveal productive regions before reducible oxide supports.",
            "card_type": "hypothesis",
            "scope": "target",
            "confidence": 0.55,
            "targets": [],
            "testable_prediction": "SiO2 tungstate rows rank above CeO2 or Nb2O5 support only rows.",
            "actionable_for": ["select_candidate"],
        },
        {
            "text": "Moderate temperature should balance methane conversion and C2 plus selectivity better than extremes.",
            "card_type": "hypothesis",
            "scope": "target",
            "confidence": 0.55,
            "targets": [],
            "testable_prediction": "Middle temperature candidates outperform low and high temperature matched rows.",
            "actionable_for": ["select_candidate"],
        },
        {
            "text": "Lower oxygen fractions can reduce over oxidation when catalyst oxygen mobility is high.",
            "card_type": "operating_window",
            "scope": "target",
            "confidence": 0.55,
            "targets": ["O2_flow"],
            "actionable_for": ["select_candidate"],
        },
        {
            "text": "Basic supports may extend methyl radical lifetimes and increase coupling probability.",
            "card_type": "reagent_property",
            "scope": "target",
            "confidence": 0.55,
            "targets": ["Support"],
            "actionable_for": ["select_candidate"],
        },
    ]

    def _invoke_json(llm, system_prompt, user_prompt, default):
        del llm, system_prompt, user_prompt, default
        return {"cards": raw_cards, "global_notes": ""}, _usage()

    cards, _ = write_initial_priors(problem, Settings(), _GraphDummyLLM(), _invoke_json)

    assert len(cards) == 12
    assert sum(card["card_type"] == "hypothesis" for card in cards) >= 4
    assert all("warm_start" in card["actionable_for"] for card in cards)


def test_active_deck_preserves_hypotheses_for_warm_start() -> None:
    cards = [
        {
            "card_id": f"kc_constraint_{index}",
            "text": f"Only propose valid dataset backed candidates during initialization phase {index}.",
            "card_type": "constraint",
            "scope": "target",
            "confidence": 1.0,
            "status": "active",
            "actionable_for": ["warm_start"],
        }
        for index in range(5)
    ]
    cards += [
        {
            "card_id": f"kc_prior_{index}",
            "text": f"Catalyst support and promoter chemistry provide useful warm start evidence {index}.",
            "card_type": "reagent_property",
            "scope": "target",
            "confidence": 0.6,
            "status": "active",
            "actionable_for": ["warm_start"],
        }
        for index in range(8)
    ]
    cards += [
        {
            "card_id": f"kc_hypothesis_{index}",
            "text": f"Warm start should prioritize plausible catalysts over support only controls {index}.",
            "card_type": "hypothesis",
            "scope": "target",
            "confidence": 0.6,
            "status": "active",
            "actionable_for": ["warm_start"],
            "testable_prediction": "Plausible catalysts beat support only controls in early observations.",
        }
        for index in range(4)
    ]

    selected = _rank_and_filter_cards(cards, max_cards=12, min_hypotheses=4)

    assert len(selected) == 12
    assert sum(card["card_type"] == "hypothesis" for card in selected) == 4
    assert all("warm_start" in card["actionable_for"] for card in selected)


def test_partial_warm_start_json_recovery_handles_truncated_array() -> None:
    text = (
        'Some analysis that should be ignored {"strategy_summary":"x","selections":['
        '{"cat":"32","Temp":800,"CT":0.38,"ar_level":"low","ch4_o2_ratio":2,'
        '"reasoning":"classic catalyst","confidence":0.8},'
        '{"cat":"28","Temp":850,"CT":0.5,"ar_level":"mid","ch4_o2_ratio":1,'
        '"reasoning":"support contrast","confidence":0.75}'
    )

    recovered = _extract_partial_direct_selection_payloads(text)

    assert [item["cat"] for item in recovered] == ["32", "28"]


def test_plan_warm_start_reduces_target_when_dataset_candidates_are_insufficient() -> None:
    settings = Settings(initial_doe_size=6, max_bo_iterations=40)
    problem_spec = _example_problem("dar")
    state = create_initial_state(problem_spec, settings)
    oracle = DatasetOracle.from_problem_spec(problem_spec)
    assert oracle is not None
    state["observations"] = [
        {"candidate": dict(candidate), "result": float(index)}
        for index, candidate in enumerate(oracle.candidates[:-3])
    ]

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

    assert updates["warm_start_target"] == 3
    assert len(updates["warm_start_queue"]) == 3
    assert "Warm-start target reduced from 6 to 3" in updates["campaign_summary"]


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
    assert set(categories) <= {"llm_direct", "random"}


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
