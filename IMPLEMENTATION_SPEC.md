# ChemBO Agent — Phase 1 Demo: Complete Implementation Specification

## Document Purpose

This document is the **complete implementation blueprint** for the ChemBO Agent Phase 1 demo. It accompanies a skeleton codebase that defines the architecture (state schema, LangGraph topology, tool interfaces, component pools, memory system). **Your task is to fill in all `TODO [codex]` stubs and make the system work end-to-end**, so that given ONLY a natural-language problem description, the agent autonomously runs a full Bayesian Optimization campaign for chemical reaction optimization (with human-in-the-loop for wet-lab experiments).

---

## 1. Architecture Overview

### 1.1 Core Philosophy

- **Single LLM cognitive core** (not multi-agent) — one powerful LLM orchestrates everything
- **LLM as enhancement to GP**, not replacement — the LLM provides domain reasoning, component selection, and interpretation; principled Bayesian methods (GP, acquisition functions) handle posterior updating and uncertainty quantification
- **6 tools** are the LLM's hands — it reasons about WHAT to do, tools do the HOW
- **3-layer memory** — Working (scratchpad), Episodic (per-iteration records), Semantic (abstracted rules)
- **Component pools** — the LLM selects embedding, surrogate, and AF from curated registries of real implementations

### 1.2 Data Flow (Single Iteration)

```
Human provides problem description
        │
        ▼
[analyze_problem] ── LLM parses problem → ProblemSpec
        │
        ▼
[configure_bo] ── LLM calls 3 advisor/selector tools → BOConfig
        │           (selects embedding, surrogate, AF from pools)
        ▼
[generate_hypotheses] ── LLM generates chemistry hypotheses
        │
        ▼
 ┌──────────────── BO LOOP ────────────────┐
 │                                         │
 │  [run_bo_iteration]                     │
 │      LLM calls bo_runner tool           │
 │      (fit surrogate + optimize AF)      │
 │      → proposed candidate               │
 │          │                              │
 │          ▼                              │
 │  [await_human_results]                  │
 │      interrupt() — human runs wet-lab   │
 │      human returns: yield + notes       │
 │          │                              │
 │          ▼                              │
 │  [interpret_results]                    │
 │      LLM calls result_interpreter       │
 │      → chemical explanation             │
 │      → memory updates                   │
 │          │                              │
 │          ▼                              │
 │  [reflect_and_decide]                   │
 │      LLM evaluates progress             │
 │      → CONTINUE / RECONFIGURE / STOP    │
 │          │                              │
 │          ├─ CONTINUE → run_bo_iteration │
 │          ├─ RECONFIGURE → configure_bo  │
 │          └─ STOP → END                  │
 └─────────────────────────────────────────┘
```

### 1.3 File Structure

```
chembo_agent/
├── main.py                          # Entry point — CLI runner
├── requirements.txt                 # Dependencies
├── IMPLEMENTATION_SPEC.md           # This document
├── config/
│   ├── __init__.py
│   └── settings.py                  # Global settings dataclass
├── core/
│   ├── __init__.py
│   ├── state.py                     # ChemBOState TypedDict + sub-structures
│   └── graph.py                     # LangGraph definition (7 nodes + routing)
├── tools/
│   ├── __init__.py
│   └── chembo_tools.py              # 6 LangChain tools
├── pools/
│   ├── __init__.py
│   └── component_pools.py           # Embedding, Surrogate, AF registries
├── memory/
│   ├── __init__.py
│   └── memory_manager.py            # 3-layer memory system
├── knowledge/
│   ├── __init__.py
│   └── reaction_kb.py               # Hardcoded domain knowledge for DAR/BH/Suzuki
└── examples/
    └── dar_problem.yaml             # Example DAR problem definition
```

---

## 2. Critical Implementation Tasks

### TASK 1: `bo_runner` Tool — The Computational Engine (HIGHEST PRIORITY)

**File**: `tools/chembo_tools.py`, function `bo_runner`

**Current state**: Stub that returns placeholder JSON.

**Required implementation**:

This function must execute a REAL BO iteration. It receives the LLM-selected configuration (embedding method key, surrogate model key, AF key) plus all observed data, and returns proposed next candidate(s).

**Step-by-step logic**:

