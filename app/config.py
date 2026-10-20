"""Настройки из .env — с проверкой значений, чтобы ошибка в .env была видна сразу."""
import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    bot_token: str
    # Кто может пользоваться ботом. Парсер работает через наш IP и один токен WB:
    # открытый для всех бот могут «заспамить», и WB забанит IP
    allowed_users: frozenset[int]
    db_path: str
    data_dir: str               # сессия WB, профиль браузера
    check_interval_hours: int   # как часто проверять цены
    price_drop_percent: float   # уведомлять, если цена упала больше чем на столько %
    search_limit: int           # сколько товаров брать из поиска
    max_trackings: int          # сколько отслеживаний можно одному пользователю
    wb_dest: str                # регион доставки WB: от него зависят цены и наличие
    timezone: ZoneInfo          # в каком поясе показывать время пользователю


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Заполни {name} в файле .env (пример — в .env.example)")
    return value


def _parse_users(value: str) -> frozenset[int]:
    try:
        users = frozenset(int(part) for part in value.replace(" ", "").split(",") if part)
    except ValueError:
        raise RuntimeError(f"ALLOWED_USERS — ID через запятую, например 123456789,987654321. "
                           f"Сейчас: {value!r}")
    if not users:
        raise RuntimeError("ALLOWED_USERS пустой: укажи хотя бы свой Telegram ID")
    return users


def _parse_int(name: str, default: int, minimum: int) -> int:
    value = os.getenv(name, str(default)).strip()
    try:
        number = int(value)
    except ValueError:
        raise RuntimeError(f"{name} должно быть целым числом, сейчас: {value!r}")
    if number < minimum:
        raise RuntimeError(f"{name} должно быть не меньше {minimum}, сейчас: {number}")
    return number


def _parse_percent(name: str, default: float) -> float:
    value = os.getenv(name, str(default)).strip().replace(",", ".")
    try:
        number = float(value)
    except ValueError:
        raise RuntimeError(f"{name} должно быть числом, сейчас: {value!r}")
    if not 0 < number < 100:
        raise RuntimeError(f"{name} должно быть от 0 до 100, сейчас: {number}")
    return number


def _parse_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise RuntimeError(f"Неизвестный часовой пояс TIMEZONE={name!r}. "
                           "Пример: Asia/Tomsk, Europe/Moscow")


def load_config(use_dotenv: bool = True) -> Config:
    """use_dotenv=False — в тестах, чтобы не подхватить настоящий .env."""
    if use_dotenv:
        load_dotenv()
    return Config(
        bot_token=_require("BOT_TOKEN"),
        allowed_users=_parse_users(_require("ALLOWED_USERS")),
        db_path=os.getenv("DB_PATH", "data/monitor.db"),
        data_dir=os.getenv("DATA_DIR", "data"),
        # Чаще раза в час проверять незачем, а WB лишние запросы не любит
        check_interval_hours=_parse_int("CHECK_INTERVAL_HOURS", 6, minimum=1),
        price_drop_percent=_parse_percent("PRICE_DROP_PERCENT", 5),
        search_limit=_parse_int("SEARCH_LIMIT", 30, minimum=1),
        max_trackings=_parse_int("MAX_TRACKINGS", 20, minimum=1),
        wb_dest=os.getenv("WB_DEST", "1259570991").strip(),
        timezone=_parse_timezone(os.getenv("TIMEZONE", "Asia/Tomsk")),
    )
