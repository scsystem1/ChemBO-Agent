# ChemBO 初始 Knowledge 构建重审与改进方案

## 结论先行

我认为当前判断基本正确：现在的 knowledge 构建在形式上很大，在状态里也保存了很多 artifact，但真正能影响 warm start 和后续 AutoBO 的有效信号很少。它的问题不是“没有知识模块”，而是“知识没有被压缩成可行动、可消费、可更新的决策上下文”。

你提出的目标更接近应该做的系统形态：

- 初始 knowledge 应该给出关于反应机理、反应物/条件、操作窗口、失败模式、相似先例等有用背景。
- 注入 state 的不应该是一大堆检索过程残留，而应该是 8-12 条短小、明确、可引用的 knowledge card。
- 每张 card 最好既有一句人类可读文本，也有结构化 prior payload，能直接影响 warm start、candidate scoring、LLM acquisition 和 result interpretation。
- 卡片应该能动态进出 state。被实验反复否定、长时间未使用、低置信且不可行动的卡片应降权或移除；被实验支持的卡片应升级为 campaign memory 或更强 prior。

不过，仅仅把知识压成 10 句话还不够。更好的方案是保留“两层知识”：

1. **Active Knowledge Deck**：state 中常驻的 8-12 张短卡，直接进入 prompt 和排序逻辑。
2. **Evidence Bank / Retrieval Artifacts**：完整检索证据、source health、coverage report 放在 artifacts 或外部缓存中，仅用于追溯、刷新和调试，不默认注入上下文。

这样既能保留可追溯性，又不会让下游节点被大量无效内容淹没。

## 当前实现实际在做什么

当前主流程大致是：

1. `core/graph.py::parse_input` 调用 `run_knowledge_augmentation(...)`。
2. `knowledge/augmentation_pipeline.py` 生成 6-12 个 retrieval queries。
3. 多源检索 local RAG / PubChem / web。
4. 去重、leakage filter、snippet 压缩。
5. 将 snippet 转成 `EvidenceRecord`。
6. 从 target-scoped evidence 生成 `ServedPrior`。
7. 生成 `knowledge_cards`，并构建 `knowledge_digests`。
8. `ContextBuilder` 在不同节点取 node-specific digest 或 fallback card。
9. warm start 和 AutoBO shortlist 用 `score_candidate_with_priors(...)` 对候选加知识分。

这里面有一些值得保留的好设计：

- 已经有 facet 概念：mechanism、precedent、composition、operating window、selectivity、failure、scope、constraint。
- 已经有 leakage filter，避免把 benchmark 的目标结果泄露到优化器。
- 已经有 `served_priors` 和 `score_candidate_with_priors(...)`，说明系统开始区分“文本知识”和“机器可用 prior”。
- 已经有 node digest，说明它想做节点级别注入。

但当前实现偏离目标的地方也很明显。

## 关键问题

### 1. State 很大，但有效 active knowledge 很小

`ChemBOState` 同时保存：

- `knowledge_state`
- `knowledge_cards`
- `retrieval_artifacts`
- `kb_priors`
- `knowledge_serving_stats`

这些字段本身不一定有错，但它们现在混在主 state 里，让人误以为“知识很多”。实际上，后续节点主要通过 `ContextBuilder._knowledge_guidance(...)` 读取 `knowledge_state.knowledge_digests`。如果 digest 质量差或为空，才 fallback 到 `knowledge_cards`。

另外，`run_knowledge_augmentation(...)` 返回的 `kb_context = format_cards_for_context(card_payloads)` 在 `parse_input` 中被命名为 `_kb_context`，随后没有进入 state，也没有进入 prompt。这意味着“格式化后的卡片上下文”实际上被丢弃了。

### 2. 当前 retrieval 覆盖面看似广，实际命中很差

我用现有脚本检查了 DAR 和 OCM benchmark：

```text
python scripts/inspect_knowledge_build.py \
  --problem-file examples/dar_problem.yaml \
  --settings-yaml dashscope_kimi.yaml \
  --show-evidence 4 \
  --show-priors 8 \
  --show-cards 8 \
  --no-progress
```

