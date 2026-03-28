"""
Graph-level regressions for review-driven fixes.
"""
from __future__ import annotations

from pathlib import Path

try:
    import core.graph as graph_module
    from langchain_core.messages import AIMessage, ToolMessage
    from langgraph.types import Command
    from config.settings import Settings
    from core.campaign_runner import format_progress_update, run_campaign
    from core.problem_loader import load_problem_file, resolve_campaign_budget
    from core.state import create_initial_state
    from test_mock import MockChemBOLLM

    TEST_DEPS_AVAILABLE = True
except ModuleNotFoundError as exc:  # pragma: no cover - local env may lack optional deps
    graph_module = None
    AIMessage = None
    ToolMessage = None
    Command = None
    Settings = None
    format_progress_update = None
    run_campaign = None
    load_problem_file = None
    resolve_campaign_budget = None
    create_initial_state = None
    MockChemBOLLM = None
    TEST_DEPS_AVAILABLE = False
    IMPORT_ERROR = exc


ROOT = Path(__file__).resolve().parent
EXAMPLE_PATH = ROOT / "examples" / "dar_problem.yaml"


def test_warm_start_filters_hallucinated_candidates():
    if not TEST_DEPS_AVAILABLE:
        print(f"Skipping graph regression test: {IMPORT_ERROR}")
        return

    original_factory = graph_module._create_llm
    graph_module._create_llm = lambda settings: MockChemBOLLM(warm_start_mode="hallucinate")
    try:
        settings = Settings(llm_model="mock", batch_size=1, max_bo_iterations=2, human_input_mode="terminal")
        graph = graph_module.build_chembo_graph(settings)
        initial_state = create_initial_state(
            "Optimize DAR yield with ligand, base, solvent, temperature, and concentration.",
            settings,
        )
        config = {"configurable": {"thread_id": "warm-start-hallucination"}}
        graph.invoke(initial_state, config=config)
        state = graph.get_state(config).values

        shortlist = state["proposal_shortlist"]
        assert shortlist
        assert all(item["candidate"].get("ligand") != "NotRealLigand" for item in shortlist)
    finally:
        graph_module._create_llm = original_factory


def test_invalid_override_candidate_falls_back_to_shortlist():
    if not TEST_DEPS_AVAILABLE:
        print(f"Skipping graph regression test: {IMPORT_ERROR}")
        return

    original_factory = graph_module._create_llm
    graph_module._create_llm = lambda settings: MockChemBOLLM(selection_mode="invalid_override")
    try:
        problem = load_problem_file(EXAMPLE_PATH)
        problem["budget"] = 3
        settings = Settings(
            llm_model="mock",
            batch_size=1,
            max_bo_iterations=3,
            initial_doe_size=1,
            human_input_mode="dataset_auto",
        )
        graph = graph_module.build_chembo_graph(settings)
        initial_state = create_initial_state(problem, settings)
        config = {"configurable": {"thread_id": "invalid-override"}}
        graph.invoke(initial_state, config=config)
        state = graph.get_state(config).values
        first_candidate = state["current_proposal"]["candidates"][0]
        oracle = graph_module.DatasetOracle.from_problem_spec(problem)
        assert oracle is not None
        first_result = oracle.lookup(first_candidate)["result"]

        graph.invoke(Command(resume={"result": first_result, "notes": "dataset_auto"}), config=config)
        state = graph.get_state(config).values

        shortlist = state["proposal_shortlist"]
        assert shortlist
        assert state["proposal_selected"]["selection_source"] == "llm_shortlist"
        assert state["proposal_selected"]["override"] is False
        assert state["current_proposal"]["selected_record"]["candidate"] == shortlist[0]["candidate"]
    finally:
        graph_module._create_llm = original_factory


