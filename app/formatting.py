"""Тексты для пользователя и разбор ввода — чистые функции, без бота и сети.
Вынесены отдельно, чтобы их было легко тестировать."""
import html
import re
import statistics

from .db import ItemRow, PricePoint, ProductSummary, Tracking
from .sources.base import EXACT_STOCK, OZON_CARD, SOURCE_NAMES, ProductInfo, format_price

KIND_ICONS = {"product": "🛍", "seller": "🏪", "search": "🔍"}
# Разделы списка «Мои отслеживания» — в этом порядке
SECTIONS = [("product", "Товары"), ("seller", "Магазины"), ("search", "Поиск")]
BUTTON_TITLE = 30   # длиннее Telegram всё равно обрежет кнопку
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


def stock_text(quantity: int, source: str = "wb") -> str:
    """11 → '11 шт.', 0 → 'нет'. Если количество неизвестно (страница товара Ozon) — 'есть'."""
    if not quantity:
        return "нет"
    return f"{quantity} шт." if source in EXACT_STOCK and quantity > 0 else "есть"


def card_price_text(card_price: int | None, source: str) -> str:
    """', по карте Ozon 3 098 ₽' — если цена по карте есть и отличается."""
    if not card_price:
        return ""
    return f", по карте {SOURCE_NAMES.get(source, source)} {format_price(card_price)}"


def esc(text: str) -> str:
    """Экранировать текст для HTML-разметки Telegram (как в lead_bot):
    название товара с символом < иначе сломает сообщение."""
    return html.escape(text, quote=False)


def product_card(info: ProductInfo) -> str:
    """Карточка товара: название, цена, наличие, размеры."""
    lines = [f"<b>{esc(info.name)}</b>",
             f"{SOURCE_NAMES.get(info.source, info.source)} · продавец: {esc(info.seller)}"]
    if info.has_sizes:
        for s in info.sizes:
            if s.quantity is not None:
                stock = stock_text(s.quantity)
            else:
                stock = "есть" if s.in_stock else "нет"
            lines.append(f"• {esc(s.name)}: {format_price(s.price)} ({stock})")
    else:
        stock = stock_text(info.quantity, info.source) if info.in_stock else "нет в наличии"
        lines.append(f"Цена: <b>{format_price(info.price)}</b>"
                     f"{card_price_text(info.card_price, info.source)}, в наличии: {stock}")
    return "\n".join(lines)


def products_preview(products: list[ProductInfo], count: int = 5) -> str:
    """Первые товары списка — показать, что нашлось."""
    lines = []
    if products and products[0].source == OZON_CARD:
        lines.append("Цены — по карте Ozon, как их показывает каталог.")
    lines += [f"{i}. {format_price(p.price)} — {esc(p.name[:50])}"
              for i, p in enumerate(products[:count], 1)]
    if len(products) > count:
        lines.append(f"… и ещё {len(products) - count}")
    return "\n".join(lines)