DAR 输出摘要：

```text
queries: 12
evidence_records: 16
served_priors: 1
knowledge_cards: 5
coverage_gaps: 7
```

唯一 served prior 是 dataset constraint：

```text
kp_constraint_01: Only propose conditions that correspond to rows present in the DAR dataset.
```

大量 evidence 是 generic textbook 或相邻反应，例如 March's Advanced、Organotransition Metal Chemistry、BH ORD 条目。它们经常和 DAR 当前变量弱相关甚至无关。

OCM 输出摘要：

```text
queries: 12
evidence_records: 15
served_priors: 3
knowledge_cards: 7
coverage_gaps: 6
```

3 个 priors 也都是显式 constraints：

```text
Only propose conditions that correspond to rows present in the OCM dataset.
Maintain a plausible methane-rich OCM operating window.
Avoid support-only catalyst compositions where all three optional metal roles are vacant.
```

OCM 的 mechanistic cards 甚至检索到了有机/交叉偶联风格文本，例如 ligand、oxidative addition、enantioenriched product，明显不适合 OCM。

这说明当前 retrieval pipeline 的 query 数量不少，但 evidence selection 和 source/family gating 不够可靠。

### 3. 真正能影响 warm start 的是 served_priors，不是普通 card

warm start 的确定性排序主要看：

- `kb_priors["warm_start_bias"]`
- `knowledge_state["served_priors"]`
- `score_candidate_with_priors(candidate, served_priors, node_name="warm_start")`
- LLM 生成的 preferred/avoided patterns

而 `served_priors` 的生成非常保守：`serve_knowledge_priors(...)` 只从 `target_records` 中提取 value preference/window/interaction/failure。Analogous/general evidence 通常不会变成 prior。对于 DAR/OCM 检查结果，target precedent 几乎没有命中，所以除 constraints 外几乎没有可用 prior。

结果是：

- warm start 进入 `knowledge_gap` 或 `coverage_first`。
- positive knowledge score 基本为 0。
- 队列主要还是 coverage/diversity 驱动，而不是知识驱动。

### 4. Node digest 把很多可能有用的 card 排除在 warm start 外

`build_node_digests(...)` 会把 prior digest 发到 applicable nodes，但普通 evidence digest 进入 warm start 的条件很窄：

- failure evidence
- constraint evidence

mechanistic、composition、operating_window、selectivity 等 evidence 多数不会进入 warm start digest。也就是说，即便检索到“温度窗口”“溶剂影响”“配体类别偏好”，如果没被编译成 `ServedPrior`，也很难影响 warm start。

### 5. Card 文本和机器 prior 没有统一

当前 `KnowledgeCard` 是一段 claim 加 metadata；`ServedPrior` 是另一套结构。它们之间通过 compatibility cards 做了一些转换，但主逻辑仍然割裂。

问题包括：

- LLM card prompt 要求 `variables_affected` 使用 role，不是 variable name。
- candidate scoring 需要 variable name 才能匹配 candidate。
- `knowledge_cards` 中可能有看似可读的 claim，但没有对应的 action payload。
- `ServedPrior` 能用于 scoring，却不一定以清晰的 card 形式注入 prompt。

因此，人类可读知识和 BO 可用知识经常不同步。

### 6. 动态出入机制还没有真正落地

当前 memory system 能记录 observation，也能在 warm-start postmortem 里生成 semantic rules。但初始 knowledge cards 本身没有明显的生命周期：

- 没有按实验结果更新 card confidence。
- 没有记录 card 是否被使用、是否帮助提升结果。
- 没有因为 contradicted 或 stale 而从 active state 中删除。
- 没有把 validated campaign rule 晋升为新的 active card。

所以“错误或一直没用的 knowledge 可以直接删除出 state”这个构想还没有实现。

## 我对你方案的判断

你的方案比当前实现更好，原因是它直接抓住了 ChemBO 里 knowledge 的正确位置：knowledge 不应该只是 RAG artifact，而应该是“有限、可行动、可被检验的决策上下文”。

