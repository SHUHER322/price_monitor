"""Тесты источника Ozon — на сохранённом ответе API, без браузера и интернета."""
import copy
import json
from pathlib import Path

import pytest

from app.sources import detect
from app.sources.base import OZON_CARD, UNKNOWN_STOCK, SourceError
from app.sources.ozon import (OzonSource, article_from_url, matches, matches_seller,
                              next_page, parse_product, parse_rubles, parse_tiles,
                              product_path, seller_id_from_url, seller_name)

FIXTURE = Path(__file__).parent / "fixtures" / "ozon_product_1291633069.json"


@pytest.fixture
def page() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def set_price(data: dict, **changes) -> dict:
    """Поменять поля виджета webPrice (он хранится JSON-строкой внутри JSON)."""
    data = copy.deepcopy(data)
    key = next(k for k in data["widgetStates"] if k.startswith("webPrice"))
    state = json.loads(data["widgetStates"][key])
    state.update(changes)
    data["widgetStates"][key] = json.dumps(state, ensure_ascii=False)
    return data


def test_parse_mixer(page):
    info = parse_product(page, "1291633069")
    assert info.source == "ozon"
    assert info.name == "Микшер Fifine mixer SC3 (Белый) Аудио микшер / звуковая карта / пульт"
    assert info.price == 342700            # обычная цена
    assert info.card_price == 309800       # по карте Ozon
    assert info.old_price == 1099000       # до скидки
    assert info.in_stock and info.quantity == UNKNOWN_STOCK   # только «есть»
    assert info.seller == "Ридберг"
    assert info.seller_id == "ridberg"
    assert info.rating == 4.9 and info.feedbacks == 1379
    assert info.url == ("https://www.ozon.ru/product/"
                        "miksher-fifine-mixer-sc3-belyy-audio-miksher-zvukovaya-karta-pult-1291633069/")


def test_out_of_stock(page):
    info = parse_product(set_price(page, isAvailable=False), "1291633069")
    assert info.price is None
    assert not info.in_stock
    assert info.card_price is None


def test_no_price_widget_is_out_of_stock(page):
    data = copy.deepcopy(page)
    data["widgetStates"] = {k: v for k, v in data["widgetStates"].items()
                            if not k.startswith("webPrice")}
    assert not parse_product(data, "1291633069").in_stock


def test_only_card_price(page):
    info = parse_product(set_price(page, price=""), "1291633069")
    assert info.price == 309800            # если обычной цены нет — берём по карте
    assert info.card_price is None


def test_removed_product_raises(page):
    with pytest.raises(SourceError):
        parse_product({"widgetStates": {}}, "1")


@pytest.mark.parametrize("text, rubles", [
    ("3 427 ₽", 3427), ("10 990 ₽", 10990), ("867 ₽", 867)])
def test_parse_rubles(text, rubles):
    assert parse_rubles(text) == rubles * 100


def test_parse_rubles_empty():
    assert parse_rubles("") is None
    assert parse_rubles(None) is None


@pytest.mark.parametrize("url, article", [
    ("https://www.ozon.ru/product/kovrik-dlya-myshki-l-chernyy-belyy-3990933477/"
     "?at=1h2p&from_sku=3990933477", "3990933477"),
    ("ozon.ru/product/3990933477/", "3990933477"),
    ("3990933477", "3990933477"),
])
def test_article_from_url(url, article):
    assert article_from_url(url) == article


def test_product_path_drops_params():
    url = "https://www.ozon.ru/product/kovrik-3990933477/?at=abc&sh=xyz"
    assert product_path(url) == "/product/kovrik-3990933477/"
    assert product_path("3990933477") == "/product/3990933477/"


def test_detect_marketplace():
    assert detect("https://www.ozon.ru/product/kovrik-3990933477/") == "ozon"
    assert detect("https://ozon.ru/t/AbC12") == "ozon"          # короткая из приложения
    assert detect("https://www.wildberries.ru/catalog/683146715/detail.aspx") == "wb"
    assert detect("https://www.ozon.ru/seller/ridberg/") is None
    assert not matches("https://www.ozon.ru/category/elektronika-15500/")



# --- поиск и магазин: плитки каталога ---

def load(name: str) -> dict:
    return json.loads((FIXTURE.parent / name).read_text(encoding="utf-8"))


