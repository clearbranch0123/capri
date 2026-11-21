from __future__ import annotations

import asyncio
import html
import logging
import re
import urllib.parse
from typing import Optional, List, Dict, Any

import httpx

from .registry import tool

logger = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"


def escape_untrusted_tag(text: str, tag_name: str) -> str:
    """Neutralize closing/opening XML delimiter tags inside untrusted content to prevent tag-breakout injection."""
    if not text or not tag_name:
        return text or ""
    safe = re.sub(
        rf"<\s*/\s*{re.escape(tag_name)}\b[^>]*>",
        f"[/{tag_name}]",
        text,
        flags=re.IGNORECASE,
    )
    safe = re.sub(
        rf"<\s*{re.escape(tag_name)}\b[^>]*>",
        f"[{tag_name}]",
        safe,
        flags=re.IGNORECASE,
    )
    return safe


def _clean_html_text(raw_html: str, max_chars: int = 3000) -> str:
    """Очищает HTML-разметку от скриптов, стилей и тегов, оставляя чистый текст."""
    # Удаление невидимых и служебных тегов
    text = re.sub(r"<!--[\s\S]*?-->", "", raw_html)
    text = re.sub(
        r"<(?:script|style|nav|footer|header|svg|noscript|form|iframe)\b[^<]*(?:(?!<\/(?:script|style|nav|footer|header|svg|noscript|form|iframe)>)<[^<]*)*<\/(?:script|style|nav|footer|header|svg|noscript|form|iframe)>",
        "",
        text,
        flags=re.IGNORECASE,
    )
    # Преобразование переносов строк и заголовков
    text = re.sub(r"<(?:h[1-6]|p|div|tr|li|br)\b[^>]*>", "\n", text, flags=re.IGNORECASE)
    # Удаление остальных HTML-тегов
    text = re.sub(r"<[^>]+>", " ", text)
    # Декодирование всех стандартных HTML-сущностей (&laquo;, &raquo;, &amp;, &quot;, etc.)
    text = html.unescape(text)
    # Нормализация пробелов и пустых строк
    lines = [line.strip() for line in text.splitlines()]
    non_empty = [line for line in lines if line]
    clean = "\n".join(non_empty)
    if len(clean) > max_chars:
        clean = clean[:max_chars].rsplit(" ", 1)[0] + "\n\n... [текст сокращен для экономии контекста]"
    return clean.strip()


async def _search_wikipedia_fallback(query: str, max_results: int = 3) -> List[Dict[str, str]]:
    """Резервный поиск через открытый Wikipedia API при недоступности DuckDuckGo."""
    results = []
    headers = {"User-Agent": f"CapriAssistant/2.2.13 ({USER_AGENT})"}
    try:
        enc_query = urllib.parse.quote(query)
        url = f"https://ru.wikipedia.org/w/api.php?action=opensearch&search={enc_query}&limit={max_results}&namespace=0&format=json"
        async with httpx.AsyncClient(headers=headers, timeout=5.0) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                data = resp.json()
                if len(data) >= 4 and data[1]:
                    titles = data[1]
                    snippets = data[2]
                    links = data[3]
                    for title, snip, link in zip(titles, snippets, links):
                        body = snip
                        if not body:
                            try:
                                enc_title = urllib.parse.quote(title)
                                s_resp = await client.get(
                                    f"https://ru.wikipedia.org/api/rest_v1/page/summary/{enc_title}",
                                    timeout=3.0,
                                )
                                if s_resp.status_code == 200:
                                    body = s_resp.json().get("extract", "")
                            except Exception:
                                pass
                        results.append(
                            {
                                "title": f"[Википедия] {title}",
                                "body": body or "Статья в Википедии",
                                "href": link,
                            }
                        )
    except Exception as e:
        logger.debug(f"Wikipedia fallback failed: {e}")
    return results