10 条左右的 card 有几个优点：

- prompt 成本低，几百词就能给 LLM 稳定背景。
- 每轮都可注入，不需要把大量 retrieval evidence 带进上下文。
- 更容易做动态维护：每张卡有 ID、状态、使用记录和 validation 记录。
- 更适合与 BO 结合：每张卡可以编译成一个 soft prior、avoidance、interaction hint 或 hypothesis。

但我建议不要只做纯文本 10 句话。最佳形态是：

```yaml
active_knowledge_deck:
  cards:
    - card_id: kc_temp_ocm_001
      text: "For OCM, higher temperature often increases methane activation but can increase COx risk; prefer testing high-temperature points only with methane-rich feeds."
      card_type: operating_window
      targets: [Temp, CH4_flow, O2_flow]
      action_payload:
        type: conditional_preference
        prefer:
          Temp: ["800", "850"]
        condition:
          CH4_O2_ratio: "methane_rich"
        avoid_if:
          O2_flow: "high"
      confidence: 0.55
      scope: general
      status: active
      evidence_refs: [ev_012, ev_019]
      actionable_for: [warm_start, select_candidate, result_interpretation]
      validation:
        used_count: 0
        supported_count: 0
        contradicted_count: 0
```

也就是说，state 中注入的是短文本，但系统内部保留结构化可执行部分。

## 建议的新架构：Active Knowledge Deck

### 核心原则

1. **Active deck 小而强**：state 中最多保留 8-12 张 active cards。
2. **每张 card 必须可追溯**：至少有 evidence refs 或明确说明是 heuristic/problem constraint。
3. **每张 card 尽量可行动**：能被 warm start、candidate selection 或 interpretation 使用。
4. **低质量知识不强行注入**：如果 retrieval 质量差，要显式进入 knowledge_gap，而不是塞无关 textbook 句子。
5. **卡片有生命周期**：active、tentative、validated、deprecated、rejected。
6. **文本与 prior 同源**：同一张 card 同时生成 prompt text 和 scoring payload，避免两套知识漂移。

### 推荐 state 结构

新增或替换为：

```yaml
knowledge_deck:
  active_cards: []
  inactive_cards: []
  max_active: 10
  build_summary:
    profile: heterogeneous_catalysis
    source_status: "offline_partial"
    coverage_level: "weak|partial|good"
    notes: []
  usage_log: []
```

每张 active card：

```yaml
card_id: "kc_..."
text: "一句话，最多 35-45 个英文词或一行中文。"
card_type: "mechanism|reagent_property|operating_window|failure_mode|constraint|analogy|interaction|hypothesis"
scope: "target|analogous|general|campaign"
confidence: 0.0
status: "active|tentative|validated|deprecated|rejected"
targets: ["variable_name_or_derived_target"]
actionable_for: ["hypothesis_generation", "warm_start", "select_candidate", "run_bo_iteration", "result_interpretation"]
action_payload:
  type: "preference|avoidance|window|interaction|constraint|hypothesis_only"
  data: {}
evidence_refs: []
source_quality:
  source_types: ["local_rag", "pubchem", "web", "problem_constraint", "campaign_observation"]
  family_match: "target|analogous|general"
validation:
  used_count: 0
  last_used_iter: null
  supported_count: 0
  contradicted_count: 0
  neutral_count: 0
  utility_score: 0.0
```

### Prompt 注入格式

每个节点只看到当前节点相关的 5-10 张卡：

```text
[Active Knowledge Cards]
KC1 confidence=0.62 scope=analogous targets=ligand_SMILES:
Bulky electron-rich phosphines can accelerate Pd C-H arylation but may be sensitive to base and solvent.

KC2 confidence=0.48 scope=general targets=temperature,concentration:
Higher temperature can improve activation but may increase decomposition or side reactions; sample both mid and high levels early.

KC3 confidence=1.00 scope=target targets=dataset:
Only propose candidates present in the benchmark dataset.
```

