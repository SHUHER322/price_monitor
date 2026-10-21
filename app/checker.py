"""Проверка цен по расписанию и уведомления.

Один проход проверки:
1. Берём все отслеживания из базы.
2. Собираем, что нужно спросить у WB. Одинаковое не спрашиваем дважды: если два
   пользователя следят за одним магазином — это один запрос.
3. Сравниваем новые цены с последними сохранёнными и находим изменения.
4. Сохраняем новые цены в историю и рассылаем уведомления.

Что считается изменением (функция find_changes — без сети и базы, легко тестировать):
    📉 цена снизилась больше чем на PRICE_DROP_PERCENT %
    🎯 цена опустилась до желаемой (только у товара, только в момент пересечения)
    ❌ товар закончился / ✅ снова в наличии
    🆕 новый товар в магазине или в топе поиска
"""
import asyncio
import logging
import random
from dataclasses import dataclass
from typing import Awaitable, Callable

from .db import Database, PricePoint, Tracking
from .formatting import esc
from .sources.base import BlockedError, ProductInfo, SourceError, format_price
from .sources.wb import WBSource

log = logging.getLogger(__name__)

MAX_LINES = 10                 # столько изменений в одном сообщении, остальное — «и ещё N»
PAUSE_BETWEEN = (2.0, 5.0)     # пауза между запросами к WB, секунды

Notify = Callable[[int, str], Awaitable[None]]   # (user_id, текст) → отправить


@dataclass(frozen=True)
class Change:
    kind: str                  # "drop", "target", "out", "back", "new"
    product: ProductInfo
    old_price: int | None = None
    percent: int | None = None


def find_changes(old: PricePoint | None, new: ProductInfo, drop_percent: float,
                 target_price: int | None = None) -> list[Change]:
    """Что изменилось у товара с прошлой проверки."""
    if old is None:            # товара раньше не видели — сравнивать не с чем
        return []
    changes = []
    was_in_stock = old.price is not None and old.quantity > 0
    if was_in_stock and not new.in_stock:
        return [Change("out", new, old.price)]
    if not was_in_stock and new.in_stock and new.price is not None:
        changes.append(Change("back", new, old.price))
    if old.price and new.price and new.price < old.price:
        percent = round((1 - new.price / old.price) * 100)
        if percent >= drop_percent:
            changes.append(Change("drop", new, old.price, percent))
    # Желаемая цена: сообщаем, когда цена её пересекла, а не на каждой проверке
    if (target_price and new.price is not None and new.price <= target_price
            and (old.price is None or old.price > target_price)):
        changes.append(Change("target", new, old.price))
    return changes


def _link(product: ProductInfo, limit: int = 60) -> str:
    return f'<a href="{product.url}">{esc(product.name[:limit])}</a>'


def change_line(change: Change) -> str:
    """Одна строка в сводке по магазину/поиску."""
    p = change.product
    if change.kind == "drop":
        return (f"📉 {_link(p, 45)}: {format_price(change.old_price)} → "
                f"{format_price(p.price)} (−{change.percent}%)")
    if change.kind == "out":
        return f"❌ {_link(p, 45)}: закончился"
    if change.kind == "back":
        return f"✅ {_link(p, 45)}: снова в наличии, {format_price(p.price)}"
    if change.kind == "new":
        return f"🆕 {_link(p, 45)}: {format_price(p.price)}"
    return f"🎯 {_link(p, 45)}: {format_price(p.price)}"


def change_notes(tracking: Tracking, changes: list[Change]) -> list[str]:
    """Строки об изменениях товара — для уведомления и для карточки в боте."""
    lines = []
    for c in changes:
        p = c.product
        if c.kind == "drop":
            lines.append(f"📉 Цена снизилась на {c.percent}%: "
                         f"{format_price(c.old_price)} → <b>{format_price(p.price)}</b>")
        elif c.kind == "target":
            lines.append(f"🎯 Желаемая цена достигнута: <b>{format_price(p.price)}</b> "
                         f"(цель {format_price(tracking.target_price)})")
        elif c.kind == "out":
            lines.append("❌ Товар закончился")
        elif c.kind == "back":
            lines.append(f"✅ Снова в наличии: <b>{format_price(p.price)}</b>")
    return lines


def product_message(tracking: Tracking, changes: list[Change]) -> str:
    """Уведомление по одному отслеживаемому товару."""
    return "\n".join([_link(changes[0].product, 100), *change_notes(tracking, changes)])


