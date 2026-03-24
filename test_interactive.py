"""
Interactive terminal harness for ChemBO human-in-the-loop runs.
"""
from __future__ import annotations

import argparse

from config.settings import Settings
from core.campaign_runner import run_campaign
from core.graph import build_chembo_graph
from core.problem_loader import load_problem_file
from core.state import create_initial_state


DEFAULT_PROBLEM = (
    "Optimize the yield of a Direct Arylation Reaction (DAR) between "
    "4-bromotoluene and 2-methylthiophene. Variables to optimize: "
    "ligand (categorical: PPh3, P(Cy)3, XPhos, SPhos, DavePhos), "
    "base (categorical: K2CO3, Cs2CO3, KOAc, CsOPiv), "
    "solvent (categorical: DMAc, DMF, NMP, toluene), "
    "temperature (continuous: 80-150C), concentration (continuous: 0.1-0.5 M). "
    "Target: maximize GC yield (%). Budget: 10 experiments."
)


def main():
    parser = argparse.ArgumentParser(description="Interactive ChemBO loop")
    parser.add_argument("--problem-file", type=str, default=None)
    parser.add_argument("--config", type=str, default=None)
    args = parser.parse_args()

    problem_text = load_problem_file(args.problem_file) if args.problem_file else DEFAULT_PROBLEM
    settings = Settings.from_yaml(args.config) if args.config else Settings()
    graph = build_chembo_graph(settings)
    initial_state = create_initial_state(problem_text, settings)
    state = run_campaign(
        graph,
        initial_state,
        settings,
        thread_id=f"interactive-{settings.experiment_id}",
        printer=print,
    )

    print("\nCampaign completed.")
    print(f"Best result: {state.get('best_result')}")
    print(f"Best candidate: {state.get('best_candidate')}")


if __name__ == "__main__":
    main()
