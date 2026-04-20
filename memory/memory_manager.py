"""
ChemBO Agent memory manager v2 with structured episodes, semantic graph rules,
token-budgeted memory packets, and maintenance hooks.
"""
from __future__ import annotations

import itertools
import json
import math
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from core.prompt_utils import compact_json

try:  # pragma: no cover - optional dependency
    from rdkit import Chem, DataStructs
    from rdkit.Chem import AllChem
except Exception:  # pragma: no cover - optional dependency
    Chem = None
    DataStructs = None
    AllChem = None


DEFAULT_NODE_BUDGETS = {
    "generate_hypotheses": 900,
    "warm_start": 1400,
    "run_bo_iteration": 1800,
    "select_candidate": 1400,
    "interpret_results": 1600,
    "reflect_and_decide": 1200,
    "default": 900,
}

DEFAULT_MESSAGE_LIMITS = {
    "generate_hypotheses": 8,
    "warm_start": 6,
    "run_bo_iteration": 6,
    "select_candidate": 8,
    "interpret_results": 8,
    "reflect_and_decide": 8,
    "memory_consolidation": 4,
    "default": 6,
}


@dataclass
class CausalAttribution:
    variable: str
    old_value: str | float | int | None = None
    new_value: str | float | int | None = None
    direction: str = "neutral"
    confidence: float = 0.5
    mechanism: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "variable": self.variable,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "direction": self.direction,
            "confidence": round(_clip(self.confidence), 4),
            "mechanism": self.mechanism,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "CausalAttribution":
        return cls(
            variable=str(payload.get("variable") or "").strip(),
            old_value=payload.get("old_value"),
            new_value=payload.get("new_value"),
            direction=str(payload.get("direction") or "neutral").strip() or "neutral",
            confidence=_coerce_float(payload.get("confidence"), 0.5),
            mechanism=str(payload.get("mechanism") or "").strip(),
        )


@dataclass
class HypothesisEvidence:
    hypothesis_id: str
    relation: str
    strength: float = 0.5
    reasoning: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "hypothesis_id": self.hypothesis_id,
            "relation": self.relation,
            "strength": round(_clip(self.strength), 4),
            "reasoning": self.reasoning,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "HypothesisEvidence":
        return cls(
            hypothesis_id=str(payload.get("hypothesis_id") or "").strip(),
            relation=str(payload.get("relation") or "neutral").strip() or "neutral",
            strength=_coerce_float(payload.get("strength"), 0.5),
            reasoning=str(payload.get("reasoning") or "").strip(),
        )


@dataclass
class Episode:
    id: str
    iteration: int
    candidate: dict[str, Any]
    result: float | None
    config_snapshot: dict[str, Any] = field(default_factory=dict)
    observation_metadata: dict[str, Any] = field(default_factory=dict)
    reflection: str = ""
    non_numerical_observations: str = ""
    lesson: str = ""
    prediction_gap: float | None = None
    delta_best: float | None = None
    importance: float = 0.0
    novelty: float = 0.0
    knowledge_tension: dict[str, Any] = field(default_factory=dict)
    causal_attributions: list[CausalAttribution] = field(default_factory=list)
    hypothesis_evidence: list[HypothesisEvidence] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    access_count: int = 0
    last_accessed: float = 0.0
    created_at: float = field(default_factory=time.time)

    @property
    def is_improvement(self) -> bool:
        return bool(self.delta_best is not None and self.delta_best > 0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "iteration": self.iteration,
            "candidate": dict(self.candidate),
            "result": self.result,
            "config_snapshot": dict(self.config_snapshot),
            "observation_metadata": dict(self.observation_metadata),
            "reflection": self.reflection,
            "non_numerical_observations": self.non_numerical_observations,
            "lesson": self.lesson,
            "lesson_learned": self.lesson,
            "prediction_gap": self.prediction_gap,
            "delta_best": self.delta_best,
            "importance": round(_clip(self.importance), 4),
            "novelty": round(_clip(self.novelty), 4),
            "knowledge_tension": dict(self.knowledge_tension),
            "causal_attributions": [item.to_dict() for item in self.causal_attributions],
            "hypothesis_evidence": [item.to_dict() for item in self.hypothesis_evidence],
            "tags": list(self.tags),
            "access_count": int(self.access_count),
            "last_accessed": float(self.last_accessed or 0.0),
            "created_at": float(self.created_at or 0.0),
            "timestamp": _ts_to_iso(self.created_at),
            "is_improvement": self.is_improvement,
        }

    def to_summary_dict(self) -> dict[str, Any]:
        candidate_brief = {
            key: value
            for key, value in list(self.candidate.items())[: min(6, len(self.candidate))]
        }
        return {
            "id": self.id,
            "iteration": self.iteration,
            "candidate": candidate_brief,
            "result": self.result,
            "importance": round(_clip(self.importance), 4),
            "delta_best": self.delta_best,
            "prediction_gap": self.prediction_gap,
            "lesson": self.lesson,
            "reflection": self.reflection[:160],
            "tags": list(self.tags[:8]),
            "hypothesis_evidence": [item.to_dict() for item in self.hypothesis_evidence[:4]],
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "Episode":
        created_at = _coerce_float(payload.get("created_at"))
        if created_at is None:
            created_at = _iso_to_ts(str(payload.get("timestamp") or ""))
        causal = [
            CausalAttribution.from_payload(item)
            for item in payload.get("causal_attributions", [])
            if isinstance(item, dict)
        ]
        evidence = [
            HypothesisEvidence.from_payload(item)
            for item in payload.get("hypothesis_evidence", [])
            if isinstance(item, dict)
        ]
        return cls(
            id=str(payload.get("id") or f"E{int(payload.get('iteration', 0) or 0)}"),
            iteration=int(payload.get("iteration", 0) or 0),
            candidate=dict(payload.get("candidate", {})),
            result=_coerce_float(payload.get("result")),
            config_snapshot=dict(payload.get("config_snapshot", {})),
            observation_metadata=dict(payload.get("observation_metadata", payload.get("metadata", {}))),
            reflection=str(payload.get("reflection") or "").strip(),
            non_numerical_observations=str(payload.get("non_numerical_observations") or "").strip(),
            lesson=str(payload.get("lesson") or payload.get("lesson_learned") or "").strip(),
            prediction_gap=_coerce_float(payload.get("prediction_gap")),
            delta_best=_coerce_float(payload.get("delta_best")),
            importance=_coerce_float(payload.get("importance"), 0.0),
            novelty=_coerce_float(payload.get("novelty"), 0.0),
            knowledge_tension=dict(payload.get("knowledge_tension", {})),
            causal_attributions=causal,
            hypothesis_evidence=evidence,
            tags=_normalize_str_list(payload.get("tags", [])),
            access_count=int(payload.get("access_count", 0) or 0),
            last_accessed=_coerce_float(payload.get("last_accessed"), 0.0),
            created_at=created_at or time.time(),
        )


@dataclass
class RetrievalQuery:
    query_type: str
    candidate: dict[str, Any] | None = None
    variable_names: list[str] = field(default_factory=list)
    variable_values: dict[str, Any] = field(default_factory=dict)
    hypothesis_ids: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    changed_pairs: list[tuple[str, Any, Any]] = field(default_factory=list)
    problem_variables: list[dict[str, Any]] = field(default_factory=list)
    current_iteration: int = 0


@dataclass
class SemanticNode:
    id: str
    rule_type: str
    statement: str
    variables: list[str] = field(default_factory=list)
    conditions: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.5
    evidence_count: int = 0
    supporting_episode_ids: list[str] = field(default_factory=list)
    contradicting_episode_ids: list[str] = field(default_factory=list)
    supporting_card_ids: list[str] = field(default_factory=list)
    conflicting_card_ids: list[str] = field(default_factory=list)
    status: str = "active"
    source: str = "observation"
    created_at_iteration: int = 0
    last_validated: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "rule_type": self.rule_type,
            "statement": self.statement,
            "variables": list(self.variables),
            "conditions": dict(self.conditions),
            "confidence": round(_clip(self.confidence), 4),
            "evidence_count": int(max(self.evidence_count, len(self.supporting_episode_ids))),
            "supporting_episode_ids": list(self.supporting_episode_ids),
            "contradicting_episode_ids": list(self.contradicting_episode_ids),
            "supporting_card_ids": list(self.supporting_card_ids),
            "conflicting_card_ids": list(self.conflicting_card_ids),
            "status": self.status,
            "source": self.source,
            "created_at_iteration": self.created_at_iteration,
            "last_validated": self.last_validated,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any], next_id: str | None = None) -> "SemanticNode":
        statement = str(
            payload.get("statement")
            or payload.get("natural_language")
            or payload.get("rule")
            or payload.get("content", {}).get("statement")
            or ""
        ).strip()
        variables = payload.get("variables")
        if not isinstance(variables, list):
            variables = []
            content = payload.get("content", {})
            if isinstance(content, dict):
                variable = str(content.get("variable") or payload.get("variable") or "").strip()
                if variable:
                    variables.append(variable)
        conditions = payload.get("conditions")
        if not isinstance(conditions, dict):
            conditions = dict(payload.get("content", {})) if isinstance(payload.get("content"), dict) else {}
        return cls(
            id=str(payload.get("id") or next_id or "R1"),
            rule_type=str(payload.get("rule_type") or "chemical_effect").strip() or "chemical_effect",
            statement=statement,
            variables=_normalize_str_list(variables),
            conditions=conditions,
            confidence=_coerce_float(payload.get("confidence"), 0.5),
            evidence_count=int(payload.get("evidence_count", 0) or 0),
            supporting_episode_ids=_normalize_str_list(
                payload.get("supporting_episode_ids", payload.get("evidence_episodes", []))
            ),
            contradicting_episode_ids=_normalize_str_list(payload.get("contradicting_episode_ids", [])),
            supporting_card_ids=_normalize_str_list(
                payload.get("supporting_card_ids", payload.get("supports_cards", []))
            ),
            conflicting_card_ids=_normalize_str_list(
                payload.get("conflicting_card_ids", payload.get("conflicts_cards", []))
            ),
            status=str(payload.get("status") or "active").strip() or "active",
            source=str(payload.get("source") or "observation").strip() or "observation",
            created_at_iteration=int(payload.get("created_at_iteration", 0) or 0),
            last_validated=int(payload.get("last_validated", 0) or 0),
            metadata=dict(payload.get("metadata", {})),
        )

    def compact(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "rule_type": self.rule_type,
            "statement": self.statement,
            "variables": list(self.variables),
            "confidence": round(_clip(self.confidence), 3),
            "status": self.status,
            "evidence_count": int(max(self.evidence_count, len(self.supporting_episode_ids))),
            "supports_cards": list(self.supporting_card_ids[:3]),
            "conflicts_cards": list(self.conflicting_card_ids[:3]),
        }


