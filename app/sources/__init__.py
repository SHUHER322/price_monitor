"""Источники данных — маркетплейсы. Каждый — отдельный модуль с общим форматом
результата (ProductInfo в base.py): поломка одного не затрагивает другие."""


def detect(url: str) -> str | None:
    """По ссылке на товар понять маркетплейс: 'wb', 'ozon' или None."""
    # Импорт внутри функции: иначе при ручном запуске `python -m app.sources.wb`
    # модуль загружался бы дважды и Python выдавал бы RuntimeWarning
    from . import ozon, wb

    if wb.matches(url):
        return "wb"
    if ozon.matches(url):
        return "ozon"
    return None
