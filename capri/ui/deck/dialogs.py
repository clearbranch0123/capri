"""Modal dialogs and workbenches extracted from Cyberdeck."""
from __future__ import annotations

import html
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QSize, Qt, QThread, Signal
from PySide6.QtGui import QColor, QCursor, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QTextEdit,
    QVBoxLayout,
)

from ... import __version__
from ...core.config import (
    PROVIDER_DEFAULTS,
    AppConfig,
    get_user_config_dir,
    save_config as _save_config,
    update_env_value as _update_env_value,
)
from ...core.memory import (
    CATEGORIES,
    CATEGORY_LABELS,
    RETENTION_MODES,
    MemoryCandidate,
    MemoryEntry,
    MemoryStore,
    memory_payload_contains_secret,
)
from ...core.resources import get_assets_dir
from ...core.sessions import SessionRecord
from ..i18n import resolve_ui_lang
from ..icons import get_icon
from ..theme import (
    COLORS,
    TYPE,
    apply_dark_palette,
    button_danger_qss,
    button_ghost_qss,
    button_primary_qss,
    button_secondary_qss,
    dialog_base_qss,
    markdown_document_css,
    scrollbar_qss,
    table_qss,
)
from . import cyberdeck_hook
from .chat_card import _format_rounds_ru, play_ui_sound

ASSETS_DIR = get_assets_dir()


def _make_dialog_header(title: str, subtitle: str, on_close) -> QHBoxLayout:
    """Shared calm dialog header layout across all secondary dialogs."""
    header = QHBoxLayout()
    header.setSpacing(12)

    title_box = QVBoxLayout()
    title_box.setSpacing(2)

    lbl_sub = QLabel(subtitle)
    lbl_sub.setStyleSheet(
        f"color: {COLORS.signal_cyan}; font-family: {TYPE.mono}; font-size: 10.5px; font-weight: 600; letter-spacing: 0.8px;"
    )
    title_box.addWidget(lbl_sub)

    lbl_title = QLabel(title)
    lbl_title.setStyleSheet(
        f"color: {COLORS.text_primary}; font-family: {TYPE.display}; font-size: 16px; font-weight: 600;"
    )
    title_box.addWidget(lbl_title)

    header.addLayout(title_box)
    header.addStretch()

    btn_close = QPushButton()
    btn_close.setIcon(get_icon("close", COLORS.text_muted, 15))
    btn_close.setIconSize(QSize(15, 15))
    btn_close.setFixedSize(30, 30)
    btn_close.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
    btn_close.setToolTip("Закрыть")
    btn_close.setStyleSheet(
        button_ghost_qss(
            text_color=COLORS.text_muted,
            hover_text=COLORS.coral,
            hover_bg=COLORS.coral_soft,
            padding="0px",
            radius=8,
        )
    )
    btn_close.clicked.connect(on_close)
    header.addWidget(btn_close)
    return header




class CapriFirstRunWizard(QDialog):
    """Minimal Closed Beta onboarding wizard (LIVING SIGNAL): name, provider/API key (.env), tool policy, avatar."""

    def __init__(self, config: AppConfig, parent=None):
        super().__init__(parent)
        self.config = config
        self.setWindowTitle("Быстрая настройка — Capri")
        self.setFixedSize(560, 506)
        self.setModal(True)
        self.setStyleSheet(dialog_base_qss())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(12)

        layout.addLayout(
            _make_dialog_header(
                "Добро пожаловать в Capri",
                "CAPRI // FIRST-RUN SETUP",
                self.reject,
            )
        )

        lbl_intro = QLabel("Настрой имя, язык интерфейса, провайдера нейросети и политику безопасности. API-ключ сохраняется только в .env.")
        lbl_intro.setWordWrap(True)
        lbl_intro.setStyleSheet(f"color: {COLORS.text_secondary}; font-family: {TYPE.sans}; font-size: 12.5px;")
        layout.addWidget(lbl_intro)

        # User name + UI Language side by side
        top_grid = QGridLayout()
        top_grid.setSpacing(10)
        lbl_name = QLabel("Как к тебе обращаться?")
        lbl_name.setStyleSheet(f"color: {COLORS.text_secondary}; font-family: {TYPE.sans}; font-size: 12px; font-weight: 600;")
        lbl_lang = QLabel("Язык / UI Language")
        lbl_lang.setStyleSheet(f"color: {COLORS.text_secondary}; font-family: {TYPE.sans}; font-size: 12px; font-weight: 600;")
        top_grid.addWidget(lbl_name, 0, 0)
        top_grid.addWidget(lbl_lang, 0, 1)

        self.edit_user_name = QLineEdit(getattr(self.config.companion, "user_name", "User") or "User")
        self.edit_user_name.setMinimumHeight(36)
        self.edit_user_name.setPlaceholderText("Например: D, Alex, Kai")
        top_grid.addWidget(self.edit_user_name, 1, 0)

        self.combo_ui_lang = QComboBox()
        self.combo_ui_lang.setMinimumHeight(36)
        self.combo_ui_lang.addItem("Русский (RU)", "ru")
        self.combo_ui_lang.addItem("English (EN)", "en")
        cur_lang = resolve_ui_lang(self.config)
        lang_idx = self.combo_ui_lang.findData(cur_lang)
        if lang_idx >= 0:
            self.combo_ui_lang.setCurrentIndex(lang_idx)
        top_grid.addWidget(self.combo_ui_lang, 1, 1)
        top_grid.setColumnStretch(0, 3)
        top_grid.setColumnStretch(1, 2)
        layout.addLayout(top_grid)

        # Provider
        lbl_prov = QLabel("Провайдер нейросети")
        lbl_prov.setStyleSheet(f"color: {COLORS.text_secondary}; font-family: {TYPE.sans}; font-size: 12px; font-weight: 600;")
        layout.addWidget(lbl_prov)
        self.combo_provider = QComboBox()
        self.combo_provider.setMinimumHeight(36)
        self._provider_ids = ["deepseek", "groq", "gemini", "sambanova", "openrouter", "openai", "ollama", "lmstudio"]
        labels = {
            "deepseek": "DeepSeek (Официальный API)",
            "groq": "Groq (Быстрый inference)",
            "gemini": "Google Gemini",
            "sambanova": "SambaNova Cloud",
            "openrouter": "OpenRouter",
            "openai": "OpenAI",
            "ollama": "Ollama (Локальный — без API-ключа)",
            "lmstudio": "LM Studio (Локальный — без API-ключа)",
        }
        cur_p = (self.config.llm.provider or "deepseek").lower()
        for pid in self._provider_ids:
            self.combo_provider.addItem(labels[pid], pid)
        if cur_p in self._provider_ids:
            self.combo_provider.setCurrentIndex(self._provider_ids.index(cur_p))
        layout.addWidget(self.combo_provider)

        # API key
        lbl_key = QLabel("API-ключ провайдера (сохраняется в .env, не в YAML)")
        lbl_key.setStyleSheet(f"color: {COLORS.text_secondary}; font-family: {TYPE.sans}; font-size: 12px; font-weight: 600;")
        layout.addWidget(lbl_key)
        self.edit_api_key = QLineEdit(self.config.llm.api_key or "")
        self.edit_api_key.setMinimumHeight(36)
        self.edit_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.edit_api_key.setPlaceholderText("Вставь API-ключ...")
        layout.addWidget(self.edit_api_key)

        # Tool policy
        lbl_pol = QLabel("Политика безопасности инструментов")
        lbl_pol.setStyleSheet(f"color: {COLORS.text_secondary}; font-family: {TYPE.sans}; font-size: 12px; font-weight: 600;")
        layout.addWidget(lbl_pol)
        self.combo_policy = QComboBox()
        self.combo_policy.setMinimumHeight(36)
        self.combo_policy.addItem("Ask — подтверждать изменяющие действия (рекомендуется)", "ask")
        self.combo_policy.addItem("Safe — строго только чтение (без изменений)", "safe")
        self.combo_policy.addItem("Trusted — десктоп авто, shell/файлы/kill с подтверждением", "trusted")
        cur_pol = getattr(getattr(self.config, "safety", None), "tool_policy", "ask")
        for idx in range(self.combo_policy.count()):
            if self.combo_policy.itemData(idx) == cur_pol:
                self.combo_policy.setCurrentIndex(idx)
                break
        layout.addWidget(self.combo_policy)

        # Avatar toggle
        self.chk_avatar = QCheckBox("Включить плавающий desktop-аватар (при наличии композитора)")
        self.chk_avatar.setChecked(bool(getattr(self.config.ui, "avatar_enabled", True)))
        self.chk_avatar.setStyleSheet(f"color: {COLORS.text_secondary}; font-family: {TYPE.sans}; font-size: 12px;")
        layout.addWidget(self.chk_avatar)

        layout.addStretch()

        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        btn_row.addStretch()

        self.btn_skip = QPushButton("Позже")
        self.btn_skip.setMinimumHeight(36)
        self.btn_skip.setStyleSheet(button_secondary_qss("8px 16px"))
        self.btn_skip.clicked.connect(self.reject)
        btn_row.addWidget(self.btn_skip)

        self.btn_save = QPushButton("Сохранить и продолжить")
        self.btn_save.setIcon(get_icon("check", COLORS.text_on_accent, 14))
        self.btn_save.setMinimumHeight(36)
        self.btn_save.setStyleSheet(button_primary_qss("8px 16px"))
        self.btn_save.clicked.connect(self._apply)
        btn_row.addWidget(self.btn_save)
        layout.addLayout(btn_row)
        apply_dark_palette(self)

    def _apply(self):
        name = self.edit_user_name.text().strip() or "User"
        pid = self.combo_provider.currentData() or "deepseek"
        api_key = self.edit_api_key.text().strip()
        policy = self.combo_policy.currentData() or "ask"

        self.config.companion.user_name = name
        self.config.llm.provider = pid
        if pid in PROVIDER_DEFAULTS:
            self.config.llm.base_url = PROVIDER_DEFAULTS[pid]["base_url"]
            self.config.llm.model = PROVIDER_DEFAULTS[pid]["default_model"]
            cyberdeck_hook("update_env_value", _update_env_value)("LLM_PROVIDER", pid)
            cyberdeck_hook("update_env_value", _update_env_value)("LLM_MODEL", self.config.llm.model)
            if api_key:
                self.config.llm.api_key = api_key
                cyberdeck_hook("update_env_value", _update_env_value)(PROVIDER_DEFAULTS[pid]["env_key"], api_key)
        self.config.safety.tool_policy = policy
        self.config.ui.language = str(self.combo_ui_lang.currentData() or "ru")
        self.config.ui.avatar_enabled = bool(self.chk_avatar.isChecked())
        cyberdeck_hook("save_config", _save_config)(self.config)
        self.accept()