```python
def bo_runner(...) -> str:
    # 1. PARSE INPUTS
    search_space = json.loads(search_space_str)      # list of variable defs
    obs_list = json.loads(observations_str)           # [{candidate: {}, result: float}, ...]
    emb_params = json.loads(embedding_params_str)
    sur_params = json.loads(surrogate_params_str)
    acq_params = json.loads(af_params_str)

    # 2. HANDLE COLD START (no observations yet)
    if len(obs_list) < initial_doe_size:
        # Generate DoE points using Latin Hypercube Sampling or random
        # For categoricals: sample uniformly from domain
        # For continuous: sample within bounds via LHS
        return json.dumps({"candidates": [doe_candidate], ...})

    # 3. BUILD EMBEDDING PIPELINE
    # Look up EMBEDDING_POOL[embedding_method].factory
    # Each factory returns an encoder: encode(candidate_dict) -> np.array
    #
    # Implementation per method:
    #   "one_hot": 
    #       For each categorical var: one-hot vector of len(domain)
    #       For each continuous var: (value - lower) / (upper - lower) normalized
    #       Concatenate all → flat vector
    #
    #   "fingerprint_concat":
    #       For vars with SMILES: compute Morgan fingerprint (radius=2, nBits=256)
    #       For other categoricals: one-hot
    #       For continuous: normalized
    #       Concatenate → flat vector
    #
    #   "llm_embedding":
    #       Convert candidate to natural language string, e.g.:
    #         "Ligand: XPhos, Base: Cs2CO3, Solvent: DMAc, Temp: 120°C, Conc: 0.3M"
    #       Call OpenAI text-embedding-3-large API → 3072-dim vector
    #       Apply PCA to reduce to ~64 dims if > 20 data points available
    #
    #   "chemberta":
    #       For each molecular variable: encode SMILES via ChemBERTa → 768-dim
    #       For non-molecular: one-hot
    #       Concatenate + optional PCA
    #
    #   "bayesbe_encoding":
    #       Use BayBE's SubstanceEncoding for molecular categoricals
    #       Or fall back to one_hot if BayBE not installed

    X = np.array([encode(obs["candidate"]) for obs in obs_list])  # (n, d)
    Y = np.array([obs["result"] for obs in obs_list])              # (n,)

    # 4. BUILD SURROGATE MODEL
    # Use BoTorch for GP variants, sklearn for RF
    #
    #   "gp_matern52":
    #       model = SingleTaskGP(
    #           train_X=torch.tensor(X),
    #           train_Y=torch.tensor(Y).unsqueeze(-1),
    #           covar_module=MaternKernel(nu=2.5, ard_num_dims=X.shape[1])
    #       )
    #       mll = ExactMarginalLogLikelihood(model.likelihood, model)
    #       fit_gpytorch_mll(mll)
    #
    #   "gp_rbf": Same but with RBFKernel
    #
    #   "gp_mixture_kernel":
    #       Separate kernels for categorical dims (HammingKernel or CategoricalKernel)
    #       and continuous dims (MaternKernel)
    #       Combined via ProductKernel or AdditiveKernel
    #       Wrap in SingleTaskGP
    #
    #   "dkl":
    #       Build a simple 3-layer MLP (d → 128 → 64 → 32)
    #       + GP layer on the 32-dim output
    #       Use DKL training from GPyTorch examples
    #       Fall back to gp_matern52 if < 20 data points
    #
    #   "random_forest":
    #       model = RandomForestRegressor(n_estimators=100)
    #       model.fit(X, Y)
    #       Wrap in a BoTorch-compatible interface or handle separately
    #
    #   "gp_tanimoto":
    #       Use custom TanimotoKernel (bit_vector similarity)
    #       Only valid when embedding produces binary fingerprints

    # 5. BUILD + OPTIMIZE ACQUISITION FUNCTION
    #   "ei": ExpectedImprovement(model, best_f=Y.max())
    #   "ucb": UpperConfidenceBound(model, beta=af_params.get("beta", 0.2))
    #   "qei": qExpectedImprovement(model, best_f=Y.max(), num_fantasies=128)
    #   "qucb": qUpperConfidenceBound(model, beta=af_params.get("beta", 0.2))
    #   "ts": Use Thompson Sampling — sample from GP posterior, optimize sample
    #   "nehvi": qNoisyExpectedHypervolumeImprovement (multi-objective)
    #
    #   For GP-based:
    #       candidates, acq_values = optimize_acqf(
    #           acq_function=af,
    #           bounds=encoded_bounds,   # bounds in embedding space
    #           q=batch_size,
    #           num_restarts=10,
    #           raw_samples=512,
    #       )
    #
    #   For RF-based: 
    #       Use random search + model prediction to find best candidate

    # 6. DECODE CANDIDATES
    # Map the optimized embedding-space vector back to the original variable space
    # For one_hot: argmax for categoricals, denormalize for continuous
    # For other embeddings: project to nearest valid point in the search space
    #   - For categoricals: encode all options, find nearest neighbor to proposed
    #   - For continuous: denormalize

    # 7. COMPUTE PREDICTIONS + UNCERTAINTIES
    # pred_mean, pred_std = model.posterior(candidate_encoded).mean, .variance.sqrt()

    # 8. RETURN
    return json.dumps({
        "status": "success",
        "candidates": [decoded_candidates],
        "predictions": [pred_mean.item()],
        "uncertainties": [pred_std.item()],
        "acquisition_values": [acq_value.item()],
        "surrogate_metrics": {
            "model": surrogate_model,
            "num_training_points": len(obs_list),
            "log_marginal_likelihood": mll_value  # if GP
        }
    })
```

