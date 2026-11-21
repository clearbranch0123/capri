import asyncio
import json
import os
import shlex
import shutil
import subprocess
import time
import urllib.parse
from pathlib import Path
from .registry import tool
from ..core.compositor import compositor, detect_compositor, is_hyprland_available, is_niri_available

_NIRI_UNAVAILABLE_MSG = (
    "Композитор Niri не обнаружен в текущей сессии (поддерживаются Niri и Hyprland). "
    "Оконные команды мягко отключены; остальные функции Capri работают в штатном режиме."
)


APP_ALIASES = {
    "dolphin": "dolphin",
    "дельфин": "dolphin",
    "файлы": "dolphin",
    "проводник": "dolphin",
    "файловый менеджер": "dolphin",
    "браузер": "firefox",
    "browser": "firefox",
    "firefox": "firefox",
    "фаерфокс": "firefox",
    "файрфокс": "firefox",
    "лиса": "firefox",
    "огнелис": "firefox",
    "мозилла": "firefox",
    "мозила": "firefox",
    "хром": "chromium",
    "chromium": "chromium",
    "chrome": "chromium",
    "терминал": "kitty",
    "консоль": "kitty",
    "terminal": "kitty",
    "kitty": "kitty",
    "telegram": "Telegram",
    "телеграм": "Telegram",
    "телеграмм": "Telegram",
    "телега": "Telegram",
    "телегу": "Telegram",
    "тг": "Telegram",
    "spotify": "spotify",
    "спотифай": "spotify",
    "спотик": "spotify",
    "музыка": "spotify",
    "код": "code",
    "вскод": "code",
    "vscode": "code",
    "code": "code",
    "obsidian": "obsidian",
    "обсидиан": "obsidian",
    "btop": "kitty -e btop",
    "nvim": "kitty -e nvim",
    "neovim": "kitty -e nvim",
    "дискорд": "discord",
    "стим": "steam",
    "chatgpt": "chatgpt",
    "чатгпт": "chatgpt",
    "гпт": "chatgpt",
    "калькулятор": "org.gnome.Calculator",
    "настройки": "inir settings",
    "settings": "inir settings",
}


