"""
ChemBO Agent knowledge base and structured prior helpers.
"""
from __future__ import annotations

from typing import Any


REACTION_KNOWLEDGE: dict[str, dict[str, Any]] = {
    "DAR": {
        "full_name": "Direct Arylation Reaction",
        "mechanism": "Pd-catalyzed C-H activation / concerted metalation-deprotonation (CMD)",
        "key_factors": [
            "Ligand choice critically affects regioselectivity and reactivity.",
            "Carbonate or carboxylate bases often support CMD-like pathways.",
            "Polar aprotic solvents frequently outperform weakly polar solvents.",
            "Temperature must balance activation with decomposition risk.",
        ],
        "common_pitfalls": [
            "Solvent decomposition above 160 C for DMAc-like conditions.",
            "Low-temperature toluene conditions may under-activate the catalytic cycle.",
            "Catalyst poisoning can occur with strongly coordinating impurities.",
        ],
        "literature_priors": {
            "best_ligands": ["P(Cy)3", "XPhos", "DavePhos"],
            "best_bases": ["Cs2CO3", "CsOPiv", "K2CO3"],
            "best_solvents": ["DMAc", "NMP"],
            "optimal_temp_range": [100, 140],
            "optimal_conc_range": [0.08, 0.18],
        },
    },
    "BH": {
        "full_name": "Buchwald-Hartwig Amination",
        "mechanism": "Pd-catalyzed C-N coupling via oxidative addition / amine activation / reductive elimination",
        "key_factors": [
            "Bulky ligands often stabilize productive oxidative addition intermediates.",
            "Strong bases are commonly required for productive amination.",
            "Substrate electronics strongly affect catalyst choice and temperature.",
        ],
        "common_pitfalls": [
            "Overly strong base may damage sensitive substrates.",
            "Ligand-metal mismatch can drive catalyst death.",
        ],
        "literature_priors": {
            "best_ligands": ["XPhos", "SPhos", "BrettPhos"],
            "best_bases": ["NaOtBu", "Cs2CO3", "LiHMDS"],
            "best_solvents": ["toluene", "dioxane", "THF"],
            "optimal_temp_range": [80, 120],
        },
    },
    "SUZUKI": {
        "full_name": "Suzuki-Miyaura Coupling",
        "mechanism": "Pd-catalyzed cross-coupling of organoborons with aryl halides",
        "key_factors": [
            "Base and solvent jointly control boronate activation.",
            "Aqueous co-solvents can help transmetalation.",
            "Ligand choice tunes reactivity for challenging halides.",
        ],
        "common_pitfalls": [
            "Protodeboronation under harsh conditions.",
            "Homocoupling side products under over-oxidizing conditions.",
        ],
        "literature_priors": {
            "best_ligands": ["PPh3", "SPhos", "XPhos"],
            "best_bases": ["K2CO3", "Cs2CO3", "K3PO4"],
            "best_solvents": ["THF/H2O", "dioxane/H2O", "DMF"],
            "optimal_temp_range": [60, 100],
        },
    },
}


DAR_HARD_CONSTRAINTS = [
    {
        "name": "dmac_temp_limit",
        "reason": "Avoid DMAc-like solvent conditions above 160 C because of decomposition risk.",
        "source": "KB:DAR.pitfalls.solvent_decomposition",
        "check": lambda candidate: not (
            _candidate_matches(candidate, "solvent", aliases=["DMAc"], smiles=["CC(N(C)C)=O"])
            and _coerce_float(candidate.get("temperature"), 0.0) > 160.0
        ),
    },
    {
        "name": "toluene_min_temp",
        "reason": "Toluene-like solvent conditions below 100 C are often under-activated for DAR.",
        "source": "KB:DAR.pitfalls.toluene_low_temp",
        "check": lambda candidate: not (
            _candidate_matches(candidate, "solvent", aliases=["toluene"], smiles=["CC1=CC=C(C)C=C1"])
            and _coerce_float(candidate.get("temperature"), 999.0) < 100.0
        ),
    },
]


def get_reaction_knowledge(reaction_type: str) -> dict[str, Any] | None:
    return REACTION_KNOWLEDGE.get(str(reaction_type or "").upper())


def get_available_reactions() -> list[str]:
    return sorted(REACTION_KNOWLEDGE)


def format_knowledge_for_llm(reaction_type: str) -> str:
    kb = get_reaction_knowledge(reaction_type)
    if not kb:
        return f"No specific knowledge available for reaction type: {reaction_type}"
    return "\n".join(
        [
            f"Reaction: {kb['full_name']}",
            f"Mechanism: {kb['mechanism']}",
            "Key factors:",
            *[f"- {item}" for item in kb.get("key_factors", [])],
            "Common pitfalls:",
            *[f"- {item}" for item in kb.get("common_pitfalls", [])],
            f"Literature priors: {kb.get('literature_priors', {})}",
        ]
    )


