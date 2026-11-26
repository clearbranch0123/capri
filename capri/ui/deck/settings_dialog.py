"""Provider, voice, vision, and safety settings dialog extracted from Cyberdeck."""
from __future__ import annotations

import os
from typing import Optional

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSlider,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ...core.config import (
    PROVIDER_DEFAULTS,
    AppConfig,
    delete_env_value as _delete_env_value,
    is_local_llm_endpoint,
    save_config as _save_config,
    update_env_value as _update_env_value,
)
from ..i18n import resolve_ui_lang, tr_ui
from ..icons import get_icon
from ..theme import (
    COLORS,
    TYPE,
    apply_dark_palette,
    button_danger_qss,
    button_primary_qss,
    button_secondary_qss,
    dialog_base_qss,
    scrollbar_qss,
)
from . import cyberdeck_hook
from .dialogs import _make_dialog_header
from .workers import APIKeyCheckWorker, MicCalibrationWorker, _track_qthread


class CapriProviderSettingsDialog(QDialog):
    """Tabbed LLM core, voice/TTS/STT engines & APIs, Vision AI, and UI/safety settings dialog."""

    def __init__(self, current_config: AppConfig, parent=None):
        super().__init__(parent)
        self.config = current_config
        self._initial_provider = (self.config.llm.provider or "groq").strip().lower()
        self._initial_base_url = (self.config.llm.base_url or "").strip()
        self._cleared_env_keys: set[str] = set()
        self._calib_worker: Optional[MicCalibrationWorker] = None
        self._key_check_workers: dict[str, APIKeyCheckWorker] = {}
        self.setWindowTitle("Настройки нейроядра, голоса и зрения — Capri")
        self.setMinimumSize(620, 620)
        self.resize(680, 660)
        self.setModal(True)
        self.setStyleSheet(dialog_base_qss())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.setSpacing(12)

        layout.addLayout(
            _make_dialog_header(
                "Нейроядро, голос, зрение и API",
                "CAPRI // CORE, VOICE, VISION & KEYS",
                self.reject,
            )
        )

        def _field_label(text: str) -> QLabel:
            lbl = QLabel(text)
            lbl.setStyleSheet(
                f"color: {COLORS.text_secondary}; font-family: {TYPE.sans}; font-size: 12px; font-weight: 600; border: none;"
            )
            return lbl

        def _make_secret_row(
            placeholder: str,
            initial_val: str,
            on_clear=None,
        ) -> tuple[QHBoxLayout, QLineEdit, QPushButton, QPushButton, QPushButton]:
            row = QHBoxLayout()
            row.setSpacing(6)
            edit = QLineEdit()
            edit.setMinimumHeight(36)
            edit.setEchoMode(QLineEdit.EchoMode.Password)
            edit.setPlaceholderText(placeholder)
            edit.setText(initial_val)
            row.addWidget(edit, stretch=1)

            btn_eye = QPushButton()
            btn_eye.setIcon(get_icon("eye", COLORS.text_secondary, 15))
            btn_eye.setIconSize(QSize(15, 15))
            btn_eye.setFixedSize(36, 36)
            btn_eye.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            btn_eye.setToolTip("Показать / скрыть ключ")
            btn_eye.setStyleSheet(button_secondary_qss("0px"))
            btn_eye.clicked.connect(lambda _=False, e=edit, b=btn_eye: self._toggle_secret_field(e, b))
            row.addWidget(btn_eye)

            btn_paste = QPushButton("Вставить")
            btn_paste.setIcon(get_icon("paste", COLORS.text_secondary, 14))
            btn_paste.setIconSize(QSize(14, 14))
            btn_paste.setFixedHeight(36)
            btn_paste.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            btn_paste.setToolTip("Вставить ключ из буфера обмена")
            btn_paste.setStyleSheet(button_secondary_qss("0px 10px"))
            btn_paste.clicked.connect(lambda _=False, e=edit: self._paste_into_field(e))
            row.addWidget(btn_paste)

            btn_clear = QPushButton()
            btn_clear.setIcon(get_icon("trash", COLORS.coral, 14))
            btn_clear.setIconSize(QSize(14, 14))
            btn_clear.setFixedSize(36, 36)
            btn_clear.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            btn_clear.setToolTip("Удалить сохранённый ключ из .env")
            btn_clear.setStyleSheet(button_danger_qss(filled=False, padding="0px"))
            if on_clear is not None:
                btn_clear.clicked.connect(on_clear)
            else:
                btn_clear.clicked.connect(edit.clear)
            row.addWidget(btn_clear)
            return row, edit, btn_eye, btn_paste, btn_clear

        self.tabs = QTabWidget()
        self.tabs.tabBar().setDrawBase(False)
        self.tabs.setStyleSheet(f"""
            QTabWidget::pane {{
                border: 1px solid {COLORS.border_hairline};
                border-radius: 10px;
                background: {COLORS.surface_alt};
                top: -1px;
            }}
            QTabBar {{
                background: transparent;
                border: none;
            }}
            QTabBar::tab {{
                background: {COLORS.surface};
                color: {COLORS.text_secondary};
                border: 1px solid {COLORS.border_hairline};
                border-bottom: none;
                border-top-left-radius: 8px;
                border-top-right-radius: 8px;
                padding: 8px 14px;
                margin-right: 4px;
                font-family: {TYPE.sans};
                font-size: 12px;
                font-weight: 600;
            }}
            QTabBar::tab:selected {{
                background: {COLORS.surface_alt};
                color: {COLORS.signal_cyan};
                border-color: {COLORS.signal_cyan_border};
            }}
            QTabBar::tab:hover:!selected {{
                color: {COLORS.text_primary};
            }}
        """)

        # =====================================================================
        # Tab 1: LLM Core
        # =====================================================================
        tab_llm = QWidget()
        tab_llm.setStyleSheet("background: transparent;")
        llm_layout = QVBoxLayout(tab_llm)
        llm_layout.setContentsMargins(16, 14, 16, 14)
        llm_layout.setSpacing(10)

        llm_layout.addWidget(_field_label("Провайдер нейросети"))

        self.combo_provider = QComboBox()
        self.combo_provider.setMinimumHeight(36)
        ui_lang = resolve_ui_lang(self.config)
        self.providers_map = {
            tr_ui("DeepSeek (Официальный API)", "DeepSeek (Official API)", lang=ui_lang): "deepseek",
            tr_ui("Groq (Сверхбыстрый / Рекомендуется)", "Groq (Ultra-fast / Recommended)", lang=ui_lang): "groq",
            tr_ui("SambaNova (Llama 3.3 70B / gpt-oss-120b)", "SambaNova (Llama 3.3 70B / gpt-oss-120b)", lang=ui_lang): "sambanova",
            tr_ui("Google Gemini (Flash / Pro)", "Google Gemini (Flash / Pro)", lang=ui_lang): "gemini",
            tr_ui("OpenRouter (DeepSeek / Claude / GPT)", "OpenRouter (DeepSeek / Claude / GPT)", lang=ui_lang): "openrouter",
            tr_ui("OpenAI (Официальный API)", "OpenAI (Official API)", lang=ui_lang): "openai",
            tr_ui("Ollama (Локальный — без ключа)", "Ollama (Local — no API key)", lang=ui_lang): "ollama",
            tr_ui("LM Studio (Локальный — без ключа)", "LM Studio (Local — no API key)", lang=ui_lang): "lmstudio",
        }
        for label in self.providers_map:
            self.combo_provider.addItem(label)

        curr_p = (self.config.llm.provider or "groq").lower()
        for idx, (_label, p_id) in enumerate(self.providers_map.items()):
            if p_id == curr_p:
                self.combo_provider.setCurrentIndex(idx)
                break
        self.combo_provider.currentIndexChanged.connect(self._on_provider_changed)
        llm_layout.addWidget(self.combo_provider)

        llm_layout.addWidget(_field_label("API-ключ LLM (хранится только в .env, права 0600)"))
        key_box, self.edit_key, self.btn_show_key, self.btn_paste_key, self.btn_clear_key = _make_secret_row(
            "Вставь API-ключ выбранного провайдера...",
            self._get_current_key_for_provider(curr_p),
            on_clear=self._on_clear_llm_key,
        )
        llm_layout.addLayout(key_box)

        llm_check_row = QHBoxLayout()
        llm_check_row.setSpacing(8)
        self.btn_check_llm_key = QPushButton("Проверить LLM API")
        self.btn_check_llm_key.setIcon(get_icon("check", COLORS.signal_cyan, 13))
        self.btn_check_llm_key.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_check_llm_key.setFixedHeight(30)
        self.btn_check_llm_key.setStyleSheet(button_secondary_qss("4px 12px", radius=7))
        self.btn_check_llm_key.clicked.connect(self._on_check_llm_key)
        llm_check_row.addWidget(self.btn_check_llm_key)

        self.lbl_llm_check_status = QLabel("Нажми, чтобы проверить доступность ключа и Base URL")
        self.lbl_llm_check_status.setWordWrap(True)
        self.lbl_llm_check_status.setStyleSheet(
            f"color: {COLORS.text_muted}; font-family: {TYPE.mono}; font-size: 11px; border: none;"
        )
        llm_check_row.addWidget(self.lbl_llm_check_status, stretch=1)
        llm_layout.addLayout(llm_check_row)

        llm_layout.addWidget(_field_label("Модель (выбери или введи свою)"))
        self.combo_model = QComboBox()
        self.combo_model.setMinimumHeight(36)
        self.combo_model.setEditable(True)
        self._populate_models_for_provider(curr_p)
        llm_layout.addWidget(self.combo_model)

        llm_layout.addWidget(_field_label("API Base URL"))
        self.edit_base_url = QLineEdit()
        self.edit_base_url.setMinimumHeight(36)
        known_defaults = {v.get("base_url") for v in PROVIDER_DEFAULTS.values() if v.get("base_url")}
        prov_default_url = PROVIDER_DEFAULTS.get(curr_p, {}).get("base_url", "")
        cur_url = (self.config.llm.base_url or "").strip()
        if not cur_url or (cur_url in known_defaults and cur_url != prov_default_url):
            cur_url = prov_default_url
        self.edit_base_url.setText(cur_url)
        self._initial_base_url = cur_url
        llm_layout.addWidget(self.edit_base_url)
        llm_layout.addStretch()

        # =====================================================================
        # Tab 2: Interface, Profile & Safety
        # =====================================================================
        tab_pref = QWidget()
        tab_pref.setStyleSheet("background: transparent;")
        pref_layout = QVBoxLayout(tab_pref)
        pref_layout.setContentsMargins(16, 14, 16, 14)
        pref_layout.setSpacing(10)

        pref_top_grid = QGridLayout()
        pref_top_grid.setSpacing(10)
        pref_top_grid.addWidget(_field_label("Как к тебе обращаться (имя в диалоге и промпте)"), 0, 0)
        pref_top_grid.addWidget(_field_label("Язык интерфейса / UI Language"), 0, 1)

        self.edit_user_name = QLineEdit()
        self.edit_user_name.setMinimumHeight(36)
        self.edit_user_name.setPlaceholderText("Например: D, Alex, Kai...")
        self.edit_user_name.setText(getattr(self.config.companion, "user_name", "User") or "User")
        pref_top_grid.addWidget(self.edit_user_name, 1, 0)

        self.combo_ui_lang = QComboBox()
        self.combo_ui_lang.setMinimumHeight(36)
        self.combo_ui_lang.addItem("Русский (RU)", "ru")
        self.combo_ui_lang.addItem("English (EN)", "en")
        cur_ui_lang = resolve_ui_lang(self.config)
        ui_lang_idx = self.combo_ui_lang.findData(cur_ui_lang)
        if ui_lang_idx >= 0:
            self.combo_ui_lang.setCurrentIndex(ui_lang_idx)
        pref_top_grid.addWidget(self.combo_ui_lang, 1, 1)
        pref_top_grid.setColumnStretch(0, 3)
        pref_top_grid.setColumnStretch(1, 2)
        pref_layout.addLayout(pref_top_grid)

        pref_layout.addWidget(_field_label("Политика безопасности инструментов (ToolPolicyEngine)"))
        self.combo_policy = QComboBox()
        self.combo_policy.setMinimumHeight(36)
        self.combo_policy.addItem("Ask — запрашивать подтверждение на изменения (Рекомендуется)", "ask")
        self.combo_policy.addItem("Safe — строгий read-only режим (запрет любых изменений)", "safe")
        self.combo_policy.addItem("Trusted — десктоп и обычные shell/файлы авто; sudo/rm/kill/секреты с подтверждением", "trusted")
        curr_pol = (getattr(self.config.safety, "tool_policy", "ask") or "ask").lower()
        pol_idx = self.combo_policy.findData(curr_pol)
        if pol_idx >= 0:
            self.combo_policy.setCurrentIndex(pol_idx)
        pref_layout.addWidget(self.combo_policy)

        toggles_frame = QFrame()
        toggles_frame.setStyleSheet(f"""
            QFrame {{
                background: {COLORS.surface};
                border: 1px solid {COLORS.border_hairline};
                border-radius: 10px;
            }}
        """)
        tl = QVBoxLayout(toggles_frame)
        tl.setContentsMargins(14, 12, 14, 12)
        tl.setSpacing(10)

        self.chk_avatar_enabled = QCheckBox("Включить рабочий desktop-аватар Capri")
        self.chk_avatar_enabled.setChecked(bool(getattr(self.config.ui, "avatar_enabled", True)))
        tl.addWidget(self.chk_avatar_enabled)

        self.chk_auto_avatar_poses = QCheckBox("Авто-позы аватара (музыка MPRIS, сон и живые реакции)")
        self.chk_auto_avatar_poses.setChecked(bool(getattr(self.config.ui, "auto_avatar_poses", True)))
        tl.addWidget(self.chk_auto_avatar_poses)

        self.chk_game_mode = QCheckBox("Игровой режим / Power Saver (автопауза в fullscreen-играх)")
        self.chk_game_mode.setChecked(bool(getattr(self.config.ui, "game_mode_enabled", True)))
        tl.addWidget(self.chk_game_mode)

        self.chk_reduced_motion = QCheckBox("Уменьшить анимации интерфейса (Reduced Motion)")
        self.chk_reduced_motion.setChecked(bool(getattr(self.config.ui, "reduced_motion", False)))
        tl.addWidget(self.chk_reduced_motion)

        self.chk_minimize_to_tray = QCheckBox("Сворачивать в системный трей при закрытии окна")
        self.chk_minimize_to_tray.setChecked(bool(getattr(self.config.ui, "minimize_to_tray", True)))
        tl.addWidget(self.chk_minimize_to_tray)

        pref_layout.addWidget(toggles_frame)
        pref_layout.addStretch()

        # =====================================================================
        # Tab 3: Voice, TTS/STT Engines, Fish Audio / ElevenLabs APIs & PipeWire AEC
        # =====================================================================
        tab_voice_scroll = QScrollArea()
        tab_voice_scroll.setWidgetResizable(True)
        tab_voice_scroll.setFrameShape(QFrame.Shape.NoFrame)
        tab_voice_scroll.setStyleSheet(f"QScrollArea {{ background: transparent; border: none; }} {scrollbar_qss(6)}")

        tab_voice = QWidget()
        tab_voice.setStyleSheet("background: transparent;")
        voice_layout = QVBoxLayout(tab_voice)
        voice_layout.setContentsMargins(16, 14, 16, 14)
        voice_layout.setSpacing(10)

        # TTS Engine & STT Mode selectors
        engines_grid = QGridLayout()
        engines_grid.setSpacing(10)

        engines_grid.addWidget(_field_label("Движок озвучки (TTS)"), 0, 0)
        engines_grid.addWidget(_field_label("Распознавание речи (STT)"), 0, 1)

        self.combo_tts_engine = QComboBox()
        self.combo_tts_engine.setMinimumHeight(36)
        ui_lang = resolve_ui_lang(self.config)
        self.combo_tts_engine.addItem(
            tr_ui(
                "Fish Audio (Рекомендуется — живой голос)",
                "Fish Audio (Recommended — expressive voice)",
                lang=ui_lang,
            ),
            "fish",
        )
        self.combo_tts_engine.addItem(
            tr_ui(
                "ElevenLabs (Облачный нейронный)",
                "ElevenLabs (Cloud neural voice)",
                lang=ui_lang,
            ),
            "elevenlabs",
        )
        self.combo_tts_engine.addItem(
            tr_ui(
                "Edge TTS (Бесплатный резерв без ключа)",
                "Edge TTS (Free fallback — no key)",
                lang=ui_lang,
            ),
            "edge",
        )
        cur_tts = (getattr(self.config.voice, "tts_engine", "fish") or "fish").strip().lower()
        tts_idx = self.combo_tts_engine.findData(cur_tts)
        self.combo_tts_engine.setCurrentIndex(tts_idx if tts_idx >= 0 else 0)
        self.combo_tts_engine.currentIndexChanged.connect(self._sync_tts_provider_panels)
        engines_grid.addWidget(self.combo_tts_engine, 1, 0)

        self.combo_stt_mode = QComboBox()
        self.combo_stt_mode.setMinimumHeight(36)
        self.combo_stt_mode.addItem("Groq Whisper Turbo (Облачный)", "groq")
        self.combo_stt_mode.addItem("Local Whisper (Оффлайн)", "local")
        cur_stt = (getattr(self.config.voice, "stt_mode", "groq") or "groq").strip().lower()
        stt_idx = self.combo_stt_mode.findData(cur_stt)
        self.combo_stt_mode.setCurrentIndex(stt_idx if stt_idx >= 0 else 0)
        engines_grid.addWidget(self.combo_stt_mode, 1, 1)
        voice_layout.addLayout(engines_grid)

        # Fish Audio API & Voice ID panel
        self.fish_panel = QFrame()
        self.fish_panel.setStyleSheet(f"""
            QFrame {{
                background: {COLORS.surface};
                border: 1px solid {COLORS.border_hairline};
                border-radius: 10px;
            }}
        """)
        fp_l = QVBoxLayout(self.fish_panel)
        fp_l.setContentsMargins(14, 11, 14, 11)
        fp_l.setSpacing(8)
        fp_l.addWidget(_field_label("Fish Audio API-ключ (FISH_AUDIO_API_KEY)"))
        fish_key_val = (
            getattr(self.config.voice, "fish_audio_api_key", "")
            or os.getenv("FISH_AUDIO_API_KEY", "")
        ).strip()
        fish_row, self.edit_fish_key, self.btn_show_fish_key, self.btn_paste_fish_key, self.btn_clear_fish_key = _make_secret_row(
            "Вставь Fish Audio API-ключ...",
            fish_key_val,
            on_clear=lambda: self._mark_env_key_cleared("FISH_AUDIO_API_KEY", self.edit_fish_key, self.lbl_fish_check_status),
        )
        fp_l.addLayout(fish_row)

        fp_l.addWidget(_field_label("Fish Audio Voice / Reference ID (FISH_AUDIO_VOICE_ID)"))
        self.edit_fish_voice_id = QLineEdit()
        self.edit_fish_voice_id.setMinimumHeight(34)
        self.edit_fish_voice_id.setPlaceholderText("ID модели голоса на fish.audio (опционально)")
        self.edit_fish_voice_id.setText(
            (getattr(self.config.voice, "fish_audio_voice_id", "") or os.getenv("FISH_AUDIO_VOICE_ID", "")).strip()
        )
        fp_l.addWidget(self.edit_fish_voice_id)

        fish_chk_row = QHBoxLayout()
        fish_chk_row.setSpacing(8)
        self.btn_check_fish_key = QPushButton("Проверить Fish API")
        self.btn_check_fish_key.setIcon(get_icon("check", COLORS.signal_cyan, 13))
        self.btn_check_fish_key.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_check_fish_key.setFixedHeight(30)
        self.btn_check_fish_key.setStyleSheet(button_secondary_qss("4px 12px", radius=7))
        self.btn_check_fish_key.clicked.connect(self._on_check_fish_key)
        fish_chk_row.addWidget(self.btn_check_fish_key)

        self.lbl_fish_check_status = QLabel("Ключ сохраняется в .env (0600)")
        self.lbl_fish_check_status.setWordWrap(True)
        self.lbl_fish_check_status.setStyleSheet(
            f"color: {COLORS.text_muted}; font-family: {TYPE.mono}; font-size: 11px; border: none;"
        )
        fish_chk_row.addWidget(self.lbl_fish_check_status, stretch=1)
        fp_l.addLayout(fish_chk_row)
        voice_layout.addWidget(self.fish_panel)

        # ElevenLabs API & Voice ID panel (shown when ElevenLabs is selected)
        self.eleven_panel = QFrame()
        self.eleven_panel.setStyleSheet(f"""
            QFrame {{
                background: {COLORS.surface};
                border: 1px solid {COLORS.border_hairline};
                border-radius: 10px;
            }}
        """)
        el_l = QVBoxLayout(self.eleven_panel)
        el_l.setContentsMargins(14, 11, 14, 11)
        el_l.setSpacing(8)
        el_l.addWidget(_field_label("ElevenLabs API-ключ (ELEVENLABS_API_KEY)"))
        el_key_val = (
            getattr(self.config.voice, "elevenlabs_api_key", "")
            or os.getenv("ELEVENLABS_API_KEY", "")
        ).strip()
        el_row, self.edit_eleven_key, self.btn_show_eleven_key, self.btn_paste_eleven_key, self.btn_clear_eleven_key = _make_secret_row(
            "Вставь ElevenLabs API-ключ...",
            el_key_val,
            on_clear=lambda: self._mark_env_key_cleared("ELEVENLABS_API_KEY", self.edit_eleven_key, self.lbl_eleven_check_status),
        )
        el_l.addLayout(el_row)

        el_l.addWidget(_field_label("ElevenLabs Voice ID (ELEVENLABS_VOICE_ID)"))
        self.edit_eleven_voice_id = QLineEdit()
        self.edit_eleven_voice_id.setMinimumHeight(34)
        self.edit_eleven_voice_id.setPlaceholderText("ID голоса ElevenLabs")
        self.edit_eleven_voice_id.setText(
            (getattr(self.config.voice, "elevenlabs_voice_id", "") or os.getenv("ELEVENLABS_VOICE_ID", "")).strip()
        )
        el_l.addWidget(self.edit_eleven_voice_id)

        el_chk_row = QHBoxLayout()
        el_chk_row.setSpacing(8)
        self.btn_check_eleven_key = QPushButton("Проверить ElevenLabs")
        self.btn_check_eleven_key.setIcon(get_icon("check", COLORS.signal_cyan, 13))
        self.btn_check_eleven_key.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_check_eleven_key.setFixedHeight(30)
        self.btn_check_eleven_key.setStyleSheet(button_secondary_qss("4px 12px", radius=7))
        self.btn_check_eleven_key.clicked.connect(self._on_check_eleven_key)
        el_chk_row.addWidget(self.btn_check_eleven_key)

        self.lbl_eleven_check_status = QLabel("Ключ сохраняется в .env (0600)")
        self.lbl_eleven_check_status.setWordWrap(True)
        self.lbl_eleven_check_status.setStyleSheet(
            f"color: {COLORS.text_muted}; font-family: {TYPE.mono}; font-size: 11px; border: none;"
        )
        el_chk_row.addWidget(self.lbl_eleven_check_status, stretch=1)
        el_l.addLayout(el_chk_row)
        voice_layout.addWidget(self.eleven_panel)

        self.lbl_tts_engine_hint = QLabel("")
        self.lbl_tts_engine_hint.setWordWrap(True)
        self.lbl_tts_engine_hint.setStyleSheet(
            f"color: {COLORS.text_muted}; font-family: {TYPE.mono}; font-size: 11px; border: none;"
        )
        voice_layout.addWidget(self.lbl_tts_engine_hint)

        voice_toggles = QFrame()
        voice_toggles.setStyleSheet(f"""
            QFrame {{
                background: {COLORS.surface};
                border: 1px solid {COLORS.border_hairline};
                border-radius: 10px;
            }}
        """)
        vtl = QVBoxLayout(voice_toggles)
        vtl.setContentsMargins(14, 12, 14, 12)
        vtl.setSpacing(9)

        self.chk_tts_enabled = QCheckBox("Озвучивать ответы Капри (TTS)")
        self.chk_tts_enabled.setChecked(bool(getattr(self.config.voice, "tts_enabled", True)))
        vtl.addWidget(self.chk_tts_enabled)

        self.chk_wake_word_enabled = QCheckBox("Активация голосом Wake-Word («Капри», оффлайн Vosk)")
        self.chk_wake_word_enabled.setChecked(bool(getattr(self.config.voice, "wake_word_enabled", False)))
        vtl.addWidget(self.chk_wake_word_enabled)

        self.chk_hands_free_barge_in = QCheckBox("Перебивать речь голосом (Hands-Free Barge-In)")
        self.chk_hands_free_barge_in.setChecked(bool(getattr(self.config.voice, "hands_free_barge_in", True)))
        vtl.addWidget(self.chk_hands_free_barge_in)

        self.chk_pipewire_aec = QCheckBox("Эхоподавление PipeWire WebRTC AEC (для колонок)")
        self.chk_pipewire_aec.setChecked(bool(getattr(self.config.voice, "pipewire_aec_enabled", False)))
        vtl.addWidget(self.chk_pipewire_aec)

        aec_row = QHBoxLayout()
        aec_row.setSpacing(8)
        self.lbl_aec_status = QLabel("PipeWire AEC: проверка...")
        self.lbl_aec_status.setWordWrap(True)
        self.lbl_aec_status.setStyleSheet(
            f"color: {COLORS.text_muted}; font-family: {TYPE.mono}; font-size: 11px; border: none;"
        )
        aec_row.addWidget(self.lbl_aec_status, stretch=1)

        self.btn_toggle_aec = QPushButton("Включить AEC")
        self.btn_toggle_aec.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_toggle_aec.setFixedHeight(30)
        self.btn_toggle_aec.setStyleSheet(button_secondary_qss("4px 12px", radius=7))
        self.btn_toggle_aec.clicked.connect(self._on_toggle_aec_clicked)
        aec_row.addWidget(self.btn_toggle_aec)
        vtl.addLayout(aec_row)
        voice_layout.addWidget(voice_toggles)

        voice_layout.addWidget(_field_label("Микрофон (источник ввода PipeWire / sounddevice)"))
        self.combo_input_device = QComboBox()
        self.combo_input_device.setMinimumHeight(34)
        self._populate_input_devices()
        voice_layout.addWidget(self.combo_input_device)

        calib_frame = QFrame()
        calib_frame.setStyleSheet(f"""
            QFrame {{
                background: {COLORS.surface};
                border: 1px solid {COLORS.border_hairline};
                border-radius: 10px;
            }}
        """)
        cl = QVBoxLayout(calib_frame)
        cl.setContentsMargins(14, 11, 14, 11)
        cl.setSpacing(8)

        thr_hdr = QHBoxLayout()
        lbl_thr_title = QLabel("Порог срабатывания Barge-In (RMS)")
        lbl_thr_title.setStyleSheet(
            f"color: {COLORS.text_secondary}; font-family: {TYPE.sans}; font-size: 12px; font-weight: 600; border: none;"
        )
        thr_hdr.addWidget(lbl_thr_title)
        thr_hdr.addStretch()

        init_thr = max(0.015, min(0.200, float(getattr(self.config.voice, "barge_in_rms_threshold", 0.032))))
        self.lbl_barge_threshold_val = QLabel(f"{init_thr:.3f}")
        self.lbl_barge_threshold_val.setStyleSheet(
            f"color: {COLORS.signal_cyan}; font-family: {TYPE.mono}; font-size: 12px; font-weight: 700; border: none;"
        )
        thr_hdr.addWidget(self.lbl_barge_threshold_val)
        cl.addLayout(thr_hdr)

        self.slider_barge_threshold = QSlider(Qt.Orientation.Horizontal)
        self.slider_barge_threshold.setRange(15, 200)
        self.slider_barge_threshold.setValue(int(round(init_thr * 1000)))
        self.slider_barge_threshold.valueChanged.connect(
            lambda v: self.lbl_barge_threshold_val.setText(f"{v / 1000.0:.3f}")
        )
        cl.addWidget(self.slider_barge_threshold)

        self.bar_mic_level = QProgressBar()
        self.bar_mic_level.setRange(0, 100)
        self.bar_mic_level.setValue(0)
        self.bar_mic_level.setFixedHeight(8)
        self.bar_mic_level.setTextVisible(False)
        self.bar_mic_level.setStyleSheet(f"""
            QProgressBar {{
                background: {COLORS.canvas};
                border: 1px solid {COLORS.border_hairline};
                border-radius: 4px;
            }}
            QProgressBar::chunk {{
                background: {COLORS.mint};
                border-radius: 3px;
            }}
        """)
        cl.addWidget(self.bar_mic_level)

        calib_row = QHBoxLayout()
        calib_row.setSpacing(8)
        self.btn_calibrate_mic = QPushButton("Калибровать микрофон (3 сек)")
        self.btn_calibrate_mic.setIcon(get_icon("mic", COLORS.signal_cyan, 14))
        self.btn_calibrate_mic.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_calibrate_mic.setFixedHeight(32)
        self.btn_calibrate_mic.setStyleSheet(button_secondary_qss("4px 12px", radius=7))
        self.btn_calibrate_mic.clicked.connect(self._on_calibrate_mic_clicked)
        calib_row.addWidget(self.btn_calibrate_mic)

        self.lbl_calibration_status = QLabel("Наушники: ~0.032 · Колонки: ~0.065 или AEC")
        self.lbl_calibration_status.setWordWrap(True)
        self.lbl_calibration_status.setStyleSheet(
            f"color: {COLORS.text_muted}; font-family: {TYPE.mono}; font-size: 10.5px; border: none;"
        )
        calib_row.addWidget(self.lbl_calibration_status, stretch=1)
        cl.addLayout(calib_row)

        voice_layout.addWidget(calib_frame)
        voice_layout.addStretch()
        tab_voice_scroll.setWidget(tab_voice)

        # =====================================================================
        # Tab 4: Vision AI ("Просмотр экрана") & Gemini API + Keys Status
        # =====================================================================
        tab_vision = QWidget()
        tab_vision.setStyleSheet("background: transparent;")
        vision_layout = QVBoxLayout(tab_vision)
        vision_layout.setContentsMargins(16, 14, 16, 14)
        vision_layout.setSpacing(10)

        self.chk_vision_enabled = QCheckBox("Включить просмотр экрана и анализ изображений (/look, Ctrl+V)")
        self.chk_vision_enabled.setChecked(bool(getattr(self.config.vision, "enabled", True)))
        vision_layout.addWidget(self.chk_vision_enabled)

        vision_layout.addWidget(_field_label("Метод анализа экрана (Vision Provider)"))
        self.combo_vision_provider = QComboBox()
        self.combo_vision_provider.setMinimumHeight(36)
        self.combo_vision_provider.addItem("Google Gemini Vision AI (Рекомендуется)", "gemini")
        self.combo_vision_provider.addItem("Локальный OCR (Tesseract, оффлайн)", "ocr")
        cur_vp = (getattr(self.config.vision, "provider", "gemini") or "gemini").strip().lower()
        vp_idx = self.combo_vision_provider.findData(cur_vp)
        self.combo_vision_provider.setCurrentIndex(vp_idx if vp_idx >= 0 else 0)
        vision_layout.addWidget(self.combo_vision_provider)

        vision_layout.addWidget(_field_label("Модель Gemini Vision"))
        self.combo_vision_model = QComboBox()
        self.combo_vision_model.setMinimumHeight(36)
        self.combo_vision_model.setEditable(True)
        for vm in ("gemini-3.1-flash-lite", "gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.0-flash"):
            self.combo_vision_model.addItem(vm)
        cur_vm = (getattr(self.config.vision, "model", "gemini-3.1-flash-lite") or "gemini-3.1-flash-lite").strip()
        vm_idx = self.combo_vision_model.findText(cur_vm)
        if vm_idx >= 0:
            self.combo_vision_model.setCurrentIndex(vm_idx)
        else:
            self.combo_vision_model.setCurrentText(cur_vm)
        vision_layout.addWidget(self.combo_vision_model)

        vision_layout.addWidget(_field_label("Gemini API-ключ (GEMINI_API_KEY — для просмотра экрана и чата Gemini)"))
        gem_key_val = (
            getattr(self.config.vision, "gemini_api_key", "")
            or getattr(self.config.voice, "gemini_api_key", "")
            or os.getenv("GEMINI_API_KEY", "")
        ).strip()
        gem_row, self.edit_gemini_key, self.btn_show_gemini_key, self.btn_paste_gemini_key, self.btn_clear_gemini_key = _make_secret_row(
            "Вставь Google Gemini API-ключ (AIza...)...",
            gem_key_val,
            on_clear=lambda: self._mark_env_key_cleared("GEMINI_API_KEY", self.edit_gemini_key, self.lbl_gemini_check_status),
        )
        vision_layout.addLayout(gem_row)

        gem_chk_row = QHBoxLayout()
        gem_chk_row.setSpacing(8)
        self.btn_check_gemini_key = QPushButton("Проверить Gemini API")
        self.btn_check_gemini_key.setIcon(get_icon("check", COLORS.signal_cyan, 13))
        self.btn_check_gemini_key.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_check_gemini_key.setFixedHeight(30)
        self.btn_check_gemini_key.setStyleSheet(button_secondary_qss("4px 12px", radius=7))
        self.btn_check_gemini_key.clicked.connect(self._on_check_gemini_key)
        gem_chk_row.addWidget(self.btn_check_gemini_key)

        self.lbl_gemini_check_status = QLabel("Используется для /look и анализа скриншотов (Ctrl+V / Область)")
        self.lbl_gemini_check_status.setWordWrap(True)
        self.lbl_gemini_check_status.setStyleSheet(
            f"color: {COLORS.text_muted}; font-family: {TYPE.mono}; font-size: 11px; border: none;"
        )
        gem_chk_row.addWidget(self.lbl_gemini_check_status, stretch=1)
        vision_layout.addLayout(gem_chk_row)

        # Compact live API keys summary box
        keys_summary_frame = QFrame()
        keys_summary_frame.setStyleSheet(f"""
            QFrame {{
                background: {COLORS.surface};
                border: 1px solid {COLORS.border_hairline};
                border-radius: 10px;
            }}
        """)
        ks_l = QVBoxLayout(keys_summary_frame)
        ks_l.setContentsMargins(14, 11, 14, 11)
        ks_l.setSpacing(6)
        ks_l.addWidget(_field_label("Статус настроенных API-ключей (.env)"))
        self.lbl_keys_overview = QLabel("")
        self.lbl_keys_overview.setWordWrap(True)
        self.lbl_keys_overview.setStyleSheet(
            f"color: {COLORS.text_secondary}; font-family: {TYPE.mono}; font-size: 11px; border: none;"
        )
        ks_l.addWidget(self.lbl_keys_overview)
        vision_layout.addWidget(keys_summary_frame)
        vision_layout.addStretch()

        self._sync_tts_provider_panels()
        self._refresh_aec_status_ui()
        self._refresh_keys_overview()

        for ed in (self.edit_key, self.edit_fish_key, self.edit_eleven_key, self.edit_gemini_key):
            ed.textChanged.connect(lambda _t: self._refresh_keys_overview())

        self.tabs.addTab(tab_llm, "Нейроядро")
        self.tabs.addTab(tab_voice_scroll, "Голос и аудио")
        self.tabs.addTab(tab_vision, "Зрение и API")
        self.tabs.addTab(tab_pref, "Интерфейс и безопасность")
        layout.addWidget(self.tabs, stretch=1)

        btn_box = QHBoxLayout()
        btn_box.setSpacing(10)

        btn_cancel = QPushButton("Отмена")
        btn_cancel.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        btn_cancel.setMinimumHeight(36)
        btn_cancel.setStyleSheet(button_secondary_qss("8px 16px"))
        btn_cancel.clicked.connect(self.reject)
        btn_box.addWidget(btn_cancel)

        btn_box.addStretch()

        self.btn_save = QPushButton("Применить и сохранить")
        self.btn_save.setIcon(get_icon("check", COLORS.text_on_accent, 14))
        self.btn_save.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_save.setMinimumHeight(36)
        self.btn_save.setStyleSheet(button_primary_qss("8px 18px"))
        self.btn_save.clicked.connect(self._apply_and_save)
        btn_box.addWidget(self.btn_save)

        layout.addLayout(btn_box)
        apply_dark_palette(self)

    def _mark_env_key_cleared(self, env_var: str, field: QLineEdit, status_lbl: Optional[QLabel] = None):
        self._cleared_env_keys.add(env_var)
        field.clear()
        if status_lbl is not None:
            status_lbl.setText(f"Ключ {env_var} будет удалён из .env после сохранения")
            status_lbl.setStyleSheet(
                f"color: {COLORS.amber}; font-family: {TYPE.mono}; font-size: 11px; border: none;"
            )
        if hasattr(self, "lbl_keys_overview"):
            self._refresh_keys_overview()

    def _on_clear_llm_key(self):
        p_id = self._get_selected_provider_id()
        env_var = PROVIDER_DEFAULTS.get(p_id, {}).get("env_key", f"{p_id.upper()}_API_KEY")
        self._mark_env_key_cleared(env_var, self.edit_key, self.lbl_llm_check_status)

    def _toggle_secret_field(self, field: QLineEdit, btn: QPushButton):
        if field.echoMode() == QLineEdit.EchoMode.Password:
            field.setEchoMode(QLineEdit.EchoMode.Normal)
            btn.setIcon(get_icon("eye-off", COLORS.signal_cyan, 15))
        else:
            field.setEchoMode(QLineEdit.EchoMode.Password)
            btn.setIcon(get_icon("eye", COLORS.text_secondary, 15))

    def _paste_into_field(self, field: QLineEdit):
        clipboard = QApplication.clipboard()
        text = clipboard.text().strip()
        if text:
            field.setText(text)

    def _sync_tts_provider_panels(self):
        eng = (self.combo_tts_engine.currentData() or "fish").lower()
        ui_lang = resolve_ui_lang(self.config)
        self.fish_panel.setVisible(eng == "fish")
        self.eleven_panel.setVisible(eng == "elevenlabs")
        hints = {
            "fish": tr_ui(
                "Используй свой API-ключ и Voice ID с fish.audio. Совет: подбирай голос на том же языке, на котором общаешься с Капри (RU-голос для русского, EN-голос для английского).",
                "Use your own API key and Voice ID from fish.audio. Tip: match the voice language to your chat language (EN voice for English, RU voice for Russian) to avoid accent distortion.",
                lang=ui_lang,
            ),
            "elevenlabs": tr_ui(
                "Используй свой API-ключ и Voice ID из ElevenLabs. Для естественного звучания язык голоса должен совпадать с языком общения.",
                "Use your own ElevenLabs API key and Voice ID. Match the voice language to your chat language for natural pronunciation.",
                lang=ui_lang,
            ),
            "edge": tr_ui(
                "Бесплатный резервный TTS без API-ключа (базовый голос). Для выразительного живого звучания рекомендуется Fish Audio или ElevenLabs.",
                "Free fallback TTS with no API key required (basic voice). For a more expressive character voice, use Fish Audio or ElevenLabs.",
                lang=ui_lang,
            ),
        }
        txt = hints.get(eng, "")
        self.lbl_tts_engine_hint.setText(txt)
        self.lbl_tts_engine_hint.setVisible(bool(txt))

    def _refresh_keys_overview(self):
        p_id = self._get_selected_provider_id()
        cur_llm_k = self.edit_key.text().strip()
        groq_ok = bool(cur_llm_k if p_id == "groq" else os.getenv("GROQ_API_KEY", "").strip())
        ds_ok = bool(cur_llm_k if p_id == "deepseek" else os.getenv("DEEPSEEK_API_KEY", "").strip())
        gem_ok = bool(self.edit_gemini_key.text().strip() or (cur_llm_k if p_id == "gemini" else ""))
        fish_ok = bool(self.edit_fish_key.text().strip())
        el_ok = bool(self.edit_eleven_key.text().strip())

        def _badge(name: str, ok: bool) -> str:
            return f"● {name}: OK" if ok else f"○ {name}: —"

        self.lbl_keys_overview.setText(
            f"{_badge('Groq', groq_ok)}   ·   {_badge('DeepSeek', ds_ok)}   ·   {_badge('Gemini Vision', gem_ok)}\n"
            f"{_badge('Fish Audio', fish_ok)}   ·   {_badge('ElevenLabs', el_ok)}"
        )

    def _start_api_key_check(
        self,
        service: str,
        api_key: str,
        btn: QPushButton,
        status_lbl: QLabel,
        base_url: str = "",
    ):
        prev = self._key_check_workers.get(service)
        if prev is not None and prev.isRunning():
            return
        allow_keyless_local = service == "llm" and is_local_llm_endpoint(self._get_selected_provider_id(), base_url)
        if not (api_key or "").strip() and not allow_keyless_local:
            status_lbl.setText("✕ Сначала введи API-ключ")
            status_lbl.setStyleSheet(
                f"color: {COLORS.coral}; font-family: {TYPE.mono}; font-size: 11px; border: none;"
            )
            return
        btn.setEnabled(False)
        status_lbl.setText("⏳ Проверка сервера..." if allow_keyless_local and not (api_key or "").strip() else "⏳ Проверка ключа...")
        status_lbl.setStyleSheet(
            f"color: {COLORS.amber}; font-family: {TYPE.mono}; font-size: 11px; border: none;"
        )
        worker = APIKeyCheckWorker(service=service, api_key=api_key, base_url=base_url, parent=self)
        self._key_check_workers[service] = worker
        _track_qthread(worker)
        worker.result_ready.connect(
            lambda svc, ok, msg, b=btn, lbl=status_lbl: self._on_api_key_check_done(svc, ok, msg, b, lbl)
        )
        worker.start()

    def _on_api_key_check_done(
        self,
        service: str,
        ok: bool,
        msg: str,
        btn: QPushButton,
        status_lbl: QLabel,
    ):
        btn.setEnabled(True)
        color = COLORS.mint if ok else COLORS.coral
        prefix = "● " if ok else "✕ "
        status_lbl.setText(f"{prefix}{msg}")
        status_lbl.setStyleSheet(
            f"color: {color}; font-family: {TYPE.mono}; font-size: 11px; border: none;"
        )

    def _on_check_llm_key(self):
        self._start_api_key_check(
            "llm",
            self.edit_key.text().strip(),
            self.btn_check_llm_key,
            self.lbl_llm_check_status,
            base_url=self.edit_base_url.text().strip(),
        )

    def _on_check_fish_key(self):
        self._start_api_key_check(
            "fish",
            self.edit_fish_key.text().strip(),
            self.btn_check_fish_key,
            self.lbl_fish_check_status,
        )

    def _on_check_eleven_key(self):
        self._start_api_key_check(
            "elevenlabs",
            self.edit_eleven_key.text().strip(),
            self.btn_check_eleven_key,
            self.lbl_eleven_check_status,
        )

    def _on_check_gemini_key(self):
        self._start_api_key_check(
            "gemini",
            self.edit_gemini_key.text().strip(),
            self.btn_check_gemini_key,
            self.lbl_gemini_check_status,
        )

    def _stop_key_check_workers(self):
        for w in list(self._key_check_workers.values()):
            try:
                w.result_ready.disconnect()
            except Exception:
                pass
            if w.isRunning():
                w.wait(1000)
        self._key_check_workers.clear()

    def _get_selected_provider_id(self) -> str:
        current_text = self.combo_provider.currentText()
        return self.providers_map.get(current_text, "groq")

    def _get_current_key_for_provider(self, provider_id: str) -> str:
        if (self.config.llm.provider or "").lower() == provider_id and self.config.llm.api_key:
            return self.config.llm.api_key
        env_var = PROVIDER_DEFAULTS.get(provider_id, {}).get("env_key", "")
        return os.getenv(env_var, "")

    def _on_provider_changed(self):
        p_id = self._get_selected_provider_id()
        default_url = PROVIDER_DEFAULTS.get(p_id, {}).get("base_url", "")
        self.edit_base_url.setText(default_url)
        key = self._get_current_key_for_provider(p_id)
        self.edit_key.setText(key)
        self._populate_models_for_provider(p_id)
        if hasattr(self, "lbl_keys_overview"):
            self._refresh_keys_overview()

    def _populate_models_for_provider(self, provider_id: str):
        self.combo_model.clear()
        models_by_provider = {
            "deepseek": ["deepseek-flash", "deepseek-v4-pro", "deepseek-chat", "deepseek-reasoner"],
            "groq": ["openai/gpt-oss-120b", "qwen/qwen3.8-27b", "openai/gpt-oss-20b"],
            "sambanova": ["Meta-Llama-3.3-70B-Instruct", "gpt-oss-120b", "MiniMax-M3.1-Flash", "gemma-4-31B-it"],
            "gemini": ["gemini-2.5-flash", "gemini-2.5-pro"],
            "openrouter": ["deepseek/deepseek-chat", "openai/gpt-4o-mini", "anthropic/claude-3.5-sonnet"],
            "openai": ["gpt-4o-mini", "gpt-4o"],
            "ollama": ["llama3.2", "qwen2.5:7b", "deepseek-r1:8b", "mistral"],
            "lmstudio": ["local-model"],
        }
        models = models_by_provider.get(provider_id, ["openai/gpt-oss-120b"])
        for m in models:
            self.combo_model.addItem(m)

        curr_m = self.config.llm.model
        if curr_m:
            idx = self.combo_model.findText(curr_m)
            if idx >= 0:
                self.combo_model.setCurrentIndex(idx)
            elif self.combo_model.count() > 0:
                self.combo_model.setCurrentIndex(0)

    def _toggle_key_visibility(self):
        self._toggle_secret_field(self.edit_key, self.btn_show_key)

    def _paste_clipboard_key(self):
        self._paste_into_field(self.edit_key)

    def _populate_input_devices(self):
        self.combo_input_device.clear()
        current_dev = (getattr(self.config.voice, "input_device", "") or "").strip()
        if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
            devices = [("", "Системный микрофон по умолчанию (Auto)")]
        else:
            from ...voice.pipewire import list_input_devices
            devices = list_input_devices()
        for key, label in devices:
            self.combo_input_device.addItem(label, key)
        if current_dev and self.combo_input_device.findData(current_dev) < 0:
            self.combo_input_device.addItem(current_dev, current_dev)
        idx = self.combo_input_device.findData(current_dev)
        self.combo_input_device.setCurrentIndex(idx if idx >= 0 else 0)

    def _refresh_aec_status_ui(self):
        if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
            self.lbl_aec_status.setText("PipeWire AEC: готов к настройке")
            self.btn_toggle_aec.setText("Включить AEC")
            return
        from ...voice.pipewire import get_pipewire_status
        st = get_pipewire_status()
        if not st.available:
            self.lbl_aec_status.setText("PipeWire / pactl не найден")
            self.btn_toggle_aec.setEnabled(False)
            return
        self.btn_toggle_aec.setEnabled(True)
        if st.aec_active:
            self.lbl_aec_status.setText(f"AEC активен: {st.aec_source_name}")
            self.lbl_aec_status.setStyleSheet(
                f"color: {COLORS.mint}; font-family: {TYPE.mono}; font-size: 11px; border: none;"
            )
            self.btn_toggle_aec.setText("Выключить AEC")
        else:
            self.lbl_aec_status.setText(f"{st.server_name}: AEC выключен")
            self.lbl_aec_status.setStyleSheet(
                f"color: {COLORS.text_muted}; font-family: {TYPE.mono}; font-size: 11px; border: none;"
            )
            self.btn_toggle_aec.setText("Включить AEC")

    def _on_toggle_aec_clicked(self):
        from ...voice.pipewire import disable_pipewire_aec, enable_pipewire_aec, get_pipewire_status
        st = get_pipewire_status()
        if st.aec_active:
            ok, msg = disable_pipewire_aec()
            if ok:
                self.chk_pipewire_aec.setChecked(False)
        else:
            ok, msg = enable_pipewire_aec()
            if ok:
                self.chk_pipewire_aec.setChecked(True)
        if ok:
            self._refresh_aec_status_ui()
            self._populate_input_devices()
        else:
            self.lbl_aec_status.setText(msg)
            self.lbl_aec_status.setStyleSheet(
                f"color: {COLORS.amber}; font-family: {TYPE.mono}; font-size: 11px; border: none;"
            )

    def _on_calibrate_mic_clicked(self):
        if self._calib_worker and self._calib_worker.isRunning():
            self._stop_calibration_worker()
            self.btn_calibrate_mic.setText("Калибровать микрофон (3 сек)")
            self.lbl_calibration_status.setText("Калибровка остановлена.")
            return
        dev_key = self.combo_input_device.currentData() or ""
        self.btn_calibrate_mic.setText("Стоп...")
        self.bar_mic_level.setValue(0)
        worker = MicCalibrationWorker(input_device=str(dev_key), parent=self)
        worker.level_changed.connect(self.bar_mic_level.setValue)
        worker.phase_changed.connect(self.lbl_calibration_status.setText)
        worker.calibration_ready.connect(self._on_calibration_finished)
        worker.failed.connect(self._on_calibration_failed)
        self._calib_worker = worker
        worker.start()

    def _on_calibration_finished(self, result):
        self.btn_calibrate_mic.setText("Калибровать микрофон (3 сек)")
        val_int = max(15, min(200, int(round(result.recommended_threshold * 1000))))
        self.slider_barge_threshold.setValue(val_int)
        self.lbl_calibration_status.setText(
            f"Готово: фон {result.noise_rms:.3f} · голос {result.speech_rms:.3f} → порог {result.recommended_threshold:.3f}"
        )

    def _on_calibration_failed(self, err: str):
        self.btn_calibrate_mic.setText("Калибровать микрофон (3 сек)")
        self.bar_mic_level.setValue(0)
        self.lbl_calibration_status.setText(err)

    def _stop_calibration_worker(self):
        w = getattr(self, "_calib_worker", None)
        if w is not None:
            w.stop()
            if w.isRunning():
                w.wait(900)
            self._calib_worker = None

    def reject(self):
        self._stop_calibration_worker()
        self._stop_key_check_workers()
        super().reject()

    def closeEvent(self, event):
        self._stop_calibration_worker()
        self._stop_key_check_workers()
        super().closeEvent(event)

    def _apply_and_save(self):
        self._stop_calibration_worker()
        self._stop_key_check_workers()
        p_id = self._get_selected_provider_id()
        key = self.edit_key.text().strip()
        model = self.combo_model.currentText().strip()
        base_url = self.edit_base_url.text().strip()

        fish_key = self.edit_fish_key.text().strip()
        fish_voice = self.edit_fish_voice_id.text().strip()
        eleven_key = self.edit_eleven_key.text().strip()
        eleven_voice = self.edit_eleven_voice_id.text().strip()
        gemini_key = self.edit_gemini_key.text().strip()

        prev_provider = (self._initial_provider or "").strip().lower()
        prev_base_url = (self._initial_base_url or "").strip()
        env_var = PROVIDER_DEFAULTS.get(p_id, {}).get("env_key", f"{p_id.upper()}_API_KEY")

        self.config.llm.provider = p_id
        if key:
            self._cleared_env_keys.discard(env_var)
            self.config.llm.api_key = key
        elif env_var in self._cleared_env_keys or p_id != prev_provider or base_url != prev_base_url:
            # Never carry a previous provider's API key over to a newly selected provider or changed/cleared Base URL,
            # and honor explicit key deletion. When Base URL changes/clears on the same provider with an empty key,
            # also purge the provider's env key so `load_config()` does not resurrect it for the new endpoint.
            self.config.llm.api_key = ""
            if p_id == prev_provider and base_url != prev_base_url:
                self._cleared_env_keys.add(env_var)
        self.config.llm.model = model
        self.config.llm.base_url = base_url
        self.config.companion.user_name = self.edit_user_name.text().strip() or "User"
        self.config.safety.tool_policy = self.combo_policy.currentData() or "ask"

        # Voice / TTS / STT
        self.config.voice.tts_enabled = self.chk_tts_enabled.isChecked()
        self.config.voice.tts_engine = str(self.combo_tts_engine.currentData() or "fish")
        self.config.voice.stt_mode = str(self.combo_stt_mode.currentData() or "groq")
        self.config.voice.wake_word_enabled = self.chk_wake_word_enabled.isChecked()
        self.config.voice.hands_free_barge_in = self.chk_hands_free_barge_in.isChecked()
        self.config.voice.pipewire_aec_enabled = self.chk_pipewire_aec.isChecked()
        self.config.voice.input_device = str(self.combo_input_device.currentData() or "").strip()
        self.config.voice.barge_in_rms_threshold = round(self.slider_barge_threshold.value() / 1000.0, 3)
        if fish_key:
            self._cleared_env_keys.discard("FISH_AUDIO_API_KEY")
            self.config.voice.fish_audio_api_key = fish_key
        elif "FISH_AUDIO_API_KEY" in self._cleared_env_keys:
            self.config.voice.fish_audio_api_key = ""
        self.config.voice.fish_audio_voice_id = fish_voice
        if eleven_key:
            self._cleared_env_keys.discard("ELEVENLABS_API_KEY")
            self.config.voice.elevenlabs_api_key = eleven_key
        elif "ELEVENLABS_API_KEY" in self._cleared_env_keys:
            self.config.voice.elevenlabs_api_key = ""
        self.config.voice.elevenlabs_voice_id = eleven_voice

        # Vision / Screen Analysis & Gemini key sync
        self.config.vision.enabled = self.chk_vision_enabled.isChecked()
        self.config.vision.provider = str(self.combo_vision_provider.currentData() or "gemini")
        self.config.vision.model = self.combo_vision_model.currentText().strip() or "gemini-3.1-flash-lite"
        if gemini_key:
            self._cleared_env_keys.discard("GEMINI_API_KEY")
            self.config.vision.gemini_api_key = gemini_key
            self.config.voice.gemini_api_key = gemini_key
        elif "GEMINI_API_KEY" in self._cleared_env_keys:
            self.config.vision.gemini_api_key = ""
            self.config.voice.gemini_api_key = ""

        # UI
        self.config.ui.language = str(self.combo_ui_lang.currentData() or "ru")
        self.config.ui.avatar_enabled = self.chk_avatar_enabled.isChecked()
        self.config.ui.auto_avatar_poses = self.chk_auto_avatar_poses.isChecked()
        self.config.ui.game_mode_enabled = self.chk_game_mode.isChecked()
        self.config.ui.reduced_motion = self.chk_reduced_motion.isChecked()
        self.config.ui.minimize_to_tray = self.chk_minimize_to_tray.isChecked()

        cyberdeck_hook("save_config", _save_config)(self.config)

        for del_var in sorted(self._cleared_env_keys):
            cyberdeck_hook("delete_env_value", _delete_env_value)(del_var)
        if key:
            cyberdeck_hook("update_env_value", _update_env_value)(env_var, key)
        if fish_key:
            cyberdeck_hook("update_env_value", _update_env_value)("FISH_AUDIO_API_KEY", fish_key)
        if fish_voice:
            cyberdeck_hook("update_env_value", _update_env_value)("FISH_AUDIO_VOICE_ID", fish_voice)
        if eleven_key:
            cyberdeck_hook("update_env_value", _update_env_value)("ELEVENLABS_API_KEY", eleven_key)
        if eleven_voice:
            cyberdeck_hook("update_env_value", _update_env_value)("ELEVENLABS_VOICE_ID", eleven_voice)
        if gemini_key:
            cyberdeck_hook("update_env_value", _update_env_value)("GEMINI_API_KEY", gemini_key)
        cyberdeck_hook("update_env_value", _update_env_value)("LLM_PROVIDER", p_id)
        cyberdeck_hook("update_env_value", _update_env_value)("LLM_MODEL", model)

        self.accept()