**Important edge cases**:
- Cold start: Return DoE points (LHS) before fitting a surrogate
- Categorical variable optimization: When AF optimization yields a point in continuous embedding space, map back to nearest valid categorical value
- Numerical stability: Normalize Y (standardize) before fitting GP
- Handle failures gracefully — if GP fitting fails, fall back to random sampling and log the error

**Validation**: After implementation, test with:
```python
result = bo_runner(
    embedding_method="one_hot",
    embedding_params="{}",
    surrogate_model="gp_matern52",
    surrogate_params="{}",
    acquisition_function="ei",
    af_params="{}",
    search_space='[{"name":"x","type":"continuous","domain":[0,1]}]',
    observations='[{"candidate":{"x":0.5},"result":0.3},{"candidate":{"x":0.2},"result":0.7}]',
    batch_size=1,
)
# Should return a valid candidate with prediction and uncertainty
```

---

### TASK 2: Embedding Factory Functions

**File**: `pools/component_pools.py` — fill in `factory` fields for each `PoolEntry`

Each factory should return a callable `Encoder` with interface:
```python
class Encoder:
    def encode(self, candidate: dict, search_space: list[dict]) -> np.ndarray:
        """Encode a single candidate point to a feature vector."""
        ...
    
    def encode_batch(self, candidates: list[dict], search_space: list[dict]) -> np.ndarray:
        """Encode multiple candidates. Returns (n, d) array."""
        ...
    
    def get_bounds(self, search_space: list[dict]) -> tuple[np.ndarray, np.ndarray]:
        """Return (lower_bounds, upper_bounds) in encoded space."""
        ...
    
    def decode(self, encoded: np.ndarray, search_space: list[dict]) -> dict:
        """Decode an encoded vector back to a candidate dict."""
        ...
    
    @property
    def dim(self) -> int:
        """Dimensionality of the encoded space."""
        ...
```

**Priority order for implementation**:
1. `one_hot` — simplest, baseline (MUST implement)
2. `fingerprint_concat` — uses RDKit (MUST implement)
3. `llm_embedding` — uses OpenAI API (implement if API key available, else skip)
4. `chemberta` / `bayesbe_encoding` — implement if libraries available

---

### TASK 3: Surrogate Model Factory Functions

**File**: `pools/component_pools.py` — fill in `factory` fields

Each factory should return a model with interface:
```python
class SurrogateModel:
    def fit(self, X: torch.Tensor, Y: torch.Tensor) -> None:
        """Fit the surrogate model on observed data."""
        ...
    
    def predict(self, X: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        """Return (mean, std) predictions."""
        ...
    
    @property
    def botorch_model(self):
        """Return underlying BoTorch model (for AF optimization)."""
        ...
```

