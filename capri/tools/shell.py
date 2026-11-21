from __future__ import annotations

import asyncio
import os
import re
import shlex
from pathlib import Path
from typing import Optional
from .registry import tool
from ..core.policy import FORBIDDEN_SHELL_PATTERNS, is_forbidden_shell_command

# Опасные деструктивные команды, запрещённые всегда (единый источник правды в capri.core.policy)
DANGEROUS_PATTERNS = list(FORBIDDEN_SHELL_PATTERNS)

MAX_SUBPROCESS_OUTPUT_BYTES = 64 * 1024  # 64 KiB hard cap per stream to prevent OOM DoS


async def _terminate_proc_safely(proc: asyncio.subprocess.Process) -> None:
    import os
    import signal

    if proc.returncode is not None:
        return

    pid = getattr(proc, "pid", None)
    use_pgroup = False
    if isinstance(pid, int) and pid > 0 and hasattr(os, "killpg"):
        try:
            use_pgroup = os.getpgid(pid) == pid and pid != os.getpgrp()
        except Exception:
            use_pgroup = False

    try:
        if use_pgroup:
            os.killpg(pid, signal.SIGTERM)
        else:
            proc.terminate()
    except ProcessLookupError:
        return
    except Exception:
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=0.5)
        return
    except Exception:
        pass
    try:
        if use_pgroup:
            os.killpg(pid, signal.SIGKILL)
        else:
            proc.kill()
    except ProcessLookupError:
        return
    except Exception:
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=1.0)
    except Exception:
        pass


async def communicate_bounded(
    proc: asyncio.subprocess.Process,
    timeout: float = 20.0,
    max_bytes: int = MAX_SUBPROCESS_OUTPUT_BYTES,
) -> tuple[bytes, bytes, bool, bool]:
    """
    Read stdout & stderr incrementally up to `max_bytes` per stream.
    Immediately terminates and reaps the subprocess if output exceeds `max_bytes` or `timeout`,
    preventing unbounded memory consumption (e.g. `cat /dev/zero` or massive `find` trees).
    Returns `(stdout_bytes, stderr_bytes, was_truncated, timed_out)`.
    """
    limit = max(1024, int(max_bytes))
    truncated = False

    # Compatibility fallback for mocked subprocesses in unit tests (real asyncio processes expose StreamReader)
    is_real_stream = isinstance(getattr(proc, "stdout", None), asyncio.StreamReader) or isinstance(
        getattr(proc, "stderr", None), asyncio.StreamReader
    )
    if not is_real_stream:
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            await _terminate_proc_safely(proc)
            return b"", b"", False, True
        out_b = out_b or b""
        err_b = err_b or b""
        if len(out_b) > limit:
            out_b = out_b[:limit]
            truncated = True
        if len(err_b) > limit:
            err_b = err_b[:limit]
            truncated = True
        return out_b, err_b, truncated, False

    async def _drain(stream: Optional[asyncio.StreamReader]) -> bytes:
        nonlocal truncated
        if stream is None:
            return b""
        buf = bytearray()
        while True:
            chunk = await stream.read(8192)
            if not chunk:
                break
            remaining = limit - len(buf)
            if remaining > 0:
                buf.extend(chunk[:remaining])
            if len(buf) >= limit and (len(chunk) > remaining or len(buf) == limit):
                # Peek or immediately stop once we exceed the byte cap
                if len(chunk) > remaining:
                    truncated = True
                    await _terminate_proc_safely(proc)
                    break
                extra = await stream.read(1)
                if extra:
                    truncated = True
                    await _terminate_proc_safely(proc)
                break
        return bytes(buf)

    async def _run() -> tuple[bytes, bytes]:
        out_b, err_b = await asyncio.gather(_drain(proc.stdout), _drain(proc.stderr))
        if proc.returncode is None:
            if truncated:
                await _terminate_proc_safely(proc)
            else:
                await proc.wait()
        return out_b, err_b

    try:
        out_bytes, err_bytes = await asyncio.wait_for(_run(), timeout=timeout)
        return out_bytes, err_bytes, truncated, False
    except asyncio.TimeoutError:
        await _terminate_proc_safely(proc)
        return b"", b"", truncated, True
    finally:
        transport = getattr(proc, "_transport", None)
        if transport is not None:
            try:
                transport.close()
            except Exception:
                pass


