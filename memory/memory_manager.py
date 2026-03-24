"""
ChemBO Agent memory manager with structured semantic rules.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass
class SemanticRule:
    id: str
    rule_type: str
    content: dict[str, Any]
    natural_language: str
    confidence: float
    status: str
    evidence_iterations: list[int]
    evidence_count: int
    source: str
    created_at: str
    last_validated: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "rule_type": self.rule_type,
            "content": self.content,
            "natural_language": self.natural_language,
            "confidence": self.confidence,
            "status": self.status,
            "evidence_iterations": self.evidence_iterations,
            "evidence_count": self.evidence_count,
            "source": self.source,
            "created_at": self.created_at,
            "last_validated": self.last_validated,
        }


class MemoryManager:
    """Three-layer memory with structured semantic rule management."""

    def __init__(self, capacity: int = 200):
        self.capacity = capacity
        self.working: dict[str, Any] = {}
        self.episodic: list[dict[str, Any]] = []
        self.semantic: list[dict[str, Any]] = []

    def update_working(self, key: str, value: Any):
        self.working[key] = value

    def get_working(self, key: str, default=None) -> Any:
        return self.working.get(key, default)

    def add_episode(
        self,
        iteration: int,
        config_snapshot: dict[str, Any],
        candidate: dict[str, Any],
        result: float | None,
        reflection: str,
        non_numerical_observations: str = "",
        lesson_learned: str = "",
    ):
        entry = {
            "iteration": iteration,
            "config_snapshot": config_snapshot,
            "candidate": candidate,
            "result": result,
            "reflection": reflection,
            "non_numerical_observations": non_numerical_observations,
            "lesson_learned": lesson_learned,
            "timestamp": datetime.now().isoformat(),
        }
        self.episodic.append(entry)
        if len(self.episodic) > self.capacity:
            self.episodic = self.episodic[-self.capacity :]

    def add_semantic_rule(self, rule_data: dict[str, Any]) -> dict[str, Any]:
        normalized = _normalize_rule_payload(rule_data, next_id=f"R{len(self.semantic) + 1}")
        if not normalized.get("natural_language"):
            return normalized

        for existing in self.semantic:
            relationship = rules_relationship(existing, normalized)
            if relationship == "same":
                existing["confidence"] = max(float(existing.get("confidence", 0.0)), normalized["confidence"])
                existing["status"] = "active"
                existing["evidence_iterations"] = sorted(
                    set(existing.get("evidence_iterations", [])) | set(normalized["evidence_iterations"])
                )
                existing["evidence_count"] = max(
                    int(existing.get("evidence_count", 0)),
                    len(existing["evidence_iterations"]),
                )
                existing["last_validated"] = datetime.now().isoformat()
                return existing
            if relationship == "contradictory":
                existing["status"] = "contradicted"
                existing["last_validated"] = datetime.now().isoformat()

        self.semantic.append(normalized)
        return normalized

    def get_all_rules(self) -> list[dict[str, Any]]:
        return sorted(self.semantic, key=lambda item: float(item.get("confidence", 0.0)), reverse=True)

    def get_rules_by_type(self, rule_type: str | list[str]) -> list[dict[str, Any]]:
        rule_types = {rule_type} if isinstance(rule_type, str) else set(rule_type)
        return [rule for rule in self.get_all_rules() if rule.get("rule_type") in rule_types]

    def retrieve_similar(self, candidate: dict[str, Any], n: int = 3) -> list[dict[str, Any]]:
        scored = []
        for episode in self.episodic:
            score = _candidate_similarity(candidate, episode.get("candidate", {}))
            scored.append((score, episode))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [episode for score, episode in scored[:n] if score > 0.0]

    def consolidate(self, observations: list[dict[str, Any]] | None = None, trigger: str = "periodic"):
        obs = observations or []
        self._statistical_consolidation(obs)
        if trigger not in {"deep", "full_review"}:
            return
        lesson_groups: dict[str, list[int]] = {}
        for episode in self.episodic:
            lesson = str(episode.get("lesson_learned", "")).strip()
            if not lesson:
                continue
            key = _normalize_text(lesson)
            if not key:
                continue
            lesson_groups.setdefault(key, []).append(int(episode.get("iteration", 0)))
        for lesson_key, iterations in lesson_groups.items():
            if len(iterations) < 2:
                continue
            exemplar = next(
                (
                    str(episode.get("lesson_learned", "")).strip()
                    for episode in self.episodic
                    if _normalize_text(str(episode.get("lesson_learned", "")).strip()) == lesson_key
                ),
                "",
            )
            if exemplar:
                self.add_semantic_rule(
                    {
                        "rule_type": "chemical_effect",
                        "content": {"statement": exemplar},
                        "natural_language": exemplar,
                        "confidence": min(0.55 + 0.08 * len(iterations), 0.82),
                        "status": "tentative",
                        "evidence_iterations": iterations,
                        "source": "consolidation",
                    }
                )

    def get_context_for_node(self, node_name: str, query: dict[str, Any] | None = None) -> str:
        query = query or {}
        parts = []
        if self.working:
            parts.append(f"[Working]\n{json.dumps(self.working, indent=2)}")
        if node_name == "select_candidate":
            rules = self.get_rules_by_type(["chemical_effect", "constraint", "interaction"])[:6]
            candidates = query.get("candidates", [])
            similar = self.retrieve_similar(candidates[0], n=3) if candidates else []
            if rules:
                parts.append(f"[Rules]\n{json.dumps(rules, indent=2)}")
            if similar:
                parts.append(f"[Similar Episodes]\n{json.dumps(similar, indent=2)}")
        elif node_name == "configure_bo":
            rules = self.get_rules_by_type(["bo_strategy"])[:5]
            if rules:
                parts.append(f"[BO Strategy Rules]\n{json.dumps(rules, indent=2)}")
        elif node_name == "interpret_results":
            latest = query.get("latest_candidate", {})
            similar = self.retrieve_similar(latest, n=3)
            if similar:
                parts.append(f"[Similar Episodes]\n{json.dumps(similar, indent=2)}")
            rules = self.get_all_rules()[:6]
            if rules:
                parts.append(f"[Rules]\n{json.dumps(rules, indent=2)}")
        elif node_name == "reflect_and_decide":
            rules = self.get_all_rules()[:8]
            if rules:
                parts.append(f"[Rules]\n{json.dumps(rules, indent=2)}")
            if query.get("performance_log"):
                parts.append(f"[Performance]\n{json.dumps(query['performance_log'][-8:], indent=2)}")
        else:
            recent = self.episodic[-3:]
            if recent:
                parts.append(f"[Recent Episodes]\n{json.dumps(recent, indent=2)}")
        return "\n\n".join(parts) if parts else "[Memory is empty]"

    def get_context_for_llm(self, max_episodes: int = 5) -> str:
        parts = []
        if self.working:
            parts.append(f"[Working]\n{json.dumps(self.working, indent=2)}")
        if self.episodic:
            parts.append(f"[Recent Episodes]\n{json.dumps(self.episodic[-max_episodes:], indent=2)}")
        if self.semantic:
            parts.append(f"[Semantic Rules]\n{json.dumps(self.get_all_rules()[:8], indent=2)}")
        return "\n\n".join(parts) if parts else "[Memory is empty]"

    def to_dict(self) -> dict[str, Any]:
        return {
            "working": self.working,
            "episodic": self.episodic,
            "semantic": self.semantic,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], capacity: int = 200) -> "MemoryManager":
        instance = cls(capacity=capacity)
        instance.working = dict(data.get("working", {}))
        instance.episodic = list(data.get("episodic", []))
        instance.semantic = list(data.get("semantic", []))
        return instance

    def _statistical_consolidation(self, observations: list[dict[str, Any]]):
        usable = [obs for obs in observations if obs.get("result") is not None]
        if len(usable) < 5:
            return

        overall_mean = sum(float(obs["result"]) for obs in usable) / len(usable)
        grouped: dict[str, dict[str, list[tuple[int, float]]]] = {}
        for obs in usable:
            iteration = int(obs.get("iteration", 0))
            candidate = obs.get("candidate", {})
            for variable, value in candidate.items():
                grouped.setdefault(variable, {}).setdefault(str(value), []).append((iteration, float(obs["result"])))

        for variable, value_groups in grouped.items():
            for value, entries in value_groups.items():
                if len(entries) < 2:
                    continue
                mean_value = sum(result for _, result in entries) / len(entries)
                effect = mean_value - overall_mean
                if abs(effect) < 8.0:
                    continue
                direction = "positive" if effect > 0 else "negative"
                self.add_semantic_rule(
                    {
                        "rule_type": "chemical_effect",
                        "content": {
                            "variable": variable,
                            "value": value,
                            "effect_direction": direction,
                            "effect_magnitude_pct": round(abs(effect), 2),
                        },
                        "natural_language": (
                            f"{variable}={value} shows {direction} effect of {effect:+.1f} vs overall mean"
                        ),
                        "confidence": min(0.4 + 0.1 * len(entries), 0.8),
                        "status": "tentative",
                        "evidence_iterations": [iteration for iteration, _ in entries],
                        "source": "consolidation",
                    }
                )


def rules_relationship(rule1: dict[str, Any], rule2: dict[str, Any]) -> str:
    if rule1.get("rule_type") != rule2.get("rule_type"):
        return "unrelated"

    content1 = rule1.get("content", {})
    content2 = rule2.get("content", {})
    if rule1.get("rule_type") == "chemical_effect":
        if content1.get("variable") == content2.get("variable") and content1.get("value") == content2.get("value"):
            if content1.get("effect_direction") == content2.get("effect_direction"):
                return "same"
            return "contradictory"

    overlap = _word_overlap(
        str(rule1.get("natural_language", "")),
        str(rule2.get("natural_language", "")),
    )
    if overlap > 0.7:
        return "same"
    if overlap > 0.4:
        return "uncertain"
    return "unrelated"


def _normalize_rule_payload(rule_data: dict[str, Any], next_id: str) -> dict[str, Any]:
    now = datetime.now().isoformat()
    evidence_iterations = [int(item) for item in rule_data.get("evidence_iterations", [])]
    return SemanticRule(
        id=str(rule_data.get("id") or next_id),
        rule_type=str(rule_data.get("rule_type") or "chemical_effect"),
        content=dict(rule_data.get("content", {})),
        natural_language=str(rule_data.get("natural_language") or rule_data.get("rule") or "").strip(),
        confidence=float(rule_data.get("confidence", 0.5)),
        status=str(rule_data.get("status") or "active"),
        evidence_iterations=evidence_iterations,
        evidence_count=max(int(rule_data.get("evidence_count", 0)), len(evidence_iterations) or 1),
        source=str(rule_data.get("source") or "observation"),
        created_at=str(rule_data.get("created_at") or now),
        last_validated=str(rule_data.get("last_validated") or now),
    ).to_dict()


def _candidate_similarity(candidate1: dict[str, Any], candidate2: dict[str, Any]) -> float:
    if not candidate1 or not candidate2:
        return 0.0
    keys = set(candidate1) | set(candidate2)
    if not keys:
        return 0.0
    score = 0.0
    for key in keys:
        if str(candidate1.get(key)) == str(candidate2.get(key)):
            score += 1.0
    return score / len(keys)


def _word_overlap(text1: str, text2: str) -> float:
    words1 = set(_normalize_text(text1).split())
    words2 = set(_normalize_text(text2).split())
    if not words1 or not words2:
        return 0.0
    return len(words1 & words2) / max(len(words1), len(words2))


def _normalize_text(text: str) -> str:
    import re

    normalized = text.lower().strip()
    normalized = re.sub(r"[^a-z0-9.%+\s]", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized
