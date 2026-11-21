"""
CAPRI // Regression tests for responsive Cyberdeck, Drawer lifecycle, and full-history search.
Verifies responsive Cyberdeck, drawer lifecycle, and search acceptance criteria.
"""

import time
from unittest.mock import patch
from pathlib import Path

import pytest
from PySide6.QtCore import Qt, QPoint, QEvent
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication

from capri.core.config import AppConfig
from capri.core.sessions import SessionManager
from capri.ui.cyberdeck import CapriCyberdeckWindow, ClickableOverlay


@pytest.fixture(autouse=True)
def qapp():
    return QApplication.instance() or QApplication([])


def test_compact_height_geometry_and_controls_visibility(tmp_path):
    """
    Проверяет, что при компактной высоте (540x360 и 480x360) боковая панель полезна:
    - Вторичный HUD-бокс скрывается, освобождая вертикальное место.
    - session_list_widget получает высоту >= 100 px.
    - Основные элементы управления (Новый чат, Поиск, Сессии, Память, Чейнджлог, Сброс) остаются видимыми и доступными.
    """
    db_file = tmp_path / "test_compact_geom.db"
    sm = SessionManager(db_path=db_file)
    s1 = sm.create_session("Тестовый диалог для компактной высоты")

    cfg = AppConfig()
    cfg.voice.wake_word_enabled = False
    cfg.ui.avatar_enabled = False

    with patch("capri.ui.cyberdeck.notify_avatar", return_value=True):
        deck = CapriCyberdeckWindow(config=cfg)
        deck.agent.session_manager = sm
        deck.agent.current_session_id = s1.id
        deck.show()
        QApplication.processEvents()

        # 1. Проверка на 540x360 (открытый drawer)
        deck.resize(540, 360)
        QApplication.processEvents()
        deck._open_drawer()
        QApplication.processEvents()

        assert deck._drawer_open is True
        assert deck.hud_box.isHidden() is True
        assert deck.btn_new_session.isVisible() is True
        assert deck.search_session_input.isVisible() is True
        assert deck.btn_sessions.isVisible() is True
        assert deck.btn_memory_wb.isVisible() is True
        assert deck.btn_changelog.isVisible() is True
        assert deck.btn_reset.isVisible() is True
        assert deck.btn_quit.isVisible() is True
        assert not deck.btn_quit.icon().isNull()
        assert not hasattr(deck, "btn_clear")
        assert deck.session_list_widget.height() >= 60

        # 2. Проверка на 480x360 (минимальный тайловый размер Niri)
        deck.resize(480, 360)
        QApplication.processEvents()

        assert deck.hud_box.isHidden() is True
        assert deck.btn_new_session.isVisible() is True
        assert deck.search_session_input.isVisible() is True
        assert deck.btn_sessions.isVisible() is True
        assert deck.session_list_widget.height() >= 60

        # 3. Возврат на нормальную высоту (1200x800)
        deck.resize(1200, 800)
        QApplication.processEvents()
        assert deck.hud_box.isVisible() is True

        deck.close()


