"""
ChemBO Agent knowledge base and structured prior helpers.
"""
from __future__ import annotations

from typing import Any


REACTION_KNOWLEDGE: dict[str, dict[str, Any]] = {
    "DAR": {
        "full_name": "Direct Arylation Reaction",
        "mechanism": "Pd-catalyzed C-H activation / concerted metalation-deprotonation (CMD)",
        "prior_granularity": "coarse",
        "key_factors": [
            "Ligand structure can strongly change both reactivity and site selectivity, so chemically diverse ligand environments are worth distinguishing early.",
            "Bases that can assist deprotonation are often more informative than treating base as an interchangeable additive.",
            "Solvent polarity and coordinating ability can shift both catalyst stability and C-H activation efficiency.",
            "Moderately elevated temperature is often needed, but temperature should be considered together with solvent and catalyst robustness.",
        ],
        "common_pitfalls": [
            "Aggressive conditions can accelerate solvent or catalyst decomposition instead of improving turnover.",
            "Very mild conditions may fail to activate the catalytic cycle at all.",
            "Strongly coordinating impurities or additives can suppress the catalyst.",
        ],
        "literature_priors": {
            "ligand_guidance": "Start from chemically distinct ligand families rather than assuming one named ligand is universally best.",
            "base_guidance": "Carbonate-like and carboxylate-like bases are reasonable anchors, but base effects can be substrate-specific.",
            "solvent_guidance": "Polar aprotic media are common starting points, with a less polar solvent useful as a mechanistic contrast.",
            "process_guidance": "Intermediate concentration and moderate-to-high temperature are often informative first probes, but not fixed optima.",
        },
    },
    "OCM": {
        "full_name": "Oxidative Coupling of Methane",
        "mechanism": (
            "Heterogeneous surface activation followed by gas-phase radical coupling. "
            "Surface oxygen species activate CH4 to methyl radicals, methyl radicals couple to form C2 products, "
            "and over-oxidation to COx is the main competing loss pathway. "
            "Redox-active sites, promoter effects, and selector phases often interact to determine whether activation is productive or over-oxidative."
        ),
        "prior_granularity": "coarse",
        "key_factors": [
            "Catalyst composition and feed composition need to be tuned together because methane activation and over-oxidation are tightly coupled.",
            "The three catalyst roles often interact: a redox-active component, a promoter component, and a selector-like component can each shift the C2/COx balance.",
            "Support identity is a first-order design choice because it can reshape the active phase ensemble rather than acting as a passive carrier.",
            "Methane-rich feeds often protect C2 selectivity, while oxygen-rich feeds tend to increase deep oxidation.",
            "High temperature is usually required for measurable activity, but the best operating region is typically a compromise between conversion and selectivity.",
            "Contact time and temperature should be explored jointly because severe combinations can raise conversion while hurting C2 yield.",
        ],
        "common_pitfalls": [
            "Oxygen-rich or otherwise severe conditions can rapidly collapse C2 selectivity through deep oxidation.",
            "Catalyst compositions missing too many functional roles often behave like weak baselines rather than optimized systems.",
            "High temperature and long contact time can improve conversion while eroding C2 yield.",
        ],
        "literature_priors": {
            "composition_guidance": "Sample families that span different redox metals, promoters, and selector phases instead of anchoring on one benchmark composition.",
            "support_guidance": "Support effects can dominate composition trends, so contrasting support families is often more informative than over-optimizing one support too early.",
            "feed_guidance": "Prefer methane-rich but not oxygen-starved feeds when probing the selectivity-conversion tradeoff.",
            "process_guidance": "Use temperature and contact time as coarse operating-window variables, not as exact targets copied from prior art.",
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
        "reason": "Avoid overly hot DMAc-like solvent conditions because solvent stability can become limiting.",
        "source": "KB:DAR.pitfalls.solvent_decomposition",
        "check": lambda candidate: not (
            _candidate_matches(candidate, "solvent", aliases=["DMAc"], smiles=["CC(N(C)C)=O"])
            and _coerce_float(candidate.get("temperature"), 0.0) > 160.0
        ),
    },
    {
        "name": "toluene_min_temp",
        "reason": "Very mild toluene-like solvent conditions are often under-activated for DAR.",
        "source": "KB:DAR.pitfalls.toluene_low_temp",
        "check": lambda candidate: not (
            _candidate_matches(candidate, "solvent", aliases=["toluene"], smiles=["CC1=CC=C(C)C=C1"])
            and _coerce_float(candidate.get("temperature"), 999.0) < 100.0
        ),
    },
]


OCM_HARD_CONSTRAINTS = [
    {
        "name": "ocm_ch4_o2_ratio_lower_bound",
        "reason": "Avoid oxygen-rich feed combinations that usually destroy C2 selectivity.",
        "source": "KB:OCM.key_factors.ch4_o2_ratio",
        "check": lambda candidate: _ocm_ch4_o2_ratio(candidate) >= 2.0,
    },
    {
        "name": "ocm_ch4_o2_ratio_upper_bound",
        "reason": "Avoid methane-rich feed combinations that fall outside the intended OCM operating window.",
        "source": "KB:OCM.key_factors.ch4_o2_ratio",
        "check": lambda candidate: _ocm_ch4_o2_ratio(candidate) <= 7.0,
    },
    {
        "name": "ocm_all_metals_vacant",
        "reason": "All-vacant M1/M2/M3 compositions are degenerate support-only conditions and should not be optimized as catalysts.",
        "source": "KB:OCM.common_pitfalls.degenerate_composition",
        "check": lambda candidate: not (
            str(candidate.get("M1", "")).strip() == "n.a."
            and str(candidate.get("M2", "")).strip() == "n.a."
            and str(candidate.get("M3", "")).strip() == "n.a."
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
    reasoning_aids = _format_reasoning_aids(kb.get("literature_priors", {}))
    return "\n".join(
        [
            f"Reaction: {kb['full_name']}",
            f"Mechanism: {kb['mechanism']}",
            "Key factors:",
            *[f"- {item}" for item in kb.get("key_factors", [])],
            "Common pitfalls:",
            *[f"- {item}" for item in kb.get("common_pitfalls", [])],
            "Reasoning aids:",
            *reasoning_aids,
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
    prior_granularity = str(kb.get("prior_granularity", "specific")).lower()
    allow_exact_bias = prior_granularity == "specific"

    warm_start_bias = {}
    if allow_exact_bias:
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
    if allow_exact_bias:
        for variable in variables:
            name = str(variable.get("name", "")).lower()
            if str(variable.get("type", "categorical")).lower() != "continuous":
                continue
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
        "prior_granularity": prior_granularity,
        "fallback_reason": None,
    }


def get_hard_constraints(reaction_type: str) -> list[dict[str, Any]]:
    reaction_key = str(reaction_type or "").upper()
    if reaction_key == "DAR":
        return list(DAR_HARD_CONSTRAINTS)
    if reaction_key == "OCM":
        return list(OCM_HARD_CONSTRAINTS)
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
        return _as_text_list(literature.get("best_ligands", []))
    if any("base" in marker for marker in variable_markers):
        return _as_text_list(literature.get("best_bases", []))
    if any("solvent" in marker for marker in variable_markers):
        return _as_text_list(literature.get("best_solvents", []))
    if any(marker.strip() == "m1" for marker in variable_markers):
        return _as_text_list(literature.get("best_M1", []))
    if any(marker.strip() == "m2" for marker in variable_markers):
        return _as_text_list(literature.get("best_M2", []))
    if any(marker.strip() == "m3" for marker in variable_markers):
        return _as_text_list(literature.get("best_M3", []))
    if any("support" in marker for marker in variable_markers):
        return _as_text_list(literature.get("best_supports", []))
    if any(marker.strip() == "temp" or marker.strip() == "temperature" for marker in variable_markers):
        return _as_text_list(literature.get("best_temperatures", []))
    if any(marker.strip() == "ct" or "contact time" in marker for marker in variable_markers):
        return _as_text_list(literature.get("best_contact_times", []))
    return []


def _normalize_weights(weights: dict[str, float]) -> dict[str, float]:
    total = sum(weights.values()) or 1.0
    return {key: value / total for key, value in weights.items()}


def _format_reasoning_aids(priors: dict[str, Any]) -> list[str]:
    if not priors:
        return ["- No curated reasoning aids available."]
    lines = []
    for key, value in priors.items():
        label = str(key).replace("_", " ").strip().capitalize()
        if isinstance(value, (list, tuple, set)):
            rendered = ", ".join(str(item) for item in value)
        else:
            rendered = str(value)
        lines.append(f"- {label}: {rendered}")
    return lines


def _as_text_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value]
    return []


def _known_interactions(reaction_type: str) -> list[dict[str, Any]]:
    if str(reaction_type or "").upper() == "DAR":
        return [
            {
                "variables": ["ligand", "base"],
                "type": "synergistic",
                "detail": "Ligand sterics/electronics and base identity often interact through the C-H activation step.",
                "source": "KB:DAR.key_factors",
            },
            {
                "variables": ["solvent", "temperature"],
                "type": "conditional",
                "detail": "Higher activation temperatures help only when solvent stability is maintained.",
                "source": "KB:DAR.key_factors",
            },
        ]
    if str(reaction_type or "").upper() == "OCM":
        return [
            {
                "variables": ["M1", "M2", "M3"],
                "type": "synergistic",
                "detail": "Redox-active, promoter, and selector roles should be considered jointly because no single component usually determines OCM performance alone.",
                "source": "KB:OCM.mechanism",
            },
            {
                "variables": ["M2", "Support"],
                "type": "conditional",
                "detail": "Promoter effects are often support-dependent because support chemistry can reshape phase stability and oxygen handling.",
                "source": "KB:OCM.key_factors",
            },
            {
                "variables": ["CH4_flow", "O2_flow"],
                "type": "ratio_critical",
                "detail": "CH4_flow and O2_flow should be tuned jointly because feed ratio often dominates the OCM selectivity-conversion tradeoff.",
                "source": "KB:OCM.key_factors",
            },
            {
                "variables": ["Temp", "CT"],
                "type": "tradeoff",
                "detail": "Higher temperature and longer contact time can increase conversion but often at the cost of deeper oxidation and lower C2+ selectivity.",
                "source": "KB:OCM.common_pitfalls",
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


def _ocm_ch4_o2_ratio(candidate: dict[str, Any]) -> float:
    ch4 = _coerce_float(candidate.get("CH4_flow"), default=0.0)
    o2 = _coerce_float(candidate.get("O2_flow"), default=0.0)
    if o2 <= 0.0:
        return 999.0
    return ch4 / o2
