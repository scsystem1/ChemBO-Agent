"""LLM prompt templates for AutoBO surrogate evaluation and acquisition selection."""
from __future__ import annotations

import json
from typing import Any

from core.prompt_utils import compact_json


AF_SOURCE_LABELS: dict[str, str] = {
    "qlogei": "qLogEI",
    "qucb": "qUCB",
    "ts": "TS",
}


def _format_af_source_summary(item: dict[str, Any]) -> str:
    af_ranks = item.get("af_ranks", {}) if isinstance(item.get("af_ranks"), dict) else {}
    af_sources = item.get("af_sources", []) if isinstance(item.get("af_sources"), list) else []
    if not af_sources:
        return "none"
    parts: list[str] = []
    for af_key in af_sources:
        label = AF_SOURCE_LABELS.get(str(af_key), str(af_key))
        rank = af_ranks.get(af_key)
        if rank is None:
            parts.append(label)
        else:
            parts.append(f"{label}#{rank}")
    return ", ".join(parts) or "none"


def _build_candidate_text(candidates: list[dict[str, Any]], *, ensemble_mode: bool) -> str:
    lines: list[str] = []
    for item in candidates:
        base = (
            f"  #{item.get('id')}: {json.dumps(item.get('candidate', {}), ensure_ascii=False)}\n"
            f"      step={item.get('selection_step', 'n/a')}, "
            f"mode={item.get('selection_mode', 'n/a')}, "
            f"mu={_fmt_metric(item.get('predicted_value'))}, "
            f"sigma={_fmt_metric(item.get('uncertainty'))}"
        )
        if ensemble_mode:
            af_sources = _format_af_source_summary(item)
            consensus = item.get("af_consensus_count", 0)
            lines.append(
                base
                + f", sources=[{af_sources}], consensus={consensus}"
            )
        else:
            lines.append(
                base
                + f", acq={_fmt_metric(item.get('acquisition_value'), precision=6)}, "
                + f"raw_acq={_fmt_metric(item.get('acquisition_value_raw'), precision=6)}"
            )
    return "\n".join(lines) or "  None"


def build_surrogate_plausibility_prompt(
    reaction_context: dict[str, Any],
    top_observations: list[dict[str, Any]],
    bottom_observations: list[dict[str, Any]],
    eval_points: list[dict[str, Any]],
    knowledge_cards_text: str = "",
    memory_rules: list[dict[str, Any]] | None = None,
) -> str:
    memory_rules = memory_rules or []

    kb_section = f"\n{knowledge_cards_text}" if str(knowledge_cards_text or "").strip() else "\n[Active Knowledge Cards]\nNone available."

    memory_section = ""
    if memory_rules:
        rule_lines = [
            f"  - [{item.get('rule_type', '')}] {item.get('statement', '')} "
            f"(conf={float(item.get('confidence', 0.0)):.2f})"
            for item in memory_rules[:5]
        ]
        memory_section = "\n[Campaign Memory Rules]\n" + "\n".join(rule_lines)

    top_obs_text = "\n".join(
        f"  #{index + 1}: {json.dumps(item.get('candidate', {}), ensure_ascii=False)} -> "
        f"y={item.get('result', 'n/a')}"
        for index, item in enumerate(top_observations[:5])
    ) or "  None"
    bottom_obs_text = "\n".join(
        f"  #{index + 1}: {json.dumps(item.get('candidate', {}), ensure_ascii=False)} -> "
        f"y={item.get('result', 'n/a')}"
        for index, item in enumerate(bottom_observations[:3])
    ) or "  None"

    eval_parts = []
    for point in eval_points:
        prediction_lines = []
        for prediction_id, prediction in point.get("predictions", {}).items():
            prediction_lines.append(
                f"    Prediction {prediction_id}: "
                f"mu={float(prediction.get('mu', 0.0)):.4f}, "
                f"sigma={float(prediction.get('sigma', 0.0)):.4f}"
            )
        eval_parts.append(
            f"  [Point {point.get('point_id', '')}]\n"
            f"  Conditions: {point.get('candidate_description', '')}\n"
            + ("\n".join(prediction_lines) or "    No predictions")
        )
    eval_text = "\n\n".join(eval_parts) if eval_parts else "  None"

    return f"""You are evaluating the quality of surrogate model predictions for a chemical reaction optimization campaign.

[Reaction Context]
{compact_json(reaction_context)}
{kb_section}
{memory_section}

[Observed Data - Yield Anchors]
Top-performing conditions:
{top_obs_text}

Low-performing conditions:
{bottom_obs_text}

[Evaluation Points with Predictions]
Each point shows predictions from different models (anonymized as A/B/C/D/E/F).

{eval_text}

[Task]
For each (Point, Prediction) pair, rate plausibility 1-5:
  5 = fully consistent with chemical expectations; sigma is also reasonable
  4 = mostly consistent; minor concerns
  3 = uncertain; could be right or wrong
  2 = likely inconsistent with chemistry
  1 = strongly violates chemical intuition or sigma is clearly wrong

Return strict JSON:
{{
  "evaluations": [
    {{
      "point_id": "P1",
      "prediction_id": "A",
      "score": 4,
      "reasoning": "..."
    }}
  ]
}}"""


