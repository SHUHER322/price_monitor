"""Источник Wildberries.

Как работает (проверено разведкой scripts/wb_probe*.py, октябрь 2026):
1. Данные отдают внутренние адреса сайта /__internal/..., но только
   с антибот-токеном (cookie x_wbaas_token) и заголовками браузера.
2. Токен получаем через настоящий Chrome (patchright) и сохраняем в файл.
3. Дальше данные берём быстрыми запросами httpx с этим токеном.
4. Если WB отверг токен (401/403/498) — один раз получаем новый и повторяем.

Что умеет:
    fetch(ссылка)          — один товар, с точными остатками по размерам
    fetch_many(артикулы)   — много товаров, пачками по 100 за запрос
    search(запрос, limit)  — топ товаров поисковой выдачи
    seller(ссылка, limit)  — все товары магазина

Ручная проверка:
    .venv\\Scripts\\python -m app.sources.wb <ссылка или артикул>
    .venv\\Scripts\\python -m app.sources.wb search "стеклянный коврик"
    .venv\\Scripts\\python -m app.sources.wb seller <ссылка на магазин>
"""
import asyncio
import json
import logging
import random
import re
import sys
from pathlib import Path

import httpx

from .base import BlockedError, ProductInfo, SizeInfo, SourceError, format_price

log = logging.getLogger(__name__)

SOURCE = "wb"
SITE = "https://www.wildberries.ru"
CARD_DETAIL = "/__internal/u-card/cards/v4/detail"   # один товар, с остатками
CARD_LIST = "/__internal/u-card/cards/v4/list"       # много товаров разом
SEARCH = "/__internal/u-search/exactmatch/ru/common/v18/search"
SELLER_CATALOG = "/__internal/u-catalog/sellers/v4/catalog"

PRODUCT_PAGE = SITE + "/catalog/{article}/detail.aspx"
SELLER_PAGE = SITE + "/seller/{seller_id}"
SEARCH_PAGE = SITE + "/catalog/0/search.aspx?search={query}"

TOKEN_COOKIE = "x_wbaas_token"
# Так начинаются адреса запросов сайта к данным (не к антиботу)
DATA_REQUEST_MARK = "/__internal/u-"
# Коды, которыми WB отвечает на плохой или протухший токен
BLOCKED_STATUSES = {401, 403, 498}
# Регион доставки: от него зависят цена и наличие. Позже вынесем в .env
DEFAULT_DEST = "1259570991"
# Остаток WB отдаёт со склада, ближайшего к региону, и, похоже, ограничивает
# показываемое число. Потолок не постоянный (видели 43, потом 49), поэтому
# в код его не зашиваем, а честно пишем об ограничении пользователю.
DEFAULT_SEARCH_LIMIT = 30   # дальше первых экранов выдачи покупатели почти не смотрят
DEFAULT_SELLER_LIMIT = 1000
LIST_BATCH = 100            # столько артикулов WB принимает в одном запросе списка
PAGE_SIZE = 100             # товаров на странице поиска и магазина
# Эти заголовки httpx выставит сам; cookie передаём отдельно
SKIP_HEADERS = {"cookie", "host", "content-length"}

ARTICLE_RE = re.compile(r"wildberries\.ru/catalog/(\d+)")
SELLER_RE = re.compile(r"wildberries\.ru/seller/(\d+)")


# --- разбор ссылок ---

def article_from_url(text: str) -> str:
    """Артикул из ссылки WB или из самого числа."""
    text = text.strip()
    if text.isdigit():
        return text
    match = ARTICLE_RE.search(text)
    if not match:
        raise SourceError(f"Это не ссылка на товар Wildberries: {text!r}")
    return match.group(1)


def seller_id_from_url(text: str) -> str:
    """ID продавца из ссылки на магазин WB или из самого числа."""
    text = text.strip()
    if text.isdigit():
        return text
    match = SELLER_RE.search(text)
    if not match:
        raise SourceError(f"Это не ссылка на магазин Wildberries: {text!r}")
    return match.group(1)


def matches(url: str) -> bool:
    """Подходит ли ссылка на товар этому источнику."""
    return ARTICLE_RE.search(url) is not None


# --- разбор ответов (без сети — легко тестировать) ---

def parse_products(data: dict) -> list[ProductInfo]:
    """Все товары из ответа WB. Формат одинаковый у карточки, списка, поиска и магазина."""
    products = data.get("products") or (data.get("data") or {}).get("products") or []
    return [_parse_product(p) for p in products]


def parse_card(data: dict, article: str) -> ProductInfo:
    """Один нужный товар из ответа."""
    for product in parse_products(data):
        if product.article == article:
            return product
    raise SourceError(f"Товар {article} не найден (удалён или неверный артикул)")


def _parse_product(product: dict) -> ProductInfo:
    article = str(product.get("id"))
    sizes = tuple(_parse_size(s) for s in product.get("sizes", []))
    # Общая цена товара — самая низкая среди размеров, которые есть в продаже
    priced = [s for s in sizes if s.price is not None]
    best = min(priced, key=lambda s: s.price) if priced else None
    feedbacks = product.get("feedbacks", 0)
    # Без отзывов WB пишет рейтинг 0 — но это «не оценивали», а не «плохо»
    rating = product.get("reviewRating", product.get("rating")) if feedbacks else None

    return ProductInfo(
        source=SOURCE,
        article=article,
        url=PRODUCT_PAGE.format(article=article),
        name=product.get("name", ""),
        price=best.price if best else None,
        old_price=best.old_price if best else None,
        quantity=product.get("totalQuantity", 0),
        sizes=sizes,
        seller=product.get("supplier", ""),
        seller_id=str(product.get("supplierId", "")),
        brand=product.get("brand", ""),
        rating=float(rating) if rating is not None else None,
        feedbacks=feedbacks,
    )


def _parse_size(size: dict) -> SizeInfo:
    # У товара без размеров WB пишет name="" и origName="0"
    name = size.get("name") or size.get("origName") or ""
    if name == "0":
        name = ""
    price = size.get("price") or {}
    # Остатки по складам есть только в карточке одного товара; в списках их нет
    stocks = size.get("stocks")
    quantity = sum(s.get("qty", 0) for s in stocks) if stocks is not None else None
    return SizeInfo(
        name=name,
        price=price.get("product"),
        old_price=price.get("basic"),
        quantity=quantity,
    )


# --- работа с сайтом ---

