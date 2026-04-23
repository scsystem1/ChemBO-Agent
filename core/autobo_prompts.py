"""LLM prompt templates for AutoBO surrogate evaluation and acquisition selection."""
from __future__ import annotations

import json
from typing import Any

from core.prompt_utils import compact_json


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

The campaign may be trapped in a local optimum. Candidates with selection_mode="diversity_escape"
or selection_mode="ensemble_disagreement" were injected specifically to test unexplored or
model-disagreement regions. You must seriously consider these escape candidates. If you still
choose raw top-1 exploitation, explicitly justify why exploitation remains appropriate despite
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

    candidate_text = "\n".join(
        f"  #{item.get('id')}: {json.dumps(item.get('candidate', {}), ensure_ascii=False)}\n"
        f"      step={item.get('selection_step', 'n/a')}, "
        f"mode={item.get('selection_mode', 'n/a')}, "
        f"mu={_fmt_metric(item.get('predicted_value'))}, "
        f"sigma={_fmt_metric(item.get('uncertainty'))}, "
        f"acq={_fmt_metric(item.get('acquisition_value'), precision=6)}, "
        f"raw_acq={_fmt_metric(item.get('acquisition_value_raw'), precision=6)}"
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

[Candidates (qLogEI-inspired sequential shortlist; #1 is the raw acquisition top-1)]
{candidate_text}

[Task]
From chemical reasoning, select the ONE candidate most worth experimenting next.
Consider:
- chemical plausibility of the predicted yield under those conditions
- whether the model predictions (mu, sigma) align with chemistry intuition
- information gain and hypothesis alignment
- active knowledge cards; cite card IDs in reasoning when they influence your choice
- if you choose candidate #1, briefly explain why following the raw acquisition top-1 is sufficient
- if you do not choose candidate #1, you must explicitly compare your chosen candidate against candidate #1,
  explain why overriding top-1 is justified now, and label the override as exploration, mechanism validation,
  or exploitation

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


def _fmt_metric(value: Any, precision: int = 4) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.{precision}f}"
    except Exception:
        return "n/a"
