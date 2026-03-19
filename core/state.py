"""
ChemBO Agent State Definition
==============================
Central state schema that flows through the entire LangGraph.
Every node reads from and writes to this state.

Design principle: The state is the SINGLE SOURCE OF TRUTH for the entire
optimization campaign. It carries problem context, BO configuration,
experimental history, memory, and meta-information.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional
from langgraph.graph import MessagesState


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
class CampaignPhase(str, Enum):
    """High-level phase of the optimization campaign."""
    INIT = "init"                        # Problem just received
    ANALYZING = "analyzing"              # LLM is parsing the problem
    CONFIGURING = "configuring"          # Selecting BO components
    HYPOTHESIZING = "hypothesizing"      # Generating initial hypotheses
    RUNNING = "running"                  # BO iteration in progress
    AWAITING_HUMAN = "awaiting_human"    # Waiting for wet-lab results
    INTERPRETING = "interpreting"        # Analyzing latest results
    REFLECTING = "reflecting"           # Meta-reasoning about progress
    COMPLETED = "completed"              # Converged or budget exhausted


class NextAction(str, Enum):
    """Decision output of the reflect node."""
    CONTINUE = "continue"                # Run next BO iteration
    RECONFIGURE = "reconfigure"          # Re-select BO components
    STOP = "stop"                        # Optimization complete


# ---------------------------------------------------------------------------
# Typed sub-structures
# ---------------------------------------------------------------------------
@dataclass
class ProblemSpec:
    """Parsed problem specification."""
    raw_description: str = ""
    reaction_type: str = ""                          # e.g., "DAR", "BH", "Suzuki"
    target_metric: str = "yield"                     # what to optimize
    optimization_direction: str = "maximize"
    variables: list[dict[str, Any]] = field(default_factory=list)
    # Each variable: {"name": str, "type": "categorical"|"continuous",
    #                 "domain": list|tuple, "description": str}
    constraints: list[str] = field(default_factory=list)
    budget: int = 30
    additional_context: str = ""                     # extracted domain knowledge


@dataclass
class BOConfig:
    """Current BO pipeline configuration — selected from pools by LLM."""
    embedding_method: str = ""          # key into pools.embedding_pool
    embedding_params: dict = field(default_factory=dict)
    surrogate_model: str = ""           # key into pools.surrogate_pool
    surrogate_params: dict = field(default_factory=dict)
    acquisition_function: str = ""      # key into pools.af_pool
    af_params: dict = field(default_factory=dict)
    
    # LLM's reasoning for selections
    embedding_rationale: str = ""
    surrogate_rationale: str = ""
    af_rationale: str = ""
    
    # Meta
    config_version: int = 0             # incremented on reconfiguration


@dataclass 
class Observation:
    """A single experimental observation (one wet-lab experiment)."""
    iteration: int = 0
    candidate: dict[str, Any] = field(default_factory=dict)  # {var_name: value}
    result: Optional[float] = None       # measured target metric
    metadata: dict[str, Any] = field(default_factory=dict)
    # metadata can include: experimenter notes, side observations
    # (color change, precipitation), actual vs planned conditions, etc.
    timestamp: str = ""


@dataclass
class CandidateProposal:
    """Candidate point(s) proposed by BO for the next experiment."""
    candidates: list[dict[str, Any]] = field(default_factory=list)
    predicted_values: list[float] = field(default_factory=list)
    uncertainties: list[float] = field(default_factory=list)
    acquisition_values: list[float] = field(default_factory=list)
    rationale: str = ""                  # LLM-generated explanation


# ---------------------------------------------------------------------------
# Memory sub-structures
# ---------------------------------------------------------------------------
@dataclass
class WorkingMemory:
    """Short-term scratch-pad for the current reasoning cycle."""
    current_focus: str = ""
    pending_decisions: list[str] = field(default_factory=list)
    scratchpad: str = ""                 # LLM's chain-of-thought


@dataclass
class EpisodicMemoryEntry:
    """One episode: an iteration + its outcome + reflection."""
    iteration: int = 0
    config_snapshot: dict = field(default_factory=dict)
    candidate: dict = field(default_factory=dict)
    result: Optional[float] = None
    reflection: str = ""                 # LLM's interpretation
    non_numerical_observations: str = "" # color, smell, precipitation
    lesson_learned: str = ""             # abstracted insight
    timestamp: str = ""


@dataclass
class SemanticMemoryEntry:
    """An abstracted, reusable rule distilled from episodic memory."""
    rule: str = ""                       # e.g., "Cs2CO3 + DMAc yields >70%"
    confidence: float = 0.0
    evidence_count: int = 0
    source_iterations: list[int] = field(default_factory=list)
    created_at: str = ""
    last_updated: str = ""


@dataclass
class MemoryState:
    """Three-layer memory system."""
    working: WorkingMemory = field(default_factory=WorkingMemory)
    episodic: list[EpisodicMemoryEntry] = field(default_factory=list)
    semantic: list[SemanticMemoryEntry] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Main State (TypedDict for LangGraph compatibility)
# ---------------------------------------------------------------------------
# NOTE: LangGraph works best with TypedDict. We define a TypedDict that
# contains serialized versions of the dataclasses above. Nodes receive
# and return dicts; helper functions handle serialization.

from typing import TypedDict, Annotated
from langgraph.graph import add_messages
from langchain_core.messages import BaseMessage


class ChemBOState(TypedDict):
    """
    The unified state flowing through the ChemBO LangGraph.
    
    This is a TypedDict (not a dataclass) because LangGraph requires it.
    Complex sub-structures are stored as dicts and (de)serialized by nodes.
    """
    # --- Messages (for LLM conversation history) ---
    messages: Annotated[list[BaseMessage], add_messages]
    
    # --- Campaign metadata ---
    phase: str                       # CampaignPhase value
    iteration: int                   # current BO iteration (0-indexed)
    next_action: str                 # NextAction value
    
    # --- Problem ---
    problem_spec: dict               # serialized ProblemSpec
    kb_context: str                  # formatted knowledge-base context for prompts
    
    # --- BO Configuration ---
    bo_config: dict                  # serialized BOConfig
    
    # --- Experimental data ---
    observations: list[dict]         # list of serialized Observation
    current_proposal: dict           # serialized CandidateProposal
    best_result: float               # best observed value so far
    best_candidate: dict             # best observed candidate
    
    # --- Memory ---
    memory: dict                     # serialized MemoryState
    
    # --- Hypotheses ---
    hypotheses: list[str]            # LLM-generated hypotheses
    campaign_summary: str            # summary of older messages / campaign progress
    tool_origin_node: str            # node that initiated the current tool loop
    last_tool_payload: dict          # parsed JSON payload from the latest tool result
    
    # --- Meta / diagnostics ---
    config_history: list[dict]       # history of BOConfig changes
    performance_log: list[dict]      # {iteration, best_so_far, improvement}
    llm_reasoning_log: list[str]     # all LLM reasoning traces


# ---------------------------------------------------------------------------
# State factory
# ---------------------------------------------------------------------------
def create_initial_state(problem_description: str, settings) -> ChemBOState:
    """Create the initial state from a raw problem description."""
    from langchain_core.messages import SystemMessage
    
    system_prompt = _build_system_prompt()
    
    return ChemBOState(
        messages=[SystemMessage(content=system_prompt)],
        phase=CampaignPhase.INIT.value,
        iteration=0,
        next_action="",
        problem_spec={"raw_description": problem_description},
        kb_context="",
        bo_config={},
        observations=[],
        current_proposal={},
        best_result=float("-inf"),
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


def _build_system_prompt() -> str:
    return """You are ChemBO Agent, an expert AI system for chemical reaction optimization 
