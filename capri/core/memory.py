from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from .config import USER_CONFIG_DIR

logger = logging.getLogger(__name__)

MEMORY_FILE = USER_CONFIG_DIR / "memory.json"

_RU_SUFFIXES = (
    "остями", "ами", "ями", "ого", "его", "ому", "ему", "ыми", "ими",
    "ах", "ях", "ов", "ев", "ей", "ой", "ий", "ый", "ая", "яя", "ое", "ее",
    "ом", "ем", "ам", "ям", "ую", "юю", "ию", "ие", "ии", "ы", "и", "а", "я", "у", "ю", "е", "о",
)


def _stem_token(token: str) -> str:
    t = token.casefold().strip()
    if len(t) <= 3:
        return t
    if any("а" <= ch <= "я" or ch == "ё" for ch in t):
        for suf in _RU_SUFFIXES:
            if t.endswith(suf) and len(t) - len(suf) >= 3:
                return t[: -len(suf)]
    else:
        for suf in ("ing", "ed", "es", "s"):
            if t.endswith(suf) and len(t) - len(suf) >= 3:
                return t[: -len(suf)]
    return t


def _tokenize_hybrid(text: str) -> list[str]:
    return [t for t in re.findall(r"[\w]+", (text or "").casefold(), flags=re.UNICODE) if len(t) >= 2]


def _char_trigrams(text: str) -> set[str]:
    clean = re.sub(r"\s+", " ", (text or "").casefold()).strip()
    if len(clean) < 3:
        return {clean} if clean else set()
    return {clean[i : i + 3] for i in range(len(clean) - 2)}

CATEGORIES = (
    "general",       # Общие факты
    "identity",      # Личность D (имя, профессия, биография, роль)
    "preferences",   # Предпочтения (стиль кода, стек, любимые вещи, тон общения)
    "environment",   # Окружение (ОС, Arch Linux, Niri, Wayland, софт, пути)
    "projects",      # Активные и постоянные проекты, репозитории
    "constraints",   # Ограничения и запреты (чего не делать, правила)
)

CATEGORY_LABELS = {
    "general": "Общее",
    "identity": "Личность D",
    "preferences": "Предпочтения",
    "environment": "Окружение / Софт",
    "projects": "Проекты",
    "constraints": "Ограничения / Правила",
}


@dataclass
class MemoryEntry:
    """Структурированная запись долговременной памяти о D."""
    key: str
    fact: str
    category: str = "general"
    importance: int = 3          # 1..5 (приоритет в системном промпте)
    confidence: float = 1.0      # 0.0..1.0 (степень уверенности)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    access_count: int = 0
    last_accessed: float = 0.0
    tags: List[str] = field(default_factory=list)
    source: str = "user"         # "user", "agent", "workbench"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, key: str, data: Any) -> MemoryEntry:
        """Создает MemoryEntry с полной поддержкой старого и нового формата JSON."""
        clean_key = str(key).lower().strip()[:64]
        if isinstance(data, str):
            return cls(
                key=clean_key,
                fact=data.strip(),
                category="general",
                importance=3,
            )

        if isinstance(data, dict):
            # Поддержка старого формата: {"fact": "...", "updated_at": "YYYY-MM-DD ..."}
            raw_upd = data.get("updated_at")
            if isinstance(raw_upd, (int, float)):
                upd_ts = float(raw_upd)
            elif isinstance(raw_upd, str) and raw_upd.strip():
                try:
                    upd_ts = time.mktime(time.strptime(raw_upd.strip(), "%Y-%m-%d %H:%M:%S"))
                except Exception:
                    try:
                        from datetime import datetime
                        upd_ts = datetime.fromisoformat(raw_upd.strip()).timestamp()
                    except Exception:
                        upd_ts = time.time()
            else:
                upd_ts = time.time()

            raw_cat = str(data.get("category", "general")).lower().strip()
            if raw_cat not in CATEGORIES:
                raw_cat = "general"

            raw_tags = data.get("tags", [])
            if not isinstance(raw_tags, list):
                raw_tags = []

            return cls(
                key=clean_key,
                fact=str(data.get("fact", "")).strip(),
                category=raw_cat,
                importance=max(1, min(5, int(data.get("importance", 3)))),
                confidence=max(0.0, min(1.0, float(data.get("confidence", 1.0)))),
                created_at=float(data.get("created_at", upd_ts)),
                updated_at=upd_ts,
                access_count=int(data.get("access_count", 0)),
                last_accessed=float(data.get("last_accessed", 0.0)),
                tags=[str(t).lower().strip() for t in raw_tags if str(t).strip()],
                source=str(data.get("source", "user")),
            )

        return cls(key=clean_key, fact=str(data).strip())