def test_null_selected_index_falls_back_to_shortlist_zero():
    if not TEST_DEPS_AVAILABLE:
        print(f"Skipping graph regression test: {IMPORT_ERROR}")
        return

    original_factory = graph_module._create_llm
    graph_module._create_llm = lambda settings: MockChemBOLLM(selection_mode="null_index")
    try:
        problem = load_problem_file(EXAMPLE_PATH)
        problem["budget"] = 3
        settings = Settings(
            llm_model="mock",
            batch_size=1,
            max_bo_iterations=3,
            initial_doe_size=1,
            human_input_mode="dataset_auto",
        )
        graph = graph_module.build_chembo_graph(settings)
        initial_state = create_initial_state(problem, settings)
        config = {"configurable": {"thread_id": "null-selected-index"}}
        graph.invoke(initial_state, config=config)
        state = graph.get_state(config).values

        first_candidate = state["current_proposal"]["candidates"][0]
        oracle = graph_module.DatasetOracle.from_problem_spec(problem)
        assert oracle is not None
        first_result = oracle.lookup(first_candidate)["result"]

        graph.invoke(Command(resume={"result": first_result, "notes": "dataset_auto"}), config=config)
        state = graph.get_state(config).values

        shortlist = state["proposal_shortlist"]
        assert shortlist
        assert state["proposal_selected"]["selected_index"] == 0
        assert state["proposal_selected"]["override"] is False
        assert state["proposal_selected"]["confidence"] == 0.5
        assert state["current_proposal"]["selected_record"]["candidate"] == shortlist[0]["candidate"]
    finally:
        graph_module._create_llm = original_factory


def test_reconfiguration_backtesting_rejects_worse_config_and_preserves_hypotheses():
    if not TEST_DEPS_AVAILABLE:
        print(f"Skipping graph regression test: {IMPORT_ERROR}")
        return

    original_factory = graph_module._create_llm
    graph_module._create_llm = lambda settings: MockChemBOLLM(reconfig_config_mode="high_noise_on_reconfig")
    try:
        problem = load_problem_file(EXAMPLE_PATH)
        problem["budget"] = 6
        settings = Settings(llm_model="mock", batch_size=1, max_bo_iterations=6, human_input_mode="dataset_auto")
        graph = graph_module.build_chembo_graph(settings)
        initial_state = create_initial_state(problem, settings)
        state = run_campaign(graph, initial_state, settings, thread_id="reconfig-backtesting", printer=None)

        assert state["reconfig_history"]
        latest = state["reconfig_history"][-1]
        assert latest["backtesting"]["required"] is True
        assert latest["accepted"] is False
        hypothesis_ids = {item["id"] for item in state["hypotheses"]}
        assert "H1" in hypothesis_ids
        assert "H3" in hypothesis_ids
    finally:
        graph_module._create_llm = original_factory


def test_campaign_summary_node_populates_final_summary():
    if not TEST_DEPS_AVAILABLE:
        print(f"Skipping graph regression test: {IMPORT_ERROR}")
        return

    original_factory = graph_module._create_llm
    graph_module._create_llm = lambda settings: MockChemBOLLM()
    try:
        problem = load_problem_file(EXAMPLE_PATH)
        problem["budget"] = 4
        settings = Settings(llm_model="mock", batch_size=1, max_bo_iterations=4, human_input_mode="dataset_auto")
        graph = graph_module.build_chembo_graph(settings)
        initial_state = create_initial_state(problem, settings)
        state = run_campaign(graph, initial_state, settings, thread_id="campaign-summary-node", printer=None)

        assert state["phase"] == "completed"
        assert state["final_summary"]
        assert state["final_summary"]["total_experiments"] == len(state["observations"])
        assert "stop_reason" in state["final_summary"]
        assert "conclusion" in state["final_summary"]
    finally:
        graph_module._create_llm = original_factory


def test_convergence_state_ignores_none_acquisition_values():
    if not TEST_DEPS_AVAILABLE:
        print(f"Skipping graph regression test: {IMPORT_ERROR}")
        return

    settings = Settings(llm_model="mock")
    state = create_initial_state("Optimize DAR yield.", settings)
    state["performance_log"] = [
        {"iteration": 1, "result": 10.0, "best_so_far": 10.0, "improved": True},
        {"iteration": 2, "result": 12.0, "best_so_far": 12.0, "improved": True},
    ]
    state["last_tool_payload"] = {"acquisition_values": [None, None]}

    convergence = graph_module.compute_convergence_state(state, settings)
    assert convergence["max_af_value"] is None


