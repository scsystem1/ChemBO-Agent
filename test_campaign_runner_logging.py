"""
Regression coverage for campaign runner logging around non-dict stream events.
"""
from __future__ import annotations

import copy
import csv
import json
import sys
import tempfile
import types
from pathlib import Path


langgraph = types.ModuleType("langgraph")
langgraph_types = types.ModuleType("langgraph.types")


class Command:
    def __init__(self, resume=None):
        self.resume = resume


langgraph_types.Command = Command
langgraph.types = langgraph_types
sys.modules.setdefault("langgraph", langgraph)
sys.modules.setdefault("langgraph.types", langgraph_types)

from config.settings import Settings
from core.campaign_runner import CampaignRunLogger, _default_run_id, run_campaign

ROOT = Path(__file__).resolve().parent
DAR_DATASET_PATH = ROOT / "data" / "DAR.csv"


class _DummyState:
    def __init__(self, values):
        self.values = values


class AIMessage:
    def __init__(self, content):
        self.content = content


class _DummyGraph:
    def __init__(self):
        self.state = _base_state()
        self.state["phase"] = "completed"
        self.state["next_action"] = "stop"
        self.state["termination_reason"] = "completed"
        self.state["final_summary"] = {"total_experiments": 0, "best_result": None, "stop_reason": "completed"}
        self.state["problem_spec"] = {"budget": 1}

    def stream(self, payload, config=None, stream_mode=None):
        yield {"__interrupt__": ({"value": {"message": "run experiment"}},)}
        yield {"campaign_summary": {"final_summary": self.state["final_summary"], "messages": []}}

    def get_state(self, config):
        return _DummyState(self.state)


def _base_state() -> dict:
    return {
        "phase": "init",
        "iteration": 0,
        "next_action": "",
        "optimization_direction": "maximize",
        "observations": [],
        "best_result": float("-inf"),
        "best_candidate": {},
        "embedding_config": {},
        "bo_config": {},
        "effective_config": {},
        "current_proposal": {},
        "proposal_selected": {},
        "proposal_shortlist": [],
        "warm_start_queue": [],
        "warm_start_active": False,
        "convergence_state": {},
        "last_tool_payload": {},
        "last_llm_usage": {},
        "llm_token_usage": {},
        "performance_log": [],
        "config_history": [],
        "reconfig_history": [],
        "llm_reasoning_log": [],
        "termination_reason": "",
        "final_summary": {},
        "memory": {"working": {}, "episodic": [], "semantic": []},
        "hypotheses": [],
        "problem_spec": {"budget": 5},
    }


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), [dict(row) for row in reader]


def _logger_output(tmp: str, run_id: str) -> tuple[Path, list[dict], str]:
    run_dir = Path(tmp) / run_id
    return run_dir, _read_jsonl(run_dir / "run_log.jsonl"), (run_dir / "timeline.md").read_text(encoding="utf-8")


def _allowed_state_delta_keys() -> set[str]:
    return {
        "phase",
        "iteration",
        "next_action",
        "observations_count",
        "best_result",
        "best_candidate",
        "embedding_method",
        "bo_signature",
        "proposal_shortlist_count",
        "selected_candidate",
        "selection_source",
        "warm_start_queue_count",
        "hypothesis_status_counts",
        "working_memory_focus",
        "convergence_state",
        "termination_reason",
    }


def test_run_campaign_logs_tuple_stream_events_without_crashing():
    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings(llm_model="mock", output_dir=tmp)
        graph = _DummyGraph()

        final_state = run_campaign(
            graph,
            {"problem_spec": {"budget": 1}},
            settings,
            thread_id="tuple-event-smoke",
            printer=None,
        )

        run_dir, records, timeline = _logger_output(tmp, "tuple-event-smoke")
        run_log = (run_dir / "run_log.jsonl").read_text(encoding="utf-8")
        interrupt_record = next(record for record in records if record.get("node") == "__interrupt__")

        assert final_state["phase"] == "completed"
        assert (run_dir / "experiment_records.csv").exists()
        assert (run_dir / "llm_trace.json").exists()
        assert "interrupt payload=" not in timeline
        assert '"raw_update"' not in run_log
        assert '"state_snapshot"' not in run_log
        assert interrupt_record["summary"] == "Waiting for experimental result."
        assert interrupt_record["reasoning"] == ["run experiment"]
        assert interrupt_record["state_delta"]["phase"] == "completed"
        assert {"summary", "reasoning", "outcome", "state_delta", "llm_usage"}.issubset(interrupt_record)
        assert "progress_lines" not in interrupt_record
        assert "update" not in interrupt_record


