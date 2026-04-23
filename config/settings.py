"""
Global settings for the ChemBO Agent.
"""
from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Optional
import uuid
import yaml


def _load_local_env_file() -> None:
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"").strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_local_env_file()


@dataclass
class Settings:
    # --- LLM ---
    llm_model: str = "kimi-k2.5"
    llm_temperature: float = 0.3
    llm_max_tokens: int = 4096
    llm_base_url: Optional[str] = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    llm_api_key_env: Optional[str] = "DASHSCOPE_API_KEY"
    llm_enable_thinking: Optional[bool] = True
    
    # --- BO ---
    max_bo_iterations: int = 40
    batch_size: int = 1                    # candidates per iteration
    initial_doe_size: int = 20             # Default warm-start DoE size
    warm_start_budget_ratio: float = 0.5   # max fraction of budget spent in warm start
    shortlist_top_k: int = 5               # shortlist size retained by the AutoBO runtime
    convergence_patience: int = 5          # iterations without improvement
    convergence_threshold: float = 0.01    # relative improvement threshold

    # --- AutoBO ---
    autobo_enabled: bool = True
    pure_reasoning_ablation_enabled: bool = False
    autobo_surrogate_pool: list[str] = field(
        default_factory=lambda: [
            "gp_indicator_matern52",
            "gp_indicator_matern32",
            "gp_indicator_smk",
            "catboost",
            "deep_ensemble",
        ]
    )
    autobo_initial_active: str = "gp_indicator_matern52"
    autobo_fitness_weights: dict[str, float] = field(
        default_factory=lambda: {
            "seq": 0.35,
            "cal": 0.20,
            "rank": 0.15,
            "llm": 0.30,
        }
    )
    autobo_layer2_min_interval: int = 8
    autobo_hysteresis_cooldown: int = 3
    autobo_switch_threshold: float = 0.5
    autobo_acq_top_k: int = 8
    autobo_shortlist_prefilter_multiplier: int = 10
    autobo_shortlist_hallucination_mode: str = "kriging_believer"
    autobo_eval_points: int = 10
    autobo_llm_acq_enabled: bool = True
    autobo_llm_plaus_enabled: bool = True
    autobo_catboost_min_obs: int = 12
    autobo_nn_min_obs: int = 20
    autobo_seq_start_n: int = 8  # deprecated under full LOOCV mode
    autobo_cal_ci_level: float = 0.95
    autobo_cal_lower_bound: float = 0.70
    autobo_cal_upper_bound: float = 0.99
    autobo_stagnation_window: int = 3
    autobo_ei_mismatch_threshold: float = 0.50
    autobo_escape_enabled: bool = True
    autobo_escape_stagnation_window: int = 5
    autobo_escape_fraction: float = 0.25
    autobo_escape_recent_window: int = 8
    autobo_disagreement_slots: int = 1
    autobo_memory_cooldown_enabled: bool = True
    reflect_interval: int = 10
    
    # --- Experiment ---
    experiment_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    experiment_name: str = "chembo_demo"
    
    # --- Memory ---
    memory_backend: str = "in_memory"      # "in_memory" | "sqlite" | "qdrant"
    episodic_memory_capacity: int = 200
    semantic_memory_path: Optional[str] = None
    memory_node_budgets: dict[str, int] = field(
        default_factory=lambda: {
            "generate_hypotheses": 900,
            "warm_start": 1400,
            "run_bo_iteration": 1800,
            "select_candidate": 1400,
            "interpret_results": 1600,
            "reflect_and_decide": 1200,
            "default": 900,
        }
    )
    memory_recent_message_limits: dict[str, int] = field(
        default_factory=lambda: {
            "generate_hypotheses": 8,
            "warm_start": 6,
            "run_bo_iteration": 6,
            "select_candidate": 8,
            "interpret_results": 8,
            "reflect_and_decide": 8,
            "memory_consolidation": 4,
            "default": 6,
        }
    )
    memory_consolidation_every_n: int = 5
    memory_llm_consolidation_enabled: bool = True
    memory_llm_consolidation_cooldown_iters: int = 5
    memory_episode_keep_recent: int = 24
    memory_episode_keep_salient: int = 96
    
    # --- Knowledge Base ---
    knowledge_base_path: Optional[str] = None    # path to reaction KB JSON/YAML
    knowledge_enabled: bool = False
    knowledge_cache_enabled: bool = True
    knowledge_cache_dir: str = "./data/knowledge_cache"
    knowledge_cache_policy: str = "strict_problem_fingerprint"

    # --- Local Knowledge / RAG ---
    local_knowledge_dir: str = "./Local_Knowledge"
    chromadb_persist_dir: str = "./data/local_rag"
    rag_backend_preference: str = "auto"  # "auto" | "chromadb" | "json"
    rag_embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    rag_reranker_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    rag_top_k: int = 5
    rag_enable_hyde: bool = False
    rag_enable_contextual_compression: bool = False
    rag_enable_llm_rerank: bool = False
    rag_enable_local_rerank: bool = False
    rag_llm_temperature: float = 0.1
    rag_llm_max_tokens: int = 1024
    augmentation_llm_max_tokens: int = 4096
    augmentation_snippet_cap: int = 36
    augmentation_chunk_char_budget: int = 900
    augmentation_llm_snippet_compression: bool = False
    web_search_max_results: int = 6

    # --- External Retrieval / Leakage Filtering ---
    tavily_api_key: str = "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"
    leakage_filter_strict: bool = True
    web_search_domains: list[str] | None = None
    
    # --- Paths ---
    output_dir: str = "./outputs"
    checkpoint_dir: str = "./checkpoints"
    
    # --- Human-in-the-loop ---
    human_input_mode: str = "dataset_auto" # "dataset_auto" | "terminal" | "api" | "file"
    human_input_timeout: int = 3600        # seconds
    inject_campaign_summary_in_context: bool = False
    interpret_results_fast_path_enabled: bool = True
    interpret_results_surprise_threshold: float = 1.5
    warm_start_per_point_llm_interpret: bool = False
    
    @classmethod
    def from_yaml(cls, path: str) -> "Settings":
        if path is None:
            return cls()
        with open(path) as f:
            data = yaml.safe_load(f)
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