class CapriToolApprovalDialog(QDialog):
    """Explicit confirmation dialog for mutating/high-impact tool calls (LIVING SIGNAL)."""

    def __init__(self, request, parent=None):
        super().__init__(parent)
        self.request = request
        self.setWindowTitle("Подтверждение действия — Capri")
        self.setMinimumWidth(520)
        self.setModal(True)
        self.setStyleSheet(dialog_base_qss())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        is_destructive = getattr(request, "risk", "mutating") in ("destructive", "forbidden")
        tag_color = COLORS.coral if is_destructive else COLORS.amber
        risk_label = "ВЫСОКИЙ РИСК" if is_destructive else "ИЗМЕНЯЮЩЕЕ ДЕЙСТВИЕ"

        layout.addLayout(
            _make_dialog_header(
                f"Разрешить инструмент «{request.tool_name}»?",
                f"CAPRI // TOOL POLICY · {risk_label}",
                self.reject,
            )
        )

        lbl_reason = QLabel(getattr(request, "reason", "Действие изменяет состояние системы."))
        lbl_reason.setWordWrap(True)
        lbl_reason.setStyleSheet(
            f"color: {COLORS.text_secondary}; font-family: {TYPE.sans}; font-size: 13px;"
        )
        layout.addWidget(lbl_reason)

        details_frame = QFrame(self)
        details_frame.setStyleSheet(f"""
            QFrame {{
                background: {COLORS.surface_code};
                border: 1px solid {tag_color};
                border-radius: 9px;
            }}
        """)
        df_layout = QVBoxLayout(details_frame)
        df_layout.setContentsMargins(12, 10, 12, 10)
        df_layout.setSpacing(4)

        lbl_summary = QLabel(getattr(request, "summary", request.tool_name))
        lbl_summary.setWordWrap(True)
        lbl_summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lbl_summary.setStyleSheet(
            f"color: {COLORS.text_primary}; font-family: {TYPE.mono}; font-size: 12px; border: none;"
        )
        df_layout.addWidget(lbl_summary)
        layout.addWidget(details_frame)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        btn_row.addStretch()

        self.btn_deny = QPushButton("Отклонить")
        self.btn_deny.setMinimumHeight(36)
        self.btn_deny.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_deny.setStyleSheet(button_secondary_qss("8px 16px"))
        self.btn_deny.clicked.connect(self.reject)
        btn_row.addWidget(self.btn_deny)

        self.btn_allow = QPushButton("Разрешить один раз")
        self.btn_allow.setIcon(get_icon("check", "#16070D" if is_destructive else COLORS.text_on_accent, 14))
        self.btn_allow.setMinimumHeight(36)
        self.btn_allow.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_allow.setStyleSheet(
            button_danger_qss(filled=True, padding="8px 16px")
            if is_destructive
            else button_primary_qss("8px 16px")
        )
        self.btn_allow.clicked.connect(self.accept)
        btn_row.addWidget(self.btn_allow)

        layout.addLayout(btn_row)


