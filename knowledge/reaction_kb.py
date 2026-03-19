"""
ChemBO Agent — Knowledge Base
==============================
Stub for the knowledge base layer. In Phase 1, this provides hardcoded
domain knowledge about common reaction types. Later phases will integrate
the five prioritized KBs (reaction vector DB, literature RAG, ligand KG,
private ELN, mechanistic constraints).

For the demo, the KB is a simple dict lookup by reaction type.
"""
from __future__ import annotations

# ============================================================================
# Hardcoded domain knowledge for Phase 1 demo
# ============================================================================

REACTION_KNOWLEDGE = {
    "DAR": {
        "full_name": "Direct Arylation Reaction",
        "mechanism": "Pd-catalyzed C-H activation / concerted metalation-deprotonation (CMD)",
        "key_factors": [
            "Ligand choice critically affects regioselectivity (mono- vs. bidentate phosphines)",
            "Base must be carbonate/carboxylate family for CMD mechanism",
            "Polar aprotic solvents (DMAc, DMF, NMP) generally preferred",
            "Temperature typically 80-150°C; higher temp risks decomposition",
            "Pd loading typically 1-5 mol%",
            "Electron-rich substrates tend to give higher yields",
        ],
        "common_pitfalls": [
            "β-elimination forming aryne intermediates at high temperature",
            "Homocoupling of the aryl halide",
            "Solvent decomposition above 160°C for DMAc",
            "Catalyst poisoning by sulfur-containing heterocycles",
        ],
        "literature_priors": {
            "best_ligands": ["P(Cy)3", "XPhos", "DavePhos"],
            "best_bases": ["Cs2CO3", "CsOPiv", "K2CO3"],
            "best_solvents": ["DMAc", "NMP"],
            "optimal_temp_range": [100, 140],
        },
    },
    "BH": {
        "full_name": "Buchwald-Hartwig Amination",
        "mechanism": "Pd-catalyzed C-N cross-coupling via oxidative addition / reductive elimination",
        "key_factors": [
            "Ligand-metal ratio is critical for preventing β-hydride elimination",
            "Bulky biaryl phosphines (SPhos, XPhos, RuPhos) are state of the art",
            "Strong bases (NaOtBu, LiHMDS) required for deprotonation",
            "Temperature typically 80-120°C",
        ],
        "common_pitfalls": [
            "Competing C-O coupling with phenolic substrates",
            "Base sensitivity — NaOtBu can cause epimerization",
        ],
        "literature_priors": {
            "best_ligands": ["XPhos", "SPhos", "tBuXPhos", "BrettPhos"],
            "best_bases": ["NaOtBu", "Cs2CO3", "LiHMDS"],
            "best_solvents": ["toluene", "dioxane", "THF"],
            "optimal_temp_range": [80, 120],
        },
    },
    "Suzuki": {
        "full_name": "Suzuki-Miyaura Coupling",
        "mechanism": "Pd-catalyzed cross-coupling of boronic acids with aryl halides",
        "key_factors": [
            "Requires base for boronate transmetalation",
            "Tolerates a wide range of functional groups",
            "Aqueous co-solvents often beneficial",
            "Temperature typically 50-100°C",
        ],
        "common_pitfalls": [
            "Protodeboronation of boronic acid under harsh conditions",
            "Homocoupling side product",
        ],
        "literature_priors": {
            "best_ligands": ["PPh3", "SPhos", "XPhos"],
            "best_bases": ["K2CO3", "Cs2CO3", "K3PO4"],
            "best_solvents": ["THF/H2O", "dioxane/H2O", "DMF"],
            "optimal_temp_range": [60, 100],
        },
    },
}


def get_reaction_knowledge(reaction_type: str) -> dict | None:
    """Retrieve hardcoded domain knowledge for a reaction type."""
    return REACTION_KNOWLEDGE.get(reaction_type.upper())


def get_available_reactions() -> list[str]:
    return list(REACTION_KNOWLEDGE.keys())


def format_knowledge_for_llm(reaction_type: str) -> str:
    """Format knowledge as a context string for LLM prompts."""
    kb = get_reaction_knowledge(reaction_type)
    if not kb:
        return f"No specific knowledge available for reaction type: {reaction_type}"
    
    import json
    return (
        f"DOMAIN KNOWLEDGE: {kb['full_name']}\n"
        f"Mechanism: {kb['mechanism']}\n"
        f"Key factors: {json.dumps(kb['key_factors'], indent=2)}\n"
        f"Common pitfalls: {json.dumps(kb['common_pitfalls'], indent=2)}\n"
        f"Literature priors: {json.dumps(kb['literature_priors'], indent=2)}"
    )
