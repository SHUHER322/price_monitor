"""График истории цены товара — картинка PNG для Telegram.

Рисуем «ступеньками»: цена держится до следующей проверки, а не плавно
перетекает между точками — так честнее показывает, когда она изменилась.
Периоды «нет в наличии» — разрыв линии.
"""
import io
from datetime import datetime
from zoneinfo import ZoneInfo

import matplotlib

matplotlib.use("Agg")  # рисовать в файл, без окна на экране (важно для сервера)
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

from .db import PricePoint  # noqa: E402
from .sources.base import format_price  # noqa: E402

# Цвета как у аватарки бота
BACKGROUND = "#1b1f2e"
GRID = "#2c3245"
TEXT = "#c9cfdb"
LINE = "#3ee6a8"
TARGET = "#ffb454"

MIN_POINTS = 2  # меньше — рисовать нечего
MIN_SPAN = 0.05  # минимальный размах шкалы: ±5% от цены — ровная цена выглядит ровной


def checks_word(count: int) -> str:
    """1 проверка, 3 проверки, 5 проверок, 21 проверка, 12 проверок."""
    if count % 10 == 1 and count % 100 != 11:
        return f"{count} проверка"
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return f"{count} проверки"
    return f"{count} проверок"


def _local(utc_text: str, tz: ZoneInfo) -> datetime:
    moment = datetime.strptime(utc_text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo("UTC"))
    return moment.astimezone(tz).replace(tzinfo=None)


def can_draw(points: list[PricePoint]) -> bool:
    return sum(1 for p in points if p.price is not None) >= MIN_POINTS


def build_price_chart(title: str, points: list[PricePoint], tz: ZoneInfo,
                      target_price: int | None = None) -> bytes:
    """Собрать PNG с графиком цены и вернуть его байты."""
    times = [_local(p.checked_at, tz) for p in points]
    # None → nan: matplotlib оставит в этом месте разрыв линии
    prices = [p.price / 100 if p.price is not None else float("nan") for p in points]

    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=120)
    fig.patch.set_facecolor(BACKGROUND)
    ax.set_facecolor(BACKGROUND)

    ax.plot(times, prices, drawstyle="steps-post", color=LINE, linewidth=2.5)
    ax.scatter(times, prices, color=LINE, s=14, zorder=3)

    if target_price:
        ax.axhline(target_price / 100, color=TARGET, linestyle="--", linewidth=1.5)
        ax.text(times[0], target_price / 100, f"  цель {format_price(target_price)}",
                color=TARGET, va="bottom", fontsize=9)

    # Подписи: последняя цена и минимум
    priced = [(t, p) for t, p in zip(times, points) if p.price is not None]
    last_time, last = priced[-1]
    ax.annotate(format_price(last.price), (last_time, last.price / 100),
                textcoords="offset points", xytext=(6, 6), color="white",
                fontsize=10, fontweight="bold")
    low_time, low = min(priced, key=lambda tp: tp[1].price)
    if low.price < last.price:   # минимум подписываем, только если он ниже текущей цены
        ax.annotate(f"мин {format_price(low.price)}", (low_time, low.price / 100),
                    textcoords="offset points", xytext=(0, -16), color=TEXT,
                    fontsize=9, ha="center")

    ax.set_title(title[:70], color="white", fontsize=12, loc="left", pad=12)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:,.0f} ₽".replace(",", " ")))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m\n%H:%M"))
    ax.tick_params(colors=TEXT, labelsize=9)
    ax.grid(color=GRID, linewidth=0.8)
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.margins(x=0.03, y=0.15)
    # Если цена почти не менялась, не растягиваем шкалу «под микроскоп»
    values = [p.price / 100 for _, p in priced]
    low_value, high_value = min(values), max(values)
    middle = (low_value + high_value) / 2
    if high_value - low_value < middle * MIN_SPAN * 2:
        bottom, top = middle * (1 - MIN_SPAN * 2), middle * (1 + MIN_SPAN * 2)
        if target_price:   # линия цели должна остаться на графике
            bottom = min(bottom, target_price / 100 * 0.97)
        ax.set_ylim(bottom, top)

    fig.tight_layout()
    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", facecolor=BACKGROUND)
    plt.close(fig)  # освободить память: бот работает долго
    return buffer.getvalue()