**Priority order**:
1. `gp_matern52` — standard BoTorch SingleTaskGP (MUST implement)
2. `gp_mixture_kernel` — for mixed categorical-continuous (MUST implement for DAR demo)
3. `random_forest` — sklearn wrapper (SHOULD implement as fallback)
4. `dkl` — GPyTorch DKL example (NICE TO HAVE for Phase 1)
5. `gp_rbf`, `gp_tanimoto` — straightforward variants (LOW PRIORITY)

---

### TASK 4: Acquisition Function Factory Functions

**File**: `pools/component_pools.py` — fill in `factory` fields

Each factory should return a BoTorch acquisition function or a compatible wrapper.

**Priority order**:
1. `ei` — BoTorch `ExpectedImprovement` (MUST)
2. `ucb` — BoTorch `UpperConfidenceBound` (MUST)
3. `ts` — Thompson Sampling via `posterior_sample` (SHOULD)
4. `qei`, `qucb` — batch variants (NICE TO HAVE)
5. `nehvi` — multi-objective (LOW PRIORITY for Phase 1)

---

### TASK 5: LangGraph Routing Robustness

**File**: `core/graph.py`

The current routing logic handles the basic flow but needs hardening:

1. **Tool call loop**: The `should_call_tools` and `route_after_tool_call` functions must correctly handle chains of tool calls. For example, `configure_bo` may trigger 3 sequential tool calls (embedding_advisor → surrogate_selector → af_selector). After each tool result comes back, the LLM may call the next tool. The routing must:
   - After tool results, always route back to the LLM node that initiated the call
   - Track which phase initiated the tool call to route back correctly
   - Handle the case where the LLM calls multiple tools in a single response

2. **Phase tracking**: Ensure `state["phase"]` is always correctly set before routing decisions. Add defensive checks.

3. **Message accumulation**: The `messages` list grows unboundedly. Implement a sliding window or summarization strategy to prevent context overflow. Suggested approach:
   - Keep the last 20 messages in full
   - Summarize older messages into a "campaign summary" message
   - Always keep the system prompt

4. **Error handling**: If the LLM produces an unparseable response (no valid JSON when expected), the node should retry once with a more explicit prompt, then proceed with defaults.

5. **`await_human_results` → `current_proposal` gap**: Currently `run_bo_iteration` calls the bo_runner tool but the result isn't automatically stored in `state["current_proposal"]`. Fix this:
   - After `bo_runner` returns, parse the candidates from the tool result
   - Store in `state["current_proposal"]` so `await_human_results` can display them
   - This may require a small post-processing step after the BO tool call

---

### TASK 6: Memory Integration into Graph Nodes

**File**: `core/graph.py` and `memory/memory_manager.py`

Currently the memory system is defined but NOT integrated into the graph nodes. Required changes:

1. **In `interpret_results`**: After the LLM interprets results, parse its output for:
   - Episodic memory entry → call `memory_manager.add_episode()`
   - Semantic rule (if confident enough) → call `memory_manager.add_semantic_rule()`
   - Update working memory with current focus

2. **In `configure_bo` and `run_bo_iteration`**: Include memory context in the prompt:
   ```python
   memory_context = memory_manager.get_context_for_llm()
   prompt += f"\n\nMEMORY CONTEXT:\n{memory_context}"
   ```

3. **In `reflect_and_decide`**: Pass memory to the reflection prompt. Semantic rules should influence the CONTINUE/RECONFIGURE/STOP decision.

4. **Serialization**: The `MemoryManager` must be serialized into `state["memory"]` (dict) at the end of each node and deserialized at the beginning. Use `MemoryManager.to_dict()` / `MemoryManager.from_dict()`.

---

### TASK 7: Knowledge Base Integration

**File**: `core/graph.py` — `analyze_problem` node

After parsing the problem, look up domain knowledge:
```python
from knowledge import format_knowledge_for_llm

reaction_type = parsed_spec.get("reaction_type", "")
kb_context = format_knowledge_for_llm(reaction_type)

# Append to system message or include in next prompt
```

This KB context should be available to `configure_bo` (for informed component selection) and `generate_hypotheses` (for grounded hypothesis generation).

---

### TASK 8: Human-in-the-Loop Testing Interface

**File**: New file `test_interactive.py`

