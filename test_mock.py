"""
Mock end-to-end validation for the refactored ChemBO workflow.
"""
from __future__ import annotations

import json
import math
from typing import Any

try:
    from langchain_core.messages import AIMessage, ToolMessage
    from langgraph.types import Command
    import core.graph as graph_module
    from config.settings import Settings
    from core.state import create_initial_state
    from memory.memory_manager import MemoryManager
    from pools.component_pools import FingerprintConcatEncoder, OneHotEncoder
    from tools.chembo_tools import bo_runner
    TEST_DEPS_AVAILABLE = True
except ModuleNotFoundError as exc:  # pragma: no cover - local env may lack optional deps
    AIMessage = None
    ToolMessage = None
    Command = None
    graph_module = None
    Settings = None
    create_initial_state = None
    MemoryManager = None
    FingerprintConcatEncoder = None
    OneHotEncoder = None
    bo_runner = None
    TEST_DEPS_AVAILABLE = False
    IMPORT_ERROR = exc


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
    def __init__(
        self,
        tools: list[Any] | None = None,
        warm_start_mode: str = "normal",
        selection_mode: str = "normal",
        reconfig_config_mode: str = "normal",
    ):
        self.tools = {tool.name: tool for tool in tools or []}
        self.warm_start_mode = warm_start_mode
        self.selection_mode = selection_mode
        self.reconfig_config_mode = reconfig_config_mode

    def bind_tools(self, tools: list[Any]):
        return MockChemBOLLM(
            tools,
            warm_start_mode=self.warm_start_mode,
            selection_mode=self.selection_mode,
            reconfig_config_mode=self.reconfig_config_mode,
        )

    def invoke(self, messages):
        prompt = _message_text(messages[-1])
        called_tools = _tool_names(messages)

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
                        "additional_context": "Mock DAR parsing.",
                    }
                )
            )

        if "Select the single best embedding method" in prompt:
            if "embedding_method_advisor" not in called_tools:
                return AIMessage(
                    content="Calling embedding advisor.",
                    tool_calls=[
                        {
                            "id": "embedding-tool",
                            "name": "embedding_method_advisor",
                            "args": {
                                "problem_summary": "DAR optimization",
                                "variable_types": "ligand, base, solvent, temperature, concentration",
                                "num_categoricals": 3,
                                "num_continuous": 2,
                                "has_smiles": False,
                                "data_volume": _experiment_count(messages),
                            },
                        }
                    ],
                )
            return AIMessage(
                content=json.dumps(
                    {
                        "method": "one_hot",
                        "params": {},
                        "rationale": "Safe baseline for low-data mixed optimization.",
                        "confidence": 0.85,
                    }
                )
            )

        if "Generate 3-5 high-value hypotheses" in prompt:
            if "hypothesis_generator" not in called_tools:
                return AIMessage(
                    content="Calling hypothesis generator.",
                    tool_calls=[
                        {
                            "id": "hypothesis-tool",
                            "name": "hypothesis_generator",
                            "args": {
                                "problem_spec": json.dumps({"reaction_type": "DAR", "variables": SEARCH_SPACE}),
                                "current_observations": json.dumps([]),
                                "memory_context": json.dumps({"episodic": [], "semantic": []}),
                            },
                        }
                    ],
                )
            return AIMessage(
                content=json.dumps(
                    {
                        "hypotheses": [
                            {
                                "id": "H1",
                                "text": "XPhos or DavePhos with carbonate-like bases in polar aprotic solvent should dominate.",
                                "mechanism": "Bulky electron-rich ligands and strong bases align with productive DAR pathways.",
                                "testable_prediction": "XPhos or DavePhos with Cs2CO3/CsOPiv in DMAc/NMP should beat weak-ligand controls.",
                                "confidence": "high",
                                "status": "active",
                            },
                            {
                                "id": "H2",
                                "text": "The temperature optimum sits around 115-125 C.",
                                "mechanism": "Higher temperature helps activation but too much heat causes losses.",
                                "testable_prediction": "Runs near 120 C should outperform 90 C and 145 C conditions.",
                                "confidence": "medium",
                                "status": "active",
                            },
                        ],
                        "working_memory_focus": "Use safe baseline components and exploit the likely temperature optimum.",
                    }
                )
            )

        if "Update the active hypotheses for reconfiguration" in prompt:
            if "hypothesis_generator" not in called_tools:
                return AIMessage(
                    content="Calling hypothesis generator.",
                    tool_calls=[
                        {
                            "id": "hypothesis-update-tool",
                            "name": "hypothesis_generator",
                            "args": {
                                "problem_spec": json.dumps({"reaction_type": "DAR", "variables": SEARCH_SPACE}),
                                "current_observations": json.dumps([]),
                                "memory_context": json.dumps({"episodic": [], "semantic": []}),
                            },
                        }
                    ],
                )
            return AIMessage(
                content=json.dumps(
                    {
                        "hypotheses": [
                            {
                                "id": "H1",
                                "text": "XPhos or DavePhos with carbonate-like bases in polar aprotic solvent should dominate.",
                                "mechanism": "Keep the strongest supported catalytic trend active through reconfiguration.",
                                "testable_prediction": "High-performing phosphine systems should remain competitive after the BO stack changes.",
                                "confidence": "high",
                                "status": "supported",
                            },
                            {
                                "id": "H3",
                                "text": "Model flexibility should increase only if the temperature optimum remains unstable.",
                                "mechanism": "Reconfiguration should target unresolved uncertainty rather than discard supported chemistry priors.",
                                "testable_prediction": "If the temperature signal remains ambiguous, a revised surrogate may improve candidate ranking.",
                                "confidence": "medium",
                                "status": "active",
                            },
                        ],
                        "working_memory_focus": "Preserve supported chemistry hypotheses while refining unresolved regions.",
                    }
                )
            )

        if "Configure the surrogate family, kernel, and acquisition function" in prompt:
            if "surrogate_model_selector" not in called_tools:
                return AIMessage(
                    content="Calling surrogate selector.",
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
                    content="Calling AF selector.",
                    tool_calls=[
                        {
                            "id": "af-tool",
                            "name": "af_selector",
                            "args": {
                                "problem_summary": "DAR optimization",
                                "surrogate_model": "gp",
                                "batch_size": 1,
                                "budget_remaining": max(1, 5 - _experiment_count(messages)),
                                "budget_total": 5,
                                "num_objectives": 1,
                                "current_best": None,
                            },
                        }
                    ],
                )
            experiment_count = _experiment_count(messages)
            if self.reconfig_config_mode == "high_noise_on_reconfig" and experiment_count >= 2:
                return AIMessage(
                    content=json.dumps(
                        {
                            "surrogate_model": "gp",
                            "surrogate_params": {"noise_level": 10.0},
                            "kernel_config": {"key": "rbf", "params": {}, "rationale": "Deliberately smooth reconfiguration candidate."},
                            "acquisition_function": "log_ei",
                            "af_params": {},
                            "rationale": "Stress backtesting with a deliberately over-smoothed surrogate.",
                            "confidence": 0.65,
                        }
                    )
                )
            return AIMessage(
                content=json.dumps(
                    {
                        "surrogate_model": "gp",
                        "surrogate_params": {},
                        "kernel_config": {"key": "matern52", "params": {}, "rationale": "General-purpose low-data kernel."},
                        "acquisition_function": "log_ei",
                        "af_params": {},
                        "rationale": "Use the standard low-data GP + LogEI baseline.",
                        "confidence": 0.88,
                    }
                )
            )

        if "Rank and explain the proposed initial experiments" in prompt:
            generated = _extract_generated_candidates(prompt)
            experiments = [
                {
                    "candidate": candidate,
                    "rationale": "Warm-start candidate chosen to balance promising priors and space coverage.",
                    "category": "prior_guided" if idx < 2 else "exploration",
                }
                for idx, candidate in enumerate(generated[:5])
            ]
            if self.warm_start_mode == "hallucinate" and experiments:
                experiments.insert(
                    0,
                    {
                        "candidate": {
                            "ligand": "NotRealLigand",
                            "base": "NotRealBase",
                            "solvent": "NotRealSolvent",
                            "temperature": 111,
                            "concentration": 0.22,
                        },
                        "rationale": "Injected invalid warm-start candidate to test validation.",
                        "category": "prior_guided",
                    },
                )
            return AIMessage(
                content=json.dumps(
                    {
                        "initial_experiments": experiments
                    }
                )
            )

        if "Call bo_runner with:" in prompt:
            return AIMessage(
                content="Calling bo_runner.",
                tool_calls=[{"id": "bo-tool", "name": "bo_runner", "args": _parse_bo_runner_args(prompt)}],
            )

        if '"note": "shortlist generated"' in prompt:
            return AIMessage(content=json.dumps({"note": "shortlist generated", "confidence": 0.82}))

        if "Select ONE candidate from the shortlist" in prompt:
            if self.selection_mode == "invalid_override":
                return AIMessage(
                    content=json.dumps(
                        {
                            "selected_index": 0,
                            "override": True,
                            "override_candidate": {
                                "base_SMILES": "O=C([O-])C.[K+]",
                                "ligand_SMILES": "not-in-dataset",
                                "solvent_SMILES": "CC(N(C)C)=O",
                                "concentration": "0.1",
                                "temperature": "105",
                            },
                            "rationale": {
                                "chemical_reasoning": "Force an out-of-dataset override to test validation.",
                                "hypothesis_alignment": "Intentionally invalid override path.",
                                "information_value": "Exercise dataset guardrails.",
                                "concerns": "Candidate is not drawn from the shortlist.",
                            },
                            "confidence": 0.7,
                        }
                    )
                )
            if self.selection_mode == "null_index":
                return AIMessage(
                    content=json.dumps(
                        {
                            "selected_index": None,
                            "override": False,
                            "override_candidate": None,
                            "rationale": {
                                "chemical_reasoning": "Model omitted the shortlist index.",
                                "hypothesis_alignment": "Test null index fallback.",
                                "information_value": "Ensure selection remains stable.",
                                "concerns": "selected_index came back null.",
                            },
                            "confidence": None,
                        }
                    )
                )
            return AIMessage(
                content=json.dumps(
                    {
                        "selected_index": 0,
                        "override": False,
                        "override_candidate": None,
                        "rationale": {
                            "chemical_reasoning": "Take the top shortlist candidate.",
                            "hypothesis_alignment": "Prioritizes H1 and H2 together.",
                            "information_value": "Improves the model in a promising region.",
                            "concerns": "",
                        },
                        "confidence": 0.8,
                    }
                )
            )

        if "Interpret the latest experimental result and update memory" in prompt:
            if "result_interpreter" not in called_tools:
                latest_result = _latest_result(messages)
                return AIMessage(
                    content="Calling result interpreter.",
                    tool_calls=[
                        {
                            "id": "result-tool",
                            "name": "result_interpreter",
                            "args": {
                                "latest_observations": json.dumps([{"result": latest_result, "candidate": {}}]),
                                "all_observations": json.dumps([]),
                                "bo_config": json.dumps({}),
                                "hypotheses": json.dumps([]),
                            },
                        }
                    ],
                )
            latest_result = _latest_result(messages)
            supported = ["H1"] if latest_result >= 60 else []
            refuted = ["H1"] if latest_result < 25 else []
            return AIMessage(
                content=json.dumps(
                    {
                        "interpretation": "The result has been logged and compared against the active hypotheses.",
                        "supported_hypotheses": supported,
                        "refuted_hypotheses": refuted,
                        "archived_hypotheses": [],
                        "episodic_memory": {
                            "reflection": "Stored the latest result and extracted a short lesson.",
                            "lesson_learned": "Promising ligand/base/solvent combinations should be revisited.",
                            "non_numerical_observations": "mock-no-visible-anomaly",
                        },
                        "semantic_rule": {
                            "rule_type": "chemical_effect",
                            "content": {
                                "variable": "ligand",
                                "value": "XPhos",
                                "effect_direction": "positive",
                                "effect_magnitude_pct": 12.0,
                            },
                            "natural_language": "XPhos tends to improve yield in this mock DAR landscape.",
                            "confidence": 0.7,
                            "status": "tentative",
                            "evidence_iterations": [_experiment_count(messages)],
                            "source": "observation",
                        }
                        if latest_result >= 60
                        else None,
                        "working_memory": {
                            "current_focus": "Decide whether to continue or reconfigure.",
                            "pending_decisions": ["continue_or_reconfigure"],
                        },
                    }
                )
            )

        if "Reflect on campaign progress and decide the next action" in prompt:
            experiment_count = _experiment_count(messages)
            if self.reconfig_config_mode != "normal":
                if experiment_count == 5 and "reconfig_gate" not in _reasoning_log(messages):
                    decision = "reconfigure"
                elif experiment_count >= 6:
                    decision = "stop"
                else:
                    decision = "continue"
            elif experiment_count == 2 and "reconfig_gate" not in _reasoning_log(messages):
                decision = "reconfigure"
            elif experiment_count >= 4:
                decision = "stop"
            else:
                decision = "continue"
            return AIMessage(
                content=json.dumps(
                    {
                        "decision": decision,
                        "reasoning": "Mock reflection based on iteration count and progress.",
                        "confidence": 0.85,
                    }
                )
            )

        if "Reply with strict JSON only" in prompt:
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
            try:
                return float(text.split("Experiment result:", 1)[1].split(".", 1)[0].strip())
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
        if key in {"batch_size", "top_k"}:
            args[key] = int(raw_value)
        else:
            args[key] = raw_value
    return args


