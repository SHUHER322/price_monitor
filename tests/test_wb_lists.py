"""Тесты поиска, магазина и списков WB — без интернета.

Ответы WB подменяем через httpx.MockTransport: это «поддельная сеть», которая
вместо настоящего сайта вызывает нашу функцию и отдаёт то, что мы скажем.
Так проверяем перелистывание страниц и обновление токена, не трогая WB.
"""
import asyncio
import copy
import json
from pathlib import Path

import httpx
import pytest

from app.sources.base import BlockedError, SourceError
from app.sources.wb import WBSource, parse_products, seller_id_from_url

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def make_source(tmp_path, handler) -> WBSource:
    """Источник с подменённой сетью, готовой сессией и без пауз."""
    session_file = tmp_path / "session.json"
    session_file.write_text(json.dumps({"cookies": {"x_wbaas_token": "old"},
                                        "headers": {"x-spa-version": "1"}}))
    return WBSource(session_file, tmp_path / "profile",
                    transport=httpx.MockTransport(handler), pause=(0, 0))


def fake_page(count: int, start_id: int, total: int) -> dict:
    """Страница выдачи из count товаров с артикулами start_id, start_id+1, ..."""
    template = load("wb_search.json")["products"][0]
    products = []
    for i in range(count):
        p = copy.deepcopy(template)
        p["id"] = start_id + i
        products.append(p)
    return {"products": products, "total": total}


# --- разбор списков ---

def test_search_response_parsed():
    products = parse_products(load("wb_search.json"))
    assert len(products) == 3
    first = products[0]
    assert first.article == "334082543"
    assert first.price == 361100
    assert first.brand == "ARDOR GAMING"
    assert first.seller == "GBS Техника"
    assert first.seller_id == "4099240"


def test_seller_response_with_sizes():
    products = parse_products(load("wb_seller_250079242.json"))
    costume = products[1]
    assert costume.has_sizes
    assert [s.name for s in costume.sizes] == ["42-44", "44-46", "46-48"]
    # В списках остатков по размерам нет — честно «неизвестно», а не 0
    assert all(s.quantity is None for s in costume.sizes)
    assert all(s.in_stock for s in costume.sizes)  # но цена есть — значит, в продаже
    assert costume.quantity == 38                  # общий остаток известен
    assert costume.rating == 4.9
    assert costume.feedbacks == 40


def test_size_without_price_is_out_of_stock():
    data = load("wb_seller_250079242.json")
    del data["products"][1]["sizes"][0]["price"]
    costume = parse_products(data)[1]
    assert not costume.sizes[0].in_stock
    assert costume.sizes[1].in_stock


@pytest.mark.parametrize("text", [
    "https://www.wildberries.ru/seller/250079242",
    "wildberries.ru/seller/250079242?sort=popular",
    "250079242",
])
def test_seller_id_from_url(text):
    assert seller_id_from_url(text) == "250079242"


def test_not_seller_link():
    with pytest.raises(SourceError):
        seller_id_from_url("https://www.wildberries.ru/catalog/683146715/detail.aspx")


# --- работа с «сетью» ---

def test_search_paginates_until_limit(tmp_path):
    pages = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        pages.append(page)
        assert request.url.params["query"] == "коврик"
        assert request.headers["x-spa-version"] == "1"   # заголовки из сессии
        return httpx.Response(200, json=fake_page(100, page * 1000, total=250))

    source = make_source(tmp_path, handler)
    products = asyncio.run(source.search("коврик", limit=150))
    assert len(products) == 150
    assert pages == [1, 2]   # третья страница не нужна


def test_seller_stops_on_last_page(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        assert request.url.params["supplier"] == "250079242"
        count = 100 if page == 1 else 40
        return httpx.Response(200, json=fake_page(count, page * 1000, total=140))

    source = make_source(tmp_path, handler)
    products = asyncio.run(source.seller("https://www.wildberries.ru/seller/250079242"))
    assert len(products) == 140


def test_fetch_many_in_batches(tmp_path):
    batches = []

    def handler(request: httpx.Request) -> httpx.Response:
        articles = request.url.params["nm"].split(";")
        batches.append(len(articles))
        return httpx.Response(200, json=fake_page(len(articles), int(articles[0]), 0))

    source = make_source(tmp_path, handler)
    articles = [str(1000 + i) for i in range(250)]
    products = asyncio.run(source.fetch_many(articles))
    assert batches == [100, 100, 50]
    assert len(products) == 250


def test_token_refreshed_once_on_498(tmp_path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        token = request.headers["cookie"]
        calls.append(token)
        if "old" in token:
            return httpx.Response(498)
        return httpx.Response(200, json=load("wb_search.json"))

    source = make_source(tmp_path, handler)
    refreshed = []

    async def fake_browser(page_url):
        refreshed.append(page_url)
        return {"cookies": {"x_wbaas_token": "new"}, "headers": {"x-spa-version": "1"}}

    source._session_from_browser = fake_browser
    products = asyncio.run(source.search("коврик", limit=3))
    assert len(products) == 3
    assert len(refreshed) == 1                      # браузер открыт один раз
    assert "search.aspx" in refreshed[0]            # на странице поиска
    assert len(calls) == 2                          # отказ → повтор с новым токеном
    saved = json.loads((tmp_path / "session.json").read_text())
    assert saved["cookies"]["x_wbaas_token"] == "new"  # новый токен сохранён в файл


def test_network_glitch_retried_once(tmp_path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            # Так выглядел реальный сбой: WB прислал мусор вместо ответа
            raise httpx.RemoteProtocolError("illegal status line")
        return httpx.Response(200, json=load("wb_search.json"))

    source = make_source(tmp_path, handler)
    assert len(asyncio.run(source.search("коврик", limit=3))) == 3
    assert len(calls) == 2


def test_network_down_gives_clear_error(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("нет сети")

    source = make_source(tmp_path, handler)
    with pytest.raises(SourceError, match="ошибка сети"):
        asyncio.run(source.search("коврик"))


def test_blocked_even_with_new_token(tmp_path):
    source = make_source(tmp_path, lambda request: httpx.Response(403))

    async def fake_browser(page_url):
        return {"cookies": {"x_wbaas_token": "new"}, "headers": {}}

    source._session_from_browser = fake_browser
    with pytest.raises(BlockedError):
        asyncio.run(source.search("коврик"))
