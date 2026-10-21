"""Тесты проверки цен и уведомлений — с поддельным WB и настоящей базой во временном файле."""
import asyncio

import pytest

from app.checker import (Checker, change_notes, find_changes, product_message,
                         summary_message)
from app.db import Database, PricePoint, Tracking
from app.sources.base import BlockedError, ProductInfo, SizeInfo, SourceError


def run(coro):
    return asyncio.run(coro)


def product(article="1", price=300000, quantity=5) -> ProductInfo:
    return ProductInfo(source="wb", article=article, name=f"Коврик {article}",
                       url=f"https://www.wildberries.ru/catalog/{article}/detail.aspx",
                       price=price, old_price=650300, quantity=quantity, seller="K A.RU")


def point(price=300000, quantity=5) -> PricePoint:
    return PricePoint("2026-10-20 10:00:00", price, 650300, quantity)


def kinds(changes) -> list[str]:
    return [c.kind for c in changes]


# --- что считается изменением ---

def test_drop_over_threshold():
    [change] = find_changes(point(300000), product(price=270000), drop_percent=5)
    assert change.kind == "drop"
    assert change.percent == 10


def test_small_drop_and_rise_ignored():
    assert find_changes(point(300000), product(price=290000), drop_percent=5) == []  # −3%
    assert find_changes(point(300000), product(price=350000), drop_percent=5) == []  # рост


def test_target_reached_only_on_crossing():
    # Пересекла желаемую цену — уведомляем
    assert "target" in kinds(find_changes(point(260000), product(price=240000), 50, 250000))
    # Уже была ниже цели на прошлой проверке — не повторяем
    assert find_changes(point(240000), product(price=239000), 50, 250000) == []


def test_out_and_back_in_stock():
    assert kinds(find_changes(point(300000), product(price=None, quantity=0), 5)) == ["out"]
    assert kinds(find_changes(point(None, 0), product(price=300000), 5)) == ["back"]


def test_first_time_seen_no_changes():
    assert find_changes(None, product(), 5) == []


def sized(*available: str, price=300000) -> ProductInfo:
    """Товар с размерами S, M, L; в наличии — перечисленные."""
    sizes = tuple(SizeInfo(n, 300000 if n in available else None, None, None)
                  for n in ("S", "M", "L"))
    return ProductInfo(source="wb", article="1", name="Куртка", url="u", price=price,
                       old_price=None, quantity=5 if available else 0, sizes=sizes)


def sized_point(*available: str) -> PricePoint:
    sizes = tuple({"name": n, "price": 300000 if n in available else None, "quantity": None}
                  for n in ("S", "M", "L"))
    return PricePoint("x", 300000, None, 5, sizes)


def test_chosen_size_out_and_back():
    [out] = find_changes(sized_point("S", "M"), sized("S"), 5, sizes=("M",))
    assert out.kind == "sizes_out" and out.sizes == ("M",)
    [back] = find_changes(sized_point("S"), sized("S", "M", "L"), 5, sizes=("M", "L"))
    assert back.kind == "sizes_back" and back.sizes == ("M", "L")


def test_other_sizes_ignored():
    # Закончился S, а следим за M — молчим
    assert find_changes(sized_point("S", "M"), sized("M"), 5, sizes=("M",)) == []


def test_chosen_sizes_replace_product_stock():
    # Закончились все размеры: с выбранными размерами — сообщение по размеру, а не «товар закончился»
    assert kinds(find_changes(sized_point("M"), sized(price=None), 5, sizes=("M",))) == ["sizes_out"]
    assert kinds(find_changes(sized_point("M"), sized(price=None), 5)) == ["out"]


def test_sizes_without_history_no_changes():
    # Прошлая проверка без размеров (до обновления бота) — сравнивать не с чем
    assert find_changes(point(300000), sized("M"), 5, sizes=("M",)) == []


def test_size_notes():
    t = tracking()
    changes = find_changes(sized_point("S"), sized("S", "M", "L"), 5, sizes=("M", "L"))
    assert change_notes(t, changes) == ["✅ Появились размеры: <b>M, L</b>"]


# --- тексты ---

def tracking(kind="product", target="1", title="Коврик", target_price=None, id=1) -> Tracking:
    return Tracking(id=id, user_id=111, kind=kind, source="wb", target=target, title=title,
                    target_price=target_price, created_at="2026-10-20 10:00:00")


def test_product_message():
    changes = find_changes(point(300000), product(price=240000), 5, 250000)
    text = product_message(tracking(target_price=250000), changes)
    assert "Цена снизилась на 20%: 3 000 ₽ → <b>2 400 ₽</b>" in text
    assert "Желаемая цена достигнута" in text
    assert 'href="https://www.wildberries.ru/catalog/1/detail.aspx"' in text


