import os
from unittest.mock import MagicMock, patch
import pytest
from PySide6.QtWidgets import QApplication
from capri.core.config import AppConfig
from capri.ui.cyberdeck import CapriChangelogDialog, CapriCyberdeckWindow


def test_changelog_dialog_init():
    app = QApplication.instance() or QApplication([])
    dlg = CapriChangelogDialog()
    assert dlg.browser is not None
    assert "2.1.1" in dlg.browser.toPlainText() or "CAPRI" in dlg.browser.toPlainText()
    dlg.close()


def test_cyberdeck_avatar_lifecycle():
    app = QApplication.instance() or QApplication([])
    cfg = AppConfig()
    cfg.voice.wake_word_enabled = False
    cfg.ui.avatar_enabled = True

    with patch("capri.ui.cyberdeck.notify_avatar", return_value=False), \
         patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_popen.return_value = mock_proc
        deck = CapriCyberdeckWindow(config=cfg)
        assert deck.avatar_proc == mock_proc
        mock_popen.assert_called_once()
        deck.close()
        mock_proc.terminate.assert_called_once()


def test_chat_message_card_dynamic_updates():
    app = QApplication.instance() or QApplication([])
    from capri.ui.cyberdeck import ChatMessageCard
    card = ChatMessageCard("Капри", "Генерация...", thought="Мысль", tools_info=["tool1"])
    assert card.lbl_text.text() == "Генерация..."
    assert card.thought == "Мысль"

    card.set_thought("Новая мысль")
    assert "Новая мысль" in card.lbl_thought.text()

    card.add_tool_info("tool2: done")
    assert len(card.tools_info) == 2

    card.set_text("Финальный ответ Капри")
    assert card.lbl_text.text() == "Финальный ответ Капри"


def test_chat_message_card_action_buttons():
    app = QApplication.instance() or QApplication([])
    from capri.ui.cyberdeck import ChatMessageCard

    # 1. Карточка пользователя D
    user_card = ChatMessageCard("D", "Привет", turn_id="turn-123", sequence=5)
    assert not user_card.btn_edit.isHidden()
    assert not user_card.btn_delete.isHidden()

    edit_signal_received = []
    delete_signal_received = []
    user_card.edit_requested.connect(lambda tid: edit_signal_received.append(tid))
    user_card.delete_requested.connect(lambda tid: delete_signal_received.append(tid))

    user_card.btn_edit.click()
    assert edit_signal_received == ["turn-123"]

    user_card.btn_delete.click()
    assert delete_signal_received == ["turn-123"]

    # 2. Карточка Капри
    asst_card = ChatMessageCard("Капри", "Привет, D!", turn_id="turn-123", sequence=5)
    assert asst_card.btn_edit.isHidden()  # Только D может редактировать свой запрос
    assert not asst_card.btn_delete.isHidden()

    # 3. Карточка без turn_id (живая генерация) -> затем динамическое добавление метаданных
    live_card = ChatMessageCard("D", "Жду...")
    assert live_card.btn_edit.isHidden()
    assert live_card.btn_delete.isHidden()

    live_card.set_turn_metadata("turn-456", 6)
    assert not live_card.btn_edit.isHidden()
    assert not live_card.btn_delete.isHidden()



def test_agent_worker_qthread_lifecycle():
    app = QApplication.instance() or QApplication([])
    from capri.ui.cyberdeck import AgentWorker
    from capri.core.agent import CapriAgent

    cfg = AppConfig()
    agent = CapriAgent(cfg)
    worker = AgentWorker(agent, "тест")
    assert not worker.isRunning()


def test_cyberdeck_visualizer_voice_stop_idle():
    app = QApplication.instance() or QApplication([])
    cfg = AppConfig()
    cfg.voice.wake_word_enabled = False
    cfg.ui.avatar_enabled = False

    with patch("capri.ui.cyberdeck.notify_avatar", return_value=True):
        deck = CapriCyberdeckWindow(config=cfg)
        deck.visualizer.set_state("listening")
        assert deck.visualizer.state == "listening"

        deck._stop_voice_input()
        assert deck.visualizer.state in ("idle", "thinking")

        deck._reset_mic_button()
        assert deck.visualizer.state == "idle"

        deck._cancel_voice_input()
        assert deck.visualizer.state == "idle"
        deck.close()


