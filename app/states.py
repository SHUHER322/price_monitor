"""Шаги диалогов (машина состояний): что бот ждёт от пользователя сейчас."""
from aiogram.fsm.state import State, StatesGroup


class AddProduct(StatesGroup):
    link = State()    # ждём ссылку на товар
    price = State()   # ждём желаемую цену (или «Пропустить»)


class AddSeller(StatesGroup):
    link = State()    # ждём ссылку на магазин


class AddSearch(StatesGroup):
    market = State()  # ждём выбор маркетплейса: Wildberries или Ozon
    query = State()   # ждём поисковый запрос


class EditTarget(StatesGroup):
    price = State()   # ждём новую желаемую цену товара
