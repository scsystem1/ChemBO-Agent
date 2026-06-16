# ChemBO Bias Mitigation —— Codex 实施 Spec

针对 Knowledge prior 过度自信、Warm start LLM 偏见、Memory consolidation 虚假因果固化三类问题的最小外科手术式改动。所有改动均为 `str_replace` 风格，给出精确锚点。

执行顺序按下方编号即可，互相之间无依赖。每条改动末尾标了**验证要点**，用于改完后快速 sanity check。

---

## 改动 1：Knowledge prior writer 加 LANGUAGE DISCIPLINE 段

**文件**：`knowledge/prompts.py`
**函数**：`build_prior_writer_prompt`
**目的**：在 system prompt 末尾追加一段语言纪律约束，禁止 LLM 在 card.text 中使用绝对化词汇，并要求低 confidence card 显式包含 hedge token。

**old_str**：

```python
        "DOWNGRADE RULE: claims about a specific variable value being better or worse, interactions between two specific values, "
        "or analogies to another system must be card_type hypothesis with testable_prediction, not reagent_property or interaction. "
        "Use reagent_property or interaction only for broad claims expected to hold across the reaction class regardless of this specific substrate or system."
    )
```

**new_str**：

```python
        "DOWNGRADE RULE: claims about a specific variable value being better or worse, interactions between two specific values, "
        "or analogies to another system must be card_type hypothesis with testable_prediction, not reagent_property or interaction. "
        "Use reagent_property or interaction only for broad claims expected to hold across the reaction class regardless of this specific substrate or system. "
        "LANGUAGE DISCIPLINE: this prior is UNVERIFIED. Card.text must NEVER use absolute words: 'only', 'always', 'never', 'must', "
        "'strictly better', 'guaranteed', 'uniformly', 'outperforms all', 'the only viable', 'is optimal', 'is best'. "
        "Prefer hedged formulations: 'in many cross-coupling settings X tends to outperform Y', 'Pd is typically preferred over Ni for aryl chlorides, "
        "though specific substrates can reverse this', 'high temperature often improves rate but may degrade selectivity'. "
        "Every card with confidence below 0.45 MUST contain at least one explicit hedge token: 'tends to', 'typically', 'often', 'in many cases', "
        "'generally', 'may', 'likely', 'commonly', 'is usually'. Cards that violate the language discipline will be rejected by the parser."
    )
```

**验证要点**：跑一次 prior writer，抽样 5 张 reagent_property 或 operating_window card 的 text，肉眼检查不出现禁用词且有 hedge token。

---

## 改动 2：Confidence calibration 与上限同步收紧

包含两处修改：prompt 文案描述 + Python 端 clip 范围。

### 2A：prompt 文案改为更保守的 calibration

**文件**：`knowledge/prompts.py`
**函数**：`build_prior_writer_prompt`

**old_str**：

```python
        "CONFIDENCE CALIBRATION: assign confidence strictly by evidence strength. "
        "Use 0.55-0.60 for textbook-level facts broadly accepted across multiple sources; reserve this tier for mechanism and well-documented failure_mode cards. "
        "Use 0.40-0.54 for domain rules of thumb that are likely correct but have reaction-specific exceptions, such as operating_window and generic failure_mode cards. "
        "Use 0.30-0.39 for plausible claims with limited precedent; in this range, strongly prefer a hypothesis card with testable_prediction. "
        "Do not default to 0.45 for everything; spread confidence across the full range based on actual certainty. "
```

**new_str**：

```python
        "CONFIDENCE CALIBRATION: this LLM-internal prior is UNVERIFIED by any campaign observation. Confidence must reflect that uncertainty. "
        "Use 0.40-0.45 for textbook-level facts broadly accepted across multiple sources; reserve this top tier for mechanism cards and well-documented failure_mode cards. "
        "Use 0.35-0.39 for domain rules of thumb that are likely correct but have reaction-specific exceptions, such as operating_window and generic failure_mode cards. "
        "Use 0.30-0.34 for plausible claims with limited precedent; in this range, strongly prefer a hypothesis card with testable_prediction. "
        "Do not default to 0.40 for everything; spread confidence across this conservative range based on actual certainty. "
        "The 0.45+ band is reserved for cards that have been validated by at least one campaign observation; you cannot use it at this stage. "
```

### 2B：Python 端 clip 上限同步

**文件**：`knowledge/prior_writer.py`
**函数**：`_normalize_cards`

**old_str**：

```python
        confidence = _clip(float(raw.get("confidence", 0.45) or 0.45), 0.3, 0.6)
```

**new_str**：

```python
        confidence = _clip(float(raw.get("confidence", 0.40) or 0.40), 0.3, 0.45)
```

**验证要点**：跑一次 prior writer，所有 card 的 confidence 必须在 [0.30, 0.45]。0.45 以上的卡片应只能由后续 campaign 验证升上去。

---

## 改动 3：Memory `_statistical_consolidation` 加 blocking control

**文件**：`memory/memory_manager.py`
**方法**：`ConsolidationEngine._statistical_consolidation`
**目的**：在生成 chemical_effect rule 前，要求"在至少一个其它变量被控制相同值的子集内"也观察到一致方向的 effect，至少 2 组 blocked comparison 才允许形成 rule。同时把 confidence 公式上限从 0.92 降到 0.75，让 memory rule 永远低于 deck card promotion 阈值（0.80）。

**old_str**：

```python
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
```

**new_str**：

```python
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
        candidate_variables = sorted(grouped.keys())
        for variable, values in grouped.items():
            blocking_vars = [v for v in candidate_variables if v != variable]
            for value, matches in values.items():
                if len(matches) < 3:
                    continue
                other_episodes = [
                    episode
                    for episode in usable
                    if str(episode.candidate.get(variable)) != value and episode.result is not None
                ]
                if len(other_episodes) < 2:
                    continue
                match_results = [episode.result for episode in matches if episode.result is not None]
                marginal_diff = _mean(match_results) - _mean([e.result for e in other_episodes])
                if direction == "minimize":
                    marginal_diff *= -1.0
                marginal_effect_size = marginal_diff / max(spread, 1.0)
                if abs(marginal_effect_size) < 0.35:
                    continue
                blocked_effects = _blocked_effect_sizes(
                    matches=matches,
                    other_episodes=other_episodes,
                    blocking_vars=blocking_vars,
                    direction=direction,
                    spread=spread,
                )
                # Require at least 2 block-controlled comparisons before forming a
                # single-variable chemical_effect rule. Without blocking, a marginal
                # mean comparison in a multi-variable space is almost always
                # confounded by co-varying categorical values.
                if len(blocked_effects) < 2:
                    report.notes.append(
                        f"Skipped chemical_effect rule for {variable}={value}: only "
                        f"{len(blocked_effects)} block-controlled comparison(s) (need >=2)."
                    )
                    continue
                blocked_sorted = sorted(blocked_effects)
                mid = len(blocked_sorted) // 2
                if len(blocked_sorted) % 2 == 1:
                    effect_size = blocked_sorted[mid]
                else:
                    effect_size = 0.5 * (blocked_sorted[mid - 1] + blocked_sorted[mid])
                if abs(effect_size) < 0.30:
                    continue
                # Sign-consistency check: if the marginal estimate and the blocked
                # median disagree in sign, the marginal effect is almost certainly
                # driven by an unmodeled covariate. Skip rather than mislead.
                if marginal_effect_size * effect_size <= 0:
                    report.notes.append(
                        f"Skipped chemical_effect rule for {variable}={value}: marginal "
                        f"effect {marginal_effect_size:+.2f} disagrees in sign with blocked "
                        f"median {effect_size:+.2f} (likely confounded)."
                    )
                    continue
                rule = SemanticNode(
                    id=f"R{semantic_graph._next_index()}",
                    rule_type="chemical_effect",
                    statement=(
                        f"{variable}={value} shows a {'positive' if effect_size > 0 else 'negative'} "
                        f"effect in this campaign (block-controlled effect_size={effect_size:+.2f}, "
                        f"{len(blocked_effects)} blocked comparison(s))"
                    ),
                    variables=[variable],
                    conditions={
                        "variable": variable,
                        "value": value,
                        "direction": "positive" if effect_size > 0 else "negative",
                        "effect_size": round(effect_size, 4),
                        "marginal_effect_size": round(marginal_effect_size, 4),
                        "blocked_comparison_count": len(blocked_effects),
                        "evidence_basis": "block-controlled comparison",
                    },
                    # Confidence cap = 0.75 keeps memory-derived rules below the
                    # deck-card promotion threshold (0.80 in _promote_memory_rules_to_cards),
                    # so statistical consolidation can never auto-promote to a knowledge card.
                    confidence=min(
                        0.75,
                        0.30
                        + 0.04 * len(match_results)
                        + 0.05 * len(blocked_effects)
                        + 0.10 * min(abs(effect_size), 1.0),
                    ),
                    evidence_count=len(match_results),
                    supporting_episode_ids=[episode.id for episode in matches],
                    status="active" if len(blocked_effects) >= 3 else "tentative",
                    source="consolidation",
                    created_at_iteration=int(state.get("iteration", 0) or 0),
                    last_validated=int(state.get("iteration", 0) or 0),
                )
                node, outcome = semantic_graph.add_rule(rule)
                if outcome == "added":
                    report.record_new_rule(node)
                else:
                    report.record_updated_rule(node)
```

### 3B：新增模块级辅助函数 `_blocked_effect_sizes`

**文件**：`memory/memory_manager.py`
**位置**：在文件末尾的 `_mean` 函数定义**之前**插入。

**锚点 old_str**（仅用于定位，不替换）：

```python
def _mean(values: list[float]) -> float:
    usable = [_coerce_float(value) for value in values]
    usable = [value for value in usable if value is not None]
    return sum(usable) / len(usable) if usable else 0.0
```

**操作**：在上面这个 `_mean` 函数定义的**正上方**插入以下函数（保留 `_mean` 不动）：

```python
def _blocked_effect_sizes(
    *,
    matches: list[Episode],
    other_episodes: list[Episode],
    blocking_vars: list[str],
    direction: str,
    spread: float,
) -> list[float]:
    """Compute (match - non-match) effect sizes within blocks defined by holding
    each blocking variable fixed.

    For each blocking variable, group all episodes by its value. Within each
    group that contains at least one match and one non-match episode, the
    within-group effect is (mean(match results) - mean(non-match results)) / spread,
    sign-flipped for minimize. The returned list is the set of these
    block-controlled effect sizes across every blocking variable and every
    populated block.

    A non-empty list provides evidence that the variable=value association is
    not driven entirely by co-variation with a single other variable.
    """
    effects: list[float] = []
    for block_var in blocking_vars:
        groups: dict[str, dict[str, list[float]]] = {}
        for episode in matches:
            block_value = str(episode.candidate.get(block_var))
            payload = groups.setdefault(block_value, {"match": [], "other": []})
            payload["match"].append(float(episode.result))
        for episode in other_episodes:
            block_value = str(episode.candidate.get(block_var))
            payload = groups.setdefault(block_value, {"match": [], "other": []})
            payload["other"].append(float(episode.result))
        for payload in groups.values():
            if not payload["match"] or not payload["other"]:
                continue
            diff = _mean(payload["match"]) - _mean(payload["other"])
            if direction == "minimize":
                diff *= -1.0
            effects.append(diff / max(spread, 1.0))
    return effects
```

**验证要点**：跑一个 DAR 短 campaign，观察 `maintenance_report.notes` 是否出现 "Skipped chemical_effect rule for ... only X block-controlled comparison(s)" —— 出现说明 blocking 在工作；所有生成的 chemical_effect rule 的 confidence 应 ≤ 0.75。

---

## 改动 4：`_llm_abstraction` 输出加 confidence 硬过滤

