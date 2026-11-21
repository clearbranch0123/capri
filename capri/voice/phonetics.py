"""
Модуль фонетической нормализации и транслитерации для русскоязычных TTS моделей (Silero, Piper).
Преобразует английские слова, команды, названия софта, железа и сленг в кириллицу
с правильной фонетикой и ударениями.
"""

import re

# Словарь специализированных терминов софта, железа, Linux и киберпанка
DICTIONARY = {
    # Приложения и утилиты
    "dolphin": "Д+олфин",
    "kitty": "К+итти",
    "alacritty": "Алакр+итти",
    "firefox": "Ф+айрфокс",
    "chromium": "Хр+омиум",
    "chrome": "Хром",
    "brave": "Брейв",
    "discord": "Диск+орд",
    "telegram": "Телегр+ам",
    "steam": "Стим",
    "spotify": "Спотиф+ай",
    "youtube": "Ют+уб",
    "obs": "О-Бэ-Эс",
    "vlc": "Вэ-Эл-Цэ",
    "mpv": "эм-пэ-вэ",
    "ffmpeg": "ФФ-мп+ег",
    "easyeffects": "Изиэфф+ектс",
    "lutris": "Л+ютрис",
    "wine": "Вайн",
    "proton": "Прот+он",
    "rofi": "Р+офи",
    "wofi": "В+офи",
    "waybar": "В+ейбар",
    "cliphist": "Клипх+ист",
    "fastfetch": "Фастф+етч",
    "neofetch": "Неоф+етч",
    "btop": "би-топ",
    "htop": "эйч-топ",

    # Linux, Compositor, Sound
    "linux": "Л+инукс",
    "arch": "Арч",
    "cachyos": "Кэши О-Эс",
    "cachy": "Кэши",
    "ubuntu": "Уб+унту",
    "debian": "Д+ебиан",
    "fedora": "Фед+ора",
    "niri": "Н+ири",
    "hyprland": "Х+айперленд",
    "sway": "Свей",
    "wayland": "В+ейланд",
    "x11": "Икс одиннадцать",
    "pipewire": "П+айпвайр",
    "wireplumber": "Вайрпл+амбер",
    "pulseaudio": "Пульс+аудио",
    "wpctl": "вэ-пэ-контр+оль",
    "grim": "грим",
    "slurp": "слёрп",
    "grimshot": "грим-шот",
    "grim-shot": "грим-шот",
    "notify-send": "нотиф+ай-сэнд",
    "mute-toggle": "мьют-т+огл",
    "mute": "мьют",
    "unmute": "анмьют",
    "toggle": "т+огл",
    "fullscreen": "фуллскр+ин",
    "select": "сел+ект",
    "workspace": "ворксп+ейс",
    "workspaces": "ворксп+ейсы",
    "kernel": "к+ернел",
    "sudo": "с+удо",
    "root": "рут",
    "bash": "баш",
    "zsh": "зэд-эс-аш",
    "fish": "фиш",
    "flatpak": "Флэтп+ак",
    "pacman": "П+акман",
    "paru": "П+ару",
    "yay": "Яй",

    # Железо и спецификации
    "cpu": "Цэ-Пэ-У",
    "gpu": "Гэ-Пэ-У",
    "ram": "Рэм",
    "vram": "Ви-Рэм",
    "rom": "Ром",
    "ssd": "Эс-Эс-Ди",
    "hdd": "Хард",
    "rtx": "Эр-Тэ-Икс",
    "gtx": "Джи-Тэ-Икс",
    "rx": "Эр-Икс",
    "nvidia": "Нв+идиа",
    "intel": "Инт+ел",
    "amd": "А-Эм-Дэ",
    "geforce": "Джиф+орс",
    "ryzen": "Р+айзен",
    "radeon": "Раде+он",
    "asus": "Ас+ус",
    "fifine": "Файф+айн",
    "usb": "Ю-Эс-Би",
    "hdmi": "Эйч-Ди-Эм-Ай",
    "dp": "Ди-Пи",
    "displayport": "Дисплейп+орт",
    "wifi": "Вай-ф+ай",
    "wi-fi": "Вай-ф+ай",
    "bluetooth": "Блют+уз",

    # Клавиши и хоткеи
    "mod": "Мод",
    "shift": "Шифт",
    "ctrl": "Контр+ол",
    "control": "Контр+ол",
    "alt": "Альт",
    "super": "С+упер",
    "enter": "Энтер",
    "tab": "Таб",
    "space": "Спейс",
    "escape": "Эск+ейп",
    "esc": "Эск+ейп",
    "backspace": "Бэксп+ейс",
    "delete": "Дел+ит",

    # AI, Дев & Cyberpunk
    "git": "Гит",
    "github": "Гитх+аб",
    "gitlab": "Гитл+аб",
    "python": "П+айтон",
    "rust": "Раст",
    "docker": "Д+окер",
    "ai": "Эй-Ай",
    "api": "А-Пи-Ай",
    "llm": "Эл-Эл-Эм",
    "groq": "Грок",
    "gemini": "Дж+емини",
    "openai": "Оупен-Эй-Ай",
    "gpt": "Джи-Пи-Ти",
    "tts": "Ти-Ти-Эс",
    "stt": "Эс-Ти-Ти",
    "piper": "П+айпер",
    "silero": "Сил+еро",
    "xtts": "Икс-Ти-Ти-Эс",
    "baya": "Б+ая",
    "irina": "Ир+ина",
    "url": "урл",
    "ui": "Ю-Ай",
    "gui": "Гуи",
    "cli": "Си-Эл-Ай",
    "os": "О-Эс",
    "id": "Ай-Ди",
    "ip": "Ай-Пи",
    "ssh": "Эс-Эс-Аш",
    "ping": "пинг",
    "pong": "понг",
    "online": "онл+айн",
    "offline": "оффл+айн",
    "link": "линк",
    "web": "веб",
    "edgerunners": "Эджр+аннерс",
    "cyberpunk": "К+айберпанк",
    "netrunner": "Нетр+аннер",
    "arasaka": "Арас+ака",
    "militech": "Милит+ех",
    "sandevistan": "Сандев+истан",
    "braindance": "Брейнд+анс",
    "night": "найт",
    "city": "с+ити",
    "capri": "К+апри",
    "teo": "Т+ео",
}