def test_context_builder_sanitizes_historical_tool_messages_for_provider_compatibility():
    if not TEST_DEPS_AVAILABLE:
        print(f"Skipping graph regression test: {IMPORT_ERROR}")
        return

    settings = Settings(llm_model="mock")
    state = create_initial_state("Optimize DAR yield.", settings)
    state["campaign_summary"] = "Prior BO rounds already happened."
    state["messages"] = state["messages"] + [
        AIMessage(
            content="Calling BO runner.",
            tool_calls=[{"id": "bo-tool", "name": "bo_runner", "args": {"top_k": 5}}],
        ),
        ToolMessage(
            content='{"status": "success", "shortlist": [{"candidate": {"x": 1}}]}',
            name="bo_runner",
            tool_call_id="bo-tool",
        ),
        AIMessage(content='{"decision": "continue"}'),
    ]

    context_messages, _ = graph_module._build_context_messages(state)

    assert not any(isinstance(message, ToolMessage) for message in context_messages)
    sanitized_ai_messages = [message for message in context_messages if isinstance(message, AIMessage)]
    assert sanitized_ai_messages
    assert all(not getattr(message, "tool_calls", None) for message in sanitized_ai_messages)


def test_true_warm_start_executes_five_real_experiments_before_bo():
    if not TEST_DEPS_AVAILABLE:
        print(f"Skipping graph regression test: {IMPORT_ERROR}")
        return

    original_factory = graph_module._create_llm
    graph_module._create_llm = lambda settings: MockChemBOLLM()
    try:
        problem = load_problem_file(EXAMPLE_PATH)
        problem["budget"] = 6
        settings = Settings(llm_model="mock", batch_size=1, max_bo_iterations=6, initial_doe_size=5, human_input_mode="dataset_auto")
        graph = graph_module.build_chembo_graph(settings)
        initial_state = create_initial_state(problem, settings)
        oracle = graph_module.DatasetOracle.from_problem_spec(problem)
        assert oracle is not None

        config = {"configurable": {"thread_id": "true-warm-start"}}
        graph.invoke(initial_state, config=config)
        state = graph.get_state(config).values

        queued = [item["candidate"] for item in state["warm_start_queue"]]
        assert len(queued) == 5
        assert state["warm_start_active"] is True
        assert state["proposal_selected"]["selection_source"] == "warm_start_queue"

        for expected_candidate in queued:
            assert state["current_proposal"]["candidates"][0] == expected_candidate
            result = oracle.lookup(expected_candidate)["result"]
            graph.invoke(Command(resume={"result": result, "notes": "dataset_auto"}), config=config)
            state = graph.get_state(config).values

        assert len(state["observations"]) == 5
        assert [obs["candidate"] for obs in state["observations"][:5]] == queued
        assert state["warm_start_active"] is False
        assert state["phase"] != "completed"
    finally:
        graph_module._create_llm = original_factory


def test_pure_reasoning_switches_to_reasoning_shortlist_after_true_warm_start():
    if not TEST_DEPS_AVAILABLE:
        print(f"Skipping graph regression test: {IMPORT_ERROR}")
        return

    original_factory = graph_module._create_llm
    graph_module._create_llm = lambda settings: MockChemBOLLM()
    try:
        problem = load_problem_file(EXAMPLE_PATH)
        problem["budget"] = 6
        settings = Settings(
            llm_model="mock",
            batch_size=1,
            max_bo_iterations=6,
            initial_doe_size=5,
            ablation_pure_reasoning=True,
            human_input_mode="dataset_auto",
        )
        graph = graph_module.build_chembo_graph(settings)
        initial_state = create_initial_state(problem, settings)
        oracle = graph_module.DatasetOracle.from_problem_spec(problem)
        assert oracle is not None

        config = {"configurable": {"thread_id": "pure-reasoning-after-warm-start"}}
        graph.invoke(initial_state, config=config)
        state = graph.get_state(config).values
        queued = [item["candidate"] for item in state["warm_start_queue"]]
        assert len(queued) == 5

        for expected_candidate in queued:
            assert state["current_proposal"]["candidates"][0] == expected_candidate
            result = oracle.lookup(expected_candidate)["result"]
            graph.invoke(Command(resume={"result": result, "notes": "dataset_auto"}), config=config)
            state = graph.get_state(config).values

        assert len(state["observations"]) == 5
        assert state["warm_start_active"] is False
        assert state["last_tool_payload"]["strategy"] == "pure_reasoning_shortlist"
        assert len(state["proposal_shortlist"]) == 5
        assert state["proposal_selected"]["selection_source"] == "llm_shortlist"
        assert all(oracle.candidate_exists(item["candidate"]) for item in state["proposal_shortlist"])
    finally:
        graph_module._create_llm = original_factory