**文件**：`memory/memory_manager.py`
**方法**：`ConsolidationEngine._llm_abstraction`
**目的**：即使 LLM 在 prompt 约束下仍输出 chemical_effect rule with confidence 0.9，Python 端无条件 cap 到 0.55；带 confound_note 的进一步 cap 到 0.40。

**old_str**：

```python
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
```

**new_str**：

```python
        for item in payload.get("new_rules", []):
            if not isinstance(item, dict):
                continue
            item = dict(item)
            proposed_type = str(item.get("rule_type") or "").strip().lower()
            try:
                proposed_conf = float(item.get("confidence") or 0.0)
            except (TypeError, ValueError):
                proposed_conf = 0.0
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            confound_note = str((metadata or {}).get("confound_note") or "").strip()
            # Hard caps by rule_type. The LLM frequently self-assigns high confidence
            # to single-variable chemical_effect claims derived from confounded
            # evidence; cap it independently of the prompt-level constraint.
            if proposed_type == "chemical_effect":
                capped_conf = min(proposed_conf, 0.55)
            elif proposed_type in {"interaction", "override"}:
                capped_conf = min(proposed_conf, 0.70)
            else:
                capped_conf = min(proposed_conf, 0.75)
            # Any rule that the LLM itself flagged as confounded must stay tentative.
            if confound_note:
                capped_conf = min(capped_conf, 0.40)
            if capped_conf < proposed_conf:
                report.notes.append(
                    f"Capped LLM-proposed {proposed_type or 'rule'} confidence "
                    f"{proposed_conf:.2f} -> {capped_conf:.2f}"
                    + (f" (confound_note: {confound_note[:60]})" if confound_note else "")
                )
            item["confidence"] = capped_conf
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
```

**验证要点**：在一次 milestone consolidation 后检查 `report.notes`，应该能看到至少一次 "Capped LLM-proposed ... confidence" 的记录（如果 LLM 自评高于 cap 的话）；所有 LLM-source rule 的 confidence 必须 ≤ 0.75。

---

## 改动 5：Warm-start LLM prompt 改为 60/40 利用/探索导向

包含 structured prompt 和 candidate-pool fallback prompt 两处对称修改。两处都用相同的 task 段落替换。

### 5A：structured prompt 的 Task 段

**文件**：`core/warm_start.py`
**函数**：`_build_warm_start_direct_structured_prompt`

**old_str**：

```python
Task:
- Return exactly {target} new direct warm-start recommendation(s); this is part of a total direct LLM allocation of {total_direct_target}.
- Prioritize the experiments that look most valuable to run early from chemical reasoning.
- You may consider diversity between recommendations, but high expected value is more important than forced diversity.
- Do not refer to BO, surrogate predictions, acquisition scores, or ranked planner indices.
- Use active knowledge cards and active hypotheses as selection evidence; make every recommendation traceable to at least one card or hypothesis when possible.
- Every recommendation must be legal, unseen, and non-duplicate.
```

**new_str**：

```python
Task:
- Return exactly {target} new direct warm-start recommendation(s); this is part of a total direct LLM allocation of {total_direct_target}.
- This is WARM START. The goal is to give the surrogate model a well-spread, informative initial dataset, NOT to maximize early outcomes. Mix exploitation and exploration.
- TARGET BLEND (aim for roughly 60% exploit / 40% explore across your picks):
  * "exploit" picks: conditions you have high chemistry confidence are productive, grounded in active knowledge cards or active hypotheses. Use these to anchor regions you believe are strong.
  * "explore" picks: chemically plausible but less-tested conditions whose outcome you are genuinely uncertain about, OR conditions specifically chosen to test an active hypothesis. A failed explore pick is informative because it constrains the surrogate model and refutes a candidate prior.
- For each selection, add a "purpose" field set to either "exploit" or "explore" so the planner can audit the blend. If your high-confidence anchors are few, lean toward more explore picks rather than re-using a single anchor.
- Categorical diversity matters: do not use the same value of any single categorical variable in more than half of your picks.
- Do not refer to BO, surrogate predictions, acquisition scores, or ranked planner indices.
- Use active knowledge cards and active hypotheses as selection evidence; every exploit pick should cite at least one card or hypothesis. Explore picks may instead cite explicit uncertainty (e.g., "this ligand class is under-represented in observed data").
- Every recommendation must be legal, unseen, and non-duplicate.
```

