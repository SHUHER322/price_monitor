"""Тесты текстов и разбора ввода (без бота и сети)."""
import pytest

from app.db import PricePoint, Tracking
from app.db import ItemRow
from app.formatting import (button_text, group_summary_text, ordered, parse_rubles,
                            product_card, short, stock_text, tracking_line, trackings_text)
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
    line = tracking_line(tracking(target_price=250000), last)
    assert line == "• WB · Коврик\n    2 763 ₽ · цель 2 500 ₽"


def test_seller_and_search_lines():
    assert tracking_line(tracking("seller", "K A.RU")) == "• WB · K A.RU"
    assert tracking_line(tracking("search", "коврик")) == "• WB · «коврик»"


def test_list_grouped_by_kind():
    items = [tracking("search", "коврик"), tracking("product", "Куртка"),
             tracking("seller", "Ридберг"), tracking("product", "Микшер")]
    assert [t.title for t in ordered(items)] == ["Куртка", "Микшер", "Ридберг", "коврик"]
    text = trackings_text(items, {})
    assert text.index("Товары") < text.index("Магазины") < text.index("Поиск")
    assert text.index("Куртка") < text.index("Микшер")      # внутри — порядок добавления


def test_long_titles_shortened():
    title = "Зимняя модная теплая куртка-пуховик C P COMPANY x PALACE"
    assert short(title, 20) == "Зимняя модная…"     # слово не режем
    assert len(short(title, 20)) <= 20
    assert button_text(tracking(title=title)).startswith("🛍 Зимняя модная")
    assert button_text(tracking("search", "коврик")) == "🔍 «коврик» · WB"


def test_group_summary():
    t = Tracking(id=2, user_id=111, kind="seller", source="ozon", target="ridberg",
                 title="Ридберг", target_price=None, created_at="x")
    rows = [ItemRow(1, "1", "А", "", "Ридберг", "u", 8900, None, 5, None, 0, "x"),
            ItemRow(2, "2", "Б", "", "Ридберг", "u", 93900, None, 5, None, 0, "x"),
            ItemRow(3, "3", "В", "", "Ридберг", "u", 601400, None, 5, None, 0, "x"),
            ItemRow(4, "4", "Г", "", "Ридберг", "u", None, None, 0, None, 0, "x")]
    text = group_summary_text(t, rows, "21.10.2026 21:28")
    assert "Магазин «Ридберг» · Ozon" in text
    assert "Товаров в магазине: 4, в наличии: 3" in text
    assert "Цены по карте Ozon: от 89 ₽ до 6 014 ₽, медиана 939 ₽" in text
    assert "Данных пока нет" in group_summary_text(t, [], "")


def test_html_escaped():
    # Название с < > не должно сломать разметку сообщения
    line = tracking_line(tracking(title="<b>Коврик</b>"))
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


@pytest.mark.parametrize("quantity, text", [(0, "нет"), (1, "1 шт."), (49, "49 шт.")])
def test_stock_text(quantity, text):
    assert stock_text(quantity) == text


from app.db import ProductSummary
from app.formatting import product_summary_text, sizes_line


def test_sizes_line():
    sizes = [{"name": "S", "price": 1, "quantity": None},    # из списка: количество неизвестно
             {"name": "M", "price": 1, "quantity": 3},
             {"name": "XL", "price": None, "quantity": None}]
    assert sizes_line(sizes) == "S, M — есть; XL — нет"
    assert sizes_line([{"name": "", "price": 1, "quantity": 5}]) is None   # без размеров


def test_product_summary_text():
    summary = ProductSummary(name="Лонгслив", url="https://wb/1", price=113800, old_price=299000,
                             quantity=43, sizes=[{"name": "M", "price": 113800, "quantity": 2}],
                             checked_at="2026-10-20 13:01:00", min_price=113800,
                             max_price=129000, checks=4)
    text = product_summary_text(tracking(target_price=100000), summary, "20.10.2026 20:01")
    assert "Сейчас: <b>1 138 ₽</b> (без скидки 2 990 ₽), в наличии: 43 шт." in text
    assert "Желаемая цена: 1 000 ₽" in text
    assert "мин 1 138 ₽, макс 1 290 ₽" in text
    assert "Размеры: M — есть" in text
    assert "Проверено: 20.10.2026 20:01" in text


def test_product_summary_text_out_of_stock_no_goal():
    summary = ProductSummary("Лонгслив", "https://wb/1", None, None, 0, [], "x", None, None, 1)
    text = product_summary_text(tracking(), summary, "—")
    assert "нет в наличии" in text
    assert "не задана" in text
    assert "мин" not in text                        # одна проверка — разброса ещё нет


def test_ozon_stock_and_card_price():
    assert stock_text(-1, "ozon") == "есть"       # страница товара Ozon: только есть/нет
    assert stock_text(1, "ozon") == "есть"        # так хранились старые записи
    assert stock_text(0, "ozon") == "нет"
    assert stock_text(225, "ozon_card") == "225 шт."   # плитка: «225 ед осталось»
    assert stock_text(-1, "ozon_card") == "есть"
    info = ProductInfo(source="ozon", article="1", url="u", name="Микшер", price=342700,
                       old_price=1099000, quantity=1, seller="Ридберг", card_price=309800)
    card = product_card(info)
    assert "Ozon · продавец: Ридберг" in card
    assert "Цена: <b>3 427 ₽</b>, по карте Ozon 3 098 ₽, в наличии: есть" in card


def test_ozon_summary_line():
    t = Tracking(id=1, user_id=111, kind="product", source="ozon", target="1", title="Микшер",
                 target_price=None, created_at="x")
    summary = ProductSummary("Микшер", "https://ozon/1", 342700, 1099000, 1, [], "x",
                             342700, 342700, 1, card_price=309800)
    text = product_summary_text(t, summary, "—")
    assert text.startswith("🛍 Ozon:")
    assert "по карте Ozon 3 098 ₽" in text
    assert "в наличии: есть" in text
