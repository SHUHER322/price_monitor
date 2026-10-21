"""Разведка Ozon, шаг 2: каким способом можно получать цену без открытия страницы.

Берём cookie и заголовки из data/ozon_session.json (их сохранил scripts.ozon_probe)
и пробуем один и тот же запрос к API страницы товара тремя способами:
    1. httpx                       — обычный Python-клиент
    2. curl_cffi (как Chrome)      — подделывает TLS-«почерк» соединения браузера
    3. fetch изнутри браузера      — запрос делает сам Chrome (patchright)

Запуск (из папки проекта):
    .venv\\Scripts\\python -m pip install curl_cffi
    .venv\\Scripts\\python -m scripts.ozon_probe_api "<ссылка на товар Ozon>"
"""
import asyncio
import json
import re
import sys
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx

DATA_DIR = Path("data")
SESSION_FILE = DATA_DIR / "ozon_session.json"
PROFILE_DIR = DATA_DIR / "ozon_browser_profile"
API = "https://www.ozon.ru/api/entrypoint-api.bx/page/json/v2?url="
PRICE_STATE = re.compile(r"webPrice")
SAVED_FILE = DATA_DIR / "ozon_api_product.json"


def api_url(product_url: str) -> str:
    """https://www.ozon.ru/product/slug-123/?at=... → API-адрес для /product/slug-123/"""
    path = urlsplit(product_url).path
    return API + quote(path, safe="/")


def find_price(text: str) -> str:
    """Достать виджет webPrice из ответа API — если он там есть."""
    try:
        states = json.loads(text).get("widgetStates", {})
    except ValueError:
        return "ответ не JSON"
    for name, state in states.items():
        if PRICE_STATE.search(name):
            data = json.loads(state) if isinstance(state, str) else state
            return (f"price={data.get('price')!r}, cardPrice={data.get('cardPrice')!r}, "
                    f"originalPrice={data.get('originalPrice')!r}, "
                    f"isAvailable={data.get('isAvailable')}")
    return f"виджета webPrice нет (виджетов: {len(states)})"


def load_session() -> tuple[dict, str]:
    session = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    headers = session["headers"]
    cookie = "; ".join(f"{k}={v}" for k, v in session["cookies"].items())
    return headers, cookie


def try_httpx(url: str, headers: dict, cookie: str) -> None:
    print("\n1) httpx")
    try:
        r = httpx.get(url, headers={**headers, "cookie": cookie}, timeout=15)
        print(f"   ответ {r.status_code}: {find_price(r.text) if r.status_code == 200 else '—'}")
    except httpx.HTTPError as e:
        print(f"   ошибка сети: {e!r}")


def try_curl_cffi(url: str, headers: dict, cookie: str) -> None:
    print("\n2) curl_cffi (как Chrome)")
    try:
        from curl_cffi import requests as curl
    except ImportError:
        print("   не установлен: .venv\\Scripts\\python -m pip install curl_cffi")
        return
    try:
        # impersonate — повторить TLS-«почерк» настоящего Chrome
        r = curl.get(url, headers={**headers, "cookie": cookie}, impersonate="chrome",
                     timeout=15)
        print(f"   ответ {r.status_code}: {find_price(r.text) if r.status_code == 200 else '—'}")
    except Exception as e:
        print(f"   ошибка: {e!r}")


async def try_browser(url: str, product_url: str) -> None:
    print("\n3) fetch изнутри браузера")
    from patchright.async_api import async_playwright
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR), channel="chrome", headless=False,
            no_viewport=True)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            # Открываем главную — так у fetch будут «родные» cookie и адрес сайта
            await page.goto("https://www.ozon.ru/", wait_until="domcontentloaded",
                            timeout=60_000)
            await asyncio.sleep(5)
            result = await page.evaluate(
                """async (url) => {
                    const r = await fetch(url, {credentials: "include"});
                    return {status: r.status, text: await r.text()};
                }""", url)
            ok = result["status"] == 200
            print(f"   ответ {result['status']}: {find_price(result['text']) if ok else '—'}")
            if ok:
                # Полный ответ — чтобы разобрать, где название, продавец, рейтинг
                SAVED_FILE.write_text(result["text"], encoding="utf-8")
                states = json.loads(result["text"]).get("widgetStates", {})
                print(f"   сохранён в {SAVED_FILE}; виджеты: {', '.join(sorted(states))}")
        finally:
            await context.close()


def main() -> None:
    if len(sys.argv) < 2 or "ozon.ru" not in sys.argv[1]:
        sys.exit("Укажи ссылку на товар Ozon в кавычках")
    if not SESSION_FILE.exists():
        sys.exit("Нет data/ozon_session.json — сначала запусти scripts.ozon_probe")
    product_url = sys.argv[1]
    url = api_url(product_url)
    headers, cookie = load_session()
    print(f"Запрос: {url}")
    if "--all" in sys.argv:   # httpx и curl_cffi уже проверены: 403 (октябрь 2026)
        try_httpx(url, headers, cookie)
        try_curl_cffi(url, headers, cookie)
    asyncio.run(try_browser(url, product_url))


if __name__ == "__main__":
    main()
