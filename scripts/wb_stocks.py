"""Разведка: из чего складывается остаток товара WB (по складам и размерам).

Запуск: .venv\\Scripts\\python scripts\\wb_stocks.py <артикул> [<артикул> ...]
"""
import asyncio
import sys
from pathlib import Path

from app.sources.wb import CARD_DETAIL, PRODUCT_PAGE, WBSource


async def main(articles: list[str]) -> None:
    source = WBSource(Path("data/wb_session.json"), Path("data/wb_browser_profile"))
    for article in articles:
        data = await source._get_json(CARD_DETAIL, source._params(nm=article),
                                      PRODUCT_PAGE.format(article=article))
        product = data["products"][0]
        print(f"\n{article} {product.get('name')}")
        print(f"  totalQuantity: {product.get('totalQuantity')}")
        for size in product.get("sizes", []):
            stocks = size.get("stocks", [])
            qty = [s.get("qty") for s in stocks]
            print(f"  размер {size.get('name') or size.get('origName')!r}: "
                  f"складов {len(stocks)}, по складам {qty}, сумма {sum(q or 0 for q in qty)}")
        await asyncio.sleep(1.5)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:] or ["309363819", "1404012653", "584605000"]))