注意：这不是把 evidence dump 进 prompt，而是把 active deck 作为“当前世界模型”给节点。

## 改进后的构建流程

### Step 1: 先定义 card slots，而不是先堆 chunks

初始 knowledge build 应该明确想填哪些 slot：

1. 反应机理：关键步骤、rate/selectivity drivers。
2. 反应物/催化剂/配体/溶剂/支持体属性：为什么某些类别可能更好。
3. 操作窗口：温度、浓度、流量、时间、压力的合理区间或风险。
4. 失败模式：deactivation、over-oxidation、decomposition、poor solubility、side reactions。
5. 相似先例：target family 优先，analog family 其次。
6. Search-space constraints：dataset-backed feasibility、hard constraints。
7. Derived features：比如 OCM 的 CH4/O2 ratio、severity、catalyst tuple。

目标不是“找 60 个 chunk”，而是“填满 8-12 张可行动卡”。

### Step 2: Retrieval 要按 facet 和变量覆盖做配额

建议每个 facet 都有独立检索预算和最低质量门槛：

```yaml
facet_budgets:
  precedent: 4 target + 2 analogous
  mechanism: 3 review/textbook
  reagent_property: 2 per key role
  operating_window: 4
  failure_mode: 3
  constraint: all problem constraints
```

排序不应该只按 embedding/BM25 相似度，还要加：

- reaction family match
- variable/role match
- source type priority
- facet relevance
- has actionable variable value
- not generic textbook noise
- leakage safe

### Step 3: 对 target/analog/general 分层处理

当前 `serve_knowledge_priors(...)` 基本只从 target evidence 生成 value priors。这个策略太保守，导致没有 target evidence 时整个 knowledge 失效。

建议：

- target evidence 可以生成强 prior。
- analogous evidence 可以生成弱 prior，权重较低，并标明 `scope=analogous`。
- general mechanistic evidence 可以生成 hypothesis-only card，不参与强排序。
- generic textbook evidence 如果没有变量/机制命中，应直接丢弃。

### Step 4: Card synthesis 必须输出 action_payload

卡片合成时要求 LLM 或规则生成：

```json
{
  "text": "...",
  "card_type": "operating_window",
  "targets": ["temperature"],
  "action_payload": {
    "type": "window",
    "preferred_values": ["105", "120"],
    "avoid_values": [],
    "strength": 0.35
  },
  "evidence_refs": ["ev_004", "ev_009"]
}
```

如果没有 action payload，就只能进入 hypothesis_generation 或 result_interpretation，不能假装影响 warm start。

### Step 5: Active deck 编译成 prior cache

不要再让 `knowledge_cards` 和 `served_priors` 各自独立。应该从 active deck 编译出：

- `warm_start_bias`
- `avoidance_rules`
- `operating_windows`
- `interaction_hints`
- `hard_constraints`
- `hypothesis_cards`

这样 prompt 和 scoring 看到的是同一套知识。

### Step 6: Warm start 使用 active cards 的方式

warm start 应分成三层：

1. **Hard filter**：dataset feasibility、hard constraints、safety constraints。
2. **Coverage design**：保证变量空间覆盖。
3. **Knowledge bias**：根据 active cards 的 action payload 加 soft score。

建议 warm start queue 中每个候选记录：

```yaml
candidate: {}
warm_start_category: exploration|balanced|exploitation
applied_card_ids: []
card_score_breakdown:
  kc_temp_001: 0.25
  kc_failure_002: -0.40
knowledge_total: -0.15
selection_reason: "Coverage point retained despite risk; tests support effect at mid severity."
```

这样后续 observation 可以知道每个结果究竟检验了哪些 knowledge cards。

## 动态出入机制

每次实验结果返回后，对相关 card 做一次轻量更新。

### Card 使用判定

一个 card 被认为 used，如果：

- 它的 `targets` 与 candidate 中的变量相交。
- 或者它的 `action_payload` 对该 candidate 产生了非零 score。
- 或者 LLM acquisition 明确引用了该 card。

### Card 支持/反驳判定

根据 observation metadata 和结果判断：

