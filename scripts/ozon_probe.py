"""Разведка Ozon: как страница товара получает цену и пускает ли антибот.

Шаг 1. Настоящий Chrome (patchright) открывает страницу товара и записывает:
       - пустил ли антибот (скриншот страницы);
       - фоновые запросы страницы и ответы, где встречается цена;
       - cookie Ozon (только имена).
Шаг 2. Повторяем самый подходящий запрос обычным httpx — с cookie и
       заголовками браузера. Если сработает, Ozon можно парсить так же, как WB:
       браузер изредка, данные быстрыми запросами.

Запуск (из папки проекта):
    .venv\\Scripts\\python -m scripts.ozon_probe <ссылка на товар Ozon>

Результаты (в data/, в Git не попадают):
    ozon_probe.png        — скриншот страницы
    ozon_probe_all.json   — ответы с ценой, чтобы разобрать их структуру
    ozon_session.json     — cookie и заголовки (секрет, никому не показывать)
"""
import asyncio
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from patchright.async_api import async_playwright

DATA_DIR = Path("data")
PROFILE_DIR = DATA_DIR / "ozon_browser_profile"   # отдельный профиль, не общий с WB
SCREENSHOT_FILE = DATA_DIR / "ozon_probe.png"
BODIES_FILE = DATA_DIR / "ozon_probe_all.json"
SESSION_FILE = DATA_DIR / "ozon_session.json"
WAIT_SECONDS = 25
SKIP_HEADERS = {"cookie", "host", "content-length"}
# Признаки цены в ответе: «1 234 ₽» или ключи, в которых Ozon обычно хранит цену
PRICE_MARK = re.compile(r"₽|webPrice|\"price\"|cardPrice", re.IGNORECASE)


async def capture(url: str) -> dict:
    requests = {}     # адрес -> заголовки (только xhr/fetch)
    responses = []

    def on_request(r):
        if r.resource_type in ("xhr", "fetch"):
            requests[r.url] = r.headers

    def on_response(r):
        if r.request.resource_type in ("xhr", "fetch", "document"):
            responses.append(r)

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR), channel="chrome",
            headless=False, no_viewport=True)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            page.on("request", on_request)
            page.on("response", on_response)
            print("Открываю страницу товара (до полуминуты, окно не трогай)...")
            await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            await asyncio.sleep(WAIT_SECONDS)
            DATA_DIR.mkdir(exist_ok=True)
            await page.screenshot(path=str(SCREENSHOT_FILE))
            title = await page.title()

            found = {}
            for r in responses:
                try:
                    text = await r.text()
                except Exception:
                    continue
                if not PRICE_MARK.search(text):
                    continue
                kind = r.request.resource_type
                try:
                    found[r.url] = {"type": kind, "status": r.status, "json": json.loads(text)}
                except ValueError:
                    # HTML или не-JSON: сохраняем кусочки вокруг цены, а не всю страницу
                    snippets = [text[max(0, m.start() - 150):m.end() + 150]
                                for m in re.finditer("₽", text)][:20]
                    found[r.url] = {"type": kind, "status": r.status, "snippets": snippets}

            cookies = {c["name"]: c["value"] for c in await context.cookies()
                       if "ozon" in c["domain"]}
        finally:
            await context.close()

    return {"title": title, "requests": requests, "found": found, "cookies": cookies}


def short(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.netloc}{parts.path}"


def main() -> None:
    if len(sys.argv) < 2 or "ozon.ru" not in sys.argv[1]:
        sys.exit("Укажи ссылку на товар Ozon: python -m scripts.ozon_probe https://www.ozon.ru/product/...")
    url = sys.argv[1]
    result = asyncio.run(capture(url))

    BODIES_FILE.write_text(json.dumps(result["found"], ensure_ascii=False, indent=1),
                           encoding="utf-8")
    print("\n=== Шаг 1: браузер ===")
    print(f"Заголовок страницы: {result['title']!r}")
    print(f"Cookie Ozon (только имена): {', '.join(sorted(result['cookies'])) or 'нет'}")
    print(f"Фоновых запросов: {len(result['requests'])}")
    print("Ответы, где есть цена:")
    for u, info in result["found"].items():
        print(f"  {info['status']} [{info['type']}] {short(u)}")
    print(f"Скриншот: {SCREENSHOT_FILE}")

    # Кандидат для повтора без браузера: фоновый запрос с ценой (обычно entrypoint-api)
    candidates = [u for u, info in result["found"].items()
                  if info["type"] in ("xhr", "fetch") and "json" in info]
    if not candidates:
        print("\nФоновых JSON-запросов с ценой нет — цена, видимо, приходит в самой "
              "HTML-странице. Пришли этот вывод целиком.")
        return
    target = candidates[0]
    headers = {k: v for k, v in result["requests"].get(target, {}).items()
               if k.lower() not in SKIP_HEADERS and not k.startswith(":")}
    SESSION_FILE.write_text(json.dumps({"cookies": result["cookies"], "headers": headers},
                                       ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"\n=== Шаг 2: тот же запрос без браузера (httpx) ===\n{short(target)}")
    cookie = "; ".join(f"{k}={v}" for k, v in result["cookies"].items())
    try:
        response = httpx.get(target, headers={**headers, "cookie": cookie}, timeout=15)
    except httpx.HTTPError as e:
        print(f"Ошибка сети: {e!r}")
        return
    has_price = bool(PRICE_MARK.search(response.text))
    print(f"Ответ: {response.status_code}, цена в ответе: {'есть' if has_price else 'НЕТ'}")
    if response.status_code == 200 and has_price:
        print("✅ Похоже, работает: cookie из браузера + быстрые запросы.")
    else:
        print("Без браузера не пустили или ответ другой. Пришли вывод — разберёмся.")


if __name__ == "__main__":
    main()
