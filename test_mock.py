"""
Mock end-to-end validation for the ChemBO Phase 1 workflow.

This script avoids external LLM APIs by patching `core.graph._create_llm`
with a deterministic mock that drives the graph through:
analyze -> hypotheses -> configure -> BO -> interpret -> reflect.
"""
from __future__ import annotations

import json
import math
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import Command

import core.graph as graph_module
from config.settings import Settings
from core.state import create_initial_state
from memory.memory_manager import MemoryManager
from pools.component_pools import FingerprintConcatEncoder, OneHotEncoder
from tools.chembo_tools import bo_runner


DAR_PROBLEM = (
    "Optimize the yield of a Direct Arylation Reaction (DAR) between "
    "4-bromotoluene and 2-methylthiophene. Variables to optimize: "
    "ligand (categorical: PPh3, P(Cy)3, XPhos, SPhos, DavePhos), "
    "base (categorical: K2CO3, Cs2CO3, KOAc, CsOPiv), "
    "solvent (categorical: DMAc, DMF, NMP, toluene), "
    "temperature (continuous: 80-150C), "
    "concentration (continuous: 0.1-0.5 M). "
    "Target: maximize GC yield (%). Budget: 5 experiments."
)


SEARCH_SPACE = [
    {"name": "ligand", "type": "categorical", "domain": ["PPh3", "P(Cy)3", "XPhos", "SPhos", "DavePhos"]},
    {"name": "base", "type": "categorical", "domain": ["K2CO3", "Cs2CO3", "KOAc", "CsOPiv"]},
    {"name": "solvent", "type": "categorical", "domain": ["DMAc", "DMF", "NMP", "toluene"]},
    {"name": "temperature", "type": "continuous", "domain": [80, 150]},
    {"name": "concentration", "type": "continuous", "domain": [0.1, 0.5]},
]


