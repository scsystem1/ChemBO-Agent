from __future__ import annotations

import csv
import json
import uuid
from pathlib import Path

import pytest

pytest.importorskip("langchain_core")
pytest.importorskip("langgraph")

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from config.settings import Settings
from core.dataset_oracle import DatasetOracle
from core.graph import (
    _build_reasoning_fallback_candidates,
    _build_warm_start_candidate_search_tool,
    build_chembo_graph,
)
from core.problem_loader import load_problem_file
from core.state import create_initial_state
from tools.chembo_tools import bo_runner


class _DummyLLM:
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
            "variables_affected": ["ligand"],
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
                    "metadata": {},
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


def _invoke_tool_loop_factory(mode: str):
    def _fake_invoke_tool_loop(llm, state, prompt, tool_map, max_turns=6):
        del llm, max_turns
        usage = {
            "calls": 1,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "estimated_calls": 0,
            "estimated": False,
        }
        messages = [HumanMessage(content=prompt)]

        if "Select the single best embedding method" in prompt:
            messages.append(
                AIMessage(
                    content=json.dumps(
                        {
                            "method": "one_hot",
                            "params": {},
                            "rationale": "Simple discrete baseline for warm-start smoke testing.",
                            "confidence": 0.9,
                        }
                    )
                )
            )
            return messages, "", usage

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
            return messages, "", usage

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
            return messages, "", usage

        if "Design the warm-start plan for this campaign." in prompt:
            search_payload = tool_map["warm_start_candidate_search"].invoke(
                {
                    "objective": "dataset-grounded warm-start shortlist",
                    "preferences": [],
                    "must_include": {},
                    "max_results": 4,
                }
            )
            search_results = json.loads(search_payload)["candidates"]
            candidates = [item["candidate"] for item in search_results]
            messages.append(
                ToolMessage(
                    content=search_payload,
                    name="warm_start_candidate_search",
                    tool_call_id="warm-start-search-1",
                )
            )
            if mode == "valid":
                initial_experiments = [
                    {
                        "candidate": candidate,
                        "rationale": f"Warm-start candidate {index + 1} grounded in the feasible search results.",
                        "category": category,
                        "knowledge_card_ids": ["kc_ligand"] if index == 0 else ["kc_temp"] if index == 1 else [],
                    }
                    for index, (candidate, category) in enumerate(
                        zip(candidates[:3], ["exploitation", "balanced", "exploration"])
                    )
                ]
            elif mode == "repair":
                initial_experiments = [
                    {
                        "candidate": candidates[0],
                        "rationale": "A reasonable first candidate.",
                        "category": "exploitation",
                        "knowledge_card_ids": ["kc_ligand"],
                    },
                    {
                        "candidate": {},
                        "rationale": "Intentionally invalid to trigger repair.",
                        "category": "balanced",
                        "knowledge_card_ids": ["kc_temp"],
                    },
                    {
                        "candidate": dict(candidates[0]),
                        "rationale": "Intentional duplicate to trigger fallback fill.",
                        "category": "exploration",
                        "knowledge_card_ids": [],
                    },
                ]
            else:
                initial_experiments = [
                    {
                        "candidate": candidate,
                        "rationale": "Fallback without knowledge cards.",
                        "category": "balanced",
                        "knowledge_card_ids": [],
                    }
                    for candidate in candidates[:3]
                ]
            messages.append(
                AIMessage(
                    content=json.dumps(
                        {
                            "strategy_summary": "Use LLM-proposed warm-start candidates grounded by the local candidate search tool.",
                            "initial_experiments": initial_experiments,
                        }
                    )
                )
            )
            return messages, "", usage

        if "Repair the invalid warm-start slots" in prompt:
            search_payload = tool_map["warm_start_candidate_search"].invoke(
                {
                    "objective": "repair invalid warm-start slots",
                    "preferences": [],
                    "must_include": {},
                    "max_results": 4,
                }
            )
            search_results = json.loads(search_payload)["candidates"]
            replacements = []
            if mode == "repair":
                replacements.append(
                    {
                        "slot": 1,
                        "candidate": search_results[1]["candidate"],
                        "rationale": "Repair the missing slot with a feasible, distinct candidate.",
                        "category": "balanced",
                        "knowledge_card_ids": ["kc_temp"],
                    }
                )
            messages.append(
                ToolMessage(
                    content=search_payload,
                    name="warm_start_candidate_search",
                    tool_call_id="warm-start-search-2",
                )
            )
            messages.append(
                AIMessage(
                    content=json.dumps(
                        {
                            "strategy_summary": "Repair invalid warm-start slots while preserving the initial plan.",
                            "replacements": replacements,
                        }
                    )
                )
            )
            return messages, "", usage

        raise AssertionError(f"Unhandled prompt in fake tool loop:\n{prompt}")

    return _fake_invoke_tool_loop


