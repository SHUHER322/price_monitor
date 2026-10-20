"""Общее для всех источников (маркетплейсов): формат результата и ошибки."""
from dataclasses import dataclass


@dataclass(frozen=True)
class SizeInfo:
    """Один размер товара (у одежды и обуви их несколько, у техники обычно один)."""
    name: str              # "M", "42", "" — если размера нет
    price: int | None      # цена в копейках; None — размер закончился
    old_price: int | None  # цена до скидки, в копейках
    quantity: int          # сколько штук этого размера в наличии


@dataclass(frozen=True)
class ProductInfo:
    """Что парсер узнал о товаре. Одинаково для WB, Ozon и любых будущих сайтов."""
    source: str            # код источника: "wb", "ozon"
    article: str           # артикул на площадке
    url: str               # ссылка на товар
    name: str
    price: int | None      # самая низкая цена среди размеров, в копейках; None — нет в наличии
    old_price: int | None  # цена до скидки у того же размера, в копейках
    quantity: int          # сколько штук в наличии всего (0 — нет в наличии)
    sizes: tuple[SizeInfo, ...] = ()  # по размерам; кортеж, т.к. объект неизменяемый

    @property
    def in_stock(self) -> bool:
        return self.quantity > 0

    @property
    def has_sizes(self) -> bool:
        """Есть ли настоящие размеры (а не один безымянный, как у техники)."""
        return any(s.name for s in self.sizes)


class SourceError(Exception):
    """Не удалось получить товар: неверная ссылка, товар удалён, ответ не разобран."""


class BlockedError(SourceError):
    """Площадка не пускает даже после обновления антибот-токена."""


def format_price(kopecks: int | None) -> str:
    """276300 → '2 763 ₽'. Деньги храним в копейках (целых числах): у дробных
    чисел бывают ошибки округления, например 0.1 + 0.2 = 0.30000000000000004."""
    if kopecks is None:
        return "—"
    rubles = kopecks // 100
    return f"{rubles:,}".replace(",", " ") + " ₽"
