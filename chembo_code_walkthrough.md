# ChemBO Agent 源代码深度导读

> **阅读对象**：对 Python、LLM 应用有基础了解，但对 LangGraph 还不熟悉的开发者。
> 
> 本文档按**数据流顺序**逐层展开整个项目的实现，对每个文件中的核心代码段给出逐行级别的解释，尤其在 LangGraph 相关概念处会展开详细说明。

---

## 目录

1. [项目全景：一句话理解这个系统在做什么](#1-项目全景)
2. [LangGraph 核心概念速查](#2-langgraph-核心概念速查)
3. [第一层：配置与入口](#3-第一层配置与入口)
   - 3.1 `config/settings.py` — 全局超参数
   - 3.2 `main.py` — CLI 入口与事件流
4. [第二层：状态定义 — 系统的"脊柱"](#4-第二层状态定义)
   - 4.1 枚举类型
   - 4.2 `ChemBOState` TypedDict
   - 4.3 `create_initial_state` 工厂函数
   - 4.4 系统提示词
5. [第三层：领域知识库](#5-第三层领域知识库)
6. [第四层：三层记忆系统](#6-第四层三层记忆系统)
   - 6.1 Working Memory
   - 6.2 Episodic Memory
   - 6.3 Semantic Memory
   - 6.4 Consolidation（情节→语义蒸馏）
   - 6.5 序列化与 LLM 上下文注入
7. [第五层：组件池 — BO 的三大引擎](#7-第五层组件池)
   - 7.1 编码器（Embedding）池
   - 7.2 代理模型（Surrogate）池
   - 7.3 采集函数（Acquisition Function）池
   - 7.4 候选点生成与采样工具
8. [第六层：六大工具](#8-第六层六大工具)
   - 8.1 三个 Advisor 工具
   - 8.2 `bo_runner` — 核心计算引擎
   - 8.3 `hypothesis_generator` 与 `result_interpreter`
9. [第七层：LangGraph 图定义 — 系统的"大脑"](#9-第七层langgraph-图定义)
   - 9.1 图的构建入口 `build_chembo_graph`
   - 9.2 LLM 创建与工具绑定
   - 9.3 LangGraph 关键辅助函数
   - 9.4 十二个节点逐一详解
   - 9.5 路由逻辑（条件边）详解
   - 9.6 图的拓扑连接与编译
   - 9.7 完整执行流程 Walk-through
10. [第八层：测试体系](#10-第八层测试体系)
    - 10.1 `test_mock.py` — 无 LLM 端到端测试
    - 10.2 `test_interactive.py` — 真人交互测试
11. [关键设计模式总结](#11-关键设计模式总结)

---

## 1. 项目全景

ChemBO Agent 是一个**基于 LangGraph 的化学反应贝叶斯优化智能体**。它的核心工作流程是：

```
人类用自然语言描述一个化学优化问题
  → LLM 解析问题、生成化学假设、选择 BO 组件
  → 进入循环：
      → BO 引擎计算下一个最有希望的实验条件
      → 系统暂停，等人类去实验室做实验
      → 人类返回实验结果
      → LLM 解读结果、更新记忆
      → LLM 决定：继续 / 重新配置 / 停止
```

整体架构哲学：**单一 LLM 认知核心 + 6 个工具 + 3 层记忆 + 知识库 + 组件池**。

文件结构全景：

```
chembo_agent/
├── main.py                     ← CLI 入口
├── config/settings.py          ← 全局配置 dataclass
├── core/
│   ├── state.py                ← 状态定义（TypedDict）—— 系统的数据骨架
│   └── graph.py                ← LangGraph 图定义 —— 系统的控制流大脑
├── tools/chembo_tools.py       ← 6 个 LangChain 工具
├── pools/component_pools.py    ← 编码器、代理模型、采集函数的注册表与实现
├── memory/memory_manager.py    ← 三层记忆系统
├── knowledge/reaction_kb.py    ← 硬编码的化学领域知识
├── examples/dar_problem.yaml   ← 示例问题定义
├── test_mock.py                ← 无 LLM 端到端测试
└── test_interactive.py         ← 真人交互测试
```

---

## 2. LangGraph 核心概念速查

在深入代码之前，先建立对 LangGraph 关键概念的理解。如果你用过 LangChain，LangGraph 可以理解为"LangChain 的有状态工作流引擎"。

### 2.1 StateGraph — 有状态的图

```python
from langgraph.graph import StateGraph

graph = StateGraph(ChemBOState)  # 用一个 TypedDict 定义图的状态 schema
```

- LangGraph 的核心是一个**有向图**，节点是 Python 函数，边定义了执行顺序。
- 所有节点共享一个**全局状态对象**（TypedDict），节点读取状态、返回部分更新。
- 状态更新是**合并式**的：节点只需返回想修改的字段，其他字段保持不变。

### 2.2 节点（Node）— 图中的计算单元

```python
def my_node(state: ChemBOState) -> dict:
    # 读取 state 中的任何字段
    current_phase = state["phase"]
    # 返回一个 dict，只包含要更新的字段
    return {"phase": "new_phase", "iteration": state["iteration"] + 1}
```

- 节点就是普通 Python 函数，签名是 `(state) -> dict`。
- 返回的 dict 会被**合并**进全局状态。

### 2.3 `add_messages` Reducer — 消息累积器

```python
from langgraph.graph import add_messages
from typing import Annotated

class MyState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]  # ← 特殊的
    phase: str                                             # ← 普通的
```

- 普通字段：节点返回新值时**覆盖**旧值。
- `Annotated[..., add_messages]` 字段：节点返回新消息时**追加**到现有列表。
- 这是 LangGraph 处理对话历史的核心机制——每个节点产出的消息不会覆盖之前的，而是追加。

### 2.4 条件边（Conditional Edges）— 动态路由

```python
graph.add_conditional_edges(
    "node_a",                    # 源节点
    route_function,              # 路由函数：(state) -> str（目标节点名）
)
```

- 路由函数根据当前状态决定下一步去哪个节点。
- 这是实现分支逻辑（if-else）的方式。

### 2.5 `interrupt()` — 人机交互断点

```python
from langgraph.types import interrupt

def await_human(state):
    result = interrupt({"message": "请输入实验结果"})  # 图暂停
    # ... result 包含人类输入
```

- 调用 `interrupt()` 时，图的执行**完全暂停**，状态被持久化到 checkpointer。
- 外部代码可以稍后用 `Command(resume=data)` 恢复执行。
- 这完美匹配化学实验的工作流：提议实验 → 人去做实验（可能几小时/几天）→ 返回结果。

### 2.6 ToolNode — 预构建的工具执行节点

```python
from langgraph.prebuilt import ToolNode

tool_node = ToolNode(ALL_TOOLS)
```

- ToolNode 是 LangGraph 预构建的节点，自动处理 LLM 的工具调用。
- 当 LLM 返回一条包含 `tool_calls` 的 `AIMessage`，ToolNode 会执行对应的工具，并将结果包装成 `ToolMessage` 追加到消息列表。

### 2.7 Checkpointer — 状态持久化

```python
from langgraph.checkpoint.memory import MemorySaver

compiled_graph = graph.compile(checkpointer=MemorySaver())
```

- `MemorySaver` 是内存中的 checkpointer（生产环境可换成 SQLite/PostgreSQL 等）。
- 每次节点执行后，完整状态被保存。
- 支持 `interrupt()` 后恢复、图的回放与调试。

### 2.8 `Command(resume=...)` — 恢复执行

```python
from langgraph.types import Command

# 恢复被 interrupt 暂停的图
graph.invoke(Command(resume={"result": 85.0, "notes": "清澈溶液"}), config=config)
```

---

## 3. 第一层：配置与入口

### 3.1 `config/settings.py`

```python
@dataclass
class Settings:
    # --- LLM ---
    llm_model: str = "claude-sonnet-4-20250514"
    llm_temperature: float = 0.3        # 偏低温度，追求稳定性
    llm_max_tokens: int = 4096

    # --- BO ---
    max_bo_iterations: int = 30         # 总实验预算
    batch_size: int = 1                 # 每轮提议几个实验
    initial_doe_size: int = 5           # 拟合模型前先做几个 DoE（设计实验）
    convergence_patience: int = 5       # 连续几轮无改善算"停滞"
    convergence_threshold: float = 0.01 # 改善幅度低于此算"无改善"

    # --- Memory ---
    episodic_memory_capacity: int = 200 # 情节记忆最多存多少条

    # --- 人机交互 ---
    human_input_mode: str = "terminal"  # "terminal" | "api" | "file"
```

**设计要点**：
- `initial_doe_size = 5`：在拟合 GP 之前先随机/LHS 采样 5 个点。这是 BO 的标准做法——GP 至少需要几个点才能拟合出有意义的后验。
- `convergence_patience = 5`：如果连续 5 轮最优值几乎不变，触发"停滞"判断。
- `from_yaml` 方法只提取 dataclass 中已定义的字段，忽略 YAML 中的额外字段，避免报错。

### 3.2 `main.py`

```python
async def run_chembo_agent(problem_description: str, settings: Settings | None = None):
    settings = settings or Settings()

    # 1. 构建 LangGraph（定义节点、边、编译）
    graph = build_chembo_graph(settings)

    # 2. 创建初始状态
    initial_state = create_initial_state(problem_description, settings)

    # 3. 执行图 —— astream 逐节点流式输出
    config = {"configurable": {"thread_id": settings.experiment_id}}

    async for event in graph.astream(initial_state, config=config):
        node_name = list(event.keys())[0]      # 事件的 key 是节点名
        state_update = event[node_name]         # value 是该节点返回的状态更新
        _print_node_output(node_name, state_update)
```

**LangGraph 关键点**：

1. **`config = {"configurable": {"thread_id": ...}}`**：
   - `thread_id` 是 LangGraph 中标识一次"对话/会话"的 ID。
   - Checkpointer 根据 thread_id 存取状态。
   - 不同的 thread_id = 不同的独立图执行。

2. **`graph.astream(initial_state, config)`**：
   - 异步流式执行图。
   - 每个节点执行完毕后，yield 一个事件 `{node_name: state_update}`。
   - 当遇到 `interrupt()` 时，流会暂停。

3. **恢复执行**：
   - 在 `test_interactive.py` 中可以看到，恢复用的是 `graph.invoke(Command(resume=...), config=config)`。
   - 同一个 `config`（同一个 `thread_id`）让 LangGraph 找到之前暂停的状态继续执行。

---

## 4. 第二层：状态定义 — `core/state.py`

这个文件定义了**整个系统的数据骨架**。LangGraph 中，状态是所有节点共享的唯一数据通道。

### 4.1 枚举类型

```python
class CampaignPhase(str, Enum):
    INIT = "init"                    # 刚收到问题
    ANALYZING = "analyzing"          # LLM 正在解析问题
    CONFIGURING = "configuring"      # 正在选择 BO 组件
    HYPOTHESIZING = "hypothesizing"  # 正在生成假设
    RUNNING = "running"              # BO 迭代进行中
    AWAITING_HUMAN = "awaiting_human"# 等待人类做实验
    INTERPRETING = "interpreting"    # 分析最新结果
    REFLECTING = "reflecting"        # 元推理（继续？重配？停止？）
    COMPLETED = "completed"          # 完成

class NextAction(str, Enum):
    CONTINUE = "continue"            # 继续下一轮 BO
    RECONFIGURE = "reconfigure"      # 重新选择 BO 组件
    STOP = "stop"                    # 优化结束
```

继承 `str, Enum` 是为了让它们**可以直接当字符串用**——存入 TypedDict 时不需要额外序列化。

### 4.2 `ChemBOState` — 核心状态 TypedDict

```python
class ChemBOState(TypedDict):
    # ─── 消息历史（LangGraph 的 add_messages 累积器）───
    messages: Annotated[list[BaseMessage], add_messages]

    # ─── 控制流 ───
    phase: str                       # 当前阶段（CampaignPhase 的值）
    iteration: int                   # 当前 BO 迭代（0-indexed）
    next_action: str                 # reflect 节点的决策

    # ─── 问题定义 ───
    problem_spec: dict               # 解析后的问题规格（含变量定义、约束等）
    kb_context: str                  # 知识库上下文（注入 prompt 的文本）

    # ─── BO 配置 ───
    bo_config: dict                  # 当前 BO 流水线配置（编码器+代理模型+AF）

    # ─── 实验数据 ───
    observations: list[dict]         # 所有实验观测
    current_proposal: dict           # 当前提议的实验
    best_result: float               # 历史最优结果
    best_candidate: dict             # 最优候选

    # ─── 记忆 ───
    memory: dict                     # 三层记忆（序列化的 MemoryManager 数据）

    # ─── 假设与摘要 ───
    hypotheses: list[str]            # 化学假设列表
    campaign_summary: str            # 老消息的压缩摘要
    tool_origin_node: str            # 当前 tool call 循环的发起节点
    last_tool_payload: dict          # 最近一次工具返回的 JSON payload

    # ─── 诊断 ───
    config_history: list[dict]       # BO 配置变更历史
    performance_log: list[dict]      # 每轮性能日志
    llm_reasoning_log: list[str]     # LLM 推理轨迹
```

**关键设计决策**：

1. **为什么用 TypedDict 而不是 dataclass？**
   - LangGraph 的 StateGraph 要求状态是 TypedDict。
   - TypedDict 是纯类型注解，运行时就是普通 dict，LangGraph 可以高效地合并更新。

2. **`messages` 字段的 `add_messages` reducer**：
   - 这是整个对话历史的容器。
   - 当一个节点返回 `{"messages": [HumanMessage("..."), AIMessage("...")]}`，这些消息会被**追加**到现有列表，而不是覆盖。
   - 其他所有字段（如 `phase`、`iteration`）都是覆盖式更新。

3. **复杂数据序列化为 dict**：
   - `problem_spec`、`bo_config`、`memory` 等都是 `dict` 类型。
   - 虽然代码中定义了 `ProblemSpec`、`BOConfig` 等 dataclass，但它们主要是文档作用。
   - 实际在状态中流动的是 dict，节点负责解析/构造这些 dict。

### 4.3 `create_initial_state` 工厂函数

```python
def create_initial_state(problem_description: str, settings) -> ChemBOState:
    system_prompt = _build_system_prompt()

    return ChemBOState(
        messages=[SystemMessage(content=system_prompt)],  # 第一条消息：系统提示
        phase=CampaignPhase.INIT.value,
        iteration=0,
        next_action="",
        problem_spec={"raw_description": problem_description},
        kb_context="",
        bo_config={},
        observations=[],
        current_proposal={},
        best_result=float("-inf"),     # 负无穷，任何实际结果都会成为 best
        best_candidate={},
        memory={
            "working": {},
            "episodic": [],
            "semantic": [],
        },
        hypotheses=[],
        campaign_summary="",
        tool_origin_node="",
        last_tool_payload={},
        config_history=[],
        performance_log=[],
        llm_reasoning_log=[],
    )
```

**注意**：`messages` 初始只有一条 `SystemMessage`。这条消息会一直保留在消息列表中（不会被截断），定义了 LLM 的角色和行为。

### 4.4 系统提示词（`_build_system_prompt`）

```
You are ChemBO Agent, an expert AI system for chemical reaction optimization
using Bayesian Optimization (BO). You operate as a single cognitive core augmented
by specialized tools.

YOUR ROLE:
- Analyze chemical optimization problems described in natural language
- Generate chemically-grounded hypotheses ...
- Select appropriate BO components ... from a curated pool ...
- Interpret experimental results ...
- Decide when to continue, reconfigure, or terminate ...

CORE PRINCIPLES:
1. LLM as ENHANCEMENT to GP, not replacement
2. Every BO component selection must have a clear scientific rationale
3. Chemical domain knowledge should inform every decision
4. Be honest about uncertainty

TOOLS AVAILABLE:
- EmbeddingMethodAdvisor / SurrogateModelSelector / AFSelector
- BORunner / HypothesisGenerator / ResultInterpreter

Workflow order:
1. Analyze → 2. Hypothesize → 3. Configure → 4. Run iterative BO loop
```

这段 system prompt 告诉 LLM：
- 你是谁（ChemBO Agent）
- 你有哪些工具
- 工作流程的顺序
- 核心原则（LLM 辅助 GP，不是替代 GP）

---

## 5. 第三层：领域知识库 — `knowledge/reaction_kb.py`

Phase 1 使用硬编码的字典，为三种反应类型提供领域知识：

```python
REACTION_KNOWLEDGE = {
    "DAR": {
        "full_name": "Direct Arylation Reaction",
        "mechanism": "Pd-catalyzed C-H activation / concerted metalation-deprotonation (CMD)",
        "key_factors": [
            "Ligand choice critically affects regioselectivity ...",
            "Base must be carbonate/carboxylate family for CMD mechanism",
            "Polar aprotic solvents (DMAc, DMF, NMP) generally preferred",
            # ...
        ],
        "literature_priors": {
            "best_ligands": ["P(Cy)3", "XPhos", "DavePhos"],
            "best_bases": ["Cs2CO3", "CsOPiv", "K2CO3"],
            "best_solvents": ["DMAc", "NMP"],
            "optimal_temp_range": [100, 140],
        },
    },
    "BH": { ... },      # Buchwald-Hartwig
    "Suzuki": { ... },   # Suzuki-Miyaura
}
```

`format_knowledge_for_llm(reaction_type)` 将知识格式化为纯文本字符串，在后续节点的 prompt 中以 `DOMAIN KNOWLEDGE:` 形式注入。这让 LLM 在选择 BO 组件和解读结果时有化学 grounding。

---

## 6. 第四层：三层记忆系统 — `memory/memory_manager.py`

### 6.1 Working Memory（工作记忆）

```python
class MemoryManager:
    def __init__(self, capacity: int = 200):
        self.working: dict[str, Any] = {}   # 简单的 key-value 存储
        self.episodic: list[dict] = []
        self.semantic: list[dict] = []

    def update_working(self, key: str, value: Any):
        self.working[key] = value
```

Working Memory 是最短时的——就是一个 dict，存"当前关注什么"、"待决策事项"等。可以随时清空。

### 6.2 Episodic Memory（情节记忆）

```python
def add_episode(self, iteration, config_snapshot, candidate, result,
                reflection, non_numerical_observations="", lesson_learned=""):
    entry = {
        "iteration": iteration,
        "config_snapshot": config_snapshot,   # 当时用的 BO 配置
        "candidate": candidate,               # 实验条件
        "result": result,                     # 实验结果
        "reflection": reflection,             # LLM 的反思
        "non_numerical_observations": ...,    # 非数值观察（颜色变化、沉淀等）
        "lesson_learned": lesson_learned,     # 抽象出的教训
        "timestamp": datetime.now().isoformat(),
    }
    self.episodic.append(entry)

    # FIFO 淘汰：超容量时只保留最新的 capacity 条
    if len(self.episodic) > self.capacity:
        self.episodic = self.episodic[-self.capacity:]
```

每次实验完成后，`finalize_interpretation` 节点会调用 `add_episode()` 记录这一轮的完整信息。

### 6.3 Semantic Memory（语义记忆）

```python
def add_semantic_rule(self, rule: str, confidence: float, source_iterations: list[int]):
    # 先检查是否已有相似规则
    for existing in self.semantic:
        if _rules_are_similar(existing["rule"], rule):
            # 合并：取更高的 confidence，增加 evidence_count
            existing["confidence"] = max(existing["confidence"], confidence)
            existing["evidence_count"] += 1
            existing["source_iterations"].extend(source_iterations)
            return

    # 新规则
    self.semantic.append({
        "rule": rule,             # 例如 "Cs2CO3 + DMAc consistently yields >70%"
        "confidence": confidence, # 0.0 ~ 1.0
        "evidence_count": 1,
        "source_iterations": source_iterations,
    })
```

**相似性判断**（`_rules_are_similar`）：
```python
def _rules_are_similar(rule1: str, rule2: str) -> bool:
    norm1 = _normalize_rule(rule1)  # 小写、去标点、压缩空格
    norm2 = _normalize_rule(rule2)
    if norm1 == norm2: return True
    if norm1 in norm2 or norm2 in norm1: return True
    # 词袋重叠率 > 60% 视为相似
    r1, r2 = set(norm1.split()), set(norm2.split())
    overlap = len(r1 & r2) / max(len(r1), len(r2))
    return overlap > 0.6
```

### 6.4 Consolidation（情节 → 语义蒸馏）

```python
def consolidate(self, llm_consolidation_fn=None):
    # 如果有 LLM 驱动的整合函数，用它
    if llm_consolidation_fn:
        new_rules = llm_consolidation_fn(self.episodic, self.semantic)
        for rule_data in new_rules:
            self.add_semantic_rule(**rule_data)
        return

    # Phase 1 的轻量级确定性逻辑：
    # 如果同一个 lesson_learned 出现 ≥ 2 次，提升为 semantic rule
    lesson_groups: dict[str, list[int]] = {}
    for episode in self.episodic:
        lesson = str(episode.get("lesson_learned", "")).strip()
        if not lesson: continue
        key = _normalize_rule(lesson)
        lesson_groups.setdefault(key, []).append(episode.get("iteration", 0))

    for normalized_lesson, iterations in lesson_groups.items():
        if len(iterations) < 2: continue
        # 找到原始文本作为规则
        exemplar = next(
            (str(ep.get("lesson_learned", "")).strip()
             for ep in self.episodic
             if _normalize_rule(str(ep.get("lesson_learned", ""))) == normalized_lesson),
            ""
        )
        if exemplar:
            confidence = min(0.55 + 0.1 * len(iterations), 0.85)
            self.add_semantic_rule(exemplar, confidence, iterations)
```

**逻辑**：如果 LLM 在不同轮次学到了相同的教训（比如多次说"XPhos looks strong"），就把它提升为一条语义规则。出现次数越多，confidence 越高（上限 0.85）。

### 6.5 LLM 上下文注入

```python
def get_context_for_llm(self, max_episodes: int = 5) -> str:
    parts = []

    # 工作记忆 → JSON dump
    if self.working:
        parts.append(f"[Working Memory]\n{json.dumps(self.working, indent=2)}")

    # 最近 5 条情节 → 摘要格式
    recent = self.get_recent_episodes(max_episodes)
    if recent:
        ep_strs = [f"  Iter {ep['iteration']}: result={ep.get('result', '?')}%, "
                   f"lesson={ep.get('lesson_learned', 'none')}" for ep in recent]
        parts.append(f"[Recent Episodes]\n" + "\n".join(ep_strs))

    # 高置信度规则 → 带分数展示
    rules = self.get_high_confidence_rules()  # confidence >= 0.7
    if rules:
        rule_strs = [f"  [{r['confidence']:.1f}] {r['rule']}" for r in rules]
        parts.append(f"[Semantic Rules]\n" + "\n".join(rule_strs))

    return "\n\n".join(parts) if parts else "[Memory is empty — first iteration]"
```

这段文本会被注入到 `configure_bo`、`run_bo_iteration`、`interpret_results` 等节点的 prompt 中，让 LLM 的决策基于历史经验。

---

## 7. 第五层：组件池 — `pools/component_pools.py`

这是项目中最"硬核"的文件，约 600 行，实现了 BO 的三大组件。

### 7.1 编码器（Embedding）池

编码器的任务：将化学候选条件（如 `{"ligand": "XPhos", "base": "Cs2CO3", "temperature": 120}`）编码为数值向量，供 GP 使用。

#### `OneHotEncoder` — 基线编码器

```python
class OneHotEncoder(BaseEncoder):
    def __init__(self, search_space: list[dict], params=None):
        super().__init__(search_space, params)
        self.specs = []
        offset = 0
        for variable in search_space:
            if variable.get("type") == "continuous":
                # continuous 变量：归一化到 [0, 1]，占 1 维
                self.specs.append({
                    "name": variable["name"],
                    "type": "continuous",
                    "low": float(variable["domain"][0]),
                    "high": float(variable["domain"][1]),
                    "slice": slice(offset, offset + 1),
                })
                offset += 1
            else:
                # categorical 变量：one-hot 编码，占 len(domain) 维
                labels = _domain_labels(variable)
                self.specs.append({
                    "name": variable["name"],
                    "type": "categorical",
                    "labels": labels,
                    "slice": slice(offset, offset + len(labels)),
                })
                offset += len(labels)
        self._dim = offset
```

**DAR 问题的编码维度计算**：
- ligand（5 选项）→ 5 维
- base（4 选项）→ 4 维
- solvent（4 选项）→ 4 维
- temperature → 1 维
- concentration → 1 维
- **总计 15 维**

**encode 方法**：

```python
def encode(self, candidate: dict) -> np.ndarray:
    vector = np.zeros(self.dim, dtype=float)
    for spec in self.specs:
        value = candidate.get(spec["name"])
        if spec["type"] == "continuous":
            # 归一化：(value - low) / (high - low)
            vector[spec["slice"]] = _normalize_continuous(value, spec["low"], spec["high"])
        else:
            # one-hot：在对应位置设 1
            idx = _safe_index(spec["labels"], value)
            vector[spec["slice"].start + idx] = 1.0
    return vector
```

**decode 方法**（从向量还原为候选条件 dict）：

```python
def decode(self, encoded: np.ndarray) -> dict:
    decoded = {}
    for spec in self.specs:
        chunk = encoded[spec["slice"]]
        if spec["type"] == "continuous":
            # 反归一化
            decoded[spec["name"]] = _denormalize_continuous(float(chunk[0]), spec["low"], spec["high"])
        else:
            # argmax → 对应的 label
            decoded[spec["name"]] = spec["labels"][int(np.argmax(chunk))]
    return decoded
```

#### `FingerprintConcatEncoder` — 化学感知编码器

对有 SMILES 数据的分子变量，使用 **Morgan fingerprint**（也叫 ECFP4）编码分子结构：

```python
# 核心逻辑：如果变量有 SMILES 且 RDKit 可用
if Chem is not None and AllChem is not None:
    for label, smiles in smiles_map.items():
        fingerprint = _fingerprint_from_smiles(smiles, self.radius, self.n_bits)
        if fingerprint is not None:
            fp_map[label] = fingerprint  # label → 256维 bit vector
```

Morgan fingerprint 是什么？它把分子的每个原子及其邻域结构哈希成一个固定长度的 bit vector，相似结构的分子会有相似的 fingerprint。对 GP 来说，这比 one-hot 提供了更丰富的结构相似性信息。

**decode 时的最近邻逻辑**：

```python
elif spec["type"] == "fingerprint":
    # 在所有合法选项中找 L2 距离最近的
    best_label, best_distance = None, float("inf")
    for label, fp in spec["fp_map"].items():
        distance = float(np.linalg.norm(chunk - fp))
        if distance < best_distance:
            best_distance, best_label = distance, label
    decoded[spec["name"]] = best_label
```

#### `GuardedFallbackEncoder` — 安全降级包装器

```python
class GuardedFallbackEncoder(BaseEncoder):
    def __init__(self, search_space, params, fallback_cls, reason):
        self._delegate = fallback_cls(search_space, params)  # 实际用的是 fallback
        self.metadata["notes"].append(reason)                # 记录降级原因
```

LLM 可以"选择" `llm_embedding` 或 `chemberta`，但实际执行时会透明降级到 `OneHotEncoder` 或 `FingerprintConcatEncoder`，并在 metadata 中记录原因。这保证了**选择空间的完整性**（LLM 能看到所有选项）和**执行的稳定性**。

### 7.2 代理模型（Surrogate）池

#### `GaussianProcessSurrogate` — 基于 sklearn 的 GP

```python
class GaussianProcessSurrogate(BaseSurrogateModel):
    def fit(self, X: np.ndarray, y: np.ndarray) -> None:
        # 根据 kernel_name 选择基础 kernel
        if self.kernel_name == "rbf":
            base_kernel = RBF(length_scale=np.ones(X.shape[1]))
        else:
            nu = 2.5 if self.kernel_name != "mixture" else 1.5
            base_kernel = Matern(length_scale=np.ones(X.shape[1]), nu=nu)

        # 组合 kernel：常数 * 基础 kernel + 白噪声
        kernel = ConstantKernel(1.0, (1e-3, 1e3)) * base_kernel \
               + WhiteKernel(noise_level=..., noise_level_bounds=...)

        self.model = GaussianProcessRegressor(
            kernel=kernel,
            alpha=max(noise_level, 1e-8),
            normalize_y=True,          # sklearn 内部做 Y 标准化
            n_restarts_optimizer=1,     # 超参数优化重启次数
        )
        self.model.fit(X, y)
```

**为什么用 sklearn 而不是 BoTorch？**

Phase 1 的务实选择：
- sklearn 的 GP 不依赖 PyTorch，安装简单
- API 更直白（`.fit()` / `.predict()`）
- 对于 <100 个数据点的小规模 BO 完全够用
- BoTorch 的优势（GPU 加速、batch AF 优化）在这个规模下体现不出来

**ARD（Automatic Relevance Determination）**：

`length_scale=np.ones(X.shape[1])` 给每个维度一个独立的长度尺度参数。训练过程中，不重要的维度的长度尺度会变得很大（相当于"忽略"那个维度），重要维度的长度尺度会变小。

#### `RandomForestSurrogate` — 基于树的替代方案

```python
class RandomForestSurrogate(BaseSurrogateModel):
    def predict(self, X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        # 用所有树的预测的均值和标准差作为预测和不确定性
        tree_predictions = np.array([tree.predict(X) for tree in self.model.estimators_])
        mean = tree_predictions.mean(axis=0)
        std = np.maximum(tree_predictions.std(axis=0), 1e-6)
        return mean, std
```

Random Forest 的"不确定性"不如 GP 那么有理论根据（不是真正的后验方差），但实际效果还行，且对噪声和高维更鲁棒。

### 7.3 采集函数（Acquisition Function）池

这里的关键设计决策：**不在连续空间上优化 AF，而是在候选池上评分**。

```python
class AcquisitionFunction:
    def score(self, mean, std, best_f, rng) -> np.ndarray:
        if self.key in {"ei", "qei"}:
            # EI 的解析公式
            improvement = mean - best_f
            z = improvement / std
            pdf = np.vectorize(NDIST.pdf)(z)   # 标准正态的 PDF
            cdf = np.vectorize(NDIST.cdf)(z)   # 标准正态的 CDF
            return improvement * cdf + std * pdf

        if self.key in {"ucb", "qucb"}:
            # UCB：μ + β·σ
            beta = float(self.params.get("beta", 0.2))
            return mean + beta * std

        if self.key == "ts":
            # Thompson Sampling：从后验中采样
            return rng.normal(loc=mean, scale=std)
```

**为什么不用 BoTorch 的 `optimize_acqf`？**

因为化学优化问题的搜索空间大量包含 categorical 变量。`optimize_acqf` 在连续空间上做梯度优化，优化完还需要把连续向量 decode 回最近的合法 categorical 值——误差大且复杂。

直接在候选池上评分更简洁：
1. 枚举所有 categorical 组合（如果组合数可控）或采样一大批候选点
2. 编码所有候选点
3. GP 预测 mean 和 std
4. AF 对每个候选打分
5. 取 top-k

### 7.4 候选点生成

```python
def hybrid_sample_candidates(search_space, num_samples, seed=0):
    rng = np.random.default_rng(seed)
    # Categorical 变量：均匀随机采样
    # Continuous 变量：拉丁超立方采样（LHS）
    lhs = _latin_hypercube(len(continuous_vars), num_samples, rng)
    # ...组合成候选点列表
```

**拉丁超立方采样（LHS）**是一种比纯随机采样更均匀的方法：把每个维度的 [0,1] 分成 n 个等宽区间，确保每个区间恰好有一个采样点。这在 DoE（实验设计）和 BO 冷启动中是标准做法。

---

## 8. 第六层：六大工具 — `tools/chembo_tools.py`

LangChain 的 `@tool` 装饰器将普通函数注册为 LLM 可调用的工具。

### 8.1 三个 Advisor 工具

这三个工具本质上都是"呈现选项 + 提供评分 + 让 LLM 做最终选择"的模式：

```python
@tool
def embedding_method_advisor(
    problem_summary: str,
    variable_types: str,
    num_categoricals: int,
    num_continuous: int,
    has_smiles: bool,
    data_volume: int,
) -> str:
    options = get_embedding_options()  # 从 EMBEDDING_POOL 获取所有选项

    scored_options = []
    for opt in options:
        score_reasons = []
        tags = opt["tags"]
        # 根据问题特征为每个选项加分/减分
        if has_smiles and tags.get("chemistry_aware"):
            score_reasons.append("+chemistry_aware (has SMILES)")
        if data_volume < 10 and tags.get("learned"):
            score_reasons.append("-learned repr may underfit with little data")
        scored_options.append({**opt, "suitability_notes": score_reasons})

    return json.dumps({
        "available_options": scored_options,
        "problem_context": { ... },
        "instruction": "Review the options above. Select ONE embedding method by its key..."
    })
```

注意：这些工具**不做选择**，只提供带评分的选项列表。最终选择权在 LLM。这是"LLM as enhancement"哲学的体现——工具提供信息，LLM做决策。

### 8.2 `bo_runner` — 核心计算引擎

这是最复杂的工具，约 200 行。我逐段解析核心流程：

```python
@tool
def bo_runner(
    embedding_method: str,      # 编码器名
    embedding_params: str,      # 编码器参数（JSON 字符串）
    surrogate_model: str,       # 代理模型名
    surrogate_params: str,
    acquisition_function: str,  # 采集函数名
    af_params: str,
    search_space: str,          # 搜索空间定义（JSON 字符串）
    observations: str,          # 已有观测（JSON 字符串）
    batch_size: int = 1,
) -> str:
```

**注意所有参数都是字符串**——这是因为 LangChain tool 的参数序列化限制。函数内部先 JSON 解析。

#### Step 1: 创建编码器 & 去重观测

```python
encoder = create_encoder(embedding_method, search_space_data, embedding_params_data)
deduped_observations = _dedupe_observations(obs_data)
# 去重逻辑：如果同一个候选点被重复实验，取结果的均值
```

#### Step 2: 冷启动判断

```python
cold_start = len(deduped_observations) < initial_doe_size
if cold_start:
    # 不拟合模型，直接生成 DoE 候选点
    candidates = _initial_design_candidates(search_space_data, observed_keys, ...)
    return json.dumps({
        "status": "success",
        "strategy": "initial_doe",
        "candidates": candidates,
        "predictions": [None, ...],       # 没有模型，无法预测
        "uncertainties": [None, ...],
        "message": "Cold start: proposing DoE candidate(s) before fitting a surrogate."
    })
```

#### Step 3: 编码数据 & Y 标准化

```python
X_obs, y_obs, fit_candidates = _observations_to_training_data(deduped_observations, encoder)
y_mean = float(np.mean(y_obs))
y_std = float(np.std(y_obs)) or 1.0
y_scaled = (y_obs - y_mean) / y_std  # 标准化：对 GP 至关重要
```

**为什么要标准化 Y？**
GP 的 kernel 超参数对 Y 的尺度敏感。标准化到均值 0、方差 1 后，GP 更容易收敛。

#### Step 4: 构建候选池 & 拟合模型 & 评分

```python
# 构建候选池（枚举或采样）
candidate_pool = _build_candidate_pool(search_space_data, observed_keys, ...)
X_pool = encoder.encode_batch(candidate_pool)

# 拟合代理模型
surrogate = create_surrogate(surrogate_model, surrogate_params_data)
try:
    surrogate.fit(X_obs, y_scaled)
    pred_mean_scaled, pred_std_scaled = surrogate.predict(X_pool)
    pred_mean = pred_mean_scaled * y_std + y_mean  # 反标准化
    pred_std = np.maximum(pred_std_scaled * abs(y_std), 1e-6)
except Exception as exc:
    # GP 失败 → 自动 fallback 到 Random Forest
    fallback_reason = f"{type(exc).__name__}: {exc}"
    surrogate = create_surrogate("random_forest", surrogate_params_data)
    surrogate.fit(X_obs, y_scaled)
    # ...
```

```python
# 采集函数评分
acquisition = create_acquisition(acquisition_function, af_params_data)
acquisition_values = acquisition.score(pred_mean, pred_std, best_f, rng)

# 取 top-k
top_indices = _top_k_indices(acquisition_values, batch_size)
chosen_candidates = [candidate_pool[idx] for idx in top_indices]
```

#### Step 5: 返回完整结果

```python
return json.dumps({
    "status": "success",
    "strategy": "model_guided_search",
    "candidates": chosen_candidates,
    "predictions": [float(pred_mean[idx]) ...],
    "uncertainties": [float(pred_std[idx]) ...],
    "acquisition_values": [float(acquisition_values[idx]) ...],
    "surrogate_metrics": {
        "model": surrogate.metadata.get("resolved_key", surrogate_model),
        "num_training_points": int(len(y_obs)),
        "log_marginal_likelihood": getattr(surrogate, "log_marginal_likelihood_", None),
    },
    "resolved_components": { ... },     # 实际使用的组件（可能和选择的不同，因为 fallback）
    "metadata": {
        "encoder_notes": ...,           # 编码器的降级信息
        "fallback_reason": ...,         # 如果发生了 fallback
    },
})
```

### 8.3 `hypothesis_generator` 与 `result_interpreter`

这两个工具**不做计算**，只整理上下文信息：

```python
@tool
def hypothesis_generator(problem_spec, current_observations, memory_context) -> str:
    obs = _loads(current_observations, [])
    if obs:
        summary = {
            "num_experiments": len(obs),
            "best_result": max(results),
            "trend": "improving" if results[-1] >= results[-2] else "flat_or_declining",
        }
    else:
        summary = {"num_experiments": 0, "note": "No data yet — generate prior hypotheses"}

    return json.dumps({
        "problem_spec": ...,
        "data_summary": summary,
        "instruction": "Generate 3-5 specific hypotheses. Return strict JSON ..."
    })
```

这些工具的哲学是：工具负责"组织数据"，LLM 负责"产出洞察"。

---

## 9. 第七层：LangGraph 图定义 — `core/graph.py`

**这是整个系统最核心的文件**，约 500 行，定义了图的所有节点、边和路由逻辑。

### 9.1 图的构建入口

```python
def build_chembo_graph(settings: Settings):
    llm = _create_llm(settings)                    # 创建裸 LLM
    llm_with_tools = llm.bind_tools(ALL_TOOLS)     # 绑定 6 个工具
    tool_node = ToolNode(ALL_TOOLS)                 # 创建工具执行节点

    graph = StateGraph(ChemBOState)                 # 用状态 schema 初始化图

    # ... 定义 12 个节点函数 ...
    # ... 添加节点和边 ...

    return graph.compile(checkpointer=MemorySaver())
```

**两种 LLM 用法**：
- `llm`：裸 LLM，用于期望纯 JSON 输出的节点（`analyze_problem`、`reflect_and_decide`）
- `llm_with_tools`：绑定了工具的 LLM，用于可能调用工具的节点

**`ToolNode(ALL_TOOLS)`**：LangGraph 预构建的节点，它做的事情是：
1. 检查传入状态的最后一条 `AIMessage` 中的 `tool_calls`
2. 执行对应的工具函数
3. 将结果包装成 `ToolMessage` 追加到消息列表

### 9.2 LLM 创建

```python
def _create_llm(settings: Settings):
    model_name = settings.llm_model.strip()
    lowered = model_name.lower()
    if lowered.startswith("claude"):
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(
            model=model_name,
            temperature=settings.llm_temperature,
            max_tokens=settings.llm_max_tokens,
        )
    if lowered.startswith(("gpt", "o1", "o3", "o4")):
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(...)
```

根据 model 名的前缀选择 LangChain 的 chat model wrapper。延迟 import 避免不必要的依赖。

### 9.3 LangGraph 关键辅助函数

#### `_build_context_messages` — 滑动窗口防止 context overflow

```python
def _build_context_messages(state: ChemBOState) -> tuple[list[BaseMessage], str]:
    messages = state.get("messages", [])
    if not messages:
        return [], state.get("campaign_summary", "")

    system_message = messages[0]         # 系统提示词永远保留
    recent = messages[1:]
    summary = state.get("campaign_summary", "")

    if len(recent) > 20:
        # 超过 20 条消息：老的压缩成摘要
        older = recent[:-20]
        recent = recent[-20:]
        older_summary = _summarize_messages(older)
        summary = older_summary if not summary else f"{summary}\n{older_summary}"

    # 构建最终上下文：系统提示 + 摘要 + 最近 20 条
    context = [system_message]
    if summary:
        context.append(HumanMessage(content=f"[CAMPAIGN SUMMARY]\n{summary}"))
    context.extend(recent)
    return context, summary
```

这解决了一个实际问题：BO campaign 可能跑 30+ 轮，每轮产生多条消息，如果全部发给 LLM 会超出 context window。所以：
- 保留最近 20 条完整消息
- 更老的消息压缩成摘要
- 系统提示词始终保留

#### `_invoke_tool_reasoner` — 调用可能使用工具的 LLM

```python
def _invoke_tool_reasoner(llm, state, prompt, origin_node, phase):
    context_messages, summary = _build_context_messages(state)
    response = llm.invoke(context_messages + [HumanMessage(content=prompt)])
    return [HumanMessage(content=prompt), response], summary
```

返回 `[HumanMessage(prompt), AIMessage(response)]`。这两条消息会通过 `add_messages` reducer 追加到状态的 messages 列表中。

如果 LLM 的 response 中包含 `tool_calls`，后续的路由逻辑会把它导向 `tool_node`。

#### `_invoke_json_node` — 调用期望纯 JSON 输出的 LLM

```python
def _invoke_json_node(llm, state, prompt, default):
    context_messages, _ = _build_context_messages(state)
    response = llm.invoke(context_messages + [HumanMessage(content=prompt)])
    parsed = _extract_json_from_response(_message_text(response))
    messages = [HumanMessage(content=prompt), response]

    if parsed is None:
        # JSON 解析失败 → 发修复 prompt 重试一次
        repair_prompt = "Your previous response did not contain valid JSON. Reply with JSON only."
        repair_response = llm.invoke(context_messages + messages + [HumanMessage(content=repair_prompt)])
        parsed = _extract_json_from_response(_message_text(repair_response)) or default
        messages += [HumanMessage(content=repair_prompt), repair_response]

    return parsed or default, messages
```

**两次机会**：先尝试解析 LLM 的输出，失败则发"只返回 JSON"的修复 prompt 重试，最终兜底用 default 值。

#### `_extract_json_from_response` — 从 LLM 文本中提取 JSON

```python
def _extract_json_from_response(text: str) -> dict | None:
    # 第一优先：Markdown 代码块中的 JSON
    code_block = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if code_block:
        try: return json.loads(code_block.group(1))
        except: pass

    # 第二优先：文本中第一个 { 到最后一个 } 之间的内容
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try: return json.loads(text[start:end+1])
        except: pass

    return None
```

LLM 经常在 JSON 前后加说明文字，这个函数能容忍这种情况。

### 9.4 十二个节点逐一详解

#### 节点 1: `analyze_problem`

**作用**：LLM 从自然语言中提取结构化的问题规格。

```python
def analyze_problem(state: ChemBOState) -> dict:
    problem_desc = state["problem_spec"].get("raw_description", "")
    prompt = f"""Analyze this chemical optimization problem ...
    Respond with EXACT JSON:
    {{
      "reaction_type": "DAR | BH | Suzuki | ...",
      "target_metric": "yield",
      "variables": [...],
      "budget": 30,
      ...
    }}"""

    # 使用裸 LLM（不绑定工具），期望纯 JSON 输出
    parsed_spec, messages = _invoke_json_node(llm, state, prompt, {"raw_description": problem_desc})
    parsed_spec["raw_description"] = problem_desc

    # 查知识库
    kb_context = format_knowledge_for_llm(parsed_spec.get("reaction_type", ""))

    return {
        "messages": messages,               # 追加到消息历史
        "problem_spec": parsed_spec,        # 覆盖 problem_spec
        "kb_context": kb_context,           # 设置知识库上下文
        "phase": CampaignPhase.ANALYZING.value,
        # ...
    }
```

**LangGraph 视角**：
- 节点读取 `state["problem_spec"]["raw_description"]`
- 返回的 dict 中，`messages` 会被 `add_messages` 追加，其他字段覆盖更新
- 返回后，图会根据边的定义流向下一个节点

#### 节点 2-3: `generate_hypotheses` + `finalize_hypotheses`

这是**双节点模式**的典型例子。

**`generate_hypotheses`**（可能调用工具的推理节点）：

```python
def generate_hypotheses(state: ChemBOState) -> dict:
    if _has_recent_tool_result(state):
        # 如果最近有 tool result → 说明 hypothesis_generator 已返回
        # 让 LLM 基于工具输出产出最终 JSON
        prompt = """Use the hypothesis_generator tool output above and now return
        the final strict JSON only. {...}
        Do not call any more tools unless the existing tool output is clearly unusable."""
    else:
        # 首次进入 → 让 LLM 先调用 hypothesis_generator 工具
        prompt = f"""Generate 3-5 chemically grounded hypotheses...
        Call the hypothesis_generator tool first, then return strict JSON..."""

    response, summary = _invoke_tool_reasoner(
        llm_with_tools,     # ← 注意：用的是绑定了工具的 LLM
        state, prompt,
        origin_node="generate_hypotheses",   # ← 记录"谁发起了 tool call"
        phase=CampaignPhase.HYPOTHESIZING.value,
    )
    return {
        "messages": response,
        "phase": CampaignPhase.HYPOTHESIZING.value,
        "tool_origin_node": "generate_hypotheses",  # ← 路由 tool_node 后回到这里
    }
```

**`_has_recent_tool_result` 的逻辑**：

```python
def _has_recent_tool_result(state: ChemBOState) -> bool:
    for message in reversed(state.get("messages", [])):
        if isinstance(message, ToolMessage):
            return True                 # 找到了工具结果
        if isinstance(message, HumanMessage):
            break                       # 遇到人类消息就停（这是上一轮的边界）
    return False
```

这个检查让节点知道自己是"第一次被调用"还是"工具返回后再次被调用"。

**`finalize_hypotheses`**（最终化节点）：

```python
def finalize_hypotheses(state: ChemBOState) -> dict:
    default = { "hypotheses": [...], "working_memory_focus": "..." }
    # 尝试从对话中提取/修复 JSON
    parsed, messages = _repair_json_from_conversation(llm, state, ..., default=default)

    # 更新记忆
    memory_manager = MemoryManager.from_dict(state["memory"], ...)
    memory_manager.update_working("current_focus", parsed.get("working_memory_focus", "..."))

    # 格式化假设为文本列表
    hypothesis_texts = [f"H{idx}: {item['hypothesis']} | ..." for idx, item in enumerate(structured, 1)]

    return {
        "messages": messages,
        "hypotheses": hypothesis_texts,     # 覆盖 hypotheses 列表
        "memory": memory_manager.to_dict(), # 更新记忆
    }
```

#### 节点 4-5: `configure_bo` + `finalize_bo_config`

`configure_bo` 需要依次调用**三个**工具（embedding_advisor → surrogate_selector → af_selector）。这是通过 tool call 循环实现的：

```
configure_bo（第1次）→ LLM 调用 embedding_method_advisor
    → tool_node 执行工具
    → route_after_tool_call 路由回 configure_bo
configure_bo（第2次）→ LLM 看到 embedding 结果，调用 surrogate_model_selector
    → tool_node 执行工具
    → route_after_tool_call 路由回 configure_bo
configure_bo（第3次）→ LLM 看到 surrogate 结果，调用 af_selector
    → tool_node 执行工具
    → route_after_tool_call 路由回 configure_bo
configure_bo（第4次）→ LLM 看到所有结果，输出最终 JSON（无 tool_calls）
    → route_after_tool_capable_node 路由到 finalize_bo_config
```

`finalize_bo_config` 的 default config 确保即使 LLM 输出不规范也有合理配置：

```python
default_config = {
    "embedding_method": "one_hot",
    "surrogate_model": "gp_matern52",
    "acquisition_function": "ei",
    # ...
}
config = default_config | parsed   # parsed 覆盖 default
config["config_version"] = len(state.get("config_history", [])) + 1
```

#### 节点 6-7: `run_bo_iteration` + `finalize_bo_proposal`

```python
def run_bo_iteration(state: ChemBOState) -> dict:
    if _has_recent_tool_result(state):
        prompt = """Use the bo_runner output above and explain the proposal in chemical terms.
        Do not call bo_runner again unless the prior tool result is invalid."""
    else:
        prompt = f"""Run BO iteration {state['iteration'] + 1}.
        Call bo_runner with:
        - embedding_method: {config.get('embedding_method', 'one_hot')}
        - surrogate_model: {config.get('surrogate_model', 'gp_matern52')}
        - search_space: {json.dumps(state['problem_spec'].get('variables', []))}
        - observations: {json.dumps(state.get('observations', []))}
        - batch_size: {settings.batch_size}
        After the tool returns, explain why the proposed condition is promising."""
```

注意 prompt 中把所有 bo_runner 需要的参数都明确列出了——这是为了让 LLM 知道怎么构造 tool call 的参数。

`finalize_bo_proposal` 从消息中提取 bo_runner 的返回：

```python
def finalize_bo_proposal(state: ChemBOState) -> dict:
    # 从消息列表中找到最近的 ToolMessage，解析其 JSON 内容
    payload = _extract_latest_tool_payload(state["messages"])
    candidates = payload.get("candidates") or []

    proposal = {
        "candidates": candidates,
        "predicted_values": payload.get("predictions") or [None ...],
        "uncertainties": payload.get("uncertainties") or [None ...],
        "rationale": _last_ai_content(state["messages"]),  # LLM 的化学解释
    }
    return {
        "current_proposal": proposal,    # 存入 state，供 await_human_results 使用
        "last_tool_payload": payload,
    }
```

#### 节点 8: `await_human_results` — interrupt 暂停点

**这是 LangGraph 人机交互的核心**：

```python
def await_human_results(state: ChemBOState) -> dict:
    proposal = state.get("current_proposal", {})
    iteration = state["iteration"]

    # ─── interrupt() 暂停图的执行 ───
    human_response = interrupt({
        "type": "experiment_request",
        "iteration": iteration + 1,
        "candidate": proposal,
        "message": f"EXPERIMENT REQUEST — Iteration {iteration + 1}\n{json.dumps(proposal)}",
    })
    # ─── 图恢复后，human_response 包含人类输入 ───

    result_value, notes = _parse_human_response(human_response)

    # 记录新的观测
    new_obs = {
        "iteration": iteration + 1,
        "candidate": proposal.get("candidates", [{}])[0],
        "result": result_value,
        "metadata": {"notes": notes},
    }
    observations = state["observations"] + [new_obs]

    # 更新最优
    best_result = state["best_result"]
    best_candidate = state["best_candidate"]
    if result_value > best_result:
        best_result = result_value
        best_candidate = new_obs["candidate"]

    return {
        "messages": [HumanMessage(content=f"Experiment result: {result_value}. Notes: {notes}")],
        "observations": observations,
        "best_result": best_result,
        "best_candidate": best_candidate,
        "iteration": iteration + 1,       # 迭代计数 +1
    }
```

**`interrupt()` 的执行模型**：
1. 当执行到 `interrupt({...})` 时，整个图的状态被快照到 checkpointer
2. 传给 `interrupt` 的 dict 会作为"暂停信号"返回给外部调用者
3. 外部调用者（test_interactive.py 或 API）展示实验提议，等待人类输入
4. 人类完成实验后，外部调用者用 `Command(resume={"result": 85.0, "notes": "..."})` 恢复
5. `interrupt()` 函数返回 `Command.resume` 中的值（即 `{"result": 85.0, "notes": "..."}`）
6. 节点继续执行后续逻辑

**`_parse_human_response` 的健壮性**：

```python
def _parse_human_response(human_response) -> tuple[float, str]:
    if isinstance(human_response, (int, float)):
        return float(human_response), ""
    if isinstance(human_response, dict):
        return float(human_response.get("result", 0.0)), str(human_response.get("notes", ""))
    if isinstance(human_response, str):
        try: return float(human_response), ""
        except ValueError:
            try:
                parsed = json.loads(human_response)
                return float(parsed.get("result", 0.0)), str(parsed.get("notes", ""))
            except: return 0.0, human_response
    return 0.0, str(human_response)
```

处理了所有可能的输入格式——数字、dict、JSON 字符串、纯文本——确保不会因为人类输入格式不对而崩溃。

#### 节点 9-10: `interpret_results` + `finalize_interpretation`

`finalize_interpretation` 是**记忆系统真正"活"起来的地方**：

```python
def finalize_interpretation(state: ChemBOState) -> dict:
    parsed, messages = _repair_json_from_conversation(llm, state, ..., default=default)
    latest_obs = state["observations"][-1]

    memory_manager = MemoryManager.from_dict(state["memory"], ...)

    # 1. 添加情节记忆
    episodic = parsed.get("episodic_memory", {})
    memory_manager.add_episode(
        iteration=latest_obs.get("iteration"),
        config_snapshot=state.get("bo_config", {}),
        candidate=latest_obs.get("candidate", {}),
        result=latest_obs.get("result"),
        reflection=episodic.get("reflection", "..."),
        lesson_learned=episodic.get("lesson_learned", ""),
    )

    # 2. 如果 LLM 提出了高置信度的语义规则，添加到语义记忆
    semantic_rule = parsed.get("semantic_rule")
    if isinstance(semantic_rule, dict) and semantic_rule.get("rule"):
        confidence = float(semantic_rule.get("confidence", 0.0))
        if confidence >= 0.6:
            memory_manager.add_semantic_rule(
                semantic_rule["rule"], confidence,
                [latest_obs.get("iteration")]
            )

    # 3. 更新工作记忆
    working_memory = parsed.get("working_memory", {})
    for key, value in working_memory.items():
        memory_manager.update_working(key, value)

    # 4. 尝试整合（情节 → 语义蒸馏）
    memory_manager.consolidate()

    return {
        "messages": messages,
        "memory": memory_manager.to_dict(),    # 序列化回 dict 存入状态
    }
```

#### 节点 11: `reflect_and_decide`

```python
def reflect_and_decide(state: ChemBOState) -> dict:
    # 快速路径：预算耗尽 → 直接停止
    observations = state.get("observations", [])
    budget = int(state["problem_spec"].get("budget", settings.max_bo_iterations))
    if len(observations) >= budget:
        return {
            "phase": CampaignPhase.COMPLETED.value,
            "next_action": NextAction.STOP.value,
            "messages": [AIMessage(content=f"Budget exhausted. Campaign complete.")],
        }

    # 检测停滞
    perf_log = state.get("performance_log", [])
    recent_results = [entry["best_so_far"] for entry in perf_log[-settings.convergence_patience:]]
    stagnant = (
        len(recent_results) >= settings.convergence_patience
        and (max(recent_results) - min(recent_results)) < settings.convergence_threshold * 100
    )

    # 让 LLM 做反思决策
    prompt = f"""Reflect on the campaign progress...
    STAGNANT: {stagnant}
    Respond with strict JSON: {{"decision": "continue"|"reconfigure"|"stop", ...}}"""

    parsed, messages = _invoke_json_node(llm, state, prompt, default={"decision": "continue"})
    decision = str(parsed.get("decision", "continue")).lower()

    # 将决策映射到 next_action
    if decision == "stop":
        next_action = NextAction.STOP.value
        phase = CampaignPhase.COMPLETED.value
    elif decision == "reconfigure" or stagnant:
        # LLM 说重配 或 系统检测到停滞 → 重配
        next_action = NextAction.RECONFIGURE.value
        phase = CampaignPhase.REFLECTING.value
    else:
        next_action = NextAction.CONTINUE.value
        phase = CampaignPhase.REFLECTING.value

    return {
        "phase": phase,
        "next_action": next_action,
        # ...
    }
```

注意 `stagnant` 判断是**系统级别**的——即使 LLM 说"continue"，如果系统检测到停滞也会强制触发 reconfigure。这是对 LLM 判断的安全网。

#### 节点 12: `tool_node`（预构建）

```python
tool_node = ToolNode(ALL_TOOLS)
graph.add_node("tool_node", tool_node)
```

ToolNode 的工作流程（LangGraph 内部实现）：
1. 读取 `state["messages"][-1]`（最后一条 AIMessage）
2. 检查其 `tool_calls` 属性
3. 对每个 tool call，找到对应的工具函数，传入参数，执行
4. 将结果包装成 `ToolMessage(content=result, tool_call_id=call.id, name=tool.name)`
5. 返回 `{"messages": [tool_messages]}`（通过 add_messages 追加）

### 9.5 路由逻辑（条件边）详解

这是理解整个图执行流程的关键。

#### `route_after_tool_capable_node` — 工具调用的分岔口

```python
def route_after_tool_capable_node(state) -> Literal[
    "tool_node",               # LLM 想调用工具
    "finalize_hypotheses",     # 假设阶段完成
    "finalize_bo_config",      # 配置阶段完成
    "finalize_bo_proposal",    # BO 迭代完成
    "finalize_interpretation"  # 解读阶段完成
]:
    last_msg = state["messages"][-1]
    if _message_has_tool_calls(last_msg):
        return "tool_node"     # 有 tool_calls → 去执行工具

    # 没有 tool_calls → 根据当前 phase 去对应的 finalize 节点
    phase = state.get("phase", "")
    if phase == CampaignPhase.HYPOTHESIZING.value:
        return "finalize_hypotheses"
    if phase == CampaignPhase.CONFIGURING.value:
        return "finalize_bo_config"
    if phase == CampaignPhase.RUNNING.value:
        return "finalize_bo_proposal"
    return "finalize_interpretation"
```

**关键理解**：这个路由函数被四个节点共用（`generate_hypotheses`、`configure_bo`、`run_bo_iteration`、`interpret_results`）。它通过检查最后一条消息是否有 tool_calls 来判断 LLM 是否想调用工具：
- 有 → 去 tool_node 执行
- 没有 → LLM 已完成推理，去 finalize 节点整理结果

#### `route_after_tool_call` — 工具执行后回到哪

```python
def route_after_tool_call(state) -> Literal[
    "generate_hypotheses",
    "configure_bo",
    "run_bo_iteration",
    "interpret_results"
]:
    origin = state.get("tool_origin_node", "run_bo_iteration")
    if origin == "generate_hypotheses": return "generate_hypotheses"
    if origin == "configure_bo": return "configure_bo"
    if origin == "interpret_results": return "interpret_results"
    return "run_bo_iteration"
```

`tool_origin_node` 字段记录了"谁发起的 tool call"，工具执行完后回到原发起节点。这样发起节点可以在第二次被调用时处理工具结果。

**这形成了一个循环**（以 configure_bo 为例）：

```
configure_bo → (LLM output has tool_calls)
    → route_after_tool_capable_node → "tool_node"
    → tool_node 执行工具
    → route_after_tool_call → "configure_bo"（因为 tool_origin_node == "configure_bo"）
    → configure_bo（第2次，_has_recent_tool_result 为 True）
    → (LLM 可能再调工具，也可能输出最终 JSON)
    → route_after_tool_capable_node → ...
```

这个循环可以执行**多轮**——configure_bo 需要调用三个工具，所以循环三次。

#### `route_after_reflect` — 反思后的分支

```python
def route_after_reflect(state) -> Literal["run_bo_iteration", "generate_hypotheses", "__end__"]:
    action = state.get("next_action", "")
    if action == NextAction.STOP.value:
        return END                           # 结束整个图
    if action == NextAction.RECONFIGURE.value:
        return "generate_hypotheses"         # 重走假设+配置流程
    return "run_bo_iteration"                # 继续 BO 循环
```

注意 `RECONFIGURE` 回到 `generate_hypotheses` 而不是 `configure_bo`——这意味着重配时会重新生成假设，因为新的实验证据可能改变了化学理解。

### 9.6 图的拓扑连接与编译

```python
# ─── 添加所有节点 ───
graph.add_node("analyze_problem", analyze_problem)
graph.add_node("generate_hypotheses", generate_hypotheses)
graph.add_node("finalize_hypotheses", finalize_hypotheses)
graph.add_node("configure_bo", configure_bo)
graph.add_node("finalize_bo_config", finalize_bo_config)
graph.add_node("run_bo_iteration", run_bo_iteration)
graph.add_node("finalize_bo_proposal", finalize_bo_proposal)
graph.add_node("await_human_results", await_human_results)
graph.add_node("interpret_results", interpret_results)
graph.add_node("finalize_interpretation", finalize_interpretation)
graph.add_node("reflect_and_decide", reflect_and_decide)
graph.add_node("tool_node", tool_node)

# ─── 固定边（无条件跳转）───
graph.add_edge(START, "analyze_problem")              # 入口
graph.add_edge("analyze_problem", "generate_hypotheses")
graph.add_edge("finalize_hypotheses", "configure_bo")
graph.add_edge("finalize_bo_config", "run_bo_iteration")
graph.add_edge("finalize_bo_proposal", "await_human_results")
graph.add_edge("await_human_results", "interpret_results")
graph.add_edge("finalize_interpretation", "reflect_and_decide")

# ─── 条件边（动态路由）───
# 四个 "可能调用工具" 的节点 → 共用同一个路由函数
graph.add_conditional_edges("generate_hypotheses", route_after_tool_capable_node)
graph.add_conditional_edges("configure_bo", route_after_tool_capable_node)
graph.add_conditional_edges("run_bo_iteration", route_after_tool_capable_node)
graph.add_conditional_edges("interpret_results", route_after_tool_capable_node)

# tool_node 执行完 → 根据 tool_origin_node 回到发起节点
graph.add_conditional_edges("tool_node", route_after_tool_call)

# reflect_and_decide → 三种可能
graph.add_conditional_edges("reflect_and_decide", route_after_reflect)

# ─── 编译 ───
return graph.compile(checkpointer=MemorySaver())
```

**可视化拓扑**：

```
                              ┌──────────────────────────────────────────────┐
                              │                                              │
START → analyze_problem → generate_hypotheses ⟷ tool_node                  │
                                   │                                         │
                          finalize_hypotheses → configure_bo ⟷ tool_node    │
                                                      │                      │
                                              finalize_bo_config             │
                                                      │                      │
                                            run_bo_iteration ⟷ tool_node    │
                                                      │                      │
                                            finalize_bo_proposal             │
                                                      │                      │
                                            await_human_results (interrupt)  │
                                                      │                      │
                                            interpret_results ⟷ tool_node   │
                                                      │                      │
                                            finalize_interpretation          │
                                                      │                      │
                                            reflect_and_decide               │
                                              │       │       │              │
                                           STOP   CONTINUE  RECONFIGURE ────┘
                                            │         │
                                           END    run_bo_iteration (直接回到 BO 循环)
```

### 9.7 完整执行流程 Walk-through

以 DAR 问题首次运行为例，跟踪一轮完整迭代：

**Phase 1: 初始化**
```
1. create_initial_state("Optimize DAR yield...") → state 初始化
2. graph.astream(initial_state, config) 开始执行
```

**Phase 2: 分析问题**
```
3. analyze_problem 节点执行
   - state["problem_spec"]["raw_description"] → LLM
   - LLM 返回 JSON：reaction_type="DAR", variables=[...], budget=30
   - 查知识库 → kb_context = DAR 的领域知识
   - 返回更新：problem_spec, kb_context, phase="analyzing"
```

**Phase 3: 生成假设**
```
4. generate_hypotheses (第1次)
   - _has_recent_tool_result → False（首次进入）
   - prompt 让 LLM 调用 hypothesis_generator 工具
   - LLM 返回 AIMessage 带 tool_calls=[{name: "hypothesis_generator", args: {...}}]
   - 返回：messages=[prompt, ai_response], tool_origin_node="generate_hypotheses"

5. route_after_tool_capable_node: 最后一条消息有 tool_calls → "tool_node"

6. tool_node 执行 hypothesis_generator
   - 工具整理问题和数据上下文
   - 返回 ToolMessage 追加到 messages

7. route_after_tool_call: tool_origin_node == "generate_hypotheses" → "generate_hypotheses"

8. generate_hypotheses (第2次)
   - _has_recent_tool_result → True（有 ToolMessage）
   - prompt 让 LLM 基于工具输出产出最终 JSON
   - LLM 返回 JSON（无 tool_calls）

9. route_after_tool_capable_node: 无 tool_calls, phase="hypothesizing" → "finalize_hypotheses"

10. finalize_hypotheses
    - 从对话中提取假设 JSON
    - 更新 working memory
    - 返回：hypotheses 列表, memory
```

**Phase 4: 配置 BO**
```
11-18. configure_bo ⟷ tool_node 循环 3 次
    第1次：LLM 调 embedding_method_advisor → tool_node → 回到 configure_bo
    第2次：LLM 调 surrogate_model_selector → tool_node → 回到 configure_bo
    第3次：LLM 调 af_selector → tool_node → 回到 configure_bo
    第4次：LLM 输出最终 JSON → finalize_bo_config
    
19. finalize_bo_config
    - 提取 BO 配置：embedding=one_hot, surrogate=gp_matern52, af=ei
    - 更新 config_history
```

**Phase 5: 第一轮 BO**
```
20. run_bo_iteration (第1次)
    - 无 tool result → prompt 让 LLM 调用 bo_runner
    - LLM 返回 tool_call

21. tool_node 执行 bo_runner
    - observations 为空 → 冷启动 → 返回 DoE 候选点
    - 例如：{"ligand": "SPhos", "base": "CsOPiv", "solvent": "NMP", "temperature": 135, "concentration": 0.28}

22. route_after_tool_call → run_bo_iteration

23. run_bo_iteration (第2次)
    - 有 tool result → LLM 用化学术语解释提议
    - 无 tool_calls → route 到 finalize_bo_proposal

24. finalize_bo_proposal
    - 从 ToolMessage 中提取 candidates, predictions, uncertainties
    - 存入 current_proposal
```

**Phase 6: 人机交互**
```
25. await_human_results
    - interrupt({candidate: proposal}) → 图暂停！
    - 外部代码展示提议，人类去做实验
    - 人类返回：Command(resume={"result": 45.0, "notes": "清澈黄色溶液"})
    - interrupt() 返回人类输入
    - 更新 observations, best_result, iteration += 1
```

**Phase 7: 解读结果**
```
26-29. interpret_results ⟷ tool_node 循环
    - LLM 调 result_interpreter → 处理结果 → 输出解读 JSON

30. finalize_interpretation
    - 添加 episodic memory
    - 可能添加 semantic rule
    - 更新 working memory
    - consolidate()
```

**Phase 8: 反思决策**
```
31. reflect_and_decide
    - 检查预算（1/30 → 远未耗尽）
    - 检查停滞（只有 1 个点 → 不可能停滞）
    - LLM 决策："continue"

32. route_after_reflect → "run_bo_iteration"

→ 回到 Phase 5，开始第二轮 BO 迭代
```

---

## 10. 第八层：测试体系

### 10.1 `test_mock.py`

#### MockChemBOLLM — 确定性 LLM Mock

```python
class MockChemBOLLM:
    def bind_tools(self, tools):
        return MockChemBOLLM(tools)  # 返回自己（mock 不真的绑定工具）

    def invoke(self, messages):
        prompt = _message_text(messages[-1])

        # 根据 prompt 中的关键字串匹配来决定返回什么
        if "Analyze this chemical optimization problem" in prompt:
            return AIMessage(content=json.dumps({
                "reaction_type": "DAR", "variables": SEARCH_SPACE, "budget": 5, ...
            }))

        if "Call the hypothesis_generator tool first" in prompt:
            # 模拟 LLM 调用工具
            return AIMessage(
                content="Calling hypothesis generator.",
                tool_calls=[{
                    "id": "hypothesis-tool",
                    "name": "hypothesis_generator",
                    "args": { ... }
                }]
            )

        if "Configure the BO pipeline" in prompt:
            called_tools = _tool_names(messages)
            # 检查哪些工具还没被调用，逐个调用
            if "embedding_method_advisor" not in called_tools:
                return AIMessage(tool_calls=[{
                    "name": "embedding_method_advisor", "args": { ... }
                }])
            if "surrogate_model_selector" not in called_tools:
                return AIMessage(tool_calls=[{
                    "name": "surrogate_model_selector", "args": { ... }
                }])
            if "af_selector" not in called_tools:
                return AIMessage(tool_calls=[{
                    "name": "af_selector", "args": { ... }
                }])
            # 所有工具都调用完了 → 返回最终配置
            return AIMessage(content=json.dumps({
                "embedding_method": "one_hot",
                "surrogate_model": "gp_matern52",
                "acquisition_function": "ei", ...
            }))
```

**设计巧妙之处**：
- `_tool_names(messages)` 扫描消息历史中的 ToolMessage，提取已调用的工具名。
- Mock LLM 通过检查"哪些工具还没调用"来决定下一步调哪个——精确模拟了真实 LLM 的多轮 tool call 行为。
- 反思节点根据 `_experiment_count(messages)` 决策：第 2 轮 reconfigure，第 4+ 轮 stop。

#### Mock Campaign 执行流程

```python
def run_mock_campaign():
    # 1. Monkey-patch LLM 工厂
    graph_module._create_llm = lambda settings: MockChemBOLLM()

    # 2. 构建图并运行
    graph = graph_module.build_chembo_graph(settings)
    graph.invoke(initial_state, config=config)
    state = graph.get_state(config).values  # 获取暂停时的完整状态

    # 3. 人机交互循环
    while state["phase"] != "completed":
        candidate = state["current_proposal"]["candidates"][0]
        result = mock_dar_objective(candidate)  # 合成目标函数
        graph.invoke(
            Command(resume={"result": result, "notes": "mock-run"}),
            config=config
        )
        state = graph.get_state(config).values

    # 4. 验证
    assert state["best_result"] > 0
    assert state["memory"]["episodic"]  # episodic memory 非空
    assert saw_reconfigure               # 经历了 reconfigure
```

**LangGraph 关键 API**：
- `graph.invoke(initial_state, config)` — 首次执行，会在 interrupt 处暂停
- `graph.get_state(config).values` — 获取当前状态
- `graph.invoke(Command(resume=data), config)` — 恢复执行

### 10.2 `test_interactive.py`

```python
def main():
    graph = build_chembo_graph(settings)
    config = {"configurable": {"thread_id": f"interactive-{settings.experiment_id}"}}

    # 首次执行（会在第一个 interrupt 处暂停）
    graph.invoke(initial_state, config=config)
    state = graph.get_state(config).values

    while state["phase"] != "completed":
        # 展示提议
        print(state["current_proposal"]["candidates"][0])

        # 人类输入
        result = float(input("Enter measured result: "))
        notes = input("Enter notes (optional): ")

        # 恢复执行
        graph.invoke(Command(resume={"result": result, "notes": notes}), config=config)
        state = graph.get_state(config).values
```

与 mock 测试的结构完全一样，只是用真实 LLM 和真人输入替代了 mock。

---

## 11. 关键设计模式总结

### 模式 1: Tool Call Loop（工具调用循环）

```
reasoning_node → route → tool_node → route → reasoning_node → route → finalize_node
```

- 四个推理节点（generate_hypotheses, configure_bo, run_bo_iteration, interpret_results）共用这个模式。
- 通过 `tool_origin_node` 状态字段追踪回路目的地。
- 通过 `_has_recent_tool_result()` 让节点区分"首次调用"和"工具返回后再次调用"。

### 模式 2: Two-Phase Node（双节点模式）

每个"智能操作"拆成两个节点：

| Reasoning Node（推理） | Finalize Node（最终化） |
|---|---|
| 调用 `llm_with_tools` | 调用裸 `llm` 或纯 Python |
| 可能触发 tool call 循环 | 不调用工具 |
| 可能被多次调用 | 只执行一次 |
| 返回原始 LLM 输出 | 解析 JSON、更新记忆、整理数据 |

### 模式 3: Candidate Pool Optimization（候选池优化）

不在连续嵌入空间做梯度优化，而是：
1. 枚举或采样一个大候选池
2. 编码 → GP 预测 → AF 打分 → 取 top-k

这对 categorical 变量主导的化学优化问题更自然、更可靠。

### 模式 4: Graceful Degradation（优雅降级）

到处都有 fallback：
- 高级编码器 → `GuardedFallbackEncoder` → 基础编码器
- GP 拟合失败 → Random Forest → 随机探索
- JSON 解析失败 → 修复 prompt 重试 → default 值
- LLM 输出不规范 → 重试一次 → 兜底默认

### 模式 5: Context Window Management（上下文窗口管理）

`_build_context_messages` 实现了滑动窗口 + 摘要策略：
- 系统提示词永远保留
- 最近 20 条消息完整保留
- 更老的消息压缩为摘要（`campaign_summary`）
- 摘要作为 `[CAMPAIGN SUMMARY]` 消息注入上下文

这保证了即使跑 30+ 轮 BO 也不会超出 LLM 的 context window。

### 模式 6: Interrupt-Resume Cycle（中断-恢复循环）

```python
# 暂停
human_response = interrupt({实验提议})

# 外部恢复
graph.invoke(Command(resume={实验结果}), config=config)
```

LangGraph 的 `interrupt()` + `Command(resume=...)` 机制完美映射了化学实验的工作流。状态通过 `MemorySaver` 持久化，即使进程重启也能恢复。
