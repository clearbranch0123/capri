# ⚡ CAPRI // Cyberpunk AI Companion for Linux (Open Beta)

[![Version](https://img.shields.io/badge/version-v3.1.0%20(Open%20Beta)-e12d53.svg?style=for-the-badge)](CHANGELOG.md)
[![Tests](https://img.shields.io/badge/Tests-200%2F200%20passed-4dd4a8.svg?style=for-the-badge)](tests/)
[![Platform](https://img.shields.io/badge/Platform-Arch%20%7C%20CachyOS%20%7C%20Fedora-e45c84.svg?style=for-the-badge)](setup.sh)
[![Window Manager](https://img.shields.io/badge/WM-Niri%20%7C%20Hyprland%20Wayland-47d1a0.svg?style=for-the-badge)](https://github.com/YaLTeR/niri)
[![License](https://img.shields.io/badge/Code%20License-MIT-4ed8e8.svg?style=for-the-badge)](LICENSE)

Локальный автономный ИИ-компаньон под Linux с характером киберпанк-нетраннера (Project Capricorn) и единой дизайн-системой **Crimson Graphite (v3.1)**.

Включает графический интерфейс **Cyberdeck** (PySide6 / Qt 6), **Relic Construct Dashboard**, токенный стриминг, скриншоты по `Ctrl+V` и Drag & Drop, централизованную политику безопасности инструментов (`SAFE / ASK / TRUSTED`), изолированный браузер («Her Own Browser»), клиент **MCP (stdio JSON-RPC 2.0)**, виртуальный микрофон PipeWire (**Join Voice**), долговременную память **Memory Workbench**, эмоциональный движок и опциональный desktop-аватар.

---

## 🖼️ Интерфейс Capri 3.1 (Crimson Graphite)

### Cyberdeck
![Capri Cyberdeck v3.1](docs/screenshots/cyberdeck.png)

### Relic Construct Dashboard
![Capri Relic Dashboard v3.1](docs/screenshots/relic.png)

---

## 📦 Системные зависимости (Arch / CachyOS / Fedora)

| Компонент | Статус | Arch / CachyOS (`pacman`) | Fedora (`dnf`) | Зачем нужен |
|---|---|---|---|---|
| **Python 3.10+** (`venv`, `pip`) | **Обязательно** | `python` | `python3 python3-pip` | Ядро и виртуальное окружение `.venv` |
| **Qt 6 / PySide6 runtime** | **Обязательно** | `qt6-wayland` | `qt6-qtwayland` | GUI Cyberdeck, Relic Dashboard и аватар |
| **PipeWire + `pactl` + `mpv`** | **Целевой стек** *(рекомендуется)* | `pipewire pipewire-pulse mpv` | `pipewire pipewire-utils mpv` | Озвучка TTS, PipeWire WebRTC AEC и Join Voice |
| **PortAudio** | Опционально | `portaudio` | `portaudio` | Запись микрофона (`Ctrl+M`), Wake-Word и Barge-In |
| **Wayland скриншоты и буфер** | Опционально | `grim slurp wl-clipboard` | `grim slurp wl-clipboard` | Команды `/look`, скриншоты и вставка `Ctrl+V` |
| **Niri / Hyprland** | **Целевой WM** *(для полного набора)* | `niri` или `hyprland` | `niri` или `hyprland` | Оконные команды, Game Mode и Spatial Presence аватара |
| **Chromium / Brave** | Опционально | `chromium` | `chromium` | JS-рендеринг (`--js`) в изолированном браузере |

> Официально поддерживаемое и протестированное окружение — **Arch / CachyOS / Fedora + Wayland (Niri или Hyprland) + PipeWire**. На других Wayland/X11-композиторах оконные функции, Game Mode и Spatial Presence мягко отключаются без ошибок; базовый чат, память, голос, MCP и изолированный браузер продолжают работать.

---

## 🚀 Установка, обновление и удаление

### 1. Чистая установка
```bash
git clone https://github.com/clearbranch0123/capri.git && cd capri
./setup.sh
```
Скрипт создаст `.venv`, установит пакет `capri` (`capri`, `capri-deck`, `capri-avatar`), развернёт чистые шаблоны `$XDG_CONFIG_HOME/capri/.env` (`0600`) и `config.yaml` (по умолчанию в `~/.config/capri/`) и установит `.desktop`-файл.

### 2. Настройка API-ключей
Ключи хранятся **только локально** в `~/.config/capri/.env` (права `0600`) и никогда не записываются в `config.yaml`:
1. Запусти `./run.sh --gui` — при первом запуске откроется мастер `/setup`, где можно выбрать провайдера и ввести ключ.
2. Или отредактируй `~/.config/capri/.env` вручную:
   - **Groq** *(быстрый бесплатный старт для чата + Whisper STT)*: `GROQ_API_KEY=...` (`console.groq.com`)
   - **DeepSeek**: `DEEPSEEK_API_KEY=...` (`platform.deepseek.com`)
   - **Google Gemini** *(чат + Vision AI для скриншотов)*: `GEMINI_API_KEY=...` (`aistudio.google.com`)
   - **OpenRouter**: `OPENROUTER_API_KEY=...` (`openrouter.ai`)
   - **Fish Audio** *(опционально, живой TTS)*: `FISH_AUDIO_API_KEY=...` и `FISH_AUDIO_VOICE_ID=...` *(без ключа работает бесплатный Edge TTS)*

### 3. Запуск
```bash
./run.sh --gui         # Cyberdeck GUI
./run.sh --doctor      # Самодиагностика окружения
./run.sh --bugreport   # Безопасный баг-репорт (без ключей и переписки)
./run.sh --test        # Полный прогон автотестов (v3.1.0: 200/200 passed)
```

### 4. Безопасное обновление (`--upgrade`) и удаление (`--uninstall`)
```bash
git pull
./setup.sh --upgrade            # Сделает pre_upgrade-бэкап профиля, обновит .venv, не затронув ключи и чаты
```
```bash
./setup.sh --uninstall          # Удалит .venv и .desktop, СОХРАНИВ ~/.config/capri (чаты, память, .env)
./setup.sh --uninstall --purge  # Полное удаление вместе с профилем ~/.config/capri
```

### Глобальный хоткей Wayland (Niri / Hyprland)
- **Niri (`~/.config/niri/config.kdl`):** `Mod+Shift+C { spawn "capri-deck" "--toggle"; }`
- **Hyprland (`~/.config/hypr/hyprland.conf`):** `bind = SUPER SHIFT, C, exec, capri-deck --toggle`
- Автоустановка правил окон (с `.capri.bak` бэкапом): `/wm-rules niri apply` или `/wm-rules hyprland apply`.

---

## 🛡️ Безопасность и политика инструментов (`SAFE / ASK / TRUSTED`)

Каждый вызов инструмента проходит через централизованный fail-closed `ToolPolicyEngine` (подробнее в [`SECURITY.md`](SECURITY.md)):

| Режим | Что разрешено автоматически | Что требует подтверждения / заблокировано |
|---|---|---|
| **`SAFE`** | Только явные read-only инструменты (кроме `.env`, `~/.ssh`, `~/.gnupg`, `~/.config/capri`, `/etc/shadow`) и read-only allowlist команд shell (без пайпов, редиректов, glob `*`/`?`, `$VAR` и опасных флагов `git -c`) | **Заблокированы** любые мутации ОС, запись файлов, память/состояние Capri и все внешние `mcp__*` инструменты |
| **`ASK`** *(по умолчанию)* | Read-only инструменты и внутреннее состояние компаньона (память, контекст, таймеры, аватар) | **Требуют подтверждения** (`CapriToolApprovalDialog` / `y/N`): shell вне allowlist, `write_file`, `kill_process`, чтение чувствительных файлов/симлинков, действия ОС и **все** `mcp__*` инструменты |
| **`TRUSTED`** | Всё из `ASK` + неразрушающее управление десктопом (`focus_window`, `media_control`, `set_volume`) | **Всё равно требуют подтверждения**: shell вне allowlist, `write_file`, `kill_process`, доступ к чувствительным путям и все `mcp__*` инструменты |

Разрушающие паттерны (`rm -rf /`, `mkfs`, `dd of=/dev/...`, `curl | sh`, `shutdown`, форк-бомбы) **заблокированы всегда** во всех режимах. Переключение: `/policy safe | ask | trusted` или через окно настроек.

---

## ⌨️ Основные слэш-команды и горячие клавиши

- **Горячие клавиши:** `Enter` (отправить), `Shift+Enter` (новая строка), `Ctrl+N` (новый чат), `Ctrl+K` (поиск диалогов), `Ctrl+V` (скриншот/текст), `Ctrl+M` (голос), `Esc` (стоп / закрыть drawer), `F2` / `Del` (переименовать / удалить сессию).
- **Диагностика и резервные копии:** `/doctor`, `/bugreport`, `/setup`, `/policy`, `/profile export`, `/profile restore <zip>`.
- **Память и контекст:** `/new`, `/sessions`, `/memory` (`/workbench`), `/context`, `/initiative`, `/mood`, `/mode [normal|focus|playful|quiet]`.
- **Интеграции v3.x:** `/mcp [status|connect|disconnect|tools]` *(асинхронное неблокирующее подключение)*, `/browser [url|search|link|status|clear]`, `/call [join|leave|status]`, `/bargein [on|off|threshold|aec on]`, `/gamemode [auto|on|off]`, `/wm-rules`, `/look`.

---

## 🩺 Troubleshooting (частые проблемы)

| Симптом | Причина и решение |
|---|---|
| `API ключ не настроен` | Запусти `/setup` в Cyberdeck или проверь ключ в `~/.config/capri/.env` (`chmod 600`), затем проверь `/doctor`. |
| Нет звука TTS | Установи `mpv` или `pipewire-pulse` / `pipewire-utils` и проверь `/doctor`. Без ключа Fish Audio переключи движок на `edge` в настройках. |
| Эхо при Hands-Free Barge-In на колонках | Включи PipeWire WebRTC AEC (`/bargein aec on`) или подними порог (`/bargein threshold 0.065`) через калибровку микрофона в Настройках. |
| Аватар открывается в тайле вместо плавающего окна | Выполни `/wm-rules niri apply` или `/wm-rules hyprland apply` и перезагрузи конфиг композитора. |
| `/mcp connect` не подключает сервер | Проверь `mcp.enabled: true` и команду сервера в `~/.config/capri/config.yaml`; при зависании сработает таймаут или `/mcp disconnect`. |
| Скриншоты `/look` не работают | Установи `grim` и `slurp` (Wayland) и проверь наличие `GEMINI_API_KEY` для Vision AI. |

---

## ⚠️ Известные ограничения Open Beta

- **Платформа:** Официально поддерживаются только Linux-дистрибутивы **Arch / CachyOS / Fedora** с **Niri** или **Hyprland** (Wayland) и **PipeWire**. На X11 или других Wayland-композиторах оконные команды, Game Mode и Spatial Presence аватара мягко деградируют.
- **Локальный Wake-Word (Vosk):** Требует загруженной локальной модели `models/vosk-model-small-ru-0.22` (отключается через `/wake off`).
- **Headless JS-браузер (`--js`):** Требует установленного `chromium`/`brave`; без него автоматически используется быстрый изолированный `httpx`-движок.
- **Сборка релизного архива:** Для создания чистого архива и `.whl` используй `./scripts/build_beta_release.sh` (проверяет `SHA256SUMS.txt` и отсутствие секретов).

---

## 🐛 Как сообщить о баге

1. Выполни в терминале `./run.sh --bugreport` (или `/bugreport` в чате Cyberdeck).
2. Команда сохранит файл в `~/.config/capri/bugreports/capri-bugreport-*.md`. В него **автоматически не попадают** переписка (`sessions.db`), память, эмоции и `.env`, а API-ключи, токены, URL-секреты и домашние пути редактируются.
3. Открой Issue через шаблон **Bug Report** на GitHub и приложи содержимое этого файла.
4. Об уязвимостях безопасности сообщай **приватно** согласно [`SECURITY.md`](SECURITY.md).

---

## 📄 Лицензия и атрибуция

- **Исходный код Capri Linux:** распространяется под лицензией **MIT** — см. [`LICENSE`](LICENSE).
- **Ассеты, спрайты, 3D-модель и аудио (`capri/assets/`):** НЕ покрываются лицензией MIT и предназначены строго для некоммерческого использования с указанием авторства — подробный перечень в [`ASSETS_NOTICE.md`](ASSETS_NOTICE.md).
- Вдохновение и благодарности: *Cyberpunk: Edgerunners 2* и open-source проект [`lachydotmcg/capri-desktop`](https://github.com/lachydotmcg/capri-desktop) (**PolyForm Noncommercial License 1.0.0**).
- Полная история релизов: [`CHANGELOG.md`](CHANGELOG.md).