class WBSource:
    name = SOURCE

    def __init__(self, session_file: Path, profile_dir: Path, dest: str = DEFAULT_DEST,
                 transport: httpx.AsyncBaseTransport | None = None,
                 pause: tuple[float, float] = (1.0, 2.5)):
        self.session_file = session_file
        self.profile_dir = profile_dir
        self.dest = dest
        # transport — «подмена сети» для тестов (httpx.MockTransport). В работе None
        self._transport = transport
        # Пауза между запросами при перелистывании, секунды (от, до)
        self._pause = pause
        # Пауза перед повтором после сбоя сети, секунды
        self._retry_delay = 3 if pause[1] > 0 else 0
        self.session = self._load_session()
        # Если несколько запросов одновременно наткнулись на протухший токен,
        # браузер должен запуститься один раз, а не на каждый запрос
        self._refresh_lock = asyncio.Lock()

    # --- сессия (токен + заголовки) ---

    def _load_session(self) -> dict | None:
        try:
            return json.loads(self.session_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def _save_session(self, session: dict) -> None:
        self.session_file.parent.mkdir(parents=True, exist_ok=True)
        self.session_file.write_text(json.dumps(session, ensure_ascii=False, indent=2),
                                     encoding="utf-8")

    async def _refresh_session(self, page_url: str, stale: dict | None) -> None:
        async with self._refresh_lock:
            # Пока ждали блокировку, сессию мог уже обновить другой запрос
            if self.session is not stale and self.session is not None:
                return
            log.info("WB: получаю новый антибот-токен через браузер")
            self.session = await self._session_from_browser(page_url)
            self._save_session(self.session)

    async def _session_from_browser(self, page_url: str) -> dict:
        # Импорт внутри функции: браузер нужен редко, и без него модуль
        # (например, в тестах) работает, даже если patchright не установлен
        from patchright.async_api import async_playwright

        headers: dict = {}

        def on_request(request):
            # Заголовки берём у первого запроса сайта к данным (/__internal/u-card,
            # u-search, u-catalog). Запросы самого антибота (/__internal/antibot/)
            # идут раньше, но в них нет нужных заголовков (x-spa-version и др.)
            if not headers and DATA_REQUEST_MARK in request.url:
                headers.update({k: v for k, v in request.headers.items()
                                if k.lower() not in SKIP_HEADERS and not k.startswith(":")})

        async with async_playwright() as p:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
                channel="chrome",
                headless=False,
                no_viewport=True,
            )
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                page.on("request", on_request)
                await page.goto(page_url, wait_until="domcontentloaded", timeout=60_000)
                cookies = {}
                for _ in range(90):  # проверка антибота может идти около минуты
                    cookies = {c["name"]: c["value"] for c in await context.cookies()
                               if "wildberries" in c["domain"]}
                    if TOKEN_COOKIE in cookies and headers:
                        break
                    await asyncio.sleep(1)
            finally:
                await context.close()

        if TOKEN_COOKIE not in cookies or not headers:
            raise BlockedError("WB не выдал антибот-токен: браузер не прошёл проверку")
        return {"cookies": cookies, "headers": headers}

    # --- запросы ---

    def _params(self, **extra) -> dict:
        """Параметры, которые сайт WB передаёт во всех запросах."""
        return {"appType": "1", "curr": "rub", "dest": self.dest, "spp": "30",
                "hide_vflags": "4294967296", "lang": "ru", "ab_testing": "false", **extra}

    async def _send(self, path: str, params: dict) -> httpx.Response:
        """Отправить запрос с текущей сессией. Каждый раз — новое соединение:
        после отказа WB старое соединение может «испортиться» (присылает мусор),
        особенно если оно висело минуту, пока браузер получал токен."""
        session = self.session
        # Cookie передаём заголовком: так httpx не смешивает их между запросами
        cookie = "; ".join(f"{k}={v}" for k, v in session["cookies"].items())
        headers = {**session["headers"], "cookie": cookie}
        for attempt in (1, 2):
            try:
                async with self._client() as client:
                    return await client.get(SITE + path, params=params, headers=headers)
            except httpx.TransportError as e:
                # Сбой сети или соединения — одна повторная попытка после паузы
                if attempt == 2:
                    raise SourceError(f"WB: ошибка сети ({e.__class__.__name__})") from e
                log.info("WB: сбой соединения (%s), повторяю", e.__class__.__name__)
                await asyncio.sleep(self._retry_delay)

    async def _get_json(self, path: str, params: dict, page_url: str) -> dict:
        """Один запрос к WB с обработкой протухшего токена.

        page_url — страница, которую откроет браузер, если понадобится новый токен.
        Вся логика «отказ → новый токен → повтор» — здесь, в одном месте.
        """
        if self.session is None:
            await self._refresh_session(page_url, stale=None)

        for attempt in (1, 2):
            session = self.session
            response = await self._send(path, params)
            if response.status_code not in BLOCKED_STATUSES:
                break
            if attempt == 2:
                raise BlockedError(f"WB не пускает даже с новым токеном "
                                   f"({response.status_code})")
            log.info("WB: токен отвергнут (%s), обновляю", response.status_code)
            await self._refresh_session(page_url, stale=session)

        if response.status_code != 200:
            raise SourceError(f"WB ответил {response.status_code}")
        try:
            return response.json()
        except ValueError:
            raise SourceError("WB прислал не JSON — возможно, изменился формат ответа")

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=15, transport=self._transport)

    async def _sleep(self) -> None:
        """Случайная пауза между запросами — как у человека, а не у робота."""
        low, high = self._pause
        if high > 0:
            await asyncio.sleep(random.uniform(low, high))

    # --- что умеет источник ---

    async def fetch(self, url: str) -> ProductInfo:
        """Один товар, с точными остатками по размерам."""
        article = article_from_url(url)
        data = await self._get_json(CARD_DETAIL, self._params(nm=article),
                                    PRODUCT_PAGE.format(article=article))
        return parse_card(data, article)

    async def fetch_many(self, articles: list[str]) -> list[ProductInfo]:
        """Много товаров, по LIST_BATCH за запрос. Остатки по размерам неизвестны.
        Удалённые товары WB просто не возвращает — их в результате не будет."""
        result = []
        for start in range(0, len(articles), LIST_BATCH):
            if start:
                await self._sleep()
            batch = articles[start:start + LIST_BATCH]
            data = await self._get_json(CARD_LIST, self._params(nm=";".join(batch)),
                                        PRODUCT_PAGE.format(article=batch[0]))
            result += parse_products(data)
        return result

    async def search(self, query: str, limit: int = DEFAULT_SEARCH_LIMIT) -> list[ProductInfo]:
        """Топ товаров поисковой выдачи (сортировка «по популярности», как у покупателя)."""
        params = {"query": query, "resultset": "catalog", "sort": "popular",
                  "suppressSpellcheck": "false", "locale": "ru"}
        return await self._paginate(SEARCH, params, limit, SEARCH_PAGE.format(query=query))

    async def seller(self, url: str, limit: int = DEFAULT_SELLER_LIMIT) -> list[ProductInfo]:
        """Все товары магазина (до limit)."""
        seller_id = seller_id_from_url(url)
        params = {"supplier": seller_id, "sort": "popular"}
        return await self._paginate(SELLER_CATALOG, params, limit,
                                    SELLER_PAGE.format(seller_id=seller_id))

    async def _paginate(self, path: str, params: dict, limit: int,
                        page_url: str) -> list[ProductInfo]:
        """Листаем страницы, пока не наберём limit или товары не кончатся."""
        result: list[ProductInfo] = []
        page = 1
        while len(result) < limit:
            if page > 1:
                await self._sleep()
            data = await self._get_json(path, self._params(**params, page=str(page)), page_url)
            products = parse_products(data)
            result += products
            total = data.get("total", 0)
            if len(products) < PAGE_SIZE or len(result) >= total:
                break  # последняя страница
            page += 1
        return result[:limit]