@tool(description="Выполнить безопасную bash-команду Linux (git status, fastfetch, ls, pacman -Qu, df -h, ps aux).")
async def run_bash(command: str) -> str:
    """Выполняет bash-команду с таймаутом, жёстким лимитом памяти вывода и выводом stdout/stderr."""
    cmd_clean = command.strip()
    if not cmd_clean:
        return "Ошибка: пустая команда."

    if is_forbidden_shell_command(cmd_clean):
        return (
            f"ВНИМАНИЕ: Команда '{cmd_clean}' расценена как потенциально опасная или разрушительная. "
            "Я заблокировала её автоматическое выполнение. Спроси прямо у пользователя подтверждение."
        )

    try:
        proc = await asyncio.create_subprocess_shell(
            cmd_clean,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(Path.home()),
            start_new_session=True,
        )
        stdout, stderr, truncated, timed_out = await communicate_bounded(
            proc, timeout=20.0, max_bytes=MAX_SUBPROCESS_OUTPUT_BYTES
        )
        if timed_out:
            return f"Команда '{cmd_clean}' превысила лимит времени (20 сек) и была остановлена."

        out_str = stdout.decode("utf-8", errors="replace").strip()
        err_str = stderr.decode("utf-8", errors="replace").strip()

        # Ограничиваем объём вывода, чтобы не переполнять контекст LLM
        max_chars = 3000
        if len(out_str) > max_chars:
            out_str = out_str[:max_chars] + f"\n... [вывод обрезан, всего {len(out_str)} символов]"
        if len(err_str) > max_chars:
            err_str = err_str[:max_chars] + f"\n... [stderr обрезан, всего {len(err_str)} символов]"
        if truncated:
            trunc_note = f"\n... [вывод превысил лимит {MAX_SUBPROCESS_OUTPUT_BYTES // 1024} KiB, процесс безопасно остановлен]"
            out_str = (out_str + trunc_note) if out_str else trunc_note.strip()

        if proc.returncode == 0 or truncated:
            return out_str if out_str else f"Команда '{cmd_clean}' выполнена успешно (вывод пуст)."
        else:
            res = []
            if out_str:
                res.append(f"STDOUT:\n{out_str}")
            if err_str:
                res.append(f"STDERR (код {proc.returncode}):\n{err_str}")
            return "\n".join(res) if res else f"Команда завершилась с кодом {proc.returncode}."

    except Exception as e:
        return f"Ошибка при запуске команды: {e}"


