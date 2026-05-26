"""LLM prompt templates for AutoBO surrogate evaluation and acquisition selection."""
from __future__ import annotations

import json
from typing import Any

from core.prompt_utils import compact_json


AF_SOURCE_LABELS: dict[str, str] = {
    "qlogei": "qLogEI",
    "qucb": "qUCB",
    "ts": "TS",
    "coverage_qlogei": "coverage-qLogEI",
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


def _format_coverage_target_summary(item: dict[str, Any]) -> str:
    targets = item.get("coverage_targets", [])
    if not isinstance(targets, list) or not targets:
        return ""
    parts: list[str] = []
    for target in targets:
        if not isinstance(target, dict):
            continue
        variable = str(target.get("variable") or "").strip()
        value = target.get("value")
        if not variable:
            continue
        unseen_text = "this categorical value has never been tested before" if bool(target.get("unseen")) else "coverage target"
        selected_text = "LLM-selected" if bool(target.get("selected_by_llm")) else "coverage-selected"
        parts.append(f"{selected_text} coverage target: {variable}={value}; {unseen_text}")
    return "; ".join(parts)


def _format_unseen_bo_value_summary(item: dict[str, Any]) -> str:
    if isinstance(item.get("coverage_targets"), list) and item.get("coverage_targets"):
        return ""
    values = item.get("unseen_categorical_values", [])
    if not isinstance(values, list) or not values:
        return ""
    parts: list[str] = []
    for value_info in values:
        if not isinstance(value_info, dict):
            continue
        variable = str(value_info.get("variable") or "").strip()
        if not variable:
            continue
        parts.append(f"{variable}={value_info.get('value')}")
    if not parts:
        return ""
    return "BO-proposed unseen categorical value(s): " + "; ".join(parts)


def _build_candidate_text(
    candidates: list[dict[str, Any]],
    *,
    ensemble_mode: bool,
    include_coverage_annotations: bool = True,
) -> str:
    lines: list[str] = []
    shortlist_size = max(1, len(candidates))
    for item in candidates:
        sigma_rank = item.get("sigma_rank")
        sigma_rank_text = (
            f"{int(sigma_rank)}/{shortlist_size}"
            if isinstance(sigma_rank, int) or (isinstance(sigma_rank, float) and float(sigma_rank).is_integer())
            else "n/a"
        )
        value_attempt_counts = item.get("value_attempt_counts", {})
        if not isinstance(value_attempt_counts, dict):
            value_attempt_counts = {}
        explore_summary = (
            f"explore={{sigma_rank={sigma_rank_text}, "
            f"changed_vs_best={item.get('changed_vs_best', 'n/a')}, "
            f"value_attempt_counts={json.dumps(value_attempt_counts, ensure_ascii=False, sort_keys=True)}}}"
        )
        base = (
            f"  #{item.get('id')}: {json.dumps(item.get('candidate', {}), ensure_ascii=False)}\n"
            f"      step={item.get('selection_step', 'n/a')}, "
            f"mode={item.get('selection_mode', 'n/a')}, "
            f"mu={_fmt_metric(item.get('predicted_value'))}, "
            f"sigma={_fmt_metric(item.get('uncertainty'))}, "
            f"{explore_summary}"
        )
        if include_coverage_annotations:
            coverage_summary = _format_coverage_target_summary(item)
            if coverage_summary:
                base += f"\n      {coverage_summary}"
            unseen_bo_summary = _format_unseen_bo_value_summary(item)
            if unseen_bo_summary:
                base += f"\n      {unseen_bo_summary}"
        if ensemble_mode:
            af_sources = _format_af_source_summary(item)
            consensus = item.get("af_consensus_count", 0)
            reference_score = _fmt_metric(item.get("ensemble_reference_score"), precision=4)
            rank_score = _fmt_metric(item.get("ensemble_weighted_rank_score"), precision=4)
            diversity_bonus = _fmt_metric(item.get("ensemble_diversity_bonus"), precision=4)
            lines.append(
                base
                + f", sources=[{af_sources}], consensus={consensus}, "
                + f"ensemble_score={reference_score}, weighted_rank={rank_score}, diversity_bonus={diversity_bonus}"
            )
        else:
            lines.append(
                base
                + f", acq={_fmt_metric(item.get('acquisition_value'), precision=6)}, "
                + f"raw_acq={_fmt_metric(item.get('acquisition_value_raw'), precision=6)}"
            )
    return "\n".join(lines) or "  None"


def build_unseen_category_coverage_prompt(
    reaction_context: dict[str, Any],
    top_observations: list[dict[str, Any]],
    bottom_observations: list[dict[str, Any]],
    recent_observations: list[dict[str, Any]],
    unseen_options: dict[str, list[dict[str, Any]]],
    total_observations: int,
    coverage_slots: int,
    knowledge_cards_text: str = "",
    memory_rules: list[dict[str, Any]] | None = None,
    active_hypotheses: list[dict[str, Any]] | None = None,
) -> str:
    memory_rules = memory_rules or []
    active_hypotheses = active_hypotheses or []
    kb_section = f"\n{knowledge_cards_text}" if str(knowledge_cards_text or "").strip() else "\n[Active Knowledge Cards]\nNone available."

    memory_section = ""
    if memory_rules:
        rule_lines = [
            f"  - [{item.get('id', '')}|{item.get('rule_type', '')}] {item.get('statement', '')} "
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
    recent_text = "\n".join(
        f"  Iter-{item.get('iteration', '?')}: {json.dumps(item.get('candidate', {}), ensure_ascii=False)} -> "
        f"y={item.get('result', 'n/a')}"
        for item in recent_observations[-6:]
    ) or "  None"

    return f"""You are choosing categorical values for early exploration in a chemical reaction optimization campaign.

[Reaction Context]
{compact_json(reaction_context)}
{kb_section}
{memory_section}
{hypothesis_section}

[Observed Data Anchors]
{top_text}

{bottom_text}

[Recent Observations]
{recent_text}

Total experiments so far: {int(total_observations)}

[Never-Tested Categorical Options]
Every value listed below has zero prior observations in this campaign. These are not candidates yet; they are categorical values that can be used to generate candidates.
{compact_json(unseen_options)}

[Task]
Choose exactly {max(int(coverage_slots), 1)} categorical values most worth exploring now, unless fewer listed values remain.
Prefer values that are chemically plausible and likely to teach something useful if they fail.
Only choose exact variable/value pairs from [Never-Tested Categorical Options].

Return strict JSON:
{{
  "targets": [
    {{
      "variable": "ligand_SMILES",
      "value": "...",
      "reasoning": "..."
    }}
  ]
}}"""


def _candidate_choice_text(count: int) -> str:
    total = max(int(count), 1)
    labels = [f"#{index}" for index in range(1, total + 1)]
    if len(labels) == 1:
        return labels[0]
    if len(labels) == 2:
        return f"{labels[0]} or {labels[1]}"
    return f"{', '.join(labels[:-1])}, or {labels[-1]}"


def _candidate_id_text(start: int, stop: int) -> str:
    if stop < start:
        return ""
    values = [str(index) for index in range(start, stop + 1)]
    if len(values) == 1:
        return values[0]
    if len(values) == 2:
        return f"{values[0]} or {values[1]}"
    return f"{', '.join(values[:-1])}, or {values[-1]}"


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
    recent_override_outcomes: list[dict[str, Any]] | None = None,
    ensemble_mode: bool = False,
    early_exploration_info: dict[str, Any] | None = None,
) -> str:
    memory_rules = memory_rules or []
    active_hypotheses = active_hypotheses or []
    recent_override_outcomes = recent_override_outcomes or []

    kb_section = f"\n{knowledge_cards_text}" if str(knowledge_cards_text or "").strip() else "\n[Active Knowledge Cards]\nNone available."

    memory_section = ""
    if memory_rules:
        rule_lines = [
            f"  - [{item.get('id', '')}|{item.get('rule_type', '')}] {item.get('statement', '')} "
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

    override_section = ""
    if recent_override_outcomes:
        override_section = "\n[Recent Override Outcomes]\n" + compact_json(recent_override_outcomes[:6])

    stagnation_section = ""
    if stagnation_info and bool(stagnation_info.get("is_stagnant")):
        stagnation_section = f"""
[Stagnation Context]
No meaningful best-result improvement for {int(stagnation_info.get("stagnation_length", 0) or 0)} consecutive iterations.
Last improvement iteration: {stagnation_info.get("last_improvement_iteration", "unknown")}
Current best result: {stagnation_info.get("best_result", "n/a")}

The BO shortlist has already increased exploration pressure where appropriate. Your job is not to maximize
one exploration attribute. Use sigma_rank, value_attempt_counts, and changed_vs_best as context while deciding
which candidate has the best chance to produce a real improvement.
In non-ensemble mode, all exploration must come from the current BO shortlist. When overriding #1 during
stagnation, mention at least two of sigma_rank, value_attempt_counts, changed_vs_best.

Prefer chemistry and trajectory reasoning:
- Chemistry: do knowledge cards, memory rules, or reaction intuition support this region?
- Trajectory: have similar conditions been tried before, and did they help or hurt?
- Information value: if the candidate fails, will that failure teach something specific?
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

    allowed_count = max(len(candidates), 1)
    allowed_choice_text = _candidate_choice_text(allowed_count)
    evidence_choice_text = _candidate_id_text(2, allowed_count)
    coverage_candidate_ids = [
        int(index + 1)
        for index, item in enumerate(candidates[:allowed_count])
        if isinstance(item.get("coverage_targets"), list) and item.get("coverage_targets")
    ]
    bo_unseen_candidate_ids = [
        int(index + 1)
        for index, item in enumerate(candidates[:allowed_count])
        if not (isinstance(item.get("coverage_targets"), list) and item.get("coverage_targets"))
        and isinstance(item.get("unseen_categorical_values"), list)
        and item.get("unseen_categorical_values")
    ]
    early_exploration_enabled = bool(
        isinstance(early_exploration_info, dict) and early_exploration_info.get("enabled")
    )
    early_round = early_exploration_info.get("bo_round_index") if isinstance(early_exploration_info, dict) else None
    early_window = early_exploration_info.get("window") if isinstance(early_exploration_info, dict) else None

    early_exploration_section = ""
    if early_exploration_enabled:
        if early_round is not None and early_window is not None:
            round_text = f"You are in post-warm-start BO round {early_round} of {early_window}."
        else:
            round_text = "You are still in the early post-warm-start exploration window."
        early_exploration_section = f"""
[Early Post-Warm-Start Exploration Guardrail]
{round_text}
Do not prematurely collapse into local optimization around the current best conditions. Use the shortlist to keep
at least one chemically plausible, under-tested direction alive when its expected learning value is competitive.
Prefer a candidate that can either improve the objective or decisively rule out a distinct region; avoid choosing
near-duplicates of the current best solely because their predicted mean is slightly higher.
"""

    coverage_priority_section = ""
    if early_exploration_enabled and (bo_unseen_candidate_ids or coverage_candidate_ids):
        bo_unseen_choice_text = ", ".join(f"#{candidate_id}" for candidate_id in bo_unseen_candidate_ids) or "none"
        coverage_choice_text = ", ".join(f"#{candidate_id}" for candidate_id in coverage_candidate_ids) or "none"
        coverage_priority_section = f"""
[Early Unseen Exploration Priority]
Candidate(s) {bo_unseen_choice_text} are BO-proposed candidates that already contain at least one categorical value
never tested in this campaign. Candidate(s) {coverage_choice_text} are LLM-guided unseen categorical coverage candidates.
During this early post-warm-start exploration window, select by this priority order:
1. First prefer a chemically plausible BO-proposed candidate with unseen categorical value(s).
2. If those are not reasonable, prefer a chemically plausible coverage candidate.
3. Select a BO-proposed candidate without unseen categorical value(s) only when all unseen-bearing and coverage
   candidates are unreasonable, and the non-unseen BO candidate is exceptionally valuable for improvement or decisive learning.
If you skip every unseen-bearing and coverage candidate, explicitly state which exception applies.
"""

    candidate_text = _build_candidate_text(
        candidates,
        ensemble_mode=ensemble_mode,
        include_coverage_annotations=early_exploration_enabled,
    )

    if ensemble_mode:
        candidate_header = (
            f"[Candidates ({allowed_count}-candidate ensemble shortlist; "
            f"{allowed_count}-slot ensemble shortlist; #1 is the ensemble reference candidate)]"
        )
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
- During stagnation, AF provenance still matters, but shortlist-internal exploration should be judged primarily through
  sigma_rank, value_attempt_counts, and changed_vs_best.
"""
        if allowed_count > 1:
            top1_guidance = (
                f"- you may choose any candidate #1 through #{allowed_count} shown above\n"
                "- if you choose candidate #1, briefly explain why following the ensemble reference candidate is sufficient\n"
                f"- if you choose candidate #2 through #{allowed_count}, explicitly compare it against candidate #1, explain why this\n"
                "  non-reference ensemble choice is justified now, and provide override_evidence"
            )
        else:
            top1_guidance = "- candidate #1 is the only available ensemble candidate; explain why it is sufficient"
        selection_mode_schema = "top1_follow|ensemble_non_reference_choice|non_top1_override"
    else:
        candidate_header = f"[Candidates ({allowed_count}-candidate qLogEI-inspired sequential shortlist; #1 is the raw acquisition top-1)]"
        af_guidance = ""
        if allowed_count > 1:
            top1_guidance = (
                "- if you choose candidate #1, briefly explain why following the raw acquisition top-1 is sufficient\n"
                f"- if you choose candidate #2 through #{allowed_count}, explicitly compare it against candidate #1, explain why overriding\n"
                "  top-1 is justified now, and provide override_evidence"
            )
        else:
            top1_guidance = "- candidate #1 is the only available candidate; explain why it is sufficient"
        selection_mode_schema = "top1_follow|non_top1_override"
    stagnation_task_guidance = ""
    if stagnation_info and bool(stagnation_info.get("is_stagnant")):
        stagnation_task_guidance = (
            "- because the campaign is stagnant, prefer candidates that open a chemically plausible and under-tested direction\n"
            "- if you override candidate #1, say why your chosen point is a better stagnation breaker than #1"
        )
    evidence_guidance = (
        f"""If selected_id is {evidence_choice_text}, override_evidence must be non-empty and must use one of:
- knowledge_card: cite an active card_id
- memory_rule: cite an active rule id such as R3
- trajectory: cite at least one observation iteration; 28 and "iter28" are both acceptable
- chemistry: provide a specific chemistry argument, not just "more exploration"
Evidence ids may be a string or list. For memory rules, prefer the displayed rule id, but a quoted rule statement is acceptable.
"""
        if evidence_choice_text
        else "Only candidate #1 is available, so override_evidence may be empty."
    )

    return f"""You are selecting the single best experiment to run next in a chemical reaction optimization campaign.

[Reaction Context]
{compact_json(reaction_context)}
{kb_section}
{memory_section}
{hypothesis_section}
{override_section}
{stagnation_section}

[Observed Data Anchors]
{top_text}

{bottom_text}

Total experiments so far: {int(total_observations)}

{candidate_header}
{candidate_text}
{af_guidance}
{early_exploration_section}
{coverage_priority_section}

[Task]
From chemical reasoning, select the ONE candidate most worth experimenting next. You may only choose {allowed_choice_text}.
Consider:
- chemical plausibility of the predicted yield under those conditions
- whether the model predictions (mu, sigma) align with chemistry intuition
- information gain and hypothesis alignment
- active knowledge cards; cite card IDs in reasoning when they influence your choice
{stagnation_task_guidance}
{top1_guidance}

{evidence_guidance}

Return strict JSON:
{{
  "selected_id": 1,
  "reasoning": "...",
  "comparison_to_top1": "...",
  "selection_mode": "{selection_mode_schema}",
  "override_evidence": {{
    "evidence_type": "knowledge_card|memory_rule|trajectory|chemistry",
    "evidence_ids": [],
    "trajectory_references": [],
    "chemistry_argument": ""
  }}
}}"""


def build_ensemble_sur_selection_prompt(
    reaction_context: dict[str, Any],
    top_observations: list[dict[str, Any]],
    bottom_observations: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    total_observations: int,
    surrogate_composite_summary: list[dict[str, Any]] | None = None,
    composite_explanation: str = "",
    knowledge_cards_text: str = "",
    memory_rules: list[dict[str, Any]] | None = None,
    active_hypotheses: list[dict[str, Any]] | None = None,
    stagnation_info: dict[str, Any] | None = None,
) -> str:
    memory_rules = memory_rules or []
    active_hypotheses = active_hypotheses or []
    surrogate_composite_summary = surrogate_composite_summary or []

    kb_section = f"\n{knowledge_cards_text}" if str(knowledge_cards_text or "").strip() else "\n[Active Knowledge Cards]\nNone available."
    memory_section = ""
    if memory_rules:
        rule_lines = [
            f"  - [{item.get('id', '')}|{item.get('rule_type', '')}] {item.get('statement', '')} "
            f"(conf={float(item.get('confidence', 0.0)):.2f}, evidence={int(item.get('evidence_count', 0) or 0)})"
            for item in memory_rules[:4]
        ]
        memory_section = "\n[Campaign Memory Rules - Soft Priors]\n" + "\n".join(rule_lines)

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
[Stagnation Context]
No meaningful best-result improvement for {int(stagnation_info.get("stagnation_length", 0) or 0)} consecutive iterations.
Current best result: {stagnation_info.get("best_result", "n/a")}
Prefer candidates that preserve plausible exploration or resolve surrogate disagreement.
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

    composite_lines = [
        f"  - {item.get('model_id')}: composite={_fmt_metric(item.get('composite'), precision=4)}, status={item.get('status', 'unknown')}"
        for item in surrogate_composite_summary[:6]
    ]
    composite_text = "\n".join(composite_lines) or "  None"
    explanation = (
        composite_explanation.strip()
        or "composite is a recent LOOCV confidence score; larger means the surrogate has been more reliable recently."
    )

    candidate_lines: list[str] = []
    for item in candidates:
        cross_scores = item.get("surrogate_cross_scores", {}) if isinstance(item.get("surrogate_cross_scores"), dict) else {}
        score_lines = []
        for model_id, score in sorted(cross_scores.items()):
            if not isinstance(score, dict):
                continue
            proposer_mark = "*" if bool(score.get("proposed")) else ""
            score_lines.append(
                f"{model_id}{proposer_mark}: mu={_fmt_metric(score.get('mu'))}, "
                f"sigma={_fmt_metric(score.get('sigma'))}, logei={_fmt_metric(score.get('logei'), precision=6)}, "
                f"rank={score.get('rank', 'n/a')}"
            )
        candidate_lines.append(
            f"  #{item.get('id')}: {json.dumps(item.get('candidate', {}), ensure_ascii=False)}\n"
            f"      proposed_by={json.dumps(item.get('proposed_by', []), ensure_ascii=False)}, "
            f"consensus={item.get('surrogate_consensus_count', len(item.get('proposed_by', []) or []))}\n"
            f"      cross_surrogate=[{'; '.join(score_lines) or 'n/a'}]"
        )
    candidate_text = "\n".join(candidate_lines) or "  None"
    allowed_count = max(len(candidates), 1)
    allowed_choice_text = _candidate_choice_text(allowed_count)

    return f"""You are selecting the single next experiment for a chemical optimization campaign.

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

[Surrogate Composite Summary]
{explanation}
{composite_text}

[Candidates From Fixed-LogEI Surrogate Ensemble]
Each surrogate proposed at most one candidate using LogEI. A star (*) in cross_surrogate marks the proposing model.
Raw LogEI is only comparable within the same surrogate, not across different surrogates.

{candidate_text}

[Task]
Choose exactly ONE candidate: {allowed_choice_text}. Do not invent a new candidate.
Use surrogate consensus, cross-surrogate disagreement, and composite values as the primary decision context.
Treat knowledge cards and memory rules as soft chemistry checks only; do not let an early prior suppress a plausible exploratory point with meaningful uncertainty or information value.

Return strict JSON:
{{
  "selected_id": 1,
  "reasoning": "...",
  "model_confidence_assessment": "...",
  "exploration_rationale": "...",
  "knowledge_memory_check": "...",
  "confidence": 0.75
}}"""


def build_af_strategy_prompt(
    reaction_context: dict[str, Any],
    strategy_context: dict[str, Any],
    knowledge_cards_text: str = "",
    memory_rules: list[dict[str, Any]] | None = None,
) -> str:
    memory_rules = memory_rules or []
    kb_section = f"\n{knowledge_cards_text}" if str(knowledge_cards_text or "").strip() else "\n[Active Knowledge Cards]\nNone available."
    memory_section = "\n[Campaign Memory Rules]\n" + compact_json(memory_rules[:5]) if memory_rules else ""
    return f"""You are setting the acquisition ensemble strategy for a chemical Bayesian optimization campaign.

You do not choose experiments directly. You only choose blend weights over acquisition strategies and the qUCB beta.

[Reaction Context]
{compact_json(reaction_context)}
{kb_section}
{memory_section}

[Current BO State]
{compact_json(strategy_context)}

[Acquisition Strategies]
- qlogei: exploitation-leaning expected improvement; useful when the surrogate is trusted.
- qucb: optimistic mean-plus-uncertainty; useful for controlled exploration and stagnation.
- ts: posterior sampling; useful for diversity when uncertainty is meaningful.

[Task]
Return conservative strategy weights. Do not set qlogei below 0.20 unless impossible; the runtime will enforce this floor.
Use higher qucb_beta during stagnation or clear uncertainty, lower beta when the campaign is improving.

Return strict JSON:
{{
  "weights": {{"qlogei": 0.5, "qucb": 0.3, "ts": 0.2}},
  "qucb_beta": 1.5,
  "reasoning": "...",
  "confidence": 0.7
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


def _fmt_metric(value: Any, precision: int = 4) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.{precision}f}"
    except Exception:
        return "n/a"
