"""Тесты графика: картинка собирается, мало данных — не рисуем."""
from zoneinfo import ZoneInfo

from app.chart import build_price_chart, can_draw
from app.db import PricePoint

TZ = ZoneInfo("Asia/Tomsk")
PNG_SIGNATURE = b"\x89PNG"   # так начинается любой PNG-файл


def point(time: str, price: int | None) -> PricePoint:
    return PricePoint(f"2026-10-20 {time}:00", price, 650300, 5 if price else 0)


def test_chart_is_png():
    points = [point("03:00", 300000), point("09:00", None), point("15:00", 276300)]
    png = build_price_chart("Коврик", points, TZ, target_price=250000)
    assert png.startswith(PNG_SIGNATURE)
    assert len(png) > 10_000      # не пустая картинка


def test_can_draw_needs_two_prices():
    assert not can_draw([])
    assert not can_draw([point("03:00", 300000)])
    assert not can_draw([point("03:00", 300000), point("09:00", None)])   # нет в наличии — не точка
    assert can_draw([point("03:00", 300000), point("09:00", 290000)])
