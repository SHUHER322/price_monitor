"""База SQLite: что отслеживаем и история цен.

Таблицы:
    trackings      — отслеживания пользователей: товар, магазин или поисковый запрос
    tracking_items — какие товары входят в отслеживание магазина/поиска
                     и на каком месте (чтобы заметить нового конкурента в выдаче)
    products       — последние известные сведения о товаре (название, продавец)
    prices         — история: цена, наличие, рейтинг на момент каждой проверки

Время храним в UTC (всемирное время) — так проверки с сервера в любом поясе
не перепутаются. В пояс пользователя переводим только при показе.
"""
import json
from dataclasses import dataclass
from datetime import datetime, timezone

import aiosqlite

from .sources.base import ProductInfo

KINDS = ("product", "seller", "search")

SCHEMA = """
CREATE TABLE IF NOT EXISTS trackings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    kind         TEXT    NOT NULL CHECK (kind IN ('product', 'seller', 'search')),
    source       TEXT    NOT NULL,          -- 'wb', позже 'ozon'
    target       TEXT    NOT NULL,          -- артикул, ID продавца или текст запроса
    title        TEXT    NOT NULL,          -- что показывать в списке
    target_price INTEGER,                   -- желаемая цена в копейках (только для товара)
    created_at   TEXT    NOT NULL,
    UNIQUE (user_id, kind, source, target)  -- одно и то же дважды не добавить
);

CREATE TABLE IF NOT EXISTS tracking_items (
    tracking_id INTEGER NOT NULL REFERENCES trackings(id) ON DELETE CASCADE,
    source      TEXT    NOT NULL,
    article     TEXT    NOT NULL,
    first_seen  TEXT    NOT NULL,
    last_seen   TEXT    NOT NULL,
    position    INTEGER,                    -- место в выдаче/магазине: 1, 2, 3…
    streak      INTEGER NOT NULL DEFAULT 2, -- сколько проверок подряд товар в выдаче
    PRIMARY KEY (tracking_id, source, article)
);

CREATE TABLE IF NOT EXISTS products (
    source    TEXT NOT NULL,
    article   TEXT NOT NULL,
    name      TEXT NOT NULL,
    seller    TEXT NOT NULL,
    seller_id TEXT NOT NULL,
    brand     TEXT NOT NULL,
    url       TEXT NOT NULL,
    PRIMARY KEY (source, article)
);

CREATE TABLE IF NOT EXISTS prices (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    source     TEXT    NOT NULL,
    article    TEXT    NOT NULL,
    checked_at TEXT    NOT NULL,
    price      INTEGER,          -- копейки; NULL — нет в наличии
    old_price  INTEGER,
    quantity   INTEGER NOT NULL,
    rating     REAL,
    feedbacks  INTEGER NOT NULL,
    sizes      TEXT,             -- цены по размерам (JSON), если размеры есть
    card_price INTEGER           -- цена по карте маркетплейса (Ozon), копейки
);

-- Индекс: быстро найти историю одного товара по времени
CREATE INDEX IF NOT EXISTS prices_by_article ON prices (source, article, checked_at);
"""


@dataclass(frozen=True)
class Tracking:
    id: int
    user_id: int
    kind: str
    source: str
    target: str
    title: str
    target_price: int | None
    created_at: str


@dataclass(frozen=True)
class ItemRow:
    """Товар отслеживания (магазина/поиска) с последней ценой — для выгрузки."""
    position: int | None
    article: str
    name: str
    brand: str
    seller: str
    url: str
    price: int | None
    old_price: int | None
    quantity: int | None
    rating: float | None
    feedbacks: int | None
    checked_at: str | None


@dataclass(frozen=True)
class ProductSummary:
    """Всё, что известно о товаре в нашей базе — для карточки в боте."""
    name: str
    url: str
    price: int | None
    old_price: int | None
    quantity: int
    sizes: list[dict]          # [{"name": "M", "price": 113800, "quantity": None}, …]
    checked_at: str
    min_price: int | None      # за всё время отслеживания
    max_price: int | None
    checks: int                # сколько раз проверяли
    card_price: int | None = None  # цена по карте (Ozon) на последней проверке


