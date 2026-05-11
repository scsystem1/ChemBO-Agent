"""
LLM-internal prior writer for the lightweight knowledge system.
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Callable

from config.settings import Settings
from knowledge.knowledge_card import VALID_ACTIONABLE_FOR, VALID_CARD_TYPES, create_knowledge_card
from knowledge.knowledge_state import infer_knowledge_profile
from knowledge.prompts import build_prior_writer_prompt


REQUIRED_COUNTS = {
    "mechanism": 1,
    "reagent_property": 2,
    "operating_window": 1,
    "failure_mode": 1,
    "hypothesis": 2,
}
DEFAULT_ACTIONABLE_FOR = ["hypothesis_generation", "select_candidate", "result_interpretation"]


def write_initial_priors(
    problem_spec: dict[str, Any],
    settings: Settings,
    llm: Any,
    invoke_json: Callable[[Any, str, str, dict[str, Any]], tuple[dict[str, Any], dict[str, Any]]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    Generate initial active-deck cards from the LLM's internal chemistry priors.
    """
    reaction = problem_spec.get("reaction", {}) if isinstance(problem_spec.get("reaction"), dict) else {}
    family = str(reaction.get("family") or problem_spec.get("reaction_type") or "").strip()
    profile = infer_knowledge_profile(family)
    rejected_cards: list[dict[str, Any]] = []
    rejection_reasons: list[str] = []
    combined_usage: dict[str, Any] = {}
    raw_response: dict[str, Any] = {}

    feedback = ""
    cards: list[dict[str, Any]] = []
    needs_evidence: list[dict[str, Any]] = []
    max_attempts = 2
    for attempt in range(max_attempts):
        system_prompt, user_prompt = build_prior_writer_prompt(problem_spec, profile, validation_feedback=feedback)
        payload, usage = invoke_json(llm, system_prompt, user_prompt, {"cards": [], "global_notes": ""})
        combined_usage = _merge_usage(combined_usage, usage)
        raw_response = payload if isinstance(payload, dict) else {}
        cards, attempt_rejected, attempt_reasons, needs_evidence = _normalize_cards(raw_response, problem_spec)
        rejected_cards.extend(attempt_rejected)
        rejection_reasons.extend(attempt_reasons)
        coverage_feedback = _coverage_feedback(cards)
        min_cards = int(getattr(settings, "prior_writer_min_cards", 6) or 6)
        if not coverage_feedback and len(cards) >= min_cards:
            break
        feedback = "; ".join(coverage_feedback + [f"Need at least {min_cards} valid cards; got {len(cards)}."])

    max_cards = int(getattr(settings, "prior_writer_max_cards", 12) or 12)
    cards = _rank_cards(cards)[: max(0, max_cards)]
    card_ids = {card.get("card_id") for card in cards}
    needs_evidence = [item for item in needs_evidence if item.get("card_id") in card_ids or item.get("text")]
    artifacts = {
        "raw_response": raw_response,
        "rejected_cards": rejected_cards,
        "rejection_reasons": rejection_reasons,
        "needs_evidence": needs_evidence,
        "llm_usage": combined_usage,
        "profile": profile,
    }
    return cards, artifacts


