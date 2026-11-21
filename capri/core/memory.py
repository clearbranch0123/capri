from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from .config import USER_CONFIG_DIR

logger = logging.getLogger(__name__)

MEMORY_FILE = USER_CONFIG_DIR / "memory.json"
DEFAULT_EPHEMERAL_TTL_SECONDS = 72 * 3600  # 3 days for temporary/ephemeral facts
MAX_CANDIDATES_PER_TURN = 3
MAX_PENDING_CANDIDATES = 20

RETENTION_MODES = ("long_term", "ephemeral")

# Strict secret/credential detection patterns for proposed memory candidates
_MEMORY_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Known API key prefixes & tokens
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}\b"),
    re.compile(r"\bsk_(?:live|test)_[A-Za-z0-9_\-]{8,}\b"),
    re.compile(r"\bgsk_[A-Za-z0-9_\-]{8,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{12,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_\-]{12,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"),
    re.compile(r"\bhf_[A-Za-z0-9]{12,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{8,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{10,}", re.IGNORECASE),
    # Private keys, PEM headers, URL credentials & 16-digit payment cards
    re.compile(r"BEGIN\s+(?:RSA|DSA|EC|OPENSSH|PGP)?\s*PRIVATE\s+KEY", re.IGNORECASE),
    re.compile(r"https?://[^/\s:@\"'<>]+:[^/\s@\"'<>]+@", re.IGNORECASE),
    re.compile(r"[?&](?:api[_-]?key|key|token|access[_-]?token|secret|auth|password|passwd|sig)=[^&\s\"'#<>]+", re.IGNORECASE),
    re.compile(r"\b(?:\d{4}[-\s]){3}\d{4}\b"),
    # Explicit password/secret/pin/seed/token terminology (RU + EN)
    re.compile(
        r"(?:^|[\s\"'«(\[{,;:])("
        r"password|passwd|passphrase|api[_\s-]?key|secret[_\s-]?key|client[_\s-]?secret|"
        r"access[_\s-]?token|refresh[_\s-]?token|auth[_\s-]?token|bot[_\s-]?token|api[_\s-]?token|"
        r"private[_\s-]?key|ssh[_\s-]?key|seed[_\s-]?phrase|recovery[_\s-]?code|pin[_\s-]?code|cvv|cvc|"
        r"парол(?:ь|я|ю|ем|е|и|ей|ям|ями|ях)|апи[\s-]?ключ\w*|api[\s-]?ключ\w*|"
        r"секретн(?:ый|ого|ая|ую|ом|ые)\s+(?:ключ|код|токен)\w*|"
        r"приватн(?:ый|ого|ая|ую|ом|ые)\s+ключ\w*|ssh[\s-]?ключ\w*|ключ\s+доступа|"
        r"токен\s+(?:доступа|от|для|авторизации|api|github|telegram|бота)|"
        r"пин[\s-]?код\w*|сид[\s-]?фраз\w*|кодов(?:ое|ого)\s+слово"
        r")(?:$|[\s\"'»)\]},;:=\-])",
        re.IGNORECASE | re.UNICODE,
    ),
    # Key=value secret assignments (including short pin=1234 or token: xyz)
    re.compile(
        r"(?:^|[\s,;])(?:[A-Z0-9_]*(?:API_KEY|SECRET|TOKEN|PASSWORD|PASSWD|PIN|PASS|CREDENTIAL)[A-Z0-9_]*|парол[ья]|токен|пин)\s*[:=]\s*\S+",
        re.IGNORECASE | re.UNICODE,
    ),
)


def contains_secret_or_sensitive_memory(text: str) -> bool:
    """Return True if text appears to contain API keys, passwords, tokens, or credentials."""
    if not text:
        return False
    for pat in _MEMORY_SECRET_PATTERNS:
        if pat.search(text):
            return True
    return False


