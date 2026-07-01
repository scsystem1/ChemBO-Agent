# ChemBO-Agent LLM Prompt Catalog

本文档整理当前项目中实际发送给 LLM 的 prompt 格式。测试代码、实验输出以及未被运行时引用的旧工具不计入主清单。

## 1. 总体消息格式

主工作流使用 LangChain message，并在 OpenAI-compatible 接口中转换为以下形式：

```json
{
  "model": "...",
  "messages": [
    {"role": "system", "content": "全局身份、输出纪律、工具协议、工作流"},
    {"role": "user", "content": "可选的 campaign summary"},
    {"role": "user|assistant|tool", "content": "最近若干条压缩后的历史消息"},
    {"role": "user", "content": "当前节点 prompt"}
  ],
  "tools": [],
  "tool_choice": "auto"
}
```

- 全局 system prompt 来自 `core/state.py::_build_system_prompt`。
- 普通节点由 `core/graph.py::_invoke_json_node` 组装：system + 可选 campaign summary + recent messages + 当前 HumanMessage。
- lightweight 节点不带历史，只使用一个精简 system prompt，强调只返回 JSON。
- tool-loop 节点会追加 assistant tool call 和 `role=tool` 的工具结果，最多循环若干轮。
- JSON 解析失败时统一追加：`Reply with strict JSON only. No prose.`，然后重试一次。
- OpenAI-compatible 线格式由 `config/llm_factory.py::_to_openai_message` 生成。

## 2. Prompt 的通用文本骨架

项目中的大多数 prompt 都遵循下面的结构：

```text
You are ... / 动作任务
[Domain Expert Guidance]

[Context / Reaction Context / HPO Context]
{compact JSON}

[Active Knowledge Cards]
...

[Campaign Memory Rules]
...

[Active Hypotheses]
...

[Observed Data / Candidates / Diagnostics]
...

[Task / Rules / Guardrails]
...

Return strict JSON:
{明确的输出 schema}
```

上下文通常使用 `compact_json(...)` 压缩为单行 JSON；descriptor 和 knowledge prior 模块为了可读性会使用缩进 JSON。

## 3. 全局 System Prompt

`core/state.py::_build_system_prompt`

分成四层：

1. `LAYER 1 - IDENTITY`：ChemBO Agent 身份、化学/HPO 专家指令、先验与后验的职责。
2. `LAYER 2 - OUTPUT DISCIPLINE`：JSON-only、简洁证据化表达、选择/原因/置信度、证据引用格式。
3. `LAYER 3 - TOOL PROTOCOL`：只有节点显式绑定工具时才调用。
4. `LAYER 4 - WORKFLOW`：parse -> AutoBO bootstrap -> hypotheses -> warm start -> BO loop -> summary。

化学和 HPO 共用模板，但通过 `core/domain.py::domain_terms` 替换专家身份、术语和证据类型。

## 4. 主工作流 Prompt

| 节点 | 主要输入段落 | 严格 JSON 输出 |
|---|---|---|
| Parse input | 原始问题描述、领域专家指令 | `reaction_type`, `target_metric`, `optimization_direction`, `variables`, `constraints`, `budget`, `additional_context` |
| Interpret result, fast | 单次结果 digest、因果归因纪律 | `interpretation`, hypothesis 状态列表, `reflection`, `knowledge_conflict`, `working_focus` |
| Interpret result, deep | 节点 context、知识卡、历史规则、相似/最好/最差实验、可选检索协议 | fast schema + `new_evidence_cards` |
| Reflect and decide | convergence、budget、当前 BO 配置、AutoBO digest、知识卡、hypothesis 状态、memory packet | `decision: continue|stop`, `reasoning`, `confidence` |
| Campaign summary | 无 LLM prompt，代码直接构造最终 summary | 无 |

注意：当前 `generate_hypotheses` 不再单独调用 LLM，而是把 knowledge deck 中的 hypothesis cards 转成运行时 hypotheses。

## 5. Warm Start Prompt

### 5.1 直接从完整结构化空间选择

`core/warm_start.py::_build_warm_start_direct_structured_prompt`

