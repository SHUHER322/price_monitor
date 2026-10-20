"""Запуск бота: настройки → база → источник WB → бот.

    .venv\\Scripts\\python -m app.main
"""
import asyncio
import logging
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from .access import AccessMiddleware
from .config import load_config
from .db import Database
from .handlers import router
from .sources.wb import WBSource

log = logging.getLogger(__name__)

COMMANDS = [
    BotCommand(command="start", description="Меню"),
    BotCommand(command="list", description="Мои отслеживания"),
    BotCommand(command="cancel", description="Отменить"),
]


async def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Запросы httpx к WB в логах не нужны — их сотни
    logging.getLogger("httpx").setLevel(logging.WARNING)

    config = load_config()
    data_dir = Path(config.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    Path(config.db_path).parent.mkdir(parents=True, exist_ok=True)

    db = Database(config.db_path)
    await db.init()
    wb_source = WBSource(data_dir / "wb_session.json", data_dir / "wb_browser_profile",
                         dest=config.wb_dest)

    bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode="HTML"))
    # Всё, что передано в Dispatcher, aiogram подставит в обработчики по имени аргумента
    dp = Dispatcher(storage=MemoryStorage(), db=db, config=config, wb_source=wb_source)
    access = AccessMiddleware(config.allowed_users)
    dp.message.outer_middleware(access)
    dp.callback_query.outer_middleware(access)
    dp.include_router(router)

    try:
        await bot.set_my_commands(COMMANDS)
    except Exception as e:  # не критично: бот работает и без списка команд
        log.warning("Не удалось задать список команд: %s", e)

    log.info("Бот запущен. Пользователей с доступом: %d", len(config.allowed_users))
    try:
        await dp.start_polling(bot)
    finally:
        # Аккуратно закрыть соединение с Telegram при остановке
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # Ctrl + C — обычная остановка, а не ошибка: печатаем строку вместо трассировки
        print("Бот остановлен.")