@dataclass
class SemanticEdge:
    source_id: str
    target_id: str
    relation: str
    weight: float = 0.5
    evidence: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "target_id": self.target_id,
            "relation": self.relation,
            "weight": round(_clip(self.weight), 4),
            "evidence": self.evidence,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "SemanticEdge":
        return cls(
            source_id=str(payload.get("source_id") or "").strip(),
            target_id=str(payload.get("target_id") or "").strip(),
            relation=str(payload.get("relation") or "supports").strip() or "supports",
            weight=_coerce_float(payload.get("weight"), 0.5),
            evidence=str(payload.get("evidence") or "").strip(),
        )


@dataclass
class MemoryWriteResult:
    episode_id: str
    explicit_rule_ids: list[str] = field(default_factory=list)
    recommended_trigger: str = "periodic"
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "episode_id": self.episode_id,
            "explicit_rule_ids": list(self.explicit_rule_ids),
            "recommended_trigger": self.recommended_trigger,
            "notes": list(self.notes),
        }


@dataclass
class ConsolidationReport:
    trigger: str
    new_rules: list[dict[str, Any]] = field(default_factory=list)
    updated_rules: list[dict[str, Any]] = field(default_factory=list)
    deprecated_rules: list[str] = field(default_factory=list)
    contradictions: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    llm_triggered: bool = False
    llm_usage: dict[str, Any] = field(default_factory=dict)
    state_updates: dict[str, Any] = field(default_factory=dict)

    def record_new_rule(self, rule: SemanticNode) -> None:
        self.new_rules.append(rule.compact())

    def record_updated_rule(self, rule: SemanticNode) -> None:
        self.updated_rules.append(rule.compact())

    def to_dict(self) -> dict[str, Any]:
        return {
            "trigger": self.trigger,
            "new_rules": list(self.new_rules),
            "updated_rules": list(self.updated_rules),
            "deprecated_rules": list(self.deprecated_rules),
            "contradictions": list(self.contradictions),
            "notes": list(self.notes),
            "llm_triggered": bool(self.llm_triggered),
            "llm_usage": dict(self.llm_usage),
            "state_updates": dict(self.state_updates),
        }


class WorkingMemoryState:
    def __init__(self, data: dict[str, Any] | None = None):
        self.data = dict(data or {})

    def update(self, key: str, value: Any) -> None:
        self.data[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.data)


class EpisodicStore:
    def __init__(self, capacity: int, keep_recent: int, keep_salient: int, episodes: list[Episode] | None = None):
        self.capacity = max(1, int(capacity or 1))
        self.keep_recent = max(1, int(keep_recent or 1))
        self.keep_salient = max(1, int(keep_salient or 1))
        self.episodes = list(episodes or [])
        self._var_index: dict[str, dict[str, set[str]]] = {}
        self._tag_index: dict[str, set[str]] = {}
        self._hypothesis_index: dict[str, set[str]] = {}
        self._change_index: dict[str, set[str]] = {}
        self._rebuild_indexes()

    def add(self, episode: Episode) -> None:
        self.episodes.append(episode)
        self._index_episode(episode)
        self._prune()

    def to_dict(self) -> list[dict[str, Any]]:
        return [episode.to_dict() for episode in self.episodes]

    def retrieve(self, query: RetrievalQuery, top_k: int = 5) -> list[Episode]:
        if not self.episodes:
            return []
        top_k = max(1, int(top_k or 1))
        candidate_ids = self._candidate_ids(query)
        candidates = [episode for episode in self.episodes if episode.id in candidate_ids] if candidate_ids else list(self.episodes)
        scored: list[tuple[float, Episode]] = []
        for episode in candidates:
            relevance = self._query_relevance(episode, query)
            chemistry = self._candidate_similarity(episode.candidate, query.candidate or {}, query.problem_variables)
            recency = self._recency_score(episode, query.current_iteration)
            reuse_utility = min(0.4, 0.05 * episode.access_count + 0.05 * (1.0 if episode.last_accessed else 0.0))
            score = (
                0.35 * relevance
                + 0.20 * chemistry
                + 0.20 * _clip(episode.importance)
                + 0.15 * recency
                + 0.10 * _clip(reuse_utility)
            )
            if score <= 0.0:
                continue
            scored.append((score, episode))
        if not scored:
            return []
        scored.sort(key=lambda item: item[0], reverse=True)
        selected = self._mmr_select(scored, top_k, query.problem_variables)
        now = time.time()
        for episode in selected:
            episode.access_count += 1
            episode.last_accessed = now
        return selected

    def _candidate_ids(self, query: RetrievalQuery) -> set[str]:
        candidate_ids: set[str] = set()
        initialized = False
        if query.variable_values:
            for variable, value in query.variable_values.items():
                var_matches = set(self._var_index.get(variable, {}).get(str(value), set()))
                if not initialized:
                    candidate_ids = set(var_matches)
                    initialized = True
                else:
                    candidate_ids |= var_matches
        if query.tags:
            tag_matches = set()
            for tag in query.tags:
                tag_matches |= self._tag_index.get(tag, set())
            if tag_matches:
                candidate_ids = candidate_ids | tag_matches if initialized else set(tag_matches)
                initialized = True
        if query.hypothesis_ids:
            hypothesis_matches = set()
            for hypothesis_id in query.hypothesis_ids:
                hypothesis_matches |= self._hypothesis_index.get(hypothesis_id, set())
            if hypothesis_matches:
                candidate_ids = candidate_ids | hypothesis_matches if initialized else set(hypothesis_matches)
                initialized = True
        if query.changed_pairs:
            pair_matches = set()
            for variable, old_value, new_value in query.changed_pairs:
                key = f"{variable}:{old_value}->{new_value}"
                pair_matches |= self._change_index.get(key, set())
            if pair_matches:
                candidate_ids = candidate_ids | pair_matches if initialized else set(pair_matches)
                initialized = True
        return candidate_ids

    def _query_relevance(self, episode: Episode, query: RetrievalQuery) -> float:
        scores: list[float] = []
        if query.candidate:
            scores.append(self._candidate_similarity(episode.candidate, query.candidate, query.problem_variables))
        if query.variable_values:
            matches = 0
            for variable, value in query.variable_values.items():
                if str(episode.candidate.get(variable)) == str(value):
                    matches += 1
            scores.append(matches / max(len(query.variable_values), 1))
        if query.variable_names:
            names = set(query.variable_names)
            causal_hits = sum(1 for item in episode.causal_attributions if item.variable in names)
            scores.append(causal_hits / max(len(names), 1))
        if query.tags:
            tag_hits = len(set(episode.tags) & set(query.tags))
            scores.append(tag_hits / max(len(query.tags), 1))
        if query.hypothesis_ids:
            supported = {item.hypothesis_id for item in episode.hypothesis_evidence}
            hypothesis_hits = len(supported & set(query.hypothesis_ids))
            scores.append(hypothesis_hits / max(len(query.hypothesis_ids), 1))
        if query.changed_pairs:
            causal_hits = 0
            for variable, old_value, new_value in query.changed_pairs:
                for item in episode.causal_attributions:
                    if (
                        item.variable == variable
                        and str(item.old_value) == str(old_value)
                        and str(item.new_value) == str(new_value)
                    ):
                        causal_hits += 1
                        break
            scores.append(causal_hits / max(len(query.changed_pairs), 1))
        return sum(scores) / len(scores) if scores else 0.0

    def _recency_score(self, episode: Episode, current_iteration: int) -> float:
        if current_iteration <= 0:
            return 1.0
        age = max(0, current_iteration - int(episode.iteration))
        return 1.0 / (1.0 + 0.18 * age)

    def _mmr_select(
        self,
        scored: list[tuple[float, Episode]],
        top_k: int,
        problem_variables: list[dict[str, Any]],
    ) -> list[Episode]:
        remaining = list(scored)
        selected: list[Episode] = []
        while remaining and len(selected) < top_k:
            best_episode = None
            best_score = float("-inf")
            for base_score, episode in remaining:
                if not selected:
                    mmr = base_score
                else:
                    redundancy = max(
                        self._candidate_similarity(episode.candidate, chosen.candidate, problem_variables)
                        for chosen in selected
                    )
                    mmr = 0.82 * base_score - 0.18 * redundancy
                if mmr > best_score:
                    best_score = mmr
                    best_episode = episode
            if best_episode is None:
                break
            selected.append(best_episode)
            remaining = [(score, episode) for score, episode in remaining if episode.id != best_episode.id]
        return selected

    def _candidate_similarity(
        self,
        candidate_a: dict[str, Any],
        candidate_b: dict[str, Any],
        problem_variables: list[dict[str, Any]],
    ) -> float:
        if not candidate_a or not candidate_b:
            return 0.0
        variable_map = {
            str(variable.get("name") or ""): variable
            for variable in problem_variables
            if str(variable.get("name") or "")
        }
        keys = set(candidate_a) | set(candidate_b)
        if not keys:
            return 0.0
        total = 0.0
        for key in keys:
            spec = variable_map.get(key, {})
            total += _value_similarity(candidate_a.get(key), candidate_b.get(key), spec)
        return total / len(keys)

    def _prune(self) -> None:
        if len(self.episodes) <= self.capacity:
            return
        recent = self.episodes[-self.keep_recent :]
        recent_ids = {episode.id for episode in recent}
        older = [episode for episode in self.episodes if episode.id not in recent_ids]
        older.sort(
            key=lambda item: (
                item.importance,
                item.access_count,
                item.last_accessed,
                item.iteration,
            ),
            reverse=True,
        )
        keep_old = older[: max(0, self.capacity - len(recent))]
        if len(keep_old) > self.keep_salient:
            keep_old = keep_old[: self.keep_salient]
        kept = sorted(keep_old + recent, key=lambda item: item.iteration)
        self.episodes = kept[-self.capacity :]
        self._rebuild_indexes()

    def _rebuild_indexes(self) -> None:
        self._var_index = {}
        self._tag_index = {}
        self._hypothesis_index = {}
        self._change_index = {}
        for episode in self.episodes:
            self._index_episode(episode)

    def _index_episode(self, episode: Episode) -> None:
        for variable, value in episode.candidate.items():
            self._var_index.setdefault(str(variable), {}).setdefault(str(value), set()).add(episode.id)
        for tag in episode.tags:
            self._tag_index.setdefault(tag, set()).add(episode.id)
        for evidence in episode.hypothesis_evidence:
            self._hypothesis_index.setdefault(evidence.hypothesis_id, set()).add(episode.id)
        for attribution in episode.causal_attributions:
            key = f"{attribution.variable}:{attribution.old_value}->{attribution.new_value}"
            self._change_index.setdefault(key, set()).add(episode.id)


