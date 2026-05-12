"""
Prompt builders for lightweight knowledge priors and evidence search.
"""
from __future__ import annotations

import json
from typing import Any


def build_prior_writer_prompt(
    problem_spec: dict[str, Any],
    profile: str,
    validation_feedback: str = "",
) -> tuple[str, str]:
    """Return system and user prompts for internal-prior card generation."""
    reaction = problem_spec.get("reaction", {}) if isinstance(problem_spec.get("reaction"), dict) else {}
    variables = [item for item in problem_spec.get("variables", []) if isinstance(item, dict)]
    reaction_context = {
        "family": str(reaction.get("family") or problem_spec.get("reaction_type") or "").strip().upper(),
        "canonical_name": str(reaction.get("canonical_name") or problem_spec.get("reaction_type") or "").strip(),
        "aliases": [str(item).strip() for item in reaction.get("aliases", []) if str(item).strip()][:6],
        "description": str(problem_spec.get("description") or problem_spec.get("raw_description") or "").strip()[:700],
        "substrates": [
            {
                "role": str(item.get("role") or "").strip(),
                "name": str(item.get("name") or "").strip(),
            }
            for item in reaction.get("substrates", [])
            if isinstance(item, dict)
        ][:8],
        "known_fixed_context": [
            {
                "role": str(item.get("role") or "").strip(),
                "value": str(item.get("value") or "").strip(),
            }
            for item in reaction.get("known_fixed_context", [])
            if isinstance(item, dict)
        ][:10],
        "constraints": [str(item).strip() for item in problem_spec.get("constraints", []) if str(item).strip()][:8],
    }
    variable_context = [_variable_prompt_payload(variable) for variable in variables]
    variable_names = [item["name"] for item in variable_context if item.get("name")]

    profile_guidance = {
        "homogeneous_cross_coupling": (
            "Use cross-coupling chemistry. Prefer mechanistic priors around oxidative addition, transmetalation, "
            "and reductive elimination; ligand bite angle/electron richness/sterics; base-solvent interactions; "
            "and failure modes such as catalyst deactivation, poor oxidative addition, or incompatible bases."
        ),
        "heterogeneous_catalysis": (
            "Use heterogeneous catalysis chemistry. Prefer priors about active sites, oxygen species, promoter/support "
            "effects, metal redox and acid-base properties, operating severity, and failure modes such as over-oxidation, "
            "sintering, coking, or transport limitations."
        ),
        "generic_fallback": (
            "Do not force a named reaction paradigm. Build conservative priors from the reaction description and variable "
            "roles, and mark more cards as needing external evidence when the claim is reaction-family specific."
        ),
    }.get(str(profile or "generic_fallback"), "")

    system_prompt = (
        "You write compact chemistry priors for a Bayesian optimization agent. "
        "Use only broadly known chemistry and the structured campaign specification. "
        "Return strict JSON only. "
        "Do not invent numerical outcomes (yield %, conversion %, temperatures, selectivity numbers), authors, DOIs, or specific paper claims. "
        "Each card must be one actionable English sentence, 10-50 words. "
        "Generate 12-16 cards with these quotas: "
        "mechanism 1-2 (how the reaction proceeds, what controls selectivity or rate), "
        "reagent_property 2-3 (specific properties of variables that affect outcomes), "
        "operating_window 1-2 (what ranges or combinations are typically productive), "
        "failure_mode 1-2 (conditions known to cause poor results or catalyst deactivation), "
        "interaction 1-2 (synergistic or antagonistic effects between variables), "
        "analogy 0-1 (conservative analogy to a known reaction class), "
        "hypothesis at least 4 (specific testable predictions for THIS campaign). "
        "Every card must include warm_start in actionable_for because these priors will directly guide initial experiment selection. "
        "Allowed card_type values: mechanism, reagent_property, operating_window, failure_mode, interaction, analogy, hypothesis. "
        "Allowed scope values: target, campaign, analogous, general. "
        "Except for mechanism and hypothesis cards, targets must be exact variable names from EXACT_VARIABLE_NAMES. "
        "For hypothesis cards, targets may be empty and testable_prediction is required; make it 15-40 words describing what observation would support or refute the claim. "
        "Use needs_external_evidence=true only when the claim is genuinely uncertain or reaction-family-specific and could affect candidate selection. "
        "CONFIDENCE CALIBRATION: assign confidence strictly by evidence strength. "
        "Use 0.55-0.60 for textbook-level facts broadly accepted across multiple sources; reserve this tier for mechanism and well-documented failure_mode cards. "
        "Use 0.40-0.54 for domain rules of thumb that are likely correct but have reaction-specific exceptions, such as operating_window and generic failure_mode cards. "
        "Use 0.30-0.39 for plausible claims with limited precedent; in this range, strongly prefer a hypothesis card with testable_prediction. "
        "Do not default to 0.45 for everything; spread confidence across the full range based on actual certainty. "
        "DOWNGRADE RULE: claims about a specific variable value being better or worse, interactions between two specific values, "
        "or analogies to another system must be card_type hypothesis with testable_prediction, not reagent_property or interaction. "
        "Use reagent_property or interaction only for broad claims expected to hold across the reaction class regardless of this specific substrate or system."
    )
    feedback_block = f"\n\nVALIDATION_FEEDBACK:\n{validation_feedback}" if validation_feedback else ""
    user_prompt = (
        f"PROFILE: {profile}\n"
        f"PROFILE_GUIDANCE: {profile_guidance}\n\n"
        f"REACTION_CONTEXT:\n{_json(reaction_context)}\n\n"
        f"VARIABLES_WITH_ROLES:\n{_json(variable_context)}\n\n"
        f"EXACT_VARIABLE_NAMES:\n{_json(variable_names)}\n\n"
        "Return JSON with shape:\n"
        "{\n"
        '  "cards": [\n'
        "    {\n"
        '      "text": "...",\n'
        '      "card_type": "mechanism|reagent_property|operating_window|failure_mode|interaction|analogy|hypothesis",\n'
        '      "scope": "target|campaign|analogous|general",\n'
        '      "confidence": 0.0,\n'
        '      "targets": ["exact_variable_name"],\n'
        '      "actionable_for": ["hypothesis_generation", "warm_start", "select_candidate", "run_bo_iteration", "result_interpretation"],\n'
        '      "testable_prediction": "Only for hypothesis cards: what observation would confirm or refute this.",\n'
        '      "needs_external_evidence": false,\n'
        '      "evidence_question": ""\n'
        "    }\n"
        "  ],\n"
        '  "global_notes": ""\n'
        "}"
        f"{feedback_block}"
    )
    return system_prompt, user_prompt