class MockChemBOLLM:
    def __init__(self, tools: list[Any] | None = None):
        self.tools = {tool.name: tool for tool in tools or []}

    def bind_tools(self, tools: list[Any]):
        return MockChemBOLLM(tools)

    def invoke(self, messages):
        prompt = _message_text(messages[-1])
        if "Analyze this chemical optimization problem" in prompt:
            return AIMessage(
                content=json.dumps(
                    {
                        "reaction_type": "DAR",
                        "target_metric": "yield",
                        "optimization_direction": "maximize",
                        "variables": SEARCH_SPACE,
                        "constraints": ["Keep temperature within practical DAR ranges."],
                        "budget": 5,
                        "additional_context": "Mock analysis for DAR validation.",
                    }
                )
            )

        if "Call the hypothesis_generator tool first" in prompt:
            return AIMessage(
                content="Calling hypothesis generator.",
                tool_calls=[
                    {
                        "id": "hypothesis-tool",
                        "name": "hypothesis_generator",
                        "args": {
                            "problem_spec": json.dumps(
                                {
                                    "reaction_type": "DAR",
                                    "variables": SEARCH_SPACE,
                                    "budget": 5,
                                }
                            ),
                            "current_observations": "[]",
                            "memory_context": json.dumps({"semantic": [], "episodic": []}),
                        },
                    }
                ],
            )

        if "Use the hypothesis_generator tool output above" in prompt:
            return AIMessage(
                content=json.dumps(
                    {
                        "hypotheses": [
                            {
                                "hypothesis": "XPhos or DavePhos with carbonate/pivalate bases in polar aprotic solvent will perform well.",
                                "mechanism": "DAR literature priors favor electron-rich bulky phosphines and carbonate/carboxylate bases.",
                                "test": "Bias early exploration toward XPhos/DavePhos with Cs2CO3 or CsOPiv in DMAc/NMP.",
                                "confidence": "high",
                            },
                            {
                                "hypothesis": "Temperatures near 120C and concentration near 0.3 M maximize yield.",
                                "mechanism": "The synthetic objective is concave around those values.",
                                "test": "Sample around 110-130C and 0.25-0.35 M.",
                                "confidence": "medium",
                            },
                        ],
                        "working_memory_focus": "Prefer literature-prior ligands and bases while probing the temperature optimum.",
                    }
                )
            )

        if "Configure the BO pipeline" in prompt or "Use the tool results above and synthesize the final strict JSON BO configuration only." in prompt:
            called_tools = _tool_names(messages)
            if "embedding_method_advisor" not in called_tools:
                return AIMessage(
                    content="Selecting embedding.",
                    tool_calls=[
                        {
                            "id": "embedding-tool",
                            "name": "embedding_method_advisor",
                            "args": {
                                "problem_summary": "DAR optimization",
                                "variable_types": "ligand,base,solvent,temperature,concentration",
                                "num_categoricals": 3,
                                "num_continuous": 2,
                                "has_smiles": False,
                                "data_volume": _experiment_count(messages),
                            },
                        }
                    ],
                )
            if "surrogate_model_selector" not in called_tools:
                return AIMessage(
                    content="Selecting surrogate.",
                    tool_calls=[
                        {
                            "id": "surrogate-tool",
                            "name": "surrogate_model_selector",
                            "args": {
                                "problem_summary": "DAR optimization",
                                "embedding_method": "one_hot",
                                "embedding_dim": 15,
                                "num_variables": 5,
                                "num_categoricals": 3,
                                "expected_data_volume": 5,
                                "noise_level": "medium",
                            },
                        }
                    ],
                )
            if "af_selector" not in called_tools:
                return AIMessage(
                    content="Selecting acquisition function.",
                    tool_calls=[
                        {
                            "id": "af-tool",
                            "name": "af_selector",
                            "args": {
                                "problem_summary": "DAR optimization",
                                "surrogate_model": "gp_matern52",
                                "batch_size": 1,
                                "budget_remaining": max(1, 5 - _experiment_count(messages)),
                                "budget_total": 5,
                                "num_objectives": 1,
                                "current_best": None,
                            },
                        }
                    ],
                )
            return AIMessage(
                content=json.dumps(
                    {
                        "embedding_method": "one_hot",
                        "embedding_params": {},
                        "embedding_rationale": "Stable mixed-space baseline for low-data DAR optimization.",
                        "surrogate_model": "gp_matern52",
                        "surrogate_params": {},
                        "surrogate_rationale": "Reliable surrogate for small data and smooth response surfaces.",
                        "acquisition_function": "ei",
                        "af_params": {},
                        "af_rationale": "Balanced exploration/exploitation for a single-objective campaign.",
                    }
                )
            )

        if "Call bo_runner with:" in prompt:
            return AIMessage(
                content="Calling bo_runner.",
                tool_calls=[
                    {
                        "id": "bo-runner-tool",
                        "name": "bo_runner",
                        "args": _parse_bo_runner_args(prompt),
                    }
                ],
            )

        if "Use the bo_runner output above and explain the proposal" in prompt:
            return AIMessage(
                content=(
                    "The proposal targets chemistry-consistent ligand/base/solvent priors while refining "
                    "temperature and concentration near the expected optimum."
                )
            )

        if "Call result_interpreter first" in prompt:
            latest_result = _latest_result(messages)
            return AIMessage(
                content="Calling result_interpreter.",
                tool_calls=[
                    {
                        "id": "result-interpreter-tool",
                        "name": "result_interpreter",
                        "args": {
                            "latest_observations": json.dumps(
                                [
                                    {
                                        "result": latest_result,
                                        "candidate": {},
                                    }
                                ]
                            ),
                            "all_observations": json.dumps([]),
                            "bo_config": json.dumps({}),
                            "hypotheses": json.dumps([]),
                        },
                    }
                ],
            )

        if "Use the result_interpreter output above" in prompt:
            latest_result = _latest_result(messages)
            semantic_rule = None
            if latest_result >= 70:
                semantic_rule = {
                    "rule": "XPhos or DavePhos with Cs2CO3/CsOPiv in DMAc/NMP tends to improve DAR yield.",
                    "confidence": 0.7,
                }
            return AIMessage(
                content=json.dumps(
                    {
                        "interpretation": "The latest result aligns with the expected DAR prior structure.",
                        "supported_hypotheses": [
                            "Bulky electron-rich ligands with carbonate-like bases in polar aprotic solvent perform well."
                        ],
                        "refuted_hypotheses": [],
                        "episodic_memory": {
                            "reflection": "Observed a coherent DAR response consistent with prior expectations.",
                            "lesson_learned": "Polar aprotic conditions with strong ligands remain strong candidates.",
                            "non_numerical_observations": "mock-no-visible-anomaly",
                        },
                        "semantic_rule": semantic_rule,
                        "working_memory": {
                            "current_focus": "Decide whether the campaign is still improving or should reconfigure.",
                            "pending_decisions": ["continue_or_reconfigure"],
                        },
                    }
                )
            )

        if "Reflect on the campaign progress" in prompt:
            experiment_count = _experiment_count(messages)
            if experiment_count == 2 and "decision=reconfigure" not in _reasoning_log(messages):
                decision = "reconfigure"
            elif experiment_count >= 4:
                decision = "stop"
            else:
                decision = "continue"
            return AIMessage(
                content=json.dumps(
                    {
                        "decision": decision,
                        "reasoning": "Mock reflection based on experiment count progression.",
                        "confidence": 0.8,
                    }
                )
            )

        if "Reply with JSON only" in prompt:
            return AIMessage(content="{}")

        return AIMessage(content="{}")