# Одиночные буквы для аббревиатур и хоткеев
LETTERS = {
    "a": "эй", "b": "би", "c": "си", "d": "ди", "e": "и",
    "f": "эф", "g": "джи", "h": "эйч", "i": "ай", "j": "джей",
    "k": "кей", "l": "эль", "m": "эм", "n": "эн", "o": "оу",
    "p": "пи", "q": "кью", "r": "ар", "s": "эс", "t": "ти",
    "u": "ю", "v": "ви", "w": "дабл-ю", "x": "икс", "y": "уай", "z": "зет"
}


def transliterate_word(w: str) -> str:
    """Фонетический транслитератор для произвольных английских слов."""
    low = w.lower()
    if low in DICTIONARY:
        return DICTIONARY[low]

    if len(low) == 1 and low in LETTERS:
        return LETTERS[low]

    w_sub = low
    # Частые суффиксы и буквосочетания
    w_sub = re.sub(r"tion\b", "шн", w_sub)
    w_sub = re.sub(r"sion\b", "жн", w_sub)
    w_sub = re.sub(r"ght\b", "йт", w_sub)
    w_sub = re.sub(r"igh", "ай", w_sub)
    w_sub = re.sub(r"eigh", "ей", w_sub)
    w_sub = re.sub(r"ough", "оу", w_sub)
    w_sub = re.sub(r"ph", "ф", w_sub)
    w_sub = re.sub(r"sh", "ш", w_sub)
    w_sub = re.sub(r"ch", "ч", w_sub)
    w_sub = re.sub(r"th", "т", w_sub)
    w_sub = re.sub(r"wh", "в", w_sub)
    w_sub = re.sub(r"qu", "кв", w_sub)
    w_sub = re.sub(r"ck", "к", w_sub)
    w_sub = re.sub(r"ea", "и", w_sub)
    w_sub = re.sub(r"ee", "и", w_sub)
    w_sub = re.sub(r"oo", "у", w_sub)
    w_sub = re.sub(r"ou", "ау", w_sub)
    w_sub = re.sub(r"ow", "оу", w_sub)
    w_sub = re.sub(r"ai|ay", "эй", w_sub)
    w_sub = re.sub(r"ei|ey", "эй", w_sub)
    w_sub = re.sub(r"oi|oy", "ой", w_sub)
    w_sub = re.sub(r"au|aw", "о", w_sub)
    w_sub = re.sub(r"c([eiy])", r"с\1", w_sub)
    w_sub = re.sub(r"g([eiy])", r"дж\1", w_sub)

    mapping = {
        "a": "а", "b": "б", "c": "к", "d": "д", "e": "е",
        "f": "ф", "g": "г", "h": "х", "i": "и", "j": "дж",
        "k": "к", "l": "л", "m": "м", "n": "н", "o": "о",
        "p": "п", "q": "к", "r": "р", "s": "с", "t": "т",
        "u": "у", "v": "в", "w": "в", "x": "кс", "y": "и", "z": "з"
    }
    res = [mapping.get(char, char) for char in w_sub]
    return "".join(res)


def normalize_phonetics(text: str) -> str:
    """Преобразует спецсимволы, английские фразы и слова в натуральный русский для озвучки."""
    # Обработка языков программирования
    text = re.sub(r"\b[Cc]\+\+", "Си плюс плюс", text)
    text = re.sub(r"\b[Cc]#", "Си шарп", text)

    # Проценты и символы
    text = re.sub(r"(\d+)\s*%", r"\1 процентов", text)
    text = re.sub(r"%", " процентов", text)
    text = re.sub(r"\$", " долларов ", text)
    text = re.sub(r"€", " евро ", text)
    text = re.sub(r"&", " и ", text)

    # Хоткеи со знаком плюс (Mod+Shift+C -> Mod плюс Shift плюс C)
    text = re.sub(r"([a-zA-Z0-9]+)\+([a-zA-Z0-9]+)", r"\1 плюс \2", text)

    # Английские токены и составные слова через дефис (grim-shot, mute-toggle)
    def replace_token(match):
        token = match.group(0)
        low = token.lower()
        if low in DICTIONARY:
            return DICTIONARY[low]
        if "-" in token:
            parts = token.split("-")
            sub_res = [DICTIONARY.get(p.lower(), transliterate_word(p)) for p in parts]
            return "-".join(sub_res)
        return transliterate_word(token)

    pattern = re.compile(r"[a-zA-Z]+(?:-[a-zA-Z]+)*")
    text = pattern.sub(replace_token, text)

    return text
