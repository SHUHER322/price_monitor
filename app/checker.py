"""Проверка цен по расписанию и уведомления.

Один проход проверки:
1. Берём все отслеживания из базы.
2. Собираем, что нужно спросить у маркетплейсов (WB, Ozon). Одинаковое не
   спрашиваем дважды: если два пользователя следят за одним магазином — это один
   запрос. Если один маркетплейс не пускает, второй всё равно проверяется.
3. Сравниваем новые цены с последними сохранёнными и находим изменения.
4. Сохраняем новые цены в историю и рассылаем уведомления.

Что считается изменением (функция find_changes — без сети и базы, легко тестировать):
    📉 цена снизилась больше чем на PRICE_DROP_PERCENT %
    🎯 цена опустилась до желаемой (только у товара, только в момент пересечения)
    ❌ товар закончился / ✅ снова в наличии
    📏 выбранный размер закончился / появился (если размеры выбраны — вместо ❌ / ✅)
    🆕 новый товар в магазине или в топе поиска
"""
import asyncio
import logging
import random
from dataclasses import dataclass
from typing import Awaitable, Callable

from .db import Database, PricePoint, Tracking
from .formatting import esc
from .sources.base import (SOURCE_NAMES, BlockedError, ProductInfo, SourceError, format_price,
                           size_available)

log = logging.getLogger(__name__)

MAX_LINES = 10                 # столько изменений в одном сообщении, остальное — «и ещё N»
PAUSE_BETWEEN = (2.0, 5.0)     # пауза между запросами к WB, секунды
Key = tuple[str, str, str]     # (вид, маркетплейс, цель): ("product", "ozon", "1291633069")

Notify = Callable[[int, str], Awaitable[None]]   # (user_id, текст) → отправить


@dataclass(frozen=True)
class Change:
    kind: str                  # "drop", "target", "out", "back", "new", "sizes_out", "sizes_back"
    product: ProductInfo
    old_price: int | None = None
    percent: int | None = None
    sizes: tuple[str, ...] = ()   # для sizes_out / sizes_back: какие размеры


def size_changes(old: PricePoint, new: ProductInfo, chosen: tuple[str, ...]) -> list[Change]:
    """Какие из выбранных размеров закончились или появились.
    Размер, который пропал из списка совсем, считаем закончившимся."""
    if not old.sizes:          # прошлых данных по размерам нет — сравнивать не с чем
        return []
    was = {s.get("name"): size_available(s) for s in old.sizes}
    now = {s.name: s.in_stock for s in new.sizes}
    gone = tuple(n for n in chosen if was.get(n) and not now.get(n, False))
    came = tuple(n for n in chosen if n in now and now[n] and not was.get(n, False))
    changes = []
    if gone:
        changes.append(Change("sizes_out", new, old.price, sizes=gone))
    if came:
        changes.append(Change("sizes_back", new, old.price, sizes=came))
    return changes


def find_changes(old: PricePoint | None, new: ProductInfo, drop_percent: float,
                 target_price: int | None = None,
                 sizes: tuple[str, ...] = ()) -> list[Change]:
    """Что изменилось у товара с прошлой проверки. sizes — выбранные размеры:
    тогда о наличии сообщаем по ним, а не по товару в целом."""
    if old is None:            # товара раньше не видели — сравнивать не с чем
        return []
    changes = []
    if sizes:
        changes += size_changes(old, new, sizes)
    else:
        was_in_stock = old.price is not None and old.quantity != 0
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
        elif c.kind == "sizes_back":
            word = "Появился размер" if len(c.sizes) == 1 else "Появились размеры"
            lines.append(f"✅ {word}: <b>{esc(', '.join(c.sizes))}</b>")
        elif c.kind == "sizes_out":
            word = "Закончился размер" if len(c.sizes) == 1 else "Закончились размеры"
            lines.append(f"❌ {word}: {esc(', '.join(c.sizes))}")
    return lines


def product_message(tracking: Tracking, changes: list[Change]) -> str:
    """Уведомление по одному отслеживаемому товару."""
    product = changes[0].product
    market = SOURCE_NAMES.get(product.source, product.source)
    return "\n".join([f"{market}: {_link(product, 100)}", *change_notes(tracking, changes)])


def summary_message(tracking: Tracking, changes: list[Change]) -> str:
    """Уведомление по магазину/поиску: все изменения одним сообщением."""
    market = SOURCE_NAMES.get(tracking.source, tracking.source)
    head = (f"🏪 Магазин «{esc(tracking.title)}» · {market}" if tracking.kind == "seller"
            else f"🔍 Поиск «{esc(tracking.title)}» · {market}")
    order = {"new": 0, "drop": 1, "back": 2, "out": 3}   # новинки и снижения — выше
    changes = sorted(changes, key=lambda c: order.get(c.kind, 9))
    lines = [f"{head}: изменения", *map(change_line, changes[:MAX_LINES])]
    if len(changes) > MAX_LINES:
        lines.append(f"… и ещё {len(changes) - MAX_LINES}. Подробнее — выгрузка в Excel "
                     "в «Мои отслеживания».")
    return "\n".join(lines)