def test_logger_summarizes_hypothesis_updates_without_debug_dump():
    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings(llm_model="mock", output_dir=tmp)
        logger = CampaignRunLogger(settings, "hypothesis-summary")
        logger.log_session_start({"problem_spec": {"budget": 5}}, {"configurable": {"thread_id": "hypothesis-summary"}})

        state = _base_state()
        state["phase"] = "hypothesizing"
        state["hypotheses"] = [
            {
                "id": "H1",
                "text": "Electron-rich ligand should stabilize oxidative addition.",
                "mechanism": "Ligand electronics control Pd insertion.",
                "testable_prediction": "XPhos-like ligands should outperform PPh3.",
                "confidence": "high",
                "status": "active",
            },
            {
                "id": "H2",
                "text": "Higher base strength should help turnover.",
                "mechanism": "Faster deprotonation closes the cycle.",
                "testable_prediction": "Cs2CO3 should beat K2CO3.",
                "confidence": "medium",
                "status": "supported",
            },
        ]
        state["memory"]["working"]["current_focus"] = "Validate ligand/base interaction before reconfiguring."
        update = {
            "messages": [
                AIMessage(
                    content=json.dumps(
                        {
                            "hypotheses": state["hypotheses"],
                            "working_memory_focus": state["memory"]["working"]["current_focus"],
                        }
                    )
                )
            ]
        }
        logger.log_graph_update(
            "generate_hypotheses",
            update,
            state,
            ["[generate_hypotheses] iter 0/5 hypotheses=2 focus=Validate ligand/base interaction before reconfiguring."],
        )
        logger.close()

        _, records, timeline = _logger_output(tmp, "hypothesis-summary")
        record = records[-1]

        assert record["summary"] == "Generated hypotheses (2 total)."
        assert any("status_counts=active=1, supported=1" in item for item in record["outcome"])
        assert any("H1 new" in item for item in record["outcome"])
        assert any("H2 new" in item for item in record["outcome"])
        assert record["state_delta"]["hypothesis_status_counts"] == {"active": 1, "supported": 1}
        assert record["state_delta"]["working_memory_focus"] == "Validate ligand/base interaction before reconfiguring."
        assert set(record["state_delta"]).issubset(_allowed_state_delta_keys())
        assert "llm_reasoning_log_tail" not in record["state_delta"]
        assert "Update Payload" not in timeline