def memory_payload_contains_secret(
    key: str,
    fact: str,
    *,
    source_quote: str = "",
    tags: Optional[List[str]] = None,
) -> bool:
    """Check key, fact, key=fact pair, optional source_quote, and tags for secrets/credentials."""
    raw_k = str(key or "").strip()
    raw_f = str(fact or "").strip()
    raw_q = str(source_quote or "").strip()
    norm_k = _normalize_memory_key(raw_k)
    candidates = [raw_k, raw_f, raw_q, f"{raw_k}={raw_f}", norm_k, f"{norm_k}={raw_f}"]
    if tags:
        candidates.extend(str(t or "").strip() for t in tags)
    return any(contains_secret_or_sensitive_memory(part) for part in candidates if part)


def _normalize_memory_key(raw: str) -> str:
    """Normalize a memory key to safe lowercase identifier form."""
    clean = re.sub(r"\s+", "_", (raw or "").strip().lower())
    clean = re.sub(r"[^\w\-]+", "", clean, flags=re.UNICODE)
    return clean.strip("_-")[:64]

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
    "identity": "Личность пользователя",
    "preferences": "Предпочтения",
    "environment": "Окружение / Софт",
    "projects": "Проекты",
    "constraints": "Ограничения / Правила",
}


@dataclass
class MemoryEntry:
    """Структурированная запись долговременной или временной памяти о пользователе."""
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
    source: str = "user"         # "user", "agent", "workbench", "approved_candidate"
    retention: str = "long_term" # "long_term" | "ephemeral"
    expires_at: Optional[float] = None

    def is_expired(self, now: Optional[float] = None) -> bool:
        if self.expires_at is None:
            return False
        ref = time.time() if now is None else float(now)
        return ref >= float(self.expires_at)

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

            raw_ret = str(data.get("retention", "long_term")).lower().strip()
            retention = raw_ret if raw_ret in RETENTION_MODES else "long_term"
            raw_exp = data.get("expires_at")
            expires_at = float(raw_exp) if isinstance(raw_exp, (int, float)) else None

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
                retention=retention,
                expires_at=expires_at,
            )

        return cls(key=clean_key, fact=str(data).strip())


@dataclass
class MemoryCandidate:
    """Кандидат памяти, предложенный моделью и ожидающий явного подтверждения пользователя."""
    id: str
    key: str
    fact: str
    category: str = "general"
    retention: str = "long_term"  # "long_term" | "ephemeral"
    importance: int = 3
    source_quote: str = ""
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> Optional["MemoryCandidate"]:
        if not isinstance(data, dict):
            return None
        key = _normalize_memory_key(str(data.get("key", "")))
        fact = re.sub(r"\s+", " ", str(data.get("fact", ""))).strip()[:240]
        if not key or not fact:
            return None
        raw_cat = str(data.get("category", "general")).lower().strip()
        cat = raw_cat if raw_cat in CATEGORIES else "general"
        raw_ret = str(data.get("retention", "long_term")).lower().strip()
        retention = raw_ret if raw_ret in RETENTION_MODES else "long_term"
        try:
            imp = max(1, min(5, int(data.get("importance", 3))))
        except Exception:
            imp = 3
        quote = re.sub(r"\s+", " ", str(data.get("source_quote", ""))).strip()[:180]
        cand_id = str(data.get("id", "")).strip()[:16] or uuid.uuid4().hex[:8]
        try:
            created = float(data.get("created_at", time.time()))
        except Exception:
            created = time.time()
        return cls(
            id=cand_id,
            key=key,
            fact=fact,
            category=cat,
            retention=retention,
            importance=imp,
            source_quote=quote,
            created_at=created,
        )