class SemanticGraph:
    def __init__(self, nodes: dict[str, SemanticNode] | None = None, edges: list[SemanticEdge] | None = None):
        self.nodes = dict(nodes or {})
        self.edges = list(edges or [])

    def add_rule(self, payload: dict[str, Any] | SemanticNode) -> tuple[SemanticNode, str]:
        next_id = f"R{self._next_index()}"
        node = payload if isinstance(payload, SemanticNode) else SemanticNode.from_payload(payload, next_id=next_id)
        if not node.statement:
            node.statement = _build_rule_statement(node)
        if not node.variables and isinstance(node.conditions, dict):
            variable = str(node.conditions.get("variable") or "").strip()
            if variable:
                node.variables = [variable]
        node.confidence = _clip(node.confidence)
        node.last_validated = max(node.last_validated, node.created_at_iteration)
        for existing in self.nodes.values():
            relation = self._detect_relation(existing, node)
            if relation == "same":
                merged = self._merge_into_existing(existing, node)
                return merged, "merged"
            if relation == "contradicts":
                self._register_contradiction(existing, node)
            elif relation in {"refines", "supports", "co_occurs"}:
                self._upsert_edge(node.id, existing.id, relation, evidence="rule relationship detected")
        self.nodes[node.id] = node
        return node, "added"

    def query_rules(
        self,
        *,
        variables: list[str] | None = None,
        rule_types: list[str] | None = None,
        min_confidence: float = 0.3,
        status: list[str] | None = None,
        limit: int = 10,
    ) -> list[SemanticNode]:
        statuses = set(status or ["active", "tentative"])
        variable_set = set(variables or [])
        rule_type_set = set(rule_types or [])
        candidates = []
        for node in self.nodes.values():
            if node.status not in statuses:
                continue
            if node.confidence < min_confidence:
                continue
            if variable_set and not (set(node.variables) & variable_set):
                continue
            if rule_type_set and node.rule_type not in rule_type_set:
                continue
            score = (
                0.55 * _clip(node.confidence)
                + 0.25 * min(math.log1p(max(node.evidence_count, 1)) / math.log(6), 1.0)
                + 0.20 * min(node.last_validated / max(node.last_validated, 1), 1.0)
            )
            candidates.append((score, node))
        candidates.sort(key=lambda item: item[0], reverse=True)
        return [node for _, node in candidates[:limit]]

    def related_rules(self, rule_id: str, depth: int = 1) -> list[tuple[SemanticNode, str]]:
        if rule_id not in self.nodes or depth < 1:
            return []
        visited = {rule_id}
        frontier = [rule_id]
        result: list[tuple[SemanticNode, str]] = []
        for _ in range(depth):
            next_frontier = []
            for current_id in frontier:
                for edge in self.edges:
                    neighbor_id = None
                    if edge.source_id == current_id:
                        neighbor_id = edge.target_id
                    elif edge.target_id == current_id:
                        neighbor_id = edge.source_id
                    if not neighbor_id or neighbor_id in visited or neighbor_id not in self.nodes:
                        continue
                    visited.add(neighbor_id)
                    result.append((self.nodes[neighbor_id], edge.relation))
                    next_frontier.append(neighbor_id)
            frontier = next_frontier
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": [node.to_dict() for node in self.nodes.values()],
            "edges": [edge.to_dict() for edge in self.edges],
        }

    @classmethod
    def from_payload(cls, payload: Any) -> "SemanticGraph":
        if isinstance(payload, dict):
            nodes = {}
            for item in payload.get("nodes", []):
                if not isinstance(item, dict):
                    continue
                node = SemanticNode.from_payload(item)
                nodes[node.id] = node
            edges = [
                SemanticEdge.from_payload(item)
                for item in payload.get("edges", [])
                if isinstance(item, dict)
            ]
            return cls(nodes=nodes, edges=edges)
        if isinstance(payload, list):
            graph = cls()
            for item in payload:
                if isinstance(item, dict):
                    graph.add_rule(item)
            return graph
        return cls()

    def _next_index(self) -> int:
        max_seen = 0
        for key in self.nodes:
            if key.startswith("R") and key[1:].isdigit():
                max_seen = max(max_seen, int(key[1:]))
        return max_seen + 1

    def _detect_relation(self, existing: SemanticNode, new: SemanticNode) -> str:
        if existing.rule_type != new.rule_type:
            if set(existing.variables) & set(new.variables):
                return "co_occurs"
            return "unrelated"
        if _normalize_text(existing.statement) == _normalize_text(new.statement):
            return "same"
        if _same_value_effect(existing, new):
            return "same"
        if _contradicting_effect(existing, new):
            return "contradicts"
        if set(existing.variables) and set(existing.variables) < set(new.variables):
            return "refines"
        if set(existing.variables) & set(new.variables):
            return "supports"
        return "unrelated"

    def _merge_into_existing(self, existing: SemanticNode, new: SemanticNode) -> SemanticNode:
        existing.confidence = max(existing.confidence, new.confidence)
        existing.statement = existing.statement or new.statement
        existing.evidence_count = max(
            existing.evidence_count,
            len(set(existing.supporting_episode_ids + new.supporting_episode_ids)),
            new.evidence_count,
        )
        existing.supporting_episode_ids = _normalize_str_list(
            existing.supporting_episode_ids + new.supporting_episode_ids
        )
        existing.contradicting_episode_ids = _normalize_str_list(
            existing.contradicting_episode_ids + new.contradicting_episode_ids
        )
        existing.supporting_card_ids = _normalize_str_list(existing.supporting_card_ids + new.supporting_card_ids)
        existing.conflicting_card_ids = _normalize_str_list(existing.conflicting_card_ids + new.conflicting_card_ids)
        existing.variables = _normalize_str_list(existing.variables + new.variables)
        existing.conditions.update(new.conditions)
        existing.metadata.update(new.metadata)
        existing.status = "active"
        existing.last_validated = max(existing.last_validated, new.last_validated, new.created_at_iteration)
        return existing

    def _register_contradiction(self, existing: SemanticNode, new: SemanticNode) -> None:
        existing.contradicting_episode_ids = _normalize_str_list(
            existing.contradicting_episode_ids + new.supporting_episode_ids
        )
        existing.confidence = max(0.1, existing.confidence * 0.88)
        if existing.confidence < 0.25:
            existing.status = "tentative"
        self._upsert_edge(new.id, existing.id, "contradicts", evidence="opposing rule direction detected")

    def _upsert_edge(self, source_id: str, target_id: str, relation: str, evidence: str = "") -> None:
        for edge in self.edges:
            if (
                edge.source_id == source_id
                and edge.target_id == target_id
                and edge.relation == relation
            ):
                edge.weight = min(1.0, edge.weight + 0.1)
                if evidence and evidence not in edge.evidence:
                    edge.evidence = f"{edge.evidence}; {evidence}".strip("; ")
                return
        self.edges.append(SemanticEdge(source_id=source_id, target_id=target_id, relation=relation, evidence=evidence))