@dataclass(frozen=True)
class PricePoint:
    """Одна запись истории цены."""
    checked_at: str
    price: int | None
    old_price: int | None
    quantity: int


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _sizes_json(info: ProductInfo) -> str | None:
    if not info.has_sizes:
        return None
    return json.dumps([{"name": s.name, "price": s.price, "quantity": s.quantity}
                       for s in info.sizes], ensure_ascii=False)


class Database:
    def __init__(self, path: str):
        self.path = path

    def _connect(self):
        return aiosqlite.connect(self.path)

    async def init(self) -> None:
        async with self._connect() as db:
            await db.executescript(SCHEMA)
            # Миграция: в базах, созданных до появления столбца position,
            # добавляем его (данные не теряются — как category в lead_bot)
            columns = {row[1] for row in await (await db.execute(
                "PRAGMA table_info(tracking_items)")).fetchall()}
            if "position" not in columns:
                await db.execute("ALTER TABLE tracking_items ADD COLUMN position INTEGER")
            price_columns = {row[1] for row in await (await db.execute(
                "PRAGMA table_info(prices)")).fetchall()}
            if "card_price" not in price_columns:
                await db.execute("ALTER TABLE prices ADD COLUMN card_price INTEGER")
            if "streak" not in columns:
                # Уже известные товары считаем «закрепившимися» (2), чтобы после
                # обновления бот не объявил их все новыми
                await db.execute("ALTER TABLE tracking_items "
                                 "ADD COLUMN streak INTEGER NOT NULL DEFAULT 2")
            await db.commit()

    # --- отслеживания ---

    async def add_tracking(self, user_id: int, kind: str, source: str, target: str,
                           title: str, target_price: int | None = None) -> tuple[int, bool]:
        """Добавить отслеживание. Возвращает (id, добавлено ли новое).
        Если такое уже есть — (id существующего, False)."""
        if kind not in KINDS:
            raise ValueError(f"Неизвестный вид отслеживания: {kind!r}")
        async with self._connect() as db:
            cursor = await db.execute(
                "INSERT OR IGNORE INTO trackings "
                "(user_id, kind, source, target, title, target_price, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, kind, source, target, title, target_price, utc_now()))
            await db.commit()
            if cursor.rowcount:
                return cursor.lastrowid, True
            row = await (await db.execute(
                "SELECT id FROM trackings WHERE user_id=? AND kind=? AND source=? AND target=?",
                (user_id, kind, source, target))).fetchone()
            return row[0], False

    async def list_trackings(self, user_id: int | None = None) -> list[Tracking]:
        """Отслеживания пользователя, или все (для проверки по расписанию)."""
        query = ("SELECT id, user_id, kind, source, target, title, target_price, created_at "
                 "FROM trackings")
        params: tuple = ()
        if user_id is not None:
            query += " WHERE user_id = ?"
            params = (user_id,)
        async with self._connect() as db:
            rows = await (await db.execute(query + " ORDER BY id", params)).fetchall()
        return [Tracking(*row) for row in rows]

    async def get_tracking(self, user_id: int, tracking_id: int) -> Tracking | None:
        """Одно отслеживание — только если оно принадлежит этому пользователю."""
        async with self._connect() as db:
            row = await (await db.execute(
                "SELECT id, user_id, kind, source, target, title, target_price, created_at "
                "FROM trackings WHERE id = ? AND user_id = ?", (tracking_id, user_id))).fetchone()
        return Tracking(*row) if row else None

    async def count_trackings(self, user_id: int) -> int:
        async with self._connect() as db:
            row = await (await db.execute(
                "SELECT COUNT(*) FROM trackings WHERE user_id = ?", (user_id,))).fetchone()
        return row[0]

    async def set_target_price(self, user_id: int, tracking_id: int,
                               target_price: int | None) -> bool:
        """Изменить желаемую цену товара (None — убрать). Только своего."""
        async with self._connect() as db:
            cursor = await db.execute(
                "UPDATE trackings SET target_price = ? "
                "WHERE id = ? AND user_id = ? AND kind = 'product'",
                (target_price, tracking_id, user_id))
            await db.commit()
            return cursor.rowcount > 0

    async def remove_tracking(self, user_id: int, tracking_id: int) -> bool:
        """Удалить. user_id в условии — чтобы нельзя было удалить чужое."""
        async with self._connect() as db:
            await db.execute("DELETE FROM tracking_items WHERE tracking_id = ?", (tracking_id,))
            cursor = await db.execute("DELETE FROM trackings WHERE id = ? AND user_id = ?",
                                      (tracking_id, user_id))
            await db.commit()
            return cursor.rowcount > 0

    async def update_tracking_items(self, tracking_id: int, source: str, articles: list[str],
                                    confirm_twice: bool = False) -> tuple[set[str], bool]:
        """Запомнить, какие товары сейчас в магазине/выдаче и на каких местах
        (порядок списка articles = порядок на сайте).

        Возвращает (о каких товарах сообщить как о новых, первая ли это проверка).
        На первой проверке ничего не сообщаем — это точка отсчёта.

        confirm_twice — для поиска: WB перемешивает выдачу (реклама, подстройка
        под покупателя), и товары то выпадают из топа, то возвращаются. Поэтому
        новым считаем товар, который продержался в топе две проверки подряд.
        Для магазина — сообщаем сразу о товаре, которого раньше не было.
        """
        now = utc_now()
        async with self._connect() as db:
            rows = await (await db.execute(
                "SELECT article, last_seen, streak FROM tracking_items WHERE tracking_id = ?",
                (tracking_id,))).fetchall()
            known = {article: (last_seen, streak) for article, last_seen, streak in rows}
            first = not known
            # Время прошлой проверки: у кого last_seen совпадает — были в выдаче и тогда
            previous = max((last for last, _ in known.values()), default=None)

            announce, values = set(), []
            for position, article in enumerate(articles, 1):
                if first:
                    streak = 2                       # точка отсчёта — уже «свои»
                elif article in known and known[article][0] == previous:
                    streak = known[article][1] + 1   # был и в прошлый раз — серия растёт
                else:
                    streak = 1                       # появился (впервые или вернулся)
                if not first:
                    if confirm_twice and streak == 2:
                        announce.add(article)        # вторая проверка подряд — закрепился
                    elif not confirm_twice and article not in known:
                        announce.add(article)        # в магазине раньше не было
                values.append((tracking_id, source, article, now, now, position, streak))

            await db.executemany(
                "INSERT INTO tracking_items "
                "(tracking_id, source, article, first_seen, last_seen, position, streak) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (tracking_id, source, article) DO UPDATE SET "
                "last_seen = excluded.last_seen, position = excluded.position, "
                "streak = excluded.streak", values)
            await db.commit()
        return announce, first

    # --- товары и цены ---

    async def last_prices(self, source: str, articles: list[str]) -> dict[str, PricePoint]:
        """Последняя сохранённая цена каждого товара — чтобы сравнить с новой."""
        if not articles:
            return {}
        marks = ",".join("?" * len(articles))
        # Для каждого артикула берём строку с самым поздним временем проверки
        query = f"""
            SELECT p.article, p.checked_at, p.price, p.old_price, p.quantity
            FROM prices p
            JOIN (SELECT article, MAX(checked_at) AS last FROM prices
                  WHERE source = ? AND article IN ({marks}) GROUP BY article) l
              ON p.article = l.article AND p.checked_at = l.last
            WHERE p.source = ?
        """
        async with self._connect() as db:
            rows = await (await db.execute(query, (source, *articles, source))).fetchall()
        return {row[0]: PricePoint(*row[1:]) for row in rows}

    async def save_snapshot(self, products: list[ProductInfo], checked_at: str | None = None) -> None:
        """Сохранить результат проверки: сведения о товарах и строку в историю цен."""
        checked_at = checked_at or utc_now()
        async with self._connect() as db:
            await db.executemany(
                "INSERT INTO products (source, article, name, seller, seller_id, brand, url) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (source, article) DO UPDATE SET name = excluded.name, "
                "seller = excluded.seller, seller_id = excluded.seller_id, "
                "brand = excluded.brand, url = excluded.url",
                [(p.source, p.article, p.name, p.seller, p.seller_id, p.brand, p.url)
                 for p in products])
            await db.executemany(
                "INSERT INTO prices (source, article, checked_at, price, old_price, quantity, "
                "rating, feedbacks, sizes, card_price) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(p.source, p.article, checked_at, p.price, p.old_price, p.quantity,
                  p.rating, p.feedbacks, _sizes_json(p), p.card_price) for p in products])
            await db.commit()

    async def price_history(self, source: str, article: str) -> list[PricePoint]:
        """Вся история цены товара, от старых к новым (для графика)."""
        async with self._connect() as db:
            rows = await (await db.execute(
                "SELECT checked_at, price, old_price, quantity FROM prices "
                "WHERE source = ? AND article = ? ORDER BY checked_at",
                (source, article))).fetchall()
        return [PricePoint(*row) for row in rows]

    async def tracking_rows(self, tracking_id: int) -> list[ItemRow]:
        """Товары, которые были в магазине/выдаче на последней проверке,
        с последней известной ценой — по порядку мест."""
        # cur  — товары с самой свежей отметкой last_seen (выпавшие из топа — старше)
        # last — id самой свежей записи цены каждого товара
        query = """
            WITH cur AS (
                SELECT source, article, position, rowid AS added FROM tracking_items
                WHERE tracking_id = ? AND last_seen =
                      (SELECT MAX(last_seen) FROM tracking_items WHERE tracking_id = ?)
            ),
            last AS (
                SELECT MAX(id) AS id FROM prices
                WHERE (source, article) IN (SELECT source, article FROM cur)
                GROUP BY source, article
            )
            SELECT cur.position, cur.article, p.name, p.brand, p.seller, p.url,
                   pr.price, pr.old_price, pr.quantity, pr.rating, pr.feedbacks, pr.checked_at
            FROM cur
            JOIN products p ON p.source = cur.source AND p.article = cur.article
            LEFT JOIN prices pr ON pr.id IN (SELECT id FROM last)
                               AND pr.source = cur.source AND pr.article = cur.article
            -- без места (старые записи до миграции) — в порядке добавления
            ORDER BY cur.position IS NULL, cur.position, cur.added
        """
        async with self._connect() as db:
            rows = await (await db.execute(query, (tracking_id, tracking_id))).fetchall()
        return [ItemRow(*row) for row in rows]

    async def product_summary(self, source: str, article: str) -> ProductSummary | None:
        """Последние сведения о товаре и разброс цены за всё время."""
        async with self._connect() as db:
            last = await (await db.execute(
                "SELECT p.name, p.url, pr.price, pr.old_price, pr.quantity, pr.sizes, "
                "pr.checked_at, pr.card_price FROM products p "
                "JOIN prices pr ON pr.source = p.source AND pr.article = p.article "
                "WHERE p.source = ? AND p.article = ? ORDER BY pr.id DESC LIMIT 1",
                (source, article))).fetchone()
            if last is None:
                return None
            # MIN и MAX пропускают NULL — периоды «нет в наличии» не мешают
            stats = await (await db.execute(
                "SELECT MIN(price), MAX(price), COUNT(*) FROM prices "
                "WHERE source = ? AND article = ?", (source, article))).fetchone()
        name, url, price, old_price, quantity, sizes, checked_at, card_price = last
        return ProductSummary(name, url, price, old_price, quantity,
                              json.loads(sizes) if sizes else [], checked_at, *stats,
                              card_price=card_price)

    async def product_urls(self, source: str, articles: list[str]) -> dict[str, str]:
        """Ссылки на товары из базы — Ozon запрашивает товар по ссылке, а не по артикулу."""
        if not articles:
            return {}
        marks = ",".join("?" * len(articles))
        async with self._connect() as db:
            rows = await (await db.execute(
                f"SELECT article, url FROM products WHERE source = ? AND article IN ({marks})",
                (source, *articles))).fetchall()
        return dict(rows)