def test_logger_summarizes_candidate_selection_rationale():
    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings(llm_model="mock", output_dir=tmp)
        logger = CampaignRunLogger(settings, "selection-summary")
        logger.log_session_start({"problem_spec": {"budget": 5}}, {"configurable": {"thread_id": "selection-summary"}})

        state = _base_state()
        state["phase"] = "selecting_candidate"
        state["iteration"] = 2
        state["proposal_selected"] = {
            "selected_index": 1,
            "override": False,
            "candidate": {"ligand": "XPhos", "base": "Cs2CO3", "temperature": 110},
            "rationale": {
                "chemical_reasoning": "Follow the strongest ligand hypothesis before changing solvent.",
                "hypothesis_alignment": "Directly tests H1 under the best base seen so far.",
                "information_value": "Separates ligand effects from solvent noise.",
                "concerns": "Temperature may still confound the attribution.",
            },
            "confidence": 0.82,
            "selection_source": "llm_shortlist",
        }
        state["current_proposal"] = {
            "candidates": [state["proposal_selected"]["candidate"]],
            "selected_index": 1,
            "shortlist": [{"candidate": state["proposal_selected"]["candidate"]}],
        }
        state["last_llm_usage"] = {
            "node": "select_candidate",
            "calls": 1,
            "input_tokens": 120,
            "output_tokens": 42,
            "total_tokens": 162,
            "estimated": False,
        }
        update = {
            "messages": [
                AIMessage(
                    content=json.dumps(
                        {
                            "selected_index": 1,
                            "override": False,
                            "rationale": state["proposal_selected"]["rationale"],
                            "confidence": 0.82,
                        }
                    )
                )
            ]
        }
        logger.log_graph_update(
            "select_candidate",
            update,
            state,
            ["[select_candidate] iter 2/5 selected source=llm_shortlist override=False candidate={ligand=XPhos, base=Cs2CO3, temperature=110}"],
        )
        logger.close()

        _, records, _ = _logger_output(tmp, "selection-summary")
        record = records[-1]

        assert record["summary"] == "Selected next experiment candidate from `llm_shortlist`."
        assert any("Follow the strongest ligand hypothesis" in item for item in record["reasoning"])
        assert any("candidate={ligand=XPhos, base=Cs2CO3, temperature=110}" in item for item in record["outcome"])
        assert any("confidence=0.82" in item for item in record["outcome"])
        assert record["llm_usage"]["total_tokens"] == 162
        assert set(record["state_delta"]).issubset(_allowed_state_delta_keys())


def test_logger_summarizes_interpret_and_reflect_events():
    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings(llm_model="mock", output_dir=tmp)
        logger = CampaignRunLogger(settings, "reflect-summary")
        logger.log_session_start({"problem_spec": {"budget": 5}}, {"configurable": {"thread_id": "reflect-summary"}})

        interpreted_state = _base_state()
        interpreted_state["phase"] = "interpreting"
        interpreted_state["iteration"] = 3
        interpreted_state["best_result"] = 63.4
        interpreted_state["observations"] = [
            {
                "iteration": 3,
                "candidate": {"ligand": "XPhos", "solvent": "DMAc"},
                "result": 63.4,
                "metadata": {"notes": "sharp improvement"},
            }
        ]
        interpreted_state["hypotheses"] = [
            {"id": "H1", "text": "XPhos is the leading ligand.", "status": "supported", "confidence": "high"},
            {"id": "H2", "text": "Solvent polarity dominates.", "status": "refuted", "confidence": "medium"},
        ]
        interpreted_state["memory"]["working"]["current_focus"] = "Probe solvent dependence around the XPhos optimum."
        interpret_update = {
            "messages": [
                AIMessage(
                    content=json.dumps(
                        {
                            "interpretation": "XPhos sharply improved yield while the solvent story weakened.",
                            "supported_hypotheses": ["H1"],
                            "refuted_hypotheses": ["H2"],
                            "archived_hypotheses": [],
                            "episodic_memory": {"reflection": "The ligand effect is now dominant."},
                            "working_memory": {"current_focus": interpreted_state["memory"]["working"]["current_focus"]},
                        }
                    )
                )
            ]
        }
        logger.log_graph_update(
            "interpret_results",
            interpret_update,
            interpreted_state,
            ["[interpret_results] iter 3/5 interpreted best=63.4 note=XPhos sharply improved yield while the solvent story weakened."],
        )

        reflected_state = copy.deepcopy(interpreted_state)
        reflected_state["phase"] = "reflecting"
        reflected_state["next_action"] = "reconfigure"
        reflected_state["convergence_state"] = {
            "is_stagnant": True,
            "stagnation_length": 3,
            "recent_improvement_rate": 0.2,
            "budget_used_ratio": 0.6,
            "last_improvement_iteration": 3,
            "max_af_value": 0.01,
        }
        reflected_state["last_llm_usage"] = {
            "node": "reflect_and_decide",
            "calls": 1,
            "input_tokens": 90,
            "output_tokens": 18,
            "total_tokens": 108,
            "estimated": True,
        }
        reflect_update = {
            "messages": [
                AIMessage(
                    content=json.dumps(
                        {
                            "decision": "reconfigure",
                            "reasoning": "Progress plateaued and the acquisition value is nearly exhausted.",
                            "confidence": 0.91,
                        }
                    )
                )
            ]
        }
        logger.log_graph_update(
            "reflect_and_decide",
            reflect_update,
            reflected_state,
            ["[reflect_and_decide] iter 3/5 decision=reconfigure stagnant=True best=63.4"],
        )
        logger.close()

        _, records, _ = _logger_output(tmp, "reflect-summary")
        interpret_record = records[1]
        reflect_record = records[2]

        assert interpret_record["summary"] == "XPhos sharply improved yield while the solvent story weakened."
        assert any("supported=H1" in item for item in interpret_record["outcome"])
        assert any("refuted=H2" in item for item in interpret_record["outcome"])
        assert any("focus=Probe solvent dependence around the XPhos optimum." in item for item in interpret_record["outcome"])

        assert reflect_record["summary"] == "Reflected on campaign progress and chose `reconfigure`."
        assert any("Progress plateaued and the acquisition value is nearly exhausted." in item for item in reflect_record["reasoning"])
        assert any("confidence=0.91" in item for item in reflect_record["reasoning"])
        assert any("is_stagnant=true" in item for item in reflect_record["outcome"])
        assert reflect_record["llm_usage"]["estimated"] is True
        assert set(reflect_record["state_delta"]).issubset(_allowed_state_delta_keys())