- 如果 card 给候选正分，结果优于当前基线或预期：`supported_count += 1`
- 如果 card 给候选正分，但结果显著差：`contradicted_count += 1`
- 如果 card 给候选负分，结果仍然好：`contradicted_count += 1`
- 如果结果不显著：`neutral_count += 1`

这里可以先用简单规则，后续再让 result interpretation LLM 参与。

### Eviction 规则

建议每轮或每 3 轮运行一次：

- `contradicted_count >= 2` 且 `supported_count == 0`：deprecate。
- `used_count == 0` 且已过 8 轮：inactive。
- `scope=general` 且没有 targets/action_payload：只保留给 hypothesis，不进入 warm start。
- 与新 campaign memory rule 冲突：降权或移除。
- 低 confidence 且 source health 差：不进入 active deck。

### Promotion 规则

- 被多次支持的 card 提升 confidence。
- campaign observation 产生的 semantic rule 可以转成 `scope=campaign` card。
- validated campaign card 优先于初始 retrieval card。

## 与当前代码的对应改造点

### 1. `knowledge/augmentation_pipeline.py`

建议新增：

- `build_active_knowledge_deck(...)`
- `compile_deck_to_priors(...)`
- `rank_and_filter_cards_for_deck(...)`

调整：

- `condense_and_build_cards(...)` 输出 action payload。
- `serve_knowledge_priors(...)` 从 active deck 编译，而不是单独从 target_records 生成。
- analogous evidence 允许生成弱 prior，但必须低权重和明确 scope。
- generic evidence 需要更强过滤，避免无关 textbook 卡片。

### 2. `knowledge/knowledge_card.py`

当前 `KnowledgeCard` 可以扩展，而不是完全推倒：

- 增加 `text` 或统一 `claim` 作为 card text。
- 增加 `targets`，使用 variable names 或 derived target names。
- 增加 `action_payload`。
- 增加 `status` 和 `validation`。
- 保留 evidence，用于追溯。

### 3. `knowledge/knowledge_state.py`

建议让 `knowledge_state` 更像轻量控制面：

```yaml
knowledge_state:
  deck_summary: {}
  active_card_ids: []
  coverage_report: {}
  source_health: []
```

大证据放 `retrieval_artifacts` 或外部文件，不默认进入主 prompt。

`score_candidate_with_priors(...)` 可以改为 `score_candidate_with_deck(...)`，直接基于 active cards 评分，并返回 `applied_card_ids`。

### 4. `core/context_builder.py`

所有节点应优先使用 `knowledge_deck.active_cards`：

- `for_generate_hypotheses`: mechanism, hypothesis, reagent_property, failure cards。
- `for_warm_start`: constraints, operating_window, preference, avoidance, interaction cards。
- `for_select_candidate`: top relevant active cards plus applied card IDs。
- `for_interpret_results`: latest candidate 的 applied cards 和可能冲突 cards。

不要让 digest fallback 悄悄替代 active cards。

### 5. `core/warm_start.py`

将 `applied_prior_ids` 升级为 `applied_card_ids`，并记录每张卡的 score。

warm start LLM prompt 应直接看到 active deck，而不是 vague `knowledge_guidance`。

### 6. `memory/memory_manager.py`

增加 knowledge lifecycle 维护：

- `update_card_validation_from_observation(...)`
- `promote_memory_rule_to_card(...)`
- `evict_stale_or_wrong_cards(...)`

注意：memory rule 和 knowledge card 不要完全混同。Memory 是 campaign-learned；initial knowledge 是 prior。二者可以互相影响，但要保留来源。

## 建议的验收标准

### Knowledge build

对 DAR/OCM 至少满足：

- active cards 数量在 6-10 之间。
- 无明显无关 textbook 卡片进入 active deck。
- 每张 active card 有 `card_id`、`text`、`scope`、`confidence`、`targets`、`actionable_for`。
- 至少 3 张 card 有 action payload，除非 source coverage 明确不足。
- 如果 coverage 不足，系统显式给出 `knowledge_gap`，不要伪装成 knowledge-guided。