Create a test script that:
1. Builds the graph
2. Runs it with a DAR problem
3. When `interrupt()` fires, prompts the user in the terminal for experimental results
4. Resumes the graph with the human input
5. Continues until the campaign completes

```python
# Pseudocode
graph = build_chembo_graph(settings)
config = {"configurable": {"thread_id": "test-001"}}

# Initial run — will pause at first await_human_results
state = await graph.ainvoke(initial_state, config)

while state["phase"] != "completed":
    # Show the proposed experiment
    print(state["current_proposal"])
    
    # Get human input
    result = input("Enter yield (%): ")
    notes = input("Notes (optional): ")
    
    # Resume
    human_input = {"result": float(result), "notes": notes}
    state = await graph.ainvoke(
        Command(resume=human_input), 
        config
    )
```

Also create a **mock mode** (`test_mock.py`) that simulates the human using a synthetic objective function:
```python
def mock_dar_objective(candidate: dict) -> float:
    """Simulated DAR yield based on known chemistry patterns."""
    yield_val = 20.0  # base yield
    
    # Ligand effects
    ligand_bonus = {"XPhos": 25, "DavePhos": 20, "P(Cy)3": 15, "SPhos": 10, "PPh3": 0}
    yield_val += ligand_bonus.get(candidate.get("ligand", ""), 0)
    
    # Base effects  
    base_bonus = {"Cs2CO3": 15, "CsOPiv": 12, "K2CO3": 5, "KOAc": 0}
    yield_val += base_bonus.get(candidate.get("base", ""), 0)
    
    # Solvent effects
    solvent_bonus = {"DMAc": 15, "NMP": 10, "DMF": 5, "toluene": -5}
    yield_val += solvent_bonus.get(candidate.get("solvent", ""), 0)
    
    # Temperature: optimal around 120°C
    temp = candidate.get("temperature", 120)
    yield_val += -0.01 * (temp - 120) ** 2 + 10
    
    # Concentration: optimal around 0.3M
    conc = candidate.get("concentration", 0.3)
    yield_val += -100 * (conc - 0.3) ** 2 + 5
    
    # Add noise
    import random
    yield_val += random.gauss(0, 3)
    
    return max(0, min(100, yield_val))
    
# Theoretical max: ~90% (XPhos + Cs2CO3 + DMAc + 120°C + 0.3M)
```

---

## 3. Implementation Priority Order

| Priority | Task | Files | Effort | Impact |
|----------|------|-------|--------|--------|
| P0 | `one_hot` encoder | `pools/component_pools.py` | 1h | Unblocks all GP-based BO |
| P0 | `gp_matern52` surrogate factory | `pools/component_pools.py` | 1h | Core surrogate |
| P0 | `ei` AF factory | `pools/component_pools.py` | 30min | Core AF |
| P0 | `bo_runner` full implementation | `tools/chembo_tools.py` | 4h | The engine |
| P1 | `fingerprint_concat` encoder | `pools/component_pools.py` | 1h | Chemistry-aware encoding |
| P1 | `gp_mixture_kernel` surrogate | `pools/component_pools.py` | 2h | Handles DAR mixed vars |
| P1 | `ucb` + `ts` AF factories | `pools/component_pools.py` | 1h | Strategy diversity |
| P1 | Routing robustness (TASK 5) | `core/graph.py` | 2h | Reliability |
| P1 | Memory integration (TASK 6) | `core/graph.py`, `memory/` | 2h | Learning across iters |
| P2 | KB integration (TASK 7) | `core/graph.py` | 1h | Chemistry grounding |
| P2 | Mock test script (TASK 8) | `test_mock.py` | 2h | Validation |
| P2 | Interactive test (TASK 8) | `test_interactive.py` | 1h | Real usage |
| P3 | `random_forest` surrogate | `pools/component_pools.py` | 1h | Fallback |
| P3 | `llm_embedding` encoder | `pools/component_pools.py` | 2h | Advanced encoding |
| P3 | `dkl` surrogate | `pools/component_pools.py` | 3h | Embedding alignment |
| P3 | Batch AFs (`qei`, `qucb`) | `pools/component_pools.py` | 1h | Parallel experiments |

---

## 4. Design Decisions & Rationale

### 4.1 Why single LLM core instead of multi-agent?

