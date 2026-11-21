from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Optional

import httpx
from PIL import Image

from .registry import tool
from ..core.config import load_config

logger = logging.getLogger(__name__)


async def analyze_image_file(
    image_path: str | Path,
    query: str = "",
    config=None,
) -> str:
    """
    Analyze any local image file (screenshot, pasted clipboard image, dropped file)
    using Pillow JPEG compression + Gemini Vision API with Tesseract OCR fallback.
    """
    img_path = Path(image_path)
    if not img_path.exists() or img_path.stat().st_size == 0:
        return "Не удалось прочитать изображение."

    from ..core.attachments import MAX_IMAGE_PIXELS, validate_image_limits
    ok_limits, limit_reason = validate_image_limits(img_path)
    if not ok_limits:
        return f"Изображение отклонено лимитами безопасности: {limit_reason}"

    cfg = config or load_config()
    if not getattr(cfg.vision, "enabled", True):
        return "Просмотр экрана (Vision AI) отключён в настройках Capri."
    vision_provider = (getattr(cfg.vision, "provider", "gemini") or "gemini").strip().lower()
    gemini_key = (
        getattr(cfg.vision, "gemini_api_key", "")
        or os.getenv("GEMINI_API_KEY", "")
        or getattr(cfg.voice, "gemini_api_key", "")
    ).strip()
    user_name = getattr(getattr(cfg, "companion", None), "user_name", "D") or "D"

    try:
        prev_limit = Image.MAX_IMAGE_PIXELS
        Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
        try:
            with Image.open(img_path) as im:
                max_dim = getattr(cfg.vision, "max_dimension", 1280)
                im.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
                buf = io.BytesIO()
                im.convert("RGB").save(buf, format="JPEG", quality=getattr(cfg.vision, "jpeg_quality", 80))
                img_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
        finally:
            Image.MAX_IMAGE_PIXELS = prev_limit
    except Exception as e:
        logger.error(f"Image compression error: {e}")
        return f"Не удалось безопасно обработать изображение: {e}"

    if vision_provider != "ocr" and gemini_key:
        model_name = getattr(cfg.vision, "model", "gemini-3.1-flash-lite") or "gemini-3.1-flash-lite"
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={gemini_key}"

        q_text = query.strip() if query.strip() else "Опиши подробно и лаконично, что изображено: код, ошибки, интерфейс, текст или картинка."
        system_instruction = (
            "Ты — оптический сенсор (зрение) автономного цифрового компаньона Capri в Linux. "
            f"Пользователь {user_name} показал тебе изображение. "
            f"Вопрос или фокус внимания: '{q_text}'. "
            "Инструкции к анализу:\n"
            "1. Если виден код или логи терминала — укажи точную ошибку, стек-трейс или причину сбоя.\n"
            "2. Если открыт браузер, приложение или картинка — лаконично опиши ключевую суть.\n"
            "3. Отвечай прямо, информативно, на русском языке, без лишней воды и предисловий."
        )
        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": system_instruction},
                        {"inline_data": {"mime_type": "image/jpeg", "data": img_b64}},
                    ]
                }
            ]
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json=payload)
                if resp.status_code == 200:
                    data = resp.json()
                    candidates = data.get("candidates", [])
                    if candidates:
                        parts = candidates[0].get("content", {}).get("parts", [])
                        if parts:
                            visual_analysis = parts[0].get("text", "").strip()
                            if visual_analysis:
                                return f"[Оптический сенсор Capri / Vision AI]:\n{visual_analysis}"
                else:
                    from ..core.doctor import redact_sensitive_text
                    logger.warning(f"Gemini Vision API status {resp.status_code}: {redact_sensitive_text(resp.text[:200])}")
        except Exception as e:
            from ..core.doctor import redact_sensitive_text
            logger.warning(f"Gemini Vision call failed: {redact_sensitive_text(str(e), extra_secrets=(gemini_key,))}")

    if shutil.which("tesseract"):
        tess_proc = await asyncio.create_subprocess_exec(
            "tesseract",
            str(img_path),
            "stdout",
            "-l",
            "rus+eng",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await tess_proc.communicate()
        ocr_text = stdout.decode("utf-8", errors="replace").strip()
        if ocr_text:
            if len(ocr_text) > 3000:
                ocr_text = ocr_text[:3000] + "\n... [текст с изображения обрезан]"
            return f"[Зрение Capri (резервный OCR-режим)]: На изображении обнаружен текст:\n---\n{ocr_text}\n---"

    return "Оптический сенсор не смог распознать изображение (проверь GEMINI_API_KEY или наличие tesseract)."


@tool(description="Оптическое зрение (Vision AI). Сделать снимок экрана и проанализировать происходящее: код, ошибки, веб-страницы, окна. query: вопрос к экрану, mode: 'fullscreen'|'select'.")
async def look_at_screen(query: str = "", mode: str = "fullscreen") -> str:
    if not shutil.which("grim"):
        return "Утилита 'grim' для скриншотов Wayland не найдена в системе."

    config = load_config()
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_img = tmp.name

    try:
        if mode == "select" and shutil.which("slurp"):
            slurp_proc = await asyncio.create_subprocess_exec(
                "slurp", stdout=asyncio.subprocess.PIPE
            )
            region, _ = await slurp_proc.communicate()
            if slurp_proc.returncode != 0 or not region.strip():
                return "Выделение области на экране отменено пользователем."

            grim_proc = await asyncio.create_subprocess_exec(
                "grim", "-g", region.decode().strip(), tmp_img
            )
            await grim_proc.communicate()
        else:
            grim_proc = await asyncio.create_subprocess_exec("grim", tmp_img)
            await grim_proc.communicate()

        if not os.path.exists(tmp_img) or os.path.getsize(tmp_img) == 0:
            return "Не удалось захватить изображение экрана."

        return await analyze_image_file(tmp_img, query=query, config=config)

    except Exception as e:
        return f"Ошибка при взгляде на экран: {e}"
    finally:
        if os.path.exists(tmp_img):
            try:
                os.remove(tmp_img)
            except Exception:
                pass


@tool(description="Прочитать текст с экрана через OCR (Tesseract). mode: 'fullscreen'|'select'.")
async def read_screen(mode: str = "fullscreen") -> str:
    """Быстрый локальный текстовый захват без обращения к Vision-моделям."""
    return await look_at_screen(query="Прочитай весь видимый текст на экране", mode=mode)