输入格式：

- domain guidance
- `CRITICAL OUTPUT RULES`
- compact context
- active knowledge cards
- validation feedback / 已接受候选
- structured search space
- warm-start 规则：约 40% exploit / 60% explore、类别覆盖、连续变量合理性、合法/未见/去重

输出：

```json
{
  "strategy_summary": "...",
  "selections": [
    {
      "variables": {},
      "reasoning": "...",
      "purpose": "exploit|explore"
    }
  ]
}
```

其中单个 selection 的精确 schema 由具体搜索空间动态注入。

### 5.2 从合法候选池选择

`core/warm_start.py::_build_warm_start_direct_candidate_pool_prompt`

与完整空间版本规则相同，但输入是带 ID 的候选池，输出为：

```json
{
  "strategy_summary": "...",
  "selected_ids": [1, 2],
  "reasoning_by_id": {"1": "[exploit] ...", "2": "[explore] ..."},
  "purpose_by_id": {"1": "exploit", "2": "explore"},
  "confidence": 0.6
}
```

两种模式都有专用 repair prompt，会附上截断后的 previous draft，要求从头重写完整 JSON。

### 5.3 Warm-start 结果解释

- 单点解释：candidate、result、best-so-far -> interpretation、hypothesis 更新、episodic memory、semantic rule、working memory。
- 批量 postmortem：全部 warm-start observations + hypotheses + 因果归因约束 -> batch interpretation、关键模式和 semantic rules。
- 默认配置下单点 LLM 解释可关闭；批量 postmortem 在 warm start 完成后执行。

## 6. AutoBO Prompt

文件：`core/autobo_prompts.py`

| Builder | 用途 | 输出 schema |
|---|---|---|
| `build_unseen_category_coverage_prompt` | 选择值得探索的未测试类别值 | `targets[{variable,value,reasoning}]` |
| `build_surrogate_plausibility_prompt` | 对匿名 surrogate prediction 的合理性打 1-5 分 | `evaluations[{point_id,prediction_id,score,reasoning}]` |
| `build_acquisition_selection_prompt` | 从 qLogEI/AF ensemble shortlist 选下一实验，可覆盖 top-1 | `selected_id`, `reasoning`, `comparison_to_top1`, `selection_mode`, `override_evidence` |
| `build_ensemble_sur_selection_prompt` | 从不同 surrogate 各自提出的候选中选择 | `selected_id`, model/exploration/knowledge assessments, `confidence` |
| `build_af_strategy_prompt` | 设置 qLogEI/qUCB/TS 权重和 qUCB beta | `weights`, `qucb_beta`, `reasoning`, `confidence` |
| `build_pure_reasoning_selection_prompt` | 无 surrogate 信息时从候选池选择 | `selected_id`, `reasoning`, `hypothesis_alignment`, `information_value`, `concerns`, `confidence` |
| `build_pure_reasoning_space_selection_prompt` | 无 surrogate 信息时直接从完整结构化空间生成一个实验 | 动态注入的单实验 schema |

这些 prompt 常见的动态区块包括：

- `Reaction Context` 或 `HPO Context`
- active knowledge cards
- campaign memory rules
- active hypotheses
- top/bottom observations
- stagnation context
- early post-warm-start exploration guardrail
- candidates 的 `mu`, `sigma`, acquisition provenance、未测试类别值、相对当前 best 的变化

## 7. Knowledge Prompt

文件：`knowledge/prompts.py`

这组 prompt 是项目中主要的“双消息”格式：builder 返回 `(system_prompt, user_prompt)`，调用时再组合或分别传入。

### 7.1 Prior writer

- chemistry 与 HPO 各有一个 system prompt。
- system 负责 card 类型配额、置信度校准、禁止编造、hedging language、target/actionable_for 约束。
- user 负责 profile、reaction/HPO context、变量 role、合法变量名以及输出 schema。

输出：

```json
{
  "cards": [
    {
      "text": "...",
      "card_type": "...",
      "scope": "target|campaign|analogous|general",
      "confidence": 0.0,
      "targets": [],
      "actionable_for": [],
      "testable_prediction": "...",
      "needs_external_evidence": false,
      "evidence_question": ""
    }
  ],
  "global_notes": ""
}
```