def test_pure_reasoning_repairs_invalid_duplicate_and_observed_candidates():
    if not TEST_DEPS_AVAILABLE:
        print(f"Skipping graph regression test: {IMPORT_ERROR}")
        return

    original_factory = graph_module._create_llm
    graph_module._create_llm = lambda settings: MockChemBOLLM(reasoning_mode="duplicates_and_invalid")
    try:
        problem = load_problem_file(EXAMPLE_PATH)
        problem["budget"] = 3
        settings = Settings(
            llm_model="mock",
            batch_size=1,
            max_bo_iterations=3,
            initial_doe_size=1,
            ablation_pure_reasoning=True,
            human_input_mode="dataset_auto",
        )
        graph = graph_module.build_chembo_graph(settings)
        initial_state = create_initial_state(problem, settings)
        oracle = graph_module.DatasetOracle.from_problem_spec(problem)
        assert oracle is not None

        config = {"configurable": {"thread_id": "pure-reasoning-repair"}}
        graph.invoke(initial_state, config=config)
        state = graph.get_state(config).values
        observed_candidate = state["current_proposal"]["candidates"][0]
        observed_key = graph_module.candidate_to_key(observed_candidate)
        first_result = oracle.lookup(observed_candidate)["result"]

        graph.invoke(Command(resume={"result": first_result, "notes": "dataset_auto"}), config=config)
        state = graph.get_state(config).values

        shortlist = state["proposal_shortlist"]
        shortlist_keys = [graph_module.candidate_to_key(item["candidate"]) for item in shortlist]
        metadata = state["last_tool_payload"]["metadata"]
        assert len(shortlist) == 5
        assert len(shortlist_keys) == len(set(shortlist_keys))
        assert observed_key not in shortlist_keys
        assert metadata["repair_attempted"] is True
        assert metadata["validation_rejections"] >= 1
        assert all(oracle.candidate_exists(item["candidate"]) for item in shortlist)
    finally:
        graph_module._create_llm = original_factory


def test_pure_reasoning_reconfigure_updates_hypotheses_without_bo_reconfigure():
    if not TEST_DEPS_AVAILABLE:
        print(f"Skipping graph regression test: {IMPORT_ERROR}")
        return

    original_factory = graph_module._create_llm
    graph_module._create_llm = lambda settings: MockChemBOLLM(reconfig_config_mode="high_noise_on_reconfig")
    try:
        problem = load_problem_file(EXAMPLE_PATH)
        problem["budget"] = 6
        settings = Settings(
            llm_model="mock",
            batch_size=1,
            max_bo_iterations=6,
            initial_doe_size=5,
            ablation_pure_reasoning=True,
            human_input_mode="dataset_auto",
        )
        graph = graph_module.build_chembo_graph(settings)
        initial_state = create_initial_state(problem, settings)
        state = run_campaign(graph, initial_state, settings, thread_id="pure-reasoning-reconfigure", printer=None)

        hypothesis_ids = {item["id"] for item in state["hypotheses"]}
        assert "H3" in hypothesis_ids
        assert len(state["config_history"]) == 1
        assert state["reconfig_history"] == []
        assert state["total_reconfigs"] == 1
        assert state["last_reconfig_iteration"] == 5
        assert state["final_summary"]["proposal_strategy"] == "pure_reasoning"
    finally:
        graph_module._create_llm = original_factory