# --- ручная проверка ---

def _print_product(info: ProductInfo) -> None:
    print(info.name)
    if info.has_sizes:
        print(f"{'Размер':<10}{'Цена':>12}{'Без скидки':>14}{'В наличии':>12}")
        for s in info.sizes:
            if s.quantity is not None:
                stock = f"{s.quantity} шт." if s.quantity else "нет"
            else:
                stock = "есть" if s.in_stock else "нет"
            print(f"{s.name:<10}{format_price(s.price):>12}"
                  f"{format_price(s.old_price):>14}{stock:>12}")
        print(f"Минимальная цена: {format_price(info.price)}")
    else:
        print(f"Цена: {format_price(info.price)} (без скидки: {format_price(info.old_price)})\n"
              f"В наличии: {info.quantity} шт.")
    rating = f"{info.rating:.1f}" if info.rating is not None else "нет оценок"
    print(f"Продавец: {info.seller}, рейтинг: {rating}, отзывов: {info.feedbacks}")
    print(info.url)


def _print_table(products: list[ProductInfo]) -> None:
    print(f"{'№':>3}  {'Артикул':<11}{'Цена':>10}  {'Рейт.':>5} {'Отз.':>6}  "
          f"{'Продавец':<20}Название")
    for i, p in enumerate(products, 1):
        rating = f"{p.rating:.1f}" if p.rating is not None else "—"
        print(f"{i:>3}  {p.article:<11}{format_price(p.price):>10}  {rating:>5} "
              f"{p.feedbacks:>6}  {p.seller[:19]:<20}{p.name[:45]}")
    print(f"Всего: {len(products)}")


async def _demo(args: list[str]) -> None:
    source = WBSource(Path("data/wb_session.json"), Path("data/wb_browser_profile"))
    if args and args[0] == "search":
        _print_table(await source.search(" ".join(args[1:])))
    elif args and args[0] == "seller":
        _print_table(await source.seller(args[1]))
    else:
        _print_product(await source.fetch(args[0] if args else "683146715"))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    asyncio.run(_demo(sys.argv[1:]))