def get_structured_priors(reaction_type: str, problem_spec: dict[str, Any] | None = None) -> dict[str, Any]:
    kb = get_reaction_knowledge(reaction_type)
    if not kb:
        return {
            "warm_start_bias": {},
            "continuous_priors": {},
            "hard_constraints": [],
            "soft_priors": [],
            "known_interactions": [],
            "fallback_reason": f"No curated KB entry for reaction type '{reaction_type}'.",
        }

    variables = problem_spec.get("variables", []) if isinstance(problem_spec, dict) else []
    literature = kb.get("literature_priors", {})

    warm_start_bias = {}
    for variable in variables:
        names = [str(variable.get("name", "")).lower(), str(variable.get("description", "")).lower()]
        domain_labels = _domain_labels(variable)
        if not domain_labels:
            continue
        candidate_values = _match_prior_values(names, literature)
        if not candidate_values:
            continue
        weights: dict[str, float] = {}
        for label in domain_labels:
            label_norm = label.lower()
            if any(candidate.lower() in label_norm or label_norm in candidate.lower() for candidate in candidate_values):
                weights[label] = 1.0
            else:
                weights[label] = 0.15
        if any(weight > 0.15 for weight in weights.values()):
            warm_start_bias[variable["name"]] = _normalize_weights(weights)

    continuous_priors = {}
    for variable in variables:
        name = str(variable.get("name", "")).lower()
        if name == "temperature" and literature.get("optimal_temp_range"):
            continuous_priors[variable["name"]] = list(literature["optimal_temp_range"])
        if "conc" in name and literature.get("optimal_conc_range"):
            continuous_priors[variable["name"]] = list(literature["optimal_conc_range"])

    return {
        "warm_start_bias": warm_start_bias,
        "continuous_priors": continuous_priors,
        "hard_constraints": [
            {key: value for key, value in constraint.items() if key != "check"}
            for constraint in get_hard_constraints(reaction_type)
        ],
        "soft_priors": list(kb.get("key_factors", [])),
        "known_interactions": _known_interactions(reaction_type),
        "fallback_reason": None,
    }


def get_hard_constraints(reaction_type: str) -> list[dict[str, Any]]:
    reaction_key = str(reaction_type or "").upper()
    if reaction_key == "DAR":
        return list(DAR_HARD_CONSTRAINTS)
    return []


def _domain_labels(variable: dict[str, Any]) -> list[str]:
    labels = []
    for entry in variable.get("domain", []):
        if isinstance(entry, dict):
            labels.append(str(entry.get("label") or entry.get("name") or entry.get("value") or entry))
        else:
            labels.append(str(entry))
    return labels


def _match_prior_values(variable_markers: list[str], literature: dict[str, Any]) -> list[str]:
    if any("ligand" in marker for marker in variable_markers):
        return [str(item) for item in literature.get("best_ligands", [])]
    if any("base" in marker for marker in variable_markers):
        return [str(item) for item in literature.get("best_bases", [])]
    if any("solvent" in marker for marker in variable_markers):
        return [str(item) for item in literature.get("best_solvents", [])]
    return []


def _normalize_weights(weights: dict[str, float]) -> dict[str, float]:
    total = sum(weights.values()) or 1.0
    return {key: value / total for key, value in weights.items()}


def _known_interactions(reaction_type: str) -> list[dict[str, Any]]:
    if str(reaction_type or "").upper() == "DAR":
        return [
            {
                "variables": ["ligand", "base"],
                "type": "synergistic",
                "detail": "Bulky electron-rich phosphines can benefit from carbonate or carboxylate bases.",
                "source": "KB:DAR.key_factors",
            },
            {
                "variables": ["solvent", "temperature"],
                "type": "conditional",
                "detail": "Higher activation temperatures help only when solvent stability is maintained.",
                "source": "KB:DAR.key_factors",
            },
        ]
    return []


def _candidate_matches(
    candidate: dict[str, Any],
    field_hint: str,
    aliases: list[str] | None = None,
    smiles: list[str] | None = None,
) -> bool:
    normalized_aliases = [target.lower() for target in aliases or []]
    exact_smiles = {str(target).strip() for target in smiles or [] if str(target).strip()}
    for key, value in candidate.items():
        key_lower = str(key).lower()
        if field_hint not in key_lower:
            continue
        value_text = str(value).strip()
        if key_lower.endswith("_smiles"):
            if value_text in exact_smiles:
                return True
            continue
        value_lower = value_text.lower()
        if any(
            value_lower == target or target in value_lower or value_lower in target
            for target in normalized_aliases
        ):
            return True
    return False


def _coerce_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