def build_acquisition_selection_prompt(
    reaction_context: dict[str, Any],
    top_observations: list[dict[str, Any]],
    bottom_observations: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    total_observations: int,
    knowledge_cards_text: str = "",
    memory_rules: list[dict[str, Any]] | None = None,
    active_hypotheses: list[dict[str, Any]] | None = None,
    stagnation_info: dict[str, Any] | None = None,
    ensemble_mode: bool = False,
) -> str:
    memory_rules = memory_rules or []
    active_hypotheses = active_hypotheses or []

    kb_section = f"\n{knowledge_cards_text}" if str(knowledge_cards_text or "").strip() else "\n[Active Knowledge Cards]\nNone available."

    memory_section = ""
    if memory_rules:
        rule_lines = [
            f"  - [{item.get('rule_type', '')}] {item.get('statement', '')} "
            f"(conf={float(item.get('confidence', 0.0)):.2f})"
            for item in memory_rules[:4]
        ]
        memory_section = "\n[Campaign Memory Rules]\n" + "\n".join(rule_lines)

    hypothesis_section = ""
    if active_hypotheses:
        hypothesis_lines = [
            f"  - [{item.get('id', '')}] {item.get('text', '')} "
            f"({item.get('status', '')}, {item.get('confidence', '')})"
            for item in active_hypotheses[:4]
        ]
        hypothesis_section = "\n[Active Hypotheses]\n" + "\n".join(hypothesis_lines)

    stagnation_section = ""
    if stagnation_info and bool(stagnation_info.get("is_stagnant")):
        top1_phrase = "the ensemble reference candidate" if ensemble_mode else "raw top-1 exploitation"
        stagnation_section = f"""
[Stagnation Alert]
No meaningful best-result improvement for {int(stagnation_info.get("stagnation_length", 0) or 0)} consecutive iterations.
Last improvement iteration: {stagnation_info.get("last_improvement_iteration", "unknown")}
Current best result: {stagnation_info.get("best_result", "n/a")}

The campaign may be trapped in a local optimum. Candidates with selection_mode="diversity_escape"
or selection_mode="ensemble_disagreement" were injected specifically to test unexplored or
model-disagreement regions. You must seriously consider these escape candidates. If you still
choose {top1_phrase}, explicitly justify why exploitation remains appropriate despite
the stagnation.
"""

    top_text = "\n".join(
        f"  Top-{index + 1}: {json.dumps(item.get('candidate', {}), ensure_ascii=False)} -> "
        f"y={item.get('result', 'n/a')}"
        for index, item in enumerate(top_observations[:3])
    ) or "  None"
    bottom_text = "\n".join(
        f"  Bottom-{index + 1}: {json.dumps(item.get('candidate', {}), ensure_ascii=False)} -> "
        f"y={item.get('result', 'n/a')}"
        for index, item in enumerate(bottom_observations[:3])
    ) or "  None"

    candidate_text = _build_candidate_text(candidates, ensemble_mode=ensemble_mode)

    if ensemble_mode:
        candidate_header = "[Candidates (ensemble shortlist; #1 is the ensemble reference candidate)]"
        af_guidance = """
[Acquisition Provenance]
The shortlist combines three acquisition strategies:
- qLogEI: prioritizes expected improvement over the current best; it is not simply "highest predicted mean".
- qUCB: optimistic scoring that explicitly rewards uncertainty through a mean-plus-uncertainty bonus.
- TS: a single posterior sample that can surface plausible high-value regions not favored by the expectation-based AFs.

Interpretation rules:
- AF source is a shortlist provenance hint, not proof; do not compare raw AF scores across different AFs.
- Candidates recommended by multiple AFs deserve extra attention because the model family reached partial consensus.
- A TS-only candidate is an exploration proposal from one posterior draw; treat it as informative but not automatically superior.
"""
        top1_guidance = (
            "- if you choose candidate #1, briefly explain why following the ensemble reference candidate is sufficient\n"
            "- if you do not choose candidate #1, you must explicitly compare your chosen candidate against candidate #1,\n"
            "  explain why overriding the ensemble reference is justified now, and label the override as exploration, mechanism validation,\n"
            "  or exploitation"
        )
    else:
        candidate_header = "[Candidates (qLogEI-inspired sequential shortlist; #1 is the raw acquisition top-1)]"
        af_guidance = ""
        top1_guidance = (
            "- if you choose candidate #1, briefly explain why following the raw acquisition top-1 is sufficient\n"
            "- if you do not choose candidate #1, you must explicitly compare your chosen candidate against candidate #1,\n"
            "  explain why overriding top-1 is justified now, and label the override as exploration, mechanism validation,\n"
            "  or exploitation"
        )

    return f"""You are selecting the single best experiment to run next in a chemical reaction optimization campaign.

[Reaction Context]
{compact_json(reaction_context)}
{kb_section}
{memory_section}
{hypothesis_section}
{stagnation_section}

[Observed Data Anchors]
{top_text}

{bottom_text}

Total experiments so far: {int(total_observations)}

{candidate_header}
{candidate_text}
{af_guidance}

[Task]
From chemical reasoning, select the ONE candidate most worth experimenting next.
Consider:
- chemical plausibility of the predicted yield under those conditions
- whether the model predictions (mu, sigma) align with chemistry intuition
- information gain and hypothesis alignment
- active knowledge cards; cite card IDs in reasoning when they influence your choice
{top1_guidance}

Return strict JSON:
{{
  "selected_id": 1,
  "reasoning": "...",
  "comparison_to_top1": "...",
  "selection_mode": "top1_follow|non_top1_override"
}}"""


