"""Разведка: можно ли получить цену товара WB через антибот-токен.

Шаг 1. Браузер открывает страницу товара и ловит:
       - cookie x_wbaas_token (антибот-токен WB);
       - запросы сайта к /__internal/ и их заголовки;
       - ответ с данными карточки товара.
Шаг 2. Повторяем запрос карточки обычным httpx — уже без браузера,
       с пойманными cookie и заголовками. Если сработает, парсер можно
       строить так: браузер только за токеном, цены — быстрыми запросами.

Запуск (из папки проекта):
    .venv\\Scripts\\python scripts\\wb_probe.py
    .venv\\Scripts\\python scripts\\wb_probe.py <ссылка или артикул>

Результаты (в data/, в Git не попадают):
    wb_session.json         — cookie и заголовки (секрет, никому не показывать)
    wb_probe_response.json  — ответ карточки, чтобы изучить его структуру
"""
import asyncio
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx
# patchright — копия Playwright с тем же кодом, но без следов автоматизации,
# по которым антибот WB распознаёт программу (обычный Playwright он ловит)
from patchright.async_api import async_playwright

DEFAULT_URL = "https://www.wildberries.ru/catalog/683146715/detail.aspx"
DATA_DIR = Path("data")
SESSION_FILE = DATA_DIR / "wb_session.json"
RESPONSE_FILE = DATA_DIR / "wb_probe_response.json"
ALL_BODIES_FILE = DATA_DIR / "wb_probe_all.json"
SCREENSHOT_FILE = DATA_DIR / "wb_probe.png"
# Профиль браузера (как у обычного пользователя: cookie, история). Хранится
# между запусками, поэтому WB видит «знакомый» браузер, а не новый каждый раз
PROFILE_DIR = DATA_DIR / "wb_browser_profile"
WAIT_SECONDS = 90  # проверка антибота может идти около минуты
TOKEN_COOKIE = "x_wbaas_token"
# Эти заголовки httpx выставит сам; cookie передаём отдельно
SKIP_HEADERS = {"cookie", "host", "content-length"}


def article_from(text: str) -> str:
    """Артикул — длинное число в ссылке (или сам аргумент, если это число)."""
    match = re.search(r"\d{5,}", text)
    if not match:
        sys.exit(f"Не нашёл артикул в {text!r}")
    return match.group()


def is_card_request(url: str, article: str) -> bool:
    """Запрос карточки товара: в адресе есть detail и наш артикул."""
    return "detail" in url and article in url


async def capture(page_url: str, article: str) -> dict:
    requests = []        # все фоновые запросы страницы (xhr/fetch): (адрес, заголовки)
    with_article = []    # ответы, в адресе которых есть наш артикул

    def on_request(r):
        if r.resource_type in ("xhr", "fetch"):
            requests.append((r.url, r.headers))

    def on_response(r):
        if r.request.resource_type in ("xhr", "fetch") and article in r.url:
            with_article.append(r)

    async with async_playwright() as p:
        # Настоящий Google Chrome (channel="chrome") с постоянным профилем.
        # headless=False — окно видно, no_viewport — размер окна как у человека.
        # Лишних настроек (свой User-Agent и т.п.) не задаём: они сами выдают бота
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            channel="chrome",
            headless=False,
            no_viewport=True,
        )
        page = context.pages[0] if context.pages else await context.new_page()
        page.on("request", on_request)
        page.on("response", on_response)

        print("Открываю страницу товара (до полутора минут, окно не трогай)...")
        await page.goto(page_url, wait_until="domcontentloaded", timeout=60_000)

        # Ждём: сначала антибот-токен, потом ответ с данными товара
        token_found = False
        for _ in range(WAIT_SECONDS):
            cookies = await context.cookies()
            token_found = any(c["name"] == TOKEN_COOKIE for c in cookies)
            if token_found and any(is_card_request(r.url, article) for r in with_article):
                break
            await asyncio.sleep(1)
        await asyncio.sleep(3)  # даём догрузиться остальному

        DATA_DIR.mkdir(exist_ok=True)
        await page.screenshot(path=str(SCREENSHOT_FILE))
        cookies = {c["name"]: c["value"] for c in await context.cookies()
                   if "wildberries" in c["domain"]}

        card_url, card_json = None, None
        bodies = {}  # адрес -> JSON всех ответов с артикулом (для изучения)
        for response in with_article:
            try:
                body = await response.json()
            except Exception:
                continue
            bodies[response.url] = body
            if card_url is None and is_card_request(response.url, article):
                card_url, card_json = response.url, body

        await context.close()

    headers = {}
    for url, h in requests:
        if url == card_url:
            headers = {k: v for k, v in h.items()
                       if k.lower() not in SKIP_HEADERS and not k.startswith(":")}

    return {
        "token_found": token_found,
        "cookies": cookies,
        "headers": headers,
        "request_paths": sorted({f"{urlsplit(u).netloc}{urlsplit(u).path}" for u, _ in requests}),
        "article_paths": [f"{urlsplit(u).netloc}{urlsplit(u).path}" for u in bodies],
        "bodies": bodies,
        "card_url": card_url,
        "card_json": card_json,
    }


def find_product(data, article: str):
    """Ищем товар в ответе: структура у WB бывает разной, проверяем варианты."""
    if not isinstance(data, dict):
        return None
    products = data.get("products") or (data.get("data") or {}).get("products") or []
    for product in products:
        if str(product.get("id")) == article:
            return product
    return products[0] if products else None


def show_price(product) -> None:
    sizes = product.get("sizes") or [{}]
    price = sizes[0].get("price") or {}
    print(f"  Название: {product.get('name')}")
    if "product" in price:
        print(f"  Цена: {price['product'] / 100:.0f} ₽ (без скидки: {price.get('basic', 0) / 100:.0f} ₽)")
    else:
        print("  Цену в привычном месте (sizes[0].price) не нашёл — смотри wb_probe_response.json")
    if "totalQuantity" in product:
        print(f"  В наличии: {product['totalQuantity']} шт.")


def main() -> None:
    arg = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    article = article_from(arg)
    page_url = arg if arg.startswith("http") else \
        f"https://www.wildberries.ru/catalog/{article}/detail.aspx"

    result = asyncio.run(capture(page_url, article))
    DATA_DIR.mkdir(exist_ok=True)
    SESSION_FILE.write_text(json.dumps(
        {"cookies": result["cookies"], "headers": result["headers"]},
        ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n=== Шаг 1: браузер ===")
    print(f"Антибот-токен {TOKEN_COOKIE}: {'есть' if result['token_found'] else 'НЕТ'}")
    print(f"Cookie WB (только имена): {', '.join(result['cookies']) or 'нет'}")
    print(f"Фоновые запросы страницы ({len(result['request_paths'])}):")
    for path in result["request_paths"] or ["нет"]:
        print(f"  {path}")
    print("Из них с артикулом в адресе:")
    for path in result["article_paths"] or ["нет"]:
        print(f"  {path}")
    ALL_BODIES_FILE.write_text(json.dumps(result["bodies"], ensure_ascii=False, indent=2),
                               encoding="utf-8")
    print(f"Скриншот страницы: {SCREENSHOT_FILE}")

    if not result["card_url"]:
        print("\nКарточку товара (detail) не нашёл. Пришли этот вывод целиком.")
        return
    RESPONSE_FILE.write_text(json.dumps(result["card_json"], ensure_ascii=False, indent=2),
                             encoding="utf-8")
    print(f"Карточка: {urlsplit(result['card_url']).path}")
    product = find_product(result["card_json"], article)
    if product:
        show_price(product)

    print("\n=== Шаг 2: тот же запрос без браузера (httpx) ===")
    try:
        response = httpx.get(result["card_url"], headers=result["headers"],
                             cookies=result["cookies"], timeout=15)
    except httpx.HTTPError as e:
        print(f"Ошибка сети: {e!r}")
        return
    print(f"Ответ: {response.status_code}")
    if response.status_code != 200:
        print("Без браузера не пустили. Пришли этот вывод — разберёмся.")
        return
    product = find_product(response.json(), article)
    if product:
        show_price(product)
        print("\n✅ Работает: токен из браузера + быстрые запросы.")
    else:
        print("Ответ 200, но товара в нём нет — смотри структуру ответа.")


if __name__ == "__main__":
    main()
