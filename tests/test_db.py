"""Тесты базы — на временном файле, который pytest создаёт и удаляет сам (tmp_path)."""
import asyncio

import pytest

from app.db import Database
from app.sources.base import ProductInfo, SizeInfo


def run(coro):
    """Запустить асинхронную функцию в обычном тесте."""
    return asyncio.run(coro)


@pytest.fixture
def db(tmp_path) -> Database:
    database = Database(str(tmp_path / "test.db"))
    run(database.init())
    return database


def product(article="1", price=276300, quantity=5, sizes=()) -> ProductInfo:
    return ProductInfo(source="wb", article=article, url=f"https://wb/{article}",
                       name=f"Товар {article}", price=price, old_price=650300,
                       quantity=quantity, sizes=sizes, seller="K A.RU", seller_id="250079242")


# --- отслеживания ---

def test_add_and_list(db):
    tracking_id, created = run(db.add_tracking(111, "product", "wb", "683146715",
                                               "Коврик", target_price=250000))
    assert created
    [tracking] = run(db.list_trackings(111))
    assert tracking.id == tracking_id
    assert tracking.kind == "product"
    assert tracking.target_price == 250000


def test_duplicate_not_added(db):
    first, _ = run(db.add_tracking(111, "seller", "wb", "250079242", "K A.RU"))
    second, created = run(db.add_tracking(111, "seller", "wb", "250079242", "K A.RU"))
    assert not created
    assert first == second
    assert run(db.count_trackings(111)) == 1


def test_users_see_only_own(db):
    run(db.add_tracking(111, "search", "wb", "коврик", "коврик"))
    run(db.add_tracking(222, "search", "wb", "коврик", "коврик"))  # у другого — можно
    assert len(run(db.list_trackings(111))) == 1
    assert len(run(db.list_trackings())) == 2   # все — для проверки по расписанию


def test_cannot_remove_others(db):
    tracking_id, _ = run(db.add_tracking(111, "search", "wb", "коврик", "коврик"))
    assert not run(db.remove_tracking(222, tracking_id))
    assert run(db.remove_tracking(111, tracking_id))
    assert run(db.list_trackings(111)) == []


def test_unknown_kind(db):
    with pytest.raises(ValueError):
        run(db.add_tracking(111, "category", "wb", "x", "x"))


def test_new_items_detected(db):
    tracking_id, _ = run(db.add_tracking(111, "search", "wb", "коврик", "коврик"))
    new, first = run(db.update_tracking_items(tracking_id, "wb", ["1", "2"]))
    assert first                       # первая проверка — не уведомляем
    new, first = run(db.update_tracking_items(tracking_id, "wb", ["2", "3"]))
    assert not first
    assert new == {"3"}                # в выдаче появился новый товар


# --- цены ---

def test_last_prices(db):
    run(db.save_snapshot([product("1", 300000), product("2", 100000)], "2026-10-20 10:00:00"))
    run(db.save_snapshot([product("1", 276300)], "2026-10-20 16:00:00"))
    last = run(db.last_prices("wb", ["1", "2", "999"]))
    assert last["1"].price == 276300            # берётся самая свежая
    assert last["2"].price == 100000
    assert "999" not in last                    # нет в базе — нет и в ответе


def test_history_in_order(db):
    run(db.save_snapshot([product("1", 300000)], "2026-10-20 10:00:00"))
    run(db.save_snapshot([product("1", None, quantity=0)], "2026-10-20 16:00:00"))
    history = run(db.price_history("wb", "1"))
    assert [p.price for p in history] == [300000, None]   # закончился — цена None
    assert history[1].quantity == 0


def test_snapshot_with_sizes(db):
    sizes = (SizeInfo("S", 250000, 500000, None), SizeInfo("M", None, None, None))
    run(db.save_snapshot([product("1", sizes=sizes)]))
    assert run(db.last_prices("wb", ["1"]))["1"].price == 276300


def test_empty_lists(db):
    assert run(db.last_prices("wb", [])) == {}
    run(db.save_snapshot([]))   # не падает


# --- выгрузка: товары отслеживания с местами ---

def test_tracking_rows_in_order_with_latest_price(db):
    tracking_id, _ = run(db.add_tracking(111, "search", "wb", "коврик", "коврик"))
    run(db.save_snapshot([product("1", 300000), product("2", 100000), product("3", 50000)],
                         "2026-10-20 10:00:00"))
    run(db.update_tracking_items(tracking_id, "wb", ["2", "1", "3"]))   # места: 2, 1, 3
    run(db.save_snapshot([product("1", 276300)], "2026-10-20 16:00:00"))
    rows = run(db.tracking_rows(tracking_id))
    assert [(r.position, r.article) for r in rows] == [(1, "2"), (2, "1"), (3, "3")]
    assert rows[1].price == 276300          # у товара 1 — свежая цена, а не первая
    assert rows[0].seller == "K A.RU"


def test_dropped_from_top_not_exported(db):
    tracking_id, _ = run(db.add_tracking(111, "search", "wb", "коврик", "коврик"))
    run(db.save_snapshot([product("1"), product("2"), product("3")]))
    run(db.update_tracking_items(tracking_id, "wb", ["1", "2"]))
    import time; time.sleep(1.1)            # следующая проверка — на секунду позже
    run(db.update_tracking_items(tracking_id, "wb", ["3", "1"]))   # «2» выпал из топа
    rows = run(db.tracking_rows(tracking_id))
    assert [(r.position, r.article) for r in rows] == [(1, "3"), (2, "1")]