def _message_text(message) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(item) for item in content)
    return str(content)


def _tool_names(messages) -> set[str]:
    names = set()
    for message in messages:
        if isinstance(message, ToolMessage):
            if getattr(message, "name", None):
                names.add(message.name)
            else:
                text = _message_text(message)
                for tool_name in [
                    "embedding_method_advisor",
                    "surrogate_model_selector",
                    "af_selector",
                    "hypothesis_generator",
                    "result_interpreter",
                    "bo_runner",
                ]:
                    if tool_name in text:
                        names.add(tool_name)
    return names


def _experiment_count(messages) -> int:
    return sum(1 for message in messages if "Experiment result:" in _message_text(message))


def _latest_result(messages) -> float:
    for message in reversed(messages):
        text = _message_text(message)
        if "Experiment result:" in text:
            prefix = text.split("Experiment result:", 1)[1].split(".", 1)[0].strip()
            try:
                return float(prefix)
            except ValueError:
                return 0.0
    return 0.0


def _reasoning_log(messages) -> str:
    return "\n".join(_message_text(message) for message in messages)


def _parse_bo_runner_args(prompt: str) -> dict[str, Any]:
    args = {}
    for line in prompt.splitlines():
        line = line.strip()
        if not line.startswith("- "):
            continue
        key, raw_value = line[2:].split(": ", 1)
        if key == "batch_size":
            args[key] = int(raw_value)
        else:
            args[key] = raw_value
    return args


def mock_dar_objective(candidate: dict) -> float:
    yield_val = 20.0
    ligand_bonus = {"XPhos": 25, "DavePhos": 20, "P(Cy)3": 15, "SPhos": 10, "PPh3": 0}
    yield_val += ligand_bonus.get(candidate.get("ligand", ""), 0)
    base_bonus = {"Cs2CO3": 15, "CsOPiv": 12, "K2CO3": 5, "KOAc": 0}
    yield_val += base_bonus.get(candidate.get("base", ""), 0)
    solvent_bonus = {"DMAc": 15, "NMP": 10, "DMF": 5, "toluene": -5}
    yield_val += solvent_bonus.get(candidate.get("solvent", ""), 0)
    temp = float(candidate.get("temperature", 120))
    yield_val += -0.01 * (temp - 120) ** 2 + 10
    conc = float(candidate.get("concentration", 0.3))
    yield_val += -100 * (conc - 0.3) ** 2 + 5
    return max(0.0, min(100.0, yield_val))