class Checker:
    def __init__(self, db: Database, sources: dict, notify: Notify, drop_percent: float,
                 search_limit: int, seller_limit: int, pause: tuple[float, float] = PAUSE_BETWEEN):
        """sources — {"wb": WBSource, "ozon": OzonSource}: у каждого есть fetch_many,
        у WB ещё search и seller."""
        self.db = db
        self.sources = sources
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
            fetched = await self._fetch(trackings)

            # Старые цены — до сохранения новых, иначе сравнивать будет не с чем
            all_products = {(p.source, p.article): p for items in fetched.values() for p in items}
            old: dict[tuple[str, str], PricePoint] = {}
            for source in {source for source, _ in all_products}:
                articles = [a for s, a in all_products if s == source]
                for article, point in (await self.db.last_prices(source, articles)).items():
                    old[(source, article)] = point

            sent = 0
            for t in trackings:
                items = fetched.get((t.kind, t.source, t.target))
                if items is None:      # этот запрос не удался — пропускаем
                    continue
                text = await self._message(t, items, old)
                if text:
                    await self.notify(t.user_id, text)
                    sent += 1
            await self.db.save_snapshot(list(all_products.values()))
            log.info("Проверка закончена: товаров %d, уведомлений %d", len(all_products), sent)
            return sent

    async def _fetch_products(self, source: str, articles: list[str]) -> list[ProductInfo]:
        if source == "wb":
            return await self.sources["wb"].fetch_many(articles)   # пачками по 100
        # Ozon запрашивает товар по ссылке — берём её из базы (или артикул, если нет)
        urls = await self.db.product_urls(source, articles)
        return await self.sources[source].fetch_many([urls.get(a, a) for a in articles])

    async def _fetch(self, trackings: list[Tracking]) -> dict[Key, list[ProductInfo]]:
        """Спросить у маркетплейсов всё нужное: товары — по маркетплейсам за раз,
        каждый магазин и поиск — один раз. Ошибка одного запроса не роняет остальные,
        а если маркетплейс не пускает — дальше его не трогаем до следующей проверки."""
        result: dict[Key, list[ProductInfo]] = {}
        blocked: set[str] = set()

        products = [t for t in trackings if t.kind == "product"]
        for source in sorted({t.source for t in products}):
            if source not in self.sources:
                log.warning("Нет источника %r — пропускаю", source)
                continue
            articles = sorted({t.target for t in products if t.source == source})
            try:
                found = {p.article: p for p in await self._fetch_products(source, articles)}
            except BlockedError as e:
                log.warning("%s не пускает: %s", source, e)
                blocked.add(source)
                continue
            except SourceError as e:
                log.warning("Товары %s: %s", source, e)
                continue
            for article in articles:
                if article in found:
                    result[("product", source, article)] = [found[article]]
                else:
                    log.info("Товар %s %s не найден (удалён?)", source, article)

        # Магазины и поиск — у WB и Ozon (у Ozon лимит магазина меньше, см. ozon.py)
        for kind in ("seller", "search"):
            for source, target in sorted({(t.source, t.target) for t in trackings
                                          if t.kind == kind}):
                src = self.sources.get(source)
                if src is None or source in blocked:
                    continue
                await self._sleep()
                try:
                    if kind == "seller":
                        items = await src.seller(target, limit=self.seller_limit)
                    else:
                        items = await src.search(target, limit=self.search_limit)
                    result[(kind, source, target)] = items
                except BlockedError as e:
                    log.warning("%s не пускает: %s", source, e)
                    blocked.add(source)
                except SourceError as e:
                    log.warning("%s %s %r: %s", source, kind, target, e)
        return result

    async def _message(self, tracking: Tracking, items: list[ProductInfo],
                       old: dict[tuple[str, str], PricePoint]) -> str | None:
        if tracking.kind == "product":
            changes = find_changes(old.get((tracking.source, tracking.target)), items[0],
                                   self.drop_percent, tracking.target_price, tracking.sizes)
            return product_message(tracking, changes) if changes else None

        # Для поиска новым считаем только закрепившийся в топе товар (см. db.py)
        # Источник товаров — у самих товаров: у Ozon в списках это OZON_CARD
        items_source = items[0].source if items else tracking.source
        new_articles, first = await self.db.update_tracking_items(
            tracking.id, items_source, [p.article for p in items],
            confirm_twice=tracking.kind == "search")
        changes = []
        for p in items:
            if p.article in new_articles and not first:
                changes.append(Change("new", p))
            else:
                changes += find_changes(old.get((p.source, p.article)), p, self.drop_percent)
        return summary_message(tracking, changes) if changes else None