def test_summary_cut_to_ten_lines():
    changes = [c for i in range(15)
               for c in find_changes(point(300000), product(str(i), 200000), 5)]
    text = summary_message(tracking("search", "коврик", "коврик"), changes)
    assert text.startswith("🔍 Поиск «коврик» · WB: изменения")
    assert text.count("📉") == 10
    assert "… и ещё 5" in text


# --- целый проход проверки ---

class FakeWB:
    """Поддельный источник: отдаёт то, что положили в словари, и считает запросы."""
    def __init__(self):
        self.products: dict[str, ProductInfo] = {}
        self.searches: dict[str, list[ProductInfo]] = {}
        self.calls: list[str] = []
        self.error: Exception | None = None

    async def fetch_many(self, articles):
        self.calls.append("fetch_many")
        if self.error:
            raise self.error
        return [self.products[a] for a in articles if a in self.products]

    async def search(self, query, limit):
        self.calls.append(f"search:{query}")
        return self.searches[query][:limit]

    async def seller(self, seller_id, limit):
        self.calls.append(f"seller:{seller_id}")
        return []


class FakeOzon:
    """Поддельный Ozon: товар запрашивается по ссылке, как у настоящего."""
    def __init__(self):
        self.products: dict[str, ProductInfo] = {}
        self.urls: list[str] = []
        self.searches: dict[str, list[ProductInfo]] = {}
        self.error: Exception | None = None

    async def search(self, query, limit):
        return self.searches[query][:limit]

    async def seller(self, seller_id, limit):
        return []

    async def fetch_many(self, urls):
        self.urls += urls
        if self.error:
            raise self.error
        return [self.products[u.rstrip("/").rsplit("/", 1)[-1]] for u in urls
                if u.rstrip("/").rsplit("/", 1)[-1] in self.products]


def ozon_product(article="777", price=342700) -> ProductInfo:
    return ProductInfo(source="ozon", article=article, name=f"Микшер {article}",
                       url=f"https://www.ozon.ru/product/{article}/", price=price,
                       old_price=None, quantity=1, seller="Ридберг")