def build_pure_reasoning_selection_prompt(
    reaction_context: dict[str, Any],
    top_observations: list[dict[str, Any]],
    bottom_observations: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    total_observations: int,
    knowledge_cards_text: str = "",
    memory_rules: list[dict[str, Any]] | None = None,
    active_hypotheses: list[dict[str, Any]] | None = None,
    stagnation_info: dict[str, Any] | None = None,
) -> str:
    memory_rules = memory_rules or []
    active_hypotheses = active_hypotheses or []

    kb_section = f"\n{knowledge_cards_text}" if str(knowledge_cards_text or "").strip() else "\n[Active Knowledge Cards]\nNone available."

    memory_section = ""
    if memory_rules:
        rule_lines = [
            f"  - [{item.get('rule_type', '')}] {item.get('statement', '')} "
            f"(conf={float(item.get('confidence', 0.0)):.2f})"
            for item in memory_rules[:4]
        ]
        memory_section = "\n[Campaign Memory Rules]\n" + "\n".join(rule_lines)

    hypothesis_section = ""
    if active_hypotheses:
        hypothesis_lines = [
            f"  - [{item.get('id', '')}] {item.get('text', '')} "
            f"({item.get('status', '')}, {item.get('confidence', '')})"
            for item in active_hypotheses[:4]
        ]
        hypothesis_section = "\n[Active Hypotheses]\n" + "\n".join(hypothesis_lines)

    stagnation_section = ""
    if stagnation_info and bool(stagnation_info.get("is_stagnant")):
        stagnation_section = f"""
[Stagnation Alert]
No meaningful best-result improvement for {int(stagnation_info.get("stagnation_length", 0) or 0)} consecutive iterations.
Last improvement iteration: {stagnation_info.get("last_improvement_iteration", "unknown")}
Current best result: {stagnation_info.get("best_result", "n/a")}
"""

    top_text = "\n".join(
        f"  Top-{index + 1}: {json.dumps(item.get('candidate', {}), ensure_ascii=False)} -> "
        f"y={item.get('result', 'n/a')}"
        for index, item in enumerate(top_observations[:3])
    ) or "  None"
    bottom_text = "\n".join(
        f"  Bottom-{index + 1}: {json.dumps(item.get('candidate', {}), ensure_ascii=False)} -> "
        f"y={item.get('result', 'n/a')}"
        for index, item in enumerate(bottom_observations[:3])
    ) or "  None"

    candidate_text = "\n".join(
        f"  #{item.get('id')}: {json.dumps(item.get('candidate', {}), ensure_ascii=False)}"
        for item in candidates
    ) or "  None"

    return f"""You are selecting the single best experiment to run next in a chemical reaction optimization campaign.

[Reaction Context]
{compact_json(reaction_context)}
{kb_section}
{memory_section}
{hypothesis_section}
{stagnation_section}

[Observed Data Anchors]
{top_text}

{bottom_text}

Total experiments so far: {int(total_observations)}

[Candidate Pool]
The following candidates are legal options for the next experiment. The IDs are only labels.
There are no surrogate predictions, no acquisition scores, and no BO ranking in this mode.
Choose exactly one candidate based only on chemical reasoning, hypothesis testing value,
knowledge cards, and campaign memory.

{candidate_text}

[Task]
Select the ONE candidate most worth experimenting next.
Consider:
- chemical plausibility under the current campaign context
- whether it tests or refines the most important active hypotheses
- whether it adds useful information beyond the current observations
- whether any active knowledge cards or campaign-memory rules support or caution against it

Return strict JSON:
{{
  "selected_id": 1,
  "reasoning": "...",
  "hypothesis_alignment": "...",
  "information_value": "...",
  "concerns": "...",
  "confidence": 0.75
}}"""