def validate_memory_candidates(
    raw_candidates: Any,
    *,
    user_text: str = "",
    existing_entries: Optional[Dict[str, MemoryEntry]] = None,
    existing_pending: Optional[List[MemoryCandidate]] = None,
    max_items: int = MAX_CANDIDATES_PER_TURN,
) -> List[MemoryCandidate]:
    """
    Строго валидирует и фильтрует предложенных моделью кандидатов памяти.
    - Отбрасывает любые ключи/пароли/токены/секреты (в key, fact, source_quote или паре key=fact).
    - Дедуплицирует относительно уже сохранённой памяти и текущей очереди pending.
    - Ограничивает число кандидатов за один раунд (по умолчанию до 3).
    """
    if isinstance(raw_candidates, dict):
        items = [raw_candidates]
    elif isinstance(raw_candidates, list):
        items = raw_candidates
    else:
        return []

    # Never propose ANY memory candidates extracted from a turn that contains secrets/credentials
    # (prevents models from paraphrasing a password/PIN into an innocent-looking key/fact).
    if user_text and contains_secret_or_sensitive_memory(user_text):
        logger.info("Skipping proposed memory candidates from turn containing secrets/credentials")
        return []

    entries_map = existing_entries or {}
    pending_keys = {c.key for c in (existing_pending or [])}
    seen_keys: set[str] = set()
    validated: List[MemoryCandidate] = []

    for item in items:
        if len(validated) >= max_items:
            break
        if not isinstance(item, dict):
            continue

        raw_key = str(item.get("key", "")).strip()
        raw_fact = str(item.get("fact", "")).strip()
        raw_quote = str(item.get("source_quote", "")).strip()

        # Hard secret/credential filter BEFORE normalization & queueing
        if (
            contains_secret_or_sensitive_memory(raw_key)
            or contains_secret_or_sensitive_memory(raw_fact)
            or contains_secret_or_sensitive_memory(raw_quote)
            or contains_secret_or_sensitive_memory(f"{raw_key}={raw_fact}")
        ):
            logger.info("Rejected sensitive/secret memory candidate (reason=secret_pattern_match)")
            continue

        cand = MemoryCandidate.from_dict(item)
        if cand is None:
            continue

        # Re-check normalized fields as defense in depth
        if (
            contains_secret_or_sensitive_memory(cand.key)
            or contains_secret_or_sensitive_memory(cand.fact)
            or contains_secret_or_sensitive_memory(f"{cand.key}={cand.fact}")
        ):
            continue

        if cand.key in seen_keys or cand.key in pending_keys:
            continue

        existing = entries_map.get(cand.key)
        if existing is not None and not existing.is_expired():
            norm_old = re.sub(r"\s+", " ", existing.fact).strip().casefold()
            norm_new = cand.fact.casefold()
            if norm_old == norm_new and existing.retention == cand.retention:
                continue

        seen_keys.add(cand.key)
        validated.append(cand)

    return validated