def summary_message(tracking: Tracking, changes: list[Change]) -> str:
    """Уведомление по магазину/поиску: все изменения одним сообщением."""
    head = (f"🏪 Магазин «{esc(tracking.title)}»" if tracking.kind == "seller"
            else f"🔍 Поиск «{esc(tracking.title)}»")
    order = {"new": 0, "drop": 1, "back": 2, "out": 3}   # новинки и снижения — выше
    changes = sorted(changes, key=lambda c: order.get(c.kind, 9))
    lines = [f"{head}: изменения", *map(change_line, changes[:MAX_LINES])]
    if len(changes) > MAX_LINES:
        lines.append(f"… и ещё {len(changes) - MAX_LINES}. Подробнее — выгрузка в Excel "
                     "в «Мои отслеживания».")
    return "\n".join(lines)


class Checker:
    def __init__(self, db: Database, wb: WBSource, notify: Notify, drop_percent: float,
                 search_limit: int, seller_limit: int, pause: tuple[float, float] = PAUSE_BETWEEN):
        self.db = db
        self.wb = wb
        self.notify = notify
        self.drop_percent = drop_percent
        self.search_limit = search_limit
        self.seller_limit = seller_limit
        self.pause = pause
        # Проверка не должна запускаться дважды одновременно (по расписанию и /check)
        self.lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self.lock.locked()

    async def _sleep(self) -> None:
        if self.pause[1] > 0:
            await asyncio.sleep(random.uniform(*self.pause))

    async def run(self, user_id: int | None = None) -> int:
        """Один проход проверки. user_id — только отслеживания этого пользователя
        (для /check). Возвращает число отправленных уведомлений."""
        async with self.lock:
            trackings = await self.db.list_trackings(user_id)
            if not trackings:
                return 0
            log.info("Проверка: %d отслеживаний", len(trackings))
            try:
                fetched = await self._fetch(trackings)
            except BlockedError as e:
                # WB не пускает — нет смысла долбить дальше, попробуем в следующий раз
                log.warning("Проверка прервана: %s", e)
                return 0

            # Старые цены — до сохранения новых, иначе сравнивать будет не с чем
            all_products = {p.article: p for items in fetched.values() for p in items}
            old = await self.db.last_prices("wb", list(all_products))

            sent = 0
            for t in trackings:
                items = fetched.get((t.kind, t.target))
                if items is None:      # этот запрос не удался — пропускаем
                    continue
                text = await self._message(t, items, old)
                if text:
                    await self.notify(t.user_id, text)
                    sent += 1
            await self.db.save_snapshot(list(all_products.values()))
            log.info("Проверка закончена: товаров %d, уведомлений %d", len(all_products), sent)
            return sent

    async def _fetch(self, trackings: list[Tracking]) -> dict[tuple[str, str], list[ProductInfo]]:
        """Спросить у WB всё нужное — каждый магазин и запрос один раз,
        отдельные товары — пачками по 100. Ошибка одного запроса не роняет остальные."""
        result: dict[tuple[str, str], list[ProductInfo]] = {}
        articles = sorted({t.target for t in trackings if t.kind == "product"})
        if articles:
            try:
                found = {p.article: p for p in await self.wb.fetch_many(articles)}
                for article in articles:
                    if article in found:
                        result[("product", article)] = [found[article]]
                    else:
                        log.info("Товар %s не найден на WB (удалён?)", article)
            except BlockedError:
                raise
            except SourceError as e:
                log.warning("Товары: %s", e)

        for kind in ("seller", "search"):
            for target in sorted({t.target for t in trackings if t.kind == kind}):
                await self._sleep()
                try:
                    if kind == "seller":
                        items = await self.wb.seller(target, limit=self.seller_limit)
                    else:
                        items = await self.wb.search(target, limit=self.search_limit)
                    result[(kind, target)] = items
                except BlockedError:
                    raise
                except SourceError as e:
                    log.warning("%s %r: %s", kind, target, e)
        return result

    async def _message(self, tracking: Tracking, items: list[ProductInfo],
                       old: dict[str, PricePoint]) -> str | None:
        if tracking.kind == "product":
            changes = find_changes(old.get(tracking.target), items[0], self.drop_percent,
                                   tracking.target_price)
            return product_message(tracking, changes) if changes else None

        # Для поиска новым считаем только закрепившийся в топе товар (см. db.py)
        new_articles, first = await self.db.update_tracking_items(
            tracking.id, "wb", [p.article for p in items],
            confirm_twice=tracking.kind == "search")
        changes = []
        for p in items:
            if p.article in new_articles and not first:
                changes.append(Change("new", p))
            else:
                changes += find_changes(old.get(p.article), p, self.drop_percent)
        return summary_message(tracking, changes) if changes else None
