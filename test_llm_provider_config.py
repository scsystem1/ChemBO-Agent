"""
Provider configuration coverage for OpenAI-compatible endpoints.
"""
from __future__ import annotations

try:
    import core.graph as graph_module
    from config.settings import Settings

    TEST_DEPS_AVAILABLE = True
except ModuleNotFoundError as exc:  # pragma: no cover - local env may lack optional deps
    graph_module = None
    Settings = None
    TEST_DEPS_AVAILABLE = False
    IMPORT_ERROR = exc


def test_dashscope_kimi_defaults_to_dashscope_key_and_thinking():
    if not TEST_DEPS_AVAILABLE:
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
    if not TEST_DEPS_AVAILABLE:
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