def _run_component_smoke_tests():
    one_hot = OneHotEncoder(SEARCH_SPACE)
    candidate = {
        "ligand": "XPhos",
        "base": "Cs2CO3",
        "solvent": "DMAc",
        "temperature": 120,
        "concentration": 0.3,
    }
    encoded = one_hot.encode(candidate)
    decoded = one_hot.decode(encoded)
    assert decoded["ligand"] == "XPhos"
    assert decoded["base"] == "Cs2CO3"
    assert math.isclose(float(decoded["temperature"]), 120.0, rel_tol=0, abs_tol=1e-6)

    fp_encoder = FingerprintConcatEncoder(SEARCH_SPACE)
    fp_roundtrip = fp_encoder.decode(fp_encoder.encode(candidate))
    assert fp_roundtrip["ligand"] in {"XPhos", "PPh3", "P(Cy)3", "SPhos", "DavePhos"}

    continuous_result = bo_runner.invoke(
        {
            "embedding_method": "one_hot",
            "embedding_params": "{}",
            "surrogate_model": "gp_matern52",
            "surrogate_params": "{}",
            "acquisition_function": "ei",
            "af_params": '{"candidate_pool_size": 128, "initial_doe_size": 1}',
            "search_space": json.dumps([{"name": "x", "type": "continuous", "domain": [0, 1]}]),
            "observations": json.dumps(
                [
                    {"candidate": {"x": 0.1}, "result": 0.2},
                    {"candidate": {"x": 0.9}, "result": 0.1},
                ]
            ),
            "batch_size": 1,
        }
    )
    continuous_payload = json.loads(continuous_result)
    assert continuous_payload["candidates"]
    assert 0 <= continuous_payload["candidates"][0]["x"] <= 1

    mixed_result = bo_runner.invoke(
        {
            "embedding_method": "one_hot",
            "embedding_params": "{}",
            "surrogate_model": "gp_matern52",
            "surrogate_params": "{}",
            "acquisition_function": "ei",
            "af_params": '{"candidate_pool_size": 128, "initial_doe_size": 1}',
            "search_space": json.dumps(SEARCH_SPACE),
            "observations": json.dumps(
                [
                    {
                        "candidate": {
                            "ligand": "PPh3",
                            "base": "KOAc",
                            "solvent": "toluene",
                            "temperature": 90,
                            "concentration": 0.2,
                        },
                        "result": 15.0,
                    },
                    {
                        "candidate": {
                            "ligand": "XPhos",
                            "base": "Cs2CO3",
                            "solvent": "DMAc",
                            "temperature": 120,
                            "concentration": 0.3,
                        },
                        "result": 80.0,
                    },
                ]
            ),
            "batch_size": 1,
        }
    )
    mixed_payload = json.loads(mixed_result)
    assert mixed_payload["candidates"]
    assert mixed_payload["candidates"][0]["ligand"] in {"PPh3", "P(Cy)3", "XPhos", "SPhos", "DavePhos"}

    memory = MemoryManager()
    memory.add_episode(1, {}, {"ligand": "XPhos"}, 75.0, "good result", lesson_learned="XPhos looks strong")
    memory.add_episode(2, {}, {"ligand": "DavePhos"}, 72.0, "good result", lesson_learned="XPhos looks strong")
    memory.add_episode(3, {}, {"ligand": "PPh3"}, 20.0, "poor result", lesson_learned="XPhos looks strong")
    memory.consolidate()
    assert memory.get_all_rules(), "expected semantic rules after consolidation"


def run_mock_campaign():
    original_factory = graph_module._create_llm
    graph_module._create_llm = lambda settings: MockChemBOLLM()
    try:
        settings = Settings(llm_model="mock", batch_size=1, max_bo_iterations=5)
        graph = graph_module.build_chembo_graph(settings)
        initial_state = create_initial_state(DAR_PROBLEM, settings)
        config = {"configurable": {"thread_id": "chembo-mock-test"}}

        graph.invoke(initial_state, config=config)
        state = graph.get_state(config).values
        iterations = 0
        saw_reconfigure = False

        while state["phase"] != "completed":
            proposal = state["current_proposal"]
            assert proposal["candidates"], "expected current proposal before interrupt resume"
            candidate = proposal["candidates"][0]
            result = mock_dar_objective(candidate)
            graph.invoke(Command(resume={"result": result, "notes": "mock-run"}), config=config)
            state = graph.get_state(config).values
            iterations += 1
            if state.get("next_action") == "reconfigure":
                saw_reconfigure = True
            if iterations > 8:
                raise RuntimeError("mock campaign exceeded expected iteration budget")

        assert state["best_result"] > 0
        assert state["memory"]["episodic"], "episodic memory should be populated"
        assert saw_reconfigure or len(state["config_history"]) > 1, "expected at least one reconfiguration pass"
        print("Mock campaign passed.")
    finally:
        graph_module._create_llm = original_factory


if __name__ == "__main__":
    _run_component_smoke_tests()
    run_mock_campaign()