@tool(description="Запустить приложение Linux или перевести на него фокус (dolphin, firefox, kitty, spotify).")
async def open_app(app_name: str) -> str:
    cleaned = app_name.lower().strip()
    target_cmd = APP_ALIASES.get(cleaned, app_name)

    # 1. Сначала проверяем, не открыто ли приложение уже в Niri/Hyprland — если да, мгновенно переводим фокус
    try:
        if compositor.is_available:
            windows = await compositor.async_list_windows()
            for w in windows:
                w_app = (w.get("app_id") or "").lower()
                target_check = target_cmd.split()[0].lower()
                if cleaned in w_app or target_check in w_app:
                    w_id = w.get("id")
                    ok, _ = await compositor.async_focus_window(w_id)
                    if ok:
                        return f"Приложение '{app_name}' уже запущено (окно '{w.get('title')}'), фокус переведён на него."
    except Exception:
        pass

    # 2. Если не открыто — запускаем новый процесс
    try:
        parts = shlex.split(target_cmd)
    except ValueError:
        return f"Некорректное имя или аргументы приложения: '{app_name}'."
    if not parts or parts[0].startswith("-"):
        return f"Некорректное имя исполняемого файла: '{app_name}'."
    bin_name = parts[0]

    if not shutil.which(bin_name):
        return f"Не удалось найти исполняемый файл для '{app_name}' (бинарник {bin_name} отсутствует)."

    try:
        subprocess.Popen(
            parts,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
        return f"Приложение '{app_name}' успешно запущено."
    except Exception as e:
        return f"Ошибка при запуске '{app_name}': {e}"


@tool(description="Закрыть приложение или окно (firefox, spotify, telegram, discord).")
async def close_app(target: str) -> str:
    from .shell import kill_process
    return await kill_process(target)


@tool(description="Открыть URL-ссылку в браузере по умолчанию.")
async def open_url(url: str) -> str:
    raw = (url or "").strip()
    if not raw or any(ch in raw for ch in ("\r", "\n", "\x00")):
        return "Ошибка: некорректный URL."
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme:
        if parsed.scheme.lower() not in ("http", "https"):
            return f"Ошибка: запрещённая схема URL '{parsed.scheme}' (разрешены только http:// и https://)."
        safe_url = raw
    else:
        if raw.startswith("-"):
            return "Ошибка: некорректный URL."
        safe_url = "https://" + raw
        parsed = urllib.parse.urlparse(safe_url)
    if not parsed.netloc:
        return "Ошибка: не указан домен/хост в URL."

    try:
        subprocess.Popen(
            ["xdg-open", safe_url],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return f"Ссылка {safe_url} открыта в браузере."
    except Exception as e:
        return f"Ошибка открытия ссылки {safe_url}: {e}"


@tool(description="Громкость PipeWire (wpctl). level: '+10%', '-10%', '50%', 'тише', 'громче', 'макс', 'mute'.")
async def set_volume(level: str) -> str:
    level = level.strip().lower()
    try:
        if level in ("mute", "unmute", "toggle"):
            arg = "1" if level == "mute" else ("0" if level == "unmute" else "toggle")
            proc = await asyncio.create_subprocess_exec(
                "wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", arg,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            await proc.communicate()
            return f"Звук переключен: {level}."

        # Разговорные команды изменения громкости
        if level in ("тише", "потише", "снизить", "убавить"):
            arg = "0.10-"
            display_level = "-10%"
        elif level in ("громче", "погромче", "прибавить", "добавить"):
            arg = "0.10+"
            display_level = "+10%"
        elif level in ("максимум", "макс", "100"):
            arg = "1.0"
            display_level = "100%"
        elif level in ("минимум", "мин", "0"):
            arg = "0.0"
            display_level = "0%"
        elif level.endswith("%"):
            val = float(level.replace("%", "")) / 100.0
            if level.startswith("+") or level.startswith("-"):
                arg = f"{abs(val):.2f}{'+' if level.startswith('+') else '-'}"
            else:
                arg = f"{val:.2f}"
            display_level = level
        else:
            # Защита от оглушения пользователя: если число передано без %, например "50",
            # то в wpctl аргумент '50' трактуется как 5000% громкости!
            try:
                val = float(level)
                if val > 1.5:
                    val = val / 100.0
                arg = f"{val:.2f}"
                display_level = f"{int(val * 100)}%"
            except ValueError:
                arg = level
                display_level = level

        proc = await asyncio.create_subprocess_exec(
            "wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", arg,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        await proc.communicate()
        return f"Громкость изменена на {display_level}."
    except Exception as e:
        return f"Ошибка регулировки звука: {e}"


@tool(description="Текущий уровень громкости звука PipeWire.")
async def get_volume() -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            "wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        output = stdout.decode().strip()
        # Output format is usually "Volume: 0.65" or "Volume: 0.65 [MUTED]"
        return f"Текущий статус звука: {output}"
    except Exception as e:
        return f"Не удалось получить громкость: {e}"


@tool(description="Действие в Niri/Hyprland: 'focus-workspace-down', 'focus-workspace-up', 'toggle-overview', 'fullscreen-window'.")
async def niri_action(action: str) -> str:
    action = action.strip()
    # Safety: don't let LLM close the session unexpectedly unless asked
    if "quit" in action.lower() or "exit" == action.lower():
        return "Действие 'quit' заблокировано для безопасности."
    if not compositor.is_available:
        return _NIRI_UNAVAILABLE_MSG

    comp_name = compositor.kind.capitalize()
    ok, msg = await compositor.async_dispatch_action(action)
    if ok:
        return f"{comp_name} действие '{action}' успешно выполнено."
    return f"{comp_name} вернул ошибку: {msg}"


@tool(description="Скриншот экрана через grim. mode: 'fullscreen'|'select'.")
async def take_screenshot(mode: str = "fullscreen") -> str:
    screenshots_dir = Path.home() / "Pictures" / "Screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=True)
    filename = f"screenshot_{int(time.time())}.png"
    filepath = screenshots_dir / filename

    try:
        if mode == "select" and shutil.which("slurp"):
            slurp_proc = await asyncio.create_subprocess_exec(
                "slurp", stdout=asyncio.subprocess.PIPE
            )
            region, _ = await slurp_proc.communicate()
            if slurp_proc.returncode != 0 or not region:
                return "Выделение области скриншота отменено пользователем."
            
            grim_proc = await asyncio.create_subprocess_exec(
                "grim", "-g", region.decode().strip(), str(filepath)
            )
            await grim_proc.communicate()
        else:
            grim_proc = await asyncio.create_subprocess_exec("grim", str(filepath))
            await grim_proc.communicate()

        # Send desktop notification
        if shutil.which("notify-send"):
            await asyncio.create_subprocess_exec(
                "notify-send", "Капри", f"Скриншот сохранён: {filename}", "-i", str(filepath)
            )

        return f"Скриншот успешно сохранён: {filepath}"
    except Exception as e:
        return f"Ошибка при создании скриншота: {e}"


@tool(description="Системная сводка: использование RAM, статус NVIDIA GPU (VRAM, темп., нагрузка), ядро Linux.")
async def get_system_status() -> str:
    status_lines = []

    # Memory
    try:
        with open("/proc/meminfo", "r") as f:
            mem_info = f.read()
        total_kb = int([line.split()[1] for line in mem_info.splitlines() if "MemTotal:" in line][0])
        avail_kb = int([line.split()[1] for line in mem_info.splitlines() if "MemAvailable:" in line][0])
        used_gb = (total_kb - avail_kb) / (1024 * 1024)
        total_gb = total_kb / (1024 * 1024)
        status_lines.append(f"RAM: {used_gb:.1f} GB / {total_gb:.1f} GB ({((used_gb/total_gb)*100):.0f}%)")
    except Exception:
        pass

    # GPU
    try:
        proc = await asyncio.create_subprocess_exec(
            "nvidia-smi",
            "--query-gpu=name,temperature.gpu,utilization.gpu,memory.used,memory.total",
            "--format=csv,noheader,nounits",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0:
            name, temp, util, mem_used, mem_total = stdout.decode().strip().split(",")
            status_lines.append(
                f"GPU: {name.strip()} | Нагрузка: {util.strip()}% | Температура: {temp.strip()}°C | VRAM: {mem_used.strip()} MB / {mem_total.strip()} MB"
            )
    except Exception:
        pass

    # Uptime
    try:
        proc = await asyncio.create_subprocess_exec(
            "uptime", "-p", stdout=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        status_lines.append(f"Uptime: {stdout.decode().strip()}")
    except Exception:
        pass

    return "\n".join(status_lines) if status_lines else "Не удалось собрать телеметрию системы."


@tool(description="Отправить системное уведомление (notify-send). title: заголовок, message: текст.")
async def send_desktop_notification(title: str, message: str) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            "notify-send", "--", str(title), str(message)
        )
        await proc.communicate()
        return f"Уведомление '{title}' отправлено."
    except Exception as e:
        return f"Ошибка отправки уведомления: {e}"


@tool(description="Список всех открытых окон в Niri или Hyprland с их ID, app_id и заголовками.")
async def list_windows() -> str:
    if not compositor.is_available:
        return _NIRI_UNAVAILABLE_MSG
    try:
        windows = await compositor.async_list_windows()
        if not windows:
            return "Открытых окон не обнаружено."

        lines = []
        for w in windows:
            focused = " [FOCUSED]" if w.get("is_focused") else ""
            floating = " [FLOATING]" if w.get("is_floating") else ""
            app_id = w.get("app_id", "unknown")
            title = w.get("title", "")
            lines.append(f"• [ID {w.get('id')}] {app_id}: \"{title}\" (WS {w.get('workspace_id')}){focused}{floating}")
        return "\n".join(lines)
    except Exception as e:
        return f"Ошибка при получении списка окон: {e}"


@tool(description="Фокус на окно по названию ('capri', 'браузер', 'терминал', 'spotify') или ID (Niri / Hyprland).")
async def focus_window(target: str = "capri") -> str:
    if not compositor.is_available:
        return _NIRI_UNAVAILABLE_MSG
    cleaned = target.strip().lower()
    try:
        windows = await compositor.async_list_windows()
        if not windows:
            return "Нет открытых окон для фокуса."

        # 1. Поиск по ID (Niri int ID или Hyprland address 0x...)
        for w in windows:
            if str(w.get("id")).lower() == cleaned:
                w_id = w.get("id")
                await compositor.async_focus_window(w_id)
                return f"Фокус переведён на окно ID {w_id} ('{w.get('title')}')."

        # 2. Алиасы приложений
        alias_map = {
            "capri": ["capri-cyberdeck", "capri-terminal", "capri"],
            "себя": ["capri-cyberdeck", "capri-terminal", "capri"],
            "self": ["capri-cyberdeck", "capri-terminal", "capri"],
            "кибердек": ["capri-cyberdeck"],
            "cyberdeck": ["capri-cyberdeck"],
            "терминал": ["kitty", "terminal", "alacritty", "wezterm"],
            "консоль": ["kitty", "terminal"],
            "kitty": ["kitty"],
            "браузер": ["firefox", "chromium", "zen", "chrome"],
            "browser": ["firefox", "chromium", "zen", "chrome"],
            "firefox": ["firefox"],
            "фаерфокс": ["firefox"],
            "файрфокс": ["firefox"],
            "лиса": ["firefox"],
            "огнелис": ["firefox"],
            "мозилла": ["firefox"],
            "мозила": ["firefox"],
            "телеграм": ["org.telegram.desktop", "telegram"],
            "телега": ["org.telegram.desktop", "telegram"],
            "тг": ["org.telegram.desktop", "telegram"],
            "spotify": ["spotify"],
            "спотифай": ["spotify"],
            "музыка": ["spotify"],
            "чатгпт": ["chatgpt"],
            "chatgpt": ["chatgpt"],
            "гпт": ["chatgpt"],
            "файлы": ["dolphin"],
            "проводник": ["dolphin"],
            "код": ["code", "vscode"],
            "code": ["code", "vscode"],
            "vscode": ["code", "vscode"],
            "obsidian": ["obsidian"],
            "обсидиан": ["obsidian"],
            "btop": ["btop"],
        }

        matched_window = None

        # Проверка по алиасу: точное совпадение или не менее 3 символов для подстроки
        for alias_key, app_keys in alias_map.items():
            is_match = (alias_key == cleaned) or (
                len(cleaned) >= 3 and (alias_key in cleaned or cleaned in alias_key)
            )
            if is_match:
                for w in windows:
                    w_app = (w.get("app_id") or "").lower()
                    if any(ak in w_app for ak in app_keys):
                        matched_window = w
                        break
            if matched_window:
                break

        # Проверка по прямому вхождению в app_id или title
        if not matched_window:
            for w in windows:
                w_app = (w.get("app_id") or "").lower()
                w_title = (w.get("title") or "").lower()
                if cleaned in w_app or cleaned in w_title:
                    matched_window = w
                    break

        if matched_window:
            w_id = matched_window.get("id")
            await compositor.async_focus_window(w_id)
            return f"Фокус успешно переведён на окно '{matched_window.get('title')}' ({matched_window.get('app_id')}, ID {w_id})."

        return f"Окно по запросу '{target}' не найдено среди активных окон."
    except Exception as e:
        return f"Ошибка при переводе фокуса: {e}"


@tool(description="Самоуправление Capri. action: 'center' (в центр), 'focus', 'maximize', 'move_left', 'move_right', 'set_mode' (value: 'focus'|'normal'|'playful'), 'avatar_action' (value: 'dance'|'glitch'|'toggle'), 'mood' (статус).")
async def capri_self_control(action: str, value: str = "") -> str:
    action_clean = (action or "").strip().lower()
    val_clean = (value or "").strip().lower()


    # 1. Фокус своего окна
    if action_clean in ("focus", "focus_self", "сфокусируй"):
        return await focus_window("capri")

    # 2. Оконные действия Niri над своим окном
    if action_clean in ("center", "отцентруй"):
        await focus_window("capri")
        await asyncio.create_subprocess_exec("niri", "msg", "action", "center-column")
        return "Окно Капри отцентровано на экране."

    if action_clean in ("maximize", "разверни", "максимизируй"):
        await focus_window("capri")
        await asyncio.create_subprocess_exec("niri", "msg", "action", "maximize-column")
        return "Колонка окна Капри развёрнута / переключена."

    if action_clean in ("toggle_floating", "плавающее", "float"):
        await focus_window("capri")
        await asyncio.create_subprocess_exec("niri", "msg", "action", "toggle-window-floating")
        return "Режим плавающего/тайлингового окна переключен."

    if action_clean in ("move_left", "влево"):
        await focus_window("capri")
        await asyncio.create_subprocess_exec("niri", "msg", "action", "move-column-left")
        return "Колонка окна Капри перемещена влево."

    if action_clean in ("move_right", "вправо"):
        await focus_window("capri")
        await asyncio.create_subprocess_exec("niri", "msg", "action", "move-column-right")
        return "Колонка окна Капри перемещена вправо."

    # 3. Аватар и анимации
    if action_clean in ("avatar_action", "avatar", "аватар"):
        from ..ui.client import notify_avatar
        act = val_clean or "playful"
        if act in ("2d", "3d", "classic", "circle"):
            from ..ui.client import set_avatar_mode
            if set_avatar_mode(act):
                return f"Режим аватара переключен на '{act}'."

        if act == "toggle":
            proc = await asyncio.create_subprocess_exec("pgrep", "-f", "capri.ui.overlay", stdout=asyncio.subprocess.PIPE)
            stdout, _ = await proc.communicate()
            if proc.returncode == 0:
                await asyncio.create_subprocess_exec("pkill", "-f", "capri.ui.overlay")
                return "Плавающий аватар скрыт."
            else:
                proj_root = Path(__file__).resolve().parent.parent.parent
                env = dict(os.environ)
                env["PYTHONPATH"] = str(proj_root)
                subprocess.Popen(
                    [str(proj_root / ".venv" / "bin" / "python"), "-m", "capri.ui.overlay"],
                    cwd=str(proj_root),
                    env=env,
                    start_new_session=True,
                )
                return "Плавающий аватар запущен."

        ok = notify_avatar(act, f"Действие: {act}")
        if ok:
            return f"Аватар переключен в анимацию '{act}'."
        return f"Команда аватару '{act}' отправлена."

    # 4. Переключение режима
    if action_clean in ("set_mode", "mode", "режим"):
        valid_modes = ("focus", "normal", "quiet", "playful")
        if val_clean in valid_modes:
            from ..core.config import load_config, save_config
            cfg = load_config()
            cfg.companion.mode = val_clean
            save_config(cfg)
            return f"Режим Капри успешно переключен на [{val_clean.upper()}]."
        return f"Неверный режим '{value}'. Доступно: {', '.join(valid_modes)}."

    # 5. Текущее эмоциональное состояние и статус
    if action_clean in ("mood", "настроение", "эмоции", "status", "статус"):
        from ..core.emotions import EmotionEngine
        eng = EmotionEngine()
        return f"Текущее состояние Капри: Настроение={eng.mood}, Доверие к D={eng.trust}/100, Раздражение={eng.irritation}%."

    # 6. Пространственное присутствие (Capri Spatial Presence)
    if action_clean in ("come", "ко мне", "подойди", "иди сюда", "сюда", "stay", "стой", "стой тут", "follow", "следуй", "home", "на базу", "домой", "в угол"):
        return await capri_locomotion(action_clean, val_clean)

    return f"Неизвестное действие самоуправления: '{action}'."


@tool(description="Перемещение аватара на экране. action: 'come' (ко мне/к окну), 'stay' (стой на месте), 'home' (на базу в угол), 'follow' (value: 'on'|'off').")
async def capri_locomotion(action: str, value: str = "") -> str:
    from ..ui.client import notify_avatar_locomotion
    act_clean = (action or "").strip().lower()
    val_clean = (value or "").strip().lower()


    if act_clean in ("come", "ко мне", "подойди", "иди сюда", "сюда", "быстро сюда"):
        ok = notify_avatar_locomotion("come")
        return "Команда перемещения 'КО МНЕ' отправлена аватару." if ok else "Плавающий аватар не запущен."

    if act_clean in ("stay", "стой", "стой тут", "стой на месте", "не двигайся", "останься", "зафиксируйся"):
        ok = notify_avatar_locomotion("stay")
        return "Аватар зафиксирован в текущей точке (Stay Mode)." if ok else "Плавающий аватар не запущен."

    if act_clean in ("follow", "следуй", "следование"):
        enable = val_clean not in ("off", "0", "false", "нет", "отключи")
        ok = notify_avatar_locomotion("follow", enabled=enable)
        state_str = "включено" if enable else "выключено"
        return f"Следование за рабочими столами {state_str}." if ok else "Плавающий аватар не запущен."

    if act_clean in ("home", "на базу", "база", "домой", "в угол", "на место", "исходная"):
        ok = notify_avatar_locomotion("home")
        return "Аватар возвращается на базу (Home Base)." if ok else "Плавающий аватар не запущен."

    return f"Неизвестное действие перемещения: '{action}'. Доступно: 'come', 'stay', 'follow', 'home'."