def _normalize_cards(
    payload: dict[str, Any],
    problem_spec: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str], list[dict[str, Any]]]:
    raw_cards = payload.get("cards", []) if isinstance(payload, dict) else []
    if not isinstance(raw_cards, list):
        return [], [], ["cards payload was not a list"], []
    variable_names = {
        str(variable.get("name") or "").strip()
        for variable in problem_spec.get("variables", [])
        if isinstance(variable, dict) and str(variable.get("name") or "").strip()
    }
    cards: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    reasons: list[str] = []
    needs_evidence: list[dict[str, Any]] = []
    seen_text: set[str] = set()

    for index, raw in enumerate(raw_cards, start=1):
        if not isinstance(raw, dict):
            rejected.append({"index": index, "raw": raw})
            reasons.append(f"card {index}: not an object")
            continue
        text = str(raw.get("text") or raw.get("claim") or "").strip()
        card_type = str(raw.get("card_type") or raw.get("type") or "").strip()
        scope = str(raw.get("scope") or "target").strip()
        targets = [str(item).strip() for item in raw.get("targets", []) if str(item).strip()] if isinstance(raw.get("targets", []), list) else []
        key = " ".join(text.lower().split())
        reason = _validate_raw_card(text, card_type, targets, variable_names)
        if not reason and card_type == "hypothesis" and not str(raw.get("testable_prediction") or "").strip():
            reason = "hypothesis card missing testable_prediction"
        if reason:
            rejected.append(dict(raw))
            reasons.append(f"card {index}: {reason}")
            continue
        if key in seen_text:
            rejected.append(dict(raw))
            reasons.append(f"card {index}: duplicate text")
            continue
        seen_text.add(key)
        if card_type in {"mechanism", "constraint", "hypothesis"}:
            valid_targets = [target for target in targets if target in variable_names]
        else:
            valid_targets = targets
        if card_type == "analogy":
            scope = "analogous"
        elif scope not in {"target", "campaign", "analogous", "general"}:
            scope = "target"
        actionable = _normalize_actionable(raw.get("actionable_for"))
        confidence = _clip(float(raw.get("confidence", 0.45) or 0.45), 0.3, 0.6)
        testable_prediction = str(raw.get("testable_prediction") or "").strip() if card_type == "hypothesis" else ""
        try:
            card = create_knowledge_card(
                text=text,
                card_type=card_type,
                scope=scope,
                confidence=confidence,
                targets=valid_targets,
                actionable_for=actionable,
                evidence_refs=[],
                source_type="llm_internal_prior",
                testable_prediction=testable_prediction,
            )
        except Exception as exc:
            rejected.append(dict(raw))
            reasons.append(f"card {index}: {type(exc).__name__}: {exc}")
            continue
        cards.append(card)
        if bool(raw.get("needs_external_evidence")):
            question = str(raw.get("evidence_question") or "").strip()
            if question:
                needs_evidence.append(
                    {
                        "card_id": card["card_id"],
                        "text": card["text"],
                        "card_type": card["card_type"],
                        "targets": list(card.get("targets", [])),
                        "evidence_question": question,
                    }
                )
    return cards, rejected, reasons, needs_evidence


def _validate_raw_card(text: str, card_type: str, targets: list[str], variable_names: set[str]) -> str:
    if card_type not in (VALID_CARD_TYPES - {"constraint"}):
        return f"invalid card_type {card_type!r}"
    word_count = len(text.split())
    if word_count < 10 or word_count > 70:
        return f"text word count {word_count} outside accepted range"
    if card_type == "hypothesis":
        return ""
    if card_type not in {"mechanism", "constraint"}:
        unknown = [target for target in targets if target not in variable_names]
        if unknown:
            return f"unknown targets {unknown}"
        if not targets and variable_names:
            return "non-mechanism card missing targets"
    return ""


def _normalize_actionable(values: Any) -> list[str]:
    if not isinstance(values, list):
        return list(DEFAULT_ACTIONABLE_FOR)
    cleaned = [str(item).strip() for item in values if str(item).strip() in VALID_ACTIONABLE_FOR]
    return cleaned or list(DEFAULT_ACTIONABLE_FOR)


def _coverage_feedback(cards: list[dict[str, Any]]) -> list[str]:
    counts = Counter(str(card.get("card_type") or "") for card in cards)
    feedback: list[str] = []
    for card_type, required in REQUIRED_COUNTS.items():
        if counts.get(card_type, 0) < required:
            feedback.append(f"Need at least {required} valid {card_type} card(s); got {counts.get(card_type, 0)}.")
    return feedback


def _rank_cards(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    priority = {
        "mechanism": 0,
        "reagent_property": 1,
        "operating_window": 2,
        "failure_mode": 3,
        "hypothesis": 4,
        "interaction": 5,
        "analogy": 6,
    }
    return sorted(
        cards,
        key=lambda card: (
            {"target": 0, "campaign": 1, "analogous": 2, "general": 3}.get(str(card.get("scope") or "general"), 99),
            priority.get(str(card.get("card_type") or ""), 99),
            -float(card.get("confidence", 0.0) or 0.0),
            str(card.get("card_id") or ""),
        ),
    )


def _merge_usage(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    merged = dict(left or {})
    for key in ("calls", "input_tokens", "output_tokens", "total_tokens", "estimated_calls"):
        merged[key] = int(merged.get(key, 0) or 0) + int((right or {}).get(key, 0) or 0)
    return merged


def _clip(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


__all__ = ["write_initial_priors"]
