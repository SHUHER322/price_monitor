"""Запуск бота: настройки → база → источник WB → бот.

    .venv\\Scripts\\python -m app.main
"""
import asyncio
import logging
from datetime import datetime, timedelta
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .access import AccessMiddleware
from .checker import Checker
from .config import load_config
from .db import Database
from .handlers import router
from .sources.wb import WBSource

log = logging.getLogger(__name__)

COMMANDS = [
    BotCommand(command="start", description="Меню"),
    BotCommand(command="list", description="Мои отслеживания"),
    BotCommand(command="check", description="Проверить цены сейчас"),
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

    # link_preview_is_disabled — без огромных превью ссылок в уведомлениях
    bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode="HTML",
                                                             link_preview_is_disabled=True))

    async def notify(user_id: int, text: str) -> None:
        """Отправить уведомление. Ошибка одному пользователю не мешает остальным."""
        try:
            await bot.send_message(user_id, text)
        except TelegramForbiddenError:
            log.warning("Пользователь %s заблокировал бота", user_id)
        except TelegramAPIError as e:
            log.warning("Не отправилось %s: %s", user_id, e)
        await asyncio.sleep(0.1)  # Telegram ограничивает частоту сообщений

    checker = Checker(db, wb_source, notify, drop_percent=config.price_drop_percent,
                      search_limit=config.search_limit, seller_limit=config.seller_limit)

    async def scheduled_check() -> None:
        try:
            await checker.run()
        except Exception:
            # Любая неожиданная ошибка — в лог, а бот и расписание продолжают работать
            log.exception("Проверка по расписанию упала")

    # Расписание: первая проверка через минуту после запуска, дальше каждые N часов.
    # coalesce — если компьютер спал и пропустил несколько запусков, сделать один
    scheduler = AsyncIOScheduler()
    scheduler.add_job(scheduled_check, "interval", hours=config.check_interval_hours,
                      next_run_time=datetime.now() + timedelta(minutes=1),
                      max_instances=1, coalesce=True, misfire_grace_time=3600)

    # Всё, что передано в Dispatcher, aiogram подставит в обработчики по имени аргумента
    dp = Dispatcher(storage=MemoryStorage(), db=db, config=config, wb_source=wb_source,
                    checker=checker)
    access = AccessMiddleware(config.allowed_users)
    dp.message.outer_middleware(access)
    dp.callback_query.outer_middleware(access)
    dp.include_router(router)

    try:
        await bot.set_my_commands(COMMANDS)
    except Exception as e:  # не критично: бот работает и без списка команд
        log.warning("Не удалось задать список команд: %s", e)

    scheduler.start()
    log.info("Бот запущен. Пользователей с доступом: %d. Проверка цен каждые %d ч.",
             len(config.allowed_users), config.check_interval_hours)
    try:
        await dp.start_polling(bot)
    finally:
        # Аккуратно остановить расписание и закрыть соединение с Telegram
        scheduler.shutdown(wait=False)
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # Ctrl + C — обычная остановка, а не ошибка: печатаем строку вместо трассировки
        print("Бот остановлен.")
