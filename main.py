"""
ChemBO Agent — Phase 1 Demo
============================
A LangGraph-based autonomous Bayesian Optimization agent for chemical reaction
optimization (validated on Direct Arylation Reactions).

Architecture: Single LLM cognitive core + 6 tool modules + 3-layer memory + knowledge base.

Usage:
    python main.py --problem "Optimize DAR yield for aryl bromide + thiophene coupling"
    python main.py --problem-file problem_description.yaml
"""
import argparse
import asyncio
import json
from pathlib import Path

from core.graph import build_chembo_graph
from core.state import ChemBOState, create_initial_state
from config.settings import Settings


async def run_chembo_agent(problem_description: str, settings: Settings | None = None):
    """Main entry point: run the ChemBO agent on a given problem."""
    settings = settings or Settings()
    
    # Build the LangGraph
    graph = build_chembo_graph(settings)
    
    # Create initial state from problem description
    initial_state = create_initial_state(problem_description, settings)
    
    print("=" * 60)
    print("ChemBO Agent — Phase 1 Demo")
    print("=" * 60)
    print(f"Problem: {problem_description[:100]}...")
    print(f"LLM: {settings.llm_model}")
    print(f"Max iterations: {settings.max_bo_iterations}")
    print("=" * 60)
    
    # Run the graph — it will pause at human-in-the-loop checkpoints
    config = {"configurable": {"thread_id": settings.experiment_id}}
    
    async for event in graph.astream(initial_state, config=config):
        node_name = list(event.keys())[0]
        state_update = event[node_name]
        
        # Pretty-print progress
        _print_node_output(node_name, state_update)
    
    print("\n✅ Optimization campaign complete.")


def _print_node_output(node_name: str, state_update: dict):
    """Pretty-print each node's output for interactive monitoring."""
    icon_map = {
        "analyze_problem": "🔬",
        "configure_bo": "⚙️",
        "generate_hypotheses": "💡",
        "run_bo_iteration": "🎯",
        "await_human_results": "🧑‍🔬",
        "interpret_results": "📊",
        "reflect_and_decide": "🤔",
    }
    icon = icon_map.get(node_name, "▶")
    print(f"\n{icon} [{node_name}]")
    
    if "messages" in state_update:
        for msg in state_update.get("messages", []):
            if hasattr(msg, "content"):
                # Truncate for display
                content = msg.content[:200] + "..." if len(msg.content) > 200 else msg.content
                print(f"   {content}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ChemBO Agent")
    parser.add_argument("--problem", type=str, help="Problem description string")
    parser.add_argument("--problem-file", type=str, help="Path to YAML problem description")
    parser.add_argument("--config", type=str, help="Path to settings YAML", default=None)
    args = parser.parse_args()
    
    if args.problem_file:
        problem = Path(args.problem_file).read_text()
    elif args.problem:
        problem = args.problem
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
    
    settings = Settings.from_yaml(args.config) if args.config else Settings()
    asyncio.run(run_chembo_agent(problem, settings))
