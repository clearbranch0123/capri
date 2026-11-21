from __future__ import annotations

import asyncio
import shutil
from .registry import tool


@tool(description="Управление медиаплеерами (Spotify, браузер). action: 'play', 'pause', 'stop', 'play-pause', 'next', 'previous', 'status' (что играет). player: 'spotify' опционально.")
async def media_control(action: str, player: str = "", **kwargs) -> str:
    if not shutil.which("playerctl"):
        return "Утилита 'playerctl' не найдена в системе."

    action_clean = (action or "").lower().strip()
    valid_actions = {
        "play": ["playerctl", "play"],
        "играть": ["playerctl", "play"],
        "pause": ["playerctl", "pause"],
        "пауза": ["playerctl", "pause"],
        "stop": ["playerctl", "stop"],
        "стоп": ["playerctl", "stop"],
        "останови": ["playerctl", "stop"],
        "остановить": ["playerctl", "stop"],
        "play-pause": ["playerctl", "play-pause"],
        "toggle": ["playerctl", "play-pause"],
        "next": ["playerctl", "next"],
        "следующий": ["playerctl", "next"],
        "previous": ["playerctl", "previous"],
        "prev": ["playerctl", "previous"],
        "предыдущий": ["playerctl", "previous"],
    }

    p_flag = ["-p", player.strip().lower()] if player and player.strip() else []

    if action_clean in ("status", "info", "current", "track", "статус"):
        try:
            # Получаем статус
            cmd = ["playerctl"] + p_flag + ["status"]
            status_proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
            )
            s_out, _ = await asyncio.wait_for(status_proc.communicate(), timeout=2.5)
            status_text = s_out.decode().strip() or "Нет активных плееров"

            # Получаем метаданные: название и артиста
            meta_cmd = ["playerctl"] + p_flag + ["metadata", "--format", "{{ artist }} - {{ title }} [{{ playerName }}]"]
            meta_proc = await asyncio.create_subprocess_exec(
                *meta_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
            )
            m_out, _ = await asyncio.wait_for(meta_proc.communicate(), timeout=2.5)
            meta_text = m_out.decode().strip()

            if meta_text and meta_text != " -  []":
                return f"Сейчас играет ({status_text}): {meta_text}"
            return f"Статус медиаплеера: {status_text} (трек не определен)."
        except asyncio.TimeoutError:
            return "Таймаут получения статуса медиаплеера."
        except Exception as e:
            return f"Ошибка при получении статуса медиа: {e}"

    if action_clean not in valid_actions:
        return f"Неизвестное действие '{action}'. Доступно: play, pause, stop, play-pause, next, previous, status."

    try:
        base_cmd = valid_actions[action_clean]
        cmd = [base_cmd[0]] + p_flag + [base_cmd[1]]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=3.0)
        if proc.returncode == 0:
            return f"Медиа-команда '{action_clean}' успешно выполнена."
        else:
            err = stderr.decode().strip()
            return f"Не удалось выполнить '{action_clean}': {err if err else 'плееры не запущены'}."
    except asyncio.TimeoutError:
        return f"Медиа-команда '{action_clean}' отправлена (таймаут ответа плеера)."
    except Exception as e:
        return f"Ошибка управления плеером: {e}"


@tool(
    description="Подключение или отключение Capri в голосовой звонок (Discord/Telegram/браузер) через виртуальный микрофон PipeWire. action: 'join', 'leave', 'status'. mix_mic: подмешивать ли микрофон пользователя (по умолчанию true)."
)
async def capri_voice_call(action: str = "status", mix_mic: bool = True) -> str:
    from ..voice.join_voice import get_join_voice_status, join_voice, leave_voice

    act = (action or "status").strip().lower()
    if act in ("join", "start", "on", "подключиться", "войти"):
        _ok, msg = join_voice(mix_user_mic=bool(mix_mic))
        return msg
    if act in ("leave", "stop", "off", "выйти", "отключиться"):
        _ok, msg = leave_voice()
        return msg
    st = get_join_voice_status()
    if not st.pipewire_available:
        return "Join Voice недоступен: PipeWire / pactl не обнаружен."
    if st.active:
        mix_lbl = "с подмешиванием твоего микрофона" if st.mic_mixed else "только голос Капри"
        return f"Join Voice АКТИВЕН ({mix_lbl}). Виртуальный микрофон: `{st.virtual_mic_name}`."
    return "Join Voice не активен. Используй `/call join` или `capri_voice_call('join')`."