class ConsolidationEngine:
    def __init__(
        self,
        every_n: int = 5,
        enable_llm_consolidation: bool = True,
        llm_cooldown_iters: int = 5,
    ):
        self.every_n = max(1, int(every_n or 1))
        self.enable_llm_consolidation = bool(enable_llm_consolidation)
        self.llm_cooldown_iters = max(1, int(llm_cooldown_iters or 1))

    def run(
        self,
        *,
        state: dict[str, Any],
        episodic_store: EpisodicStore,
        semantic_graph: SemanticGraph,
        llm_adapter: Any = None,
    ) -> ConsolidationReport:
        trigger = str(state.get("_memory_trigger") or "periodic")
        report = ConsolidationReport(trigger=trigger)
        current_iter = int(state.get("iteration", 0) or 0)
        last_run = int(state.get("_memory_last_maint_iter", 0) or 0)
        report.state_updates["_memory_last_maint_iter"] = current_iter
        report.state_updates["_memory_last_llm_iter"] = int(state.get("_memory_last_llm_iter", 0) or 0)
        usable = [episode for episode in episodic_store.episodes if episode.result is not None]
        if not usable:
            return report
        if current_iter > 0 and last_run == current_iter:
            report.notes.append(f"Skipped duplicate maintenance at iteration {current_iter}.")
            return report
        self._statistical_consolidation(state, usable, semantic_graph, report)
        if len(usable) >= self.every_n and len(usable) % self.every_n == 0:
            self._interaction_detection(state, usable, semantic_graph, report)
        self._align_with_knowledge(state, usable, semantic_graph, report)
        self._stagnation_strategy(state, usable, semantic_graph, report)
        self._decay_stale_rules(state, semantic_graph, report)
        if self._should_trigger_llm(trigger, usable, state) and llm_adapter is not None:
            self._llm_abstraction(state, usable, semantic_graph, llm_adapter, report)
        return report

    def _statistical_consolidation(
        self,
        state: dict[str, Any],
        usable: list[Episode],
        semantic_graph: SemanticGraph,
        report: ConsolidationReport,
    ) -> None:
        all_results = [episode.result for episode in usable if episode.result is not None]
        if len(all_results) < 5:
            return
        spread = max(statistics.pstdev(all_results) if len(all_results) > 1 else 0.0, 5.0)
        direction = str(state.get("optimization_direction") or "maximize").lower()
        grouped: dict[str, dict[str, list[Episode]]] = {}
        for episode in usable:
            for variable, value in episode.candidate.items():
                grouped.setdefault(str(variable), {}).setdefault(str(value), []).append(episode)
        for variable, values in grouped.items():
            for value, matches in values.items():
                if len(matches) < 3:
                    continue
                other_results = [
                    episode.result
                    for episode in usable
                    if str(episode.candidate.get(variable)) != value and episode.result is not None
                ]
                if len(other_results) < 2:
                    continue
                match_results = [episode.result for episode in matches if episode.result is not None]
                signed_effect = (_mean(match_results) - _mean(other_results))
                if direction == "minimize":
                    signed_effect *= -1.0
                effect_size = signed_effect / max(spread, 1.0)
                if abs(effect_size) < 0.35:
                    continue
                rule = SemanticNode(
                    id=f"R{semantic_graph._next_index()}",
                    rule_type="chemical_effect",
                    statement=(
                        f"{variable}={value} shows a {'positive' if effect_size > 0 else 'negative'} "
                        f"effect in this campaign (effect_size={effect_size:+.2f})"
                    ),
                    variables=[variable],
                    conditions={
                        "variable": variable,
                        "value": value,
                        "direction": "positive" if effect_size > 0 else "negative",
                        "effect_size": round(effect_size, 4),
                    },
                    confidence=min(0.92, 0.35 + 0.08 * len(match_results) + 0.15 * min(abs(effect_size), 1.0)),
                    evidence_count=len(match_results),
                    supporting_episode_ids=[episode.id for episode in matches],
                    status="active" if len(match_results) >= 4 else "tentative",
                    source="consolidation",
                    created_at_iteration=int(state.get("iteration", 0) or 0),
                    last_validated=int(state.get("iteration", 0) or 0),
                )
                node, outcome = semantic_graph.add_rule(rule)
                if outcome == "added":
                    report.record_new_rule(node)
                else:
                    report.record_updated_rule(node)

    def _interaction_detection(
        self,
        state: dict[str, Any],
        usable: list[Episode],
        semantic_graph: SemanticGraph,
        report: ConsolidationReport,
    ) -> None:
        variables = sorted({key for episode in usable for key in episode.candidate})
        direction = str(state.get("optimization_direction") or "maximize").lower()
        spread = max(
            statistics.pstdev([episode.result for episode in usable if episode.result is not None])
            if len(usable) > 1
            else 0.0,
            5.0,
        )
        for var_a, var_b in itertools.combinations(variables, 2):
            grouped: dict[tuple[str, str], list[float]] = {}
            values_a: set[str] = set()
            values_b: set[str] = set()
            for episode in usable:
                value_a = str(episode.candidate.get(var_a))
                value_b = str(episode.candidate.get(var_b))
                values_a.add(value_a)
                values_b.add(value_b)
                grouped.setdefault((value_a, value_b), []).append(float(episode.result))
            if len(values_a) < 2 or len(values_b) < 2 or len(grouped) < 4:
                continue
            conditional_effects = []
            for value_b in values_b:
                bucket = {
                    value_a: _mean(grouped.get((value_a, value_b), []))
                    for value_a in values_a
                    if grouped.get((value_a, value_b))
                }
                if len(bucket) < 2:
                    continue
                ordered = list(bucket.values())
                conditional_effects.append(max(ordered) - min(ordered))
            if len(conditional_effects) < 2:
                continue
            interaction_strength = (max(conditional_effects) - min(conditional_effects)) / max(spread, 1.0)
            if direction == "minimize":
                interaction_strength = abs(interaction_strength)
            if interaction_strength < 0.25:
                continue
            rule = SemanticNode(
                id=f"R{semantic_graph._next_index()}",
                rule_type="interaction",
                statement=f"{var_a} and {var_b} show a campaign-specific interaction effect",
                variables=[var_a, var_b],
                conditions={"interaction_strength": round(interaction_strength, 4)},
                confidence=min(0.88, 0.35 + 0.12 * min(interaction_strength, 1.0)),
                evidence_count=sum(len(values) for values in grouped.values()),
                supporting_episode_ids=[episode.id for episode in usable],
                status="tentative",
                source="consolidation",
                created_at_iteration=int(state.get("iteration", 0) or 0),
                last_validated=int(state.get("iteration", 0) or 0),
            )
            node, outcome = semantic_graph.add_rule(rule)
            if outcome == "added":
                report.record_new_rule(node)
            else:
                report.record_updated_rule(node)

    def _align_with_knowledge(
        self,
        state: dict[str, Any],
        usable: list[Episode],
        semantic_graph: SemanticGraph,
        report: ConsolidationReport,
    ) -> None:
        cards = state.get("knowledge_cards", [])
        variables = state.get("problem_spec", {}).get("variables", [])
        role_map = _build_variable_role_map(variables)
        direction = str(state.get("optimization_direction") or "maximize").lower()
        for card in cards:
            if not isinstance(card, dict) or card.get("category") != "reagent_prior":
                continue
            target_vars = _card_target_variables(card, role_map)
            if not target_vars:
                continue
            preferred = _card_top_values(card)
            if not preferred:
                continue
            for variable in target_vars:
                matched = [
                    episode for episode in usable
                    if str(episode.candidate.get(variable, "")).lower().strip() in preferred
                ]
                others = [
                    episode for episode in usable
                    if str(episode.candidate.get(variable, "")).lower().strip() not in preferred
                    and episode.candidate.get(variable) is not None
                ]
                if len(matched) < 2 or len(others) < 2:
                    continue
                signed_effect = _mean([episode.result for episode in matched]) - _mean([episode.result for episode in others])
                if direction == "minimize":
                    signed_effect *= -1.0
                if signed_effect < -5.0:
                    rule = SemanticNode(
                        id=f"R{semantic_graph._next_index()}",
                        rule_type="override",
                        statement=(
                            f"Campaign evidence overrides prior card {card.get('card_id')} for {variable}; "
                            f"retrieved preferred values underperform here"
                        ),
                        variables=[variable],
                        conditions={
                            "variable": variable,
                            "preferred_values": sorted(preferred),
                            "comparison_delta": round(signed_effect, 3),
                        },
                        confidence=min(0.82, 0.45 + 0.05 * len(matched)),
                        evidence_count=len(matched),
                        supporting_episode_ids=[episode.id for episode in matched],
                        conflicting_card_ids=[str(card.get("card_id") or "")],
                        status="active",
                        source="knowledge_alignment",
                        created_at_iteration=int(state.get("iteration", 0) or 0),
                        last_validated=int(state.get("iteration", 0) or 0),
                    )
                    node, outcome = semantic_graph.add_rule(rule)
                    if outcome == "added":
                        report.record_new_rule(node)
                    else:
                        report.record_updated_rule(node)
                elif signed_effect > 5.0:
                    for node in semantic_graph.query_rules(
                        variables=[variable],
                        rule_types=["chemical_effect", "override"],
                        min_confidence=0.0,
                        status=["active", "tentative", "deprecated"],
                        limit=12,
                    ):
                        node.supporting_card_ids = _normalize_str_list(node.supporting_card_ids + [str(card.get("card_id") or "")])
                        node.confidence = min(0.97, node.confidence + 0.03)
                        report.record_updated_rule(node)

    def _stagnation_strategy(
        self,
        state: dict[str, Any],
        usable: list[Episode],
        semantic_graph: SemanticGraph,
        report: ConsolidationReport,
    ) -> None:
        convergence = state.get("convergence_state", {}) or {}
        if not convergence.get("is_stagnant"):
            return
        recent = usable[-4:]
        avg_changes = 0.0
        comparisons = 0
        for previous, current in zip(recent, recent[1:]):
            comparisons += 1
            avg_changes += _count_candidate_changes(previous.candidate, current.candidate)
        avg_changes = avg_changes / comparisons if comparisons else 0.0
        diagnosis = "search is changing too few variables between iterations" if avg_changes <= 1.5 else "recent changes are broad but not productive"
        rule = SemanticNode(
            id=f"R{semantic_graph._next_index()}",
            rule_type="strategy",
            statement=(
                "Recent performance is stagnant; prioritize reconfiguration or stronger exploration on the next loop"
            ),
            variables=[],
            conditions={
                "diagnosis": diagnosis,
                "stagnation_length": int(convergence.get("stagnation_length", 0) or 0),
                "avg_changes": round(avg_changes, 3),
            },
            confidence=min(0.85, 0.45 + 0.04 * int(convergence.get("stagnation_length", 0) or 0)),
            evidence_count=len(recent),
            supporting_episode_ids=[episode.id for episode in recent],
            status="active",
            source="consolidation",
            created_at_iteration=int(state.get("iteration", 0) or 0),
            last_validated=int(state.get("iteration", 0) or 0),
        )
        node, outcome = semantic_graph.add_rule(rule)
        if outcome == "added":
            report.record_new_rule(node)
        else:
            report.record_updated_rule(node)

    def _decay_stale_rules(
        self,
        state: dict[str, Any],
        semantic_graph: SemanticGraph,
        report: ConsolidationReport,
    ) -> None:
        current_iteration = int(state.get("iteration", 0) or 0)
        for node in semantic_graph.nodes.values():
            if node.status == "deprecated":
                continue
            age = current_iteration - int(node.last_validated or 0)
            if age <= self.every_n * 2:
                continue
            node.confidence = max(0.1, node.confidence * (0.96 ** max(age - self.every_n * 2, 1)))
            if node.confidence < 0.2:
                node.status = "deprecated"
                report.deprecated_rules.append(node.id)
            elif node.confidence < 0.3:
                node.status = "tentative"
            report.record_updated_rule(node)

    def _llm_abstraction(
        self,
        state: dict[str, Any],
        usable: list[Episode],
        semantic_graph: SemanticGraph,
        llm_adapter: Any,
        report: ConsolidationReport,
    ) -> None:
        top_episodes = sorted(usable, key=lambda item: (item.importance, item.iteration), reverse=True)[:6]
        existing_rules = [node.compact() for node in semantic_graph.query_rules(min_confidence=0.25, limit=8)]
        cards = [
            {
                "card_id": item.get("card_id"),
                "category": item.get("category"),
                "claim": item.get("claim"),
                "variables_affected": item.get("variables_affected", []),
            }
            for item in state.get("knowledge_cards", [])[:6]
            if isinstance(item, dict)
        ]
        default = {"new_rules": [], "updated_rules": []}
        prompt = f"""You are consolidating ChemBO campaign memory.

Based only on the structured episodes, current semantic rules, and knowledge cards,
return 0-2 NEW reusable rules that would materially help future candidate selection,
result interpretation, or reconfiguration. Prefer strategy, interaction, or override rules.

EPISODES:
{compact_json([episode.to_summary_dict() for episode in top_episodes])}

EXISTING_RULES:
{compact_json(existing_rules)}

KNOWLEDGE_CARDS:
{compact_json(cards)}

Return strict JSON:
{{
  "new_rules": [
    {{
      "rule_type": "chemical_effect|interaction|constraint|strategy|override",
      "statement": "...",
      "variables": ["..."],
      "conditions": {{}},
      "confidence": 0.0,
      "supporting_episode_ids": ["E1"],
      "supporting_card_ids": ["kc_..."],
      "conflicting_card_ids": []
    }}
  ],
  "updated_rules": [
    {{
      "id": "R1",
      "confidence": 0.0,
      "status": "active|tentative|deprecated"
    }}
  ]
}}"""
        try:
            payload, usage = llm_adapter.invoke_json(prompt, default)
        except Exception as exc:  # pragma: no cover - runtime safeguard
            report.notes.append(f"LLM consolidation failed: {type(exc).__name__}: {exc}")
            return
        report.llm_triggered = True
        report.llm_usage = dict(usage or {})
        report.state_updates["_memory_last_llm_iter"] = int(state.get("iteration", 0) or 0)
        if not isinstance(payload, dict):
            return
        for item in payload.get("new_rules", []):
            if not isinstance(item, dict):
                continue
            node, outcome = semantic_graph.add_rule(
                SemanticNode.from_payload(
                    {
                        **item,
                        "source": "llm_consolidation",
                        "created_at_iteration": int(state.get("iteration", 0) or 0),
                        "last_validated": int(state.get("iteration", 0) or 0),
                    }
                )
            )
            if outcome == "added":
                report.record_new_rule(node)
            else:
                report.record_updated_rule(node)
        for item in payload.get("updated_rules", []):
            if not isinstance(item, dict):
                continue
            rule_id = str(item.get("id") or "").strip()
            if rule_id not in semantic_graph.nodes:
                continue
            node = semantic_graph.nodes[rule_id]
            node.confidence = _clip(_coerce_float(item.get("confidence"), node.confidence))
            status = str(item.get("status") or "").strip()
            if status:
                node.status = status
            node.last_validated = int(state.get("iteration", 0) or 0)
            report.record_updated_rule(node)

    def _should_trigger_llm(self, trigger: str, usable: list[Episode], state: dict[str, Any]) -> bool:
        if not self.enable_llm_consolidation:
            return False
        if len(usable) < self.every_n:
            return False
        if trigger not in {"milestone", "improvement", "stagnation", "reflection"}:
            return False
        current_iter = int(state.get("iteration", 0) or 0)
        last_llm_iter = int(state.get("_memory_last_llm_iter", 0) or 0)
        if last_llm_iter <= 0:
            return True
        cooldown = max(self.every_n, self.llm_cooldown_iters)
        return current_iter <= 0 or (current_iter - last_llm_iter) >= cooldown