def _run_to_first_interrupt(monkeypatch, problem_spec: dict, *, cards: list[dict], mode: str) -> dict:
    monkeypatch.setattr("core.graph._create_llm", lambda settings: _DummyLLM())
    monkeypatch.setattr("core.graph.run_knowledge_augmentation", lambda problem_spec, settings: (cards, {"card_count": len(cards)}))
    monkeypatch.setattr("core.graph._invoke_tool_loop", _invoke_tool_loop_factory(mode))

    settings = Settings(
        initial_doe_size=3,
        max_bo_iterations=5,
        human_input_mode="dataset_auto",
    )
    graph = build_chembo_graph(settings)
    initial_state = create_initial_state(problem_spec, settings)
    config = {"configurable": {"thread_id": f"test-{uuid.uuid4().hex[:8]}"}}
    list(graph.stream(initial_state, config=config, stream_mode="updates"))
    return graph.get_state(config).values


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
                "preferences": [],
                "must_include": {},
                "max_results": 3,
            }
        )
    )

    assert payload["status"] == "success"
    assert len(payload["candidates"]) == 3
    assert all(oracle.candidate_exists(item["candidate"]) for item in payload["candidates"])


@pytest.mark.parametrize("problem_name", ["dar", "ocm"])
def test_graph_warm_start_smoke_uses_knowledge_cards_and_removes_legacy_kb(monkeypatch, problem_name: str) -> None:
    state = _run_to_first_interrupt(
        monkeypatch,
        _example_problem(problem_name),
        cards=_sample_knowledge_cards(),
        mode="valid",
    )
    oracle = DatasetOracle.from_problem_spec(state["problem_spec"])

    assert "kb_context" not in state
    assert "kb_priors" not in state
    assert state["knowledge_cards"]
    assert state["retrieval_artifacts"]["card_count"] == len(_sample_knowledge_cards())
    assert len(state["warm_start_queue"]) == 3
    assert state["warm_start_active"] is True
    assert state["proposal_selected"]["selection_source"] == "warm_start_queue"
    assert oracle is not None
    assert oracle.candidate_exists(state["proposal_selected"]["candidate"])
    assert {item["warm_start_category"] for item in state["warm_start_queue"]} <= {
        "exploitation",
        "exploration",
        "balanced",
    }
    assert all("warm_start_rationale" in item for item in state["warm_start_queue"])
    assert all("warm_start_card_refs" in item for item in state["warm_start_queue"])


def test_warm_start_repair_and_fallback_fill_invalid_slots(monkeypatch) -> None:
    state = _run_to_first_interrupt(
        monkeypatch,
        _example_problem("dar"),
        cards=_sample_knowledge_cards(),
        mode="repair",
    )
    oracle = DatasetOracle.from_problem_spec(state["problem_spec"])

    assert oracle is not None
    assert len(state["warm_start_queue"]) == 3
    assert all(oracle.candidate_exists(item["candidate"]) for item in state["warm_start_queue"])
    assert len({json.dumps(item["candidate"], sort_keys=True) for item in state["warm_start_queue"]}) == 3
    assert any(
        "repair_replacements=1" in line and "fallback_fill=1" in line
        for line in state["llm_reasoning_log"]
        if "[warm_start]" in line
    )


