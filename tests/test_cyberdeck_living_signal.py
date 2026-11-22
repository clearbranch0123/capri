"""
CAPRI // LIVING SIGNAL v2 UI & Motion Regression Tests.
Covers the LIVING SIGNAL v2 UI & motion specification:
- Shared theme tokens & HiDPI vector SVG icons
- Auto-growing multiline ComposerTextEdit (Enter vs Shift+Enter, QLineEdit API compatibility)
- State-driven AudioWaveVisualizer (zero idle timer, hidden in idle, reduced-motion handling)
- WelcomeCanvas empty-chat composition & live suggestion actions
- ChatMessageCard grouped tool chips, local CodeBlockWidget rendering, thought disclosure,
  and suppression of synthetic [INITIATIVE // ...] user bubbles on reload
- MotionController & persisted config.ui.reduced_motion preference
- Secondary dialogs & CapriRelicDashboard cohesion
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication, QPlainTextEdit

from capri.core.config import AppConfig
from capri.core.sessions import SessionManager
from capri.ui.cyberdeck import (
    CapriCyberdeckWindow,
    CapriMemoryWorkbenchDialog,
    CapriProviderSettingsDialog,
    CapriResetDialog,
    ChatMessageCard,
)
from capri.ui.dashboard import CapriRelicDashboard
from capri.ui.icons import get_icon, render_svg_pixmap
from capri.ui.motion import MotionController
from capri.ui.theme import COLORS, METRICS, TYPE
from capri.ui.widgets import AudioWaveVisualizer, CodeBlockWidget, ComposerTextEdit


@pytest.fixture(autouse=True)
def qapp():
    return QApplication.instance() or QApplication([])


def test_theme_tokens_and_svg_icons():
    assert COLORS.canvas == "#0A0B0F"
    assert COLORS.crimson == "#E12D53"
    assert COLORS.signal_cyan == "#4ED8E8"
    assert COLORS.amber == "#E8AD52"
    assert METRICS.column_max_width >= 800
    assert "Inter" in TYPE.sans
    assert "JetBrains" in TYPE.mono

    for name in ("sidebar", "plus", "search", "sessions", "memory", "eye", "mic", "send", "stop", "chat", "shield", "capri-signal"):
        pix = render_svg_pixmap(name, COLORS.crimson, 16)
        assert not pix.isNull()
        assert pix.width() > 0
        icon = get_icon(name, COLORS.text_secondary, 16)
        assert not icon.isNull()


def test_composer_multiline_enter_and_compatibility():
    editor = ComposerTextEdit()
    editor.resize(400, 40)
    editor.show()
    QApplication.processEvents()

    sent = []
    editor.returnPressed.connect(lambda: sent.append(editor.text()))

    editor.setText("Первая строка")
    assert editor.text() == "Первая строка"
    initial_h = editor.height()

    # Shift+Enter inserts a newline and does NOT send
    shift_enter = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier, "\n")
    editor.keyPressEvent(shift_enter)
    QApplication.processEvents()
    assert len(sent) == 0
    assert "\n" in editor.text()
    assert editor.height() > initial_h

    # Plain Enter emits returnPressed
    plain_enter = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier, "\r")
    editor.keyPressEvent(plain_enter)
    assert len(sent) == 1
    assert "Первая строка" in sent[0]

    editor.clear()
    assert editor.text() == ""
    editor.close()


def test_audio_visualizer_idle_timer_and_reduced_motion():
    from PySide6.QtWidgets import QVBoxLayout, QWidget

    host = QWidget()
    layout = QVBoxLayout(host)
    viz = AudioWaveVisualizer(parent=host, reduced_motion=False)
    layout.addWidget(viz)
    host.show()
    QApplication.processEvents()

    # Hidden and timer stopped by default in idle
    assert viz.state == "idle"
    assert viz.isHidden() is True
    assert viz.timer.isActive() is False

    # Active state shows visualizer and starts timer
    viz.set_state("listening")
    QApplication.processEvents()
    assert viz.isHidden() is False
    assert viz.isVisible() is True
    assert viz.timer.isActive() is True

    # Reduced motion stops the timer while keeping the state indicator visible
    viz.set_reduced_motion(True)
    QApplication.processEvents()
    assert viz.isHidden() is False
    assert viz.timer.isActive() is False

    # Back to idle hides widget and keeps timer stopped
    viz.set_state("idle")
    QApplication.processEvents()
    assert viz.isHidden() is True
    assert viz.timer.isActive() is False
    host.close()


def test_chat_card_grouped_tools_code_blocks_and_thought_disclosure():
    md_text = (
        "Вот пример кода:\n\n"
        "```python\n"
        "def very_long_function_name_for_local_horizontal_scroll_check(alpha, beta, gamma, delta):\n"
        "    return alpha + beta + gamma + delta\n"
        "```\n\n"
        "Готово, D."
    )
    card = ChatMessageCard(
        "Капри",
        md_text,
        thought="Проверяю компактное отображение мыслей и кода",
        tools_info=["Запуск: run_bash", "Запуск: run_bash", "Запуск: run_bash", "Запуск: run_bash"],
        turn_id="turn-99",
        sequence=1,
    )
    card.show()
    QApplication.processEvents()

    # Full raw text compatibility preserved
    assert card.lbl_text.text() == md_text

    # Dedicated CodeBlockWidget exists with NoWrap horizontal scroll
    code_blocks = card.findChildren(CodeBlockWidget)
    assert len(code_blocks) == 1
    assert code_blocks[0].editor.lineWrapMode() == QPlainTextEdit.LineWrapMode.NoWrap

    # 4 identical tool calls grouped into a single "run_bash ×4" pill
    assert len(card.tools_info) == 4
    tool_buttons = [card.tb_layout.itemAt(i).widget() for i in range(card.tb_layout.count()) if card.tb_layout.itemAt(i).widget()]
    assert len(tool_buttons) == 1
    assert "run_bash ×4" in tool_buttons[0].text()

    # Expanding tool details works
    assert card.tools_detail_box.isHidden() is True
    tool_buttons[0].click()
    QApplication.processEvents()
    assert card.tools_detail_box.isHidden() is False

    # Thought disclosure starts collapsed and expands on toggle
    assert card.lbl_thought.isHidden() is True
    card.btn_thought_toggle.setChecked(True)
    QApplication.processEvents()
    assert card.lbl_thought.isHidden() is False
    card.close()


def test_welcome_canvas_and_proactive_bubble_suppression(tmp_path):
    db_file = tmp_path / "test_welcome_proactive.db"
    sm = SessionManager(db_path=db_file)
    empty_sess = sm.create_session("Пустой чат")
    pro_sess = sm.create_session("Чат с инициативой")
    sm.add_turn(
        pro_sess.id,
        user_text="[INITIATIVE // idle_checkin]",
        assistant_text="Эй, D, ты на связи?",
        thought="[INITIATIVE // IDLE_CHECKIN]",
        status="proactive",
    )

    cfg = AppConfig()
    cfg.voice.wake_word_enabled = False
    cfg.ui.avatar_enabled = False

    with patch("capri.ui.cyberdeck.notify_avatar", return_value=True):
        deck = CapriCyberdeckWindow(config=cfg)
        deck.agent.session_manager = sm
        deck.show()
        QApplication.processEvents()

        # Empty session shows WelcomeCanvas
        deck._load_session_into_ui(empty_sess.id)
        QApplication.processEvents()
        assert deck.welcome_canvas.isHidden() is False
        assert len(deck.welcome_canvas.suggestion_buttons) == 4

        # Triggering a real suggestion hides WelcomeCanvas and outputs system/assistant card
        deck._on_welcome_suggestion("context")
        QApplication.processEvents()
        assert deck.welcome_canvas.isHidden() is True

        # Reloading proactive session does NOT render a fake D user bubble for [INITIATIVE // ...]
        deck._load_session_into_ui(pro_sess.id)
        QApplication.processEvents()
        cards = [
            deck.chat_layout.itemAt(i).widget()
            for i in range(deck.chat_layout.count())
            if isinstance(deck.chat_layout.itemAt(i).widget(), ChatMessageCard)
        ]
        assert len(cards) == 1
        assert cards[0].is_assistant is True
        assert cards[0].is_proactive is True
        assert "ты на связи" in cards[0].lbl_text.text()
        deck.close()


def test_reduced_motion_controller_and_settings_persistence(tmp_path):
    ctrl = MotionController(reduced_motion=False)
    assert ctrl.reduced_motion is False
    ctrl.set_reduced_motion(True)
    assert ctrl.reduced_motion is True
    assert ctrl.is_instant is True

    cfg = AppConfig()
    cfg.ui.reduced_motion = False
    dlg = CapriProviderSettingsDialog(cfg)
    assert dlg.chk_reduced_motion.isChecked() is False
    dlg.chk_reduced_motion.setChecked(True)

    with patch("capri.ui.cyberdeck.save_config") as mock_save, \
         patch("capri.ui.cyberdeck.update_env_value"):
        dlg._apply_and_save()
        assert cfg.ui.reduced_motion is True
        mock_save.assert_called_once()
    dlg.close()


def test_secondary_dialogs_living_signal_consistency(tmp_path):
    cfg = AppConfig()
    cfg.voice.wake_word_enabled = False
    cfg.ui.avatar_enabled = False

    with patch("capri.ui.cyberdeck.notify_avatar", return_value=True):
        deck = CapriCyberdeckWindow(config=cfg)

        wb = CapriMemoryWorkbenchDialog(deck)
        assert wb.table.verticalHeader().isVisible() is False
        wb.close()

        reset_dlg = CapriResetDialog(deck)
        reset_dlg.close()

        dash = CapriRelicDashboard()
        assert dash.bar_sync is not None
        dash.close()

        deck.close()


def test_beta_readiness_welcome_balance_mode_sync_and_proactive_history(tmp_path):
    db_file = tmp_path / "test_beta_readiness.db"
    sm = SessionManager(db_path=db_file)
    s_new = sm.create_session("Новый диалог")

    # 1. Proactive turn must NOT overwrite default session title with [INITIATIVE // ...]
    sm.add_turn(
        s_new.id,
        user_text="[INITIATIVE // idle_checkin]",
        assistant_text="Ты тут, D?",
        thought="[INITIATIVE // IDLE_CHECKIN]",
        status="proactive",
    )
    assert sm.get_session(s_new.id).title == "Новый диалог"
    hist = sm.get_model_history(s_new.id)
    assert all("[INITIATIVE //" not in m.get("content", "") for m in hist)

    # 2. Provider settings dialog resolves provider default base_url when mismatched
    cfg = AppConfig()
    cfg.llm.provider = "deepseek"
    cfg.llm.model = "deepseek-flash"
    cfg.voice.wake_word_enabled = False
    cfg.ui.avatar_enabled = False
    dlg = CapriProviderSettingsDialog(cfg)
    assert "api.deepseek.com" in dlg.edit_base_url.text()
    dlg.close()

    # 3. WelcomeCanvas vertical balance at 1600x900 & mode cycle prompt sync
    s_empty = sm.create_session("Чистый старт")
    with patch("capri.ui.cyberdeck.notify_avatar", return_value=True), \
         patch("capri.ui.cyberdeck.save_config") as mock_save:
        deck = CapriCyberdeckWindow(config=cfg)
        deck.agent.session_manager = sm
        deck.show()
        deck.resize(1600, 900)
        deck._load_session_into_ui(s_empty.id)
        QApplication.processEvents()

        top_margin = deck.welcome_canvas._outer_layout.contentsMargins().top()
        assert top_margin >= 120
        for btn in deck.welcome_canvas.suggestion_buttons:
            assert btn.isVisible() is True
            assert btn.height() >= 54

        # Cycling mode updates agent.system_prompt and persists config
        deck._cycle_mode()  # normal -> focus
        assert deck.config.companion.mode == "focus"
        assert "РЕЖИМ [FOCUS]" in deck.agent.system_prompt
        mock_save.assert_called()

        # Bare AltGr must NOT trigger voice shortcut (preserves 3rd-level layout symbols),
        # whereas Ctrl+M triggers voiceShortcutPressed cleanly.
        voice_triggers = []
        with patch.object(deck, "_toggle_voice_input", side_effect=lambda: voice_triggers.append(True)):
            deck.input_field.voiceShortcutPressed.disconnect()
            deck.input_field.voiceShortcutPressed.connect(deck._toggle_voice_input)
            altgr_ev = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_AltGr, Qt.KeyboardModifier.NoModifier)
            deck.input_field.keyPressEvent(altgr_ev)
            assert len(voice_triggers) == 0

            ctrl_m_ev = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_M, Qt.KeyboardModifier.ControlModifier)
            deck.input_field.keyPressEvent(ctrl_m_ev)
            assert len(voice_triggers) == 1

        deck.close()


def test_close_window_during_active_stt_transcription_waits_for_thread_finish(tmp_path):
    """Closing Cyberdeck while STT transcription is in-flight cancels the async STT task and waits for QThread finish."""
    import asyncio
    import threading
    import numpy as np
    from capri.voice.stt import SpeechRecognizer

    stt_entered = threading.Event()
    stt_cancelled = threading.Event()

    def fake_record_audio(self, max_duration=6.0, silence_timeout=1.3, stop_event=None):
        return np.ones(1600, dtype=np.float32) * 0.25

    async def slow_transcribe(self, audio):
        stt_entered.set()
        try:
            await asyncio.sleep(8.0)
            return "Распознанный текст после закрытия"
        except asyncio.CancelledError:
            stt_cancelled.set()
            raise

    cfg = AppConfig()
    cfg.voice.wake_word_enabled = False
    cfg.ui.avatar_enabled = False

    with patch("capri.ui.cyberdeck.notify_avatar", return_value=True), \
         patch.object(SpeechRecognizer, "record_audio", fake_record_audio), \
         patch.object(SpeechRecognizer, "transcribe_audio_array", slow_transcribe):
        deck = CapriCyberdeckWindow(config=cfg)
        deck.show()
        QApplication.processEvents()

        late_texts = []
        deck._toggle_voice_input()
        vt = deck.voice_thread
        vt.text_ready.connect(lambda t: late_texts.append(t))

        # Wait until worker thread is genuinely inside transcribe_audio_array
        assert stt_entered.wait(timeout=2.0) is True
        assert vt.isRunning() is True

        # Close window while 8s STT transcription is actively in-flight
        deck.close()

        # In-flight async STT task must be cancelled and QThread must have finished before closeEvent returns
        from capri.ui.cyberdeck import _ACTIVE_QTHREADS, _track_qthread
        assert stt_cancelled.is_set() is True
        assert vt.isRunning() is False
        assert vt.isFinished() is True
        assert vt not in _ACTIVE_QTHREADS
        # Calling _track_qthread on an already-finished thread must not re-add it to _ACTIVE_QTHREADS
        _track_qthread(vt)
        assert vt not in _ACTIVE_QTHREADS
        assert len(late_texts) == 0


def test_live_tool_results_stay_in_detail_drawer_and_never_overflow_chat_width(tmp_path):
    """
    Regression test for v2.9.0 live UI overflow bug:
    Multi-line tool_result payloads (get_system_status, list_windows) must NEVER render as
    giant QPushButton pills or push chat_container wider than the viewport at 1020px or 480px.
    """
    db_file = tmp_path / "test_tool_overflow.db"
    sm = SessionManager(db_path=db_file)
    sess = sm.create_session("Проверка инструментов")

    sys_status_out = (
        "CPU: 14% | RAM: 55% (8.8/16.0 GB)\n"
        "GPU: NVIDIA GeForce RTX 3050 | Нагрузка: 22% | Температура: 53°C\n"
        "Uptime: 6 hours, 42 minutes"
    )
    list_win_out = (
        '1. "CAPRI // LIVING SIGNAL v2.9.0" (WS 1)\n'
        '2. "Spotify Free" (WS 2)\n'
        '3. "TrueSlash" (WS 11)\n'
        '4. "Обсудить улучшения проекта | capri" (WS 10)\n'
        '5. "capri-avatar" (WS 16) [FLOATING]\n'
        '6. "популярные 3 дистрибутивы - Google Search — Mozilla Firefox" (WS 5)'
    )
    prose = (
        "Живая, тёплая — 53 градуса на чипе, вентиляторы даже не вспотели. "
        "RAM под 55%, VRAM почти пустая, аптайм шесть сорок два.\n\n"
        "А вот у тебя в песочнице разброс: Spotify на втором воркспейсе, TrueSlash с каким-то слэшем, "
        "в kitty висит «Обсудить улучшения проекта | capri», а в Firefox — популярные дистры."
    )

    cfg = AppConfig()
    cfg.voice.wake_word_enabled = False
    cfg.ui.avatar_enabled = False

    with patch("capri.ui.cyberdeck.notify_avatar", return_value=True):
        deck = CapriCyberdeckWindow(config=cfg)
        deck.agent.session_manager = sm
        deck.agent.current_session_id = sess.id
        deck.show()
        deck.resize(1020, 760)
        QApplication.processEvents()

        # Simulate live tool_start + multi-line tool_result signals on active response card
        card = deck._append_message("Капри", prose, thought="Проверяю статус системы и окна", animate=False)
        deck.current_response_card = card
        deck.current_tools_log = []

        deck._on_tool("Запуск: get_system_status", "start")
        deck._on_tool(sys_status_out, "result")
        deck._on_tool("Запуск: list_windows", "start")
        deck._on_tool(list_win_out, "result")
        QApplication.processEvents()

        # Strictly 2 clean single-line tool pills — NEVER raw multi-line stdout pills
        pills = [
            card.tb_layout.itemAt(i).widget()
            for i in range(card.tb_layout.count())
            if card.tb_layout.itemAt(i) and card.tb_layout.itemAt(i).widget()
        ]
        assert len(pills) == 2
        pill_texts = [p.text() for p in pills]
        assert pill_texts == ["get_system_status", "list_windows"]
        assert all("\n" not in t for t in pill_texts)
        assert deck.current_tools_log == ["Запуск: get_system_status", "Запуск: list_windows"]

        # Full results live strictly inside the collapsible tools_detail_box
        assert card.tools_detail_box.isHidden() is True
        detail_txt = card.tools_detail_box.text()
        assert "get_system_status" in detail_txt
        assert "RTX 3050" in detail_txt
        assert "Spotify Free" in detail_txt

        # Verify zero horizontal overflow at medium (1020px with docked sidebar) and compact (480px)
        for win_w, win_h in ((1020, 760), (480, 640)):
            deck.resize(win_w, win_h)
            QApplication.processEvents()
            vp_w = deck.scroll_area.viewport().width()
            assert deck.chat_container.width() <= vp_w
            assert card.width() <= deck.chat_container.width()
            assert card.lbl_text.width() <= card.bubble.width()
            assert deck.composer_capsule.width() <= deck.workspace_panel.width()

        # Expanding tools_detail_box at 480px must also NEVER cause horizontal overflow
        pills[0].click()
        QApplication.processEvents()
        assert card.tools_detail_box.isHidden() is False
        assert deck.chat_container.width() <= deck.scroll_area.viewport().width()

        # Reloading from SQLite must render identical compact pills + expandable result log
        sm.add_turn(
            sess.id,
            user_text="Капри, как дела?",
            assistant_text=prose,
            thought="Проверка при загрузке из БД",
            tools=[
                {"name": "get_system_status", "args": "{}", "result": sys_status_out},
                {"name": "list_windows", "args": "{}", "result": list_win_out},
            ],
        )
        deck._load_session_into_ui(sess.id)
        QApplication.processEvents()
        cards = [
            deck.chat_layout.itemAt(i).widget()
            for i in range(deck.chat_layout.count())
            if isinstance(deck.chat_layout.itemAt(i).widget(), ChatMessageCard)
        ]
        asst_cards = [c for c in cards if c.is_assistant]
        assert len(asst_cards) == 1
        reloaded_pills = [
            asst_cards[0].tb_layout.itemAt(i).widget().text()
            for i in range(asst_cards[0].tb_layout.count())
            if asst_cards[0].tb_layout.itemAt(i) and asst_cards[0].tb_layout.itemAt(i).widget()
        ]
        assert reloaded_pills == ["get_system_status", "list_windows"]
        assert "RTX 3050" in asst_cards[0].tools_detail_box.text()
        assert "Spotify Free" in asst_cards[0].tools_detail_box.text()

        deck.close()


def test_dark_combobox_popups_and_cleaned_avatar_menu_and_relic_dashboard():
    """
    Regression test for UI polish hotfix:
    - All QComboBox popups in Settings / Setup / Memory dialogs enforce dark LIVING SIGNAL
      background + high-contrast text (preventing white-on-white unreadable dropdowns).
    - Relic Dashboard no longer exposes the obsolete 'Терминал Capri' launcher button.
    - Avatar context menu is sleek LIVING SIGNAL-styled and purged of obsolete/debug items
      ('Открыть терминал', 'Подойти к окну', 'HUD-радар', manual debug states).
    """
    from PySide6.QtGui import QPalette
    from PySide6.QtWidgets import QComboBox, QPushButton
    from capri.ui.overlay import CapriAvatarWidget

    cfg = AppConfig()
    cfg.voice.wake_word_enabled = False
    cfg.ui.avatar_enabled = False

    dlg = CapriProviderSettingsDialog(cfg)
    combos = dlg.findChildren(QComboBox)
    assert len(combos) >= 4
    for cb in combos:
        pal = cb.view().palette()
        base_col = pal.color(QPalette.ColorRole.Base)
        text_col = pal.color(QPalette.ColorRole.Text)
        assert base_col.lightness() < 60, f"Expected dark popup background for {cb}, got {base_col.name()}"
        assert text_col.lightness() > 200, f"Expected light popup text for {cb}, got {text_col.name()}"
        assert "background-color:" in cb.view().styleSheet()
    dlg.close()

    dash = CapriRelicDashboard()
    from capri.ui.widgets import SegmentedEngramBar
    assert isinstance(dash.bar_sync, SegmentedEngramBar)
    btn_texts = [b.text() for b in dash.findChildren(QPushButton)]
    assert not any("терминал" in t.lower() for t in btn_texts)
    assert not any("тест голоса" in t.lower() for t in btn_texts)
    assert any("normal" in t.lower() for t in btn_texts)
    assert any("focus" in t.lower() for t in btn_texts)
    assert any("playful" in t.lower() for t in btn_texts)
    assert any("quiet" in t.lower() for t in btn_texts)
    dash.close()

    avatar = CapriAvatarWidget(mode="2d")
    menu, _actions = avatar._build_context_menu()
    top_labels = [a.text() for a in menu.actions() if not a.isSeparator()]
    joined_low = " | ".join(top_labels).lower()
    for removed in ("терминал", "подойти к окну", "hud-радар", "состояние:", "глитч", "зафиксировать тут"):
        assert removed not in joined_low, f"Obsolete menu item '{removed}' should be removed: {top_labels}"
    assert any("relic hud" in lbl.lower() for lbl in top_labels)
    assert any("позы" in lbl.lower() for lbl in top_labels)
    assert any("авто-позы" in lbl.lower() for lbl in top_labels)
    assert any("game mode" in lbl.lower() for lbl in top_labels)
    assert any("закрыть аватар" in lbl.lower() for lbl in top_labels)
    assert "#ff007f" not in menu.styleSheet().lower()
    avatar.close()


def test_v31_unified_crimson_graphite_shared_widgets_and_real_telemetry(tmp_path):
    """
    Verifies Capri 3.1 Unified UI Redesign:
    - Cyberdeck and Relic Dashboard both share `SegmentedEngramBar` and `CapriSignalMark`.
    - Live telemetry labels reflect real SessionManager / MemoryStore / ToolRegistry state (no fake values).
    - Relic Dashboard removes 'Тест голоса' & 'Терминал Capri' while keeping real mode cycling & runtime status.
    - Keyboard shortcuts Ctrl+N and Ctrl+K are registered on Cyberdeck.
    """
    from capri.ui.widgets import CapriSignalMark, SegmentedEngramBar, UserMonogramBadge

    bar = SegmentedEngramBar(segments=5)
    bar.setValue(85)
    assert bar.value() == 85
    bar.setValue(150)
    assert bar.value() == 100

    badge = UserMonogramBadge("D", size=20)
    assert badge.width() == 20

    # Single-letter user nickname keeps only the monogram badge (no duplicate "(D) D")
    from PySide6.QtWidgets import QLabel
    card_single = ChatMessageCard("D", "Привет")
    assert not any(lbl.text() == "D" for lbl in card_single.findChildren(QLabel))
    card_multi = ChatMessageCard("Alex", "Привет")
    assert any(lbl.text() == "Alex" for lbl in card_multi.findChildren(QLabel))
    card_single.deleteLater()
    card_multi.deleteLater()

    db_file = tmp_path / "test_v31_telemetry.db"
    sm = SessionManager(db_path=db_file)
    s1 = sm.create_session("Тест 3.1")
    sm.add_turn(s1.id, user_text="Привет", assistant_text="На связи!")

    cfg = AppConfig()
    cfg.voice.wake_word_enabled = False
    cfg.ui.avatar_enabled = False

    with patch("capri.ui.cyberdeck.notify_avatar", return_value=True):
        deck = CapriCyberdeckWindow(config=cfg)
        deck.agent.session_manager = sm
        deck.agent.current_session_id = s1.id
        deck.refresh_stats()

        assert isinstance(deck.bar_engram, SegmentedEngramBar)
        assert isinstance(deck.brand_mark, CapriSignalMark)
        assert deck.lbl_metric_context.text() == "1"
        assert "фактов" in deck.lbl_metric_memory.text()
        assert "активны" in deck.lbl_metric_tools.text()
        assert hasattr(deck, "_shortcut_new_chat")
        assert hasattr(deck, "_shortcut_search")

        # Verify fluid assistant card expansion & full-width text wrapping on 1920x1040 fullscreen
        deck._load_session_into_ui(s1.id)
        deck.resize(1920, 1040)
        deck.show()
        QApplication.processEvents()
        QApplication.processEvents()
        asst_cards = [
            deck.chat_layout.itemAt(i).widget()
            for i in range(deck.chat_layout.count())
            if isinstance(deck.chat_layout.itemAt(i).widget(), ChatMessageCard)
            and deck.chat_layout.itemAt(i).widget().is_assistant
        ]
        assert asst_cards
        c = asst_cards[0]
        assert c.bubble.width() >= 1100, f"Assistant card too narrow on 1920px: {c.bubble.width()}"
        assert abs(c.lbl_text.document().textWidth() - c.lbl_text.viewport().width()) <= 4.0
        deck.close()

    dash = CapriRelicDashboard()
    assert isinstance(dash.bar_sync, SegmentedEngramBar)
    assert isinstance(dash.brand_mark, CapriSignalMark)
    assert "фактов" in dash.lbl_memory_count.text()
    assert "активны" in dash.lbl_tools_count.text()
    assert "·" in dash.lbl_runtime_badge.text()
    dash.close()