class ContextAssembler:
    NODE_PROFILES = {
        "generate_hypotheses": ["working_focus", "key_episodes", "chemical_effects", "knowledge_tensions"],
        "run_bo_iteration": ["working_focus", "chemical_effects", "interaction_rules", "key_episodes", "knowledge_overrides"],
        "select_candidate": ["working_focus", "candidate_snapshot", "similar_episodes", "active_rules", "knowledge_conflicts"],
        "interpret_results": ["working_focus", "causal_history", "contradiction_alerts", "active_rules", "recent_episodes"],
        "reflect_and_decide": ["working_focus", "performance_trend", "convergence_snapshot", "strategy_rules", "stagnation_diagnosis"],
        "default": ["working_focus", "recent_episodes", "active_rules"],
    }

    def __init__(self, node_budgets: dict[str, int] | None = None):
        self.node_budgets = {**DEFAULT_NODE_BUDGETS, **(node_budgets or {})}

    def assemble(
        self,
        node_name: str,
        *,
        state: dict[str, Any],
        episodic_store: EpisodicStore,
        semantic_graph: SemanticGraph,
        working_memory: WorkingMemoryState,
        query: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        query = dict(query or {})
        budget = int(self.node_budgets.get(node_name, self.node_budgets["default"]))
        sections = self.NODE_PROFILES.get(node_name, self.NODE_PROFILES["default"])
        packet: dict[str, Any] = {
            "node": node_name,
            "budget_tokens": budget,
            "sections": {},
        }
        for section in sections:
            content = self._build_section(
                section,
                node_name=node_name,
                state=state,
                episodic_store=episodic_store,
                semantic_graph=semantic_graph,
                working_memory=working_memory,
                query=query,
            )
            if content not in (None, "", [], {}):
                packet["sections"][section] = content
        packet["estimated_tokens"] = _estimate_tokens(packet["sections"])
        packet["trimmed"] = False
        if packet["estimated_tokens"] > budget:
            packet["sections"] = self._trim_sections(packet["sections"], budget)
            packet["estimated_tokens"] = _estimate_tokens(packet["sections"])
            packet["trimmed"] = True
        return packet

    def _build_section(
        self,
        section: str,
        *,
        node_name: str,
        state: dict[str, Any],
        episodic_store: EpisodicStore,
        semantic_graph: SemanticGraph,
        working_memory: WorkingMemoryState,
        query: dict[str, Any],
    ) -> Any:
        problem_variables = state.get("problem_spec", {}).get("variables", [])
        if section == "working_focus":
            return {
                "current_focus": working_memory.get("current_focus", ""),
                "pending_decisions": list(working_memory.get("pending_decisions", []) or [])[:4],
            }
        if section == "candidate_snapshot":
            shortlist = state.get("proposal_shortlist", []) or []
            return [
                {
                    "index": idx,
                    "candidate": item.get("candidate", {}),
                    "predicted_value": item.get("predicted_value"),
                    "uncertainty": item.get("uncertainty"),
                    "acquisition_value": item.get("acquisition_value"),
                }
                for idx, item in enumerate(shortlist[:4])
            ]
        if section == "similar_episodes":
            shortlist = state.get("proposal_shortlist", []) or []
            candidate = query.get("candidate") or (shortlist[0].get("candidate", {}) if shortlist else {})
            episodes = episodic_store.retrieve(
                RetrievalQuery(
                    query_type="similar_candidate",
                    candidate=candidate,
                    problem_variables=problem_variables,
                    current_iteration=int(state.get("iteration", 0) or 0),
                ),
                top_k=4,
            )
            return [episode.to_summary_dict() for episode in episodes]
        if section == "causal_history":
            latest = state.get("observations", [])[-1] if state.get("observations") else {}
            previous = state.get("observations", [])[-2] if len(state.get("observations", [])) >= 2 else {}
            changed_pairs = _extract_changed_pairs(previous.get("candidate", {}), latest.get("candidate", {}))
            episodes = episodic_store.retrieve(
                RetrievalQuery(
                    query_type="causal",
                    variable_names=[item[0] for item in changed_pairs],
                    changed_pairs=changed_pairs,
                    problem_variables=problem_variables,
                    current_iteration=int(state.get("iteration", 0) or 0),
                ),
                top_k=4,
            )
            return [
                {
                    "episode": episode.id,
                    "iteration": episode.iteration,
                    "result": episode.result,
                    "causal_attributions": [item.to_dict() for item in episode.causal_attributions[:3]],
                    "reflection": episode.reflection[:120],
                }
                for episode in episodes
            ]
        if section == "chemical_effects":
            return [node.compact() for node in semantic_graph.query_rules(rule_types=["chemical_effect"], limit=6)]
        if section == "interaction_rules":
            return [node.compact() for node in semantic_graph.query_rules(rule_types=["interaction"], limit=4)]
        if section == "strategy_rules":
            return [node.compact() for node in semantic_graph.query_rules(rule_types=["strategy"], limit=4)]
        if section == "active_rules":
            return [
                node.compact()
                for node in semantic_graph.query_rules(
                    rule_types=["chemical_effect", "interaction", "override", "constraint"],
                    limit=6,
                )
            ]
        if section == "knowledge_overrides":
            return [node.compact() for node in semantic_graph.query_rules(rule_types=["override"], limit=4)]
        if section == "knowledge_conflicts":
            latest_rules = semantic_graph.query_rules(rule_types=["override"], min_confidence=0.2, limit=4)
            return [
                {
                    "rule": node.statement,
                    "conflicting_cards": list(node.conflicting_card_ids[:3]),
                    "confidence": round(node.confidence, 3),
                }
                for node in latest_rules
            ]
        if section == "contradiction_alerts":
            latest = state.get("observations", [])[-1] if state.get("observations") else {}
            candidate = latest.get("candidate", {})
            result_value = _coerce_float(latest.get("result"))
            alerts = []
            for node in semantic_graph.query_rules(
                variables=list(candidate.keys()),
                rule_types=["chemical_effect", "override"],
                min_confidence=0.2,
                limit=8,
            ):
                if _node_contradicts_observation(node, candidate, result_value, state.get("optimization_direction", "maximize")):
                    alerts.append(
                        {
                            "rule_id": node.id,
                            "statement": node.statement,
                            "confidence": round(node.confidence, 3),
                        }
                    )
            return alerts[:4]
        if section == "key_episodes":
            episodes = sorted(
                episodic_store.episodes,
                key=lambda item: (item.importance, item.iteration),
                reverse=True,
            )[:4]
            return [episode.to_summary_dict() for episode in episodes]
        if section == "recent_episodes":
            return [episode.to_summary_dict() for episode in episodic_store.episodes[-4:]]
        if section == "knowledge_tensions":
            return [
                {
                    "episode": episode.id,
                    "reason": episode.knowledge_tension.get("reason", ""),
                    "conflicting_cards": episode.knowledge_tension.get("conflicting_cards", []),
                }
                for episode in episodic_store.episodes[-4:]
                if episode.knowledge_tension
            ]
        if section == "performance_trend":
            return list(state.get("performance_log", [])[-6:])
        if section == "config_trend":
            return [
                {
                    "version": item.get("config_version"),
                    "surrogate_model": item.get("surrogate_model"),
                    "kernel": item.get("kernel_config", {}).get("key"),
                    "acquisition_function": item.get("acquisition_function"),
                    "validated": item.get("validated", True),
                }
                for item in state.get("config_history", [])[-4:]
            ]
        if section == "convergence_snapshot":
            return dict(state.get("convergence_state", {}))
        if section == "stagnation_diagnosis":
            convergence = state.get("convergence_state", {}) or {}
            if not convergence.get("is_stagnant"):
                return {}
            recent = episodic_store.episodes[-4:]
            change_counts = [
                _count_candidate_changes(previous.candidate, current.candidate)
                for previous, current in zip(recent, recent[1:])
            ]
            return {
                "is_stagnant": True,
                "stagnation_length": convergence.get("stagnation_length", 0),
                "average_candidate_changes": round(_mean(change_counts), 3) if change_counts else 0.0,
                "strategy_rules": [node.compact() for node in semantic_graph.query_rules(rule_types=["strategy"], limit=2)],
            }
        return {}

    def _trim_sections(self, sections: dict[str, Any], budget: int) -> dict[str, Any]:
        trimmed = json.loads(json.dumps(sections))
        while _estimate_tokens(trimmed) > budget:
            changed = False
            for key, value in list(trimmed.items()):
                if isinstance(value, list) and len(value) > 1:
                    trimmed[key] = value[:-1]
                    changed = True
                    if _estimate_tokens(trimmed) <= budget:
                        return trimmed
                elif isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict) and value[0]:
                    item = dict(value[0])
                    item.pop(next(reversed(item)))
                    trimmed[key] = [item] if item else []
                    changed = True
                    if _estimate_tokens(trimmed) <= budget:
                        return trimmed
                elif isinstance(value, dict) and value:
                    last_key = next(reversed(value))
                    value.pop(last_key)
                    changed = True
                    if _estimate_tokens(trimmed) <= budget:
                        return trimmed
            if not changed:
                if trimmed:
                    trimmed.pop(next(reversed(trimmed)))
                    continue
                break
        return trimmed