def test_search_tiles():
    products = parse_tiles(load("ozon_search_page.json"))
    assert len(products) == 4
    first = products[0]
    assert first.source == OZON_CARD           # цена по карте — хранится отдельно
    assert first.article == "5448022388"
    assert first.name == "Стеклянный коврик для мыши Crystal AIR, XL, прозрачный"
    assert first.price == 620900 and first.old_price == 2099000
    assert first.quantity == 1                 # «1 ед осталось»
    assert first.rating == 4.8 and first.feedbacks == 251
    assert first.url == ("https://www.ozon.ru/product/"
                         "steklyannyy-kovrik-dlya-myshi-crystal-air-xl-prozrachnyy-5448022388/")
    assert products[2].quantity == 225
    assert products[1].feedbacks == 17383      # «17 383 отзыва» — с узким пробелом


def test_seller_page():
    data = load("ozon_seller_page.json")
    assert seller_name(data) == "Ридберг"
    products = parse_tiles(data, "Ридберг", "ridberg")
    assert [p.article for p in products] == ["1291636512", "2903913180", "1291633069"]
    assert all(p.seller == "Ридберг" for p in products)
    assert next_page(data) is None
    assert next_page(load("ozon_search_page.json")).startswith("/search/")


def test_tile_without_price_skipped():
    data = load("ozon_search_page.json")
    key = next(k for k in data["widgetStates"] if k.startswith("tileGrid"))
    grid = json.loads(data["widgetStates"][key])
    grid["items"][0]["mainState"] = [s for s in grid["items"][0]["mainState"]
                                     if s["type"] != "priceV2"]
    data["widgetStates"][key] = json.dumps(grid, ensure_ascii=False)
    assert len(parse_tiles(data)) == 3


@pytest.mark.parametrize("text", ["https://www.ozon.ru/seller/ridberg/",
                                  "ozon.ru/seller/ridberg/?sorting=new", "ridberg"])
def test_seller_id(text):
    assert seller_id_from_url(text) == "ridberg"


def test_seller_link_detection():
    assert matches_seller("https://www.ozon.ru/seller/ridberg/")
    assert not matches_seller("https://www.ozon.ru/product/kovrik-3990933477/")


def fake_page(start: int, count: int, next_path: str | None) -> dict:
    """Страница каталога с товарами start…start+count-1 (подделка для листания)."""
    template = json.loads(next(v for k, v in load("ozon_search_page.json")["widgetStates"].items()
                               if k.startswith("tileGrid")))["items"][0]
    items = []
    for i in range(start, start + count):
        tile = copy.deepcopy(template)
        tile["sku"] = tile["id"] = str(i)
        tile["action"]["link"] = f"/product/item-{i}/"
        items.append(tile)
    pager = {"nextPage": next_path} if next_path else {}
    return {"widgetStates": {"tileGridDesktop-1-default-1": json.dumps({"items": items}),
                             "infiniteVirtualPaginator-1-default-1": json.dumps(pager)}}


def run_catalog(responses: dict, start: str, limit: int) -> tuple[list, list]:
    """Пролистать каталог на поддельных ответах; вернуть товары и запрошенные адреса."""
    import asyncio
    calls = []
    source = OzonSource(Path("unused"), pause=(0, 0))

    async def fake_api(page, path):
        calls.append(path)
        return responses[path]

    source._api = fake_api
    products = asyncio.run(source._catalog(start, limit))
    return products, calls


def test_search_pagination():
    # Как у настоящего поиска: страница = 2 порции по 8, следующая — &page=N
    responses = {
        "/search/?text=x": fake_page(1, 8, "/p1b"), "/p1b": fake_page(9, 8, None),
        "/search/?text=x&page=2": fake_page(17, 8, "/p2b"), "/p2b": fake_page(25, 8, None),
    }
    products, calls = run_catalog(responses, "/search/?text=x", limit=30)
    assert len(products) == 30
    assert [p.article for p in products[:3]] == ["1", "2", "3"]   # порядок выдачи сохранён
    assert calls == ["/search/?text=x", "/p1b", "/search/?text=x&page=2", "/p2b"]


def test_seller_pagination_overlap():
    # Как у настоящего магазина: каждая страница наполовину повторяет предыдущую
    responses = {
        "/seller/r/": fake_page(1, 8, "/a"), "/a": fake_page(9, 8, None),
        "/seller/r/?page=2": fake_page(9, 8, "/b"), "/b": fake_page(17, 8, None),
        "/seller/r/?page=3": fake_page(17, 8, "/c"), "/c": fake_page(17, 8, None),  # конец
    }
    products, calls = run_catalog(responses, "/seller/r/", limit=100)
    assert len(products) == 24                 # повторы отброшены
    assert len({p.article for p in products}) == 24
    assert calls[-1] == "/c"                   # страница без новых товаров — стоп
