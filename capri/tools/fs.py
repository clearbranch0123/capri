from __future__ import annotations

import asyncio
import os
from pathlib import Path
from .registry import tool


def _resolve_path(p: str) -> Path:
    expanded = os.path.expanduser(p.strip())
    path = Path(expanded)
    if not path.is_absolute():
        path = Path.home() / path
    return path.resolve()


MAX_READ_LINES = 500
MAX_READ_CHARS = 16000


@tool(description="Прочитать текстовый файл (конфиг, скрипт, лог, код). path: путь, max_lines: максимум строк (default 150).")
async def read_file(path: str, max_lines: int = 150) -> str:
    target = _resolve_path(path)
    if not target.exists():
        return f"Файл не найден: {target}"
    if target.is_dir():
        return f"Путь {target} является директорией, используй list_directory."

    bounded_lines = max(1, min(MAX_READ_LINES, int(max_lines)))
    try:
        def _read():
            with open(target, "r", encoding="utf-8", errors="replace") as f:
                lines = []
                total_chars = 0
                for i in range(bounded_lines + 1):
                    line = f.readline(4096)
                    if not line:
                        break
                    if i >= bounded_lines:
                        lines.append(f"... [файл обрезан, показано {bounded_lines} строк]")
                        break
                    clean_ln = line.rstrip("\r\n")
                    if total_chars + len(clean_ln) > MAX_READ_CHARS:
                        remain = max(0, MAX_READ_CHARS - total_chars)
                        if remain > 0:
                            lines.append(f"{i+1:4d} | {clean_ln[:remain]}")
                        lines.append(f"... [файл обрезан по лимиту {MAX_READ_CHARS} символов]")
                        break
                    total_chars += len(clean_ln)
                    lines.append(f"{i+1:4d} | {clean_ln}")
                return "\n".join(lines)

        content = await asyncio.to_thread(_read)
        return f"Содержимое {target}:\n---\n{content}\n---"
    except Exception as e:
        return f"Ошибка при чтении файла {target}: {e}"


@tool(description="Записать текст content в файл по пути path.")
async def write_file(path: str, content: str) -> str:
    target = _resolve_path(path)
    try:
        def _write():
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "w", encoding="utf-8") as f:
                f.write(content)

        await asyncio.to_thread(_write)
        return f"Файл успешно записан: {target} ({len(content)} символов)."
    except Exception as e:
        return f"Ошибка при записи файла {target}: {e}"


@tool(description="Список файлов и папок в директории path (default '~').")
async def list_directory(path: str = "~") -> str:
    target = _resolve_path(path)
    if not target.exists():
        return f"Папка не существует: {target}"
    if not target.is_dir():
        return f"Путь {target} не является папкой."

    try:
        def _list():
            entries = []
            for item in sorted(target.iterdir()):
                # Пропускаем скрытые файлы, если не запрошены явно
                if item.name.startswith(".") and not path.endswith(".*"):
                    continue
                prefix = "📁 [DIR] " if item.is_dir() else "📄 [FILE]"
                size_str = ""
                if item.is_file():
                    size_kb = item.stat().st_size / 1024
                    size_str = f" ({size_kb:.1f} KB)"
                entries.append(f"{prefix} {item.name}{size_str}")
                if len(entries) >= 60:
                    entries.append("... [список ограничен 60 элементами]")
                    break
            return entries

        items = await asyncio.to_thread(_list)
        return f"Содержимое {target}:\n" + "\n".join(items) if items else f"Папка {target} пуста."
    except Exception as e:
        return f"Ошибка при чтении директории {target}: {e}"


MAX_FIND_OUTPUT_BYTES = 64 * 1024  # 64 KiB cap for `find` stdout to prevent OOM DoS


@tool(description="Рекурсивный поиск файлов по маске pattern (например '*.py', '*.kdl') в search_path.")
async def find_files(pattern: str, search_path: str = "~") -> str:
    from .shell import communicate_bounded

    pat = (pattern or "").strip()
    if not pat:
        return "Укажи маску поиска (например '*.py')."
    target = _resolve_path(search_path)
    if not target.exists():
        return f"Путь {target} не существует."

    try:
        proc = await asyncio.create_subprocess_exec(
            "find", str(target), "-maxdepth", "4", "-name", pat,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        stdout, _, truncated, timed_out = await communicate_bounded(
            proc, timeout=15.0, max_bytes=MAX_FIND_OUTPUT_BYTES
        )
        if timed_out:
            return f"Поиск файлов по '{pat}' в {target} превысил лимит времени и был остановлен."
        raw_lines = [line.strip() for line in stdout.decode("utf-8", errors="replace").splitlines() if line.strip()]
        # Never expose sensitive .env / private key filenames in generic search listings
        lines = [
            ln for ln in raw_lines
            if os.path.basename(ln).lower() not in (".env", ".env.local", "id_rsa", "id_ed25519")
        ]
        if not lines:
            return f"Файлы по шаблону '{pat}' в {target} не найдены."
        shown = lines[:25]
        suffix = []
        if len(lines) > 25:
            suffix.append(f"... и ещё {len(lines) - 25} файлов")
        if truncated:
            suffix.append(f"... [вывод find ограничен {MAX_FIND_OUTPUT_BYTES // 1024} KiB, процесс остановлен]")
        return f"Найдено {len(lines)} совпадений по '{pat}':\n" + "\n".join(shown + suffix)
    except Exception as e:
        return f"Ошибка при поиске файлов: {e}"
