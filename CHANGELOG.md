# 📋 // CAPRI // CHANGELOG

Все заметные пользовательские изменения проекта **Capri AI Companion** документируются в этом файле.
Формат основан на [Keep a Changelog](https://keepachangelog.com/ru/1.0.0/), проект следует [Семантическому Версионированию](https://semver.org/lang/ru/).

---

## [3.1.0] — 2026-11-21 (Open Beta)

Первый публичный релиз **Capri AI Companion for Linux** на базе дизайн-системы **Crimson Graphite**, централизованной политики безопасности и полного набора десктоп-интеграций под Wayland (Niri / Hyprland) и PipeWire.

### 🎨 Интерфейс — Crimson Graphite (Cyberdeck + Relic Construct Dashboard)
- **Единая дизайн-система (`capri/ui/theme.py`, `icons.py`, `widgets.py`, `motion.py`):**
  - Графитовые поверхности **Dark Graphite** (`#0A0B0F` / `#0E1016` / `#141720`), акцент **Crimson Red** (`#E12D53`), системный **Cyan** (`#4ED8E8`) и инструментальный **Amber** (`#E8AD52`), векторные HiDPI SVG-иконки и режим `reduced_motion`.
  - **Cyberdeck:** многострочный композер (`Enter` / `Shift+Enter`), токенный стриминг, скриншоты из буфера (`Ctrl+V`) и Drag & Drop файлов, группировка tool-пилюль, сворачиваемый блок рассуждений, адаптивный сайдбар/drawer для узких тайлов Niri/Hyprland, поиск по полной истории (`Ctrl+K`, FTS5 + Unicode `casefold`), откат/редактирование раундов и кнопка полного выключения в нижней панели сайдбара.
  - **Relic Construct Dashboard:** живая телеметрия синхронизации энграммы, настроения, доверия, банка памяти, активных инструментов и переключения режимов поведения (`Normal / Focus / Playful / Quiet`).

### ⚙️ Практичный центр настроек API, голоса и зрения
- **4-вкладочное окно настроек (`Нейроядро`, `Голос и аудио`, `Зрение и API`, `Интерфейс и безопасность`):**
  - Выбор LLM-провайдера (`DeepSeek`, `Groq`, `Google Gemini`, `SambaNova`, `OpenRouter`, `OpenAI`) и кастомного `Base URL` с автоматической очисткой ключа в памяти и `.env` при смене провайдера или URL.
  - Выбор движков прямо через UI: **TTS** (`Fish Audio`, `Edge`, `ElevenLabs`, `Gemini TTS`, `Silero`), **STT** (`Groq Whisper Turbo`, `Local Whisper`), **Wake-Word** и **Vision** (`Gemini Vision` / `OCR`).
  - Ввод, просмотр, вставка, явное удаление из `.env` (`0600`) и живая проверка кнопкой **«Проверить API»** для ключей LLM, Fish Audio (+ Voice ID), ElevenLabs (+ Voice ID) и Gemini Vision.

### 🛡️ Безопасность и политика инструментов (`SAFE / ASK / TRUSTED`)
- Централизованный fail-closed `ToolPolicyEngine`: режимы `SAFE`, `ASK` (по умолчанию) и `TRUSTED`, подтверждение опасных/мутирующих операций (`CapriToolApprovalDialog`), безусловная блокировка разрушающих паттернов.
- Защита shell и файловой системы: блокировка glob-обходов и опасных флагов `git`, защита симлинков и чувствительных путей (`.env`, `~/.ssh`, `~/.gnupg`, `~/.config/capri`, автозапуск, `.git/hooks`), потоковый лимит вывода подпроцессов (`64 KiB`) и лимит чтения файлов.
- SSRF-защита веб-инструментов и изолированного браузера («Her Own Browser», `httpx` + опциональный headless Chromium `--js`) с проверкой редиректов и DNS.
- Безопасность **MCP (stdio JSON-RPC 2.0):** асинхронное неблокирующее `/mcp connect` с отменой и таймаутом, префикс `mcp__*` без перезаписи встроенных инструментов, лимит фрейма `1 MiB`, экранирование границ вывода и редакция секретов `mcp.servers[*].env` в ошибках и `stderr`.
- Безопасный `/bugreport` и диагностика `/doctor`: автоматическая редакция API-ключей, токенов, JWT, URL-секретов и домашних путей; история чатов, память и `.env` никогда не включаются в отчёт.
- Документирована политика раскрытия уязвимостей в [`SECURITY.md`](SECURITY.md).

### 🎙️ Голос, память, десктоп и окружение
- **Потоковый TTS + Barge-In + Join Voice:** sentence-streaming озвучка, hands-free перебивание (VAD), поддержка PipeWire WebRTC AEC, калибровка микрофона и виртуальный микрофон PipeWire (`/call join`).
- **Память и непрерывность:** SQLite-сессии с FTS5 и роллинг-сжатием, **Memory Workbench** (`/memory`), эмоциональный движок с межсессионной памятью, контекст и инициатива, архив/восстановление профиля (`/profile export` / `restore`).
- **Интеграции Wayland (Niri / Hyprland):** 2D/3D desktop-аватар, MPRIS авто-позы, Game Mode / Power Saver, генератор оконных правил (`/wm-rules`), системный трей и `capri-deck --toggle`.

### 📦 Установка, упаковка и проверка качества
- Полная поддержка `XDG_CONFIG_HOME` / `XDG_DATA_HOME`, чистая установка `./setup.sh`, безопасное обновление `./setup.sh --upgrade` (с pre-upgrade снимком профиля) и удаление `./setup.sh --uninstall [--purge]`.
- Разделение лицензий: исходный код под **MIT** ([`LICENSE`](LICENSE)), некоммерческие условия и атрибуция ассетов в [`ASSETS_NOTICE.md`](ASSETS_NOTICE.md).
- CI-воркфлоу GitHub Actions, Dependabot, шаблон Bug Report, сборщик релизов `scripts/build_beta_release.sh` с проверкой `SHA256SUMS.txt` и скрипт верификации установки `scripts/verify_open_beta_install.sh`.
- **Верификация релиза v3.1.0:** полный прогон автотестов — **200/200 passed**, изолированная проверка чистой установки, обновления и удаления из релизного архива — **PASSED**.