def build_pure_reasoning_space_selection_prompt(
    reaction_context: dict[str, Any],
    top_observations: list[dict[str, Any]],
    bottom_observations: list[dict[str, Any]],
    total_observations: int,
    space_description: str,
    output_schema: str,
    knowledge_cards_text: str = "",
    memory_rules: list[dict[str, Any]] | None = None,
    active_hypotheses: list[dict[str, Any]] | None = None,
    stagnation_info: dict[str, Any] | None = None,
    validation_feedback: str = "",
) -> str:
    memory_rules = memory_rules or []
    active_hypotheses = active_hypotheses or []

    kb_section = f"\n{knowledge_cards_text}" if str(knowledge_cards_text or "").strip() else "\n[Active Knowledge Cards]\nNone available."

    memory_section = ""
    if memory_rules:
        rule_lines = [
            f"  - [{item.get('rule_type', '')}] {item.get('statement', '')} "
            f"(conf={float(item.get('confidence', 0.0)):.2f})"
            for item in memory_rules[:4]
        ]
        memory_section = "\n[Campaign Memory Rules]\n" + "\n".join(rule_lines)

    hypothesis_section = ""
    if active_hypotheses:
        hypothesis_lines = [
            f"  - [{item.get('id', '')}] {item.get('text', '')} "
            f"({item.get('status', '')}, {item.get('confidence', '')})"
            for item in active_hypotheses[:4]
        ]
        hypothesis_section = "\n[Active Hypotheses]\n" + "\n".join(hypothesis_lines)

    stagnation_section = ""
    if stagnation_info and bool(stagnation_info.get("is_stagnant")):
        stagnation_section = f"""
[Stagnation Alert]
No meaningful best-result improvement for {int(stagnation_info.get("stagnation_length", 0) or 0)} consecutive iterations.
Last improvement iteration: {stagnation_info.get("last_improvement_iteration", "unknown")}
Current best result: {stagnation_info.get("best_result", "n/a")}
"""

    validation_section = ""
    if str(validation_feedback or "").strip():
        validation_section = f"""
[Validation Feedback]
{validation_feedback}
Use this feedback to correct the next answer. Return a new valid recommendation only.
"""

    top_text = "\n".join(
        f"  Top-{index + 1}: {json.dumps(item.get('candidate', {}), ensure_ascii=False)} -> "
        f"y={item.get('result', 'n/a')}"
        for index, item in enumerate(top_observations[:3])
    ) or "  None"
    bottom_text = "\n".join(
        f"  Bottom-{index + 1}: {json.dumps(item.get('candidate', {}), ensure_ascii=False)} -> "
        f"y={item.get('result', 'n/a')}"
        for index, item in enumerate(bottom_observations[:3])
    ) or "  None"

    return f"""You are selecting the single best experiment to run next in a chemical reaction optimization campaign.

[Reaction Context]
{compact_json(reaction_context)}
{kb_section}
{memory_section}
{hypothesis_section}
{stagnation_section}
{validation_section}

[Observed Data Anchors]
{top_text}

{bottom_text}

Total experiments so far: {int(total_observations)}

[Structured Search Space]
Choose the next experiment directly from the structured legal search space below.
There are no surrogate predictions, no acquisition scores, and no BO ranking in this mode.
If categorical options are represented by IDs, return those IDs exactly.

{space_description}

[Task]
Select the ONE next experiment that is most worth running.
Consider:
- chemical plausibility under the current campaign context
- whether it tests or refines the most important active hypotheses
- whether it adds useful information beyond the current observations
- whether any active knowledge cards or campaign-memory rules support or caution against it
- the recommendation must be legal and unseen

Return strict JSON:
{output_schema}"""