### Warm start

- `warm_start_queue` 中每个候选都有 `applied_card_ids` 或明确 `knowledge_mode=knowledge_gap`。
- 如果 active deck 有可行动 cards，至少一部分候选的 `knowledge_total` 非零。
- 队列仍保持 coverage/diversity，不被低置信知识完全绑架。

### Downstream flow

- `generate_hypotheses` prompt 能看到 active deck。
- `select_candidate` prompt 能看到 active deck 和 candidate-level card scores。
- `interpret_results` 能判断结果是否支持/反驳相关 cards。
- card 的 status/confidence 会随实验变化。

### Debuggability

`scripts/inspect_knowledge_build.py` 应输出：

```text
Active Deck:
- card_id
- text
- scope/confidence
- targets
- action_payload summary
- evidence refs

Deck Diagnostics:
- cards rejected by irrelevance
- cards rejected by leakage
- cards inactive due to low confidence
- source coverage by facet
```

## 建议的分阶段实施

### Phase 0: 先加评估，不改行为

- 扩展 `inspect_knowledge_build.py`，打印当前 node digests、active prompt guidance、warm_start 可行动 prior 数量。
- 增加两个 fixture：DAR 和 OCM 的 expected knowledge diagnostics。
- 明确当前 baseline：DAR/OCM 基本没有有效 value prior。

### Phase 1: 引入 Active Knowledge Deck

- 新增 deck 数据结构。
- 从现有 `knowledge_cards` 和 `served_priors` 生成 active deck。
- `ContextBuilder` 改为注入 active deck。
- 保持旧字段兼容。

### Phase 2: 改善 retrieval 和 card synthesis

- 增加 facet/role/source 配额。
- 增强 family gating 和 generic textbook noise filter。
- card synthesis 输出 action payload。
- analogous evidence 生成弱 prior。

### Phase 3: warm start 和 AutoBO 消费 deck

- `score_candidate_with_deck(...)`
- warm start queue 记录 `applied_card_ids` 和 card-level score。
- AutoBO shortlist annotation 使用同一套 card scoring。

### Phase 4: 动态维护

- result interpretation 更新 card validation。
- maintenance 阶段进行 card eviction/promotion。
- campaign memory rule 可以生成 campaign-scope card。

### Phase 5: 验证和 ablation

- 对比：
  - no knowledge
  - current knowledge
  - active deck only
  - active deck + dynamic lifecycle
- 指标：
  - early best after 10 iterations
  - time-to-threshold
  - knowledge usage rate
  - contradicted-card eviction rate
  - prompt token overhead

## 和 Claude 讨论时可以聚焦的问题

1. `KnowledgeCard` 是否应该直接扩展，还是新增 `ActiveKnowledgeCard`，避免破坏旧接口？
2. `served_priors` 是否应该完全由 active deck 编译产生？
3. analogous/general evidence 应该如何加权，避免无 target evidence 时完全失明，也避免胡乱引导？
4. card eviction 应该放在 memory manager，还是独立的 knowledge manager？
5. 在 dataset-backed benchmark 中，哪些来源的数值信息算 leakage？外部 ORD yield 是否应该完全禁止进入 card？
6. warm start 中 knowledge score 的权重应该固定、随 confidence 调整，还是由 AutoBO calibration 动态调整？
7. 是否要把 retrieval artifacts 从主 LangGraph state 中移出，只保留 path/reference，减少 checkpoint 体积？

## 最推荐的最终形态

最终我建议把初始 knowledge 的目标定义成：

> Build a small, actionable, traceable Active Knowledge Deck that acts as the agent's prior over the reaction. The deck must be compact enough to inject into every relevant state context, structured enough to compile into BO priors, and dynamic enough to be validated, demoted, or replaced as campaign evidence accumulates.

这比当前“检索很多 chunk，生成一些 cards，再由 digest/prior 零散消费”的方式更贴近 ChemBO 的设计目标，也更符合你最初说的“10 条左右 knowledge card 直接注入 state 并影响 warm start 和后续流程”的构想。

