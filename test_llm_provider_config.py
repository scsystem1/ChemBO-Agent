"""
Provider configuration coverage for OpenAI-compatible endpoints.
"""
from __future__ import annotations

from pathlib import Path

from config.settings import Settings

try:
    import core.graph as graph_module

    GRAPH_DEPS_AVAILABLE = True
except ModuleNotFoundError as exc:  # pragma: no cover - local env may lack optional deps
    graph_module = None
    GRAPH_DEPS_AVAILABLE = False
    IMPORT_ERROR = exc


def test_dashscope_kimi_defaults_to_dashscope_key_and_thinking():
    if not GRAPH_DEPS_AVAILABLE:
        print(f"Skipping provider config test: {IMPORT_ERROR}")
        return

    settings = Settings(
        llm_model="kimi-k2.5",
        llm_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    )

    assert graph_module._resolve_openai_api_key_env(settings, settings.llm_model.lower()) == "DASHSCOPE_API_KEY"
    assert graph_module._openai_compatible_model_kwargs(settings, settings.llm_model.lower()) == {
        "extra_body": {"enable_thinking": True}
    }


def test_explicit_openai_compatible_overrides_take_precedence():
    if not GRAPH_DEPS_AVAILABLE:
        print(f"Skipping provider config test: {IMPORT_ERROR}")
        return

    settings = Settings(
        llm_model="kimi-k2.5",
        llm_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        llm_api_key_env="CUSTOM_KEY_ENV",
        llm_enable_thinking=False,
    )

    assert graph_module._resolve_openai_api_key_env(settings, settings.llm_model.lower()) == "CUSTOM_KEY_ENV"
    assert graph_module._openai_compatible_model_kwargs(settings, settings.llm_model.lower()) == {}


def test_settings_defaults_use_kimi_dashscope_and_30_budget():
    settings = Settings()

    assert settings.llm_model == "kimi-k2.5"
    assert settings.llm_base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert settings.llm_api_key_env == "DASHSCOPE_API_KEY"
    assert settings.max_bo_iterations == 30
    assert settings.ablation_pure_reasoning is False


def test_yaml_presets_align_with_kimi_defaults():
    root = Path(__file__).resolve().parent
    lightning = Settings.from_yaml(str(root / "lightning.yaml"))
    dashscope = Settings.from_yaml(str(root / "dashscope_kimi.yaml"))
    ocm = Settings.from_yaml(str(root / "dashscope_kimi_ocm.yaml"))

    for preset in (lightning, dashscope):
        assert preset.llm_model == "kimi-k2.5"
        assert preset.llm_base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"
        assert preset.llm_api_key_env == "DASHSCOPE_API_KEY"
        assert preset.max_bo_iterations == 30

    assert ocm.llm_model == "kimi-k2.5"
    assert ocm.llm_base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert ocm.llm_api_key_env == "DASHSCOPE_API_KEY"
    assert ocm.max_bo_iterations == 30
    assert ocm.human_input_mode == "dataset_auto"
    assert ocm.ablation_pure_reasoning is True
