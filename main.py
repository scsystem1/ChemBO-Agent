"""
ChemBO Agent — Phase 1 Demo
============================
"""
import argparse
from pathlib import Path

from core.campaign_runner import run_campaign
from core.graph import build_chembo_graph
from core.problem_loader import load_problem_file, problem_preview
from core.state import create_initial_state
from config.settings import Settings
from pools.component_pools import detect_runtime_capabilities

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "lightning.yaml"


def run_chembo_agent(problem_description: str | dict, settings: Settings | None = None):
    """Main entry point: run the ChemBO agent on a given problem."""
    settings = settings or Settings()
    graph = build_chembo_graph(settings)
    initial_state = create_initial_state(problem_description, settings)

    print("=" * 60)
    print("ChemBO Agent — Phase 1 Demo")
    print("=" * 60)
    print(f"Problem: {problem_preview(problem_description)[:100]}...")
    print(f"LLM: {settings.llm_model}")
    print(f"Max iterations: {settings.max_bo_iterations}")
    print(f"Human input mode: {settings.human_input_mode}")
    print(f"Runtime mode: {detect_runtime_capabilities()['runtime_mode']}")
    print("=" * 60)

    final_state = run_campaign(graph, initial_state, settings, printer=print)
    print("\nOptimization campaign complete.")
    print(f"Best result: {final_state.get('best_result')}")
    print(f"Best candidate: {final_state.get('best_candidate')}")
    return final_state


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ChemBO Agent")
    parser.add_argument("--problem", type=str, help="Problem description string")
    parser.add_argument("--problem-file", type=str, help="Path to YAML problem description")
    parser.add_argument("--config", type=str, help="Path to settings YAML", default=None)
    args = parser.parse_args()
    
    if args.problem:
        problem = args.problem
    elif args.problem_file:
        problem = load_problem_file(args.problem_file)
    else:
        # Default demo problem
        problem = (
            "Optimize the yield of a Direct Arylation Reaction (DAR) between "
            "4-bromotoluene and 2-methylthiophene. Variables to optimize: "
            "ligand (categorical: PPh3, P(Cy)3, XPhos, SPhos, DavePhos), "
            "base (categorical: K2CO3, Cs2CO3, KOAc, CsOPiv), "
            "solvent (categorical: DMAc, DMF, NMP, toluene), "
            "temperature (continuous: 80-150°C), "
            "concentration (continuous: 0.1-0.5 M). "
            "Target: maximize GC yield (%). Budget: 30 experiments."
        )
    
    if args.config:
        settings = Settings.from_yaml(args.config)
    elif DEFAULT_CONFIG_PATH.exists():
        settings = Settings.from_yaml(str(DEFAULT_CONFIG_PATH))
    else:
        settings = Settings()
    run_chembo_agent(problem, settings)
