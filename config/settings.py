"""
Global settings for the ChemBO Agent.
"""
from dataclasses import dataclass, field
from typing import Optional
import uuid
import yaml


@dataclass
class Settings:
    # --- LLM ---
    llm_model: str = "kimi-k2.5"
    llm_temperature: float = 0.3
    llm_max_tokens: int = 4096
    llm_base_url: Optional[str] = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    llm_api_key_env: Optional[str] = "DASHSCOPE_API_KEY"
    llm_enable_thinking: Optional[bool] = None
    
    # --- BO ---
    max_bo_iterations: int = 30
    batch_size: int = 1                    # candidates per iteration
    initial_doe_size: int = 5              # Design of Experiments for warmstart
    shortlist_top_k: int = 5               # shortlist size returned by bo_runner
    ablation_pure_reasoning: bool = False  # replace BO shortlist generation with LLM-only reasoning
    convergence_patience: int = 5          # iterations without improvement
    convergence_threshold: float = 0.01    # relative improvement threshold
    
    # --- Experiment ---
    experiment_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    experiment_name: str = "chembo_demo"
    
    # --- Memory ---
    memory_backend: str = "in_memory"      # "in_memory" | "sqlite" | "qdrant"
    episodic_memory_capacity: int = 200
    semantic_memory_path: Optional[str] = None
    
    # --- Knowledge Base ---
    knowledge_base_path: Optional[str] = None    # path to reaction KB JSON/YAML

    # --- Local Knowledge / RAG ---
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
