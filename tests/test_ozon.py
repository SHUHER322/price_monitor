"""Тесты источника Ozon — на сохранённом ответе API, без браузера и интернета."""
import copy
import json
from pathlib import Path

import pytest

from app.sources import detect
from app.sources.base import SourceError
from app.sources.ozon import (article_from_url, matches, parse_product, parse_rubles,
                              product_path)

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
    assert info.in_stock and info.quantity == 1
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