class MemoryManager:
    """Facade for working, episodic, and semantic memory."""

    def __init__(
        self,
        capacity: int = 200,
        *,
        node_budgets: dict[str, int] | None = None,
        consolidation_every_n: int = 5,
        enable_llm_consolidation: bool = True,
        llm_cooldown_iters: int = 5,
        episode_keep_recent: int = 24,
        episode_keep_salient: int = 96,
    ):
        self.capacity = max(1, int(capacity or 1))
        self.node_budgets = {**DEFAULT_NODE_BUDGETS, **(node_budgets or {})}
        self.working_state = WorkingMemoryState()
        self.episodic_store = EpisodicStore(
            self.capacity,
            keep_recent=episode_keep_recent,
            keep_salient=episode_keep_salient,
        )
        self.semantic_graph = SemanticGraph()
        self.maintenance_engine = ConsolidationEngine(
            every_n=consolidation_every_n,
            enable_llm_consolidation=enable_llm_consolidation,
            llm_cooldown_iters=llm_cooldown_iters,
        )
        self.context_assembler = ContextAssembler(node_budgets=self.node_budgets)

    @property
    def working(self) -> dict[str, Any]:
        return self.working_state.data

    @property
    def episodic(self) -> list[dict[str, Any]]:
        return self.episodic_store.to_dict()

    @property
    def semantic(self) -> list[dict[str, Any]]:
        return [node.to_dict() for node in self.semantic_graph.query_rules(min_confidence=0.0, status=["active", "tentative", "deprecated"], limit=max(len(self.semantic_graph.nodes), 1))]

    def update_working(self, key: str, value: Any) -> None:
        self.working_state.update(key, value)

    def get_working(self, key: str, default: Any = None) -> Any:
        return self.working_state.get(key, default)

    def add_episode(
        self,
        iteration: int,
        config_snapshot: dict[str, Any],
        candidate: dict[str, Any],
        result: float | None,
        reflection: str,
        non_numerical_observations: str = "",
        lesson_learned: str = "",
    ) -> None:
        episode = Episode(
            id=f"E{int(iteration)}",
            iteration=int(iteration),
            candidate=dict(candidate),
            result=_coerce_float(result),
            config_snapshot=dict(config_snapshot),
            reflection=str(reflection or "").strip(),
            non_numerical_observations=str(non_numerical_observations or "").strip(),
            lesson=str(lesson_learned or "").strip(),
            created_at=time.time(),
        )
        self.episodic_store.add(episode)

    def add_semantic_rule(self, rule_data: dict[str, Any]) -> dict[str, Any]:
        node, _ = self.semantic_graph.add_rule(rule_data)
        return node.to_dict()

    def record_result(self, state: dict[str, Any], interpretation_payload: dict[str, Any]) -> MemoryWriteResult:
        if "episodic_memory" not in interpretation_payload:
            interpretation_payload = _expand_flat_interpretation_payload(interpretation_payload)
        latest = state.get("observations", [])[-1] if state.get("observations") else {}
        previous = state.get("observations", [])[-2] if len(state.get("observations", [])) >= 2 else {}
        episodic_payload = interpretation_payload.get("episodic_memory", {}) if isinstance(interpretation_payload, dict) else {}
        metadata = dict(latest.get("metadata", {}))
        explicit_rules = []
        candidate = dict(latest.get("candidate", {}))
        result_value = _coerce_float(latest.get("result"))
        best_before = _coerce_float(metadata.get("best_before_result"))
        direction = str(state.get("optimization_direction") or "maximize").lower()
        delta_best = _delta_best(best_before, result_value, direction)
        predicted = _coerce_float(metadata.get("predicted_value"))
        prediction_gap = result_value - predicted if predicted is not None and result_value is not None else None
        novelty = self._compute_novelty(candidate, state.get("problem_spec", {}).get("variables", []))
        causal = _normalize_causal_attributions(episodic_payload.get("causal_attributions", []))
        if not causal:
            causal = _derive_causal_attributions(previous.get("candidate", {}), candidate, result_value, delta_best)
        hypothesis_evidence = _normalize_hypothesis_evidence(
            episodic_payload.get("hypothesis_evidence", []),
            interpretation_payload,
        )
        knowledge_tension = _normalize_knowledge_tension(episodic_payload.get("knowledge_tension", {}))
        importance = _compute_importance(
            result_value=result_value,
            delta_best=delta_best,
            prediction_gap=prediction_gap,
            novelty=novelty,
            hypothesis_evidence=hypothesis_evidence,
            knowledge_tension=knowledge_tension,
        )
        lesson = str(episodic_payload.get("lesson") or episodic_payload.get("lesson_learned") or "").strip()
        episode = Episode(
            id=f"E{int(latest.get('iteration', state.get('iteration', 0)))}",
            iteration=int(latest.get("iteration", state.get("iteration", 0)) or 0),
            candidate=candidate,
            result=result_value,
            config_snapshot=dict(state.get("effective_config", {})),
            observation_metadata=metadata,
            reflection=str(episodic_payload.get("reflection") or interpretation_payload.get("interpretation") or "").strip(),
            non_numerical_observations=str(episodic_payload.get("non_numerical_observations") or "").strip(),
            lesson=lesson,
            prediction_gap=prediction_gap,
            delta_best=delta_best,
            importance=importance,
            novelty=novelty,
            knowledge_tension=knowledge_tension,
            causal_attributions=causal,
            hypothesis_evidence=hypothesis_evidence,
            tags=_episode_tags(candidate, result_value, delta_best, causal, hypothesis_evidence, knowledge_tension),
            created_at=time.time(),
        )
        self.episodic_store.add(episode)
        if isinstance(interpretation_payload.get("semantic_rule"), dict) and interpretation_payload.get("semantic_rule"):
            node, _ = self.semantic_graph.add_rule(
                {
                    **interpretation_payload["semantic_rule"],
                    "created_at_iteration": episode.iteration,
                    "last_validated": episode.iteration,
                    "supporting_episode_ids": [episode.id]
                    + list(interpretation_payload["semantic_rule"].get("supporting_episode_ids", [])),
                }
            )
            explicit_rules.append(node.id)
        for key, value in (interpretation_payload.get("working_memory") or {}).items():
            self.update_working(str(key), value)
        recommended_trigger = self._maintenance_trigger(state, episode)
        notes = []
        if knowledge_tension.get("conflicting_cards"):
            notes.append("episode conflicts with retrieved priors")
        if episode.is_improvement:
            notes.append("episode improved best-so-far")
        return MemoryWriteResult(
            episode_id=episode.id,
            explicit_rule_ids=explicit_rules,
            recommended_trigger=recommended_trigger,
            notes=notes,
        )

    def build_memory_packet(
        self,
        node_name: str,
        state: dict[str, Any],
        query: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.context_assembler.assemble(
            node_name,
            state=state,
            episodic_store=self.episodic_store,
            semantic_graph=self.semantic_graph,
            working_memory=self.working_state,
            query=query,
        )

    def get_context_for_node(self, node_name: str, query: dict[str, Any] | None = None) -> str:
        packet = self.build_memory_packet(node_name, {"iteration": 0, "observations": []}, query=query)
        return json.dumps(packet, indent=2)

    def get_context_for_llm(self, max_episodes: int = 5) -> str:
        packet = {
            "working_focus": self.working_state.to_dict(),
            "recent_episodes": [item.to_summary_dict() for item in self.episodic_store.episodes[-max_episodes:]],
            "semantic_rules": [item.compact() for item in self.semantic_graph.query_rules(limit=6)],
        }
        return json.dumps(packet, indent=2)

    def run_maintenance(
        self,
        state: dict[str, Any],
        trigger: str,
        llm_adapter: Any = None,
    ) -> ConsolidationReport:
        state_for_run = dict(state)
        state_for_run["_memory_trigger"] = trigger
        return self.maintenance_engine.run(
            state=state_for_run,
            episodic_store=self.episodic_store,
            semantic_graph=self.semantic_graph,
            llm_adapter=llm_adapter,
        )

    def consolidate(self, observations: list[dict[str, Any]] | None = None, trigger: str = "periodic") -> ConsolidationReport:
        del observations
        return self.run_maintenance({"observations": [], "iteration": 0}, trigger=trigger)

    def export_campaign_memory(self) -> dict[str, Any]:
        return {
            "semantic_rules": [
                node.to_dict()
                for node in self.semantic_graph.query_rules(
                    min_confidence=0.5,
                    status=["active", "tentative"],
                    limit=max(len(self.semantic_graph.nodes), 1),
                )
            ],
            "key_episodes": [
                episode.to_summary_dict()
                for episode in sorted(
                    self.episodic_store.episodes,
                    key=lambda item: (item.importance, item.iteration),
                    reverse=True,
                )[:10]
            ],
            "interaction_effects": [
                node.to_dict()
                for node in self.semantic_graph.query_rules(
                    rule_types=["interaction"],
                    min_confidence=0.35,
                    status=["active", "tentative"],
                    limit=max(len(self.semantic_graph.nodes), 1),
                )
            ],
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 2,
            "working": self.working_state.to_dict(),
            "episodic": self.episodic_store.to_dict(),
            "semantic": self.semantic_graph.to_dict(),
        }

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        capacity: int = 200,
        *,
        node_budgets: dict[str, int] | None = None,
        consolidation_every_n: int = 5,
        enable_llm_consolidation: bool = True,
        llm_cooldown_iters: int = 5,
        episode_keep_recent: int = 24,
        episode_keep_salient: int = 96,
    ) -> "MemoryManager":
        instance = cls(
            capacity=capacity,
            node_budgets=node_budgets,
            consolidation_every_n=consolidation_every_n,
            enable_llm_consolidation=enable_llm_consolidation,
            llm_cooldown_iters=llm_cooldown_iters,
            episode_keep_recent=episode_keep_recent,
            episode_keep_salient=episode_keep_salient,
        )
        payload = dict(data or {})
        instance.working_state = WorkingMemoryState(payload.get("working", {}))
        instance.episodic_store = EpisodicStore(
            capacity=capacity,
            keep_recent=episode_keep_recent,
            keep_salient=episode_keep_salient,
            episodes=[
                Episode.from_payload(item)
                for item in payload.get("episodic", [])
                if isinstance(item, dict)
            ],
        )
        instance.semantic_graph = SemanticGraph.from_payload(payload.get("semantic", []))
        instance.context_assembler = ContextAssembler(node_budgets=instance.node_budgets)
        return instance

    def _compute_novelty(self, candidate: dict[str, Any], problem_variables: list[dict[str, Any]]) -> float:
        if not self.episodic_store.episodes:
            return 1.0
        similarities = [
            self.episodic_store._candidate_similarity(candidate, episode.candidate, problem_variables)
            for episode in self.episodic_store.episodes[-12:]
        ]
        return round(_clip(1.0 - max(similarities or [0.0])), 4)

    def _maintenance_trigger(self, state: dict[str, Any], episode: Episode) -> str:
        convergence = state.get("convergence_state", {}) or {}
        if episode.is_improvement and episode.delta_best is not None and episode.delta_best > 0:
            return "improvement"
        if convergence.get("stagnation_length", 0) >= 3:
            return "stagnation"
        if episode.iteration > 0 and episode.iteration % self.maintenance_engine.every_n == 0:
            return "milestone"
        if episode.knowledge_tension.get("conflicting_cards"):
            return "stagnation"
        return "periodic"


