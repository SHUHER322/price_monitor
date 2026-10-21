"""Источник Ozon.

Как работает (проверено разведкой scripts/ozon_probe*.py, октябрь 2026):
1. Данные страницы товара отдаёт API сайта /api/entrypoint-api.bx/page/json/v2.
2. Ozon проверяет не только cookie, но и «почерк» соединения (TLS-отпечаток):
   httpx и даже curl_cffi с подделкой Chrome получают 403. Пускает только
   настоящий браузер. Поэтому запросы делает сам Chrome (patchright) — через
   fetch изнутри открытой страницы.
3. На одну проверку браузер открывается один раз, все товары запрашиваются
   в нём по очереди с паузами, потом браузер закрывается.

Чего у Ozon нет: количества на складе — только «есть / нет». Поэтому quantity
у товаров Ozon условное: 1 — в наличии, 0 — нет (см. EXACT_STOCK в base.py).

Ручная проверка:
    .venv\\Scripts\\python -m app.sources.ozon "<ссылка на товар Ozon>"
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

from .base import BlockedError, ProductInfo, SourceError, format_price

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


# --- разбор ссылок ---

def matches(url: str) -> bool:
    """Подходит ли ссылка на товар этому источнику."""
    return bool(PRODUCT_RE.search(url) or SHORT_RE.search(url))


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
        quantity=1 if available and current is not None else 0,   # Ozon: только есть/нет
        seller=seller.get("name", ""),
        seller_id=seller_match.group(1) if seller_match else "",
        rating=float(score["totalScore"]) if feedbacks and score.get("totalScore") else None,
        feedbacks=feedbacks,
        card_price=card if card != current else None,
    )


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
        result = await page.evaluate(
            """async (url) => {
                const r = await fetch(url, {credentials: "include"});
                return {status: r.status, text: await r.text()};
            }""", API + quote(path, safe="/"))
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


async def _demo(url: str) -> None:
    info = await OzonSource(Path("data/ozon_browser_profile")).fetch(url)
    card = f", по карте {format_price(info.card_price)}" if info.card_price else ""
    print(f"{info.name}\nЦена: {format_price(info.price)}{card} "
          f"(без скидки: {format_price(info.old_price)})\n"
          f"В наличии: {'да' if info.in_stock else 'нет'}\n"
          f"Продавец: {info.seller}, рейтинг: {info.rating}, отзывов: {info.feedbacks}\n"
          f"{info.url}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    asyncio.run(_demo(sys.argv[1] if len(sys.argv) > 1 else "1291633069"))