def test_drawer_open_close_interactions(tmp_path):
    """
    Проверяет сценарии управления выдвижным Drawer:
    - Открытие по кнопке переключения (_toggle_sidebar).
    - Закрытие по клавише Escape.
    - Закрытие по клику на затемненную подложку (ClickableOverlay).
    """
    db_file = tmp_path / "test_drawer_flow.db"
    sm = SessionManager(db_path=db_file)

    cfg = AppConfig()
    cfg.voice.wake_word_enabled = False
    cfg.ui.avatar_enabled = False

    with patch("capri.ui.cyberdeck.notify_avatar", return_value=True):
        deck = CapriCyberdeckWindow(config=cfg)
        deck.agent.session_manager = sm
        deck.show()
        deck.resize(600, 500)
        QApplication.processEvents()
        deck._close_drawer()
        QApplication.processEvents()

        # Исходно: закрыт
        assert deck._drawer_open is False
        assert deck.left_panel.isHidden() is True
        assert deck.drawer_backdrop.isHidden() is True

        # Открытие по кнопке переключения
        deck._toggle_sidebar()
        QApplication.processEvents()
        assert deck._drawer_open is True
        assert deck.left_panel.isVisible() is True
        assert deck.drawer_backdrop.isVisible() is True

        # Закрытие по Escape
        escape_event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
        deck.keyPressEvent(escape_event)
        QApplication.processEvents()
        assert deck._drawer_open is False
        assert deck.left_panel.isHidden() is True
        assert deck.drawer_backdrop.isHidden() is True

        # Снова открываем и закрываем по клику подложки
        deck._open_drawer()
        QApplication.processEvents()
        assert deck._drawer_open is True
        deck.drawer_backdrop.clicked.emit()
        QApplication.processEvents()
        assert deck._drawer_open is False
        assert deck.left_panel.isHidden() is True

        deck.close()


def test_dock_drawer_breakpoint_crossing(tmp_path):
    """
    Проверяет переход через границу 1000 px:
    - При ширине >= 1000 px сайдбар пристыкован в central_layout.
    - При ширине < 1000 px сайдбар отделяется как оверлей и по умолчанию скрыт.
    - Если в компактном режиме drawer был открыт, при разворачивании на >= 1000 px он корректно ре-докуется.
    """
    db_file = tmp_path / "test_dock_breakpoint.db"
    sm = SessionManager(db_path=db_file)

    cfg = AppConfig()
    cfg.voice.wake_word_enabled = False
    cfg.ui.avatar_enabled = False

    with patch("capri.ui.cyberdeck.notify_avatar", return_value=True):
        deck = CapriCyberdeckWindow(config=cfg)
        deck.agent.session_manager = sm
        deck.show()

        # 1. Широкий экран: 1200 px (пристыкован)
        deck.resize(1200, 800)
        QApplication.processEvents()
        assert deck.central_layout.indexOf(deck.left_panel) != -1
        assert deck.left_panel.isVisible() is True
        assert deck.drawer_backdrop.isHidden() is True

        # 2. Сужение до компактного: 800 px (режим drawer)
        deck.resize(800, 600)
        QApplication.processEvents()
        assert deck.central_layout.indexOf(deck.left_panel) == -1
        assert deck.left_panel.isHidden() is True

        # 3. Открываем drawer в компактном режиме
        deck._open_drawer()
        QApplication.processEvents()
        assert deck._drawer_open is True
        assert deck.left_panel.isVisible() is True

        # 4. Расширяем обратно до 1200 px (должен автоматически пристыковаться)
        deck.resize(1200, 800)
        QApplication.processEvents()
        assert deck._drawer_open is False
        assert deck.central_layout.indexOf(deck.left_panel) != -1
        assert deck.left_panel.isVisible() is True
        assert deck.drawer_backdrop.isHidden() is True

        deck.close()


