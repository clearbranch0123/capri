import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from capri.tools.web import _clean_html_text, web_search, fetch_web_page, get_weather


def test_clean_html_text():
    html_sample = """
    <html>
        <head>
            <style>body { color: red; }</style>
            <script>console.log("secret");</script>
        </head>
        <body>
            <header><nav>Home | About</nav></header>
            <h1>Заголовок статьи</h1>
            <p>Это первый параграф с &laquo;кавычками&raquo; и &amp; амперсандом.</p>
            <footer>Footer copyright 2026</footer>
        </body>
    </html>
    """
    cleaned = _clean_html_text(html_sample, max_chars=500)
    assert "console.log" not in cleaned
    assert "color: red" not in cleaned
    assert "Home | About" not in cleaned
    assert "Footer copyright" not in cleaned
    assert "Заголовок статьи" in cleaned
    assert "первый параграф с «кавычками» и & амперсандом." in cleaned


@pytest.mark.asyncio
async def test_fetch_web_page_validation():
    # Невалидный URL
    res = await fetch_web_page("ftp://invalid-scheme.com")
    assert "Ошибка: URL должен начинаться с http:// или https://" in res


@pytest.mark.asyncio
async def test_fetch_web_page_mock():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"content-type": "text/html; charset=utf-8"}
    mock_resp.text = "<html><body><h1>Cyberpunk Lore</h1><p>Relic chip details.</p></body></html>"

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_resp
        res = await fetch_web_page("https://nightcity.wiki/lore")
        assert "Cyberpunk Lore" in res
        assert "Relic chip details." in res
        assert "https://nightcity.wiki/lore" in res


@pytest.mark.asyncio
async def test_web_search_empty_query():
    res = await web_search("   ")
    assert "Укажи поисковый запрос" in res


@pytest.mark.asyncio
async def test_web_search_mock_ddgs():
    fake_results = [
        {"title": "CD Projekt Red", "body": "Official site of Cyberpunk 2077 developer.", "href": "https://cdpred.com"},
        {"title": "Night City Wire", "body": "News and updates about the city of dreams.", "href": "https://nightcity.com"},
    ]

    with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
        mock_thread.return_value = fake_results
        res = await web_search("Cyberpunk updates", max_results=2)
        assert "Результаты поиска по 'Cyberpunk updates'" in res
        assert "[CD Projekt Red](https://cdpred.com)" in res
        assert "Night City Wire" in res


@pytest.mark.asyncio
async def test_web_search_fallback():
    # Когда DDGS возвращает пустой список, срабатывает Wikipedia fallback
    with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread, \
         patch("capri.tools.web._search_wikipedia_fallback", new_callable=AsyncMock) as mock_wiki:
        mock_thread.return_value = []
        mock_wiki.return_value = [
            {"title": "[Википедия] Джонни Сильверхенд", "body": "Персонаж Cyberpunk 2077.", "href": "https://ru.wikipedia.org/wiki/Johnny"}
        ]
        res = await web_search("Джонни Сильверхенд")
        assert "Результаты поиска по 'Джонни Сильверхенд'" in res
        assert "[Википедия] Джонни Сильверхенд" in res
        assert "ru.wikipedia.org" in res
