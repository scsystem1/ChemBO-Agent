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