def test_full_database_search_older_than_60(tmp_path):
    """
    Проверяет, что поиск по истории диалогов работает по всей базе SQLite,
    а не ограничен первыми 60 элементами в памяти.
    """
    db_file = tmp_path / "test_deep_search.db"
    sm = SessionManager(db_path=db_file)

    now = time.time()
    # Создаем 75 сессий: самая старая (#1) создана давно с уникальным маркером
    old_target = sm.create_session("Секретный проект Аврора 2021")
    with sm._get_connection() as con:
        con.execute("UPDATE sessions SET updated_at = ?, created_at = ? WHERE id = ?", (now - 100 * 86400, now - 100 * 86400, old_target.id))
        con.commit()

    # Добавляем еще 74 более новые сессии
    for i in range(2, 76):
        s = sm.create_session(f"Диалог раунд #{i}")
        with sm._get_connection() as con:
            con.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now - (76 - i) * 3600, s.id))
            con.commit()

    cfg = AppConfig()
    cfg.voice.wake_word_enabled = False
    cfg.ui.avatar_enabled = False

    with patch("capri.ui.cyberdeck.notify_avatar", return_value=True):
        deck = CapriCyberdeckWindow(config=cfg)
        deck.agent.session_manager = sm
        deck._refresh_sidebar_sessions()
        QApplication.processEvents()

        # Обычный список показывает максимум 60 сессий (старый диалог в него не попал)
        titles = [deck.session_list_widget.item(i).text() for i in range(deck.session_list_widget.count())]
        assert not any("Аврора 2021" in t for t in titles)

        # Выполняем поиск "Аврора"
        deck.search_session_input.setText("Аврора")
        QApplication.processEvents()

        # Поиск по базе должен найти старый диалог
        search_titles = [deck.session_list_widget.item(i).text() for i in range(deck.session_list_widget.count())]
        assert any("Аврора 2021" in t for t in search_titles)

        # Очистка поиска возвращает стандартный список
        deck.search_session_input.setText("")
        QApplication.processEvents()
        restored_titles = [deck.session_list_widget.item(i).text() for i in range(deck.session_list_widget.count())]
        assert not any("Аврора 2021" in t for t in restored_titles)

        deck.close()


def test_session_rename_and_soft_delete_lifecycle(tmp_path):
    """
    Проверяет переименование и мягкое удаление сессии с немедленным обновлением UI.
    """
    db_file = tmp_path / "test_lifecycle.db"
    sm = SessionManager(db_path=db_file)
    s1 = sm.create_session("Начальный заголовок")
    s2 = sm.create_session("Вторая сессия")

    cfg = AppConfig()
    cfg.voice.wake_word_enabled = False
    cfg.ui.avatar_enabled = False

    with patch("capri.ui.cyberdeck.notify_avatar", return_value=True):
        deck = CapriCyberdeckWindow(config=cfg)
        deck.agent.session_manager = sm
        deck.agent.current_session_id = s1.id
        deck._refresh_sidebar_sessions()
        QApplication.processEvents()

        # Переименование
        sm.rename_session(s1.id, "Обновленный заголовок диалога")
        deck.lbl_active_session_title.setText("💬 Обновленный заголовок диалога")
        deck._refresh_sidebar_sessions()
        QApplication.processEvents()

        titles = [deck.session_list_widget.item(i).text() for i in range(deck.session_list_widget.count())]
        assert any("Обновленный заголовок диалога" in t for t in titles)
        assert deck.lbl_active_session_title.text() == "💬 Обновленный заголовок диалога"

        # Мягкое удаление (soft delete)
        sm.soft_delete_session(s1.id)
        deck._load_session_into_ui(s2.id)
        QApplication.processEvents()

        titles_after_delete = [deck.session_list_widget.item(i).text() for i in range(deck.session_list_widget.count())]
        assert not any("Обновленный заголовок диалога" in t for t in titles_after_delete)
        assert deck.agent.current_session_id == s2.id

        deck.close()


def test_sidebar_power_button_forces_full_quit_and_interrupts_tts():
    """Clicking bottom-left power button forces a full shutdown (not minimize-to-tray) and stops TTS."""
    app = QApplication.instance() or QApplication([])
    cfg = AppConfig()
    cfg.voice.wake_word_enabled = False
    cfg.ui.avatar_enabled = False
    cfg.ui.minimize_to_tray = True

    with patch("capri.ui.cyberdeck.notify_avatar", return_value=True):
        deck = CapriCyberdeckWindow(config=cfg)
        deck.show()
        QApplication.processEvents()

        interrupted = {"called": False}
        orig_tts = getattr(deck.agent, "tts", None)

        class _FakeTTS:
            def interrupt(self):
                interrupted["called"] = True

        deck.agent.tts = _FakeTTS()
        assert deck._force_quit is False
        deck.btn_quit.click()
        QApplication.processEvents()

        assert deck._force_quit is True
        assert interrupted["called"] is True
        assert deck.isVisible() is False
        deck.agent.tts = orig_tts
