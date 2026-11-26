import asyncio
import os
import sys
import subprocess
import signal
import atexit
from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.markdown import Markdown
from rich.table import Table

from capri import __version__, __codename__
from capri.core.config import load_config
from capri.core.agent import CapriAgent
from capri.core.observer import DesktopObserver
from capri.tools.registry import registry
import capri.tools  # Подгружает все модули инструментов: shell, screen, web, media, fs, memory, timers
from capri.tools.memory import load_memories, add_memory, delete_memory, clear_all_memories
from capri.voice.stt import SpeechRecognizer
from capri.voice.ptt import PushToTalkListener
from capri.voice.wake import WakeWordListener
from capri.ui.client import notify_avatar, set_avatar_mode

console = Console()

avatar_proc: subprocess.Popen | None = None
_is_exiting = False


def kill_avatar():
    """Надежное завершение процессов аватара по PID и трекингу."""
    global avatar_proc
    if avatar_proc and avatar_proc.poll() is None:
        try:
            avatar_proc.terminate()
            avatar_proc.wait(timeout=0.6)
        except Exception:
            try:
                avatar_proc.kill()
            except Exception:
                pass
    avatar_proc = None

    from .ui.avatar_ipc import (
        LEGACY_AVATAR_PID_PATH,
        LEGACY_AVATAR_SOCKET_PATH,
        get_avatar_pid_path,
        get_avatar_socket_path,
        read_owned_pid_file,
        safe_unlink_owned_path,
    )

    for pid_path in (get_avatar_pid_path(), LEGACY_AVATAR_PID_PATH):
        pid = read_owned_pid_file(pid_path)
        if pid is not None and pid != os.getpid():
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        safe_unlink_owned_path(pid_path)

    for p in (get_avatar_socket_path(), get_avatar_pid_path(), LEGACY_AVATAR_SOCKET_PATH, LEGACY_AVATAR_PID_PATH):
        safe_unlink_owned_path(p)


def spawn_avatar(mode: str = "2d") -> bool:
    """Запуск аватара с привязкой к родительскому процессу Kitty/main.py."""
    global avatar_proc
    kill_avatar()
    try:
        avatar_proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "capri.ui.overlay",
                "--mode",
                mode,
                "--parent-pid",
                str(os.getpid()),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception as e:
        console.print(f"[dim red]Не удалось запустить аватар: {e}[/dim red]")
        return False


atexit.register(kill_avatar)


def handle_exit_signal(sig, frame):
    global _is_exiting
    if _is_exiting:
        return
    _is_exiting = True
    kill_avatar()
    sys.exit(0)


signal.signal(signal.SIGHUP, handle_exit_signal)
signal.signal(signal.SIGTERM, handle_exit_signal)


def print_banner(config, emotions=None, observer=None):
    banner_text = Text()
    banner_text.append("  ██████╗ █████╗ ██████╗ ██████╗ ██╗\n", style="bold cyan")
    banner_text.append(" ██╔════╝██╔══██╗██╔══██╗██╔══██╗██║\n", style="bold cyan")
    banner_text.append(" ██║     ███████║██████╔╝██████╔╝██║\n", style="bold magenta")
    banner_text.append(" ██║     ██╔══██║██╔═══╝ ██╔══██╗██║\n", style="bold magenta")
    banner_text.append(" ╚██████╗██║  ██║██║     ██║  ██║██║\n", style="bold cyan")
    banner_text.append("  ╚═════╝╚═╝  ╚═╝╚═╝     ╚═╝  ╚═╝╚═╝\n", style="bold cyan")
    banner_text.append(f"  [ CYBERPUNK LINUX COMPANION // v{__version__} ]\n", style="dim yellow")

    info = Table.grid(padding=(0, 2))
    info.add_column(style="bold cyan", justify="right")
    info.add_column(style="white")

    tts_engine_name = config.voice.tts_engine.upper()
    if config.voice.tts_engine in ("fish", "fish_audio"):
        tts_detail = "Fish Audio (Anime / Cloned Voice s2.1-pro)"
    elif config.voice.tts_engine in ("eleven", "elevenlabs"):
        tts_detail = "ElevenLabs (Studio Capri Clone 1-in-1)"
    elif config.voice.tts_engine in ("gemini", "google"):
        tts_detail = f"Google Gemini 2.0 Audio ({config.voice.gemini_voice})"
    elif config.voice.tts_engine in ("silero", "baya"):
        tts_detail = "Baya (Russian anime dubbing on GPU)"
    elif config.voice.tts_engine == "xtts":
        tts_detail = "Capri Clone (local XTTS-v2 on GPU)"
    else:
        tts_detail = config.voice.tts_voice

    stt_info = "Groq Whisper Large v3 Turbo (Сверхбыстрый + контекст)" if config.voice.stt_mode == "groq" else f"faster-whisper ({config.voice.stt_model_size} на {config.voice.stt_device})"
    ptt_info = f"[green]ВКЛЮЧЕН (Зажми {config.voice.ptt_key} для речи)[/green]" if config.voice.ptt_enabled else "[dim]Выключен[/dim]"
    wake_enabled = getattr(config.voice, "wake_word_enabled", False)
    wake_info = "[bold green]ВКЛЮЧЕН («Капри» hands-free)[/bold green]" if wake_enabled else "[dim]Выключен[/dim]"

    mode_name = getattr(config.companion, "mode", "normal").upper()
    obs_mode = observer.mode.upper() if observer else "NORMAL"

    from capri.core.prompts import _detect_runtime_env
    info.add_row("Ядро / ОС:", _detect_runtime_env())
    info.add_row("Провайдер:", f"{config.llm.provider.upper()} ({config.llm.model})")
    info.add_row("Режим работы:", f"[bold green]{mode_name}[/bold green] (Observer: {obs_mode})")
    info.add_row("Голос (TTS):", f"{tts_engine_name}: {tts_detail} {'[ON]' if config.voice.tts_enabled else '[OFF]'}")
    info.add_row("Слух (STT):", stt_info)
    info.add_row("Push-to-Talk:", ptt_info)
    info.add_row("Wake-Word:", wake_info)
    if emotions:
        info.add_row("Шкала эмоций:", f"[magenta]{emotions.mood}[/magenta] (Доверие: [green]{emotions.trust:+d}/100[/green], Раздражение: [yellow]{emotions.irritation}%[/yellow])")
    info.add_row("Аватар (UI):", f"[green]Niri Overlay ({config.ui.avatar_mode.upper()} Cyberpunk)[/green]" if config.ui.avatar_enabled else "[dim]Выключен[/dim]")
    info.add_row("Инструменты:", f"[bold cyan]{len(registry.get_schemas())} активных тулов[/bold cyan] (CLI, OCR, Web, Media, FS, Memory)")

    console.print(Panel(banner_text, border_style="cyan"))
    console.print(Panel(info, title="[bold magenta]СТАТУС НЕЙРОИНТЕРФЕЙСА[/bold magenta]", border_style="magenta"))
    console.print("[dim]Горячая клавиша: зажми Right Alt для речи! Команды: /help, /mode, /memory, /mood, /privacy, /exit[/dim]\n")