def test_progress_formatter_emits_readable_summary():
    if not TEST_DEPS_AVAILABLE:
        print(f"Skipping graph regression test: {IMPORT_ERROR}")
        return

    settings = Settings(llm_model="mock", max_bo_iterations=50)
    state = create_initial_state("Optimize DAR yield.", settings)
    state["problem_spec"]["budget"] = 50
    state["phase"] = "running"
    state["last_tool_payload"] = {"status": "success"}
    state["proposal_shortlist"] = [{"candidate": {"ligand": "XPhos", "temperature": 120}}]

    lines = format_progress_update("run_bo_iteration", {"phase": "running"}, state, settings)
    assert lines
    assert "shortlist=1" in lines[0]
    assert "iter 0/50" in lines[0]


def test_progress_formatter_emits_reasoning_strategy_summary():
    if not TEST_DEPS_AVAILABLE:
        print(f"Skipping graph regression test: {IMPORT_ERROR}")
        return

    settings = Settings(llm_model="mock", max_bo_iterations=50, ablation_pure_reasoning=True)
    state = create_initial_state("Optimize DAR yield.", settings)
    state["problem_spec"]["budget"] = 50
    state["phase"] = "running"
    state["last_tool_payload"] = {"status": "success", "strategy": "pure_reasoning_shortlist"}
    state["proposal_shortlist"] = [{"candidate": {"ligand": "XPhos", "temperature": 120}}]

    lines = format_progress_update("run_reasoning_iteration", {"phase": "running"}, state, settings)
    assert lines
    assert "strategy=pure_reasoning_shortlist" in lines[0]
    assert "shortlist=1" in lines[0]


def test_llm_token_usage_accumulates_during_warm_start():
    if not TEST_DEPS_AVAILABLE:
        print(f"Skipping graph regression test: {IMPORT_ERROR}")
        return

    original_factory = graph_module._create_llm
    graph_module._create_llm = lambda settings: MockChemBOLLM()
    try:
        settings = Settings(llm_model="mock", batch_size=1, max_bo_iterations=3, initial_doe_size=1, human_input_mode="terminal")
        graph = graph_module.build_chembo_graph(settings)
        initial_state = create_initial_state(
            "Optimize DAR yield with ligand, base, solvent, temperature, and concentration.",
            settings,
        )
        config = {"configurable": {"thread_id": "token-usage-warm-start"}}
        graph.invoke(initial_state, config=config)
        state = graph.get_state(config).values

        usage = state["llm_token_usage"]
        assert usage["calls"] > 0
        assert usage["input_tokens"] > 0
        assert usage["output_tokens"] > 0
        assert usage["total_tokens"] == usage["input_tokens"] + usage["output_tokens"]
        assert usage["by_node"]["warm_start"]["calls"] == 1
        assert state["last_llm_usage"]["node"] == "warm_start"

        lines = format_progress_update("warm_start", {"phase": "warm_starting"}, state, settings)
        assert "tokens=" in lines[0]
    finally:
        graph_module._create_llm = original_factory


def test_interpret_results_formatter_prefers_structured_interpretation():
    if not TEST_DEPS_AVAILABLE:
        print(f"Skipping graph regression test: {IMPORT_ERROR}")
        return

    settings = Settings(llm_model="mock", max_bo_iterations=50)
    state = create_initial_state("Optimize DAR yield.", settings)
    state["problem_spec"]["budget"] = 50
    state["best_result"] = 36.66
    state["last_llm_usage"] = {
        "node": "interpret_results",
        "calls": 1,
        "input_tokens": 120,
        "output_tokens": 45,
        "total_tokens": 165,
        "estimated": True,
        "estimated_calls": 1,
    }
    update = {
        "messages": [
            AIMessage(
                content=(
                    '{"interpretation": "Iteration 4 yielded 11.72% with a tri-aryl phosphine ligand; '
                    'carbonate base plus aromatic solvent still looks weak."}'
                )
            )
        ]
    }

    lines = format_progress_update("interpret_results", update, state, settings)
    assert "Iteration 4 yielded 11.72%" in lines[0]
    assert "tokens=~120/45/165" in lines[0]