def _extract_generated_candidates(prompt: str) -> list[dict[str, Any]]:
    marker = "GENERATED_CANDIDATES:\n"
    if marker not in prompt:
        return []
    tail = prompt.split(marker, 1)[1]
    block = tail.split("\n\nReturn strict JSON", 1)[0].strip()
    try:
        return json.loads(block)
    except json.JSONDecodeError:
        return []


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
    if not TEST_DEPS_AVAILABLE:
        print(f"Skipping component smoke tests: {IMPORT_ERROR}")
        return
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

    continuous_payload = json.loads(
        bo_runner.invoke(
            {
                "embedding_method": "one_hot",
                "embedding_params": "{}",
                "surrogate_model": "gp",
                "surrogate_params": "{}",
                "acquisition_function": "log_ei",
                "af_params": '{"candidate_pool_size": 128, "initial_doe_size": 1}',
                "search_space": json.dumps([{"name": "x", "type": "continuous", "domain": [0, 1]}]),
                "observations": json.dumps(
                    [
                        {"candidate": {"x": 0.1}, "result": 0.2},
                        {"candidate": {"x": 0.9}, "result": 0.1},
                        {"candidate": {"x": 0.4}, "result": 0.6},
                    ]
                ),
                "batch_size": 1,
                "kernel_config": '{"key": "matern52", "params": {}}',
            }
        )
    )
    assert continuous_payload["shortlist"]
    assert 0 <= continuous_payload["candidates"][0]["x"] <= 1

    mixed_payload = json.loads(
        bo_runner.invoke(
            {
                "embedding_method": "one_hot",
                "embedding_params": "{}",
                "surrogate_model": "gp",
                "surrogate_params": "{}",
                "acquisition_function": "log_ei",
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
                        {
                            "candidate": {
                                "ligand": "DavePhos",
                                "base": "CsOPiv",
                                "solvent": "NMP",
                                "temperature": 115,
                                "concentration": 0.28,
                            },
                            "result": 76.0,
                        },
                    ]
                ),
                "batch_size": 1,
                "kernel_config": '{"key": "mixed_sum_product", "params": {}}',
                "reaction_type": "DAR",
            }
        )
    )
    assert mixed_payload["shortlist"]
    assert mixed_payload["resolved_components"]["acquisition_function"] == "log_ei"

    memory = MemoryManager()
    for idx, result in enumerate([75.0, 72.0, 20.0, 68.0, 70.0], start=1):
        ligand = "XPhos" if idx != 3 else "PPh3"
        memory.add_episode(idx, {}, {"ligand": ligand}, result, "reflection", lesson_learned="XPhos looks strong")
    memory.consolidate(
        observations=[
            {"iteration": 1, "candidate": {"ligand": "XPhos"}, "result": 75.0},
            {"iteration": 2, "candidate": {"ligand": "XPhos"}, "result": 72.0},
            {"iteration": 3, "candidate": {"ligand": "PPh3"}, "result": 20.0},
            {"iteration": 4, "candidate": {"ligand": "XPhos"}, "result": 68.0},
            {"iteration": 5, "candidate": {"ligand": "XPhos"}, "result": 70.0},
        ]
    )
    assert memory.get_all_rules(), "expected semantic rules after consolidation"


def run_mock_campaign():
    if not TEST_DEPS_AVAILABLE:
        print(f"Skipping mock campaign: {IMPORT_ERROR}")
        return
    original_factory = graph_module._create_llm
    graph_module._create_llm = lambda settings: MockChemBOLLM()
    try:
        settings = Settings(llm_model="mock", batch_size=1, max_bo_iterations=5, initial_doe_size=1)
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
            saw_reconfigure = saw_reconfigure or state.get("next_action") == "reconfigure"
            if iterations > 8:
                raise RuntimeError("mock campaign exceeded expected iteration budget")

        assert state["embedding_locked"] is True
        assert state["effective_config"]["acquisition_function"] == "log_ei"
        assert state["best_result"] > 0
        assert state["memory"]["episodic"], "episodic memory should be populated"
        assert state["proposal_selected"], "candidate selection metadata should be populated"
        assert saw_reconfigure or len(state["config_history"]) > 1, "expected at least one reconfiguration pass"
        print("Mock campaign passed.")
    finally:
        graph_module._create_llm = original_factory


if __name__ == "__main__":
    _run_component_smoke_tests()
    run_mock_campaign()
