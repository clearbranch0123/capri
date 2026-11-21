from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Dict, Any, List, Optional

from .config import USER_CONFIG_DIR

logger = logging.getLogger(__name__)

CONTEXT_FILE = USER_CONFIG_DIR / "context.json"


@dataclass
class ContextItem:
    """Элемент непрерывности рабочего контекста."""
    id: str
    kind: str           # "thread" (открытая задача), "decision" (решение), "next_step" (шаг), "note" (заметка)
    content: str
    status: str = "active"  # "active", "resolved", "superseded"
    project: str = "default"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    source: str = "user"    # "user", "agent", "handoff"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> ContextItem:
        return cls(
            id=str(data.get("id", f"ctx-{int(time.time())}")),
            kind=str(data.get("kind", "thread")),
            content=str(data.get("content", "")),
            status=str(data.get("status", "active")),
            project=str(data.get("project", "default")),
            created_at=float(data.get("created_at", time.time())),
            updated_at=float(data.get("updated_at", time.time())),
            source=str(data.get("source", "user")),
        )


class ContextStore:
    """
    Хранилище непрерывности рабочего контекста (Context Continuity).
    Управляет открытыми задачами (open threads), принятыми решениями (decisions),
    следующими шагами (next steps) и мостом между сессиями диалогов.
    Сохраняется в ~/.config/capri/context.json независимо от сессий (sessions.db) и эмоций (emotion.json).
    """

    def __init__(self, file_path: Optional[Path] = None):
        self.file_path = file_path or CONTEXT_FILE
        self.active_project: str = "default"
        self.items: List[ContextItem] = []
        self.last_session_summary: str = ""
        self.last_update: float = time.time()
        self._last_loaded_mtime: float = 0.0
        self.load()

    def _sync_disk(self):
        """Проверяет mtime файла на диске и перезагружает состояние, если файл изменился."""
        if not self.file_path.exists():
            return
        try:
            mtime = self.file_path.stat().st_mtime
            if mtime > self._last_loaded_mtime:
                self.load()
        except Exception:
            pass

    def load(self):
        if not self.file_path.exists():
            return
        try:
            self._last_loaded_mtime = self.file_path.stat().st_mtime
            with open(self.file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.active_project = str(data.get("active_project", "default"))
                self.last_session_summary = str(data.get("last_session_summary", ""))
                self.last_update = float(data.get("last_update", time.time()))

                raw_items = data.get("items", [])
                self.items = [ContextItem.from_dict(it) for it in raw_items if isinstance(it, dict)]
        except Exception as ex:
            logger.warning(f"Failed to load context store: {ex}")

    def save(self):
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.last_update = time.time()
            data = {
                "active_project": self.active_project,
                "last_session_summary": self.last_session_summary,
                "last_update": self.last_update,
                "items": [it.to_dict() for it in self.items],
            }
            with open(self.file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            try:
                self._last_loaded_mtime = self.file_path.stat().st_mtime
            except Exception:
                self._last_loaded_mtime = time.time()
        except Exception as ex:
            logger.warning(f"Failed to save context store: {ex}")

    def add_item(
        self,
        kind: str,
        content: str,
        project: Optional[str] = None,
        source: str = "user",
    ) -> ContextItem:
        self._sync_disk()
        clean_content = content.strip()
        clean_kind = kind.lower().strip()
        if clean_kind in ("step", "next_step", "nextstep"):
            clean_kind = "next_step"
        elif clean_kind in ("task", "thread"):
            clean_kind = "thread"
        elif clean_kind not in ("decision", "note"):
            clean_kind = "thread"

        item_proj = (project or self.active_project or "default").strip()

        # Компактный гарантированно уникальный читаемый ID
        item_id = f"ctx-{int(time.time()) % 10000:04d}-{uuid.uuid4().hex[:6]}"
        item = ContextItem(
            id=item_id,
            kind=clean_kind,
            content=clean_content,
            status="active",
            project=item_proj,
            created_at=time.time(),
            updated_at=time.time(),
            source=source,
        )
        self.items.append(item)
        if len(self.items) > 50:
            self.items = self.items[-50:]
        self.save()
        return item

    def resolve_item(self, item_id: str) -> bool:
        self._sync_disk()
        clean_id = item_id.lower().strip()
        for it in self.items:
            if it.id.lower() == clean_id or it.id.lower().endswith(clean_id):
                it.status = "resolved"
                it.updated_at = time.time()
                self.save()
                return True
        return False

    def supersede_item(self, item_id: str, new_content: str) -> Optional[ContextItem]:
        self._sync_disk()
        clean_id = item_id.lower().strip()
        for it in self.items:
            if it.id.lower() == clean_id or it.id.lower().endswith(clean_id):
                it.status = "superseded"
                it.updated_at = time.time()
                new_item = self.add_item(
                    kind=it.kind,
                    content=new_content,
                    project=it.project,
                    source="user",
                )
                return new_item
        return None

    def delete_item(self, item_id: str) -> bool:
        self._sync_disk()
        clean_id = item_id.lower().strip()
        initial_len = len(self.items)
        self.items = [
            it for it in self.items
            if it.id.lower() != clean_id and not it.id.lower().endswith(clean_id)
        ]
        if len(self.items) < initial_len:
            self.save()
            return True
        return False

    def clear_context(self, project: Optional[str] = None) -> bool:
        self._sync_disk()
        if project:
            self.items = [it for it in self.items if it.project.lower() != project.lower()]
        else:
            self.items = []
            self.last_session_summary = ""
        self.save()
        return True

    def set_session_summary(self, summary: str):
        self._sync_disk()
        self.last_session_summary = summary.strip()
        self.save()

    def set_active_project(self, project: str):
        self._sync_disk()
        self.active_project = project.strip() or "default"
        self.save()

    def get_active_items(self, kind: Optional[str] = None, project: Optional[str] = None) -> List[ContextItem]:
        self._sync_disk()
        res = [it for it in self.items if it.status == "active"]
        if kind:
            res = [it for it in res if it.kind == kind]
        if project:
            res = [it for it in res if it.project.lower() == project.lower()]
        return res

    def get_prompt_context(self) -> str:
        """
        Формирует компактный, строго ограниченный по длине блок рабочего контекста для системного промпта.
        Ограничен (до ~250 токенов): берет до 4 открытых задач, 3 решений и 2 следующих шагов,
        с обрезкой длинного контента до 160 символов на запись.
        """
        active_items = self.get_active_items()
        if not active_items and not self.last_session_summary:
            return ""

        def _clip(text: str, max_len: int = 160) -> str:
            t = text.strip().replace("\n", " ")
            return t if len(t) <= max_len else t[:max_len].rstrip() + "..."

        threads = [it for it in active_items if it.kind == "thread"][-4:]
        decisions = [it for it in active_items if it.kind == "decision"][-3:]
        next_steps = [it for it in active_items if it.kind == "next_step"][-2:]
        notes = [it for it in active_items if it.kind == "note"][-2:]

        lines = []
        if self.active_project and self.active_project != "default":
            lines.append(f"Активный проект/направление: [{self.active_project}]")
        if self.last_session_summary:
            lines.append(f"Итог предыдущей сессии: {_clip(self.last_session_summary, 180)}")

        if threads:
            lines.append("Открытые рабочие задачи (Open Threads):")
            for t in threads:
                lines.append(f"  • [#{t.id}] {_clip(t.content)}")

        if decisions:
            lines.append("Ключевые принятые решения (Decisions):")
            for d in decisions:
                lines.append(f"  • [#{d.id}] {_clip(d.content)}")

        if next_steps:
            lines.append("Следующие шаги (Next Steps):")
            for ns in next_steps:
                lines.append(f"  • [#{ns.id}] {_clip(ns.content)}")

        if notes:
            lines.append("Рабочие заметки (Context Notes):")
            for n in notes:
                lines.append(f"  • {_clip(n.content)}")

        return f"""
---
[РАБОЧИЙ КОНТЕКСТ И НЕПРЕРЫВНОСТЬ ДИАЛОГА]:
{chr(10).join(lines)}
(Используй этот контекст для непрерывности работы. Если D спрашивает 'что дальше', 'на чем остановились' или 'продолжаем' — опирайся на эти открытые задачи и решения).
---
"""

    def get_status_card(self) -> str:
        """Форматированная карточка для команды /context в Cyberdeck."""
        active_items = self.get_active_items()
        threads = [it for it in active_items if it.kind == "thread"]
        decisions = [it for it in active_items if it.kind == "decision"]
        next_steps = [it for it in active_items if it.kind == "next_step"]
        notes = [it for it in active_items if it.kind == "note"]

        lines = [
            f"РАБОЧИЙ КОНТЕКСТ И НЕПРЕРЫВНОСТЬ (CONTEXT CONTINUITY):",
            f"  Проект:          {self.active_project}",
        ]
        if self.last_session_summary:
            lines.append(f"  Прошлая сессия:  {self.last_session_summary}")

        if threads:
            lines.append("\n  [Открытые задачи]:")
            for t in threads:
                lines.append(f"    • [#{t.id}] {t.content}")
        else:
            lines.append("\n  [Открытые задачи]: нет активных")

        if decisions:
            lines.append("\n  [Принятые решения]:")
            for d in decisions:
                lines.append(f"    ✓ [#{d.id}] {d.content}")

        if next_steps:
            lines.append("\n  [Следующие шаги]:")
            for ns in next_steps:
                lines.append(f"    → [#{ns.id}] {ns.content}")

        if notes:
            lines.append("\n  [Заметки]:")
            for n in notes:
                lines.append(f"    ℹ {n.content}")

        lines.append("\n  Команды: /context add <thread|decision|step> <текст> | /context resolve <id> | /context clear")
        return "\n".join(lines)
