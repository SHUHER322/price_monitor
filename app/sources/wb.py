"""Источник Wildberries.

Как работает (проверено разведкой scripts/wb_probe.py, октябрь 2026):
1. Цену отдаёт внутренний адрес сайта /__internal/u-card/cards/v4/detail,
   но только с антибот-токеном (cookie x_wbaas_token) и заголовками браузера.
2. Токен получаем один раз через настоящий Chrome (patchright) и сохраняем в файл.
3. Дальше цены берём быстрыми запросами httpx с этим токеном.
4. Если WB отверг токен (401/403/498) — один раз получаем новый и повторяем.

Ручная проверка:
    .venv\\Scripts\\python -m app.sources.wb <ссылка или артикул>
"""
import asyncio
import json
import logging
import re
import sys
from pathlib import Path

import httpx

from .base import BlockedError, ProductInfo, SizeInfo, SourceError, format_price

log = logging.getLogger(__name__)

SOURCE = "wb"
CARD_URL = "https://www.wildberries.ru/__internal/u-card/cards/v4/detail"
PRODUCT_PAGE = "https://www.wildberries.ru/catalog/{article}/detail.aspx"
TOKEN_COOKIE = "x_wbaas_token"
# Коды, которыми WB отвечает на плохой или протухший токен
BLOCKED_STATUSES = {401, 403, 498}
# Регион доставки: от него зависят цена и наличие. Позже вынесем в .env
DEFAULT_DEST = "1259570991"
# Эти заголовки httpx выставит сам; cookie передаём отдельно
SKIP_HEADERS = {"cookie", "host", "content-length"}

ARTICLE_RE = re.compile(r"wildberries\.ru/catalog/(\d+)")


def article_from_url(text: str) -> str:
    """Артикул из ссылки WB или из самого числа."""
    text = text.strip()
    if text.isdigit():
        return text
    match = ARTICLE_RE.search(text)
    if not match:
        raise SourceError(f"Это не ссылка на товар Wildberries: {text!r}")
    return match.group(1)


def matches(url: str) -> bool:
    """Подходит ли ссылка этому источнику."""
    return ARTICLE_RE.search(url) is not None


def parse_card(data: dict, article: str) -> ProductInfo:
    """Разбор ответа WB. Отдельная функция без сети — её легко тестировать."""
    products = data.get("products") or (data.get("data") or {}).get("products") or []
    product = next((p for p in products if str(p.get("id")) == article), None)
    if product is None:
        raise SourceError(f"Товар {article} не найден (удалён или неверный артикул)")

    sizes = tuple(_parse_size(s) for s in product.get("sizes", []))
    # Общая цена товара — самая низкая среди размеров, которые есть в продаже
    priced = [s for s in sizes if s.price is not None]
    best = min(priced, key=lambda s: s.price) if priced else None

    return ProductInfo(
        source=SOURCE,
        article=article,
        url=PRODUCT_PAGE.format(article=article),
        name=product.get("name", ""),
        price=best.price if best else None,
        old_price=best.old_price if best else None,
        quantity=product.get("totalQuantity", sum(s.quantity for s in sizes)),
        sizes=sizes,
    )


def _parse_size(size: dict) -> SizeInfo:
    # У товара без размеров WB пишет name="" и origName="0"
    name = size.get("name") or size.get("origName") or ""
    if name == "0":
        name = ""
    price = size.get("price") or {}
    # Остаток размера лежит по складам — складываем
    quantity = sum(stock.get("qty", 0) for stock in size.get("stocks", []))
    return SizeInfo(
        name=name,
        price=price.get("product"),
        old_price=price.get("basic"),
        quantity=quantity,
    )


class WBSource:
    name = SOURCE

    def __init__(self, session_file: Path, profile_dir: Path, dest: str = DEFAULT_DEST):
        self.session_file = session_file
        self.profile_dir = profile_dir
        self.dest = dest
        self.session = self._load_session()
        # Если несколько товаров одновременно наткнулись на протухший токен,
        # браузер должен запуститься один раз, а не на каждый товар
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

    async def _refresh_session(self, article: str, stale: dict | None) -> None:
        async with self._refresh_lock:
            # Пока ждали блокировку, сессию мог уже обновить другой запрос
            if self.session is not stale and self.session is not None:
                return
            log.info("WB: получаю новый антибот-токен через браузер")
            self.session = await self._session_from_browser(article)
            self._save_session(self.session)

    async def _session_from_browser(self, article: str) -> dict:
        # Импорт внутри функции: браузер нужен редко, и без него модуль
        # (например, в тестах) работает, даже если patchright не установлен
        from patchright.async_api import async_playwright

        headers: dict = {}

        def on_request(request):
            # Заголовки берём у первого запроса сайта к /__internal/
            if not headers and "/__internal/" in request.url:
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
                await page.goto(PRODUCT_PAGE.format(article=article),
                                wait_until="domcontentloaded", timeout=60_000)
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

    # --- получение товара ---

    async def _request_card(self, client: httpx.AsyncClient, article: str) -> httpx.Response:
        params = {
            "appType": "1", "curr": "rub", "dest": self.dest, "spp": "30",
            "hide_vflags": "4294967296", "mtype": "257", "lang": "ru",
            "ab_testing": "false", "nm": article,
        }
        return await client.get(CARD_URL, params=params,
                                headers=self.session["headers"],
                                cookies=self.session["cookies"])

    async def fetch(self, url: str) -> ProductInfo:
        article = article_from_url(url)
        if self.session is None:
            await self._refresh_session(article, stale=None)

        async with httpx.AsyncClient(timeout=15) as client:
            response = await self._request_card(client, article)
            if response.status_code in BLOCKED_STATUSES:
                log.info("WB: токен отвергнут (%s), обновляю", response.status_code)
                await self._refresh_session(article, stale=self.session)
                response = await self._request_card(client, article)
                if response.status_code in BLOCKED_STATUSES:
                    raise BlockedError(f"WB не пускает даже с новым токеном "
                                       f"({response.status_code})")
            if response.status_code != 200:
                raise SourceError(f"WB ответил {response.status_code}")
            try:
                data = response.json()
            except ValueError:
                raise SourceError("WB прислал не JSON — возможно, изменился формат ответа")
        return parse_card(data, article)


async def _demo(url: str) -> None:
    source = WBSource(Path("data/wb_session.json"), Path("data/wb_browser_profile"))
    info = await source.fetch(url)
    print(info.name)
    if info.has_sizes:
        print(f"{'Размер':<10}{'Цена':>12}{'Без скидки':>14}{'В наличии':>12}")
        for s in info.sizes:
            stock = f"{s.quantity} шт." if s.quantity else "нет"
            print(f"{s.name:<10}{format_price(s.price):>12}"
                  f"{format_price(s.old_price):>14}{stock:>12}")
        print(f"Минимальная цена: {format_price(info.price)}")
    else:
        print(f"Цена: {format_price(info.price)} (без скидки: {format_price(info.old_price)})\n"
              f"В наличии: {info.quantity} шт.")
    print(info.url)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    asyncio.run(_demo(sys.argv[1] if len(sys.argv) > 1 else "683146715"))
