"""Разведка: есть ли у WB готовая история цены товара (price-history.json).

Файл лежит на серверах картинок и карточек basket-XX.wbbasket.ru. Номер сервера
XX зависит от артикула, поэтому перебираем серверы по очереди, пока не найдём.
Это статические файлы, антибот-токен для них, скорее всего, не нужен — проверим.

Запуск: .venv\\Scripts\\python -m scripts.wb_price_history [артикул ...]
"""
import sys
import time
from datetime import datetime

import httpx

HOSTS = range(1, 41)   # basket-01 … basket-40


def url_for(article: int, host: int, name: str) -> str:
    vol, part = article // 100_000, article // 1_000
    return (f"https://basket-{host:02d}.wbbasket.ru/vol{vol}/part{part}/{article}"
            f"/info/{name}")


def probe(client: httpx.Client, article: int) -> None:
    print(f"\n=== {article}")
    for host in HOSTS:
        # Сначала card.json — так находим «свой» сервер товара
        try:
            card = client.get(url_for(article, host, "ru/card.json"))
        except httpx.HTTPError as e:
            print(f"  basket-{host:02d}: ошибка сети {e.__class__.__name__}")
            continue
        if card.status_code != 200:
            continue
        print(f"  Сервер товара: basket-{host:02d} (card.json → 200)")
        history = client.get(url_for(article, host, "price-history.json"))
        print(f"  price-history.json → {history.status_code}")
        if history.status_code != 200:
            return
        data = history.json()
        print(f"  Записей: {len(data)}. Пример записи: {data[0] if data else '—'}")
        for row in data[-6:]:
            moment = datetime.fromtimestamp(row.get("dt", 0)).strftime("%d.%m.%Y")
            print(f"    {moment}: {row.get('price')}")
        return
    print("  card.json не нашёлся ни на одном сервере basket-01…40")


def main(articles: list[int]) -> None:
    with httpx.Client(timeout=10) as client:
        for article in articles:
            probe(client, article)
            time.sleep(1)


if __name__ == "__main__":
    main([int(a) for a in sys.argv[1:]] or [683146715, 584605000, 309363819])