We evolved from an 8-agent "expert committee" design toward a single cognitive core because:
- **Phase 1 demo needs simplicity** — multi-agent coordination adds debugging complexity
- **Tool-augmented single agent performs comparably** (validated by DeepRare ablations)
- **The 6 tools encapsulate what would be agent responsibilities** — each tool is essentially a "micro-agent" with a focused interface
- **Multi-agent can be layered on later** — the tool interfaces are stable enough to wrap into full agents

### 4.2 Why pools + LLM selection instead of fixed configuration?

This is a core innovation aligned with LMABO (2026): the LLM analyzes the problem's structural features (dimensionality, variable types, noise, data volume) and REASONS about which BO components are most suitable. This is:
- More principled than random/default selection
- More flexible than hardcoded rules
- Auditable — the LLM provides rationale for every choice
- Adaptable — mid-campaign reconfiguration is built-in

### 4.3 Why interrupt() for human-in-the-loop?

LangGraph's `interrupt()` is the correct mechanism because:
- It persists the graph state to the checkpointer
- It allows the graph to be resumed later (even after process restart)
- It integrates naturally with web UIs and APIs
- It maps exactly to the chemistry workflow: propose → wait for wet-lab → resume

### 4.4 Memory design choices

- **Working memory** is ephemeral — cleared between major phases
- **Episodic memory** is append-only with capacity limit — LRU eviction
- **Semantic memory** uses deduplication — similar rules are merged, not duplicated
- **Consolidation** (episodic → semantic) is LLM-driven — the LLM identifies patterns across episodes and articulates them as rules

---

## 5. Testing Checklist

Before declaring Phase 1 complete, verify:

- [ ] `bo_runner` returns valid candidates for a 2-variable continuous problem
- [ ] `bo_runner` returns valid candidates for a mixed categorical-continuous problem
- [ ] `one_hot` encoder correctly encodes/decodes DAR search space
- [ ] `gp_matern52` fits on 5+ observations and produces predictions
- [ ] `ei` acquisition function returns candidate with positive EI value
- [ ] Full graph runs for 3 iterations on mock DAR problem without crashing
- [ ] LLM correctly selects different configs for different problem types
- [ ] Memory stores episodic entries after each iteration
- [ ] `reflect_and_decide` correctly triggers STOP when budget exhausted
- [ ] `reflect_and_decide` correctly triggers RECONFIGURE when stagnation detected
- [ ] Cold start (0 observations) handled via DoE
- [ ] Human-in-the-loop interrupt/resume works in terminal mode

---

## 6. Environment Setup

```bash
# Create virtual environment
python -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Set API keys
export ANTHROPIC_API_KEY="your-key"          # for Claude (main LLM)
export OPENAI_API_KEY="your-key"             # for text-embedding-3-large (optional)

# Run mock test
python test_mock.py

# Run interactive demo
python main.py --problem-file examples/dar_problem.yaml
```

---

## 7. Key API References

### BoTorch (for GP surrogates + AFs)
```python
from botorch.models import SingleTaskGP
from botorch.fit import fit_gpytorch_mll
from botorch.acquisition import ExpectedImprovement, UpperConfidenceBound
from botorch.optim import optimize_acqf
from gpytorch.mlls import ExactMarginalLogLikelihood
from gpytorch.kernels import MaternKernel, RBFKernel, ScaleKernel
```

### LangGraph (for graph construction)
```python
from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import ToolNode
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import interrupt, Command
```

### RDKit (for fingerprints)
```python
from rdkit import Chem
from rdkit.Chem import AllChem
mol = Chem.MolFromSmiles(smiles)
fp = AllChem.GetMorganFingerprintAsBitVect(mol, radius=2, nBits=256)
```

---

## 8. Out of Scope for Phase 1 (Future Phases)

- Multi-objective optimization (NEHVI)
- Cross-campaign transfer learning
- Qdrant vector DB for episodic memory
- Neo4j knowledge graph for ligand-property relationships
- LLM-driven memory consolidation (episodic → semantic)
- EvoAgentX-style self-evolution of tools/prompts
- L1-L5 adjustable autonomy levels
- Real literature RAG (currently hardcoded KB)
- Web UI / Streamlit dashboard
- Causal BO with LLM-extracted mechanistic DAGs