class CapriResetDialog(QDialog):
    """Engram reset confirmation dialog styled in LIVING SIGNAL."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Сброс энграммы — Capri")
        self.setFixedSize(560, 274)
        self.setModal(True)
        self.setStyleSheet(dialog_base_qss())

        layout = QHBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(20)

        face_frame = QFrame()
        face_frame.setFixedSize(156, 226)
        face_frame.setStyleSheet(f"""
            QFrame {{
                background: {COLORS.canvas};
                border: 1px solid {COLORS.border_subtle};
                border-radius: 10px;
            }}
        """)
        ff_layout = QVBoxLayout(face_frame)
        ff_layout.setContentsMargins(6, 6, 6, 8)
        ff_layout.setSpacing(6)

        lbl_face = QLabel()
        lbl_face.setAlignment(Qt.AlignmentFlag.AlignCenter)
        face_path = ASSETS_DIR / "capri_face_dialog.png"
        if not face_path.exists():
            face_path = ASSETS_DIR / "avatar.png"

        if face_path.exists():
            pix = QPixmap(str(face_path)).scaled(
                144,
                182,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            lbl_face.setPixmap(pix)
        ff_layout.addWidget(lbl_face)

        lbl_id = QLabel("CONSTRUCT // CAPRI")
        lbl_id.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl_id.setStyleSheet(
            f"color: {COLORS.coral}; font-family: {TYPE.mono}; font-size: 9px; font-weight: 600; letter-spacing: 0.6px; border: none;"
        )
        ff_layout.addWidget(lbl_id)
        layout.addWidget(face_frame)

        right_layout = QVBoxLayout()
        right_layout.setSpacing(10)

        lbl_sys_tag = QLabel("ПРОТОКОЛ ПЕРЕЗАГРУЗКИ RELIC 2.0")
        lbl_sys_tag.setStyleSheet(
            f"color: {COLORS.coral}; font-family: {TYPE.mono}; font-size: 10.5px; font-weight: 600; letter-spacing: 0.8px;"
        )
        right_layout.addWidget(lbl_sys_tag)

        lbl_title = QLabel("Сбросить энграмму?")
        lbl_title.setStyleSheet(
            f"color: {COLORS.text_primary}; font-family: {TYPE.display}; font-size: 19px; font-weight: 600;"
        )
        right_layout.addWidget(lbl_title)

        user_name = getattr(getattr(getattr(parent, "config", None), "companion", None), "user_name", "User") or "User"
        lbl_quote = QLabel(f"«Думаешь, я так просто забуду все твои грешки, {user_name}?»")
        lbl_quote.setWordWrap(True)
        lbl_quote.setStyleSheet(
            f"color: {COLORS.signal_cyan}; font-family: {TYPE.sans}; font-style: italic; font-size: 13px;"
        )
        right_layout.addWidget(lbl_quote)

        lbl_desc = QLabel(
            "Сброс вернёт уровень доверия к базовым +25, обнулит раздражение (0%) "
            "и очистит историю эмоциональных событий и конфликтов."
        )
        lbl_desc.setWordWrap(True)
        lbl_desc.setStyleSheet(
            f"color: {COLORS.text_secondary}; font-family: {TYPE.sans}; font-size: 12px; line-height: 1.4;"
        )
        right_layout.addWidget(lbl_desc)
        right_layout.addStretch()

        btn_box = QHBoxLayout()
        btn_box.setSpacing(10)
        btn_box.addStretch()

        btn_cancel = QPushButton("Пусть живёт")
        btn_cancel.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        btn_cancel.setMinimumHeight(36)
        btn_cancel.setStyleSheet(button_secondary_qss("8px 16px"))
        btn_cancel.clicked.connect(self.reject)
        btn_box.addWidget(btn_cancel)

        btn_confirm = QPushButton("Сбросить энграмму")
        btn_confirm.setIcon(get_icon("reset", "#16070D", 14))
        btn_confirm.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        btn_confirm.setMinimumHeight(36)
        btn_confirm.setStyleSheet(button_danger_qss(filled=True, padding="8px 16px"))
        btn_confirm.clicked.connect(self.accept)
        btn_box.addWidget(btn_confirm)

        right_layout.addLayout(btn_box)
        layout.addLayout(right_layout)


class CapriChangelogDialog(QDialog):
    """Release notes dialog styled in LIVING SIGNAL."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Capri — Хроника обновлений v{__version__}")
        self.resize(740, 540)
        self.setMinimumSize(520, 380)
        self.setModal(True)
        self.setStyleSheet(dialog_base_qss())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(14)

        layout.addLayout(
            _make_dialog_header(
                f"История обновлений (v{__version__})",
                "CAPRI // SYSTEM UPDATE CHRONICLES",
                self.close,
            )
        )

        self.browser = QTextBrowser()
        self.browser.setOpenExternalLinks(True)
        self.browser.document().setDefaultStyleSheet(markdown_document_css())
        self.browser.setStyleSheet(f"""
            QTextBrowser {{
                background: {COLORS.canvas};
                color: {COLORS.text_primary};
                border: 1px solid {COLORS.border_subtle};
                border-radius: 10px;
                padding: 14px;
                font-family: {TYPE.sans};
                font-size: 13px;
            }}
            {scrollbar_qss(6)}
        """)

        candidate_paths = [
            Path(__file__).resolve().parent.parent.parent / "CHANGELOG.md",
            Path(__file__).resolve().parent.parent / "CHANGELOG.md",
            Path(sys.prefix) / "share" / "capri" / "CHANGELOG.md",
        ]
        content = ""
        for p in candidate_paths:
            if p.exists():
                try:
                    loaded = p.read_text(encoding="utf-8")
                    if loaded.strip():
                        content = loaded
                        break
                except Exception:
                    pass

        if not content:
            try:
                import importlib.resources as pkg_resources
                content = pkg_resources.files("capri").joinpath("CHANGELOG.md").read_text(encoding="utf-8")
            except Exception:
                pass

        if content:
            self.browser.setMarkdown(content)
        else:
            self.browser.setText("Хроника обновлений (CHANGELOG.md) не найдена.")

        layout.addWidget(self.browser, stretch=1)

        bottom_layout = QHBoxLayout()
        bottom_layout.addStretch()

        btn_done = QPushButton("Закрыть")
        btn_done.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        btn_done.setMinimumHeight(36)
        btn_done.setStyleSheet(button_secondary_qss("8px 20px"))
        btn_done.clicked.connect(self.accept)
        bottom_layout.addWidget(btn_done)
        layout.addLayout(bottom_layout)