def test_migration_adds_position(tmp_path):
    # База «старого образца» — без столбца position, как у созданной до обновления
    import sqlite3
    path = str(tmp_path / "old.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE tracking_items (tracking_id INTEGER NOT NULL, source TEXT NOT NULL, "
                "article TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, "
                "PRIMARY KEY (tracking_id, source, article))")
    con.execute("INSERT INTO tracking_items VALUES (1, 'wb', '42', 'x', 'x')")
    con.commit()
    con.close()
    run(Database(path).init())
    con = sqlite3.connect(path)
    columns = {row[1] for row in con.execute("PRAGMA table_info(tracking_items)")}
    assert "position" in columns
    assert con.execute("SELECT article FROM tracking_items").fetchall() == [("42",)]  # данные целы
    con.close()


def test_get_tracking_only_own(db):
    tracking_id, _ = run(db.add_tracking(111, "seller", "wb", "250079242", "K A.RU"))
    assert run(db.get_tracking(111, tracking_id)).title == "K A.RU"
    assert run(db.get_tracking(222, tracking_id)) is None


# --- карточка товара ---

def test_set_target_price_only_own_product(db):
    product_id, _ = run(db.add_tracking(111, "product", "wb", "1", "Коврик", target_price=250000))
    seller_id, _ = run(db.add_tracking(111, "seller", "wb", "2", "K A.RU"))
    assert run(db.set_target_price(111, product_id, 200000))
    assert run(db.get_tracking(111, product_id)).target_price == 200000
    assert run(db.set_target_price(111, product_id, None))           # убрать цель
    assert run(db.get_tracking(111, product_id)).target_price is None
    assert not run(db.set_target_price(222, product_id, 100000))     # чужое — нельзя
    assert not run(db.set_target_price(111, seller_id, 100000))      # у магазина цели нет


def test_product_summary(db):
    assert run(db.product_summary("wb", "1")) is None
    run(db.save_snapshot([product("1", 300000)], "2026-10-20 10:00:00"))
    run(db.save_snapshot([product("1", None, quantity=0)], "2026-10-20 13:00:00"))
    sizes = (SizeInfo("S", 250000, 500000, 3), SizeInfo("M", None, None, 0))
    run(db.save_snapshot([product("1", 276300, sizes=sizes)], "2026-10-20 16:00:00"))
    summary = run(db.product_summary("wb", "1"))
    assert summary.price == 276300                  # последняя цена
    assert (summary.min_price, summary.max_price) == (276300, 300000)  # «нет в наличии» не мешает
    assert summary.checks == 3
    assert [s["name"] for s in summary.sizes] == ["S", "M"]
    assert summary.checked_at == "2026-10-20 16:00:00"


# --- «новые в топе» для поиска: только закрепившиеся ---

def check(db, tracking_id, articles):
    """Одна «проверка» поиска; между проверками — секунда, как будто прошло время."""
    import time
    time.sleep(1.1)
    new, _ = run(db.update_tracking_items(tracking_id, "wb", articles, confirm_twice=True))
    return new


def test_search_new_only_after_two_checks(db):
    tracking_id, _ = run(db.add_tracking(111, "search", "wb", "коврик", "коврик"))
    assert check(db, tracking_id, ["1", "2"]) == set()        # точка отсчёта
    assert check(db, tracking_id, ["1", "3"]) == set()        # 3 появился — ждём
    assert check(db, tracking_id, ["1", "3"]) == {"3"}        # держится вторую — новый
    assert check(db, tracking_id, ["1", "3"]) == set()        # о нём уже сообщили


def test_search_flicker_not_reported(db):
    tracking_id, _ = run(db.add_tracking(111, "search", "wb", "коврик", "коврик"))
    check(db, tracking_id, ["1"])
    # Товар 5 «мелькает»: то есть, то нет — ни разу не две проверки подряд
    assert check(db, tracking_id, ["1", "5"]) == set()
    assert check(db, tracking_id, ["1"]) == set()
    assert check(db, tracking_id, ["1", "5"]) == set()


def test_baseline_items_not_reported_after_dropout(db):
    tracking_id, _ = run(db.add_tracking(111, "search", "wb", "коврик", "коврик"))
    check(db, tracking_id, ["1", "2"])
    assert check(db, tracking_id, ["1", "2"]) == set()        # «свои» с первой проверки



# --- Ozon: цена по карте и ссылки ---

def test_card_price_and_urls(db):
    info = ProductInfo(source="ozon", article="777", url="https://www.ozon.ru/product/mikser-777/",
                       name="Микшер", price=342700, old_price=None, quantity=1,
                       card_price=309800)
    run(db.save_snapshot([info]))
    assert run(db.product_summary("ozon", "777")).card_price == 309800
    assert run(db.product_urls("ozon", ["777", "1"])) == {
        "777": "https://www.ozon.ru/product/mikser-777/"}
    assert run(db.product_urls("ozon", [])) == {}


def test_migration_adds_card_price(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE prices (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, "
                "article TEXT NOT NULL, checked_at TEXT NOT NULL, price INTEGER, old_price INTEGER, "
                "quantity INTEGER NOT NULL, rating REAL, feedbacks INTEGER NOT NULL, sizes TEXT)")
    con.execute("INSERT INTO prices (source, article, checked_at, price, quantity, feedbacks) "
                "VALUES ('wb', '1', 'x', 100, 1, 0)")
    con.commit()
    con.close()
    run(Database(path).init())
    con = sqlite3.connect(path)
    assert "card_price" in {row[1] for row in con.execute("PRAGMA table_info(prices)")}
    assert con.execute("SELECT COUNT(*) FROM prices").fetchone() == (1,)   # данные целы
    con.close()