def test_provider_settings_dialog(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    from capri.ui.cyberdeck import CapriProviderSettingsDialog
    cfg = AppConfig()
    cfg.llm.provider = "groq"
    cfg.llm.api_key = "test-groq-key"
    cfg.llm.model = "openai/gpt-oss-120b"

    dlg = CapriProviderSettingsDialog(cfg)
    assert "Groq" in dlg.combo_provider.currentText()
    assert dlg.edit_key.text() == "test-groq-key"

    # Switch provider to DeepSeek
    for i in range(dlg.combo_provider.count()):
        if "DeepSeek" in dlg.combo_provider.itemText(i):
            dlg.combo_provider.setCurrentIndex(i)
            break

    assert dlg._get_selected_provider_id() == "deepseek"
    assert "api.deepseek.com" in dlg.edit_base_url.text()
    assert dlg.combo_model.currentText() == "deepseek-flash"

    # Switch provider to SambaNova
    for i in range(dlg.combo_provider.count()):
        if "SambaNova" in dlg.combo_provider.itemText(i):
            dlg.combo_provider.setCurrentIndex(i)
            break

    assert dlg._get_selected_provider_id() == "sambanova"
    assert "sambanova.ai" in dlg.edit_base_url.text()
    assert dlg.combo_model.count() > 0

    dlg.edit_key.setText("samba-test-key-123")

    with patch("capri.ui.cyberdeck.save_config") as mock_save, \
         patch("capri.ui.cyberdeck.update_env_value") as mock_env:
        dlg._apply_and_save()
        assert cfg.llm.provider == "sambanova"
        assert cfg.llm.api_key == "samba-test-key-123"
        assert "sambanova.ai" in cfg.llm.base_url
        mock_save.assert_called_once()
        mock_env.assert_called()
    dlg.close()


def test_sessions_dialog_and_restoration(tmp_path):
    app = QApplication.instance() or QApplication([])
    from capri.core.sessions import SessionManager
    from capri.ui.cyberdeck import CapriSessionsDialog

    db_file = tmp_path / "test_sessions.db"
    sm = SessionManager(db_path=db_file)
    s1 = sm.create_session("Первый тестовый диалог")
    sm.add_turn(
        s1.id,
        user_text="Привет, Капри!",
        assistant_text="Здравствуй, D! Системы в норме.",
        thought="Инициализация связи",
        tools=[{"name": "media_control", "args": {"action": "status"}}],
    )
    s2 = sm.create_session("Второй диалог про код")
    sm.add_turn(
        s2.id,
        user_text="Покажи статус Niri",
        assistant_text="Вот активные окна...",
        thought="Проверка окон",
    )

    cfg = AppConfig()
    cfg.voice.wake_word_enabled = False
    cfg.ui.avatar_enabled = False

    with patch("capri.ui.cyberdeck.notify_avatar", return_value=True):
        deck = CapriCyberdeckWindow(config=cfg)
        deck.agent.session_manager = sm
        deck.agent.current_session_id = s2.id

        assert hasattr(deck, "btn_sessions")
        assert hasattr(deck, "btn_new_session")
        assert "сессии" in deck.btn_sessions.text().lower()
        assert not deck.btn_sessions.icon().isNull()
        assert "новый" in deck.btn_new_session.text().lower()
        assert not deck.btn_new_session.icon().isNull()

        # Проверка диалога сессий
        dlg = CapriSessionsDialog(deck)
        assert dlg.list_sessions.count() >= 2
        assert dlg.btn_resume.isEnabled()

        # Поиск / фильтрация
        dlg.edit_search.setText("Первый")
        assert dlg.list_sessions.count() == 1
        dlg.edit_search.setText("")
        assert dlg.list_sessions.count() >= 2

        # Восстановление первой сессии в UI
        deck._load_session_into_ui(s1.id)
        assert deck.agent.current_session_id == s1.id

        # Проверка, что карточки сообщений отрисовались в chat_layout
        card_texts = [
            item.widget().lbl_text.text()
            for i in range(deck.chat_layout.count())
            if (item := deck.chat_layout.itemAt(i)) and item.widget() and hasattr(item.widget(), "lbl_text")
        ]
        assert any("Привет, Капри!" in t for t in card_texts)
        assert any("Здравствуй, D!" in t for t in card_texts)

        # Проверка /new
        deck._handle_command("/new Третья сессия")
        assert deck.agent.current_session_id != s1.id
        assert deck.agent.session_manager.get_session(deck.agent.current_session_id).title == "Третья сессия"

        # Проверка /backup
        deck._handle_command("/backup")

        dlg.close()
        deck.close()


def test_cyberdeck_turn_edit_and_delete_actions(tmp_path):
    app = QApplication.instance() or QApplication([])
    from capri.core.sessions import SessionManager
    from capri.ui.cyberdeck import ChatMessageCard
    from PySide6.QtWidgets import QMessageBox

    db_file = tmp_path / "test_turn_actions.db"
    sm = SessionManager(db_path=db_file)
    sess = sm.create_session("Тест редактирования и удаления")
    t1 = sm.add_turn(
        sess.id,
        user_text="Первый вопрос D",
        assistant_text="Первый ответ Капри",
    )
    t2 = sm.add_turn(
        sess.id,
        user_text="Второй вопрос D",
        assistant_text="Второй ответ Капри",
    )

    cfg = AppConfig()
    cfg.voice.wake_word_enabled = False
    cfg.ui.avatar_enabled = False

    with patch("capri.ui.cyberdeck.notify_avatar", return_value=True):
        deck = CapriCyberdeckWindow(config=cfg)
        deck.agent.session_manager = sm
        deck.agent.current_session_id = sess.id

        # 1. Загрузка сессии в UI
        deck._load_session_into_ui(sess.id)

        # 2. Проверка живого прикрепления метаданных (_attach_turn_metadata_to_live_cards)
        user_card = ChatMessageCard("D", "Новый живой запрос")
        resp_card = ChatMessageCard("Капри", "Новый живой ответ")
        deck.current_user_card = user_card
        deck.current_response_card = resp_card
        deck.chat_layout.insertWidget(deck.chat_layout.count() - 1, user_card)
        deck.chat_layout.insertWidget(deck.chat_layout.count() - 1, resp_card)

        # Добавим turn 3 в базу и вызовем привязку метаданных
        t3 = sm.add_turn(sess.id, user_text="Новый живой запрос", assistant_text="Новый живой ответ")
        deck._attach_turn_metadata_to_live_cards()

        assert user_card.turn_id == t3.id
        assert user_card.sequence == t3.sequence
        assert not user_card.btn_edit.isHidden()
        assert not user_card.btn_delete.isHidden()
        assert resp_card.turn_id == t3.id
        assert not resp_card.btn_delete.isHidden()

        # 3. Клик на кнопку редактирования (Edit) для раунда t2
        deck._on_card_edit_clicked(t2.id)

        # Проверяем откат в SQLite
        remaining_turns = sm.get_turns(sess.id)
        assert len(remaining_turns) == 1
        assert remaining_turns[0].id == t1.id

        # Проверяем, что в поле ввода подставился текст раунда t2
        assert deck.input_field.text() == "Второй вопрос D"

        # 4. Проверка удаления раунда (Delete) с подтверждением
        with patch("PySide6.QtWidgets.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
            deck._on_card_delete_clicked(t1.id)

        remaining_after_delete = sm.get_turns(sess.id)
        assert len(remaining_after_delete) == 0

        deck.close()


def test_settings_dialog_tts_stt_vision_and_api_key_checks(tmp_path):
    app = QApplication.instance() or QApplication([])
    from capri.ui.cyberdeck import CapriProviderSettingsDialog, check_api_key_sync

    cfg = AppConfig()
    cfg.llm.provider = "groq"
    cfg.llm.api_key = "gsk-init"
    cfg.voice.tts_engine = "fish"
    cfg.voice.stt_mode = "groq"
    cfg.vision.provider = "gemini"
    cfg.vision.model = "gemini-3.1-flash-lite"

    dlg = CapriProviderSettingsDialog(cfg)

    # All 4 practical tabs exist (LLM, Voice, Vision/API, Interface/Safety)
    assert dlg.tabs.count() == 4

    # Switch TTS engine -> edge hides Fish panel, switching back -> fish shows Fish panel
    idx_edge = dlg.combo_tts_engine.findData("edge")
    dlg.combo_tts_engine.setCurrentIndex(idx_edge)
    assert dlg.fish_panel.isHidden() is True
    assert dlg.eleven_panel.isHidden() is True

    idx_fish = dlg.combo_tts_engine.findData("fish")
    dlg.combo_tts_engine.setCurrentIndex(idx_fish)
    assert dlg.fish_panel.isHidden() is False

    # Configure Fish Audio, STT local, Wake-Word off, Vision Gemini key & model
    dlg.edit_fish_key.setText("fish_ui_key_998877")
    dlg.edit_fish_voice_id.setText("voice_ref_capri_01")
    dlg.combo_stt_mode.setCurrentIndex(dlg.combo_stt_mode.findData("local"))
    dlg.chk_wake_word_enabled.setChecked(False)
    dlg.chk_vision_enabled.setChecked(True)
    dlg.combo_vision_provider.setCurrentIndex(dlg.combo_vision_provider.findData("gemini"))
    dlg.combo_vision_model.setCurrentText("gemini-2.5-flash")
    dlg.edit_gemini_key.setText("AIzaFakeTestGeminiKey987654321")

    env_calls = []
    with patch("capri.ui.cyberdeck.save_config") as mock_save, \
         patch("capri.ui.cyberdeck.update_env_value", side_effect=lambda k, v: env_calls.append((k, v))):
        dlg._apply_and_save()
        mock_save.assert_called_once()

    assert cfg.voice.tts_engine == "fish"
    assert cfg.voice.stt_mode == "local"
    assert cfg.voice.wake_word_enabled is False
    assert cfg.voice.fish_audio_api_key == "fish_ui_key_998877"
    assert cfg.voice.fish_audio_voice_id == "voice_ref_capri_01"
    assert cfg.vision.enabled is True
    assert cfg.vision.provider == "gemini"
    assert cfg.vision.model == "gemini-2.5-flash"
    assert cfg.vision.gemini_api_key == "AIzaFakeTestGeminiKey987654321"
    assert cfg.voice.gemini_api_key == "AIzaFakeTestGeminiKey987654321"

    env_dict = dict(env_calls)
    assert env_dict["FISH_AUDIO_API_KEY"] == "fish_ui_key_998877"
    assert env_dict["FISH_AUDIO_VOICE_ID"] == "voice_ref_capri_01"
    assert env_dict["GEMINI_API_KEY"] == "AIzaFakeTestGeminiKey987654321"
    dlg.close()

    # Live API check helper: handles 200 OK, 401 reject, and redacts raw keys on network errors
    ok_resp = MagicMock(status_code=200)
    bad_resp = MagicMock(status_code=401)
    with patch("httpx.Client.get", return_value=ok_resp):
        ok, msg = check_api_key_sync("fish", "fish_secret_123456")
        assert ok is True and "200" in msg
        ok_g, msg_g = check_api_key_sync("gemini", "AIzaFakeSecretKey1234567890")
        assert ok_g is True and "200" in msg_g

    with patch("httpx.Client.get", return_value=bad_resp):
        ok_f, msg_f = check_api_key_sync("llm", "gsk_wrong_123456", base_url="https://api.groq.com/openai/v1")
        assert ok_f is False and "401" in msg_f

    with patch("httpx.Client.get", side_effect=RuntimeError("boom https://api.test?key=AIzaLeakMe999888777666")):
        ok_e, msg_e = check_api_key_sync("gemini", "AIzaLeakMe999888777666")
        assert ok_e is False
        assert "AIzaLeakMe999888777666" not in msg_e


def test_settings_provider_switch_clears_stale_key_and_explicit_delete_removes_env(tmp_path, monkeypatch):
    """
    Regression test for provider-switching credential isolation:
    1. Switching LLM provider (e.g. Groq -> DeepSeek) when the new provider has no key
       MUST clear `config.llm.api_key` so the old Groq key is NEVER sent to DeepSeek / new Base URL.
    2. Clicking the clear/delete key button MUST remove the stored key from both in-memory config
       and `.env` + `os.environ` via `delete_env_value`.
    """
    app = QApplication.instance() or QApplication([])
    from capri.core.config import delete_env_value, update_env_value
    from capri.ui.cyberdeck import CapriProviderSettingsDialog

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    # 1. Switch Groq (with key) -> DeepSeek (empty key): stale Groq key MUST be cleared
    cfg = AppConfig()
    cfg.llm.provider = "groq"
    cfg.llm.api_key = "gsk_old_groq_key_must_not_leak"
    cfg.llm.base_url = "https://api.groq.com/openai/v1"

    dlg = CapriProviderSettingsDialog(cfg)
    for i in range(dlg.combo_provider.count()):
        if "DeepSeek" in dlg.combo_provider.itemText(i):
            dlg.combo_provider.setCurrentIndex(i)
            break
    assert dlg.edit_key.text() == ""

    with patch("capri.ui.cyberdeck.save_config"), patch("capri.ui.cyberdeck.update_env_value"):
        dlg._apply_and_save()

    assert cfg.llm.provider == "deepseek"
    assert cfg.llm.api_key == "", "Previous provider API key must never carry over on provider switch!"
    dlg.close()

    # 1b. Same provider, clearing Base URL (non-empty -> empty) with empty key MUST clear
    #     stale API key in memory AND purge it from `.env` + `os.environ` so next load_config() won't resurrect it
    url_env = tmp_path / "url_clear.env"
    update_env_value("OPENAI_API_KEY", "sk_custom_endpoint_secret_999", env_path=url_env)
    assert "OPENAI_API_KEY=sk_custom_endpoint_secret_999" in url_env.read_text(encoding="utf-8")

    cfg_url = AppConfig()
    cfg_url.llm.provider = "openai"
    cfg_url.llm.api_key = "sk_custom_endpoint_secret_999"
    cfg_url.llm.base_url = "https://custom.llm.example/v1"

    dlg_url = CapriProviderSettingsDialog(cfg_url)
    dlg_url.edit_key.setText("")
    dlg_url.edit_base_url.setText("")
    with patch("capri.ui.cyberdeck.save_config"), \
         patch("capri.ui.cyberdeck.update_env_value", side_effect=lambda k, v: update_env_value(k, v, env_path=url_env)), \
         patch("capri.ui.cyberdeck.delete_env_value", side_effect=lambda k: delete_env_value(k, env_path=url_env)):
        dlg_url._apply_and_save()

    assert cfg_url.llm.provider == "openai"
    assert cfg_url.llm.base_url == ""
    assert cfg_url.llm.api_key == "", "Clearing Base URL must clear stale API key in memory!"
    assert "OPENAI_API_KEY" not in url_env.read_text(encoding="utf-8"), "Clearing Base URL must purge key from .env!"
    assert "OPENAI_API_KEY" not in os.environ, "Clearing Base URL must purge key from os.environ!"
    dlg_url.close()

    # 2. Explicit delete button removes key from config AND real isolated `.env` + `os.environ`
    test_env = tmp_path / "isolated_del.env"
    update_env_value("FISH_AUDIO_API_KEY", "fish_old_secret_12345", env_path=test_env)
    update_env_value("GEMINI_API_KEY", "AIzaOldGeminiKey1234567890", env_path=test_env)
    assert "FISH_AUDIO_API_KEY=fish_old_secret_12345" in test_env.read_text(encoding="utf-8")

    cfg2 = AppConfig()
    cfg2.llm.provider = "groq"
    cfg2.llm.api_key = "gsk_delete_me_12345"
    cfg2.voice.fish_audio_api_key = "fish_old_secret_12345"
    cfg2.vision.gemini_api_key = "AIzaOldGeminiKey1234567890"
    cfg2.voice.gemini_api_key = "AIzaOldGeminiKey1234567890"

    dlg2 = CapriProviderSettingsDialog(cfg2)
    dlg2.btn_clear_key.click()
    dlg2.btn_clear_fish_key.click()
    dlg2.btn_clear_gemini_key.click()

    with patch("capri.ui.cyberdeck.save_config"), \
         patch("capri.ui.cyberdeck.update_env_value", side_effect=lambda k, v: update_env_value(k, v, env_path=test_env)), \
         patch("capri.ui.cyberdeck.delete_env_value", side_effect=lambda k: delete_env_value(k, env_path=test_env)):
        dlg2._apply_and_save()

    assert cfg2.llm.api_key == ""
    assert cfg2.voice.fish_audio_api_key == ""
    assert cfg2.vision.gemini_api_key == ""
    assert cfg2.voice.gemini_api_key == ""
    env_content = test_env.read_text(encoding="utf-8")
    assert "FISH_AUDIO_API_KEY" not in env_content
    assert "GEMINI_API_KEY" not in env_content
    assert "FISH_AUDIO_API_KEY" not in os.environ
    assert "GEMINI_API_KEY" not in os.environ
    dlg2.close()