class MemoryStore:
    """
    Хранилище долговременной памяти и инструментов управления (Memory Workbench).
    Управляет структурированными фактами о D, категоризацией, приоритетами и поиском.
    Сохраняется в ~/.config/capri/memory.json с сохранением полной обратной совместимости.
    """

    def __init__(self, file_path: Optional[Path] = None):
        self.file_path = file_path or MEMORY_FILE
        self.entries: Dict[str, MemoryEntry] = {}
        self._last_loaded_mtime: float = 0.0
        self._lock = threading.RLock()
        self.load()

    def _sync_disk(self):
        """Проверяет mtime файла на диске и перезагружает состояние, если файл изменился."""
        with self._lock:
            if not self.file_path.exists():
                return
            try:
                mtime = self.file_path.stat().st_mtime
                if mtime > self._last_loaded_mtime:
                    self.load()
            except Exception:
                pass

    def load(self):
        with self._lock:
            if not self.file_path.exists():
                return
            try:
                self._last_loaded_mtime = self.file_path.stat().st_mtime
                with open(self.file_path, "r", encoding="utf-8") as f:
                    raw_data = json.load(f)
                    if isinstance(raw_data, dict):
                        items = raw_data.get("entries", raw_data)
                        self.entries = {}
                        for k, v in items.items():
                            clean_k = str(k).lower().strip()[:64]
                            if clean_k:
                                self.entries[clean_k] = MemoryEntry.from_dict(clean_k, v)
            except Exception as ex:
                logger.warning(f"Failed to load memory store: {ex}")

    def save(self):
        with self._lock:
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self.file_path.with_name(f".{self.file_path.name}.tmp.{time.time_ns()}")
            try:
                data = {k: entry.to_dict() for k, entry in self.entries.items()}
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                tmp_path.replace(self.file_path)
                try:
                    self._last_loaded_mtime = self.file_path.stat().st_mtime
                except Exception:
                    self._last_loaded_mtime = time.time()
            except Exception as ex:
                logger.warning(f"Failed to save memory store: {ex}")
                if tmp_path.exists():
                    try:
                        tmp_path.unlink()
                    except Exception:
                        pass

    def add_or_update(
        self,
        key: str,
        fact: str,
        category: str = "general",
        importance: int = 3,
        confidence: float = 1.0,
        tags: Optional[List[str]] = None,
        source: str = "user",
    ) -> MemoryEntry:
        with self._lock:
            self._sync_disk()
            k = key.lower().strip()[:64]
            f_text = fact.strip()
            cat = category.lower().strip() if category else "general"
            if cat not in CATEGORIES:
                cat = "general"
            imp = max(1, min(5, int(importance)))
            conf = max(0.0, min(1.0, float(confidence)))
            t_list = [t.lower().strip()[:32] for t in (tags or []) if t.strip()]

            now = time.time()
            if k in self.entries:
                entry = self.entries[k]
                entry.fact = f_text
                entry.category = cat
                entry.importance = imp
                entry.confidence = conf
                entry.updated_at = now
                if tags is not None:
                    entry.tags = t_list
                entry.source = source
            else:
                entry = MemoryEntry(
                    key=k,
                    fact=f_text,
                    category=cat,
                    importance=imp,
                    confidence=conf,
                    created_at=now,
                    updated_at=now,
                    tags=t_list,
                    source=source,
                )
                self.entries[k] = entry

            self.save()
            return entry

    def delete(self, key: str) -> bool:
        with self._lock:
            self._sync_disk()
            k = key.lower().strip()[:64]
            if k in self.entries:
                del self.entries[k]
                self.save()
                return True
            return False

    def clear(self, category: Optional[str] = None) -> int:
        with self._lock:
            self._sync_disk()
            if category:
                cat = category.lower().strip()
                keys_to_del = [k for k, v in self.entries.items() if v.category == cat]
                for k in keys_to_del:
                    del self.entries[k]
                if keys_to_del:
                    self.save()
                return len(keys_to_del)
            else:
                cnt = len(self.entries)
                self.entries = {}
                self.save()
                return cnt

    def _score_entry(
        self,
        entry: MemoryEntry,
        q_raw: str,
        q_tokens: list[str],
        q_stems: list[str],
        q_trigrams: set[str],
    ) -> float:
        key_l = entry.key.casefold()
        fact_l = entry.fact.casefold()
        tags_l = [t.casefold() for t in entry.tags]
        haystack = f"{key_l} {fact_l} {' '.join(tags_l)}"

        score = 0.0
        # 1. Exact substring matches (highest precision)
        if q_raw in key_l:
            score += 6.0
        if q_raw in fact_l:
            score += 4.5
        if any(q_raw in t for t in tags_l):
            score += 4.0

        # 2. Token & stem overlap (handles Russian/English inflections and multi-word queries)
        doc_tokens = _tokenize_hybrid(haystack)
        doc_token_set = set(doc_tokens)
        doc_stem_set = {_stem_token(t) for t in doc_tokens}
        if q_tokens:
            exact_hits = sum(1 for t in q_tokens if t in doc_token_set)
            stem_hits = sum(
                1
                for st in q_stems
                if any(ds.startswith(st) or st.startswith(ds) for ds in doc_stem_set if len(ds) >= 3 and len(st) >= 3)
            )
            score += (exact_hits / len(q_tokens)) * 4.0
            score += (stem_hits / len(q_tokens)) * 2.8

        # 3. Character 3-gram similarity (fuzzy resilience against typos)
        if q_trigrams and len(q_raw) >= 4:
            doc_tri = _char_trigrams(haystack)
            if doc_tri:
                overlap = len(q_trigrams & doc_tri) / max(1, len(q_trigrams))
                if overlap >= 0.45:
                    score += overlap * 2.2

        if score <= 0.0:
            return 0.0

        # 4. Importance + subtle recency prior
        imp_boost = 0.25 * max(1, min(5, entry.importance))
        age_days = max(0.0, (time.time() - entry.updated_at) / 86400.0)
        recency_boost = 0.35 / (1.0 + math.log1p(age_days))
        return score + imp_boost + recency_boost

    def search(
        self,
        query: str = "",
        category: Optional[str] = None,
        tag: Optional[str] = None,
    ) -> List[MemoryEntry]:
        with self._lock:
            self._sync_disk()
            q = query.casefold().strip()
            cat = category.lower().strip() if category else None
            t_filter = tag.lower().strip() if tag else None

            candidates = []
            for entry in self.entries.values():
                if cat and entry.category != cat:
                    continue
                if t_filter and t_filter not in entry.tags:
                    continue
                candidates.append(entry)

            if not q:
                candidates.sort(key=lambda x: (x.importance, x.updated_at), reverse=True)
                return candidates

            q_tokens = _tokenize_hybrid(q)
            q_stems = [_stem_token(t) for t in q_tokens]
            q_tri = _char_trigrams(q)

            scored: list[tuple[float, int, float, MemoryEntry]] = []
            for entry in candidates:
                sc = self._score_entry(entry, q, q_tokens, q_stems, q_tri)
                if sc > 0.0:
                    scored.append((sc, entry.importance, entry.updated_at, entry))

            scored.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
            return [item[3] for item in scored]

    def search_hybrid(
        self,
        query: str,
        limit: int = 6,
        category: Optional[str] = None,
    ) -> List[MemoryEntry]:
        """Associative hybrid recall (substring + stem + fuzzy trigram + importance + recency)."""
        if not (query or "").strip():
            return []
        return self.search(query=query, category=category)[: max(1, limit)]

    def get_prompt_context(self, max_tokens_budget: int = 350) -> str:
        """
        Формирует строгий компактный блок долговременной памяти для системного промпта.
        Факты сгруппированы по категориям и отсортированы по важности.
        Ограничен по бюджету токенов (~max_tokens_budget токенов, 1 токен ~ 4 символа).
        Ключи и факты строго ограничены по длине, исключая разрастание промпта.
        """
        with self._lock:
            self._sync_disk()
            if not self.entries:
                return ""

            sorted_entries = sorted(
                self.entries.values(),
                key=lambda x: (x.importance, x.updated_at),
                reverse=True,
            )

            # Бюджет по символам (~4 символа на токен)
            max_chars = max(200, max_tokens_budget * 4)
            header = "---\n[ДОЛГОВРЕМЕННАЯ ПАМЯТЬ — СОХРАНЁННЫЕ ФАКТЫ О D]:\n"
            footer = "\n(Используй эти факты естественно при необходимости, не перечисляя их без повода.)\n---\n"
            budget_for_lines = max_chars - len(header) - len(footer)

            # Отбираем наиболее важные записи, не превышая бюджет
            selected_entries: List[Tuple[str, str, str]] = []  # (category, key_str, fact_str)
            curr_len = 0
            for entry in sorted_entries[:24]:
                clean_k = entry.key.strip()
                key_str = clean_k if len(clean_k) <= 40 else clean_k[:37].rstrip() + "..."
                clean_f = entry.fact.strip().replace("\n", " ")
                fact_str = clean_f if len(clean_f) <= 120 else clean_f[:120].rstrip() + "..."
                entry_line_len = len(f"    • [{key_str}]: {fact_str}\n")
                if curr_len + entry_line_len > budget_for_lines:
                    if not selected_entries and budget_for_lines > 50:
                        # Если даже первый факт превысил лимит, урезаем факт под бюджет
                        avail_fact = max(20, budget_for_lines - len(f"    • [{key_str}]: ...\n"))
                        fact_str = fact_str[:avail_fact].rstrip() + "..."
                        selected_entries.append((entry.category, key_str, fact_str))
                    break
                selected_entries.append((entry.category, key_str, fact_str))
                curr_len += entry_line_len

            if not selected_entries:
                return ""

            by_cat: Dict[str, List[Tuple[str, str]]] = {}
            for cat, key_str, fact_str in selected_entries:
                by_cat.setdefault(cat, []).append((key_str, fact_str))

            lines = []
            for cat in CATEGORIES:
                cat_items = by_cat.get(cat, [])
                if not cat_items:
                    continue
                label = CATEGORY_LABELS.get(cat, cat.capitalize())
                lines.append(f"  [{label}]:")
                for key_str, fact_str in cat_items:
                    lines.append(f"    • [{key_str}]: {fact_str}")

            return f"""
---
[ДОЛГОВРЕМЕННАЯ ПАМЯТЬ — СОХРАНЁННЫЕ ФАКТЫ О D]:
{chr(10).join(lines)}
(Используй эти факты естественно при необходимости, не перечисляя их без повода.)
---
"""

    def get_stats_card(self) -> str:
        """Текстовая сводка для команды /memory в Cyberdeck."""
        with self._lock:
            self._sync_disk()
            total = len(self.entries)
            if total == 0:
                return (
                    "🧠 MEMORY WORKBENCH // ДОЛГОВРЕМЕННАЯ ПАМЯТЬ D:\n"
                    "  Память пуста. Запомни факты через: /memory add <ключ> <факт> или в диалоге."
                )

            cat_counts = {}
            for cat in CATEGORIES:
                cnt = sum(1 for e in self.entries.values() if e.category == cat)
                if cnt > 0:
                    cat_counts[cat] = cnt

            lines = [
                f"🧠 MEMORY WORKBENCH // ДОЛГОВРЕМЕННАЯ ПАМЯТЬ D (Всего записей: {total}):",
            ]
            cat_summary = ", ".join(f"{CATEGORY_LABELS.get(c, c)}: {n}" for c, n in cat_counts.items())
            lines.append(f"  Категории: {cat_summary}\n")

            for cat in CATEGORIES:
                items = [e for e in self.entries.values() if e.category == cat]
                if not items:
                    continue
                lbl = CATEGORY_LABELS.get(cat, cat.capitalize())
                lines.append(f"  [{lbl.upper()}]:")
                for it in sorted(items, key=lambda x: (x.importance, x.updated_at), reverse=True):
                    stars = "★" * it.importance
                    lines.append(f"    • [{it.key}] ({stars}): {it.fact}")

            lines.append("\n  Команды: /memory add <ключ> <факт> | /memory edit <ключ> <факт> | /forget <ключ> | /memory workbench")
            return "\n".join(lines)

    def export_dict(self) -> dict:
        with self._lock:
            self._sync_disk()
            return {k: v.to_dict() for k, v in self.entries.items()}

    def import_dict(self, data: dict) -> int:
        with self._lock:
            self._sync_disk()
            if not isinstance(data, dict):
                return 0
            count = 0
            for k, v in data.items():
                clean_k = str(k).lower().strip()[:64]
                if clean_k:
                    self.entries[clean_k] = MemoryEntry.from_dict(clean_k, v)
                    count += 1
            if count > 0:
                self.save()
            return count

    def replace_all(self, data: dict) -> int:
        """Полностью заменяет текущий словарь памяти переданным словарем (для обратной совместимости save_memories)."""
        with self._lock:
            self._sync_disk()
            if not isinstance(data, dict):
                data = {}
            self.entries = {}
            count = 0
            for k, v in data.items():
                clean_k = str(k).lower().strip()[:64]
                if clean_k:
                    self.entries[clean_k] = MemoryEntry.from_dict(clean_k, v)
                    count += 1
            self.save()
            return count
