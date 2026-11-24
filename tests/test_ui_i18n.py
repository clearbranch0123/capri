import os
from unittest.mock import patch

from PySide6.QtWidgets import QApplication

from capri.core.config import AppConfig
from capri.core.emotions import EmotionEngine
from capri.ui.avatar_popover import CapriAvatarQuickPopover
from capri.ui.dashboard import CapriRelicDashboard
from capri.ui.i18n import format_active_tools, format_facts, format_rounds, resolve_ui_lang, tr_ui
from capri.ui.quick_ask import CapriQuickAskBar


def _ensure_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


def test_i18n_defaults_to_ru_and_honors_en():
    cfg = AppConfig()
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("CAPRI_UI_LANG", None)
        assert resolve_ui_lang(cfg) == "ru"
        assert tr_ui("Память", "Memory", cfg) == "Память"
        assert format_rounds(1, config=cfg) == "1 раунд"
        assert format_facts(2, 1, config=cfg) == "2 фактов (+1)"

        cfg.ui.language = "en"
        assert resolve_ui_lang(cfg) == "en"
        assert tr_ui("Память", "Memory", cfg) == "Memory"
        assert format_rounds(1, config=cfg) == "1 turn"
        assert format_rounds(3, config=cfg) == "3 turns"
        assert format_facts(2, 1, config=cfg) == "2 facts (+1)"
        assert format_active_tools(47, config=cfg) == "47 active"


def test_widgets_render_english_when_ui_lang_en(tmp_path):
    _ensure_app()
    emo = EmotionEngine(file_path=tmp_path / "emotion.json")
    emo.trust = 48
    assert emo.get_relationship_stage_label("en") == "Trusted Partner ⚡"
    assert emo.get_relationship_stage_label("ru") == "Проверенный напарник ⚡"

    cfg = AppConfig()
    cfg.ui.language = "en"
    with (
        patch.dict(os.environ, {"CAPRI_UI_LANG": "en"}),
        patch("capri.ui.dashboard.load_config", return_value=cfg),
        patch("capri.ui.dashboard.EmotionEngine", return_value=emo),
    ):
        pop = CapriAvatarQuickPopover(None)
        assert pop.btn_quick_ask.text() == "⚡ Quick Ask"
        assert pop.pose_buttons["perch"].text() == "Perch"
        pop.close()

        qa = CapriQuickAskBar(None)
        assert "Ask Capri" in qa.input_edit.placeholderText()
        assert qa.btn_copy.text() == "Copy"
        qa.close()

        relic = CapriRelicDashboard(None)
        assert relic.lbl_stage_line.text() == "Trusted Partner ⚡"
        assert "Online," in relic.lbl_quote.text()
        relic.close()


def test_cyberdeck_lang_button_and_settings_dialogs(tmp_path):
    _ensure_app()
    from capri.core.memory import MemoryStore
    from capri.core.sessions import SessionManager
    from capri.ui.cyberdeck import CapriCyberdeckWindow, CapriFirstRunWizard, CapriProviderSettingsDialog

    cfg = AppConfig()
    cfg.ui.language = "ru"
    cfg.voice.wake_word_enabled = False
    cfg.ui.avatar_enabled = False

    with (
        patch.dict(os.environ, {}, clear=False),
        patch("capri.ui.cyberdeck.notify_avatar", return_value=True),
        patch("capri.ui.cyberdeck.send_avatar_control", return_value=True),
    ):
        os.environ.pop("CAPRI_UI_LANG", None)
        deck = CapriCyberdeckWindow(config=cfg)
        deck.agent.session_manager = SessionManager(db_path=tmp_path / "s.db")
        deck.agent.memory_store = MemoryStore(file_path=tmp_path / "m.json")
        assert deck.btn_lang_toggle.text() == "RU"
        assert deck.btn_new_session.text() == "Новый чат"

        # 1-click sidebar dock toggle switches RU -> EN live
        deck.btn_lang_toggle.click()
        assert deck.config.ui.language == "en"
        assert deck.btn_lang_toggle.text() == "EN"
        assert deck.btn_new_session.text() == "New Chat"

        # 1-click back EN -> RU
        deck.btn_lang_toggle.click()
        assert deck.config.ui.language == "ru"
        assert deck.btn_lang_toggle.text() == "RU"
        assert deck.btn_new_session.text() == "Новый чат"

        # Provider Settings dialog & First-Run Wizard expose combo_ui_lang
        dlg_settings = CapriProviderSettingsDialog(cfg, deck)
        assert dlg_settings.combo_ui_lang.findData("en") >= 0
        dlg_settings.close()

        dlg_wizard = CapriFirstRunWizard(cfg, deck)
        assert dlg_wizard.combo_ui_lang.findData("en") >= 0
        dlg_wizard.close()
        deck.close()

        pop = CapriAvatarQuickPopover(None)
        pop.set_language("en")
        assert pop.btn_quick_ask.text() == "⚡ Quick Ask"
        pop.set_language("ru")
        assert pop.btn_quick_ask.text() == "⚡ Быстрый вопрос"
        pop.close()

