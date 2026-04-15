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
    llm_model: str = "qwen3-vl-235b-a22b-thinking"
    llm_temperature: float = 0.3
    llm_max_tokens: int = 4096
    llm_base_url: Optional[str] = "https://models.sjtu.edu.cn/api/v1"
    llm_api_key_env: Optional[str] = "MINIMAX_API_KEY"
    llm_enable_thinking: Optional[bool] = None
    
    # --- BO ---
    max_bo_iterations: int = 30
    batch_size: int = 1                    # candidates per iteration
    initial_doe_size: int = 5              # Design of Experiments for warmstart
    shortlist_top_k: int = 5               # shortlist size returned by bo_runner
    ablation_pure_reasoning: bool = False  # replace BO shortlist generation with LLM-only reasoning
    force_embedding_method: Optional[str] = None  # force a specific embedding key and skip LLM selection
    convergence_patience: int = 5          # iterations without improvement
    convergence_threshold: float = 0.01    # relative improvement threshold
    
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
            "configure_bo": 1100,
            "warm_start": 1400,
            "select_candidate": 1400,
            "run_reasoning_iteration": 1800,
            "interpret_results": 1600,
            "reflect_and_decide": 1200,
            "default": 900,
        }
    )
    memory_recent_message_limits: dict[str, int] = field(
        default_factory=lambda: {
            "generate_hypotheses": 8,
            "update_hypotheses": 8,
            "configure_bo": 8,
            "warm_start": 6,
            "run_bo_iteration": 6,
            "run_reasoning_iteration": 8,
            "select_candidate": 8,
            "interpret_results": 8,
            "reflect_and_decide": 8,
            "memory_consolidation": 4,
            "default": 6,
        }
    )
    memory_consolidation_every_n: int = 5
    memory_llm_consolidation_enabled: bool = True
    memory_episode_keep_recent: int = 24
    memory_episode_keep_salient: int = 96
    
    # --- Knowledge Base ---
    knowledge_base_path: Optional[str] = None    # path to reaction KB JSON/YAML

    # --- Local Knowledge / RAG ---
    enable_knowledge_augmentation: bool = True
    enable_runtime_retrieval: bool = True
    local_knowledge_dir: str = "./Local_Knowledge"
    chromadb_persist_dir: str = "./data/local_rag"
    rag_embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    rag_reranker_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    rag_top_k: int = 5
    rag_enable_hyde: bool = True
    rag_enable_contextual_compression: bool = True
    rag_enable_llm_rerank: bool = False
    rag_enable_local_rerank: bool = False
    rag_llm_temperature: float = 0.1
    rag_llm_max_tokens: int = 1024
    augmentation_llm_max_tokens: int = 4096
    augmentation_snippet_cap: int = 36
    augmentation_chunk_char_budget: int = 900

    # --- External Retrieval / Leakage Filtering ---
    semantic_scholar_api_key: str = ""
    tavily_api_key: str = "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"
    leakage_filter_strict: bool = True
    web_search_domains: list[str] | None = None
    
    # --- Paths ---
    output_dir: str = "./outputs"
    checkpoint_dir: str = "./checkpoints"
    
    # --- Human-in-the-loop ---
    human_input_mode: str = "dataset_auto" # "dataset_auto" | "terminal" | "api" | "file"
    human_input_timeout: int = 3600        # seconds
    
    @classmethod
    def from_yaml(cls, path: str) -> "Settings":
        if path is None:
            return cls()
        with open(path) as f:
            data = yaml.safe_load(f)
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