@pytest.fixture
def setup(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    run(db.init())
    wb = FakeWB()
    sent: list[tuple[int, str]] = []

    async def notify(user_id, text):
        sent.append((user_id, text))

    checker = Checker(db, {"wb": wb, "ozon": FakeOzon()}, notify, drop_percent=5,
                      search_limit=30, seller_limit=500, pause=(0, 0))
    return db, wb, checker, sent


def test_product_drop_notified_once(setup):
    db, wb, checker, sent = setup
    run(db.add_tracking(111, "product", "wb", "1", "Коврик", target_price=None))
    run(db.save_snapshot([product("1", 300000)], "2026-10-20 10:00:00"))   # точка отсчёта

    wb.products["1"] = product("1", 270000)
    assert run(checker.run()) == 1
    assert sent[0][0] == 111
    assert "Цена снизилась на 10%" in sent[0][1]

    sent.clear()                       # цена та же — второй раз не сообщаем
    assert run(checker.run()) == 0


def test_same_search_requested_once_for_two_users(setup):
    db, wb, checker, sent = setup
    for user in (111, 222):
        tracking_id, _ = run(db.add_tracking(user, "search", "wb", "коврик", "коврик"))
        run(db.update_tracking_items(tracking_id, "wb", ["1", "2"],
                                     confirm_twice=True))                 # точка отсчёта
    run(db.save_snapshot([product("1"), product("2")], "2026-10-20 10:00:00"))

    import time; time.sleep(1.1)
    wb.searches["коврик"] = [product("3", 200000), product("1", 250000), product("2")]
    assert run(checker.run()) == 2                 # по сообщению каждому
    assert wb.calls == ["search:коврик"]           # но WB спросили один раз
    text = sent[0][1]
    assert "📉" in text and "Коврик 1" in text     # снижение цены у товара 1
    assert "🆕" not in text                        # товар 3 пока мог просто «мелькнуть»

    sent.clear()
    time.sleep(1.1)
    run(checker.run())                             # товар 3 продержался вторую проверку
    assert "🆕" in sent[0][1] and "Коврик 3" in sent[0][1]


def test_check_only_own_trackings(setup):
    db, wb, checker, sent = setup
    run(db.add_tracking(111, "product", "wb", "1", "Коврик"))
    run(db.add_tracking(222, "product", "wb", "2", "Скалка"))
    run(db.save_snapshot([product("1"), product("2")], "2026-10-20 10:00:00"))
    wb.products = {"1": product("1", 100000), "2": product("2", 100000)}
    run(checker.run(user_id=111))
    assert [user for user, _ in sent] == [111]


def test_blocked_stops_quietly(setup):
    db, wb, checker, sent = setup
    run(db.add_tracking(111, "product", "wb", "1", "Коврик"))
    wb.error = BlockedError("не пускает")
    assert run(checker.run()) == 0                 # без падения
    assert sent == []


def test_one_failed_request_does_not_stop_others(setup):
    db, wb, checker, sent = setup
    run(db.add_tracking(111, "product", "wb", "1", "Коврик"))
    tracking_id, _ = run(db.add_tracking(111, "search", "wb", "коврик", "коврик"))
    run(db.update_tracking_items(tracking_id, "wb", ["2"]))
    run(db.save_snapshot([product("2", 300000)], "2026-10-20 10:00:00"))
    wb.error = SourceError("сеть")                 # товары не получились…
    wb.searches["коврик"] = [product("2", 200000)]
    assert run(checker.run()) == 1                 # …а поиск проверился
    assert "Поиск «коврик»" in sent[0][1]


def test_ozon_checked_by_url_from_db(setup):
    db, wb, checker, sent = setup
    ozon = checker.sources["ozon"]
    run(db.add_tracking(111, "product", "ozon", "777", "Микшер"))
    run(db.save_snapshot([ozon_product("777", 342700)], "2026-10-20 10:00:00"))
    ozon.products["777"] = ozon_product("777", 300000)
    assert run(checker.run()) == 1
    assert ozon.urls == ["https://www.ozon.ru/product/777/"]   # ссылка взята из базы
    assert sent[0][1].startswith("Ozon:")
    assert "снизилась на 12%" in sent[0][1]


def test_ozon_blocked_does_not_stop_wb(setup):
    db, wb, checker, sent = setup
    checker.sources["ozon"].error = BlockedError("Ozon не пускает")
    run(db.add_tracking(111, "product", "ozon", "777", "Микшер"))
    run(db.add_tracking(111, "product", "wb", "1", "Коврик"))
    run(db.save_snapshot([ozon_product("777"), product("1", 300000)], "2026-10-20 10:00:00"))
    wb.products["1"] = product("1", 200000)
    assert run(checker.run()) == 1                 # WB проверился, хотя Ozon не пустил
    assert sent[0][1].startswith("WB:")


def test_same_article_on_two_marketplaces_not_mixed(setup):
    db, wb, checker, sent = setup
    run(db.add_tracking(111, "product", "wb", "777", "Коврик WB"))
    run(db.add_tracking(111, "product", "ozon", "777", "Микшер Ozon"))
    run(db.save_snapshot([product("777", 100000), ozon_product("777", 342700)],
                         "2026-10-20 10:00:00"))
    wb.products["777"] = product("777", 100000)               # на WB цена та же
    checker.sources["ozon"].products["777"] = ozon_product("777", 300000)   # на Ozon упала
    assert run(checker.run()) == 1
    assert sent[0][1].startswith("Ozon:")


def ozon_tile(article="5", price=300000) -> ProductInfo:
    """Товар из плитки каталога Ozon: цена по карте, источник ozon_card."""
    return ProductInfo(source="ozon_card", article=article, name=f"Коврик {article}",
                       url=f"https://www.ozon.ru/product/{article}/", price=price,
                       old_price=None, quantity=50)


def test_ozon_search_prices_kept_apart_from_product(setup):
    db, wb, checker, sent = setup
    ozon = checker.sources["ozon"]
    # Тот же товар 777: и как отдельный товар (обычная цена), и в поиске (цена по карте)
    run(db.add_tracking(111, "product", "ozon", "777", "Микшер"))
    tracking_id, _ = run(db.add_tracking(111, "search", "ozon", "микшер", "микшер"))
    run(db.save_snapshot([ozon_product("777", 342700), ozon_tile("777", 302300)],
                         "2026-10-20 10:00:00"))
    run(db.update_tracking_items(tracking_id, "ozon_card", ["777"], confirm_twice=True))
    # Цены не изменились — уведомлений быть не должно, хотя по карте дешевле обычной
    ozon.products["777"] = ozon_product("777", 342700)
    ozon.searches["микшер"] = [ozon_tile("777", 302300)]
    assert run(checker.run()) == 0
    # Цена по карте упала — уведомление по поиску Ozon
    ozon.searches["микшер"] = [ozon_tile("777", 250000)]
    assert run(checker.run()) == 1
    assert sent[0][1].startswith("🔍 Поиск «микшер» · Ozon")