def rules_relationship(rule1: dict[str, Any], rule2: dict[str, Any]) -> str:
    node1 = SemanticNode.from_payload(rule1)
    node2 = SemanticNode.from_payload(rule2)
    if _same_value_effect(node1, node2) or _normalize_text(node1.statement) == _normalize_text(node2.statement):
        return "same"
    if _contradicting_effect(node1, node2):
        return "contradictory"
    if set(node1.variables) & set(node2.variables):
        return "uncertain"
    return "unrelated"


def _build_rule_statement(node: SemanticNode) -> str:
    variable = str(node.conditions.get("variable") or "").strip()
    value = str(node.conditions.get("value") or "").strip()
    direction = str(node.conditions.get("direction") or node.conditions.get("effect_direction") or "").strip()
    if variable and value and direction:
        return f"{variable}={value} has a {direction} effect"
    return node.statement or "campaign-derived rule"


def _episode_tags(
    candidate: dict[str, Any],
    result_value: float | None,
    delta_best: float | None,
    causal_attributions: list[CausalAttribution],
    hypothesis_evidence: list[HypothesisEvidence],
    knowledge_tension: dict[str, Any],
) -> list[str]:
    tags = []
    if result_value is not None:
        tags.append("result:observed")
    if delta_best is not None:
        tags.append("result:improved" if delta_best > 0 else "result:non_improving")
    for variable, value in candidate.items():
        tags.append(f"var:{variable}={value}")
    for item in causal_attributions:
        tags.append(f"change:{item.variable}")
    for item in hypothesis_evidence:
        tags.append(f"hypothesis:{item.hypothesis_id}:{item.relation}")
    for card_id in knowledge_tension.get("conflicting_cards", []):
        tags.append(f"knowledge_conflict:{card_id}")
    return _normalize_str_list(tags)


def _expand_flat_interpretation_payload(payload: dict[str, Any]) -> dict[str, Any]:
    knowledge_conflict = payload.get("knowledge_conflict", {})
    reflection = str(payload.get("reflection") or payload.get("interpretation") or "").strip()
    working_focus = str(payload.get("working_focus") or "Continue collecting evidence.").strip()
    return {
        "interpretation": str(payload.get("interpretation") or "").strip(),
        "supported_hypotheses": list(payload.get("supported_hypotheses", []) or []),
        "refuted_hypotheses": list(payload.get("refuted_hypotheses", []) or []),
        "archived_hypotheses": list(payload.get("archived_hypotheses", []) or []),
        "episodic_memory": {
            "reflection": reflection,
            "lesson_learned": reflection,
            "non_numerical_observations": "",
            "causal_attributions": [],
            "hypothesis_evidence": [],
            "knowledge_tension": knowledge_conflict if isinstance(knowledge_conflict, dict) else {},
        },
        "semantic_rule": None,
        "working_memory": {
            "current_focus": working_focus,
            "pending_decisions": [],
        },
    }


def _normalize_causal_attributions(payload: Any) -> list[CausalAttribution]:
    return [
        item
        for item in (
            CausalAttribution.from_payload(entry)
            for entry in (payload if isinstance(payload, list) else [])
            if isinstance(entry, dict)
        )
        if item.variable
    ]


def _derive_causal_attributions(
    previous_candidate: dict[str, Any],
    current_candidate: dict[str, Any],
    result_value: float | None,
    delta_best: float | None,
) -> list[CausalAttribution]:
    if not current_candidate:
        return []
    direction = "positive" if delta_best is not None and delta_best > 0 else "negative" if delta_best is not None and delta_best < 0 else "neutral"
    mechanism = "Candidate change coincided with a measurable result shift." if result_value is not None else ""
    items = []
    for variable, old_value, new_value in _extract_changed_pairs(previous_candidate, current_candidate):
        items.append(
            CausalAttribution(
                variable=variable,
                old_value=old_value,
                new_value=new_value,
                direction=direction,
                confidence=0.35,
                mechanism=mechanism,
            )
        )
    return items