def short(text: str, limit: int) -> str:
    """Обрезать длинное название: 'Зимняя модная тёплая куртка-пуховик' → 'Зимняя модная…'."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]
    if text[limit - 1] != " " and " " in cut[limit // 2:]:   # не рвём слово посередине
        cut = cut[:cut.rindex(" ")]
    return cut.rstrip(" ,.-(/") + "…"


def ordered(trackings: list[Tracking]) -> list[Tracking]:
    """Сначала товары, потом магазины, потом поиск; внутри — в порядке добавления.
    В этом же порядке идут и текст списка, и кнопки под ним."""
    rank = {kind: i for i, (kind, _) in enumerate(SECTIONS)}
    return sorted(trackings, key=lambda t: rank.get(t.kind, len(rank)))


def button_text(tracking: Tracking) -> str:
    """Кнопка в списке: '🛍 Микшер Fifine mixer SC3… · Ozon'."""
    market = SOURCE_NAMES.get(tracking.source, tracking.source)
    title = short(tracking.title, BUTTON_TITLE)
    if tracking.kind == "search":
        title = f"«{title}»"
    return f"{KIND_ICONS.get(tracking.kind, '•')} {title} · {market}"


def tracking_line(tracking: Tracking, last: PricePoint | None = None) -> str:
    """Строка в списке «Мои отслеживания». У товара под названием — цена и цель."""
    market = SOURCE_NAMES.get(tracking.source, tracking.source)
    title = esc(short(tracking.title, 45))
    if tracking.kind == "search":
        title = f"«{title}»"
    line = f"• {market} · {title}"
    if tracking.kind != "product":
        return line
    details = []
    if last is not None:
        details.append(format_price(last.price) if last.price is not None else "нет в наличии")
    if tracking.target_price:
        details.append(f"цель {format_price(tracking.target_price)}")
    return line + (f"\n    {' · '.join(details)}" if details else "")


def trackings_text(trackings: list[Tracking],
                   last_prices: dict[tuple[str, str], PricePoint]) -> str:
    """last_prices — по ключу (маркетплейс, артикул). Отслеживания — по разделам."""
    if not trackings:
        return ("Пока ничего не отслеживается.\n"
                "Выберите в меню, за чем следить: товар, магазин или поиск.")
    lines = ["<b>Мои отслеживания</b>"]
    for kind, name in SECTIONS:
        group = [t for t in trackings if t.kind == kind]
        if not group:
            continue
        lines += ["", f"{KIND_ICONS[kind]} <b>{name}</b>"]
        for t in group:
            last = last_prices.get((t.source, t.target)) if kind == "product" else None
            lines.append(tracking_line(t, last))
    lines += ["", "Нажмите на кнопку ниже, чтобы открыть подробности."]
    return "\n".join(lines)


def group_summary_text(tracking: Tracking, rows: list[ItemRow], checked_local: str) -> str:
    """Карточка магазина/поиска (по данным из нашей базы): сколько товаров и цены."""
    market = SOURCE_NAMES.get(tracking.source, tracking.source)
    head = "🏪 Магазин" if tracking.kind == "seller" else "🔍 Поиск"
    lines = [f"{head} «{esc(tracking.title)}» · {market}"]
    if not rows:
        lines.append("Данных пока нет — они появятся после первой проверки.")
        return "\n".join(lines)
    prices = [r.price for r in rows if r.price is not None]
    where = "в выдаче" if tracking.kind == "search" else "в магазине"
    lines.append(f"Товаров {where}: {len(rows)}, в наличии: {len(prices)}")
    if prices:
        label = "Цены по карте Ozon" if tracking.source == "ozon" else "Цены"
        lines.append(f"{label}: от {format_price(min(prices))} до {format_price(max(prices))}, "
                     f"медиана {format_price(round(statistics.median(prices)))}")
    if checked_local:
        lines.append(f"Проверено: {checked_local}")
    lines += ["", "📊 В Excel — все товары с ценами, скидками, рейтингом и ссылками."]
    return "\n".join(lines)


def sizes_line(sizes: list[dict]) -> str | None:
    """[S есть, M есть, XL нет] → 'S, M — есть; XL — нет'. Без размеров — None."""
    named = [s for s in sizes if s.get("name")]
    if not named:
        return None
    def available(s: dict) -> bool:
        return s.get("price") is not None and s.get("quantity") != 0
    have = [esc(s["name"]) for s in named if available(s)]
    missing = [esc(s["name"]) for s in named if not available(s)]
    parts = []
    if have:
        parts.append(f"{', '.join(have)} — есть")
    if missing:
        parts.append(f"{', '.join(missing)} — нет")
    return "; ".join(parts)


def product_summary_text(tracking: Tracking, summary: ProductSummary, checked_local: str) -> str:
    """Карточка отслеживаемого товара (по данным из нашей базы)."""
    market = SOURCE_NAMES.get(tracking.source, tracking.source)
    lines = [f'🛍 {market}: <a href="{summary.url}">{esc(summary.name)}</a>']
    if summary.price is not None:
        old = f" (без скидки {format_price(summary.old_price)})" if summary.old_price else ""
        card = card_price_text(summary.card_price, tracking.source)
        lines.append(f"Сейчас: <b>{format_price(summary.price)}</b>{card}{old}, "
                     f"в наличии: {stock_text(summary.quantity, tracking.source)}")
    else:
        lines.append("Сейчас: <b>нет в наличии</b>")
    lines.append(f"Желаемая цена: {format_price(tracking.target_price)}"
                 if tracking.target_price else
                 "Желаемая цена: не задана (сообщу о любом заметном снижении)")
    if summary.checks > 1 and summary.min_price is not None:
        lines.append(f"За время отслеживания: мин {format_price(summary.min_price)}, "
                     f"макс {format_price(summary.max_price)}")
    sizes = sizes_line(summary.sizes)
    if sizes:
        lines.append(f"Размеры: {sizes}")
    lines.append(f"Проверено: {checked_local}")
    return "\n".join(lines)