class MemoryStore:
    """
    Хранилище долговременной памяти и инструментов управления (Memory Workbench).
    Управляет структурированными фактами о пользователе, категоризацией, приоритетами,
    сроком жизни (long_term / ephemeral) и изолированной очередью кандидатов на подтверждение.
    Сохраняется в ~/.config/capri/memory.json с сохранением полной обратной совместимости.
    """

    def __init__(self, file_path: Optional[Path] = None):
        self.file_path = file_path or MEMORY_FILE
        self.pending_path = self.file_path.with_name(f"pending_{self.file_path.name}")
        self.entries: Dict[str, MemoryEntry] = {}
        self.pending_candidates: List[MemoryCandidate] = []
        self._last_loaded_mtime: float = 0.0
        self._last_pending_mtime: float = 0.0
        self._lock = threading.RLock()
        self.load()
        self._load_pending()

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

    def _sync_pending_disk(self):
        """Синхронизирует очередь кандидатов pending_memory.json, если файл обновился на диске."""
        with self._lock:
            if not self.pending_path.exists():
                return
            try:
                mtime = self.pending_path.stat().st_mtime
                if mtime > self._last_pending_mtime:
                    self._load_pending()
            except Exception:
                pass

    def _load_pending(self):
        with self._lock:
            if not self.pending_path.exists():
                self.pending_candidates = []
                return
            try:
                self._last_pending_mtime = self.pending_path.stat().st_mtime
                with open(self.pending_path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                loaded: List[MemoryCandidate] = []
                if isinstance(raw, list):
                    for item in raw:
                        cand = MemoryCandidate.from_dict(item)
                        if cand is not None and not (
                            contains_secret_or_sensitive_memory(cand.key)
                            or contains_secret_or_sensitive_memory(cand.fact)
                            or contains_secret_or_sensitive_memory(cand.source_quote)
                            or contains_secret_or_sensitive_memory(f"{cand.key}={cand.fact}")
                        ):
                            loaded.append(cand)
                self.pending_candidates = loaded[:MAX_PENDING_CANDIDATES]
            except Exception as ex:
                logger.warning(f"Failed to load pending memory candidates: {ex}")

    def _save_pending(self):
        with self._lock:
            self.pending_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self.pending_path.with_name(f".{self.pending_path.name}.tmp.{time.time_ns()}")
            try:
                payload = [c.to_dict() for c in self.pending_candidates]
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                try:
                    os.chmod(tmp_path, 0o600)
                except Exception:
                    pass
                tmp_path.replace(self.pending_path)
                try:
                    self._last_pending_mtime = self.pending_path.stat().st_mtime
                except Exception:
                    self._last_pending_mtime = time.time()
            except Exception as ex:
                logger.warning(f"Failed to save pending memory candidates: {ex}")
                if tmp_path.exists():
                    try:
                        tmp_path.unlink()
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

    def prune_expired(self, now: Optional[float] = None) -> int:
        """Удаляет истёкшие временные (ephemeral) записи из памяти."""
        with self._lock:
            ref = time.time() if now is None else float(now)
            expired_keys = [k for k, entry in self.entries.items() if entry.is_expired(now=ref)]
            if not expired_keys:
                return 0
            for k in expired_keys:
                del self.entries[k]
            self.save()
            return len(expired_keys)

    def add_or_update(
        self,
        key: str,
        fact: str,
        category: str = "general",
        importance: int = 3,
        confidence: float = 1.0,
        tags: Optional[List[str]] = None,
        source: str = "user",
        retention: str = "long_term",
        ttl_seconds: Optional[float] = None,
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
            ret = (retention or "long_term").lower().strip()
            if ret not in RETENTION_MODES:
                ret = "long_term"

            now = time.time()
            if ret == "ephemeral":
                ttl = float(ttl_seconds) if (ttl_seconds is not None and ttl_seconds > 0) else float(DEFAULT_EPHEMERAL_TTL_SECONDS)
                expires_at: Optional[float] = now + ttl
            else:
                expires_at = None

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
                entry.retention = ret
                entry.expires_at = expires_at
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
                    retention=ret,
                    expires_at=expires_at,
                )
                self.entries[k] = entry

            self.save()
            return entry

    # ── PENDING CANDIDATES (USER-CONTROLLED MEMORY GATE) ───────────────────

    def propose_candidates(self, raw_candidates: Any, user_text: str = "") -> List[MemoryCandidate]:
        """
        Валидирует и добавляет предложенных моделью кандидатов в изолированную очередь pending.
        НИКОГДА не записывает в memory.json и не инъецирует в системный промпт до подтверждения пользователя.
        """
        with self._lock:
            self._sync_disk()
            self._sync_pending_disk()
            self.prune_expired()
            validated = validate_memory_candidates(
                raw_candidates,
                user_text=user_text,
                existing_entries=self.entries,
                existing_pending=self.pending_candidates,
            )
            if not validated:
                return []
            self.pending_candidates.extend(validated)
            if len(self.pending_candidates) > MAX_PENDING_CANDIDATES:
                self.pending_candidates = self.pending_candidates[-MAX_PENDING_CANDIDATES:]
            self._save_pending()
            return validated

    def list_pending(self) -> List[MemoryCandidate]:
        with self._lock:
            self._sync_pending_disk()
            return list(self.pending_candidates)

    def _find_pending_index(self, cand_id_or_key: str) -> int:
        needle = (cand_id_or_key or "").strip().lower()
        if not needle:
            return -1
        # 1. Exact id match
        for idx, cand in enumerate(self.pending_candidates):
            if cand.id.lower() == needle:
                return idx
        # 2. Exact key match
        for idx, cand in enumerate(self.pending_candidates):
            if cand.key.lower() == needle:
                return idx
        # 3. Unambiguous id prefix match
        matches = [idx for idx, cand in enumerate(self.pending_candidates) if cand.id.lower().startswith(needle)]
        if len(matches) == 1:
            return matches[0]
        return -1

    def get_pending(self, cand_id_or_key: str) -> Optional[MemoryCandidate]:
        with self._lock:
            self._sync_pending_disk()
            idx = self._find_pending_index(cand_id_or_key)
            if idx < 0:
                return None
            return self.pending_candidates[idx]

    def approve_candidate(
        self,
        cand_id_or_key: str,
        *,
        key: Optional[str] = None,
        fact: Optional[str] = None,
        category: Optional[str] = None,
        retention: Optional[str] = None,
        importance: Optional[int] = None,
        tags: Optional[List[str]] = None,
        ttl_seconds: Optional[float] = None,
    ) -> Optional[MemoryEntry]:
        """Подтверждает кандидата и переносит его в активную память (memory.json)."""
        with self._lock:
            self._sync_disk()
            self._sync_pending_disk()
            idx = self._find_pending_index(cand_id_or_key)
            if idx < 0:
                return None
            cand = self.pending_candidates[idx]
            final_key = _normalize_memory_key(key) if key is not None else cand.key
            final_fact = (fact if fact is not None else cand.fact).strip()
            if not final_key or not final_fact:
                return None

            # Defense in depth: re-run secret filter right before saving (blocks edited secrets!)
            raw_key_check = key if key is not None else cand.key
            if memory_payload_contains_secret(
                raw_key_check,
                final_fact,
                source_quote=cand.source_quote,
                tags=tags,
            ):
                logger.warning(
                    "Blocked approval of pending memory candidate id=%s (reason=secret_pattern_match)",
                    cand.id,
                )
                return None

            final_cat = category if category is not None else cand.category
            final_ret = retention if retention is not None else cand.retention
            final_imp = importance if importance is not None else cand.importance

            entry = self.add_or_update(
                key=final_key,
                fact=final_fact,
                category=final_cat,
                importance=final_imp,
                tags=tags,
                source="approved_candidate",
                retention=final_ret,
                ttl_seconds=ttl_seconds,
            )
            del self.pending_candidates[idx]
            self._save_pending()
            return entry

    def reject_candidate(self, cand_id_or_key: str) -> bool:
        """Отклоняет и удаляет кандидата из очереди ожидания."""
        with self._lock:
            self._sync_pending_disk()
            idx = self._find_pending_index(cand_id_or_key)
            if idx < 0:
                return False
            del self.pending_candidates[idx]
            self._save_pending()
            return True

    def clear_pending(self) -> int:
        """Очищает всю очередь неподтверждённых кандидатов памяти."""
        with self._lock:
            self._sync_pending_disk()
            cnt = len(self.pending_candidates)
            if cnt:
                self.pending_candidates = []
                self._save_pending()
            return cnt

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
            self.prune_expired()
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

    def get_prompt_context(self, max_tokens_budget: int = 350, user_name: str = "User") -> str:
        """
        Формирует строгий компактный блок долговременной памяти для системного промпта.
        Факты сгруппированы по категориям и отсортированы по важности.
        Ограничен по бюджету токенов (~max_tokens_budget токенов, 1 токен ~ 4 символа).
        Ключи и факты строго ограничены по длине, исключая разрастание промпта.
        Включает ТОЛЬКО подтверждённые и не истёкшие записи (никогда pending_candidates).
        """
        u_name = (user_name or "").strip() or "User"
        with self._lock:
            self._sync_disk()
            self.prune_expired()
            if not self.entries:
                return ""

            sorted_entries = sorted(
                self.entries.values(),
                key=lambda x: (x.importance, x.updated_at),
                reverse=True,
            )

            # Бюджет по символам (~4 символа на токен)
            max_chars = max(200, max_tokens_budget * 4)
            header = f"---\n[ДОЛГОВРЕМЕННАЯ ПАМЯТЬ — СОХРАНЁННЫЕ ФАКТЫ ({u_name})]:\n"
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
                ret_suffix = " (временно)" if entry.retention == "ephemeral" else ""
                key_disp = f"{key_str}{ret_suffix}"
                entry_line_len = len(f"    • [{key_disp}]: {fact_str}\n")
                if curr_len + entry_line_len > budget_for_lines:
                    if not selected_entries and budget_for_lines > 50:
                        # Если даже первый факт превысил лимит, урезаем факт под бюджет
                        avail_fact = max(20, budget_for_lines - len(f"    • [{key_disp}]: ...\n"))
                        fact_str = fact_str[:avail_fact].rstrip() + "..."
                        selected_entries.append((entry.category, key_disp, fact_str))
                    break
                selected_entries.append((entry.category, key_disp, fact_str))
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
[ДОЛГОВРЕМЕННАЯ ПАМЯТЬ — СОХРАНЁННЫЕ ФАКТЫ ({u_name})]:
{chr(10).join(lines)}
(Используй эти факты естественно при необходимости, не перечисляя их без повода.)
---
"""

    def get_stats_card(self, user_name: str = "User") -> str:
        """Текстовая сводка для команды /memory в Cyberdeck."""
        u_name = (user_name or "").strip() or "User"
        with self._lock:
            self._sync_disk()
            self._sync_pending_disk()
            self.prune_expired()
            total = len(self.entries)
            pending = list(self.pending_candidates)

            if total == 0 and not pending:
                return (
                    f"🧠 MEMORY WORKBENCH // ДОЛГОВРЕМЕННАЯ ПАМЯТЬ ({u_name}):\n"
                    "  Память пуста. Запомни факты через: /memory add <ключ> <факт> или в диалоге."
                )

            lines = [
                f"🧠 MEMORY WORKBENCH // ДОЛГОВРЕМЕННАЯ ПАМЯТЬ ({u_name}, всего записей: {total}):",
            ]
            if total > 0:
                cat_counts = {}
                for cat in CATEGORIES:
                    cnt = sum(1 for e in self.entries.values() if e.category == cat)
                    if cnt > 0:
                        cat_counts[cat] = cnt

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
                        ret_tag = " [временно]" if it.retention == "ephemeral" else ""
                        lines.append(f"    • [{it.key}] ({stars}){ret_tag}: {it.fact}")
            else:
                lines.append("  Подтверждённых записей пока нет.")

            if pending:
                lines.append(f"\n  [ОЖИДАЮТ ПОДТВЕРЖДЕНИЯ ({len(pending)})]:")
                for cand in pending:
                    ret_lbl = "временно 72ч" if cand.retention == "ephemeral" else "постоянно"
                    lines.append(f"    • {cand.id} [{cand.key}] ({ret_lbl}): {cand.fact}")

            lines.append(
                "\n  Команды: /memory add <ключ> <факт> | /memory edit <ключ> <факт> | "
                "/memory pending | /memory approve <id> | /memory reject <id> | /forget <ключ> | /memory workbench"
            )
            return "\n".join(lines)

    def export_dict(self) -> dict:
        with self._lock:
            self._sync_disk()
            self.prune_expired()
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
