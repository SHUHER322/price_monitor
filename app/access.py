"""Доступ только для пользователей из ALLOWED_USERS.

Middleware («прослойка») — код, который aiogram выполняет перед каждым
обработчиком. Здесь он проверяет, есть ли пользователь в списке, и если нет —
отвечает и дальше обработчики не вызывает.
"""
import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

log = logging.getLogger(__name__)


class AccessMiddleware(BaseMiddleware):
    def __init__(self, allowed_users: frozenset[int]):
        self.allowed_users = allowed_users

    async def __call__(self, handler: Callable[[TelegramObject, dict], Awaitable[Any]],
                       event: TelegramObject, data: dict) -> Any:
        user = getattr(event, "from_user", None)
        if user is not None and user.id in self.allowed_users:
            return await handler(event, data)   # свой — пропускаем дальше

        log.info("Отказ в доступе: id=%s", user.id if user else "?")
        # ID в ответе — чтобы владелец мог добавить человека в ALLOWED_USERS
        text = f"Это приватный бот. Ваш Telegram ID: <code>{user.id if user else '?'}</code>"
        if isinstance(event, Message):
            await event.answer(text)
        elif isinstance(event, CallbackQuery):
            await event.answer("Нет доступа", show_alert=True)
        return None  # обработчик не вызываем