def _normalize_hypothesis_evidence(payload: Any, interpretation_payload: dict[str, Any]) -> list[HypothesisEvidence]:
    items = [
        item
        for item in (
            HypothesisEvidence.from_payload(entry)
            for entry in (payload if isinstance(payload, list) else [])
            if isinstance(entry, dict)
        )
        if item.hypothesis_id
    ]
    if items:
        return items
    derived = []
    for hypothesis_id in interpretation_payload.get("supported_hypotheses", []) or []:
        derived.append(HypothesisEvidence(hypothesis_id=str(hypothesis_id), relation="supports", strength=0.72))
    for hypothesis_id in interpretation_payload.get("refuted_hypotheses", []) or []:
        derived.append(HypothesisEvidence(hypothesis_id=str(hypothesis_id), relation="refutes", strength=0.8))
    for hypothesis_id in interpretation_payload.get("archived_hypotheses", []) or []:
        derived.append(HypothesisEvidence(hypothesis_id=str(hypothesis_id), relation="neutral", strength=0.45))
    return derived


def _normalize_knowledge_tension(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    normalized = {
        "has_conflict": bool(payload.get("has_conflict", False)),
        "conflicting_cards": _normalize_str_list(payload.get("conflicting_cards", [])),
        "reason": str(payload.get("reason") or "").strip(),
    }
    if normalized["conflicting_cards"]:
        normalized["has_conflict"] = True
    return normalized


def _compute_importance(
    *,
    result_value: float | None,
    delta_best: float | None,
    prediction_gap: float | None,
    novelty: float,
    hypothesis_evidence: list[HypothesisEvidence],
    knowledge_tension: dict[str, Any],
) -> float:
    result_impact = min(1.0, abs(delta_best or 0.0) / 20.0)
    surprise = min(1.0, abs(prediction_gap or 0.0) / 25.0)
    hypothesis_impact = min(1.0, sum(item.strength for item in hypothesis_evidence) / 2.0)
    knowledge_conflict = min(1.0, 0.5 if knowledge_tension.get("has_conflict") else 0.0 + 0.15 * len(knowledge_tension.get("conflicting_cards", [])))
    if result_value is None:
        result_impact *= 0.5
        surprise *= 0.5
    return round(
        _clip(
            0.30 * result_impact
            + 0.25 * surprise
            + 0.20 * _clip(novelty)
            + 0.15 * hypothesis_impact
            + 0.10 * knowledge_conflict
        ),
        4,
    )


def _build_variable_role_map(variables: list[dict[str, Any]]) -> dict[str, list[str]]:
    role_map: dict[str, list[str]] = {}
    for variable in variables:
        name = str(variable.get("name") or "").strip()
        role = str(variable.get("role") or "").strip()
        if name:
            role_map.setdefault(name.lower(), []).append(name)
        if role and name:
            role_map.setdefault(role.lower(), []).append(name)
    return role_map


def _card_target_variables(card: dict[str, Any], role_map: dict[str, list[str]]) -> list[str]:
    names = []
    for item in card.get("variables_affected", []) or []:
        names.extend(role_map.get(str(item).lower(), []))
    return _normalize_str_list(names)


def _card_top_values(card: dict[str, Any]) -> set[str]:
    preferred: set[str] = set()
    for evidence in card.get("evidence", []) or []:
        if not isinstance(evidence, dict):
            continue
        metadata = evidence.get("metadata", {})
        if not isinstance(metadata, dict):
            continue
        for value in metadata.get("top_values", []) or []:
            preferred.add(str(value).lower().strip())
    return preferred


def _node_contradicts_observation(
    node: SemanticNode,
    candidate: dict[str, Any],
    result_value: float | None,
    optimization_direction: str,
) -> bool:
    if result_value is None or node.rule_type not in {"chemical_effect", "override"}:
        return False
    variable = str(node.conditions.get("variable") or "").strip()
    value = str(node.conditions.get("value") or "").strip()
    if not variable or not value or str(candidate.get(variable)) != value:
        return False
    direction = str(node.conditions.get("direction") or "").strip().lower()
    baseline = _coerce_float(node.conditions.get("comparison_delta"), 0.0)
    if optimization_direction == "minimize":
        baseline *= -1.0
    if direction == "positive":
        return baseline > 0 and result_value < 0
    if direction == "negative":
        return baseline < 0 and result_value > 0
    return False


def _extract_changed_pairs(previous: dict[str, Any], current: dict[str, Any]) -> list[tuple[str, Any, Any]]:
    pairs = []
    for key in sorted(set(previous) | set(current)):
        if str(previous.get(key)) != str(current.get(key)):
            pairs.append((key, previous.get(key), current.get(key)))
    return pairs


def _count_candidate_changes(previous: dict[str, Any], current: dict[str, Any]) -> int:
    return len(_extract_changed_pairs(previous, current))


def _delta_best(best_before: float | None, result_value: float | None, optimization_direction: str) -> float | None:
    if best_before is None or result_value is None:
        return None
    if str(optimization_direction).lower() == "minimize":
        return best_before - result_value
    return result_value - best_before


def _same_value_effect(node_a: SemanticNode, node_b: SemanticNode) -> bool:
    if node_a.rule_type != node_b.rule_type:
        return False
    return (
        str(node_a.conditions.get("variable") or "") == str(node_b.conditions.get("variable") or "")
        and str(node_a.conditions.get("value") or "") == str(node_b.conditions.get("value") or "")
        and str(node_a.conditions.get("direction") or node_a.conditions.get("effect_direction") or "")
        == str(node_b.conditions.get("direction") or node_b.conditions.get("effect_direction") or "")
    )


def _contradicting_effect(node_a: SemanticNode, node_b: SemanticNode) -> bool:
    if node_a.rule_type != node_b.rule_type:
        return False
    if str(node_a.conditions.get("variable") or "") != str(node_b.conditions.get("variable") or ""):
        return False
    if str(node_a.conditions.get("value") or "") != str(node_b.conditions.get("value") or ""):
        return False
    direction_a = str(node_a.conditions.get("direction") or node_a.conditions.get("effect_direction") or "")
    direction_b = str(node_b.conditions.get("direction") or node_b.conditions.get("effect_direction") or "")
    return bool(direction_a and direction_b and direction_a != direction_b)


def _value_similarity(value_a: Any, value_b: Any, variable_spec: dict[str, Any]) -> float:
    if value_a is None or value_b is None:
        return 0.0
    if str(value_a) == str(value_b):
        return 1.0
    num_a = _coerce_float(value_a)
    num_b = _coerce_float(value_b)
    if num_a is not None and num_b is not None:
        domain = variable_spec.get("domain", []) if isinstance(variable_spec, dict) else []
        span = 1.0
        if isinstance(domain, list) and len(domain) >= 2:
            low = _coerce_float(domain[0])
            high = _coerce_float(domain[1])
            if low is not None and high is not None and high > low:
                span = high - low
        return max(0.0, 1.0 - abs(num_a - num_b) / max(span, 1e-6))
    smiles_a = _resolve_smiles(value_a, variable_spec)
    smiles_b = _resolve_smiles(value_b, variable_spec)
    if smiles_a and smiles_b:
        similarity = _smiles_similarity(smiles_a, smiles_b)
        if similarity is not None:
            return similarity
    tokens_a = set(_normalize_text(str(value_a)).split())
    tokens_b = set(_normalize_text(str(value_b)).split())
    if tokens_a and tokens_b:
        return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)
    return 0.0


def _resolve_smiles(value: Any, variable_spec: dict[str, Any]) -> str | None:
    mapping = variable_spec.get("smiles_map", {}) if isinstance(variable_spec, dict) else {}
    if isinstance(mapping, dict):
        mapped = mapping.get(str(value))
        if mapped:
            return str(mapped)
    raw = str(value or "").strip()
    if not raw or Chem is None:
        return None
    if any(char in raw for char in "=#[]()"):
        return raw
    return None


def _smiles_similarity(smiles_a: str, smiles_b: str) -> float | None:
    if Chem is None or AllChem is None or DataStructs is None:
        return None
    mol_a = Chem.MolFromSmiles(smiles_a)
    mol_b = Chem.MolFromSmiles(smiles_b)
    if mol_a is None or mol_b is None:
        return None
    fp_a = AllChem.GetMorganFingerprintAsBitVect(mol_a, 2, 2048)
    fp_b = AllChem.GetMorganFingerprintAsBitVect(mol_b, 2, 2048)
    return float(DataStructs.TanimotoSimilarity(fp_a, fp_b))


def _estimate_tokens(payload: Any) -> int:
    try:
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    except Exception:
        text = str(payload)
    return max(1, (len(text) + 3) // 4)


def _normalize_str_list(values: Any) -> list[str]:
    raw_values = values if isinstance(values, list) else [values]
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in raw_values:
        value = str(raw or "").strip()
        if not value or value in seen:
            continue
        normalized.append(value)
        seen.add(value)
    return normalized


def _normalize_text(text: str) -> str:
    import re

    cleaned = str(text or "").lower().strip()
    cleaned = re.sub(r"[^a-z0-9.%+\s]", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _coerce_float(value: Any, default: float | None = None) -> float | None:
    if value is None or isinstance(value, bool):
        return default
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(result):
        return default
    return result


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return min(max(float(value), low), high)


def _mean(values: list[float]) -> float:
    usable = [_coerce_float(value) for value in values]
    usable = [value for value in usable if value is not None]
    return sum(usable) / len(usable) if usable else 0.0


def _ts_to_iso(timestamp: float | None) -> str:
    if timestamp is None:
        return ""
    return datetime.fromtimestamp(float(timestamp)).isoformat()


def _iso_to_ts(raw: str) -> float | None:
    raw = str(raw or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).timestamp()
    except ValueError:
        return None