### 5B：structured prompt 的 schema 注释也补一句

**文件**：`core/warm_start.py`
**函数**：`_build_warm_start_direct_structured_prompt`

**old_str**：

```python
Each item in "selections" must follow this single-experiment schema:
{structured_spec.get("output_schema", "{}")}
```

**new_str**：

```python
Each item in "selections" must follow this single-experiment schema, with one extra required key "purpose" set to "exploit" or "explore":
{structured_spec.get("output_schema", "{}")}
```

### 5C：candidate-pool fallback prompt 的 Task 段

**文件**：`core/warm_start.py`
**函数**：`_build_warm_start_direct_candidate_pool_prompt`

**old_str**：

```python
Task:
- Return exactly {target} candidate id(s); this is part of a total direct LLM allocation of {total_direct_target}.
- Prioritize the experiments that look most valuable to run early from chemical reasoning.
- Diversity is useful but not mandatory.
- Do not refer to BO, surrogate predictions, acquisition scores, or ranked planner indices.
- Use active knowledge cards and active hypotheses as selection evidence when possible.
```

**new_str**：

```python
Task:
- Return exactly {target} candidate id(s); this is part of a total direct LLM allocation of {total_direct_target}.
- This is WARM START. The goal is to give the surrogate model a well-spread, informative initial dataset, NOT to maximize early outcomes. Mix exploitation and exploration.
- TARGET BLEND (aim for roughly 60% exploit / 40% explore across your selected ids):
  * "exploit" picks: candidates you have high chemistry confidence will perform well, grounded in active knowledge cards or active hypotheses.
  * "explore" picks: chemically plausible but less-tested candidates whose outcome you are genuinely uncertain about, OR candidates specifically chosen to test an active hypothesis. A failed explore pick still constrains the surrogate model.
- In "reasoning_by_id" you must label each chosen id with either "[exploit]" or "[explore]" as the first token of its rationale, so the planner can audit the blend.
- If your high-confidence anchors are few, lean toward more explore picks rather than re-using one anchor.
- Categorical diversity matters: avoid having the same value of any single categorical variable appear in more than half of your picks.
- Do not refer to BO, surrogate predictions, acquisition scores, or ranked planner indices.
- Use active knowledge cards and active hypotheses as selection evidence when possible.
```

### 5D：candidate-pool prompt 的 JSON 示例补 reasoning 标签提示

**文件**：`core/warm_start.py`
**函数**：`_build_warm_start_direct_candidate_pool_prompt`

**old_str**：

```python
Return strict JSON:
{{
  "strategy_summary": "...",
  "selected_ids": [1, 2],
  "reasoning_by_id": {{"1": "...", "2": "..."}},
  "confidence": 0.6
}}"""
```

**new_str**：

```python
Return strict JSON (each reasoning_by_id value must start with "[exploit]" or "[explore]"):
{{
  "strategy_summary": "...",
  "selected_ids": [1, 2],
  "reasoning_by_id": {{"1": "[exploit] ...", "2": "[explore] ..."}},
  "confidence": 0.6
}}"""
```

**验证要点**：跑一次 warm-start，从 `direct_records` 的 `warm_start_rationale` 字段抽样，检查文案是否能看出 exploit/explore 区分；用 grep 找 prompt 里有"60% exploit / 40% explore"字符串确认替换生效。注意：prompt 端是软指导，**Python 端的硬兜底见改动 5E**。

---

### 5E：Python 端 enforce 至少 3 个 explore picks 硬配额

**文件**：`core/warm_start.py`
**位置**：`_select_llm_direct_warm_start_records` 函数末尾（在它把 LLM 解析后的 records list 返回给 `plan_warm_start` 之前）。Codex 需要根据实际函数边界定位 return 语句前的位置。