@tool(
    description="Поиск информации в интернете (новости, статьи, события, факты, релизы). query: поисковый запрос, max_results: количество ссылок (по умолчанию 4)."
)
async def web_search(query: str, max_results: int = 4) -> str:
    """Выполняет веб-поиск по запросу через DuckDuckGo с автоматическим резервным переключением."""
    query = query.strip()
    if not query:
        return "Укажи поисковый запрос."

    def _sync_ddgs():
        for mod_name in ("ddgs", "duckduckgo_search"):
            try:
                mod = __import__(mod_name)
                DDGS = getattr(mod, "DDGS")
                with DDGS(timeout=5) as ddgs:
                    res = list(ddgs.text(query, max_results=max_results))
                    if res:
                        return res
                    res_html = list(ddgs.text(query, max_results=max_results, backend="html"))
                    if res_html:
                        return res_html
            except Exception as ex:
                logger.debug(f"DDGS attempt with {mod_name} failed: {ex}")
        return []

    results = await asyncio.to_thread(_sync_ddgs)

    if not results:
        results = await _search_wikipedia_fallback(query, max_results=max_results)

    if not results:
        return f"По запросу '{query}' ничего не найдено в сети или поисковые шлюзы временно недоступны."

    formatted = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "").strip()
        body = r.get("body", "").strip()
        href = r.get("href", "").strip()
        formatted.append(f"{i}. [{title}]({href})\n   {body}")

    return f"Результаты поиска по '{query}':\n\n" + "\n\n".join(formatted)


@tool(
    description="Загружает и читает текстовое содержимое веб-страницы по указанному URL (статьи, новости, документация, GitHub)."
)
async def fetch_web_page(url: str, max_chars: int = 3000) -> str:
    """Скачивает веб-страницу по ссылке (с SSRF-защитой и изоляцией недоверенного контента) и извлекает чистый текст."""
    from .browser import SSRFBlockedError, _SSRFGuardedAsyncTransport, validate_browser_url
    from ..core.doctor import redact_sensitive_text

    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        return "Ошибка: URL должен начинаться с http:// или https://"

    ok_init, msg_init = validate_browser_url(url, resolve_dns=False)
    if not ok_init:
        return f"Загрузка страницы отклонена политикой безопасности: {msg_init}"

    bounded_chars = max(500, min(8000, int(max_chars)))
    try:
        headers = {"User-Agent": USER_AGENT}
        transport = _SSRFGuardedAsyncTransport()
        async with httpx.AsyncClient(
            headers=headers,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
            timeout=8.0,
        ) as client:
            cur_url = url
            resp = None
            for _ in range(5):
                ok_hop, msg_hop = validate_browser_url(cur_url, resolve_dns=False)
                if not ok_hop:
                    return f"Загрузка страницы отклонена политикой безопасности: {msg_hop}"
                resp = await client.get(cur_url)
                if resp.status_code in (301, 302, 303, 307, 308):
                    loc = resp.headers.get("location")
                    if not loc:
                        break
                    cur_url = urllib.parse.urljoin(cur_url, loc)
                    continue
                break

            if resp is None:
                return f"Не удалось загрузить страницу {url}."
            if resp.status_code in (301, 302, 303, 307, 308):
                return f"Слишком много перенаправлений при загрузке {url}."
            if resp.status_code != 200:
                return f"Не удалось загрузить страницу {cur_url} (HTTP {resp.status_code})."

            content_type = resp.headers.get("content-type", "").lower()
            if (
                "text" not in content_type
                and "json" not in content_type
                and "html" not in content_type
                and "xml" not in content_type
            ):
                return f"Страница {cur_url} вернула нетекстовый контент ({content_type})."

            raw_text = resp.text
            clean_text = _clean_html_text(raw_text, max_chars=bounded_chars)
            if not clean_text:
                return f"Страница {cur_url} загружена, но полезного текста на ней не обнаружено."

            safe_text = escape_untrusted_tag(clean_text, "web_page_content")
            return (
                f"Содержимое страницы [{cur_url}]:\n\n"
                f'<web_page_content untrusted="true">\n{safe_text}\n</web_page_content>'
            )
    except SSRFBlockedError as e:
        return f"Загрузка страницы отклонена политикой безопасности: {e}"
    except httpx.TimeoutException:
        return f"Таймаут подключения к {url} (сервер не ответил за 8 секунд)."
    except Exception as e:
        return f"Ошибка при загрузке страницы {url}: {redact_sensitive_text(str(e))}"


@tool(description="Текущая погода и прогноз для города city (например 'Москва', 'Алматы') или текущей локации.")
async def get_weather(city: str = "") -> str:
    city_param = city.strip().replace(" ", "+")
    url = f"https://wttr.in/{city_param}?format=%l:+%c+%t,+ветер+%w,+влажность+%h&lang=ru"
    if not city_param:
        url = "https://wttr.in/?format=%l:+%c+%t,+ветер+%w,+влажность+%h&lang=ru"

    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            resp = await client.get(url, headers={"User-Agent": "curl/8.0"})
            if resp.status_code == 200:
                text = resp.text.strip()
                return f"Погода: {text}"
            else:
                return f"Не удалось получить погоду (код {resp.status_code})."
    except Exception as e:
        return f"Ошибка при запросе погоды: {e}"
