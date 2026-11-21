"""
CAPRI // Модуль управления и персистентности сессий диалогов (Session Logistics Engine).
Архитектура: локальный SQLite в WAL-режиме, строгая изоляция сессий от личности и настроек,
стабильные UUIDv4, ключи пагинации, мягкое удаление (soft-delete), онлайн-бэкап и проверка целостности.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Any

from .config import USER_CONFIG_DIR

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = USER_CONFIG_DIR / "sessions.db"
CURRENT_SCHEMA_VERSION = 2

COMPACT_THRESHOLD_TURNS = 10
COMPACT_KEEP_RECENT_TURNS = 6
MAX_SESSION_SUMMARY_CHARS = 1500


def _build_fts5_query(query: str) -> str:
    """Convert raw user query into a safe FTS5 prefix MATCH expression."""
    tokens = [t for t in re.findall(r"[\w]+", query.casefold(), flags=re.UNICODE) if len(t) >= 2]
    if not tokens:
        return ""
    # Prefix matching (`token*`) handles Russian/English inflections cleanly in FTS5
    safe_terms = [f'"{tok[:40]}"*' for tok in tokens[:8]]
    return " OR ".join(safe_terms)


@dataclass
class TurnRecord:
    id: str
    session_id: str
    sequence: int
    timestamp: float
    user_text: str
    assistant_text: str
    thought: str = ""
    tools: List[Dict[str, Any]] = field(default_factory=list)
    model_messages: List[Dict[str, Any]] = field(default_factory=list)
    status: str = "completed"  # "completed", "interrupted", "error"


@dataclass
class SessionRecord:
    id: str
    title: str
    created_at: float
    updated_at: float
    is_archived: bool = False
    is_deleted: bool = False
    turn_count: int = 0
    preview: str = ""
    summary: str = ""
    compacted_up_to_seq: int = 0


class SessionManager:
    """
    Менеджер постоянного хранения сессий диалогов Capri.
    Потокобезопасен: открывает изолированное соединение на каждую операцию с таймаутом ожидания блокировки.
    """

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.db_path), timeout=8.0, check_same_thread=False)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA busy_timeout=5000;")
        con.execute("PRAGMA foreign_keys=ON;")
        con.execute("PRAGMA synchronous=NORMAL;")
        return con

    def _init_db(self):
        with self._lock, self._get_connection() as con:
            cur = con.cursor()
            cur.execute("PRAGMA user_version;")
            ver = cur.fetchone()[0]

            if ver == 0:
                con.execute("BEGIN IMMEDIATE;")
                con.execute("""
                    CREATE TABLE IF NOT EXISTS meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                """)
                con.execute("""
                    CREATE TABLE IF NOT EXISTS sessions (
                        id TEXT PRIMARY KEY,
                        title TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        is_archived INTEGER DEFAULT 0,
                        is_deleted INTEGER DEFAULT 0,
                        turn_count INTEGER DEFAULT 0,
                        preview TEXT DEFAULT '',
                        summary TEXT DEFAULT '',
                        compacted_up_to_seq INTEGER DEFAULT 0
                    );
                """)
                con.execute("""
                    CREATE TABLE IF NOT EXISTS turns (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                        sequence INTEGER NOT NULL,
                        timestamp REAL NOT NULL,
                        user_text TEXT NOT NULL,
                        assistant_text TEXT NOT NULL,
                        thought TEXT DEFAULT '',
                        tools_json TEXT DEFAULT '[]',
                        model_messages_json TEXT DEFAULT '[]',
                        status TEXT DEFAULT 'completed',
                        UNIQUE(session_id, sequence)
                    );
                """)
                con.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5(
                        turn_id UNINDEXED,
                        session_id UNINDEXED,
                        user_text,
                        assistant_text,
                        tokenize='unicode61'
                    );
                """)
                con.execute("CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at DESC);")
                con.execute("CREATE INDEX IF NOT EXISTS idx_sessions_active ON sessions(is_deleted, is_archived);")
                con.execute("CREATE INDEX IF NOT EXISTS idx_turns_session_seq ON turns(session_id, sequence ASC);")
                con.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION};")
                con.commit()
            elif ver == 1:
                con.execute("BEGIN IMMEDIATE;")
                existing_cols = {
                    r[1] for r in con.execute("PRAGMA table_info(sessions);").fetchall()
                }
                if "summary" not in existing_cols:
                    con.execute("ALTER TABLE sessions ADD COLUMN summary TEXT DEFAULT '';")
                if "compacted_up_to_seq" not in existing_cols:
                    con.execute("ALTER TABLE sessions ADD COLUMN compacted_up_to_seq INTEGER DEFAULT 0;")
                con.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5(
                        turn_id UNINDEXED,
                        session_id UNINDEXED,
                        user_text,
                        assistant_text,
                        tokenize='unicode61'
                    );
                """)
                con.execute("DELETE FROM turns_fts;")
                con.execute("""
                    INSERT INTO turns_fts (turn_id, session_id, user_text, assistant_text)
                    SELECT id, session_id, user_text, assistant_text FROM turns;
                """)
                con.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION};")
                con.commit()
            elif ver > CURRENT_SCHEMA_VERSION:
                logger.warning(f"База данных создана более новой версией схемы ({ver} > {CURRENT_SCHEMA_VERSION}).")

    def create_session(self, title: Optional[str] = None) -> SessionRecord:
        """Создает новую сессию с уникальным UUIDv4 и делает ее активной."""
        s_id = str(uuid.uuid4())
        now = time.time()
        session_title = (title or "Новый диалог").strip()

        with self._lock, self._get_connection() as con:
            con.execute("BEGIN IMMEDIATE;")
            con.execute(
                """
                INSERT INTO sessions (id, title, created_at, updated_at, is_archived, is_deleted, turn_count, preview)
                VALUES (?, ?, ?, ?, 0, 0, 0, '')
                """,
                (s_id, session_title, now, now),
            )
            con.execute(
                """
                INSERT INTO meta (key, value) VALUES ('active_session_id', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (s_id,),
            )
            con.commit()

        return SessionRecord(
            id=s_id,
            title=session_title,
            created_at=now,
            updated_at=now,
            is_archived=False,
            is_deleted=False,
            turn_count=0,
            preview="",
        )

    def get_active_session_id(self) -> Optional[str]:
        with self._get_connection() as con:
            row = con.execute("SELECT value FROM meta WHERE key = 'active_session_id'").fetchone()
            if row:
                s_id = row[0]
                # Проверяем, существует ли сессия и не удалена ли она
                s_row = con.execute("SELECT id FROM sessions WHERE id = ? AND is_deleted = 0", (s_id,)).fetchone()
                if s_row:
                    return s_id
        return None

    def get_or_create_active_session(self) -> SessionRecord:
        """Возвращает текущую активную сессию либо создает новую при ее отсутствии."""
        active_id = self.get_active_session_id()
        if active_id:
            s = self.get_session(active_id)
            if s and not s.is_deleted:
                return s
        return self.create_session("Новый диалог")

    def set_active_session(self, session_id: str) -> bool:
        """Устанавливает активную сессию."""
        with self._lock, self._get_connection() as con:
            row = con.execute("SELECT id FROM sessions WHERE id = ? AND is_deleted = 0", (session_id,)).fetchone()
            if not row:
                return False
            con.execute("BEGIN IMMEDIATE;")
            con.execute(
                """
                INSERT INTO meta (key, value) VALUES ('active_session_id', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (session_id,),
            )
            con.commit()
            return True

    def get_session(self, session_id: str) -> Optional[SessionRecord]:
        with self._get_connection() as con:
            row = con.execute(
                """
                SELECT id, title, created_at, updated_at, is_archived, is_deleted,
                       turn_count, preview, summary, compacted_up_to_seq
                FROM sessions WHERE id = ?
                """,
                (session_id,),
            ).fetchone()
            if not row:
                return None
            return SessionRecord(
                id=row["id"],
                title=row["title"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                is_archived=bool(row["is_archived"]),
                is_deleted=bool(row["is_deleted"]),
                turn_count=row["turn_count"],
                preview=row["preview"] or "",
                summary=row["summary"] or "",
                compacted_up_to_seq=int(row["compacted_up_to_seq"] or 0),
            )

    def list_sessions(
        self,
        limit: int = 50,
        include_archived: bool = False,
        include_deleted: bool = False,
    ) -> List[SessionRecord]:
        """Возвращает список сессий, отсортированных по дате последнего обновления."""
        conds = []
        if not include_deleted:
            conds.append("is_deleted = 0")
        if not include_archived:
            conds.append("is_archived = 0")

        where_clause = f"WHERE {' AND '.join(conds)}" if conds else ""
        query = f"""
            SELECT id, title, created_at, updated_at, is_archived, is_deleted,
                   turn_count, preview, summary, compacted_up_to_seq
            FROM sessions
            {where_clause}
            ORDER BY updated_at DESC
            LIMIT ?
        """

        with self._get_connection() as con:
            rows = con.execute(query, (limit,)).fetchall()
            return [
                SessionRecord(
                    id=r["id"],
                    title=r["title"],
                    created_at=r["created_at"],
                    updated_at=r["updated_at"],
                    is_archived=bool(r["is_archived"]),
                    is_deleted=bool(r["is_deleted"]),
                    turn_count=r["turn_count"],
                    preview=r["preview"] or "",
                    summary=r["summary"] or "",
                    compacted_up_to_seq=int(r["compacted_up_to_seq"] or 0),
                )
                for r in rows
            ]

    def search_sessions(
        self,
        query: str,
        limit: int = 60,
        include_archived: bool = False,
    ) -> List[SessionRecord]:
        """
        Гибридный полнотекстовый поиск сессий (SQLite FTS5 BM25 по всем раундам +
        Unicode casefold по заголовку, превью и сжатой сводке).
        """
        q = query.strip()
        if not q:
            return self.list_sessions(limit=limit, include_archived=include_archived)

        q_folded = q.casefold()
        fts_expr = _build_fts5_query(q)
        conds = ["is_deleted = 0"]
        if not include_archived:
            conds.append("is_archived = 0")

        scores: dict[str, float] = {}
        records: dict[str, SessionRecord] = {}

        with self._get_connection() as con:
            con.create_function("py_contains", 2, lambda text, needle: needle in (text or "").casefold())

            # 1. Direct Unicode casefold matches on title / preview / summary
            meta_sql = f"""
                SELECT id, title, created_at, updated_at, is_archived, is_deleted,
                       turn_count, preview, summary, compacted_up_to_seq
                FROM sessions
                WHERE {' AND '.join(conds)}
                  AND (py_contains(title, ?) OR py_contains(preview, ?) OR py_contains(summary, ?))
                ORDER BY updated_at DESC
                LIMIT ?
            """
            for r in con.execute(meta_sql, [q_folded, q_folded, q_folded, limit * 2]).fetchall():
                sid = r["id"]
                rec = SessionRecord(
                    id=sid,
                    title=r["title"],
                    created_at=r["created_at"],
                    updated_at=r["updated_at"],
                    is_archived=bool(r["is_archived"]),
                    is_deleted=bool(r["is_deleted"]),
                    turn_count=r["turn_count"],
                    preview=r["preview"] or "",
                    summary=r["summary"] or "",
                    compacted_up_to_seq=int(r["compacted_up_to_seq"] or 0),
                )
                records[sid] = rec
                sc = 0.0
                if q_folded in (rec.title or "").casefold():
                    sc += 6.0
                if q_folded in (rec.preview or "").casefold():
                    sc += 3.5
                if q_folded in (rec.summary or "").casefold():
                    sc += 3.0
                scores[sid] = max(scores.get(sid, 0.0), sc)

            # 2. Full-text FTS5 BM25 matches across ALL turn contents
            fts_session_ids: dict[str, float] = {}
            if fts_expr:
                try:
                    fts_rows = con.execute(
                        """
                        SELECT session_id, bm25(turns_fts) AS rank
                        FROM turns_fts
                        WHERE turns_fts MATCH ?
                        ORDER BY rank ASC
                        LIMIT ?
                        """,
                        (fts_expr, limit * 4),
                    ).fetchall()
                    for fr in fts_rows:
                        sid = fr["session_id"]
                        # SQLite bm25() returns negative scores where more negative = better match
                        rel = 4.0 + min(6.0, abs(float(fr["rank"] or 0.0)))
                        fts_session_ids[sid] = max(fts_session_ids.get(sid, 0.0), rel)
                except sqlite3.OperationalError:
                    pass

            # 3. Fallback Unicode casefold scan on turn texts (handles Cyrillic mid-word substrings)
            if not fts_session_ids:
                turn_rows = con.execute(
                    """
                    SELECT DISTINCT t.session_id
                    FROM turns t
                    JOIN sessions s ON s.id = t.session_id
                    WHERE s.is_deleted = 0
                      AND (py_contains(t.user_text, ?) OR py_contains(t.assistant_text, ?))
                    LIMIT ?
                    """,
                    (q_folded, q_folded, limit * 2),
                ).fetchall()
                for tr in turn_rows:
                    fts_session_ids[tr["session_id"]] = max(fts_session_ids.get(tr["session_id"], 0.0), 3.0)

            missing_sids = [sid for sid in fts_session_ids if sid not in records]
            if missing_sids:
                placeholders = ",".join("?" for _ in missing_sids)
                extra_conds = [f"id IN ({placeholders})", "is_deleted = 0"]
                if not include_archived:
                    extra_conds.append("is_archived = 0")
                extra_sql = f"""
                    SELECT id, title, created_at, updated_at, is_archived, is_deleted,
                           turn_count, preview, summary, compacted_up_to_seq
                    FROM sessions
                    WHERE {' AND '.join(extra_conds)}
                """
                for r in con.execute(extra_sql, missing_sids).fetchall():
                    sid = r["id"]
                    records[sid] = SessionRecord(
                        id=sid,
                        title=r["title"],
                        created_at=r["created_at"],
                        updated_at=r["updated_at"],
                        is_archived=bool(r["is_archived"]),
                        is_deleted=bool(r["is_deleted"]),
                        turn_count=r["turn_count"],
                        preview=r["preview"] or "",
                        summary=r["summary"] or "",
                        compacted_up_to_seq=int(r["compacted_up_to_seq"] or 0),
                    )

            for sid, rel in fts_session_ids.items():
                if sid in records:
                    scores[sid] = scores.get(sid, 0.0) + rel

        ranked = sorted(
            records.values(),
            key=lambda rec: (scores.get(rec.id, 0.0), rec.updated_at),
            reverse=True,
        )
        return ranked[:limit]

    def search_turns_hybrid(
        self,
        query: str,
        limit: int = 6,
        exclude_session_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Ассоциативный поиск релевантных прошлых раундов по всей базе (FTS5 + BM25 + fallback).
        Возвращает список словарей: {session_id, session_title, sequence, user_text, assistant_text, score}.
        """
        q = (query or "").strip()
        if not q:
            return []
        fts_expr = _build_fts5_query(q)
        q_folded = q.casefold()
        results: List[Dict[str, Any]] = []
        seen_ids: set[str] = set()

        with self._get_connection() as con:
            if fts_expr:
                try:
                    params: List[Any] = [fts_expr]
                    excl_sql = ""
                    if exclude_session_id:
                        excl_sql = "AND t.session_id != ?"
                        params.append(exclude_session_id)
                    params.append(limit)
                    rows = con.execute(
                        f"""
                        SELECT t.id, t.session_id, s.title AS session_title, t.sequence,
                               t.user_text, t.assistant_text, bm25(turns_fts) AS rank
                        FROM turns_fts
                        JOIN turns t ON t.id = turns_fts.turn_id
                        JOIN sessions s ON s.id = t.session_id
                        WHERE turns_fts MATCH ?
                          AND s.is_deleted = 0
                          {excl_sql}
                        ORDER BY rank ASC, t.timestamp DESC
                        LIMIT ?
                        """,
                        params,
                    ).fetchall()
                    for r in rows:
                        seen_ids.add(r["id"])
                        results.append(
                            {
                                "turn_id": r["id"],
                                "session_id": r["session_id"],
                                "session_title": r["session_title"],
                                "sequence": r["sequence"],
                                "user_text": r["user_text"],
                                "assistant_text": r["assistant_text"],
                                "score": abs(float(r["rank"] or 0.0)),
                            }
                        )
                except sqlite3.OperationalError:
                    pass

            if len(results) < limit:
                con.create_function("py_contains", 2, lambda text, needle: needle in (text or "").casefold())
                params2: List[Any] = [q_folded, q_folded]
                excl_sql2 = ""
                if exclude_session_id:
                    excl_sql2 = "AND t.session_id != ?"
                    params2.append(exclude_session_id)
                params2.append(limit)
                fb_rows = con.execute(
                    f"""
                    SELECT t.id, t.session_id, s.title AS session_title, t.sequence,
                           t.user_text, t.assistant_text
                    FROM turns t
                    JOIN sessions s ON s.id = t.session_id
                    WHERE s.is_deleted = 0
                      AND (py_contains(t.user_text, ?) OR py_contains(t.assistant_text, ?))
                      {excl_sql2}
                    ORDER BY t.timestamp DESC
                    LIMIT ?
                    """,
                    params2,
                ).fetchall()
                for r in fb_rows:
                    if r["id"] in seen_ids:
                        continue
                    seen_ids.add(r["id"])
                    results.append(
                        {
                            "turn_id": r["id"],
                            "session_id": r["session_id"],
                            "session_title": r["session_title"],
                            "sequence": r["sequence"],
                            "user_text": r["user_text"],
                            "assistant_text": r["assistant_text"],
                            "score": 1.0,
                        }
                    )
                    if len(results) >= limit:
                        break
        return results

    def get_session_summary(self, session_id: str) -> str:
        with self._get_connection() as con:
            row = con.execute("SELECT summary FROM sessions WHERE id = ?", (session_id,)).fetchone()
            return (row["summary"] or "").strip() if row else ""

    def compact_session_if_needed(
        self,
        session_id: str,
        threshold: int = COMPACT_THRESHOLD_TURNS,
        keep_recent: int = COMPACT_KEEP_RECENT_TURNS,
    ) -> str:
        """
        Автоматическое скользящее сжатие (rolling auto-compression) старых раундов длинной сессии.
        Хранит последние `keep_recent` раундов дословно, а более старые сворачивает в компактную
        структурированную сводку `sessions.summary` (без лишних вызовов LLM и с лимитом символов).
        """
        with self._lock, self._get_connection() as con:
            s_row = con.execute(
                "SELECT turn_count, summary, compacted_up_to_seq FROM sessions WHERE id = ? AND is_deleted = 0",
                (session_id,),
            ).fetchone()
            if not s_row:
                return ""
            total_turns = int(s_row["turn_count"] or 0)
            existing_summary = (s_row["summary"] or "").strip()
            compacted_seq = int(s_row["compacted_up_to_seq"] or 0)
            if total_turns < threshold:
                return existing_summary

            seq_rows = con.execute(
                """
                SELECT sequence, user_text, assistant_text, status
                FROM turns
                WHERE session_id = ?
                ORDER BY sequence ASC
                """,
                (session_id,),
            ).fetchall()
            if len(seq_rows) <= keep_recent:
                return existing_summary

            cutoff_idx = len(seq_rows) - keep_recent
            to_fold = [r for r in seq_rows[:cutoff_idx] if int(r["sequence"]) > compacted_seq]
            if not to_fold:
                return existing_summary

            new_lines: List[str] = []
            if existing_summary:
                new_lines.extend(ln for ln in existing_summary.splitlines() if ln.strip())

            new_max_seq = compacted_seq
            for r in to_fold:
                seq_num = int(r["sequence"])
                new_max_seq = max(new_max_seq, seq_num)
                if (r["status"] or "") == "proactive" or (r["user_text"] or "").strip().startswith("[INITIATIVE //"):
                    continue
                u_snip = re.sub(r"\s+", " ", (r["user_text"] or "").strip())[:95]
                a_snip = re.sub(r"\s+", " ", (r["assistant_text"] or "").strip())[:110]
                if not u_snip and not a_snip:
                    continue
                bullet = f"• [#{seq_num}] Запрос: {u_snip}"
                if a_snip:
                    bullet += f" → Итог: {a_snip}"
                new_lines.append(bullet)

            # Enforce strict character budget keeping most recent compacted entries + oldest anchor
            summary_text = "\n".join(new_lines)
            if len(summary_text) > MAX_SESSION_SUMMARY_CHARS and len(new_lines) > 2:
                anchor = new_lines[0]
                tail_lines: List[str] = []
                budget = MAX_SESSION_SUMMARY_CHARS - len(anchor) - 24
                cur = 0
                for ln in reversed(new_lines[1:]):
                    if cur + len(ln) + 1 > budget:
                        break
                    tail_lines.append(ln)
                    cur += len(ln) + 1
                tail_lines.reverse()
                summary_text = "\n".join([anchor, "… (ранние детали сжаты) …"] + tail_lines)

            con.execute("BEGIN IMMEDIATE;")
            con.execute(
                "UPDATE sessions SET summary = ?, compacted_up_to_seq = ? WHERE id = ?",
                (summary_text, new_max_seq, session_id),
            )
            con.commit()
            return summary_text

    def add_turn(
        self,
        session_id: str,
        user_text: str,
        assistant_text: str,
        thought: str = "",
        tools: Optional[List[Dict[str, Any]]] = None,
        model_messages: Optional[List[Dict[str, Any]]] = None,
        status: str = "completed",
    ) -> TurnRecord:
        """Атомарно добавляет раунд общения в сессию, обновляет счетчик и автозаголовок."""
        now = time.time()
        t_id = str(uuid.uuid4())
        tools_data = tools or []
        model_msgs = model_messages or []

        with self._lock, self._get_connection() as con:
            con.execute("BEGIN IMMEDIATE;")
            # Получаем следующий порядковый номер раунда
            seq_row = con.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM turns WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            seq = seq_row[0]

            con.execute(
                """
                INSERT INTO turns (
                    id, session_id, sequence, timestamp, user_text, assistant_text,
                    thought, tools_json, model_messages_json, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    t_id,
                    session_id,
                    seq,
                    now,
                    user_text,
                    assistant_text,
                    thought,
                    json.dumps(tools_data, ensure_ascii=False),
                    json.dumps(model_msgs, ensure_ascii=False),
                    status,
                ),
            )

            con.execute(
                """
                INSERT INTO turns_fts (turn_id, session_id, user_text, assistant_text)
                VALUES (?, ?, ?, ?)
                """,
                (t_id, session_id, user_text, assistant_text),
            )

            # Генерация заголовка, если это первый раунд или заголовок дефолтный
            sess_row = con.execute("SELECT title FROM sessions WHERE id = ?", (session_id,)).fetchone()
            curr_title = sess_row["title"] if sess_row else "Новый диалог"
            is_proactive_turn = (status == "proactive") or user_text.strip().startswith("[INITIATIVE //")
            if curr_title in ("Новый диалог", "", "Сессия") and user_text.strip() and not is_proactive_turn:
                # Обрезаем первый запрос D под красивый заголовок карточки
                clean_q = user_text.strip().replace("\n", " ")
                new_title = clean_q[:42] + ("…" if len(clean_q) > 42 else "")
            else:
                new_title = curr_title

            preview_snippet = (assistant_text or user_text).strip().replace("\n", " ")[:80]

            con.execute(
                """
                UPDATE sessions
                SET updated_at = ?, turn_count = turn_count + 1, title = ?, preview = ?
                WHERE id = ?
                """,
                (now, new_title, preview_snippet, session_id),
            )
            con.commit()

        return TurnRecord(
            id=t_id,
            session_id=session_id,
            sequence=seq,
            timestamp=now,
            user_text=user_text,
            assistant_text=assistant_text,
            thought=thought,
            tools=tools_data,
            model_messages=model_msgs,
            status=status,
        )

    def get_turns(
        self,
        session_id: str,
        limit: int = 100,
        after_sequence: Optional[int] = None,
    ) -> List[TurnRecord]:
        """Ключевая пагинация раундов диалога (keyset pagination) по номеру sequence."""
        params: List[Any] = [session_id]
        where_extra = ""
        if after_sequence is not None:
            where_extra = "AND sequence > ?"
            params.append(after_sequence)
        params.append(limit)

        query = f"""
            SELECT id, session_id, sequence, timestamp, user_text, assistant_text,
                   thought, tools_json, model_messages_json, status
            FROM turns
            WHERE session_id = ? {where_extra}
            ORDER BY sequence ASC
            LIMIT ?
        """

        with self._get_connection() as con:
            rows = con.execute(query, params).fetchall()
            turns = []
            for r in rows:
                tools = []
                msgs = []
                try:
                    tools = json.loads(r["tools_json"])
                except Exception:
                    pass
                try:
                    msgs = json.loads(r["model_messages_json"])
                except Exception:
                    pass

                turns.append(
                    TurnRecord(
                        id=r["id"],
                        session_id=r["session_id"],
                        sequence=r["sequence"],
                        timestamp=r["timestamp"],
                        user_text=r["user_text"],
                        assistant_text=r["assistant_text"],
                        thought=r["thought"],
                        tools=tools,
                        model_messages=msgs,
                        status=r["status"],
                    )
                )
            return turns

    def get_recent_turns(self, session_id: str, limit: int = 100) -> List[TurnRecord]:
        """Возвращает N последних раундов диалога в хронологическом порядке (sequence ASC)."""
        query = """
            SELECT id, session_id, sequence, timestamp, user_text, assistant_text,
                   thought, tools_json, model_messages_json, status
            FROM (
                SELECT * FROM turns
                WHERE session_id = ?
                ORDER BY sequence DESC
                LIMIT ?
            )
            ORDER BY sequence ASC
        """
        with self._get_connection() as con:
            rows = con.execute(query, (session_id, limit)).fetchall()
            turns = []
            for r in rows:
                tools = []
                msgs = []
                try:
                    tools = json.loads(r["tools_json"])
                except Exception:
                    pass
                try:
                    msgs = json.loads(r["model_messages_json"])
                except Exception:
                    pass

                turns.append(
                    TurnRecord(
                        id=r["id"],
                        session_id=r["session_id"],
                        sequence=r["sequence"],
                        timestamp=r["timestamp"],
                        user_text=r["user_text"],
                        assistant_text=r["assistant_text"],
                        thought=r["thought"],
                        tools=tools,
                        model_messages=msgs,
                        status=r["status"],
                    )
                )
            return turns

    def get_model_history(self, session_id: str, limit: int = 500) -> List[Dict[str, Any]]:
        """
        Восстанавливает чистый массив сообщений для LLM (`agent.history`), исключая системный промпт.
        Загружает последние limit раундов диалога в хронологическом порядке.
        """
        turns = self.get_recent_turns(session_id, limit=limit)
        history: List[Dict[str, Any]] = []
        for t in turns:
            if t.model_messages:
                history.extend(t.model_messages)
            else:
                # Фоллбэк: если model_messages_json был пуст
                is_pro = getattr(t, "status", "") == "proactive" or (t.user_text or "").strip().startswith("[INITIATIVE //")
                u_content = "(Тишина в эфире — проактивное обращение Капри к пользователю)" if is_pro else t.user_text
                history.append({"role": "user", "content": u_content})
                if t.assistant_text:
                    history.append({"role": "assistant", "content": t.assistant_text})

        # Санитизация для моделей с Chain-of-Thought (DeepSeek Reasoning API):
        # Если в истории есть хотя бы одно рассуждение, ни одно assistant-сообщение не должно быть пустым
        has_any_reasoning = any(
            m.get("role") == "assistant" and bool(str(m.get("reasoning_content", "")).strip())
            for m in history
        )
        if has_any_reasoning:
            for m in history:
                if m.get("role") == "assistant":
                    rc = str(m.get("reasoning_content", "")).strip()
                    if not rc:
                        if "tool_calls" in m and m["tool_calls"]:
                            tool_names = ", ".join(tc.get("function", {}).get("name", "tool") for tc in m["tool_calls"])
                            m["reasoning_content"] = f"Запуск системных инструментов: {tool_names}."
                        else:
                            m["reasoning_content"] = "Анализ контекста диалога и формулирование ответа пользователю."

        return history

    def get_turn(self, turn_id: str) -> Optional[TurnRecord]:
        """Возвращает раунд диалога по его ID."""
        with self._get_connection() as con:
            r = con.execute(
                """
                SELECT id, session_id, sequence, timestamp, user_text, assistant_text,
                       thought, tools_json, model_messages_json, status
                FROM turns WHERE id = ?
                """,
                (turn_id,),
            ).fetchone()
            if not r:
                return None
            tools, msgs = [], []
            try:
                tools = json.loads(r["tools_json"])
            except Exception:
                pass
            try:
                msgs = json.loads(r["model_messages_json"])
            except Exception:
                pass
            return TurnRecord(
                id=r["id"],
                session_id=r["session_id"],
                sequence=r["sequence"],
                timestamp=r["timestamp"],
                user_text=r["user_text"],
                assistant_text=r["assistant_text"],
                thought=r["thought"],
                tools=tools,
                model_messages=msgs,
                status=r["status"],
            )

    def rollback_session(self, session_id: str, from_sequence: int) -> int:
        """
        Удаляет все раунды с sequence >= from_sequence в указанной сессии (откат ветки диалога).
        Атомарно пересчитывает turn_count, обновляет preview и updated_at.
        Возвращает количество удаленных раундов.
        """
        with self._lock, self._get_connection() as con:
            con.execute("BEGIN IMMEDIATE;")
            del_ids = [
                r["id"]
                for r in con.execute(
                    "SELECT id FROM turns WHERE session_id = ? AND sequence >= ?",
                    (session_id, from_sequence),
                ).fetchall()
            ]
            cur = con.execute(
                "DELETE FROM turns WHERE session_id = ? AND sequence >= ?",
                (session_id, from_sequence),
            )
            deleted_count = cur.rowcount
            for tid in del_ids:
                con.execute("DELETE FROM turns_fts WHERE turn_id = ?", (tid,))

            # Пересчитываем актуальный turn_count
            count_row = con.execute(
                "SELECT COUNT(*) as cnt FROM turns WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            new_count = count_row["cnt"] if count_row else 0

            # Находим новый последний раунд для preview
            last_turn = con.execute(
                "SELECT user_text, assistant_text FROM turns WHERE session_id = ? ORDER BY sequence DESC LIMIT 1",
                (session_id,),
            ).fetchone()

            if last_turn:
                new_preview = (last_turn["assistant_text"] or last_turn["user_text"]).strip().replace("\n", " ")[:80]
            else:
                new_preview = ""

            # Если откат затронул уже сжатые раунды — сбрасываем устаревшую сводку
            s_comp = con.execute(
                "SELECT compacted_up_to_seq FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            comp_seq = int(s_comp["compacted_up_to_seq"] or 0) if s_comp else 0
            now = time.time()
            if from_sequence <= comp_seq:
                con.execute(
                    "UPDATE sessions SET turn_count = ?, preview = ?, updated_at = ?, summary = '', compacted_up_to_seq = 0 WHERE id = ?",
                    (new_count, new_preview, now, session_id),
                )
            else:
                con.execute(
                    "UPDATE sessions SET turn_count = ?, preview = ?, updated_at = ? WHERE id = ?",
                    (new_count, new_preview, now, session_id),
                )
            con.commit()
            return deleted_count

    def delete_turn(self, turn_id: str) -> bool:
        """Удаляет конкретный раунд диалога по ID и обновляет счетчик сессии."""
        turn = self.get_turn(turn_id)
        if not turn:
            return False
        with self._lock, self._get_connection() as con:
            con.execute("BEGIN IMMEDIATE;")
            cur = con.execute("DELETE FROM turns WHERE id = ?", (turn_id,))
            if cur.rowcount == 0:
                con.rollback()
                return False
            con.execute("DELETE FROM turns_fts WHERE turn_id = ?", (turn_id,))

            count_row = con.execute(
                "SELECT COUNT(*) as cnt FROM turns WHERE session_id = ?",
                (turn.session_id,),
            ).fetchone()
            new_count = count_row["cnt"] if count_row else 0

            last_turn = con.execute(
                "SELECT user_text, assistant_text FROM turns WHERE session_id = ? ORDER BY sequence DESC LIMIT 1",
                (turn.session_id,),
            ).fetchone()
            new_preview = (
                (last_turn["assistant_text"] or last_turn["user_text"]).strip().replace("\n", " ")[:80]
                if last_turn
                else ""
            )

            now = time.time()
            con.execute(
                "UPDATE sessions SET turn_count = ?, preview = ?, updated_at = ? WHERE id = ?",
                (new_count, new_preview, now, turn.session_id),
            )
            con.commit()
            return True

    def rename_session(self, session_id: str, new_title: str) -> bool:
        title_clean = new_title.strip()
        if not title_clean:
            return False
        with self._lock, self._get_connection() as con:
            con.execute("BEGIN IMMEDIATE;")
            cur = con.execute("UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?", (title_clean, time.time(), session_id))
            con.commit()
            return cur.rowcount > 0

    def soft_delete_session(self, session_id: str) -> bool:
        """Мягкое удаление (помечает сессию как удаленную без физического уничтожения)."""
        with self._lock, self._get_connection() as con:
            con.execute("BEGIN IMMEDIATE;")
            cur = con.execute("UPDATE sessions SET is_deleted = 1, updated_at = ? WHERE id = ?", (time.time(), session_id))
            # Если удалена текущая активная сессия, сбрасываем active_session_id
            con.execute("DELETE FROM meta WHERE key = 'active_session_id' AND value = ?", (session_id,))
            con.commit()
            return cur.rowcount > 0

    def restore_session(self, session_id: str) -> bool:
        """Восстановление сессии из корзины."""
        with self._lock, self._get_connection() as con:
            con.execute("BEGIN IMMEDIATE;")
            cur = con.execute("UPDATE sessions SET is_deleted = 0, updated_at = ? WHERE id = ?", (time.time(), session_id))
            con.commit()
            return cur.rowcount > 0

    def purge_session(self, session_id: str) -> bool:
        """Полное физическое удаление сессии и всех ее раундов (ON DELETE CASCADE)."""
        with self._lock, self._get_connection() as con:
            con.execute("BEGIN IMMEDIATE;")
            con.execute("DELETE FROM turns_fts WHERE session_id = ?", (session_id,))
            cur = con.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            con.execute("DELETE FROM meta WHERE key = 'active_session_id' AND value = ?", (session_id,))
            con.commit()
            return cur.rowcount > 0

    def backup_to(self, target_path: Path) -> bool:
        """
        Создает атомарную резервную копию базы данных через официальный SQLite Online Backup API.
        Гарантирует отсутствие повреждений даже во время активной записи.
        """
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._get_connection() as src_con:
            with sqlite3.connect(str(target_path)) as dst_con:
                src_con.backup(dst_con)
        return target_path.exists() and target_path.stat().st_size > 0

    def verify_integrity(self) -> bool:
        """Проверяет целостность базы через PRAGMA integrity_check."""
        try:
            with self._get_connection() as con:
                row = con.execute("PRAGMA integrity_check;").fetchone()
                return bool(row and row[0] == "ok")
        except Exception as e:
            logger.error(f"Integrity check failed: {e}")
            return False