**目的**：prompt 端是软指导（"aim for roughly 60% exploit / 40% explore"），但 Python 端有硬兜底——如果 LLM 输出的 `purpose=explore` picks 数 < 3 且 LLM direct records 总数 ≥ 5，则把 LLM 标记 `purpose=exploit` 的 records 中 LLM 自评最末几条**重 label 为 explore**。

**关键设计决策——为什么是 relabel 而非真正换 candidates**：

1. **不破坏 LLM 的化学合理性筛选**：LLM 已选 candidates 都是它认为化学上有意义的；硬把它们替换成 "maximin-distance 最远但化学上不一定合理"的 candidates 会反过来损害 warm-start 的化学质量。
2. **真实 explore 由 stratified coverage fill 承担**：warm-start 的另一半（random_target）已经在改动方案外的现有逻辑里通过 stratified fill 补充未覆盖的 categorical 值——这才是 candidate 层的真实 explore。LLM picks 这边的"3 个 explore 配额"实际是 reasoning/labeling 层的硬约束，保证后续 postmortem、interpret_results 等 prompt 阅读这批 records 时能看到 explore 标签。
3. **不需要碰复杂的 candidate selection/合法性校验逻辑**：保持 surgical。

**实施指引**：在 `_select_llm_direct_warm_start_records` 装配完 records 列表（每个 element 为 dict，至少含 `candidate`、`warm_start_rationale`、`purpose` 等字段）之后、return 之前，调用下面这个新增辅助函数：

```python
def _enforce_explore_quota(
    records: list[dict[str, Any]],
    *,
    min_explore: int = 3,
) -> dict[str, int]:
    """Hard floor on explore-purpose picks among LLM direct records.

    When the LLM selects fewer than ``min_explore`` records with
    ``purpose == "explore"``, demote the lowest-ranked exploit records (those
    nearest the tail of the LLM-returned ordering, which tends to reflect the
    LLM's own priority) to explore. The candidate itself is unchanged; only the
    label and rationale prefix are rewritten. Returns an audit summary.

    The quota is suspended when the list is shorter than 5, because at that
    scale every pick is effectively an explore anyway.
    """
    if len(records) < 5:
        return {"explore_before": 0, "explore_after": 0, "relabeled": 0, "skipped": True}

    def _purpose(record: dict[str, Any]) -> str:
        return str(record.get("purpose") or "").strip().lower()

    explore_before = sum(1 for r in records if _purpose(r) == "explore")
    if explore_before >= min_explore:
        return {
            "explore_before": explore_before,
            "explore_after": explore_before,
            "relabeled": 0,
            "skipped": False,
        }

    needed = min_explore - explore_before
    relabeled = 0
    for record in reversed(records):
        if needed == 0:
            break
        if _purpose(record) != "exploit":
            continue
        record["purpose"] = "explore"
        record["warm_start_audit_note"] = (
            "relabeled exploit->explore by Python audit to satisfy "
            f"explore quota >= {min_explore}"
        )
        existing_rationale = str(record.get("warm_start_rationale") or "").strip()
        if existing_rationale and not existing_rationale.startswith("[relabel:"):
            record["warm_start_rationale"] = (
                f"[relabel: exploit->explore for explore-quota] {existing_rationale}"
            )
        needed -= 1
        relabeled += 1

    return {
        "explore_before": explore_before,
        "explore_after": explore_before + relabeled,
        "relabeled": relabeled,
        "skipped": False,
    }
```

**调用方式**：在 `_select_llm_direct_warm_start_records` 把 records 返回给 `plan_warm_start` 之前调用：

```python
audit_summary = _enforce_explore_quota(records, min_explore=3)
# 把 audit_summary 写到 state 或日志,便于回归验证;具体写法依现有状态传递机制而定。
# 若该函数已经把 records 写入 state["warm_start_direct_records"],可加一行:
# state.setdefault("warm_start_audit", {})["explore_quota"] = audit_summary
```

