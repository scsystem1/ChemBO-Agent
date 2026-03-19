"""
ChemBO Agent — Three-Layer Memory System
==========================================
Working Memory: Short-term scratch-pad for current reasoning cycle
Episodic Memory: Per-iteration records (what happened + reflection)
Semantic Memory: Abstracted, reusable rules distilled from experience

Design inspired by ChemAgent (ICLR 2025) and Reflexion (NeurIPS 2023),
adapted for iterative BO campaigns where memory accumulates across rounds.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional
from dataclasses import dataclass, field, asdict


# ============================================================================
# Memory Manager
# ============================================================================
class MemoryManager:
    """Manages the three-layer memory system for a BO campaign."""
    
    def __init__(self, capacity: int = 200):
        self.capacity = capacity
        self.working: dict[str, Any] = {}
        self.episodic: list[dict] = []
        self.semantic: list[dict] = []
    
    # --- Working Memory ---
    
    def update_working(self, key: str, value: Any):
        """Update a working memory slot."""
        self.working[key] = value
    
    def get_working(self, key: str, default=None) -> Any:
        return self.working.get(key, default)
    
    def clear_working(self):
        self.working = {}
    
    # --- Episodic Memory ---
    
    def add_episode(
        self,
        iteration: int,
        config_snapshot: dict,
        candidate: dict,
        result: Optional[float],
        reflection: str,
        non_numerical_observations: str = "",
        lesson_learned: str = "",
    ):
        """Record one experimental episode."""
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
        
        # Evict oldest if over capacity
        if len(self.episodic) > self.capacity:
            self.episodic = self.episodic[-self.capacity:]
    
    def get_recent_episodes(self, n: int = 5) -> list[dict]:
        return self.episodic[-n:]
    
    def get_best_episodes(self, n: int = 5) -> list[dict]:
        """Return episodes with the highest results."""
        sorted_eps = sorted(
            [e for e in self.episodic if e.get("result") is not None],
            key=lambda x: x["result"],
            reverse=True,
        )
        return sorted_eps[:n]
    
    # --- Semantic Memory ---
    
    def add_semantic_rule(
        self,
        rule: str,
        confidence: float,
        source_iterations: list[int],
    ):
        """Add a reusable rule distilled from episodic experience.
        
        Rules should be specific and actionable, e.g.:
        - "Cs2CO3 + DMAc consistently yields >70% for this substrate class"
        - "XPhos outperforms PPh3 when steric bulk is moderate"
        - "Temperature above 130°C causes decomposition"
        """
        # Check for duplicates / conflicting rules
        for existing in self.semantic:
            if _rules_are_similar(existing["rule"], rule):
                # Update existing rule
                existing["confidence"] = max(existing["confidence"], confidence)
                existing["evidence_count"] += 1
                existing["source_iterations"].extend(source_iterations)
                existing["last_updated"] = datetime.now().isoformat()
                return
        
        self.semantic.append({
            "rule": rule,
            "confidence": confidence,
            "evidence_count": 1,
            "source_iterations": source_iterations,
            "created_at": datetime.now().isoformat(),
            "last_updated": datetime.now().isoformat(),
        })
    
    def get_high_confidence_rules(self, threshold: float = 0.7) -> list[dict]:
        return [r for r in self.semantic if r["confidence"] >= threshold]
    
    def get_all_rules(self) -> list[dict]:
        return sorted(self.semantic, key=lambda x: x["confidence"], reverse=True)
    
    # --- Consolidation: Episodic → Semantic ---
    
    def consolidate(self, llm_consolidation_fn=None):
        """
        Attempt to distill new semantic rules from accumulated episodic memory.
        
        This is a key differentiator: active consolidation of experimental
        experience into reusable rules. In the full implementation, this
        calls the LLM to analyze patterns across episodes.
        
        TODO [codex]: Implement LLM-driven consolidation:
        1. Group episodes by variable values
        2. Identify recurring patterns (e.g., "base X always works better")
        3. Call LLM to articulate the pattern as a semantic rule
        4. Validate rule against evidence count
        """
        if llm_consolidation_fn:
            new_rules = llm_consolidation_fn(self.episodic, self.semantic)
            for rule_data in new_rules:
                self.add_semantic_rule(**rule_data)
            return

        if len(self.episodic) < 3:
            return

        # Lightweight deterministic consolidation for Phase 1:
        # convert repeated lessons into low-confidence semantic hints.
        lesson_groups: dict[str, list[int]] = {}
        for episode in self.episodic:
            lesson = str(episode.get("lesson_learned", "")).strip()
            if not lesson:
                continue
            key = _normalize_rule(lesson)
            if not key:
                continue
            lesson_groups.setdefault(key, []).append(int(episode.get("iteration", 0)))

        for normalized_lesson, iterations in lesson_groups.items():
            if len(iterations) < 2:
                continue
            exemplar = next(
                (
                    str(ep.get("lesson_learned", "")).strip()
                    for ep in self.episodic
                    if _normalize_rule(str(ep.get("lesson_learned", "")).strip()) == normalized_lesson
                ),
                "",
            )
            if exemplar:
                confidence = min(0.55 + 0.1 * len(iterations), 0.85)
                self.add_semantic_rule(exemplar, confidence, iterations)
    
    # --- Serialization ---
    
    def to_dict(self) -> dict:
        return {
            "working": self.working,
            "episodic": self.episodic,
            "semantic": self.semantic,
        }
    
    @classmethod
    def from_dict(cls, data: dict, capacity: int = 200) -> "MemoryManager":
        mm = cls(capacity=capacity)
        mm.working = data.get("working", {})
        mm.episodic = data.get("episodic", [])
        mm.semantic = data.get("semantic", [])
        return mm
    
    def get_context_for_llm(self, max_episodes: int = 5) -> str:
        """Format memory as a context string for LLM prompts."""
        parts = []
        
        if self.working:
            parts.append(f"[Working Memory]\n{json.dumps(self.working, indent=2)}")
        
        recent = self.get_recent_episodes(max_episodes)
        if recent:
            ep_strs = []
            for ep in recent:
                ep_strs.append(
                    f"  Iter {ep['iteration']}: "
                    f"result={ep.get('result', '?')}%, "
                    f"lesson={ep.get('lesson_learned', 'none')}"
                )
            parts.append(f"[Recent Episodes]\n" + "\n".join(ep_strs))
        
        rules = self.get_high_confidence_rules()
        if rules:
            rule_strs = [f"  [{r['confidence']:.1f}] {r['rule']}" for r in rules]
            parts.append(f"[Semantic Rules]\n" + "\n".join(rule_strs))
        
        return "\n\n".join(parts) if parts else "[Memory is empty — first iteration]"


# ============================================================================
# Helpers
# ============================================================================
def _rules_are_similar(rule1: str, rule2: str) -> bool:
    """Simple heuristic to detect duplicate/similar semantic rules.
    
    TODO [codex]: Replace with embedding-based similarity.
    """
    norm1 = _normalize_rule(rule1)
    norm2 = _normalize_rule(rule2)
    if not norm1 or not norm2:
        return False
    if norm1 == norm2:
        return True
    if norm1 in norm2 or norm2 in norm1:
        return True

    r1 = set(norm1.split())
    r2 = set(norm2.split())
    
    if not r1 or not r2:
        return False
    
    overlap = len(r1 & r2) / max(len(r1), len(r2))
    return overlap > 0.6


def _normalize_rule(rule: str) -> str:
    """Normalize a rule string for deterministic matching."""
    import re

    normalized = rule.lower().strip()
    normalized = re.sub(r"[^a-z0-9.%+\s]", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized
