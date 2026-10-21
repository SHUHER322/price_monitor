"""Источник Ozon.

Как работает (проверено разведкой scripts/ozon_probe*.py, октябрь 2026):
1. Данные страницы товара отдаёт API сайта /api/entrypoint-api.bx/page/json/v2.
2. Ozon проверяет не только cookie, но и «почерк» соединения (TLS-отпечаток):
   httpx и даже curl_cffi с подделкой Chrome получают 403. Пускает только
   настоящий браузер. Поэтому запросы делает сам Chrome (patchright) — через
   fetch изнутри открытой страницы.
3. На одну проверку браузер открывается один раз, все товары запрашиваются
   в нём по очереди с паузами, потом браузер закрывается.

Страница товара не показывает количество на складе — только «есть / нет»
(quantity = UNKNOWN_STOCK). Зато в поиске и магазине у плиток часто есть
«N ед осталось».

Поиск и магазин (разведка scripts/ozon_probe_pages.py): API отдаёт страницу
каталога (&page=N) порциями по 8 товаров, следующая порция — по ссылке
nextPage. В поиске страница — около 16 новых товаров; в магазине страницы
идут «внахлёст» (половина повторяется), поэтому лимит магазина Ozon скромнее.
Цена в плитке — цена по карте Ozon, а не обычная: такие товары помечаем
источником OZON_CARD, чтобы их цены не смешивались с ценами страницы товара.

Ручная проверка:
    .venv\\Scripts\\python -m app.sources.ozon "<ссылка на товар Ozon>"
    .venv\\Scripts\\python -m app.sources.ozon search "стеклянный коврик"
    .venv\\Scripts\\python -m app.sources.ozon seller https://www.ozon.ru/seller/ridberg/
"""
import asyncio
import json
import logging
import random
import re
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlsplit

from .base import (OZON_CARD, UNKNOWN_STOCK, BlockedError, ProductInfo, SourceError,
                   format_price)

log = logging.getLogger(__name__)

SOURCE = "ozon"
SITE = "https://www.ozon.ru"
API = SITE + "/api/entrypoint-api.bx/page/json/v2?url="
BLOCKED_STATUSES = {401, 403, 429}

# /product/kovrik-dlya-myshki-3990933477/ → артикул 3990933477 (число в конце)
PRODUCT_RE = re.compile(r"ozon\.ru/product/(?:[^/?#]*-)?(\d+)/?")
# Короткие ссылки из приложения: https://ozon.ru/t/AbCdEf
SHORT_RE = re.compile(r"ozon\.ru/t/[\w-]+")
SELLER_RE = re.compile(r"ozon\.ru/seller/([\w-]+)")
DIGITS = re.compile(r"\d+")
STOCK_LEFT = re.compile(r"(\d[\d\s\u2009\u00a0]*)\s*ед\.?\s*остал")
SELLER_LIMIT_CAP = 100   # магазины Ozon листаются «внахлёст» — больше брать дорого
MAX_EXTRA_PAGES = 2      # запас страниц сверх расчётных (повторы, реклама)


# --- разбор ссылок ---

def matches(url: str) -> bool:
    """Подходит ли ссылка на товар этому источнику."""
    return bool(PRODUCT_RE.search(url) or SHORT_RE.search(url))


def seller_id_from_url(text: str) -> str:
    """https://www.ozon.ru/seller/ridberg/ → 'ridberg' (или число, если ссылка с ID).
    Уже готовый ID ('ridberg') возвращается как есть — так он хранится в базе."""
    text = text.strip()
    if re.fullmatch(r"[\w-]+", text):
        return text
    match = SELLER_RE.search(text)
    if not match:
        raise SourceError(f"Это не ссылка на магазин Ozon: {text!r}")
    return match.group(1)


def matches_seller(url: str) -> bool:
    return SELLER_RE.search(url) is not None


def article_from_url(text: str) -> str:
    text = text.strip()
    if text.isdigit():
        return text
    match = PRODUCT_RE.search(text)
    if not match:
        raise SourceError(f"Это не ссылка на товар Ozon: {text!r}")
    return match.group(1)


def product_path(text: str) -> str:
    """Путь страницы товара для API: /product/slug-123/ (без параметров ?at=…).
    Для одного артикула — /product/123/ (Ozon сам найдёт товар)."""
    text = text.strip()
    if text.isdigit():
        return f"/product/{text}/"
    path = urlsplit(text if "://" in text else "https://" + text).path
    if not path.endswith("/"):
        path += "/"
    return path


# --- разбор ответа (без сети — легко тестировать) ---

def parse_rubles(text: str | None) -> int | None:
    """'3 427 ₽' (с узкими пробелами) → 342700 копеек. Пусто → None."""
    if not text:
        return None
    digits = "".join(DIGITS.findall(text))
    return int(digits) * 100 if digits else None


def _states(data: dict) -> dict[str, dict]:
    """widgetStates: {"webPrice-3121879-default-1": "<json-строка>"} →
    {"webPrice": {…}} — по первому виджету каждого вида."""
    result = {}
    for key, value in (data.get("widgetStates") or {}).items():
        kind = key.split("-", 1)[0]
        if kind in result:
            continue
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                continue
        if isinstance(value, dict):
            result[kind] = value
    return result


def _canonical_url(data: dict, article: str) -> str:
    for link in (data.get("seo") or {}).get("link") or []:
        if link.get("rel") == "canonical" and link.get("href"):
            return link["href"]
    return f"{SITE}/product/{article}/"


def parse_product(data: dict, article: str | None = None) -> ProductInfo:
    """Товар из ответа API страницы Ozon."""
    states = _states(data)
    heading = states.get("webProductHeading", {})
    sticky = states.get("webStickyProducts", {})
    price = states.get("webPrice")
    name = heading.get("title") or sticky.get("name")
    if not name:
        raise SourceError("Ozon не вернул данные товара — возможно, товар удалён "
                          "или изменился формат страницы")
    article = article or str(sticky.get("sku") or "")

    available = bool(price and price.get("isAvailable", True))
    current = parse_rubles(price.get("price")) if available else None
    card = parse_rubles(price.get("cardPrice")) if available else None
    original = parse_rubles(price.get("originalPrice")) if available else None
    if current is None:       # бывает, что цена только по карте
        current = card

    seller = sticky.get("seller") or {}
    seller_match = SELLER_RE.search(seller.get("link") or "")
    score = states.get("webReviewProductScore", {})
    feedbacks = int(score.get("reviewsCount") or 0)

    return ProductInfo(
        source=SOURCE,
        article=article,
        url=_canonical_url(data, article),
        name=name,
        price=current,
        old_price=original if original and current and original > current else None,
        quantity=UNKNOWN_STOCK if available and current is not None else 0,  # только есть/нет
        seller=seller.get("name", ""),
        seller_id=seller_match.group(1) if seller_match else "",
        rating=float(score["totalScore"]) if feedbacks and score.get("totalScore") else None,
        feedbacks=feedbacks,
        card_price=card if card != current else None,
    )


# --- плитки поиска и магазина ---

def _tile_name(tile: dict) -> str:
    for state in tile.get("mainState", []):
        if state.get("type") == "textDS" and (
                state.get("id") == "name"
                or (state["textDS"].get("testInfo") or {}).get("automatizationId") == "tile-name"):
            return state["textDS"].get("text", "")
    return ""


def _tile_prices(tile: dict) -> tuple[int | None, int | None]:
    """(цена, цена до скидки) из плитки."""
    price = original = None
    for state in tile.get("mainState", []):
        if state.get("type") != "priceV2":
            continue
        for item in state["priceV2"].get("price", []):
            if item.get("textStyle") == "PRICE":
                price = parse_rubles(item.get("text"))
            elif item.get("textStyle") == "ORIGINAL_PRICE":
                original = parse_rubles(item.get("text"))
    return price, original


def _tile_stock(tile: dict) -> int | None:
    """«225 ед осталось» → 225; нет такой надписи → None."""
    for state in tile.get("mainState", []):
        if state.get("type") == "textDS":
            match = STOCK_LEFT.search(state["textDS"].get("text", ""))
            if match:
                return int("".join(DIGITS.findall(match.group(1))))
    return None