def test_warm_start_without_knowledge_cards_still_builds_valid_queue(monkeypatch) -> None:
    state = _run_to_first_interrupt(
        monkeypatch,
        _example_problem("dar"),
        cards=[],
        mode="no_cards",
    )
    oracle = DatasetOracle.from_problem_spec(state["problem_spec"])

    assert state["knowledge_cards"] == []
    assert len(state["warm_start_queue"]) == 3
    assert oracle is not None
    assert all(oracle.candidate_exists(item["candidate"]) for item in state["warm_start_queue"])
    assert all(item["warm_start_card_refs"] == [] for item in state["warm_start_queue"])


def test_bo_runner_low_data_fallback_no_longer_requires_kb_priors() -> None:
    search_space = [
        {"name": "ligand", "type": "categorical", "domain": ["A", "B"]},
        {"name": "base", "type": "categorical", "domain": ["X", "Y"]},
    ]
    payload = json.loads(
        bo_runner.invoke(
            {
                "embedding_method": "one_hot",
                "embedding_params": "{}",
                "surrogate_model": "gp",
                "surrogate_params": "{}",
                "acquisition_function": "log_ei",
                "af_params": "{}",
                "search_space": json.dumps(search_space),
                "observations": "[]",
                "batch_size": 1,
                "top_k": 3,
                "kernel_config": "{}",
                "reaction_type": "demo",
                "optimization_direction": "maximize",
            }
        )
    )

    assert payload["status"] == "warm_start_fallback"
    assert len(payload["shortlist"]) == 3
    assert all(item["constraint_satisfied"] for item in payload["shortlist"])


def test_bo_runner_dataset_candidate_pool_excludes_non_dataset_combinations(tmp_path: Path) -> None:
    dataset_path = tmp_path / "toy_dataset.csv"
    with dataset_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["ligand", "base", "yield"])
        writer.writeheader()
        writer.writerow({"ligand": "A", "base": "X", "yield": "10"})
        writer.writerow({"ligand": "A", "base": "Y", "yield": "20"})
        writer.writerow({"ligand": "B", "base": "X", "yield": "30"})

    search_space = [
        {"name": "ligand", "type": "categorical", "domain": ["A", "B"]},
        {"name": "base", "type": "categorical", "domain": ["X", "Y"]},
    ]
    dataset_spec = {
        "csv_path": str(dataset_path),
        "feature_columns": ["ligand", "base"],
        "target_column": "yield",
    }
    dataset_candidates = [
        {"ligand": "A", "base": "X"},
        {"ligand": "A", "base": "Y"},
        {"ligand": "B", "base": "X"},
    ]
    payload = json.loads(
        bo_runner.invoke(
            {
                "embedding_method": "one_hot",
                "embedding_params": "{}",
                "surrogate_model": "gp",
                "surrogate_params": "{}",
                "acquisition_function": "log_ei",
                "af_params": "{}",
                "search_space": json.dumps(search_space),
                "observations": "[]",
                "batch_size": 1,
                "top_k": 4,
                "kernel_config": "{}",
                "dataset_spec": json.dumps(dataset_spec),
                "reaction_type": "demo",
                "optimization_direction": "maximize",
            }
        )
    )

    shortlist_candidates = [item["candidate"] for item in payload["shortlist"]]
    assert payload["metadata"]["candidate_pool_source"] == "dataset"
    assert {"ligand": "B", "base": "Y"} not in shortlist_candidates
    assert all(candidate in dataset_candidates for candidate in shortlist_candidates)


def test_reasoning_fallback_candidates_work_without_legacy_kb_priors() -> None:
    problem_spec = _example_problem("ocm")
    oracle = DatasetOracle.from_problem_spec(problem_spec)

    candidates = _build_reasoning_fallback_candidates(
        variables=problem_spec["variables"],
        observed_keys=set(),
        hard_constraints=[],
        oracle=oracle,
        limit=4,
        seed=0,
    )

    assert len(candidates) == 4
    assert oracle is not None
    assert all(oracle.candidate_exists(item["candidate"]) for item in candidates)