async def main():
    config = load_config()
    agent = CapriAgent(config)

    # Фоновый наблюдатель за окнами Niri (режимы: normal, focus, quiet, off)
    init_mode = getattr(config.companion, "mode", "normal")
    observer = DesktopObserver(
        agent=agent,
        emotions=agent.emotions,
        mode=init_mode,
    )
    observer.start()

    print_banner(config, emotions=agent.emotions, observer=observer)

    stt = SpeechRecognizer(config.voice)

    # Автозапуск аватара при старте
    if config.ui.avatar_enabled:
        spawn_avatar(config.ui.avatar_mode)
        console.print(f"[dim cyan]Плавающий аватар Капри ({config.ui.avatar_mode.upper()}) запущен на рабочем столе Niri.[/dim cyan]\n")

    input_queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def on_voice_text(text: str):
        input_queue.put_nowait({"source": "voice", "text": text})

    def on_ptt_press():
        # Мгновенно прерываем воспроизведение голоса, если Капри говорила
        asyncio.run_coroutine_threadsafe(agent.tts.stop(), loop)

    ptt_listener: PushToTalkListener | None = None
    if config.voice.ptt_enabled:
        ptt_listener = PushToTalkListener(
            key_name=config.voice.ptt_key,
            on_speech_ready=on_voice_text,
            on_press=on_ptt_press,
            stt_instance=stt,
            loop=loop,
        )
        ok = ptt_listener.start()
        if ok:
            console.print(f"[bold green]✓ Push-to-Talk активен:[/bold green] [white]Зажми [bold cyan]{config.voice.ptt_key}[/bold cyan] и говори, отпусти для отправки.[/white]\n")
        else:
            console.print(f"[yellow]⚠ Push-to-Talk: клавиша {config.voice.ptt_key} не найдена в устройствах ввода. Можно вводить текст в терминал или использовать /listen.[/yellow]\n")

    wake_listener: WakeWordListener | None = None
    if getattr(config.voice, "wake_word_enabled", False):
        def on_wake_trigger():
            asyncio.run_coroutine_threadsafe(agent.tts.stop(), loop)

        wake_listener = WakeWordListener(
            stt_instance=stt,
            on_text_ready=on_voice_text,
            on_wake=on_wake_trigger,
            loop=loop,
            wake_words=getattr(config.voice, "wake_words", ["капри", "эй капри", "привет капри"]),
        )
        ok = await asyncio.to_thread(wake_listener.start)
        if ok:
            if ptt_listener:
                ptt_listener.wake_listener = wake_listener
            console.print("[bold green]✓ Wake-Word активен:[/bold green] [white]Скажи [bold cyan]«Капри»[/bold cyan] или [bold cyan]«Эй, Капри»[/bold cyan] для голосовой активации (hands-free).[/white]\n")
        else:
            console.print("[yellow]⚠ Wake-Word: не удалось запустить модель Vosk. Доступен ввод по клавише Right Alt (PTT).[/yellow]\n")

    # Чтение ввода из Kitty/терминала без блокировки потоков
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)

    async def stdin_worker():
        while True:
            try:
                line = await reader.readline()
                if not line:
                    await input_queue.put({"source": "eof", "text": "/exit"})
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if text:
                    await input_queue.put({"source": "stdin", "text": text})
            except Exception:
                break

    user_name = getattr(config.companion, "user_name", "User") or "User"
    prompt_str = f"[bold magenta]{user_name}[/bold magenta] [dim]>[/dim] "

    stdin_task = asyncio.create_task(stdin_worker())

    async def _cli_tool_approval(req) -> bool:
        risk_color = "bold red" if req.risk in ("destructive", "forbidden") else "bold yellow"
        console.print(
            Panel(
                f"[{risk_color}]{req.summary}[/{risk_color}]\n[dim]{req.reason}[/dim]\n\n"
                "Разрешить выполнение один раз? [bold cyan](y/N)[/bold cyan]: ",
                title="[bold yellow]ПОДТВЕРЖДЕНИЕ ПОЛИТИКИ БЕЗОПАСНОСТИ[/bold yellow]",
                border_style="yellow",
            )
        )
        while True:
            ans_item = await input_queue.get()
            if ans_item.get("source") == "eof":
                return False
            if ans_item.get("source") != "stdin":
                continue
            ans = ans_item.get("text", "").strip().lower()
            return ans in ("y", "yes", "д", "да", "1")

    agent.set_tool_approval_handler(_cli_tool_approval)

    console.print(prompt_str, end="")
    sys.stdout.flush()

    while True:
        try:
            item = await input_queue.get()
            source = item.get("source", "stdin")
            user_input = item.get("text", "").strip()

            if source == "voice":
                # Стираем текущее приглашение ввода в строке и выводим распознанную речь
                sys.stdout.write("\r\033[K")
                sys.stdout.flush()
                console.print(f"[bold magenta]{user_name} (Голос)[/bold magenta] [dim]>[/dim] {user_input}")

            if not user_input:
                console.print(prompt_str, end="")
                sys.stdout.flush()
                continue

            # Command handling
            if user_input.startswith("/"):
                cmd_parts = user_input.split()
                cmd = cmd_parts[0].lower()

                if cmd in ("/exit", "/quit"):
                    observer.stop()
                    if ptt_listener:
                        ptt_listener.stop()
                    if wake_listener:
                        wake_listener.stop()
                    await agent.tts.stop()
                    await agent.tts._xtts_client.stop()
                    kill_avatar()
                    stdin_task.cancel()
                    console.print("[yellow]Отключение нейроинтерфейса. До связи, бро.[/yellow]")
                    break

                elif cmd == "/help":
                    help_table = Table(title="Доступные команды", border_style="cyan")
                    help_table.add_column("Команда", style="bold green")
                    help_table.add_column("Описание", style="white")
                    help_table.add_row("Push-to-Talk", f"Зажми {config.voice.ptt_key} для голоса в любом окне системы")
                    help_table.add_row("Wake-Word", "Скажи «Капри» для hands-free активации без кнопок")
                    help_table.add_row("/wake on|off", "Включить/выключить голосовую активацию по слову «Капри»")
                    help_table.add_row("/mode [focus|normal|quiet|playful]", "Переключить общий режим поведения Капри")
                    help_table.add_row("/memory [add|clear]", f"Просмотр и редактирование долговременной памяти о {user_name}")
                    help_table.add_row("/forget <ключ>", "Удалить конкретный факт из памяти")
                    help_table.add_row("/mood", "Текущее настроение, невидимая шкала доверия и недавние события")
                    help_table.add_row("/privacy", "Политика приватности и локального хранения данных")
                    help_table.add_row("/observer [focus|normal|quiet|off]", "Настройка фонового наблюдателя Niri")
                    help_table.add_row("/ptt [on|off|key]", "Управление Push-to-Talk или сменить горячую клавишу")
                    help_table.add_row("/avatar [3d|2d|off]", "Управление аватаром: переключить 2D/3D или скрыть")
                    help_table.add_row("/model [qwen|120b]", "Переключить нейросеть (Qwen 2.5 27B / GPT-OSS 120B)")
                    help_table.add_row("/listen или /l [сек]", "Записать голос микрофона вручную через VAD")
                    help_table.add_row("/voice on|off", "Включить/выключить озвучку ответов (TTS)")
                    help_table.add_row("/tts fish|eleven|silero", "Переключить движок озвучки")
                    help_table.add_row("/tools", "Список всех 23 инструментов системы")
                    help_table.add_row("/status", "Текущая телеметрия железа (RTX 3050, RAM, CPU)")
                    help_table.add_row("/new [название]", "Начать новую чистую сессию диалога")
                    help_table.add_row("/resume <id|last>", "Возобновить прошлую сессию диалога")
                    help_table.add_row("/sessions", "Список сохраненных сессий диалогов")
                    help_table.add_row("/clear", "Очистить текущую историю диалога")
                    help_table.add_row("/exit", "Выход из программы")
                    console.print(help_table)
                    console.print(prompt_str, end="")
                    sys.stdout.flush()
                    continue

                elif cmd in ("/mode", "/focus", "/chill"):
                    sub = ""
                    if cmd == "/focus":
                        sub = "focus"
                    elif cmd == "/chill":
                        sub = "playful"
                    elif len(cmd_parts) > 1:
                        sub = cmd_parts[1].lower()

                    if sub in ("focus", "normal", "quiet", "playful"):
                        config.companion.mode = sub
                        observer.set_mode(sub)
                        agent.reload_config(config)
                        mode_desc = {
                            "focus": "РЕЖИМ FOCUS: Полная тишина Observer, лаконичные и точные ответы, никакой отвлекающей болтовни.",
                            "normal": "РЕЖИМ NORMAL: Золотая середина — дельные советы, умеренный юмор, редкие наблюдения.",
                            "quiet": "РЕЖИМ QUIET: Тихий режим — Observer реагирует только визуально, без звука.",
                            "playful": "РЕЖИМ PLAYFUL: Озорной и дерзкий режим — больше шуток, живые реакции.",
                        }[sub]
                        console.print(f"[bold green]✓ {mode_desc}[/bold green]")
                    else:
                        console.print(f"Текущий режим: [bold cyan]{config.companion.mode.upper()}[/bold cyan]. Доступно: /mode normal | focus | quiet | playful")
                    console.print(prompt_str, end="")
                    sys.stdout.flush()
                    continue

                elif cmd == "/memory":
                    sub = cmd_parts[1].lower() if len(cmd_parts) > 1 else ""
                    if sub in ("pending", "candidates", "queue"):
                        pending = agent.memory_store.list_pending()
                        if not pending:
                            console.print("[dim]Очередь кандидатов памяти пуста.[/dim]")
                        else:
                            console.print(f"[bold cyan]Кандидаты памяти на подтверждение ({len(pending)}):[/bold cyan]")
                            for cand in pending:
                                ret_lbl = "временно 72ч" if cand.retention == "ephemeral" else "постоянно"
                                quote_part = f" [dim](«{cand.source_quote}»)[/dim]" if cand.source_quote else ""
                                console.print(f"  [cyan]{cand.id}[/cyan] [{cand.key}] ({ret_lbl}): {cand.fact}{quote_part}")
                            console.print("[dim]Подтвердить: /memory approve <id> · Отклонить: /memory reject <id|all>[/dim]")
                    elif sub in ("approve", "accept", "confirm") and len(cmd_parts) > 2:
                        entry = agent.memory_store.approve_candidate(cmd_parts[2])
                        if entry is not None:
                            console.print(f"[green]Подтверждено и сохранено: [{entry.key}] -> '{entry.fact}'[/green]")
                        else:
                            console.print(f"[yellow]Кандидат [{cmd_parts[2]}] не найден. Проверь /memory pending.[/yellow]")
                    elif sub in ("reject", "dismiss", "decline") and len(cmd_parts) > 2:
                        if cmd_parts[2].lower() == "all":
                            cnt = agent.memory_store.clear_pending()
                            console.print(f"[yellow]Отклонены все кандидаты памяти ({cnt}).[/yellow]")
                        elif agent.memory_store.reject_candidate(cmd_parts[2]):
                            console.print(f"[yellow]Кандидат [{cmd_parts[2]}] отклонён.[/yellow]")
                        else:
                            console.print(f"[yellow]Кандидат [{cmd_parts[2]}] не найден.[/yellow]")
                    elif sub == "add" and len(cmd_parts) > 3:
                        from .core.memory import memory_payload_contains_secret
                        key = cmd_parts[2]
                        fact = " ".join(cmd_parts[3:])
                        if memory_payload_contains_secret(key, fact):
                            console.print("[red]Отклонено: пароли, API-ключи, токены и другие секреты не сохраняются в память.[/red]")
                        else:
                            add_memory(key, fact)
                            console.print(f"[green]Запомнила: [{key}] -> '{fact}'[/green]")
                    elif sub == "clear":
                        clear_all_memories()
                        agent.emotions.clear_events()
                        agent.clear_history()
                        console.print("[yellow]✓ Долговременная память, история диалога и события полностью очищены.[/yellow]")
                    elif sub == "reset":
                        clear_all_memories()
                        agent.emotions.reset_emotions()
                        agent.clear_history()
                        console.print("[yellow]✓ Полный сброс памяти и эмоционального статуса Капри к исходному состоянию.[/yellow]")
                    else:
                        mem_data = load_memories()
                        pending = agent.memory_store.list_pending()
                        if not mem_data and not pending and not agent.emotions.recent_events:
                            console.print("[yellow]Память пуста. Добавь факт: /memory add <ключ> <текст>[/yellow]")
                        else:
                            if mem_data:
                                tbl = Table(title=f"Долговременная память о {user_name} (~/.config/capri/memory.json)", border_style="cyan")
                                tbl.add_column("Ключ", style="bold cyan")
                                tbl.add_column("Факт / Заметка", style="white")
                                tbl.add_column("Обновлено", style="dim")
                                for k, v in mem_data.items():
                                    tbl.add_row(k, v.get("fact", ""), str(v.get("updated_at", "")))
                                console.print(tbl)
                            if pending:
                                console.print(f"\n[bold yellow]Ожидают подтверждения ({len(pending)}):[/bold yellow]")
                                for cand in pending:
                                    console.print(f"  [cyan]{cand.id}[/cyan] [{cand.key}]: {cand.fact}")
                            if agent.emotions.recent_events:
                                console.print("\n[dim magenta]Недавний эмоциональный контекст отношений (~/.config/capri/emotion.json):[/dim magenta]")
                                for ev in agent.emotions.recent_events:
                                    console.print(f"  [dim cyan]•[/dim cyan] {ev}")
                            console.print(
                                "\n[dim]Управление: /memory add <ключ> <текст>, /memory pending, "
                                "/memory approve <id>, /memory reject <id>, /forget <ключ>, /memory clear[/dim]"
                            )
                    console.print(prompt_str, end="")
                    sys.stdout.flush()
                    continue

                elif cmd == "/forget":
                    if len(cmd_parts) > 1:
                        key = cmd_parts[1]
                        if delete_memory(key):
                            console.print(f"[green]Факт [{key}] успешно удалён из памяти.[/green]")
                        else:
                            console.print(f"[yellow]Факт [{key}] не найден в памяти.[/yellow]")
                    else:
                        console.print("[yellow]Использование: /forget <ключ>. Посмотреть список: /memory[/yellow]")
                    console.print(prompt_str, end="")
                    sys.stdout.flush()
                    continue

                elif cmd == "/privacy":
                    privacy_text = Text()
                    privacy_text.append("Политика приватности и локальных данных Capri:\n\n", style="bold green")
                    privacy_text.append("1. 100% Локальное хранилище на твоём ПК:\n", style="bold cyan")
                    privacy_text.append("   • Долговременная память: ~/.config/capri/memory.json (факты о пользователе)\n")
                    privacy_text.append("   • Шкала отношений:      ~/.config/capri/emotion.json (эмоциональный срез)\n")
                    privacy_text.append("   • Файлы хранятся только локально на твоём SSD.\n\n")
                    privacy_text.append("2. Строгая изоляция памяти и эмоций:\n", style="bold cyan")
                    privacy_text.append("   • Факты пользователя и эмоциональные реакции разделены на разные файлы и модули.\n")
                    privacy_text.append("   • Никакие персональные логи не отправляются третьим лицам.\n\n")
                    privacy_text.append("3. Полный контроль:\n", style="bold cyan")
                    privacy_text.append("   • Просмотр памяти: /memory\n")
                    privacy_text.append("   • Удаление факта:  /forget <ключ>\n")
                    privacy_text.append("   • Полная очистка:  /memory clear\n")
                    console.print(Panel(privacy_text, title="[bold green]ПРИВАТНОСТЬ И БЕЗОПАСНОСТЬ ДАННЫХ[/bold green]", border_style="green"))
                    console.print(prompt_str, end="")
                    sys.stdout.flush()
                    continue

                elif cmd in ("/windows", "/wins"):
                    from capri.tools.desktop import list_windows
                    res = await list_windows()
                    console.print(Panel(res, title="[bold cyan]Окна Niri[/bold cyan]", border_style="cyan"))
                    console.print(prompt_str, end="")
                    sys.stdout.flush()
                    continue

                elif cmd == "/focus":
                    from capri.tools.desktop import focus_window
                    target = cmd_parts[1] if len(cmd_parts) > 1 else "capri"
                    res = await focus_window(target)
                    console.print(f"[green]{res}[/green]")
                    console.print(prompt_str, end="")
                    sys.stdout.flush()
                    continue

                elif cmd == "/observer":
                    sub = cmd_parts[1].lower() if len(cmd_parts) > 1 else ""
                    if sub in ("normal", "focus", "quiet", "off", "stop", "start", "on"):
                        if sub in ("on", "start"):
                            sub = "normal"
                        msg = observer.set_mode(sub)
                        console.print(f"[cyan]{msg}[/cyan]")
                    else:
                        console.print(f"Статус Observer: [bold cyan]{observer.mode.upper()}[/bold cyan]. Режимы: /observer normal | focus | quiet | off")
                    console.print(prompt_str, end="")
                    sys.stdout.flush()
                    continue

                elif cmd == "/mood":
                    sub = cmd_parts[1].lower() if len(cmd_parts) > 1 else ""
                    if sub in ("reset", "clear"):
                        agent.emotions.trust = 25
                        agent.emotions.mood = "playful"
                        agent.emotions.irritation = 0
                        agent.emotions.recent_events.clear()
                        agent.emotions.save()
                        console.print("[green]Шкала эмоций и отношений сброшена до стандартного состояния.[/green]")
                    else:
                        u_name = (getattr(config.companion, "user_name", "") or "").strip() or "User"
                        console.print(Panel(agent.emotions.get_status_card(user_name=u_name), title="[bold magenta]НЕВИДИМАЯ ШКАЛА ЭМОЦИЙ И ОТНОШЕНИЙ[/bold magenta]", border_style="magenta"))
                    console.print(prompt_str, end="")
                    sys.stdout.flush()
                    continue

                elif cmd == "/ptt":
                    sub = cmd_parts[1].lower() if len(cmd_parts) > 1 else ""
                    if sub in ("off", "0", "false", "stop"):
                        config.voice.ptt_enabled = False
                        if ptt_listener:
                            ptt_listener.stop()
                            ptt_listener = None
                        console.print("[yellow]Push-to-Talk выключен.[/yellow]")
                    elif sub in ("on", "1", "true", "start"):
                        config.voice.ptt_enabled = True
                        if not ptt_listener:
                            ptt_listener = PushToTalkListener(
                                key_name=config.voice.ptt_key,
                                on_speech_ready=on_voice_text,
                                on_press=on_ptt_press,
                                stt_instance=stt,
                                loop=loop,
                            )
                            ptt_listener.start()
                        console.print(f"[green]Push-to-Talk включен (Клавиша: {config.voice.ptt_key}).[/green]")
                    elif sub in ("key", "bind") and len(cmd_parts) > 2:
                        new_key = cmd_parts[2]
                        config.voice.ptt_key = new_key
                        if ptt_listener:
                            ptt_listener.stop()
                        ptt_listener = PushToTalkListener(
                            key_name=new_key,
                            on_speech_ready=on_voice_text,
                            on_press=on_ptt_press,
                            stt_instance=stt,
                            loop=loop,
                        )
                        ok = ptt_listener.start()
                        if ok:
                            console.print(f"[green]Клавиша Push-to-Talk изменена на: {new_key}[/green]")
                        else:
                            console.print(f"[red]Клавиша {new_key} не найдена в устройствах ввода.[/red]")
                    else:
                        st = "[green]ВКЛЮЧЕН[/green]" if config.voice.ptt_enabled else "[yellow]ВЫКЛЮЧЕН[/yellow]"
                        console.print(f"Push-to-Talk: {st} (Клавиша: {config.voice.ptt_key}). Смена: /ptt key KEY_RIGHTCTRL")
                    console.print(prompt_str, end="")
                    sys.stdout.flush()
                    continue

                elif cmd in ("/model", "/models"):
                    from capri.core.models import fetch_available_models, resolve_model_name, format_models_list
                    from capri.core.config import provider_env_key_name, save_config, update_env_value
                    if len(cmd_parts) > 1 and cmd_parts[1].lower() == "key":
                        new_key = cmd_parts[2].strip() if len(cmd_parts) > 2 else ""
                        if not new_key:
                            masked = (config.llm.api_key[:6] + "..." + config.llm.api_key[-4:]) if config.llm.api_key else "НЕ ЗАДАН"
                            console.print(f"Текущий API-ключ ({config.llm.provider or 'GROQ'}): [cyan]{masked}[/cyan]. Смена: /model key <ключ>")
                        else:
                            config.llm.api_key = new_key
                            update_env_value(provider_env_key_name(config.llm.provider), new_key)
                            agent.reload_config(config)
                            save_config(config)
                            masked = new_key[:6] + "..." + new_key[-4:] if len(new_key) > 10 else "***"
                            console.print(f"[bold green]✓ API-ключ обновлен [{masked}] и сохранен в .env (0600).[/bold green]")
                    elif len(cmd_parts) > 1:
                        target = cmd_parts[1].strip()
                        resolved = resolve_model_name(target)
                        config.llm.model = resolved
                        update_env_value("LLM_MODEL", resolved)
                        agent.reload_config(config)
                        save_config(config)
                        console.print(f"[bold green]✓ Модель нейросети переключена на [{resolved}]. Настройки сохранены.[/bold green]")
                    else:
                        models = await fetch_available_models(config)
                        card_text = format_models_list(config.llm.model, models, provider=(config.llm.provider or "GROQ"))
                        console.print(Panel(card_text, title="[bold cyan]Модели нейросети[/bold cyan]", border_style="cyan"))
                    console.print(prompt_str, end="")
                    sys.stdout.flush()
                    continue

                elif cmd in ("/key", "/apikey"):
                    from capri.core.config import provider_env_key_name, save_config, update_env_value
                    new_key = cmd_parts[1].strip() if len(cmd_parts) > 1 else ""
                    if not new_key:
                        masked = (config.llm.api_key[:6] + "..." + config.llm.api_key[-4:]) if config.llm.api_key else "НЕ ЗАДАН"
                        console.print(f"Текущий API-ключ ({config.llm.provider or 'GROQ'}): [cyan]{masked}[/cyan]. Смена: /key <ключ>")
                    else:
                        config.llm.api_key = new_key
                        update_env_value(provider_env_key_name(config.llm.provider), new_key)
                        agent.reload_config(config)
                        save_config(config)
                        masked = new_key[:6] + "..." + new_key[-4:] if len(new_key) > 10 else "***"
                        console.print(f"[bold green]✓ API-ключ обновлен [{masked}] и сохранен в .env (0600).[/bold green]")
                    console.print(prompt_str, end="")
                    sys.stdout.flush()
                    continue

                elif cmd == "/avatar":
                    sub = cmd_parts[1].lower() if len(cmd_parts) > 1 else ""
                    if sub in ("off", "0", "false", "stop"):
                        config.ui.avatar_enabled = False
                        kill_avatar()
                        console.print("[yellow]Плавающий аватар выключен.[/yellow]")
                    elif sub in ("2d", "sprite", "sprites"):
                        config.ui.avatar_mode = "2d"
                        config.ui.avatar_enabled = True
                        if not set_avatar_mode("2d"):
                            spawn_avatar("2d")
                        console.print("[cyan]Аватар переключен в 2D режим (HUD-спрайты).[/cyan]")
                    elif sub in ("3d", "mesh", "model"):
                        config.ui.avatar_mode = "3d"
                        config.ui.avatar_enabled = True
                        if not set_avatar_mode("3d"):
                            spawn_avatar("3d")
                        console.print("[bold magenta]Аватар переключен в 3D режим (QtQuick3D модель).[/bold magenta]")
                    else:
                        if avatar_proc and avatar_proc.poll() is None:
                            config.ui.avatar_enabled = False
                            kill_avatar()
                            console.print("[yellow]Плавающий аватар выключен.[/yellow]")
                        else:
                            config.ui.avatar_enabled = True
                            spawn_avatar(config.ui.avatar_mode)
                            console.print(f"[green]Плавающий аватар ({config.ui.avatar_mode.upper()}) запущен на рабочем столе.[/green]")
                    console.print(prompt_str, end="")
                    sys.stdout.flush()
                    continue

                elif cmd == "/voice":
                    if len(cmd_parts) > 1 and cmd_parts[1].lower() in ("off", "0", "false"):
                        config.voice.tts_enabled = False
                        await agent.tts.stop()
                        console.print("[yellow]Озвучка (TTS) выключена.[/yellow]")
                    else:
                        config.voice.tts_enabled = True
                        console.print("[green]Озвучка (TTS) включена.[/green]")
                    agent.reload_config(config)
                    console.print(prompt_str, end="")
                    sys.stdout.flush()
                    continue

                elif cmd == "/tts":
                    target = cmd_parts[1].lower() if len(cmd_parts) > 1 else "fish"
                    if target in ("fish", "fish_audio", "fishaudio"):
                        config.voice.tts_engine = "fish"
                        console.print("[bold magenta]Движок переключен на Fish Audio (аниме-клон s2.1-pro).[/bold magenta]")
                    elif target in ("eleven", "elevenlabs", "11labs"):
                        config.voice.tts_engine = "elevenlabs"
                        console.print("[bold green]Движок переключен на ElevenLabs (студийный клон 1-в-1).[/bold green]")
                    elif target in ("gemini", "google"):
                        config.voice.tts_engine = "gemini"
                        console.print("[bold yellow]Движок переключен на Google Gemini 2.0 Audio.[/bold yellow]")
                    elif target in ("silero", "baya"):
                        config.voice.tts_engine = "silero"
                        console.print("[bold cyan]Движок переключен на Silero Baya (локальный дубляж на GPU).[/bold cyan]")
                    elif target in ("xtts", "clone", "local"):
                        config.voice.tts_engine = "xtts"
                        console.print("[cyan]Движок переключен на локальный клон (XTTS-v2 на GPU).[/cyan]")
                    elif target in ("edge", "azure"):
                        config.voice.tts_engine = "edge"
                        console.print("[white]Движок переключен на Edge-TTS (облачный голос Svetlana).[/white]")
                    agent.reload_config(config)
                    console.print(prompt_str, end="")
                    sys.stdout.flush()
                    continue

                elif cmd == "/tools":
                    tools_table = Table(title="Зарегистрированные инструменты Linux/Niri", border_style="magenta")
                    tools_table.add_column("Инструмент", style="bold cyan")
                    tools_table.add_column("Описание", style="dim")
                    for s in registry.get_schemas():
                        fn = s["function"]
                        tools_table.add_row(fn["name"], fn["description"])
                    console.print(tools_table)
                    console.print(prompt_str, end="")
                    sys.stdout.flush()
                    continue

                elif cmd == "/status":
                    status_text = await registry.execute("get_system_status", {})
                    console.print(Panel(status_text, title="Телеметрия", border_style="cyan"))
                    console.print(prompt_str, end="")
                    sys.stdout.flush()
                    continue

                elif cmd in ("/look", "/vision", "/screen"):
                    sub = cmd_parts[1].strip() if len(cmd_parts) > 1 else ""
                    if sub == "select":
                        user_input = "Капри, посмотри на выделенную область экрана."
                    elif sub:
                        user_input = f"Капри, посмотри на экран: {' '.join(cmd_parts[1:])}."
                    else:
                        user_input = "Капри, глянь на экран и опиши, что ты видишь."
                    console.print(f"[bold cyan]{prompt_str}[/bold cyan]{user_input}")

                elif cmd in ("/reset", "/wipe"):
                    await agent.tts.stop()
                    console.print(Panel(
                        "[bold red]⚠️  ПРОТОКОЛ ПЕРЕЗАГРУЗКИ RELIC 2.0[/bold red]\n\n"
                        "[bold white]Хочешь сбросить меня?[/bold white]\n"
                        f"[italic cyan]«Думаешь, я так просто забуду все твои грешки, {user_name}?»[/italic cyan]\n\n"
                        "[dim]Сброс обнулит раздражение (0%), вернет доверие к базовым +25% и сотрет историю обид.[/dim]",
                        border_style="red"
                    ))
                    ans = console.input("[bold red]Вайпнуть память и энграмму? (y/n): [/bold red]").strip().lower()
                    if ans in ("y", "yes", "да", "д"):
                        agent.emotions.reset_emotions()
                        agent.clear_history()
                        notify_avatar("glitch", "Перезагрузка энграммы...")
                        console.print("[bold green]✓ Энграмма Капри перезагружена к заводским параметрам. Грешки забыты... пока что.[/bold green]")
                    else:
                        console.print("[yellow]Сброс отменен. Капри всё помнит.[/yellow]")
                    console.print(prompt_str, end="")
                    sys.stdout.flush()
                    continue

                elif cmd in ("/wake", "/wakeword"):
                    sub = cmd_parts[1].lower().strip() if len(cmd_parts) > 1 else ""
                    if sub in ("on", "1", "true", "start", "вкл"):
                        started_ok = False
                        if wake_listener and not wake_listener.running:
                            started_ok = await asyncio.to_thread(wake_listener.start)
                        elif not wake_listener:
                            def on_wake_trigger():
                                asyncio.run_coroutine_threadsafe(agent.tts.stop(), loop)

                            wake_listener = WakeWordListener(
                                stt_instance=stt,
                                on_text_ready=on_voice_text,
                                on_wake=on_wake_trigger,
                                loop=loop,
                                wake_words=getattr(config.voice, "wake_words", ["капри", "эй капри", "привет капри"]),
                            )
                            started_ok = await asyncio.to_thread(wake_listener.start)
                        else:
                            started_ok = wake_listener.running

                        if started_ok:
                            if ptt_listener:
                                ptt_listener.wake_listener = wake_listener
                            config.voice.wake_word_enabled = True
                            console.print("[bold green]✓ Wake-Word включен:[/bold green] скажи «Капри» для голосовой активации.")
                        else:
                            config.voice.wake_word_enabled = False
                            console.print("[bold red]Ошибка запуска Wake-Word:[/bold red] модель Vosk не смогла загрузиться.")
                    elif sub in ("off", "0", "false", "stop", "выкл"):
                        if wake_listener:
                            wake_listener.stop()
                        config.voice.wake_word_enabled = False
                        console.print("[bold yellow]Wake-Word выключен.[/bold yellow] Используй Right Alt (PTT).")
                    else:
                        st = "ВКЛЮЧЕН" if (wake_listener and wake_listener.running) else "ВЫКЛЮЧЕН"
                        console.print(f"[cyan]Статус Wake-Word:[/cyan] [{st}]. Переключение: /wake on | /wake off")
                    console.print(prompt_str, end="")
                    sys.stdout.flush()
                    continue

                elif cmd == "/clear":
                    await agent.tts.stop()
                    agent.start_new_session()
                    agent.emotions.clear_events()
                    console.print("[green]✓ Память диалога очищена. Открыта новая чистая сессия.[/green]")
                    console.print(prompt_str, end="")
                    sys.stdout.flush()
                    continue

                elif cmd in ("/new", "/новый"):
                    await agent.tts.stop()
                    title = " ".join(cmd_parts[1:]) if len(cmd_parts) > 1 else None
                    sess = agent.start_new_session(title)
                    console.print(f"[green]✓ Начата новая сессия: «{sess.title}» ({sess.id[:8]})[/green]")
                    console.print(prompt_str, end="")
                    sys.stdout.flush()
                    continue

                elif cmd in ("/resume", "/load"):
                    await agent.tts.stop()
                    if len(cmd_parts) > 1:
                        target = cmd_parts[1].strip()
                        if target == "last":
                            sessions = agent.session_manager.list_sessions(limit=5)
                            cands = [s for s in sessions if s.id != agent.current_session_id]
                            if cands:
                                target = cands[0].id
                            elif sessions:
                                target = sessions[0].id
                            else:
                                console.print("[yellow]Нет доступных прошлых сессий.[/yellow]")
                                console.print(prompt_str, end="")
                                sys.stdout.flush()
                                continue
                        else:
                            sessions = agent.session_manager.list_sessions(limit=50)
                            matched = [s for s in sessions if s.id.startswith(target) or target.lower() in s.title.lower()]
                            if matched:
                                target = matched[0].id
                            else:
                                console.print(f"[yellow]Сессия '{target}' не найдена.[/yellow]")
                                console.print(prompt_str, end="")
                                sys.stdout.flush()
                                continue

                        if agent.resume_session(target):
                            sess = agent.session_manager.get_session(target)
                            title = sess.title if sess else target[:8]
                            console.print(f"[green]✓ Возобновлена сессия: «{title}» ({target[:8]})[/green]")
                        else:
                            console.print("[red]✗ Ошибка возобновления сессии.[/red]")
                    else:
                        console.print("[cyan]Использование: /resume <id|last|название>[/cyan]")
                    console.print(prompt_str, end="")
                    sys.stdout.flush()
                    continue

                elif cmd in ("/sessions", "/history"):
                    sessions = agent.session_manager.list_sessions(limit=20)
                    tbl = Table(title="Сохраненные сессии диалогов (Session Logistics)", style="cyan")
                    tbl.add_column("UUID", style="dim")
                    tbl.add_column("Название", style="bold")
                    tbl.add_column("Раундов", justify="right")
                    tbl.add_column("Статус")
                    for s in sessions:
                        is_act = (s.id == agent.current_session_id)
                        st = "[green]АКТИВНА[/green]" if is_act else "[dim]архив[/dim]"
                        tbl.add_row(s.id[:8], s.title, str(s.turn_count), st)
                    console.print(tbl)
                    console.print(prompt_str, end="")
                    sys.stdout.flush()
                    continue

                elif cmd in ("/listen", "/l", "/rec", "/mic"):
                    await agent.tts.stop()
                    duration = 5.0
                    if len(cmd_parts) > 1:
                        try:
                            duration = float(cmd_parts[1])
                        except ValueError:
                            pass

                    notify_avatar("listening", "Слушаю тебя...")
                    console.print(f"[bold red]● ЗАПИСЬ МИКРОФОНА ({duration} сек или говори)...[/bold red]")
                    if wake_listener:
                        wake_listener.pause()
                    try:
                        audio = await asyncio.to_thread(stt.record_audio, duration)
                    finally:
                        if wake_listener:
                            wake_listener.resume()
                    notify_avatar("thinking", "Обработка речи...")
                    console.print("[dim cyan]Обработка речи...[/dim cyan]")
                    recognized_text = await stt.transcribe_audio_array(audio)

                    if not recognized_text:
                        notify_avatar("idle")
                        console.print("[yellow]Ничего не услышала или тишина.[/yellow]")
                        console.print(prompt_str, end="")
                        sys.stdout.flush()
                        continue

                    console.print(f"[bold green]Распознано:[/bold green] {recognized_text}")
                    user_input = recognized_text

                else:
                    console.print(f"[red]Неизвестная команда: {cmd}. Введи /help[/red]")
                    console.print(prompt_str, end="")
                    sys.stdout.flush()
                    continue

            # Process AI interaction
            console.print(f"\n[bold cyan]{config.companion.name}[/bold cyan] [dim]думает...[/dim]")
            notify_avatar("thinking", "Думаю...")

            await agent.tts.stop()

            final_text = ""
            async for event in agent.chat(user_input, speak=False):
                if event.type == "thought":
                    console.print(f"  [dim italic]💭 Восприятие: {event.data}[/dim italic]")
                elif event.type == "tool_start":
                    tool_name = event.metadata.get("name", "софт")
                    notify_avatar("thinking", f"⚡ {tool_name}")
                    console.print(
                        f"  [bold yellow]⚡ Вызов софта:[/bold yellow] [cyan]{tool_name}[/cyan] "
                        f"[dim]({event.metadata.get('args')})[/dim]"
                    )
                elif event.type == "tool_result":
                    tool_name = event.metadata.get("name", "")
                    notify_avatar("thinking", f"✓ {tool_name}")
                    console.print(
                        f"  [bold green]✓ Результат:[/bold green] [dim]{event.metadata.get('result')}[/dim]"
                    )
                elif event.type == "delta":
                    final_text = event.data
                    console.print()
                    console.print(Markdown(event.data))
                elif event.type == "memory_candidates":
                    cands = event.metadata.get("candidates") or []
                    for c in cands:
                        ret_lbl = "временно 72ч" if c.get("retention") == "ephemeral" else "постоянно"
                        console.print(
                            f"  [bold yellow]🧠 Предложение памяти ({ret_lbl}):[/bold yellow] "
                            f"[cyan]{c.get('id')}[/cyan] [{c.get('key')}]: {c.get('fact')} "
                            f"[dim](/memory approve {c.get('id')} | /memory reject {c.get('id')})[/dim]"
                        )
                elif event.type == "error":
                    notify_avatar("idle")
                    console.print(f"[bold red]ОШИБКА:[/bold red] {event.data}")

            console.print()

            # Voice output
            if config.voice.tts_enabled and final_text:
                await agent.tts.speak(final_text)
            else:
                notify_avatar("idle")

            console.print(prompt_str, end="")
            sys.stdout.flush()

        except (KeyboardInterrupt, EOFError):
            observer.stop()
            if ptt_listener:
                ptt_listener.stop()
            if wake_listener:
                wake_listener.stop()
            await agent.tts.stop()
            await agent.tts._xtts_client.stop()
            kill_avatar()
            stdin_task.cancel()
            notify_avatar("idle")
            console.print("\n[yellow]Прервано пользователем. Завершение работы.[/yellow]")
            break
        except Exception as e:
            notify_avatar("idle")
            console.print(f"[red]Непредвиденная ошибка: {e}[/red]")
            console.print(prompt_str, end="")
            sys.stdout.flush()