def build_warm_start_structured_seed_prompt(
    reaction_context: dict[str, Any],
    active_hypotheses: list[dict[str, Any]],
    warm_start_target: int,
    direct_seed_target: int,
    space_description: str,
    single_experiment_schema: str,
    knowledge_cards_text: str = "",
) -> str:
    active_hypotheses = active_hypotheses or []

    kb_section = f"\n{knowledge_cards_text}" if str(knowledge_cards_text or "").strip() else "\n[Active Knowledge Cards]\nNone available."

    hypothesis_section = ""
    if active_hypotheses:
        hypothesis_lines = [
            f"  - [{item.get('id', '')}] {item.get('text', '')} "
            f"({item.get('status', '')}, {item.get('confidence', '')})"
            for item in active_hypotheses[:4]
        ]
        hypothesis_section = "\n[Active Hypotheses]\n" + "\n".join(hypothesis_lines)

    return f"""You are planning the first part of a warm-start experimental set for a chemical reaction optimization campaign.

[Reaction Context]
{compact_json(reaction_context)}
{kb_section}
{hypothesis_section}

[Warm-Start Objective]
The full warm-start set will contain {int(warm_start_target)} experiments.
You are responsible only for the first {int(direct_seed_target)} direct warm-start seeds.
These seeds should be the strongest high-conviction experiments in the full legal search space.
The remaining {max(int(warm_start_target) - int(direct_seed_target), 0)} warm-start slots will be chosen later by
a separate algorithm plus additional warm-start guidance to improve coverage and diversity.

[Structured Search Space]
The search space below is a structured representation of the true legal full space, not a sampled shortlist.
If categorical values are represented by IDs, return those IDs exactly.

{space_description}

[Task]
Select exactly {int(direct_seed_target)} distinct unseen experiments for the warm start prefix.
Focus on:
- the chemically strongest candidates under your world knowledge and the current campaign context
- alignment with the most important active hypotheses and knowledge cards
- slight diversity among the chosen seeds when several candidates appear similarly strong

Do NOT use these first {int(direct_seed_target)} picks to force broad coverage of the space.
The remaining warm-start slots will handle additional coverage and diversity later.

Each item in "selected_experiments" must follow this single-experiment schema:
{single_experiment_schema}

Return strict JSON:
{{
  "strategy_summary": "...",
  "selected_experiments": [
    {{
      "reasoning": "...",
      "confidence": 0.75
    }}
  ]
}}"""


def _fmt_metric(value: Any, precision: int = 4) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.{precision}f}"
    except Exception:
        return "n/a"