def _tile_rating(tile: dict) -> tuple[float | None, int]:
    """Рейтинг и число отзывов: в плитке это значки ⭐ и 💬, за каждым — текст."""
    rating, feedbacks = None, 0
    for state in tile.get("mainState", []):
        if state.get("type") != "labelListV2":
            continue
        items = state["labelListV2"].get("items", [])
        for icon, text in zip(items, items[1:]):
            name = ((icon.get("icon") or {}).get("icon") or {}).get("icon", "")
            value = (text.get("text") or {}).get("text", "")
            if "star" in name and value:
                try:
                    rating = float(value.replace(",", "."))
                except ValueError:
                    pass
            elif "dialog" in name and value:
                feedbacks = int("".join(DIGITS.findall(value)) or 0)
    return (rating if feedbacks else None), feedbacks


def parse_tile(tile: dict, seller: str = "", seller_id: str = "") -> ProductInfo | None:
    """Товар из плитки каталога. Без цены или ссылки — None (пропускаем)."""
    article = str(tile.get("sku") or tile.get("id") or "")
    link = (tile.get("action") or {}).get("link", "")
    price, original = _tile_prices(tile)
    if not article or not link or price is None:
        return None
    stock = _tile_stock(tile)
    rating, feedbacks = _tile_rating(tile)
    return ProductInfo(
        source=OZON_CARD,                # цена по карте Ozon — хранится отдельно
        article=article,
        url=SITE + urlsplit(link).path,  # без ?at=… — это одноразовые метки
        name=_tile_name(tile),
        price=price,
        old_price=original if original and original > price else None,
        quantity=stock if stock is not None else UNKNOWN_STOCK,
        seller=seller,
        seller_id=seller_id,
        rating=rating,
        feedbacks=feedbacks,
    )


def parse_tiles(data: dict, seller: str = "", seller_id: str = "") -> list[ProductInfo]:
    """Все товары из ответа API страницы каталога (по порядку)."""
    result = []
    for key, value in (data.get("widgetStates") or {}).items():
        if not key.startswith("tileGrid"):
            continue
        grid = json.loads(value) if isinstance(value, str) else value
        for tile in grid.get("items", []):
            product = parse_tile(tile, seller, seller_id)
            if product is not None:
                result.append(product)
    return result


def next_page(data: dict) -> str | None:
    """Ссылка на следующую порцию товаров (подгрузка при прокрутке)."""
    for key, value in (data.get("widgetStates") or {}).items():
        if key.startswith("infiniteVirtualPaginator"):
            state = json.loads(value) if isinstance(value, str) else value
            return state.get("nextPage") or None
    return None


def seller_name(data: dict) -> str:
    """Название магазина со страницы продавца."""
    state = _states(data).get("sellerTransparency", {})
    return (state.get("title") or {}).get("text", "")


# --- работа с сайтом через браузер ---

class OzonSource:
    name = SOURCE

    def __init__(self, profile_dir: Path, pause: tuple[float, float] = (1.5, 3.5)):
        self.profile_dir = profile_dir
        self.pause = pause
        # Один профиль браузера нельзя открыть дважды одновременно —
        # проверка по расписанию и кнопка «Обновить» ждут друг друга
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def browser(self):
        """Открыть Chrome на главной Ozon (чтобы у запросов были «родные» cookie)
        и закрыть по окончании. Внутри можно сделать много запросов."""
        from patchright.async_api import async_playwright   # нужен только здесь

        async with self._lock, async_playwright() as p:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir), channel="chrome",
                headless=False, no_viewport=True)
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                await page.goto(SITE + "/", wait_until="domcontentloaded", timeout=60_000)
                await asyncio.sleep(5)   # даём пройти проверке антибота
                yield page
            finally:
                await context.close()

    async def _api(self, page, path: str) -> dict:
        """Запрос к API страницы изнутри браузера."""
        # safe: в ссылках nextPage уже есть ?, &, = и %-коды — их не трогаем
        result = await page.evaluate(
            """async (url) => {
                const r = await fetch(url, {credentials: "include"});
                return {status: r.status, text: await r.text()};
            }""", API + quote(path, safe="/?=&%"))
        status = result["status"]
        if status in BLOCKED_STATUSES:
            raise BlockedError(f"Ozon не пускает ({status})")
        if status != 200:
            raise SourceError(f"Ozon ответил {status}")
        try:
            return json.loads(result["text"])
        except ValueError:
            raise SourceError("Ozon прислал не JSON — возможно, изменился формат ответа")

    async def _resolve(self, page, url: str) -> str:
        """Короткую ссылку из приложения (ozon.ru/t/…) превращаем в обычную."""
        if not SHORT_RE.search(url):
            return url
        await page.goto(url if "://" in url else "https://" + url,
                        wait_until="domcontentloaded", timeout=60_000)
        if not PRODUCT_RE.search(page.url):
            raise SourceError("Короткая ссылка Ozon не ведёт на товар")
        return page.url

    async def _fetch_on(self, page, url: str) -> ProductInfo:
        url = await self._resolve(page, url)
        data = await self._api(page, product_path(url))
        return parse_product(data, article_from_url(url))

    async def fetch(self, url: str) -> ProductInfo:
        """Один товар (по ссылке или артикулу)."""
        async with self.browser() as page:
            return await self._fetch_on(page, url)

    async def fetch_many(self, urls: list[str]) -> list[ProductInfo]:
        """Много товаров за одно открытие браузера. Ошибка одного товара
        не мешает остальным; если Ozon блокирует — останавливаемся."""
        result = []
        if not urls:
            return result
        async with self.browser() as page:
            for i, url in enumerate(urls):
                if i:
                    await asyncio.sleep(random.uniform(*self.pause))
                try:
                    result.append(await self._fetch_on(page, url))
                except BlockedError:
                    raise
                except SourceError as e:
                    log.warning("Ozon %s: %s", url, e)
        return result


    # --- поиск и магазин ---

    async def _catalog(self, start: str, limit: int, seller: str = "",
                       seller_id: str = "", first: dict | None = None,
                       page=None) -> list[ProductInfo]:
        """Листаем каталог: страницы &page=N, внутри — порции по nextPage.
        Повторы (реклама, «внахлёст» у магазинов) отбрасываем, порядок сохраняем."""
        found: dict[str, ProductInfo] = {}
        max_pages = limit // 8 + MAX_EXTRA_PAGES
        for number in range(1, max_pages + 1):
            sep = "&" if "?" in start else "?"
            path = start if number == 1 else f"{start}{sep}page={number}"
            before = len(found)
            while path and len(found) < limit:
                data = first if (first is not None and number == 1 and path == start) \
                    else await self._api(page, path)
                for product in parse_tiles(data, seller, seller_id):
                    found.setdefault(product.article, product)
                path = next_page(data)
                if path:
                    await asyncio.sleep(random.uniform(*self.pause))
            if len(found) >= limit or len(found) == before:
                break   # набрали или страница не дала новых — дальше не идём
            await asyncio.sleep(random.uniform(*self.pause))
        return list(found.values())[:limit]

    async def search(self, query: str, limit: int = 30) -> list[ProductInfo]:
        """Топ поисковой выдачи Ozon (как видит покупатель, с рекламой)."""
        async with self.browser() as page:
            return await self._catalog("/search/?text=" + quote(query), limit, page=page)

    async def seller(self, url: str, limit: int = SELLER_LIMIT_CAP) -> list[ProductInfo]:
        """Товары магазина Ozon (не больше SELLER_LIMIT_CAP)."""
        seller_id = seller_id_from_url(url)
        start = f"/seller/{seller_id}/"
        async with self.browser() as page:
            first = await self._api(page, start)
            name = seller_name(first) or seller_id
            return await self._catalog(start, min(limit, SELLER_LIMIT_CAP), name, seller_id,
                                       first=first, page=page)


async def _demo(args: list[str]) -> None:
    source = OzonSource(Path("data/ozon_browser_profile"))
    if args and args[0] in ("search", "seller"):
        products = (await source.search(" ".join(args[1:])) if args[0] == "search"
                    else await source.seller(args[1]))
        for i, p in enumerate(products, 1):
            stock = f"{p.quantity} шт." if p.quantity > 0 else "есть"
            print(f"{i:>3}. {format_price(p.price):>9} (по карте)  {stock:>8}  "
                  f"{p.rating or '—'}  {p.name[:60]}")
        print(f"Всего: {len(products)}")
        return
    url = args[0] if args else "1291633069"
    info = await source.fetch(url)
    card = f", по карте {format_price(info.card_price)}" if info.card_price else ""
    print(f"{info.name}\nЦена: {format_price(info.price)}{card} "
          f"(без скидки: {format_price(info.old_price)})\n"
          f"В наличии: {'да' if info.in_stock else 'нет'}\n"
          f"Продавец: {info.seller}, рейтинг: {info.rating}, отзывов: {info.feedbacks}\n"
          f"{info.url}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    asyncio.run(_demo(sys.argv[1:]))
