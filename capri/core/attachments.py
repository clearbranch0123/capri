"""
Composer Attachment Pipeline for Capri Closed Beta (Drag & Drop + Ctrl+V Screenshots & Files).
Supports image attachments (analyzed via Gemini Vision / OCR fallback in background worker)
and UTF-8 text/code/log files (safely bounded and formatted into Markdown fenced blocks).
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

from .config import AppConfig

logger = logging.getLogger(__name__)

AttachmentKind = Literal["image", "text"]

IMAGE_EXTENSIONS: frozenset[str] = frozenset({
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif",
})

TEXT_EXTENSIONS: frozenset[str] = frozenset({
    ".txt", ".md", ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".yaml", ".yml",
    ".toml", ".ini", ".conf", ".cfg", ".sh", ".bash", ".zsh", ".fish", ".kdl",
    ".css", ".scss", ".html", ".htm", ".xml", ".sql", ".rs", ".go", ".c", ".cpp",
    ".h", ".hpp", ".java", ".kt", ".rb", ".php", ".log", ".csv", ".diff", ".patch",
    ".service", ".desktop", ".gitignore", ".dockerfile",
})

MAX_ATTACHMENTS = 6
MAX_TEXT_BYTES = 2 * 1024 * 1024  # 2 MB safety cap for text/code files
MAX_IMAGE_BYTES = 15 * 1024 * 1024  # 15 MB safety cap for image attachments
MAX_IMAGE_PIXELS = 40_000_000  # 40 MP decompression-bomb cap
MAX_TEXT_CHARS_IN_PROMPT = 8000


@dataclass
class AttachmentItem:
    path: Path
    kind: AttachmentKind
    display_name: str
    size_bytes: int = 0
    is_temp: bool = False


def validate_image_limits(
    file_path: Path,
    max_bytes: int = MAX_IMAGE_BYTES,
    max_pixels: int = MAX_IMAGE_PIXELS,
) -> tuple[bool, str]:
    """Check image byte size and decoded pixel count without loading full pixel data into RAM."""
    try:
        size = file_path.stat().st_size
    except OSError as e:
        return False, f"Файл недоступен: {e}"
    if size <= 0:
        return False, "Пустой файл изображения."
    if size > max_bytes:
        return False, f"Изображение превышает лимит размера ({size // (1024 * 1024)} МБ > {max_bytes // (1024 * 1024)} МБ)."
    try:
        from PIL import Image
        prev_limit = Image.MAX_IMAGE_PIXELS
        Image.MAX_IMAGE_PIXELS = max_pixels
        try:
            with Image.open(file_path) as im:
                w, h = im.size
                if w <= 0 or h <= 0:
                    return False, "Некорректные размеры изображения."
                if (w * h) > max_pixels:
                    return False, f"Изображение превышает лимит пикселей ({w}x{h} > {max_pixels})."
        finally:
            Image.MAX_IMAGE_PIXELS = prev_limit
    except Image.DecompressionBombError as e:
        return False, f"Изображение превышает лимит пикселей ({max_pixels}): {e}"
    except Exception as e:
        return False, f"Некорректное или слишком большое изображение: {e}"
    return True, "OK"


def classify_attachment(file_path: Path | str) -> Optional[AttachmentItem]:
    """Validate and classify a local file path into an image or text AttachmentItem."""
    p = Path(file_path).expanduser()
    if not p.exists() or not p.is_file():
        return None
    ext = p.suffix.lower()
    size = p.stat().st_size

    if ext in IMAGE_EXTENSIONS:
        ok, _reason = validate_image_limits(p)
        if not ok:
            return None
        return AttachmentItem(path=p, kind="image", display_name=p.name, size_bytes=size, is_temp=False)

    if ext in TEXT_EXTENSIONS or p.name.lower() in ("dockerfile", "makefile", "readme", "license"):
        if size > MAX_TEXT_BYTES:
            return None
        return AttachmentItem(path=p, kind="text", display_name=p.name, size_bytes=size, is_temp=False)

    # Heuristic UTF-8 text probe for extensionless or uncommon code/config files
    if size <= MAX_TEXT_BYTES:
        try:
            sample = p.read_bytes()[:4096]
            if b"\x00" not in sample:
                sample.decode("utf-8")
                return AttachmentItem(path=p, kind="text", display_name=p.name, size_bytes=size, is_temp=False)
        except Exception:
            pass
    return None


DEFAULT_CLIPBOARD_CACHE_DIR = Path(tempfile.gettempdir()) / "capri_clipboard"


def save_clipboard_image_bytes(png_bytes: bytes, cache_dir: Optional[Path] = None) -> AttachmentItem:
    """Persist pasted clipboard image bytes into a temp PNG attachment (enforcing image byte/pixel limits)."""
    if len(png_bytes) > MAX_IMAGE_BYTES:
        raise ValueError(f"Скриншот из буфера превышает лимит {MAX_IMAGE_BYTES // (1024 * 1024)} МБ.")
    target_dir = cache_dir or DEFAULT_CLIPBOARD_CACHE_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    name = f"clipboard_{time.strftime('%H%M%S')}_{os.getpid()}_{uuid.uuid4().hex[:6]}.png"
    out = target_dir / name
    out.write_bytes(png_bytes)
    ok, reason = validate_image_limits(out)
    if not ok:
        try:
            out.unlink()
        except OSError:
            pass
        raise ValueError(reason)
    return AttachmentItem(
        path=out,
        kind="image",
        display_name=name,
        size_bytes=len(png_bytes),
        is_temp=True,
    )


def cleanup_temp_attachment(item: Optional[AttachmentItem]) -> None:
    """Safely delete an attachment file ONLY when it is a temporary clipboard artifact (`is_temp=True`)."""
    if item is None or not getattr(item, "is_temp", False):
        return
    try:
        p = Path(item.path)
        if p.exists() and p.is_file():
            p.unlink()
    except OSError:
        pass


def format_user_display_with_attachments(user_text: str, attachments: list[AttachmentItem]) -> str:
    """Build clean user-facing chat card text with attachment tags."""
    if not attachments:
        return user_text
    tags = " · ".join(
        f"[{'IMG' if a.kind == 'image' else 'FILE'}: {a.display_name}]"
        for a in attachments
    )
    base = (user_text or "").strip()
    if not base:
        return tags
    return f"{base}\n\n{tags}"


async def build_prompt_with_attachments(
    user_text: str,
    attachments: list[AttachmentItem],
    config: Optional[AppConfig] = None,
) -> str:
    """
    Enrich user prompt with attached text file contents and Vision AI analysis of attached images.
    Designed to run asynchronously inside AgentWorker so UI never freezes.
    """
    if not attachments:
        return user_text

    from ..tools.screen import analyze_image_file

    sections: list[str] = []
    base_text = (user_text or "").strip()
    if base_text:
        sections.append(base_text)
    else:
        sections.append("Проанализируй прикреплённые материалы.")

    for att in attachments[:MAX_ATTACHMENTS]:
        if att.kind == "image":
            try:
                analysis = await analyze_image_file(
                    att.path,
                    query=base_text or "Опиши подробно, что изображено на картинке/скриншоте (код, ошибки, интерфейс).",
                    config=config,
                )
            except Exception as e:
                logger.warning(f"Image attachment analysis failed for {att.display_name}: {e}")
                analysis = f"(Не удалось проанализировать изображение: {e})"
            sections.append(f"[Прикреплённое изображение: {att.display_name}]\n{analysis}")
        else:
            try:
                raw = att.path.read_text(encoding="utf-8", errors="replace")
                truncated = ""
                if len(raw) > MAX_TEXT_CHARS_IN_PROMPT:
                    raw = raw[:MAX_TEXT_CHARS_IN_PROMPT]
                    truncated = f"\n... [обрезано до {MAX_TEXT_CHARS_IN_PROMPT} символов]"
                lang = att.path.suffix.lstrip(".").lower()
                sections.append(f"[Прикреплённый файл: {att.display_name}]\n```{lang}\n{raw}{truncated}\n```")
            except Exception as e:
                sections.append(f"[Прикреплённый файл: {att.display_name}]\n(Ошибка чтения файла: {e})")

    return "\n\n".join(sections)
