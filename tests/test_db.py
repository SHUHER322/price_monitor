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
