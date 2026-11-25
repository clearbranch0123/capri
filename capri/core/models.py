"""
Управление и динамическое получение доступных моделей по API-ключу и провайдеру (Groq, OpenRouter, Gemini, OpenAI).
"""

from __future__ import annotations

import httpx
from typing import List, Dict, Optional
from .config import AppConfig, is_local_llm_endpoint

# Алиасы моделей для быстрого ввода в консоли и GUI
MODEL_ALIASES: Dict[str, str] = {
    # Groq / Open weight models
    "120b": "openai/gpt-oss-120b",
    "gpt-120b": "openai/gpt-oss-120b",
    "gpt": "openai/gpt-oss-120b",
    "120": "openai/gpt-oss-120b",
    "gpt120": "openai/gpt-oss-120b",

    "27b": "qwen/qwen3.8-27b",
    "qwen": "qwen/qwen3.8-27b",
    "27": "qwen/qwen3.8-27b",
    "qwen27": "qwen/qwen3.8-27b",

    "20b": "openai/gpt-oss-20b",
    "gpt-20b": "openai/gpt-oss-20b",
    "20": "openai/gpt-oss-20b",

    # Gemini
    "flash": "gemini-2.5-flash",
    "gemini": "gemini-2.5-flash",
    "pro": "gemini-2.5-pro",

    # DeepSeek
    "deepseek": "deepseek-flash",
    "ds": "deepseek-flash",
    "v4": "deepseek-flash",
    "v4.1": "deepseek-flash",
    "v4-pro": "deepseek-v4-pro",
    "v3": "deepseek-chat",
    "r1": "deepseek-reasoner",

    # OpenRouter
    "openrouter-deepseek": "deepseek/deepseek-chat",
    "claude": "anthropic/claude-3.5-sonnet",

    # SambaNova
    "llama": "Meta-Llama-3.3-70B-Instruct",
    "llama70": "Meta-Llama-3.3-70B-Instruct",
    "minimax": "MiniMax-M3.1-Flash",
    "gemma": "gemma-4-31B-it",
}


def resolve_model_name(name_or_alias: str) -> str:
    """Разрешает алиас (например '120b', 'qwen', '20b') в полное имя модели."""
    cleaned = name_or_alias.strip().lower()
    return MODEL_ALIASES.get(cleaned, name_or_alias.strip())


def _fallback_models_for_provider(provider: str) -> List[str]:
    if provider == "deepseek":
        return ["deepseek-flash", "deepseek-v4-pro", "deepseek-chat", "deepseek-reasoner"]
    if provider == "groq":
        return ["openai/gpt-oss-120b", "qwen/qwen3.8-27b", "openai/gpt-oss-20b"]
    if provider == "sambanova":
        return ["Meta-Llama-3.3-70B-Instruct", "gpt-oss-120b", "MiniMax-M3.1-Flash", "gemma-4-31B-it"]
    if provider == "gemini":
        return ["gemini-2.5-flash", "gemini-2.5-pro"]
    if provider == "openrouter":
        return ["deepseek/deepseek-chat", "openai/gpt-4o-mini", "anthropic/claude-3.5-sonnet"]
    if provider == "openai":
        return ["gpt-4o-mini", "gpt-4o"]
    if provider == "ollama":
        return ["llama3.2", "qwen2.5:7b", "deepseek-r1:8b", "mistral"]
    if provider == "lmstudio":
        return ["local-model"]
    return ["deepseek-flash", "openai/gpt-oss-120b"]


async def fetch_available_models(config: AppConfig) -> List[str]:
    """
    Запрашивает список доступных диалоговых моделей по текущему API-ключу и провайдеру
    (включая локальные серверы Ollama / LM Studio без API-ключа).
    """
    provider = (config.llm.provider or "groq").lower()
    api_key = (config.llm.api_key or "").strip()
    base_url = (config.llm.base_url or "").strip()
    is_local = is_local_llm_endpoint(provider, base_url)

    if not api_key and not is_local:
        return _fallback_models_for_provider(provider)

    if base_url:
        try:
            url = f"{base_url.rstrip('/')}/models"
            headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
            async with httpx.AsyncClient(timeout=4.0) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code == 200:
                    data = resp.json().get("data", [])
                    result = []
                    for m in data:
                        mid = m.get("id", "")
                        if not mid:
                            continue
                        if any(
                            mid.startswith(x)
                            for x in [
                                "whisper",
                                "meta-llama/llama-prompt-guard",
                                "canopylabs",
                                "text-embedding",
                                "allam",
                                "openai/gpt-oss-safeguard",
                            ]
                        ):
                            continue
                        result.append(mid)
                    if result:
                        return result
        except Exception:
            pass

    return _fallback_models_for_provider(provider)


def format_models_list(current_model: str, available_models: List[str], provider: str = "GROQ") -> str:
    """Форматирует красивую киберпанк-карточку со списком моделей и алиасами."""
    lines = [f"МОДЕЛИ НЕЙРОСЕТИ (Провайдер: {provider.upper()}):\n"]
    lines.append(f"Текущая активная модель: [cyan]{current_model}[/cyan] [bold green][АКТИВНА][/bold green]\n")
    lines.append("Доступные модели по твоему API-ключу:")

    descriptions = {
        "qwen/qwen3.8-27b": "27B параметров, гиперскорость ответа (~1000 токенов/сек)",
        "openai/gpt-oss-120b": "120B параметров, глубокий ролеплей, сочный сарказм и логика",
        "openai/gpt-oss-20b": "20B параметров, ультра-быстрая",
        "gemini-2.5-flash": "Google Gemini 2.5 Flash, мультимодальная",
        "gemini-2.5-pro": "Google Gemini 2.5 Pro, максимальное качество рассуждений",
    }

    # Показываем основные модели
    seen = set()
    for m in available_models:
        if m in seen:
            continue
        seen.add(m)
        alias_hints = []
        for a, full in MODEL_ALIASES.items():
            if full == m and len(a) <= 6:
                alias_hints.append(a)
        alias_str = f"[{' / '.join(alias_hints[:2])}]" if alias_hints else ""
        desc = descriptions.get(m, "")
        desc_str = f" — {desc}" if desc else ""
        is_cur = " [АКТИВНА]" if m == current_model else ""

        lines.append(f"  • {alias_str:<14} {m}{desc_str}{is_cur}")

    lines.append("\nКоманда переключения:")
    lines.append("  /model 120b   или   /model qwen   или   /model <название>")
    return "\n".join(lines)
