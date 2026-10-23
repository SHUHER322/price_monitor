"""Тесты настроек: правильные значения читаются, ошибки в .env видны сразу."""
import pytest

from app.config import load_config

ENV_NAMES = ["BOT_TOKEN", "ALLOWED_USERS", "DB_PATH", "DATA_DIR", "CHECK_INTERVAL_HOURS",
             "PRICE_DROP_PERCENT", "SEARCH_LIMIT", "SELLER_LIMIT", "MAX_TRACKINGS", "WB_DEST", "TIMEZONE",
             "OZON_PROXY"]


@pytest.fixture
def env(monkeypatch):
    """Чистое окружение с минимально нужными настройками.
    monkeypatch — инструмент pytest: временно меняет переменные окружения
    и сам возвращает всё как было после теста."""
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BOT_TOKEN", "123:abc")
    monkeypatch.setenv("ALLOWED_USERS", "111, 222")
    return monkeypatch


def test_defaults(env):
    config = load_config(use_dotenv=False)
    assert config.allowed_users == {111, 222}
    assert config.check_interval_hours == 6
    assert config.price_drop_percent == 5
    assert config.search_limit == 30
    assert config.seller_limit == 500
    assert config.max_trackings == 20
    assert str(config.timezone) == "Asia/Tomsk"


def test_custom_values(env):
    env.setenv("CHECK_INTERVAL_HOURS", "2")
    env.setenv("PRICE_DROP_PERCENT", "7,5")   # запятая тоже подойдёт
    env.setenv("TIMEZONE", "Europe/Moscow")
    config = load_config(use_dotenv=False)
    assert config.check_interval_hours == 2
    assert config.price_drop_percent == 7.5
    assert str(config.timezone) == "Europe/Moscow"


@pytest.mark.parametrize("name", ["BOT_TOKEN", "ALLOWED_USERS"])
def test_required(env, name):
    env.delenv(name)
    with pytest.raises(RuntimeError, match=name):
        load_config(use_dotenv=False)


@pytest.mark.parametrize("name, value", [
    ("ALLOWED_USERS", "мой id"),
    ("CHECK_INTERVAL_HOURS", "0"),      # чаще раза в час нельзя
    ("CHECK_INTERVAL_HOURS", "шесть"),
    ("PRICE_DROP_PERCENT", "150"),
    ("SEARCH_LIMIT", "-1"),
    ("TIMEZONE", "Tomsk"),
])
def test_bad_values(env, name, value):
    env.setenv(name, value)
    with pytest.raises(RuntimeError, match=name):
        load_config(use_dotenv=False)


def test_no_ozon_proxy_by_default(env):
    assert load_config(use_dotenv=False).ozon_proxy is None
    env.setenv("OZON_PROXY", "   ")
    assert load_config(use_dotenv=False).ozon_proxy is None


def test_ozon_proxy_with_login(env):
    env.setenv("OZON_PROXY", "http://user:p%40ss@1.2.3.4:8000")
    assert load_config(use_dotenv=False).ozon_proxy == {
        "server": "http://1.2.3.4:8000", "username": "user", "password": "p@ss"}
    env.setenv("OZON_PROXY", "5.6.7.8:3128")           # без схемы — считаем http
    assert load_config(use_dotenv=False).ozon_proxy == {"server": "http://5.6.7.8:3128"}


@pytest.mark.parametrize("value", ["1.2.3.4", "ftp://1.2.3.4:21", "http://1.2.3.4:port",
                                   "socks5://u:p@1.2.3.4:1080"])
def test_bad_ozon_proxy(env, value):
    env.setenv("OZON_PROXY", value)
    with pytest.raises(RuntimeError, match="OZON_PROXY"):
        load_config(use_dotenv=False)