class CapriSessionsDialog(QDialog):
    """Sessions archive, preview, and backup manager styled in LIVING SIGNAL."""

    def __init__(self, parent: "CapriCyberdeckWindow"):
        super().__init__(parent)
        self.cyberdeck = parent
        self.agent = parent.agent
        self.session_manager = parent.agent.session_manager
        self.selected_session_id: Optional[str] = None
        self.selected_session: Optional[SessionRecord] = None
        self.sessions: list[SessionRecord] = []

        self.setWindowTitle("Архив сессий — Capri")
        self.resize(860, 560)
        self.setMinimumSize(640, 420)
        self.setModal(True)
        self.setStyleSheet(dialog_base_qss())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(14)

        layout.addLayout(
            _make_dialog_header(
                "Архив диалогов и резервные копии",
                "CAPRI // SESSION LOGISTICS",
                self.reject,
            )
        )

        body_layout = QHBoxLayout()
        body_layout.setSpacing(14)

        left_box = QVBoxLayout()
        left_box.setSpacing(8)

        self.edit_search = QLineEdit()
        self.edit_search.setMinimumHeight(34)
        self.edit_search.setPlaceholderText("Поиск по названию или тексту...")
        self.edit_search.addAction(get_icon("search", COLORS.text_muted, 14), QLineEdit.ActionPosition.LeadingPosition)
        self.edit_search.setClearButtonEnabled(True)
        self.edit_search.textChanged.connect(self._on_search_changed)
        left_box.addWidget(self.edit_search)

        self.list_sessions = QListWidget()
        self.list_sessions.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.list_sessions.setStyleSheet(f"""
            QListWidget {{
                background: {COLORS.canvas};
                border: 1px solid {COLORS.border_subtle};
                border-radius: 10px;
                padding: 4px;
                color: {COLORS.text_primary};
                font-family: {TYPE.sans};
                font-size: 12.5px;
                outline: none;
            }}
            QListWidget::item {{
                padding: 9px 10px;
                border-radius: 7px;
                margin-bottom: 2px;
                border-left: 2px solid transparent;
            }}
            QListWidget::item:hover {{
                background: {COLORS.surface};
            }}
            QListWidget::item:selected {{
                background: {COLORS.surface_active};
                border-left: 2px solid {COLORS.signal_cyan};
                color: {COLORS.text_primary};
            }}
            {scrollbar_qss(6)}
        """)
        self.list_sessions.itemSelectionChanged.connect(self._on_selection_changed)
        self.list_sessions.itemDoubleClicked.connect(lambda _: self._resume_selected())
        left_box.addWidget(self.list_sessions, stretch=1)

        self.lbl_count = QLabel("Сессий: 0")
        self.lbl_count.setStyleSheet(f"color: {COLORS.text_muted}; font-family: {TYPE.mono}; font-size: 11px;")
        left_box.addWidget(self.lbl_count)

        body_layout.addLayout(left_box, stretch=4)

        right_panel = QFrame()
        right_panel.setStyleSheet(f"""
            QFrame {{
                background: {COLORS.canvas};
                border: 1px solid {COLORS.border_subtle};
                border-radius: 10px;
            }}
        """)
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(16, 14, 16, 14)
        right_layout.setSpacing(10)

        self.lbl_session_title = QLabel("Выберите сессию из списка слева")
        self.lbl_session_title.setStyleSheet(
            f"color: {COLORS.text_primary}; font-family: {TYPE.display}; font-size: 14px; font-weight: 600; border: none;"
        )
        self.lbl_session_title.setWordWrap(True)
        right_layout.addWidget(self.lbl_session_title)

        self.lbl_session_meta = QLabel("UUID: -\nСоздана: -\nОбновлена: -")
        self.lbl_session_meta.setStyleSheet(
            f"color: {COLORS.text_muted}; font-family: {TYPE.mono}; font-size: 10.5px; line-height: 1.4; border: none;"
        )
        right_layout.addWidget(self.lbl_session_meta)

        lbl_preview_title = QLabel("ПРЕДПРОСМОТР ДИАЛОГА")
        lbl_preview_title.setStyleSheet(
            f"color: {COLORS.text_muted}; font-family: {TYPE.mono}; font-size: 10px; font-weight: 600; letter-spacing: 0.6px; border: none;"
        )
        right_layout.addWidget(lbl_preview_title)

        self.txt_preview = QTextEdit()
        self.txt_preview.setReadOnly(True)
        self.txt_preview.setStyleSheet(f"""
            QTextEdit {{
                background: {COLORS.surface_alt};
                color: {COLORS.text_primary};
                border: 1px solid {COLORS.border_hairline};
                border-radius: 8px;
                padding: 10px;
                font-family: {TYPE.sans};
                font-size: 12.5px;
            }}
            {scrollbar_qss(6)}
        """)
        right_layout.addWidget(self.txt_preview, stretch=1)

        actions_box = QHBoxLayout()
        actions_box.setSpacing(8)

        self.btn_resume = QPushButton("Восстановить")
        self.btn_resume.setIcon(get_icon("sessions", COLORS.text_on_accent, 14))
        self.btn_resume.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_resume.setMinimumHeight(36)
        self.btn_resume.setEnabled(False)
        self.btn_resume.setStyleSheet(button_primary_qss("7px 14px"))
        self.btn_resume.clicked.connect(self._resume_selected)
        actions_box.addWidget(self.btn_resume)

        self.btn_delete = QPushButton("Удалить")
        self.btn_delete.setIcon(get_icon("trash", COLORS.coral, 14))
        self.btn_delete.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_delete.setMinimumHeight(36)
        self.btn_delete.setEnabled(False)
        self.btn_delete.setStyleSheet(button_danger_qss(filled=False, padding="7px 14px"))
        self.btn_delete.clicked.connect(self._delete_selected)
        actions_box.addWidget(self.btn_delete)
        actions_box.addStretch()

        right_layout.addLayout(actions_box)
        body_layout.addWidget(right_panel, stretch=6)
        layout.addLayout(body_layout, stretch=1)

        footer_layout = QHBoxLayout()
        footer_layout.setSpacing(10)

        btn_new = QPushButton("Новая сессия")
        btn_new.setIcon(get_icon("plus", COLORS.signal_cyan, 14))
        btn_new.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        btn_new.setMinimumHeight(36)
        btn_new.setStyleSheet(button_secondary_qss("8px 14px"))
        btn_new.clicked.connect(self._create_new_session)
        footer_layout.addWidget(btn_new)

        btn_backup = QPushButton("Бэкап базы")
        btn_backup.setIcon(get_icon("download", COLORS.text_secondary, 14))
        btn_backup.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        btn_backup.setMinimumHeight(36)
        btn_backup.setStyleSheet(button_secondary_qss("8px 14px"))
        btn_backup.clicked.connect(self._create_backup)
        footer_layout.addWidget(btn_backup)

        footer_layout.addStretch()

        btn_close = QPushButton("Закрыть")
        btn_close.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        btn_close.setMinimumHeight(36)
        btn_close.setStyleSheet(button_secondary_qss("8px 18px"))
        btn_close.clicked.connect(self.reject)
        footer_layout.addWidget(btn_close)

        layout.addLayout(footer_layout)
        self._refresh_session_list()

    def _refresh_session_list(self):
        raw_query = self.edit_search.text().strip()
        filter_text = raw_query.casefold()
        self.list_sessions.clear()
        if raw_query and hasattr(self.session_manager, "search_sessions"):
            self.sessions = self.session_manager.search_sessions(raw_query, limit=100)
        else:
            self.sessions = self.session_manager.list_sessions(limit=100)

        active_id = self.agent.current_session_id
        count = 0
        target_row = 0
        for s in self.sessions:
            if filter_text and filter_text not in (s.title or "").lower() and filter_text not in (s.preview or "").lower():
                continue

            count += 1
            is_active = s.id == active_id
            status_tag = " · активна" if is_active else ""
            date_str = time.strftime("%d.%m.%Y %H:%M", time.localtime(s.updated_at))
            rounds_str = _format_rounds_ru(s.turn_count) if s.turn_count else "пустая"

            item = QListWidgetItem()
            item.setText(f"{s.title}{status_tag}\n{date_str}  ·  {rounds_str}")
            item.setData(Qt.ItemDataRole.UserRole, s.id)
            if is_active:
                item.setForeground(QColor(COLORS.signal_cyan))
                target_row = self.list_sessions.count()
            self.list_sessions.addItem(item)

        self.lbl_count.setText(f"Сессий: {count}")

        if self.list_sessions.count() > 0:
            self.list_sessions.setCurrentRow(target_row)
        else:
            self._clear_preview()

    def _on_search_changed(self, text: str):
        self._refresh_session_list()

    def _on_selection_changed(self):
        items = self.list_sessions.selectedItems()
        if not items:
            self._clear_preview()
            return

        session_id = items[0].data(Qt.ItemDataRole.UserRole)
        self.selected_session_id = session_id
        sess = self.session_manager.get_session(session_id)
        if not sess:
            self._clear_preview()
            return

        self.selected_session = sess
        is_active = sess.id == self.agent.current_session_id
        active_badge = f" <span style='color: {COLORS.mint}; font-size: 11px;'>● активна</span>" if is_active else ""
        escaped_title = html.escape(sess.title)
        self.lbl_session_title.setText(f"{escaped_title}{active_badge}")

        created_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(sess.created_at))
        updated_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(sess.updated_at))
        self.lbl_session_meta.setText(
            f"UUID: {sess.id}\nСоздана: {created_str}  ·  Обновлена: {updated_str}\nРаундов: {sess.turn_count}"
        )

        u_name = (
            getattr(getattr(getattr(self.parent(), "config", None), "companion", None), "user_name", "") or ""
        ).strip() or "User"
        turns = self.session_manager.get_recent_turns(session_id, limit=30)
        if turns:
            lines = []
            for t in turns:
                is_pro = getattr(t, "status", "") == "proactive" or (t.user_text or "").startswith("[INITIATIVE //")
                if t.user_text and not is_pro:
                    lines.append(f"<b style='color: {COLORS.coral};'>{html.escape(u_name)}:</b> {html.escape(t.user_text)}")
                if t.assistant_text:
                    clean_resp = html.escape(t.assistant_text).replace("\n", "<br>")
                    lines.append(f"<b style='color: {COLORS.signal_cyan};'>Капри:</b> {clean_resp}")
                lines.append(f"<hr style='border: 0; border-top: 1px solid {COLORS.border_hairline}; margin: 8px 0;'>")
            self.txt_preview.setHtml("".join(lines))
        else:
            self.txt_preview.setHtml(f"<i style='color: {COLORS.text_muted};'>В этой сессии пока нет сообщений.</i>")

        self.btn_resume.setEnabled(True)
        self.btn_delete.setEnabled(True)

    def _clear_preview(self):
        self.selected_session_id = None
        self.selected_session = None
        self.lbl_session_title.setText("Выберите сессию из списка слева")
        self.lbl_session_meta.setText("UUID: -\nСоздана: -\nОбновлена: -")
        self.txt_preview.clear()
        self.btn_resume.setEnabled(False)
        self.btn_delete.setEnabled(False)

    def _resume_selected(self):
        if not self.selected_session_id:
            return
        play_ui_sound("ui_click")
        self.cyberdeck._load_session_into_ui(self.selected_session_id)
        self.accept()

    def _delete_selected(self):
        if not self.selected_session_id:
            return
        play_ui_sound("ui_glitch")
        target_id = self.selected_session_id
        is_active = target_id == self.agent.current_session_id

        self.session_manager.soft_delete_session(target_id)
        if is_active:
            self.cyberdeck._start_new_session(auto=True)

        self._refresh_session_list()

    def _create_new_session(self):
        self.cyberdeck._start_new_session()
        self.accept()

    def _create_backup(self):
        play_ui_sound("ui_click")
        backup_dir = get_user_config_dir() / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        ts_str = time.strftime("%Y%m%d_%H%M%S")
        target = backup_dir / f"sessions_backup_{ts_str}.db"
        if self.session_manager.backup_to(target):
            self.cyberdeck._append_message("Система", f"Резервная копия создана:\n{target}")
            self.txt_preview.setHtml(f"<b style='color: {COLORS.mint};'>Резервная копия создана:</b><br>{target}")
        else:
            self.txt_preview.setHtml(f"<b style='color: {COLORS.coral};'>Ошибка создания резервной копии базы.</b>")


