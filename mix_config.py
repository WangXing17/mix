from __future__ import annotations

import json
from pathlib import Path
from typing import Any

APP_NAME = "mix"

DEFAULT_CONFIG: dict[str, Any] = {
    "clis": [
        {
            "id": "claude",
            "name": "Claude Code",
            "command": "claude",
            "protocols": ["anthropic"],
            "model_args": ["--model", "{model}"],
        },
        {
            "id": "codex",
            "name": "Codex CLI",
            "command": "codex",
            "protocols": ["openai"],
            "model_args": ["--model", "{model}"],
        },
    ],
    "providers": [
        {
            "id": "adsgateway",
            "name": "AdsConflux Gateway",
            "type": "openai",
            "base_url": "https://chat.adsconflux.xyz/openapi/v1",
            "openai_base_url": "https://chat.adsconflux.xyz/openapi/v1",
            "anthropic_base_url": "https://chat.adsconflux.xyz/openapi",
            "api_key_env": "MMS_API_KEY",
            "models_endpoint": "/models",
            "models": [
                {"id": "glm-5", "name": "glm-5"},
                {"id": "qwen3.6-plus", "name": "qwen3.6-plus"},
                {"id": "gpt-5", "name": "gpt-5"},
            ],
        },
        {
            "id": "anthropic",
            "name": "Anthropic",
            "type": "anthropic",
            "base_url": "https://api.anthropic.com",
            "api_key_env": "ANTHROPIC_API_KEY",
            "models": [
                {"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6"},
                {"id": "claude-opus-4-7", "name": "Claude Opus 4.7"},
            ],
        },
        {
            "id": "openai",
            "name": "OpenAI",
            "type": "openai",
            "base_url": "https://api.openai.com/v1",
            "api_key_env": "OPENAI_API_KEY",
            "models": [
                {"id": "gpt-5", "name": "GPT-5"},
                {"id": "gpt-5-mini", "name": "GPT-5 Mini"},
            ],
        },
        {
            "id": "openrouter",
            "name": "OpenRouter",
            "type": "openai",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_env": "OPENROUTER_API_KEY",
            "models": [
                {"id": "anthropic/claude-sonnet-4.5", "name": "Claude Sonnet via OpenRouter"},
                {"id": "openai/gpt-5", "name": "GPT-5 via OpenRouter"},
            ],
        },
        {
            "id": "glm",
            "name": "GLM",
            "type": "openai",
            "base_url": "https://open.bigmodel.cn/api/paas/v4",
            "api_key_env": "GLM_API_KEY",
            "models": [
                {"id": "glm-4.5", "name": "GLM 4.5"},
                {"id": "glm-4.5-air", "name": "GLM 4.5 Air"},
            ],
        },
        {
            "id": "qwen",
            "name": "Qwen DashScope",
            "type": "openai",
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "api_key_env": "DASHSCOPE_API_KEY",
            "models": [
                {"id": "qwen3.6-plus", "name": "Qwen3.6 Plus"},
                {"id": "qwen3-coder-plus", "name": "Qwen3 Coder Plus"},
            ],
        },
    ],
}


def load_config_file(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        return DEFAULT_CONFIG
    loaded = json.loads(config_path.read_text(encoding="utf-8"))
    merged = dict(DEFAULT_CONFIG)
    merged.update({key: value for key, value in loaded.items() if value is not None})
    return merged