def test_logger_preserves_full_semantic_details_without_truncation():
    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings(llm_model="mock", output_dir=tmp)
        logger = CampaignRunLogger(settings, "no-truncation")
        logger.log_session_start({"problem_spec": {"budget": 5}}, {"configurable": {"thread_id": "no-truncation"}})

        long_hypothesis = (
            "Electron-rich bulky phosphines should outperform leaner ligands because they accelerate oxidative "
            "addition, stabilize the productive Pd cycle, and reduce off-cycle resting states across the full "
            "temperature window without needing an immediate solvent change."
        )
        warm_state = _base_state()
        warm_state["phase"] = "warm_starting"
        warm_state["iteration"] = 0
        warm_state["next_action"] = "run_warm_start"
        warm_state["best_result"] = 71.25
        warm_state["best_candidate"] = {
            "ligand": "XPhos",
            "base": "CsOPiv",
            "solvent": "DMAc",
            "temperature": 110,
            "concentration": 0.2,
        }
        warm_state["embedding_config"] = {"method": "fingerprint_concat"}
        warm_state["bo_config"] = {
            "surrogate_model": "gp",
            "kernel_config": {"key": "matern52"},
            "acquisition_function": "log_ei",
        }
        warm_state["proposal_shortlist"] = [
            {
                "candidate": {
                    "ligand": "XPhos",
                    "base": "CsOPiv",
                    "solvent": "DMAc",
                    "temperature": 110,
                    "concentration": 0.2,
                },
                "warm_start_category": "prior_guided",
                "warm_start_rationale": (
                    "Rank 1: Matches the leading literature prior, isolates the most promising ligand/base pair, "
                    "and keeps the solvent fixed so the next result can be attributed cleanly without ambiguity."
                ),
            },
            {
                "candidate": {
                    "ligand": "BrettPhos",
                    "base": "Cs2CO3",
                    "solvent": "DMAc",
                    "temperature": 100,
                    "concentration": 0.15,
                },
                "warm_start_category": "prior_guided",
                "warm_start_rationale": "Rank 2: Keeps the solvent fixed while probing whether a second bulky phosphine reproduces the same trend.",
            },
            {
                "candidate": {
                    "ligand": "SPhos",
                    "base": "K3PO4",
                    "solvent": "tAmOH",
                    "temperature": 120,
                    "concentration": 0.1,
                },
                "warm_start_category": "exploration",
                "warm_start_rationale": "Rank 3: Pushes into a more weakly supported solvent/base region to measure transferability.",
            },
            {
                "candidate": {
                    "ligand": "PPh3",
                    "base": "K2CO3",
                    "solvent": "DMSO",
                    "temperature": 90,
                    "concentration": 0.05,
                },
                "warm_start_category": "exploration",
                "warm_start_rationale": "Rank 4: Low-priority baseline that anchors the bottom of the search space for contrast.",
            },
        ]
        warm_state["warm_start_queue"] = list(warm_state["proposal_shortlist"])
        warm_state["hypotheses"] = [
            {
                "id": "H1",
                "text": long_hypothesis,
                "mechanism": "Bulky electron-rich ligands accelerate oxidative addition.",
                "testable_prediction": "XPhos-like ligands should outperform PPh3-like ligands.",
                "confidence": "high",
                "status": "active",
            }
        ]
        warm_state["memory"]["working"]["current_focus"] = (
            "Keep ligand and base attribution clean before opening a solvent branch; preserve enough context to "
            "explain why each warm-start candidate was chosen."
        )
        logger.log_graph_update(
            "warm_start",
            {"messages": []},
            warm_state,
            ["[warm_start] iter 0/5 warm-start queued=4 prior_guided=2 exploration=2"],
        )
        logger.close()

        _, records, timeline = _logger_output(tmp, "no-truncation")
        record = records[-1]

        assert len(record["outcome"]) == 4
        assert any("Rank 1: Matches the leading literature prior" in item for item in record["outcome"])
        assert any("Rank 4: Low-priority baseline" in item for item in record["outcome"])
        assert any("concentration=0.2" in item for item in record["outcome"])
        assert record["state_delta"]["working_memory_focus"].endswith("why each warm-start candidate was chosen.")
        assert "..." not in timeline
        assert "#4 | {ligand=PPh3, base=K2CO3, solvent=DMSO, temperature=90, concentration=0.05}" in timeline
        assert "best candidate: {ligand=XPhos, base=CsOPiv, solvent=DMAc, temperature=110, concentration=0.2}" in timeline
        assert "bo signature: gp/matern52/log_ei" in timeline
        assert "proposal shortlist count: 4" in timeline
        assert "working memory focus: Keep ligand and base attribution clean before opening a solvent branch; preserve enough context to explain why each warm-start candidate was chosen." in timeline


