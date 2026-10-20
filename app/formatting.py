"""Тексты для пользователя и разбор ввода — чистые функции, без бота и сети.
Вынесены отдельно, чтобы их было легко тестировать."""
import html
import re

from .db import PricePoint, Tracking
from .sources.base import ProductInfo, format_price

KIND_ICONS = {"product": "🛍", "seller": "🏪", "search": "🔍"}
MAX_RUBLES = 10_000_000  # больше — скорее опечатка

_NOT_DIGITS = re.compile(r"[^\d.,]")


def parse_rubles(text: str) -> int | None:
    """'2500', '2 500', '2500 ₽', '2499,90' → копейки. Неверный ввод → None."""
    if "-" in text:   # отрицательная цена — точно ошибка
        return None
    cleaned = _NOT_DIGITS.sub("", text).replace(",", ".")
    if not cleaned or cleaned.count(".") > 1:
        return None
    try:
        rubles = float(cleaned)
    except ValueError:
        return None
    if not 0 < rubles <= MAX_RUBLES:
        return None
    return round(rubles * 100)


def esc(text: str) -> str:
    """Экранировать текст для HTML-разметки Telegram (как в lead_bot):
    название товара с символом < иначе сломает сообщение."""
    return html.escape(text, quote=False)


def product_card(info: ProductInfo) -> str:
    """Карточка товара: название, цена, наличие, размеры."""
    lines = [f"<b>{esc(info.name)}</b>",
             f"Продавец: {esc(info.seller)}"]
    if info.has_sizes:
        for s in info.sizes:
            if s.quantity is not None:
                stock = f"{s.quantity} шт." if s.quantity else "нет"
            else:
                stock = "есть" if s.in_stock else "нет"
            lines.append(f"• {esc(s.name)}: {format_price(s.price)} ({stock})")
    else:
        stock = f"{info.quantity} шт." if info.in_stock else "нет в наличии"
        lines.append(f"Цена: <b>{format_price(info.price)}</b>, в наличии: {stock}")
    return "\n".join(lines)


def products_preview(products: list[ProductInfo], count: int = 5) -> str:
    """Первые товары списка — показать, что нашлось."""
    lines = [f"{i}. {format_price(p.price)} — {esc(p.name[:50])}"
             for i, p in enumerate(products[:count], 1)]
    if len(products) > count:
        lines.append(f"… и ещё {len(products) - count}")
    return "\n".join(lines)


def tracking_line(number: int, tracking: Tracking, last: PricePoint | None = None) -> str:
    """Строка в списке «Мои отслеживания»."""
    icon = KIND_ICONS.get(tracking.kind, "•")
    title = esc(tracking.title[:60])
    if tracking.kind == "product":
        parts = [f"{number}. {icon} {title}"]
        if last is not None:
            parts.append(f"сейчас {format_price(last.price)}")
        if tracking.target_price:
            parts.append(f"цель {format_price(tracking.target_price)}")
        return " — ".join(parts)
    if tracking.kind == "seller":
        return f"{number}. {icon} Магазин «{title}»"
    return f"{number}. {icon} Поиск «{title}»"


def trackings_text(trackings: list[Tracking], last_prices: dict[str, PricePoint]) -> str:
    if not trackings:
        return ("Пока ничего не отслеживается.\n"
                "Выберите в меню, за чем следить: товар, магазин или поиск.")
    lines = ["<b>Мои отслеживания:</b>"]
    for i, t in enumerate(trackings, 1):
        last = last_prices.get(t.target) if t.kind == "product" else None
        lines.append(tracking_line(i, t, last))
    lines.append("\nЧтобы убрать, нажмите кнопку с номером.")
    return "\n".join(lines)
