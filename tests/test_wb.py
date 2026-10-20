"""Тесты разбора ответа WB — на сохранённом ответе, без интернета."""
import copy
import json
from pathlib import Path

import pytest

from app.sources.base import SourceError, format_price
from app.sources.wb import article_from_url, matches, parse_card

FIXTURE = Path(__file__).parent / "fixtures" / "wb_card_683146715.json"


@pytest.fixture
def card() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_parse_mousepad(card):
    info = parse_card(card, "683146715")
    assert info.name == "PADSMITH стеклянный коврик для мыши 450×400MM"
    assert info.price == 276300
    assert info.old_price == 650300
    assert info.quantity == 11
    assert info.in_stock
    assert info.source == "wb"


def test_out_of_stock_has_no_price(card):
    # Так WB отдаёт закончившийся товар: размеры без цены, остаток 0
    data = copy.deepcopy(card)
    product = data["products"][0]
    product["totalQuantity"] = 0
    for size in product["sizes"]:
        size.pop("price")
    info = parse_card(data, "683146715")
    assert info.price is None
    assert not info.in_stock


def test_no_real_sizes_for_mousepad(card):
    # У коврика один безымянный размер — таблицу размеров показывать не нужно
    info = parse_card(card, "683146715")
    assert len(info.sizes) == 1
    assert info.sizes[0].name == ""
    assert info.sizes[0].quantity == 11
    assert not info.has_sizes


@pytest.fixture
def clothes(card) -> dict:
    """Тот же ответ, но как у одежды: три размера, M закончился."""
    data = copy.deepcopy(card)
    base = data["products"][0]["sizes"][0]
    data["products"][0]["sizes"] = [
        {**base, "name": "S", "origName": "42",
         "price": {"basic": 500000, "product": 250000},
         "stocks": [{"qty": 2}, {"qty": 1}]},
        {**base, "name": "M", "origName": "44", "stocks": []},
        {**base, "name": "L", "origName": "46",
         "price": {"basic": 500000, "product": 199900},
         "stocks": [{"qty": 5}]},
    ]
    del data["products"][0]["sizes"][1]["price"]
    data["products"][0]["totalQuantity"] = 8
    return data


def test_sizes_listed(clothes):
    info = parse_card(clothes, "683146715")
    assert info.has_sizes
    assert [s.name for s in info.sizes] == ["S", "M", "L"]
    s, m, l = info.sizes
    assert (s.price, s.quantity) == (250000, 3)   # остатки складываются по складам
    assert (m.price, m.quantity) == (None, 0)     # размер закончился
    assert (l.price, l.old_price, l.quantity) == (199900, 500000, 5)


def test_cheapest_size_is_product_price(clothes):
    info = parse_card(clothes, "683146715")
    assert info.price == 199900
    assert info.old_price == 500000
    assert info.quantity == 8


def test_unknown_article_raises(card):
    with pytest.raises(SourceError):
        parse_card(card, "1")


@pytest.mark.parametrize("text", [
    "https://www.wildberries.ru/catalog/683146715/detail.aspx",
    "https://www.wildberries.ru/catalog/683146715/detail.aspx?targetUrl=MI",
    "wildberries.ru/catalog/683146715/detail.aspx",
    "683146715",
    "  683146715 ",
])
def test_article_from_url(text):
    assert article_from_url(text) == "683146715"


@pytest.mark.parametrize("text", ["https://www.ozon.ru/product/123456/", "коврик", ""])
def test_not_wb_link(text):
    assert not matches(text)
    with pytest.raises(SourceError):
        article_from_url(text)


@pytest.mark.parametrize("kopecks, expected", [
    (276300, "2 763 ₽"),
    (99, "0 ₽"),
    (123456700, "1 234 567 ₽"),
    (None, "—"),
])
def test_format_price(kopecks, expected):
    assert format_price(kopecks) == expected