def test_default_run_id_uses_model_task_name_task_type_and_run_id():
    settings = Settings(
        llm_model="kimi-k2.5",
        experiment_name="dar_benchmark",
        experiment_id="run03",
    )
    initial_state = {
        "problem_spec": {
            "reaction_type": "DAR",
            "budget": 5,
        }
    }

    run_id = _default_run_id(initial_state, settings)

    assert run_id == "kimi-k2.5_dar_benchmark_DAR_run03"


def test_logger_exports_dataset_aligned_experiment_csv_and_llm_trace():
    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings(llm_model="mock", output_dir=tmp)
        logger = CampaignRunLogger(settings, "artifact-export")
        problem_spec = {
            "budget": 5,
            "target_metric": "yield",
            "dataset": {
                "csv_path": str(DAR_DATASET_PATH.resolve()),
                "feature_columns": [
                    "base_SMILES",
                    "ligand_SMILES",
                    "solvent_SMILES",
                    "concentration",
                    "temperature",
                ],
                "target_column": "yield",
                "row_id_column": "",
            },
        }
        logger.log_session_start({"problem_spec": problem_spec}, {"configurable": {"thread_id": "artifact-export"}})

        candidate = {
            "base_SMILES": "O=C([O-])C.[K+]",
            "ligand_SMILES": "CN(C)C1=CC=CC(N(C)C)=C1C2=CC=CC=C2P(C(C)(C)C)C3=CC=CC=C3",
            "solvent_SMILES": "CC(N(C)C)=O",
            "concentration": "0.1",
            "temperature": "105",
        }
        trace_state = _base_state()
        trace_state["problem_spec"] = problem_spec
        trace_state["phase"] = "selecting_candidate"
        trace_state["iteration"] = 1
        trace_state["proposal_selected"] = {
            "selected_index": 0,
            "override": False,
            "candidate": candidate,
            "rationale": {
                "chemical_reasoning": "This ligand looked strongest in prior evidence.",
                "hypothesis_alignment": "It directly probes the leading ligand hypothesis.",
                "information_value": "It separates ligand effects from solvent changes.",
                "concerns": "Temperature is still fixed.",
            },
            "confidence": 0.88,
            "selection_source": "llm_shortlist",
        }
        trace_state["last_llm_usage"] = {
            "node": "select_candidate",
            "calls": 1,
            "input_tokens": 111,
            "output_tokens": 33,
            "total_tokens": 144,
            "estimated": False,
        }
        logger.log_graph_update(
            "select_candidate",
            {
                "messages": [
                    AIMessage(
                        content=json.dumps(
                            {
                                "selected_index": 0,
                                "override": False,
                                "rationale": trace_state["proposal_selected"]["rationale"],
                                "confidence": 0.88,
                            }
                        )
                    )
                ]
            },
            trace_state,
            [
                "[select_candidate] iter 1/5 selected source=llm_shortlist override=False "
                "candidate={base_SMILES=O=C([O-])C.[K+], ligand_SMILES=CN(C)C1=CC=CC(N(C)C)=C1C2=CC=CC=C2P(C(C)(C)C)C3=CC=CC=C3, solvent_SMILES=CC(N(C)C)=O, concentration=0.1, temperature=105}"
            ],
        )

        final_state = copy.deepcopy(trace_state)
        final_state["phase"] = "completed"
        final_state["observations"] = [
            {
                "iteration": 1,
                "candidate": candidate,
                "result": 78.95,
                "metadata": {
                    "notes": "dataset_auto",
                    "dataset_path": str(DAR_DATASET_PATH.resolve()),
                    "dataset_target_column": "yield",
                    "dataset_row_id": "2",
                },
            }
        ]
        final_state["best_result"] = 78.95
        final_state["best_candidate"] = candidate
        final_state["llm_reasoning_log"] = ["[select_candidate] override=False index=0"]
        final_state["llm_token_usage"] = {
            "calls": 1,
            "input_tokens": 111,
            "output_tokens": 33,
            "total_tokens": 144,
            "estimated_calls": 0,
            "by_node": {
                "select_candidate": {
                    "calls": 1,
                    "input_tokens": 111,
                    "output_tokens": 33,
                    "total_tokens": 144,
                    "estimated_calls": 0,
                }
            },
        }
        final_state["final_summary"] = {
            "total_experiments": 1,
            "best_result": 78.95,
            "best_candidate": candidate,
            "stop_reason": "completed",
            "proposal_strategy": "bo",
            "llm_token_usage": final_state["llm_token_usage"],
            "final_config": {},
        }

        logger.log_session_end(final_state)
        logger.close()

        run_dir = Path(tmp) / "artifact-export"
        fieldnames, rows = _read_csv(run_dir / "experiment_records.csv")
        trace = json.loads((run_dir / "llm_trace.json").read_text(encoding="utf-8"))

        assert fieldnames == ["", "base_SMILES", "ligand_SMILES", "solvent_SMILES", "concentration", "temperature", "yield"]
        assert rows == [
            {
                "": "2",
                "base_SMILES": candidate["base_SMILES"],
                "ligand_SMILES": candidate["ligand_SMILES"],
                "solvent_SMILES": candidate["solvent_SMILES"],
                "concentration": "0.1",
                "temperature": "105",
                "yield": "78.95",
            }
        ]
        assert trace["run_id"] == "artifact-export"
        assert trace["state_reasoning_log"] == ["[select_candidate] override=False index=0"]
        assert trace["llm_token_usage"]["total_tokens"] == 144
        assert len(trace["steps"]) == 1
        assert trace["steps"][0]["node"] == "select_candidate"
        assert trace["steps"][0]["llm_usage"]["total_tokens"] == 144
        assert trace["steps"][0]["messages"][0]["role"] == "ai"
        assert trace["steps"][0]["parsed_output"]["selected_index"] == 0