def build_evidence_query_prompt(question: str, context: str, problem_spec: dict[str, Any]) -> tuple[str, str]:
    reaction = problem_spec.get("reaction", {}) if isinstance(problem_spec.get("reaction"), dict) else {}
    system_prompt = (
        "Rewrite a chemistry evidence need into concise web search queries. "
        "Return strict JSON only with queries and key_terms. Prefer specific reaction/reagent terms."
    )
    user_prompt = (
        f"QUESTION:\n{str(question or '').strip()}\n\n"
        f"CAMPAIGN_CONTEXT:\n{str(context or '').strip()}\n\n"
        f"REACTION_FAMILY: {str(reaction.get('family') or problem_spec.get('reaction_type') or '').strip()}\n\n"
        'Return {"queries": ["query one", "query two"], "key_terms": ["term"]}.'
    )
    return system_prompt, user_prompt


def build_evidence_compression_prompt(
    question: str,
    context: str,
    chunks: list[dict[str, Any]],
) -> tuple[str, str]:
    system_prompt = (
        "You compress web evidence for a chemistry optimization agent. "
        "Use only the supplied sanitized snippets. Do not report numerical outcomes unless they are already redacted. "
        "Return strict JSON only."
    )
    user_prompt = (
        f"QUESTION:\n{str(question or '').strip()}\n\n"
        f"WHY_THIS_WAS_ASKED:\n{str(context or '').strip()}\n\n"
        f"SANITIZED_SNIPPETS:\n{_json(chunks)}\n\n"
        "Return JSON with shape:\n"
        "{\n"
        '  "answers": [{"answer": "one or two sentences", "relevance": 0.0, "citation": "short citation or title", "url": "https://..."}],\n'
        '  "best_answer": "1-3 sentence synthesis",\n'
        '  "notes": []\n'
        "}"
    )
    return system_prompt, user_prompt


def _variable_prompt_payload(variable: dict[str, Any]) -> dict[str, Any]:
    domain = variable.get("domain", [])
    preview = domain[:8] if isinstance(domain, list) else []
    return {
        "name": str(variable.get("name") or "").strip(),
        "role": str(variable.get("role") or "").strip(),
        "type": str(variable.get("type") or "categorical").strip(),
        "domain_preview": preview,
        "domain_size": len(domain) if isinstance(domain, list) else 0,
        "unit": str(variable.get("unit") or "").strip(),
        "description": str(variable.get("description") or "").strip()[:220],
    }


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


__all__ = [
    "build_evidence_compression_prompt",
    "build_evidence_query_prompt",
    "build_prior_writer_prompt",
]