### 7.2 Evidence search

- query rewrite：question + campaign context + reaction family -> `queries`, `key_terms`。
- evidence compression：question + why asked + sanitized snippets -> `answers`, `best_answer`, `notes`。

## 8. Descriptor / Feature Prompt

| Builder | 输入 | 输出 |
|---|---|---|
| `embeddings/descriptors/selector_prompt.py::build_descriptor_selection_prompt` | descriptor-enabled variables、白名单 descriptor IDs、可选优化轨迹 | `selected_descriptors_by_variable`, `rationales`, `warnings` |
| `embeddings/descriptors/audit_prompt.py::build_descriptor_audit_prompt` | 当前 schema、代表性 best/worst/median observations、模型与 descriptor diagnostics、可选替代项 | `decision: keep_current|propose_challenger` + 完整 challenger schema |
| `pools/deep_ensemble_features.py::build_deep_ensemble_feature_spec_prompt` | 带 SMILES 的类别变量 | 每个变量 5-6 个 RDKit descriptor names + rationale |

descriptor prompt 只允许使用白名单中的 descriptor，不允许编造名称或数值。HPO 模式下这些 chemistry descriptor prompt 返回空字符串，不调用 LLM。

## 9. Memory Consolidation Prompt

`memory/memory_manager.py::_llm_abstraction`

输入：最多 6 条高价值 episodes、最多 8 条现有 semantic rules、knowledge references，以及 interaction-first 的因果归因规则。

输出：

```json
{
  "new_rules": [
    {
      "rule_type": "chemical_effect|parameter_effect|interaction|constraint|strategy|override",
      "statement": "...",
      "variables": [],
      "conditions": {},
      "confidence": 0.0,
      "supporting_episode_ids": [],
      "supporting_card_ids": [],
      "conflicting_card_ids": [],
      "metadata": {"confound_note": ""}
    }
  ],
  "updated_rules": [{"id": "R1", "confidence": 0.0, "status": "active|tentative|deprecated"}]
}
```

运行时还会在代码层面对不同 rule type 的置信度做硬上限，不能只依赖 prompt 自报置信度。

## 10. Prompt 注入片段

这些不是独立 LLM 请求，但会被插入其他 prompt：

- `knowledge/knowledge_card.py::format_deck_for_prompt`：`[Active Knowledge Cards]`，每卡包含 ID、类型、confidence、scope、targets、text、可选 prediction。
- `core/context_builder.py`：为 hypothesis、warm start、candidate selection、interpretation、reflection、surrogate evaluation 分别裁剪上下文。
- `core/ocm_domain.py::build_domain_prompt`：OCM catalyst/temperature/flow/ratio 的合法空间说明。
- `core/suzuki_domain.py::build_domain_prompt`：合法 substrate pairs 和 ligand/base/solvent vocabulary。
- `core/domain.py::domain_terms`：将同一模板切换成 chemistry 或 HPO 话术。

## 11. 当前未进入主流程的旧工具

`tools/chembo_tools.py` 中的 `hypothesis_generator` 和 `result_interpreter` 会生成带 `instruction` 字段的结构化工具结果，但当前没有生产代码引用它们。它们不应与当前运行时 prompt 清单混在一起。

## 12. 维护建议

当前 prompt 风格总体一致，但定义分散在 `graph.py`, `warm_start.py`, `autobo_prompts.py`, `knowledge/prompts.py`, descriptor 和 memory 模块中。后续修改时建议至少保持以下约定：

1. 所有决策 prompt 明确列出合法输入范围和 JSON schema。
2. 长上下文统一走 `compact_json` 和 node-specific context builder。
3. 需要工具的节点明确写 tool protocol；其他节点不绑定工具。
4. repair prompt 保持统一，但 warm-start 的数量/合法性失败继续使用专用 repair。
5. chemistry/HPO 差异继续集中在 `domain_terms`，避免模板内散落硬编码 chemistry 术语。
