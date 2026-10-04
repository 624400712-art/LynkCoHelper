# -*- coding: utf-8 -*-
"""Provider-neutral facade for comment generation."""

from lynkco_common import env_value, load_env_data
from lynkco_chatanywhere import CommentGenerationError
from lynkco_chatanywhere import generate_comment as _generate_chatanywhere_comment
from lynkco_glm import generate_comment as _generate_glm_comment


DEFAULT_CHATANYWHERE_MODEL = "gpt-4o-mini"
DEFAULT_GLM_MODEL = "glm-4.6v-flash"
SUPPORTED_PROVIDERS = frozenset(("chatanywhere", "glm"))


def load_ai_config() -> dict:
    """Load AI settings with environment variables taking precedence over env.json."""
    ai_config = load_env_data().get("ai", {})
    if not isinstance(ai_config, dict):
        ai_config = {}

    def json_value(json_name: str, default: str = "") -> str:
        configured = ai_config.get(json_name, "")
        if isinstance(configured, str) and configured.strip():
            return configured.strip()
        return default

    def environment_value(environment_name: str) -> str:
        return env_value(environment_name)

    provider = (environment_value("LYNKCO_AI_PROVIDER") or
                json_value("provider", "chatanywhere")).casefold()
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError("LYNKCO_AI_PROVIDER must be chatanywhere or glm")
    if provider == "glm":
        api_key = (environment_value("GLM_API_KEY") or
                   environment_value("ZHIPU_API_KEY") or json_value("apiKey"))
        model = environment_value("GLM_MODEL") or json_value("model", DEFAULT_GLM_MODEL)
    else:
        api_key = environment_value("CHATANYWHERE_API_KEY") or json_value("apiKey")
        model = environment_value("CHATANYWHERE_MODEL") or json_value("model", DEFAULT_CHATANYWHERE_MODEL)
    return {"provider": provider, "api_key": api_key, "model": model}


def generate_comment(post: dict, api_key: str, model: str = None, session=None) -> str:
    """Generate a comment using the selected provider's implementation."""
    config = load_ai_config()
    provider = config["provider"]
    selected_model = model or config["model"]
    if provider == "glm":
        return _generate_glm_comment(
            post, api_key, model=selected_model, session=session,
        )
    if provider == "chatanywhere":
        return _generate_chatanywhere_comment(
            post, api_key, model=selected_model, session=session,
        )
    raise CommentGenerationError("模型供应商无效")


__all__ = ["CommentGenerationError", "generate_comment", "load_ai_config"]