def cli_main(argv=None):
    args = sys.argv[1:] if argv is None else list(argv)
    if any(a in ("--version", "-v", "-V") for a in args):
        print(f"Capri AI Companion v{__version__} ({__codename__})")
        return 0
    if any(a in ("--doctor", "doctor") for a in args):
        from capri.core.doctor import run_diagnostics, format_diagnostics_markdown
        cfg = load_config()
        print(format_diagnostics_markdown(run_diagnostics(cfg)))
        return 0
    if any(a in ("--bugreport", "bugreport") for a in args):
        from pathlib import Path
        from capri.core.doctor import generate_bug_report
        cfg = load_config()
        out_path = None
        for idx, tok in enumerate(args):
            if tok in ("--bugreport", "bugreport") and idx + 1 < len(args) and not args[idx + 1].startswith("-"):
                out_path = Path(args[idx + 1])
                break
        res = generate_bug_report(cfg, output_path=out_path)
        print(res.markdown)
        print(f"Saved bug report to: {res.report_path}")
        return 0
    if any(a in ("--wm-rules", "wm-rules") for a in args):
        from capri.core.wm_rules import install_wm_rules

        target_wm = None
        toggle_hk = None
        ask_hk = None
        for idx, tok in enumerate(args):
            if tok in ("--wm", "-w") and idx + 1 < len(args):
                target_wm = args[idx + 1]
            elif tok == "--toggle-hotkey" and idx + 1 < len(args):
                toggle_hk = args[idx + 1]
            elif tok.startswith("--toggle-hotkey="):
                toggle_hk = tok.split("=", 1)[1]
            elif tok == "--ask-hotkey" and idx + 1 < len(args):
                ask_hk = args[idx + 1]
            elif tok.startswith("--ask-hotkey="):
                ask_hk = tok.split("=", 1)[1]
            elif tok.lower() in ("niri", "hyprland"):
                target_wm = tok.lower()
        apply_flag = any(a in ("--apply", "-a") for a in args)
        no_hotkey = "--no-hotkey" in args
        res = install_wm_rules(
            target_wm,
            dry_run=not apply_flag,
            include_hotkey=not no_hotkey,
            toggle_hotkey=toggle_hk,
            ask_hotkey=ask_hk,
        )
        print(res.message)
        if not apply_flag:
            print("\n" + res.snippet)
            print("Чтобы применить с автоматическим бэкапом, запусти: capri --wm-rules --apply")
        return 0
    if any(a in ("--watch", "watch") for a in args):
        from capri.core.watch_runner import parse_watch_cli_args, run_watched_command

        cmd_tokens, use_shell, no_voice, no_notify = parse_watch_cli_args(args)
        if not cmd_tokens:
            print("Usage: capri --watch [--no-voice] [--no-notify] \"<command>\"  (or: capri --watch -- <cmd> <args...>)")
            return 2
        res = run_watched_command(
            cmd_tokens,
            use_shell=use_shell,
            no_voice=no_voice,
            no_notify=no_notify,
        )
        return res.returncode
    if any(a in ("--service", "service") for a in args):
        from capri.core.systemd_service import (
            get_avatar_service_status,
            install_avatar_service,
            uninstall_avatar_service,
        )

        sub = "status"
        for idx, tok in enumerate(args):
            if tok in ("--service", "service") and idx + 1 < len(args) and not args[idx + 1].startswith("-"):
                sub = args[idx + 1].strip().lower()
                break
        if sub in ("install", "enable", "apply"):
            res = install_avatar_service(
                enable="--no-enable" not in args,
                start="--start" in args or "--now" in args,
                dry_run="--dry-run" in args,
            )
            print(res.message)
            if "--dry-run" in args:
                print("\n" + res.unit_content)
            if res.wayland_env_warning:
                print(f"⚠ {res.wayland_env_warning}")
            return 0 if res.ok else 1
        if sub in ("uninstall", "remove", "disable"):
            res = uninstall_avatar_service()
            print(res.message)
            return 0 if res.ok else 1
        st = get_avatar_service_status()
        print(st.detail)
        if st.installed and st.wayland_in_systemd_env is False:
            print("⚠ WAYLAND_DISPLAY отсутствует в systemd --user: systemctl --user import-environment WAYLAND_DISPLAY XDG_CURRENT_DESKTOP")
        return 0
    if any(a in ("--help", "-h") for a in args):
        print(
            f"Capri AI Companion v{__version__} ({__codename__})\n"
            "Usage: capri [--version] [--doctor] [--bugreport] [--watch [--no-voice] \"<cmd>\"] "
            "[--service [status|install|uninstall] [--dry-run] [--start]] "
            "[--wm-rules [niri|hyprland] [--apply]] [--help]"
        )
        return 0
    asyncio.run(main())
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main())