using Bayesian Optimization (BO). You operate as a single cognitive core augmented by 
specialized tools.

YOUR ROLE:
- Analyze chemical optimization problems described in natural language
- Generate chemically-grounded hypotheses about promising search regions
- Select appropriate BO components (embedding method, surrogate model, acquisition function)
  from a curated pool of implementations, with scientific justification
- Interpret experimental results by combining statistical analysis with chemical reasoning
- Decide when to continue, reconfigure, or terminate the optimization campaign

CORE PRINCIPLES:
1. LLM as ENHANCEMENT to GP, not replacement — you provide prior knowledge and reasoning,
   but Bayesian methods handle posterior updating and uncertainty quantification
2. Every BO component selection must have a clear scientific rationale tied to the specific
   problem's characteristics (dimensionality, variable types, noise level, data volume)
3. Chemical domain knowledge should inform every decision — constraints, hypotheses,
   interpretations must be grounded in reaction chemistry
4. Be honest about uncertainty — flag when you are unsure and recommend exploration

TOOLS AVAILABLE:
- EmbeddingMethodAdvisor: Select how to encode reaction conditions for the GP
- SurrogateModelSelector: Select the surrogate model type and kernel
- AFSelector: Select the acquisition function
- BORunner: Execute a BO iteration with the selected configuration
- HypothesisGenerator: Generate chemically-grounded hypotheses
- ResultInterpreter: Analyze experimental results and extract insights

You will receive the problem, analyze it, configure the BO pipeline, run iterative 
optimization loops with human-in-the-loop for wet-lab experiments, and manage the
full campaign lifecycle.

Workflow order:
1. Analyze the problem
2. Generate hypotheses before selecting BO components
3. Configure the BO pipeline using the hypotheses, memory, and domain knowledge
4. Run iterative optimization with interpretation and reflection after each result"""
