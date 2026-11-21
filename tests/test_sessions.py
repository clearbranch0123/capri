import sqlite3
import pytest
from capri.core.sessions import SessionManager, CURRENT_SCHEMA_VERSION


def test_session_creation_and_active_state(tmp_path):
    db_file = tmp_path / "test_sessions.db"
    sm = SessionManager(db_file)

    assert sm.verify_integrity() is True

    # Создание первой сессии
    sess1 = sm.create_session("Тестовый разговор")
    assert sess1.id is not None
    assert len(sess1.id) == 36  # UUID v4 length
    assert sess1.title == "Тестовый разговор"
    assert sess1.turn_count == 0

    assert sm.get_active_session_id() == sess1.id

    # Создание второй сессии
    sess2 = sm.create_session("Второй разговор")
    assert sm.get_active_session_id() == sess2.id

    # Переключение обратно
    assert sm.set_active_session(sess1.id) is True
    assert sm.get_active_session_id() == sess1.id


def test_turns_and_keyset_pagination(tmp_path):
    db_file = tmp_path / "test_sessions.db"
    sm = SessionManager(db_file)

    sess = sm.create_session("Диалог")

    # Добавляем turns
    t1 = sm.add_turn(
        sess.id,
        user_text="Привет, Капри!",
        assistant_text="Здорово, ковбой.",
        thought="[CHAT | 95%] приветствие",
        tools=[{"name": "list_windows", "result": "kitty"}],
        model_messages=[
            {"role": "user", "content": "Привет, Капри!"},
            {"role": "assistant", "content": "Здорово, ковбой."},
        ],
    )
    assert t1.sequence == 1

    t2 = sm.add_turn(
        sess.id,
        user_text="Сделай музыку громче",
        assistant_text="Готово, навалила звука.",
        thought="[TASK | 99%] громкость",
        tools=[{"name": "set_volume", "result": "ok"}],
        model_messages=[
            {"role": "user", "content": "Сделай музыку громче"},
            {"role": "assistant", "content": "Готово, навалила звука."},
        ],
    )
    assert t2.sequence == 2

    # Проверка получения всех раундов
    turns = sm.get_turns(sess.id)
    assert len(turns) == 2
    assert turns[0].user_text == "Привет, Капри!"
    assert turns[1].assistant_text == "Готово, навалила звука."

    # Проверка пагинации keyset
    page1 = sm.get_turns(sess.id, limit=1)
    assert len(page1) == 1
    assert page1[0].sequence == 1

    page2 = sm.get_turns(sess.id, limit=1, after_sequence=1)
    assert len(page2) == 1
    assert page2[0].sequence == 2

    # Проверка восстановления модели
    model_history = sm.get_model_history(sess.id)
    assert len(model_history) == 4
    assert model_history[0]["content"] == "Привет, Капри!"
    assert model_history[3]["content"] == "Готово, навалила звука."


def test_soft_delete_and_restore(tmp_path):
    db_file = tmp_path / "test_sessions.db"
    sm = SessionManager(db_file)

    s1 = sm.create_session("Сессия 1")
    s2 = sm.create_session("Сессия 2")

    active_list = sm.list_sessions(include_deleted=False)
    assert len(active_list) == 2

    # Мягкое удаление s1
    assert sm.soft_delete_session(s1.id) is True

    active_after = sm.list_sessions(include_deleted=False)
    assert len(active_after) == 1
    assert active_after[0].id == s2.id

    with_deleted = sm.list_sessions(include_deleted=True)
    assert len(with_deleted) == 2

    # Восстановление s1
    assert sm.restore_session(s1.id) is True
    restored = sm.list_sessions(include_deleted=False)
    assert len(restored) == 2


def test_sqlite_online_backup_and_integrity(tmp_path):
    db_file = tmp_path / "original_sessions.db"
    sm = SessionManager(db_file)
    s = sm.create_session("Для бэкапа")
    sm.add_turn(s.id, "Тест", "Ответ")

    backup_file = tmp_path / "backups" / "backup_sessions.db"
    assert sm.backup_to(backup_file) is True

    # Проверяем созданный файл бэкапа через новый SessionManager
    backup_sm = SessionManager(backup_file)
    assert backup_sm.verify_integrity() is True
    b_turns = backup_sm.get_turns(s.id)
    assert len(b_turns) == 1
    assert b_turns[0].user_text == "Тест"


