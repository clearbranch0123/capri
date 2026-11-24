"""Lightweight bilingual UI string helper (`ru` default, `en` supported)."""

from __future__ import annotations

import os
from typing import Any, Optional


_EN_CATEGORIES = {
    "identity": "Identity",
    "preferences": "Preferences",
    "environment": "Environment",
    "projects": "Projects",
    "general": "General",
}


def resolve_ui_lang(config: Any = None) -> str:
    """Return `'en'` or `'ru'` (honors `CAPRI_UI_LANG`, passed `config`, or saved `load_config()`)."""
    env_lang = (os.environ.get("CAPRI_UI_LANG") or "").strip().lower()
    if env_lang.startswith("en"):
        return "en"
    if env_lang.startswith("ru"):
        return "ru"
    cfg_obj = config
    if cfg_obj is None:
        try:
            from ..core.config import load_config

            cfg_obj = load_config()
        except Exception:
            cfg_obj = None
    if cfg_obj is not None:
        ui_obj = getattr(cfg_obj, "ui", cfg_obj)
        cfg_lang = str(getattr(ui_obj, "language", "") or "").strip().lower()
        if cfg_lang.startswith("en"):
            return "en"
        if cfg_lang.startswith("ru"):
            return "ru"
    return "ru"


def is_english_ui(config: Any = None, lang: Optional[str] = None) -> bool:
    resolved = (lang or resolve_ui_lang(config)).strip().lower()
    return resolved.startswith("en")


def tr_ui(ru_text: str, en_text: str, config: Any = None, *, lang: Optional[str] = None) -> str:
    return en_text if is_english_ui(config, lang=lang) else ru_text


def format_rounds(count: int, *, lang: Optional[str] = None, config: Any = None) -> str:
    n = abs(int(count))
    if is_english_ui(config, lang=lang):
        return f"{n} turn" if n == 1 else f"{n} turns"
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} раунд"
    if 2 <= (n % 10) <= 4 and not (12 <= (n % 100) <= 14):
        return f"{n} раунда"
    return f"{n} раундов"


def _ru_facts_noun(count: int) -> str:
    n = abs(int(count))
    if n % 10 == 1 and n % 100 != 11:
        return "факт"
    if 2 <= (n % 10) <= 4 and not (12 <= (n % 100) <= 14):
        return "факта"
    return "фактов"


def format_facts(saved_cnt: int, pend_cnt: int = 0, *, lang: Optional[str] = None, config: Any = None) -> str:
    n = int(saved_cnt)
    if is_english_ui(config, lang=lang):
        word = "fact" if abs(n) == 1 else "facts"
        return f"{n} {word} (+{pend_cnt})" if pend_cnt else f"{n} {word}"
    noun = _ru_facts_noun(n)
    return f"{n} {noun} (+{pend_cnt})" if pend_cnt else f"{n} {noun}"


def format_active_tools(cnt: int, *, lang: Optional[str] = None, config: Any = None) -> str:
    if is_english_ui(config, lang=lang):
        return f"{cnt} active"
    return f"{cnt} активны"


def mood_label(mood: str, *, detailed: bool = False, lang: Optional[str] = None, config: Any = None) -> str:
    key = (mood or "neutral").strip().lower()
    if is_english_ui(config, lang=lang):
        if detailed:
            return {
                "playful": "Playful / Mischievous",
                "warm": "Warm / Loyal",
                "sarcastic": "Sarcastic",
                "annoyed": "Annoyed",
                "neutral": "Calm",
            }.get(key, mood)
        return {
            "playful": "Playful",
            "warm": "Warm",
            "sarcastic": "Sarcastic",
            "annoyed": "Annoyed",
            "neutral": "Calm",
        }.get(key, mood)
    if detailed:
        return {
            "playful": "Игривое / Озорное",
            "warm": "Тёплое / Лояльное",
            "sarcastic": "Саркастичное",
            "annoyed": "Раздражённое",
            "neutral": "Спокойное",
        }.get(key, mood)
    return {
        "playful": "Игривое",
        "warm": "Тёплое",
        "sarcastic": "Саркастичное",
        "annoyed": "Раздражённое",
        "neutral": "Спокойное",
    }.get(key, mood)


def category_label(category: str, ru_labels: dict[str, str], *, lang: Optional[str] = None, config: Any = None) -> str:
    if is_english_ui(config, lang=lang):
        return _EN_CATEGORIES.get(category, category)
    return ru_labels.get(category, category)