@tool(description="Показать топ процессов по CPU или памяти. sort_by: 'cpu'|'mem', limit: число (default 6).")
async def list_top_processes(sort_by: str = "cpu", limit: int = 6) -> str:
    sort_flag = "-%cpu" if sort_by.lower() == "cpu" else "-%mem"
    try:
        proc = await asyncio.create_subprocess_exec(
            "ps", "aux", f"--sort={sort_flag}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        lines = stdout.decode("utf-8", errors="replace").splitlines()
        header = lines[0]
        top_lines = lines[1 : limit + 1]

        # Оставляем ключевые колонки: USER, PID, %CPU, %MEM, COMMAND
        formatted = [f"{'USER':<10} {'PID':<8} {'%CPU':<6} {'%MEM':<6} COMMAND"]
        for line in top_lines:
            parts = line.split(None, 10)
            if len(parts) >= 11:
                user, pid, cpu, mem = parts[0], parts[1], parts[2], parts[3]
                cmd = parts[10]
                if len(cmd) > 50:
                    cmd = cmd[:47] + "..."
                formatted.append(f"{user:<10} {pid:<8} {cpu:<6} {mem:<6} {cmd}")

        return "\n".join(formatted)
    except Exception as e:
        return f"Не удалось получить список процессов: {e}"


@tool(description="Завершить процесс или приложение по имени/PID (например: 'firefox', 'spotify', 'telegram').")
async def kill_process(target: str) -> str:
    target = target.strip()
    if not target:
        return "Укажи имя процесса или PID."

    # Защита системных процессов, графических серверов и VPN
    protected = (
        "niri", "pipewire", "wireplumber", "systemd", "kitty", "login", "init",
        "xwayland", "xwayland-satellite", "happ", "happd", "xray", "v2ray", "sing-box",
        "clash", "openvpn", "wireguard", "wg-quick", "capri"
    )
    if target.lower() in protected:
        return f"Процесс '{target}' является критическим системным компонентом или сетевым сервисом (VPN/Xwayland). Завершение заблокировано."

    try:
        if target.isdigit():
            # Защищаем собственный PID
            if int(target) == os.getpid():
                return f"Процесс PID {target} является процессом Capri. Завершение заблокировано."

            # Проверяем, не является ли целевой PID защищённым процессом
            try:
                is_prot = False
                if os.path.exists(f"/proc/{target}/comm"):
                    with open(f"/proc/{target}/comm", "r") as f:
                        comm = f.read().strip().lower()
                    # Учитываем 15-байтовое усечение в /proc/<pid>/comm (например: xwayland-satell)
                    if any(comm == p or p.startswith(comm) or comm.startswith(p[:15]) for p in protected):
                        is_prot = True
                if not is_prot and os.path.exists(f"/proc/{target}/cmdline"):
                    with open(f"/proc/{target}/cmdline", "r") as f:
                        cmdline = f.read().replace("\0", " ").lower()
                    if any(p in cmdline for p in ("capri.ui", "xwayland-satellite", "happ", "happd", "xray", "sing-box", "v2ray")):
                        is_prot = True

                if is_prot:
                    return f"Процесс PID {target} является системным/VPN компонентом. Завершение заблокировано."
            except Exception:
                pass

            proc = await asyncio.create_subprocess_exec("kill", "-15", target)
            await proc.communicate()
            if proc.returncode == 0:
                return f"Сигнал завершения (SIGTERM) отправлен процессу PID {target}."
            return f"Не удалось завершить процесс с PID {target}."

        cleaned = target.lower().strip()
        from .desktop import APP_ALIASES
        resolved = APP_ALIASES.get(cleaned, target)

        # 1. Сначала проверяем активные окна Niri — закрываем через безопасное Niri close-window по Window ID!
        # Это предотвращает убийство общего PID xwayland-satellite, который делят все X11-окна.
        try:
            niri_proc = await asyncio.create_subprocess_exec(
                "niri", "msg", "-j", "windows",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await niri_proc.communicate()
            if niri_proc.returncode == 0:
                import json
                windows = json.loads(stdout.decode())
                for w in windows:
                    app_id = (w.get("app_id") or "").lower()
                    title = (w.get("title") or "").lower()
                    check_names = [cleaned, resolved.lower()]
                    if any(c in app_id or c in title for c in check_names if len(c) >= 3):
                        w_id = w.get("id")
                        if w_id is not None:
                            close_proc = await asyncio.create_subprocess_exec(
                                "niri", "msg", "action", "close-window", "--id", str(w_id),
                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                            )
                            await close_proc.communicate()
                            if close_proc.returncode == 0:
                                return f"Окно '{w.get('title')}' (ID {w_id}) успешно закрыто через Niri."
        except Exception:
            pass

        # 2. Безопасное завершение процессов с защитой от самоликвидации и выключения системных сервисов
        targets_to_try = [resolved, cleaned, target]
        for t in dict.fromkeys(targets_to_try):
            if not t or t.lower() in protected:
                continue

            # Сначала ищем по точному имени процесса (`--` блокирует flag-инъекцию при target вроде `-u root`)
            pgrep_proc = await asyncio.create_subprocess_exec(
                "pgrep", "-i", "--", t,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            pids_out, _ = await pgrep_proc.communicate()
            candidate_pids = [p.strip() for p in pids_out.decode().split() if p.strip().isdigit()]

            # Если по имени не найдено и строка содержит от 4 символов — ищем по cmdline
            if not candidate_pids and len(t) >= 4:
                pgrep_proc = await asyncio.create_subprocess_exec(
                    "pgrep", "-i", "-f", "--", t,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
                pids_out, _ = await pgrep_proc.communicate()
                candidate_pids = [p.strip() for p in pids_out.decode().split() if p.strip().isdigit()]

            # Фильтруем PIDs: исключаем себя, родительский терминал и защищённые процессы
            safe_pids_to_kill = []
            for p_str in candidate_pids:
                pid_int = int(p_str)
                if pid_int in (os.getpid(), os.getppid()):
                    continue
                is_safe = True
                try:
                    if os.path.exists(f"/proc/{pid_int}/comm"):
                        with open(f"/proc/{pid_int}/comm", "r") as f:
                            comm = f.read().strip().lower()
                        if any(comm == p or p.startswith(comm) or comm.startswith(p[:15]) for p in protected):
                            is_safe = False
                    if is_safe and os.path.exists(f"/proc/{pid_int}/cmdline"):
                        with open(f"/proc/{pid_int}/cmdline", "r") as f:
                            cmdline = f.read().replace("\0", " ").lower()
                        if any(p in cmdline for p in ("capri.ui", "xwayland-satellite", "happ", "happd", "xray", "sing-box", "v2ray")):
                            is_safe = False
                except Exception:
                    pass

                if is_safe:
                    safe_pids_to_kill.append(p_str)

            if safe_pids_to_kill:
                killed_count = 0
                for spid in safe_pids_to_kill:
                    kproc = await asyncio.create_subprocess_exec("kill", "-15", spid)
                    await kproc.communicate()
                    if kproc.returncode == 0:
                        killed_count += 1
                if killed_count > 0:
                    return f"Завершен процесс '{target}' (PID: {', '.join(safe_pids_to_kill)})."

        return f"Процесс '{target}' не найден среди активных процессов или окон."
    except Exception as e:
        return f"Ошибка при завершении процесса: {e}"