class CapriMemoryEditDialog(QDialog):
    """Add/edit dialog for Memory Workbench entries and pending candidates."""

    def __init__(
        self,
        parent=None,
        entry: Optional[MemoryEntry] = None,
        default_category: str = "general",
        candidate: Optional[MemoryCandidate] = None,
    ):
        super().__init__(parent)
        self.entry = entry
        self.candidate = candidate
        is_edit = entry is not None and candidate is None
        is_candidate = candidate is not None

        if is_candidate:
            self.setWindowTitle("Подтверждение кандидата памяти — Capri")
        else:
            self.setWindowTitle("Редактор памяти — Capri" if is_edit else "Новый факт памяти — Capri")
        self.resize(540, 440)
        self.setModal(True)
        self.setStyleSheet(dialog_base_qss())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        user_name = getattr(getattr(getattr(parent, "cyberdeck", None), "config", None), "companion", None)
        if user_name is None:
            user_name = getattr(getattr(parent, "config", None), "companion", None)
        u_label = getattr(user_name, "user_name", "User") if user_name else "User"
        if is_candidate:
            hdr_title = "Проверка кандидата памяти"
        else:
            hdr_title = "Редактирование факта" if is_edit else f"Новый факт о {u_label}"
        layout.addLayout(
            _make_dialog_header(
                hdr_title,
                "MEMORY WORKBENCH",
                self.reject,
            )
        )

        init_key = candidate.key if is_candidate else (entry.key if entry else "")
        init_cat = candidate.category if is_candidate else (entry.category if entry else default_category)
        init_imp = candidate.importance if is_candidate else (entry.importance if entry else 3)
        init_ret = candidate.retention if is_candidate else (getattr(entry, "retention", "long_term") if entry else "long_term")
        init_fact = candidate.fact if is_candidate else (entry.fact if entry else "")

        layout.addWidget(QLabel("Ключ (идентификатор факта)"))
        self.edit_key = QLineEdit(init_key)
        self.edit_key.setMinimumHeight(34)
        self.edit_key.setPlaceholderText("например: favorite_editor, stack, habit...")
        if is_edit:
            self.edit_key.setEnabled(False)
        layout.addWidget(self.edit_key)

        row = QHBoxLayout()
        row.setSpacing(10)

        cat_box = QVBoxLayout()
        cat_box.addWidget(QLabel("Категория"))
        self.combo_cat = QComboBox()
        self.combo_cat.setMinimumHeight(34)
        for cat in CATEGORIES:
            lbl = CATEGORY_LABELS.get(cat, cat)
            self.combo_cat.addItem(f"{lbl} ({cat})", cat)
        idx = self.combo_cat.findData(init_cat)
        if idx >= 0:
            self.combo_cat.setCurrentIndex(idx)
        cat_box.addWidget(self.combo_cat)
        row.addLayout(cat_box)

        imp_box = QVBoxLayout()
        imp_box.addWidget(QLabel("Приоритет"))
        self.combo_imp = QComboBox()
        self.combo_imp.setMinimumHeight(34)
        self.combo_imp.addItem("★☆☆☆☆ (1 — Низкая)", 1)
        self.combo_imp.addItem("★★☆☆☆ (2 — Ниже среднего)", 2)
        self.combo_imp.addItem("★★★☆☆ (3 — Стандартная)", 3)
        self.combo_imp.addItem("★★★★☆ (4 — Высокая)", 4)
        self.combo_imp.addItem("★★★★★ (5 — Критическая)", 5)
        idx_imp = self.combo_imp.findData(init_imp)
        if idx_imp >= 0:
            self.combo_imp.setCurrentIndex(idx_imp)
        imp_box.addWidget(self.combo_imp)
        row.addLayout(imp_box)

        ret_box = QVBoxLayout()
        ret_box.addWidget(QLabel("Срок хранения"))
        self.combo_retention = QComboBox()
        self.combo_retention.setMinimumHeight(34)
        self.combo_retention.addItem("Постоянно", "long_term")
        self.combo_retention.addItem("Временно (72ч)", "ephemeral")
        idx_ret = self.combo_retention.findData(init_ret if init_ret in RETENTION_MODES else "long_term")
        if idx_ret >= 0:
            self.combo_retention.setCurrentIndex(idx_ret)
        ret_box.addWidget(self.combo_retention)
        row.addLayout(ret_box)

        layout.addLayout(row)

        layout.addWidget(QLabel("Текст факта"))
        self.edit_fact = QTextEdit()
        self.edit_fact.setPlaceholderText("Опиши факт или знание...")
        self.edit_fact.setPlainText(init_fact)
        self.edit_fact.setMaximumHeight(96)
        layout.addWidget(self.edit_fact)

        if is_candidate and candidate.source_quote:
            lbl_quote = QLabel(f"Источник из диалога: «{candidate.source_quote}»")
            lbl_quote.setWordWrap(True)
            lbl_quote.setStyleSheet(f"color: {COLORS.text_muted}; font-size: 11px;")
            layout.addWidget(lbl_quote)

        layout.addWidget(QLabel("Теги (через запятую)"))
        self.edit_tags = QLineEdit(", ".join(entry.tags) if entry and entry.tags else "")
        self.edit_tags.setMinimumHeight(34)
        self.edit_tags.setPlaceholderText("arch, linux, dev, habit...")
        layout.addWidget(self.edit_tags)

        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(10)
        btn_layout.addStretch()

        self.btn_cancel = QPushButton("Отмена")
        self.btn_cancel.setMinimumHeight(36)
        self.btn_cancel.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_cancel.setStyleSheet(button_secondary_qss("7px 16px"))
        self.btn_cancel.clicked.connect(self.reject)
        btn_layout.addWidget(self.btn_cancel)

        self.btn_save = QPushButton("Запомнить" if is_candidate else "Сохранить")
        self.btn_save.setIcon(get_icon("check", COLORS.text_on_accent, 14))
        self.btn_save.setMinimumHeight(36)
        self.btn_save.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_save.setStyleSheet(button_primary_qss("7px 18px"))
        self.btn_save.clicked.connect(self._on_save)
        btn_layout.addWidget(self.btn_save)

        layout.addLayout(btn_layout)
        apply_dark_palette(self)

    def _on_save(self):
        k = self.edit_key.text().strip()
        f = self.edit_fact.toPlainText().strip()
        if not k or not f:
            QMessageBox.warning(self, "Ошибка", "Ключ и текст факта не могут быть пустыми.")
            return
        tags = [t.strip().lower() for t in self.edit_tags.text().split(",") if t.strip()]
        if memory_payload_contains_secret(k, f, tags=tags):
            QMessageBox.warning(
                self,
                "Секреты запрещены",
                "Пароли, API-ключи, токены, PIN-коды и другие секреты не сохраняются в память.",
            )
            return
        self.accept()

    def get_data(self) -> dict:
        tags_raw = self.edit_tags.text().split(",")
        tags = [t.strip().lower() for t in tags_raw if t.strip()]
        return {
            "key": self.edit_key.text().strip().lower(),
            "fact": self.edit_fact.toPlainText().strip(),
            "category": self.combo_cat.currentData(),
            "importance": self.combo_imp.currentData(),
            "retention": self.combo_retention.currentData() or "long_term",
            "tags": tags,
        }