**candidate-pool fallback 路径**：如果 `_select_llm_direct_warm_start_records` 内部存在 structured / candidate-pool 两条分支，分别有独立的 records 装配点，需要在**两条分支**最终装配完 records 之后都调用一次 `_enforce_explore_quota`。Codex 实施时请确认两条分支都覆盖。

**为什么从尾部反向 demote**：LLM 通常按它自评的"价值优先级"排列 selections，越往后越是它自己也没那么确信的 exploit 选项。demote 最末的 exploit 比 demote 首位的损失最小。

**验证要点**：跑一次 DAR/OCM warm-start，检查日志或 state 中的 audit_summary：

| LLM 输出 explore 数 | 期望 audit_summary |
|---|---|
| ≥ 3 | `relabeled=0`, `explore_after = explore_before` |
| < 3 且 records ≥ 5 | `relabeled = 3 - explore_before`, `explore_after = 3` |
| records < 5 | `skipped=True` |

被 relabel 的 records 应有 `warm_start_audit_note` 字段，且 `warm_start_rationale` 以 `[relabel: exploit->explore for explore-quota]` 开头。

---

## 改动 6：Warm-start postmortem 加 confidence cap，自动禁止升级 card

包含 prompt 端的硬上限说明 + Python 端的 confidence cap enforce。Python 端 cap 0.55 < `_promote_memory_rules_to_cards` 的 0.80 promotion 阈值，所以 postmortem rule 物理上不可能被升级为 deck card。

### 6A：postmortem prompt 加 [WARM-START EVIDENCE STRENGTH] 段

**文件**：`core/warm_start.py`
**函数**：`run_warm_start_postmortem`

**old_str**：

```python
4. A single-variable chemical_effect rule needs isolated or near-isolated support; otherwise keep it tentative, low-confidence, and mark the evidence as confounded in metadata.evidence_basis.
5. Never claim that a value is the only viable option or should be permanently excluded based solely on sparse warm-start data.

Return strict JSON:
```

**new_str**：

```python
4. A single-variable chemical_effect rule needs isolated or near-isolated support; otherwise keep it tentative, low-confidence, and mark the evidence as confounded in metadata.evidence_basis.
5. Never claim that a value is the only viable option or should be permanently excluded based solely on sparse warm-start data.

[WARM-START EVIDENCE STRENGTH]
Warm-start observations are by design sparse, varied, and high-variance. They cannot support strong causal claims.
- NO rule extracted from warm-start alone may have confidence above 0.55. Values above 0.55 will be capped down silently by the parser.
- NO chemical_effect rule (single-variable causal claim) may have confidence above 0.45.
- Interaction and strategy rules may go up to 0.55 because they admit configuration-level evidence.
- Warm-start rules will NEVER be promoted to active knowledge cards regardless of confidence; treat them as soft memory only.

Return strict JSON:
```

### 6B：Python 端 enforce confidence cap

**文件**：`core/warm_start.py`
**函数**：`run_warm_start_postmortem`

**old_str**：

```python
    added_rule_count = 0
    for rule_payload in parsed.get("semantic_rules", []):
        if not isinstance(rule_payload, dict) or not str(rule_payload.get("statement") or "").strip():
            continue
        memory_manager.add_semantic_rule(
            {
                **rule_payload,
                "source": "warm_start_postmortem",
                "created_at_iteration": int(state.get("iteration", 0) or 0),
                "last_validated": int(state.get("iteration", 0) or 0),
            }
        )
        added_rule_count += 1
```

**new_str**：

