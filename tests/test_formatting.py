"""Тесты текстов и разбора ввода (без бота и сети)."""
import pytest

from app.db import PricePoint, Tracking
from app.formatting import (parse_rubles, product_card, stock_text, tracking_line,
                            trackings_text)
from app.sources.base import ProductInfo, SizeInfo


@pytest.mark.parametrize("text, kopecks", [
    ("2500", 250000),
    ("2 500", 250000),
    ("2500 ₽", 250000),
    ("2500р", 250000),
    ("2499,90", 249990),
    ("2499.9", 249990),
])
def test_parse_rubles(text, kopecks):
    assert parse_rubles(text) == kopecks


@pytest.mark.parametrize("text", ["", "дёшево", "0", "-5", "1.2.3", "99999999999"])
def test_parse_rubles_bad(text):
    assert parse_rubles(text) is None


def tracking(kind="product", title="Коврик", target_price=None) -> Tracking:
    return Tracking(id=7, user_id=111, kind=kind, source="wb", target="683146715",
                    title=title, target_price=target_price, created_at="2026-10-20 10:00:00")


def test_product_line_with_price_and_goal():
    last = PricePoint("2026-10-20 10:00:00", 276300, 650300, 11)
    line = tracking_line(1, tracking(target_price=250000), last)
    assert line == "1. 🛍 Коврик — сейчас 2 763 ₽ — цель 2 500 ₽"


def test_seller_and_search_lines():
    assert tracking_line(2, tracking("seller", "K A.RU")) == "2. 🏪 Магазин «K A.RU»"
    assert tracking_line(3, tracking("search", "коврик")) == "3. 🔍 Поиск «коврик»"


def test_html_escaped():
    # Название с < > не должно сломать разметку сообщения
    line = tracking_line(1, tracking(title="<b>Коврик</b>"))
    assert "<b>" not in line
    assert "&lt;b&gt;" in line


def test_empty_list_hint():
    assert "Пока ничего" in trackings_text([], {})


def test_card_with_sizes():
    info = ProductInfo(source="wb", article="1", url="u", name="Костюм", price=105700,
                       old_price=None, quantity=38, seller="K A.RU",
                       sizes=(SizeInfo("S", 105700, None, 3), SizeInfo("M", None, None, None)))
    card = product_card(info)
    assert "• S: 1 057 ₽ (3 шт.)" in card
    assert "• M: — (нет)" in card


@pytest.mark.parametrize("quantity, text", [(0, "нет"), (1, "1 шт."), (42, "42 шт."),
                                            (43, "43+ шт.")])
def test_stock_text(quantity, text):
    assert stock_text(quantity) == text