class CapriMemoryWorkbenchDialog(QDialog):
    """Long-term Memory Workbench styled in LIVING SIGNAL with dark viewport/headers."""

    def __init__(self, parent: "CapriCyberdeckWindow"):
        super().__init__(parent)
        self.cyberdeck = parent
        self.store = getattr(parent.agent, "memory_store", None) or MemoryStore()

        self.setWindowTitle("Memory Workbench — Capri")
        self.resize(920, 580)
        self.setMinimumSize(680, 440)
        self.setModal(True)
        self.setStyleSheet(dialog_base_qss())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(14)

        layout.addLayout(
            _make_dialog_header(
                "Memory Workbench — Долговременная память",
                "CAPRI // COGNITIVE REPOSITORY",
                self.accept,
            )
        )

        bar = QHBoxLayout()
        bar.setSpacing(10)

        self.edit_search = QLineEdit()
        self.edit_search.setMinimumHeight(36)
        self.edit_search.setPlaceholderText("Поиск по ключу, факту или тегу...")
        self.edit_search.addAction(get_icon("search", COLORS.text_muted, 14), QLineEdit.ActionPosition.LeadingPosition)
        self.edit_search.setClearButtonEnabled(True)
        self.edit_search.textChanged.connect(self._refresh_table)
        bar.addWidget(self.edit_search, stretch=3)

        self.combo_cat_filter = QComboBox()
        self.combo_cat_filter.setMinimumHeight(36)
        self.combo_cat_filter.addItem("Все категории", "")
        for cat in CATEGORIES:
            lbl = CATEGORY_LABELS.get(cat, cat)
            self.combo_cat_filter.addItem(f"{lbl} ({cat})", cat)
        self.combo_cat_filter.currentIndexChanged.connect(self._refresh_table)
        bar.addWidget(self.combo_cat_filter, stretch=2)

        self.btn_add = QPushButton("Добавить факт")
        self.btn_add.setIcon(get_icon("plus", COLORS.text_on_accent, 14))
        self.btn_add.setMinimumHeight(36)
        self.btn_add.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_add.setStyleSheet(button_primary_qss("7px 14px"))
        self.btn_add.clicked.connect(self._on_add)
        bar.addWidget(self.btn_add)

        layout.addLayout(bar)

        # Pending memory candidates review strip (hidden when queue is empty)
        self.pending_frame = QFrame(self)
        self.pending_frame.setStyleSheet(f"""
            QFrame {{
                background: {COLORS.surface_raised};
                border: 1px solid {COLORS.crimson};
                border-radius: 10px;
            }}
        """)
        pf_layout = QHBoxLayout(self.pending_frame)
        pf_layout.setContentsMargins(12, 8, 12, 8)
        pf_layout.setSpacing(10)

        pf_text_col = QVBoxLayout()
        pf_text_col.setSpacing(2)
        self.lbl_wb_pending_title = QLabel("")
        self.lbl_wb_pending_title.setStyleSheet(
            f"color: {COLORS.crimson}; font-family: {TYPE.mono}; font-size: 10px; font-weight: 700; border: none;"
        )
        self.lbl_wb_pending_body = QLabel("")
        self.lbl_wb_pending_body.setWordWrap(True)
        self.lbl_wb_pending_body.setStyleSheet(f"color: {COLORS.text_primary}; font-size: 12px; border: none;")
        pf_text_col.addWidget(self.lbl_wb_pending_title)
        pf_text_col.addWidget(self.lbl_wb_pending_body)
        pf_layout.addLayout(pf_text_col, stretch=1)

        self.btn_wb_approve = QPushButton("Запомнить")
        self.btn_wb_approve.setMinimumHeight(30)
        self.btn_wb_approve.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_wb_approve.setStyleSheet(button_primary_qss("4px 12px"))
        self.btn_wb_approve.clicked.connect(self._on_approve_top_pending)
        pf_layout.addWidget(self.btn_wb_approve)

        self.btn_wb_edit_pending = QPushButton("Править")
        self.btn_wb_edit_pending.setMinimumHeight(30)
        self.btn_wb_edit_pending.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_wb_edit_pending.setStyleSheet(button_secondary_qss("4px 10px"))
        self.btn_wb_edit_pending.clicked.connect(self._on_edit_top_pending)
        pf_layout.addWidget(self.btn_wb_edit_pending)

        self.btn_wb_reject = QPushButton("Отклонить")
        self.btn_wb_reject.setMinimumHeight(30)
        self.btn_wb_reject.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_wb_reject.setStyleSheet(button_danger_qss(filled=False, padding="4px 10px"))
        self.btn_wb_reject.clicked.connect(self._on_reject_top_pending)
        pf_layout.addWidget(self.btn_wb_reject)

        self.pending_frame.setVisible(False)
        layout.addWidget(self.pending_frame)

        self.table = QTableWidget()
        self.table.setColumnCount(5)
        user_name = getattr(self.cyberdeck.config.companion, "user_name", "User") or "User"
        self.table.setHorizontalHeaderLabels(["Ключ", f"Факт о {user_name}", "Категория", "Важность", "Обновлено"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(True)
        self.table.verticalHeader().setVisible(False)
        self.table.doubleClicked.connect(self._on_edit)
        self.table.viewport().setStyleSheet(f"background: {COLORS.canvas}; background-color: {COLORS.canvas};")
        self.table.setStyleSheet(table_qss() + scrollbar_qss(6))
        layout.addWidget(self.table, stretch=1)

        self.lbl_status = QLabel("")
        self.lbl_status.setStyleSheet(f"color: {COLORS.text_muted}; font-family: {TYPE.mono}; font-size: 11px;")
        layout.addWidget(self.lbl_status)

        bottom = QHBoxLayout()
        bottom.setSpacing(8)

        self.btn_edit = QPushButton("Редактировать")
        self.btn_edit.setIcon(get_icon("edit", COLORS.text_secondary, 14))
        self.btn_edit.setMinimumHeight(36)
        self.btn_edit.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_edit.setStyleSheet(button_secondary_qss("7px 12px"))
        self.btn_edit.clicked.connect(self._on_edit)
        bottom.addWidget(self.btn_edit)

        self.btn_delete = QPushButton("Удалить")
        self.btn_delete.setIcon(get_icon("trash", COLORS.coral, 14))
        self.btn_delete.setMinimumHeight(36)
        self.btn_delete.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_delete.setStyleSheet(button_danger_qss(filled=False, padding="7px 12px"))
        self.btn_delete.clicked.connect(self._on_delete)
        bottom.addWidget(self.btn_delete)

        self.btn_clear_cat = QPushButton("Очистить категорию")
        self.btn_clear_cat.setIcon(get_icon("clear", COLORS.coral, 14))
        self.btn_clear_cat.setMinimumHeight(36)
        self.btn_clear_cat.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_clear_cat.setStyleSheet(button_danger_qss(filled=False, padding="7px 12px"))
        self.btn_clear_cat.clicked.connect(self._on_clear_category)
        bottom.addWidget(self.btn_clear_cat)

        bottom.addStretch()

        self.btn_export = QPushButton("Экспорт")
        self.btn_export.setIcon(get_icon("download", COLORS.text_secondary, 14))
        self.btn_export.setMinimumHeight(36)
        self.btn_export.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_export.setStyleSheet(button_secondary_qss("7px 12px"))
        self.btn_export.clicked.connect(self._on_export)
        bottom.addWidget(self.btn_export)

        self.btn_import = QPushButton("Импорт")
        self.btn_import.setIcon(get_icon("upload", COLORS.text_secondary, 14))
        self.btn_import.setMinimumHeight(36)
        self.btn_import.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_import.setStyleSheet(button_secondary_qss("7px 12px"))
        self.btn_import.clicked.connect(self._on_import)
        bottom.addWidget(self.btn_import)

        self.btn_close = QPushButton("Закрыть")
        self.btn_close.setMinimumHeight(36)
        self.btn_close.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_close.setStyleSheet(button_secondary_qss("7px 16px"))
        self.btn_close.clicked.connect(self.accept)
        bottom.addWidget(self.btn_close)

        layout.addLayout(bottom)
        apply_dark_palette(self)
        self._refresh_table()

    def _refresh_table(self):
        q = self.edit_search.text().strip()
        cat = self.combo_cat_filter.currentData() or None
        results = self.store.search(query=q, category=cat)

        self.table.setRowCount(len(results))
        for row, entry in enumerate(results):
            item_k = QTableWidgetItem(entry.key)
            item_k.setForeground(QColor(COLORS.signal_cyan))

            item_f = QTableWidgetItem(entry.fact)
            cat_label = CATEGORY_LABELS.get(entry.category, entry.category)
            item_c = QTableWidgetItem(cat_label)
            item_c.setForeground(QColor(COLORS.text_secondary))

            item_imp = QTableWidgetItem("★" * entry.importance)
            item_imp.setForeground(QColor(COLORS.amber))

            upd_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(entry.updated_at))
            if getattr(entry, "retention", "long_term") == "ephemeral":
                upd_str += " · врем."
            item_u = QTableWidgetItem(upd_str)
            item_u.setForeground(QColor(COLORS.text_muted))

            self.table.setItem(row, 0, item_k)
            self.table.setItem(row, 1, item_f)
            self.table.setItem(row, 2, item_c)
            self.table.setItem(row, 3, item_imp)
            self.table.setItem(row, 4, item_u)

        total = len(self.store.entries)
        visible = len(results)
        pending = self.store.list_pending()
        pend_note = f"  ·  На проверке: {len(pending)}" if pending else ""
        self.lbl_status.setText(f"Отображается записей: {visible} из {total}  ·  Категория: {cat or 'все'}{pend_note}")
        self._refresh_pending_banner()

    def _refresh_pending_banner(self):
        pending = self.store.list_pending()
        if not pending:
            self.pending_frame.setVisible(False)
            return
        top = pending[0]
        ret_lbl = "ВРЕМЕННО 72Ч" if top.retention == "ephemeral" else "ПОСТОЯННО"
        self.lbl_wb_pending_title.setText(
            f"КАНДИДАТ ПАМЯТИ (1/{len(pending)}) · {ret_lbl} · #{top.id}"
        )
        self.lbl_wb_pending_body.setText(f"[{top.key}]: {top.fact}")
        if top.source_quote:
            self.lbl_wb_pending_body.setToolTip(f"Источник: «{top.source_quote}»")
        else:
            self.lbl_wb_pending_body.setToolTip("")
        self.pending_frame.setVisible(True)

    def _notify_cyberdeck_memory_changed(self):
        if hasattr(self.cyberdeck, "_refresh_memory_candidate_bar"):
            self.cyberdeck._refresh_memory_candidate_bar()
        if hasattr(self.cyberdeck, "refresh_stats"):
            self.cyberdeck.refresh_stats()

    def _on_approve_top_pending(self):
        pending = self.store.list_pending()
        if not pending:
            return
        self.store.approve_candidate(pending[0].id)
        self._refresh_table()
        self._notify_cyberdeck_memory_changed()

    def _on_edit_top_pending(self):
        pending = self.store.list_pending()
        if not pending:
            return
        cand = pending[0]
        dlg = CapriMemoryEditDialog(self, candidate=cand)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            data = dlg.get_data()
            self.store.approve_candidate(
                cand.id,
                key=data["key"],
                fact=data["fact"],
                category=data["category"],
                importance=data["importance"],
                retention=data.get("retention", cand.retention),
                tags=data["tags"],
            )
            self._refresh_table()
            self._notify_cyberdeck_memory_changed()

    def _on_reject_top_pending(self):
        pending = self.store.list_pending()
        if not pending:
            return
        self.store.reject_candidate(pending[0].id)
        self._refresh_table()
        self._notify_cyberdeck_memory_changed()

    def _get_selected_key(self) -> Optional[str]:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        return item.text() if item else None

    def _on_add(self):
        cat_filter = self.combo_cat_filter.currentData() or "general"
        dlg = CapriMemoryEditDialog(self, default_category=cat_filter)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            data = dlg.get_data()
            self.store.add_or_update(
                key=data["key"],
                fact=data["fact"],
                category=data["category"],
                importance=data["importance"],
                retention=data.get("retention", "long_term"),
                tags=data["tags"],
                source="workbench",
            )
            self._refresh_table()
            self._notify_cyberdeck_memory_changed()

    def _on_edit(self):
        k = self._get_selected_key()
        if not k or k not in self.store.entries:
            QMessageBox.information(self, "Внимание", "Выберите запись для редактирования.")
            return
        entry = self.store.entries[k]
        dlg = CapriMemoryEditDialog(self, entry=entry)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            data = dlg.get_data()
            self.store.add_or_update(
                key=k,
                fact=data["fact"],
                category=data["category"],
                importance=data["importance"],
                retention=data.get("retention", getattr(entry, "retention", "long_term")),
                tags=data["tags"],
                source="workbench",
            )
            self._refresh_table()
            self._notify_cyberdeck_memory_changed()

    def _on_delete(self):
        k = self._get_selected_key()
        if not k:
            QMessageBox.information(self, "Внимание", "Выберите запись для удаления.")
            return
        reply = QMessageBox.question(
            self,
            "Подтверждение",
            f"Удалить факт с ключом [{k}] из долговременной памяти?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.store.delete(k)
            self._refresh_table()

    def _on_clear_category(self):
        cat = self.combo_cat_filter.currentData()
        target_name = CATEGORY_LABELS.get(cat, cat) if cat else "ВСЮ ПАМЯТЬ"
        reply = QMessageBox.question(
            self,
            "Очистка памяти",
            f"Вы уверены, что хотите полностью очистить [{target_name}]?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.store.clear(category=cat or None)
            self._refresh_table()

    def _on_export(self):
        backup_dir = get_user_config_dir() / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        default_path = str(backup_dir / f"memory_export_{int(time.time())}.json")
        file_path, _ = QFileDialog.getSaveFileName(
            self, "Экспорт долговременной памяти", default_path, "JSON Files (*.json)"
        )
        if file_path:
            try:
                data = self.store.export_dict()
                with open(file_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                QMessageBox.information(self, "Успех", f"Память успешно экспортирована в:\n{file_path}")
            except Exception as e:
                QMessageBox.critical(self, "Ошибка", f"Не удалось экспортировать файл:\n{e}")

    def _on_import(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Импорт долговременной памяти", str(Path.home()), "JSON Files (*.json)"
        )
        if file_path:
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    raw_data = json.load(f)
                cnt = self.store.import_dict(raw_data)
                self._refresh_table()
                QMessageBox.information(self, "Успех", f"Успешно импортировано записей: {cnt}")
            except Exception as e:
                QMessageBox.critical(self, "Ошибка", f"Не удалось импортировать файл:\n{e}")