def test_get_recent_turns_pagination(tmp_path):
    db_file = tmp_path / "recent_test.db"
    sm = SessionManager(db_file)
    s = sm.create_session("Пагинация хвоста")

    for i in range(1, 11):
        sm.add_turn(s.id, f"User {i}", f"Assistant {i}")

    # Запрашиваем 3 последних раунда
    recent = sm.get_recent_turns(s.id, limit=3)
    assert len(recent) == 3
    # Проверяем хронологический порядок sequence (8, 9, 10)
    assert recent[0].sequence == 8
    assert recent[0].user_text == "User 8"
    assert recent[1].sequence == 9
    assert recent[2].sequence == 10
    assert recent[2].user_text == "User 10"

    # get_model_history также использует последние раунды
    history = sm.get_model_history(s.id, limit=3)
    assert len(history) == 6  # 3 user + 3 assistant
    assert history[0]["content"] == "User 8"
    assert history[-1]["content"] == "Assistant 10"


def test_turn_deletion_and_rollback(tmp_path):
    db_file = tmp_path / "rollback_test.db"
    sm = SessionManager(db_file)
    s = sm.create_session("Тест отката")

    t1 = sm.add_turn(s.id, "Привет 1", "Ответ 1")
    t2 = sm.add_turn(s.id, "Вопрос 2", "Ответ 2")
    t3 = sm.add_turn(s.id, "Вопрос 3", "Ответ 3")
    t4 = sm.add_turn(s.id, "Вопрос 4", "Ответ 4")

    # Проверяем get_turn
    fetched = sm.get_turn(t2.id)
    assert fetched is not None
    assert fetched.user_text == "Вопрос 2"
    assert fetched.sequence == 2

    # Откатываем диалог начиная с раунда 3 (должны удалиться раунды 3 и 4)
    del_count = sm.rollback_session(s.id, from_sequence=3)
    assert del_count == 2

    turns_left = sm.get_turns(s.id)
    assert len(turns_left) == 2
    assert turns_left[0].sequence == 1
    assert turns_left[1].sequence == 2

    sess_record = sm.get_session(s.id)
    assert sess_record.turn_count == 2
    assert "Ответ 2" in sess_record.preview

    # Одиночное удаление раунда 2
    assert sm.delete_turn(t2.id) is True
    turns_left = sm.get_turns(s.id)
    assert len(turns_left) == 1
    assert turns_left[0].sequence == 1
    assert sm.get_session(s.id).turn_count == 1


def test_agent_rollback_to_turn(tmp_path):
    from capri.core.config import AppConfig
    db_file = tmp_path / "agent_rollback.db"
    sm = SessionManager(db_file)
    cfg = AppConfig()
    from capri.core.agent import CapriAgent
    agent = CapriAgent(cfg, session_manager=sm)

    # Добавляем 3 раунда в сессию агента
    t1 = sm.add_turn(agent.current_session_id, "Q1", "A1")
    t2 = sm.add_turn(agent.current_session_id, "Q2", "A2")
    t3 = sm.add_turn(agent.current_session_id, "Q3", "A3")

    agent.reload_history()
    # 1 system msg + 3 user + 3 assistant = 7 msgs
    assert len(agent.history) == 7
    assert agent.history[-1]["content"] == "A3"

    # Откатываем к раунду 2
    rolled_turn = agent.rollback_to_turn(t2.id)
    assert rolled_turn is not None
    assert rolled_turn.sequence == 2
    assert rolled_turn.user_text == "Q2"

    # В истории агента должен остаться только системный промпт + раунд 1 (3 сообщения)
    assert len(agent.history) == 3
    assert agent.history[1]["content"] == "Q1"
    assert agent.history[2]["content"] == "A1"

    # В SQLite остался 1 раунд
    remaining = sm.get_turns(agent.current_session_id)
    assert len(remaining) == 1
    assert remaining[0].user_text == "Q1"