```python
    added_rule_count = 0
    for rule_payload in parsed.get("semantic_rules", []):
        if not isinstance(rule_payload, dict) or not str(rule_payload.get("statement") or "").strip():
            continue
        rule_payload = dict(rule_payload)
        try:
            raw_conf = float(rule_payload.get("confidence") or 0.0)
        except (TypeError, ValueError):
            raw_conf = 0.0
        proposed_type = str(rule_payload.get("rule_type") or "").strip().lower()
        # Warm-start observations are sparse and high-variance. Cap confidence
        # below the deck-card promotion threshold (0.80) so postmortem rules
        # can never auto-promote into the active knowledge deck.
        if proposed_type == "chemical_effect":
            capped_conf = min(raw_conf, 0.45)
        else:
            capped_conf = min(raw_conf, 0.55)
        rule_payload["confidence"] = capped_conf
        memory_manager.add_semantic_rule(
            {
                **rule_payload,
                "source": "warm_start_postmortem",
                "created_at_iteration": int(state.get("iteration", 0) or 0),
                "last_validated": int(state.get("iteration", 0) or 0),
            }
        )
        added_rule_count += 1
```

**验证要点**：跑一次完整的 warm-start + postmortem 流程，检查 `semantic_graph` 中所有 `source="warm_start_postmortem"` 的 node：chemical_effect 的 confidence 必须 ≤ 0.45，其它类型 ≤ 0.55；后续 iteration 的 `knowledge_deck.cards` 中不应出现 `source_type` 关联到 warm-start postmortem 的卡片。

---

## 完成后的整体回归测试建议

跑一次 DAR 短 campaign（30 iter）和一次 OCM 短 campaign（30 iter），抓取以下指标对比改动前后：

1. **Prior writer 输出**：所有 card 的 confidence 必须落在 [0.30, 0.45]；text 不出现禁用绝对词；低 confidence card 包含 hedge token。
2. **Statistical consolidation 输出**：observe `report.notes` 中 "Skipped chemical_effect rule" 的频次；改动后应当频繁出现（说明 blocking 在过滤虚假因果）。
3. **LLM abstraction 输出**：observe `report.notes` 中 "Capped LLM-proposed ... confidence" 的频次；有非零次数说明硬过滤在工作。
4. **Warm-start picks（prompt 软约束）**：日志里 `warm_start_rationale` 抽样应能区分 exploit/explore。
5. **Warm-start explore 硬配额（Python audit）**：state 或日志中应能找到 `warm_start_audit.explore_quota` 字段，且 `explore_after >= 3`（当 records ≥ 5 时）。如果 LLM 自然就给够了 3 个 explore，`relabeled=0`；否则有非零 relabel 数量且对应 records 带 `warm_start_audit_note` 字段。
6. **Warm-start postmortem rules**：semantic graph 中 source=warm_start_postmortem 的所有 node confidence ≤ 0.55；不出现在 `knowledge_deck`。
7. **下游影响**：iteration 8-15 期间的 BO 表现（best-so-far 增长曲线、stagnation_length）改动后应不弱于改动前。如果出现明显回退，重点排查改动 3（blocking 是否过严，可考虑把"need >=2"改成"need >=1"）。

---

## 注意事项

- **改动 3 是其中改动量最大的一处**，建议先在 `git` 上单独拉一个分支跑两个 seed 验证不退化再 merge。
- 改动 5 分两层：5A-5D 是 prompt 端的 60:40 软指导（不修改 Python 解析逻辑，LLM 输出的 `purpose` 字段 / `[exploit]/[explore]` 标签由现有 record 装配逻辑透传）；5E 是 Python 端的硬配额兜底，只 relabel 不换 candidate，保证下游永远看到 ≥ 3 个 explore 标签。两层互补：prompt 软指导确保 LLM 在 candidate 选择时主动考虑 explore，Python 硬配额确保哪怕 LLM 完全无视 prompt 也有标签层兜底。
- 改动 5E **不修改 candidate selection 逻辑**——真实的 candidate 多样性兜底由现有的 stratified coverage fill（warm-start 的另一半 random_target）承担。
- 改动 6 通过 confidence cap 而非新增分支隔离实现"禁止升级 card"，符合最小改动原则。`_promote_memory_rules_to_cards` 完全不需要改。
- 所有改动后保留现有 fallback 路径不变（例如改动 5C 在 LLM 不输出 purpose 标签时不会让 Python 报错，5E 的 `_enforce_explore_quota` 也对缺失 `purpose` 字段安全——`_purpose` 返回空串，既不被记为 explore 也不被 demote）。
