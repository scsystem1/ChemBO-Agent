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
    _merge_llm_usage,
    _update_hypothesis_statuses,
    build_chembo_graph,
    compute_convergence_state,
)
from core.problem_loader import load_problem_file
from core.state import create_initial_state
from core.warm_start import (
    _build_warm_start_candidate_search_tool,
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
            "title": "Ligand prior",
            "category": "reagent_prior",
            "claim": "Bulky electron-rich ligands are often productive starting points.",
            "confidence": "high",
            "reaction_families": ["DAR"],
            "variables_affected": ["ligand_SMILES"],
            "actionable_for": ["warm_start", "hypothesis_generation"],
            "scope": "target",
            "leakage_state": "passed",
            "tags": ["test"],
            "evidence": [
                {
                    "source_type": "review",
                    "document_id": "doc-1",
                    "chunk_id": "chunk-1",
                    "citation": "Review A",
                    "snippet": "Bulky ligands can improve oxidative addition and reductive elimination balance.",
                    "metadata": {"top_values": ["P(C1CCCCC1)(C2CCCCC2)C3CCCCC3"]},
                }
            ],
        },
        {
            "card_id": "kc_temp",
            "title": "Temperature note",
            "category": "property",
            "claim": "Moderate-to-high temperatures are often needed to activate challenging coupling manifolds.",
            "confidence": "medium",
            "reaction_families": ["DAR"],
            "variables_affected": ["temperature"],
            "actionable_for": ["warm_start"],
            "scope": "general",
            "leakage_state": "passed",
            "tags": ["test"],
            "evidence": [
                {
                    "source_type": "review",
                    "document_id": "doc-2",
                    "chunk_id": "chunk-2",
                    "citation": "Review B",
                    "snippet": "Temperature can be a key activation lever.",
                    "metadata": {},
                }
            ],
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

        if "Design deterministic guidance for the warm-start planner." in prompt:
            if "Repair the invalid warm-start slots" in prompt:
                raise AssertionError("Legacy warm-start repair path should not be used.")
            search_payload = tool_map["warm_start_candidate_search"].invoke(
                {
                    "objective": "dataset-grounded deterministic warm start",
                    "preferences": [],
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
                            "strategy_summary": "Use deterministic coverage, then bias toward knowledge-supported settings.",
                            "preferred_patterns": [
                                {
                                    "variable": "temperature",
                                    "preferred_values": ["120", "105"],
                                    "weight": 1.2,
                                    "reason": "Moderate-to-high temperature prior.",
                                    "knowledge_card_ids": ["kc_temp"],
                                }
                            ],
                            "avoided_patterns": [],
                            "category_targets": {
                                "exploration": max(1, int(round(target * 0.5))),
                                "balanced": max(1, int(round(target * 0.3))),
                                "exploitation": max(0, target - max(1, int(round(target * 0.5))) - max(1, int(round(target * 0.3)))),
                            },
                            "priority_indices": [0, 1, 2],
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
        episode_keep_recent=int(getattr(settings, "memory_episode_keep_recent", 24)),
        episode_keep_salient=int(getattr(settings, "memory_episode_keep_salient", 96)),
    )


def _run_to_first_interrupt(monkeypatch, problem_spec: dict, *, cards: list[dict]) -> dict:
    monkeypatch.setattr("core.graph._create_llm", lambda settings, enable_thinking_override=None: _GraphDummyLLM())
    monkeypatch.setattr(
        "core.graph.run_knowledge_augmentation",
        lambda problem_spec, settings: (cards, {"card_count": len(cards)}),
    )
    monkeypatch.setattr("core.graph._invoke_tool_loop", _invoke_tool_loop_factory())

    settings = Settings(
        initial_doe_size=6,
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
    state["knowledge_cards"] = _sample_knowledge_cards()

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


def test_plan_warm_start_is_deterministic_and_orders_queue_by_category() -> None:
    settings = Settings(initial_doe_size=6, max_bo_iterations=35)
    problem_spec = _example_problem("dar")
    state = create_initial_state(problem_spec, settings)
    state["knowledge_cards"] = _sample_knowledge_cards()

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
    assert categories == sorted(categories, key=lambda item: {"exploration": 0, "balanced": 1, "exploitation": 2}[item])


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
    assert state["knowledge_cards"]
    assert state["retrieval_artifacts"]["card_count"] == len(_sample_knowledge_cards())
    assert len(state["warm_start_queue"]) == 6
    assert state["warm_start_active"] is True
    assert state["proposal_selected"]["selection_source"] == "warm_start_queue"
    assert oracle is not None
    assert oracle.candidate_exists(state["proposal_selected"]["candidate"])
    categories = [item["warm_start_category"] for item in state["warm_start_queue"]]
    assert categories == sorted(categories, key=lambda item: {"exploration": 0, "balanced": 1, "exploitation": 2}[item])


def test_interpret_warm_start_result_stays_lightweight() -> None:
    settings = Settings(initial_doe_size=6, max_bo_iterations=35)
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

    def _invoke_llm_with_tracking(llm, messages):
        del llm
        return (
            AIMessage(
                content=json.dumps(
                    {
                        "interpretation": "Warm-start point improved the current benchmark.",
                        "supported_hypotheses": [],
                        "refuted_hypotheses": [],
                        "archived_hypotheses": [],
                        "episodic_memory": {
                            "reflection": "Warm-start observation logged.",
                            "lesson_learned": "",
                            "non_numerical_observations": "",
                            "causal_attributions": [],
                            "hypothesis_evidence": [],
                            "knowledge_tension": {"has_conflict": False, "conflicting_cards": [], "reason": ""},
                        },
                        "semantic_rule": None,
                        "working_memory": {"current_focus": "Collecting warm-start data.", "pending_decisions": []},
                    }
                )
            ),
            _usage(),
        )

    updates = interpret_warm_start_result(
        state,
        settings,
        _GraphDummyLLM(),
        memory_manager=memory_manager,
        build_context_messages=lambda state, **kwargs: ([HumanMessage(content="context")], ""),
        invoke_llm_with_tracking=_invoke_llm_with_tracking,
        extract_json_from_response=lambda text: json.loads(text),
        message_text=lambda message: message.content,
        state_messages=_state_messages_identity,
        updated_campaign_summary=_updated_campaign_summary_stub,
        attach_llm_usage=_attach_llm_usage_stub,
    )

    assert len(updates["memory"]["semantic"]["nodes"]) == semantic_before
    assert "[interpret_results:lightweight]" in updates["llm_reasoning_log"][-2]
    assert "hypotheses" not in updates


def test_run_warm_start_postmortem_only_uses_warm_start_observations_and_updates_memory() -> None:
    settings = Settings(initial_doe_size=6, max_bo_iterations=35)
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
        build_context_messages=lambda state, **kwargs: ([HumanMessage(content="context")], ""),
        invoke_llm_with_tracking=_invoke_llm_with_tracking,
        extract_json_from_response=lambda text: json.loads(text),
        message_text=lambda message: message.content,
        compute_convergence_state=compute_convergence_state,
        update_hypothesis_statuses=_update_hypothesis_statuses,
        merge_llm_usage=_merge_llm_usage,
    )

    assert '"selection_source": "warm_start_queue"' in captured_prompt["text"]
    assert '"selection_source": "autobo"' not in captured_prompt["text"]
    assert payload["batch_interpretation"].startswith("Warm-start established")
    assert payload["hypotheses"][0]["status"] == "supported"
    assert payload["added_rule_count"] == 1
    assert payload["memory"]["semantic"]["nodes"]
