"""Разведка Ozon, шаг 2 для списков: листание по nextPage и цена в плитке.

1. Для поиска и магазина идём по цепочке nextPage (так страница подгружает
   товары при прокрутке): сколько товаров приходит, нет ли повторов.
2. Ищем в магазине товар COMPARE_SKU и сравниваем цену в плитке с ценами
   на странице товара (обычная / по карте) — какую цену показывает список.

Запуск (из папки проекта):
    .venv\\Scripts\\python -m scripts.ozon_probe_pages
"""
import asyncio
import builtins
import json
from pathlib import Path
from urllib.parse import quote

from patchright.async_api import async_playwright

DATA_DIR = Path("data")
PROFILE_DIR = DATA_DIR / "ozon_browser_profile"
API = "https://www.ozon.ru/api/entrypoint-api.bx/page/json/v2?url="
STEPS = 6                      # сколько раз пройти по nextPage внутри страницы
PAGES = 3                      # сколько страниц (&page=N) перебрать
LOG_FILE = DATA_DIR / "ozon_lists" / "walk_log.txt"
_log_lines: list[str] = []


def print(*args) -> None:   # noqa: A001 — печать и в консоль, и в файл
    line = " ".join(str(a) for a in args)
    _log_lines.append(line)
    builtins.print(line)
COMPARE_SKU = "1291633069"     # белый микшер: на странице товара 3 427 ₽, по карте 3 098 ₽
START = {
    "Поиск": "/search/?text=" + quote("стеклянный коврик для мыши"),
    "Магазин": "/seller/ridberg/",
}


async def api(page, path: str) -> dict:
    result = await page.evaluate(
        """async (url) => {
            const r = await fetch(url, {credentials: "include"});
            return {status: r.status, text: await r.text()};
        }""", API + quote(path, safe="/?=&%"))
    if result["status"] != 200:
        raise RuntimeError(f"ответ {result['status']}")
    return json.loads(result["text"])


def widgets(data: dict, prefix: str) -> list[dict]:
    found = []
    for key, value in data.get("widgetStates", {}).items():
        if key.startswith(prefix):
            found.append(json.loads(value) if isinstance(value, str) else value)
    return found


def tile_prices(tile: dict) -> list[str]:
    for state in tile.get("mainState", []):
        if state.get("type") == "priceV2":
            return [f"{p.get('text')} ({p.get('textStyle')})"
                    for p in state["priceV2"].get("price", [])]
    return []


async def walk(page, name: str, start: str) -> dict[str, dict]:
    print(f"\n=== {name}")
    tiles: dict[str, dict] = {}
    for number in range(1, PAGES + 1):
        sep = "&" if "?" in start else "?"
        path = start if number == 1 else f"{start}{sep}page={number}"
        print(f" страница {number}:")
        before = len(tiles)
        await walk_page(page, path, tiles)
        print(f"  → новых за страницу: {len(tiles) - before}")
        await asyncio.sleep(2.5)
    return tiles


async def walk_page(page, path: str, tiles: dict[str, dict]) -> None:
    for step in range(1, STEPS + 1):
        data = await api(page, path)
        items = [t for grid in widgets(data, "tileGrid") for t in grid.get("items", [])]
        new = [t for t in items if str(t.get("sku") or t.get("id")) not in tiles]
        for t in items:
            tiles.setdefault(str(t.get("sku") or t.get("id")), t)
        pager = (widgets(data, "infiniteVirtualPaginator") or [{}])[0]
        print(f"  шаг {step}: товаров {len(items)}, новых {len(new)}, всего {len(tiles)}; "
              f"nextPage: {'есть' if pager.get('nextPage') else 'НЕТ'}")
        if not pager.get("nextPage"):
            break
        path = pager["nextPage"]
        await asyncio.sleep(2.5)


async def main() -> None:
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR), channel="chrome", headless=False,
            no_viewport=True)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            print("Открываю Ozon (окно не трогай, около минуты)...")
            await page.goto("https://www.ozon.ru/", wait_until="domcontentloaded",
                            timeout=60_000)
            await asyncio.sleep(5)
            results = {}
            for name, path in START.items():
                results[name] = await walk(page, name, path)
        finally:
            await context.close()

    seller_tiles = results["Магазин"]
    print(f"\n=== Цена в плитке у {COMPARE_SKU}")
    if COMPARE_SKU in seller_tiles:
        print(f"  в плитке: {tile_prices(seller_tiles[COMPARE_SKU])}")
        print("  на странице товара было: обычная 3 427 ₽, по карте 3 098 ₽, до скидки 10 990 ₽")
    else:
        print("  в просмотренных страницах магазина его нет — пришли вывод, возьмём другой")
        first = next(iter(seller_tiles.items()), None)
        if first:
            print(f"  для сравнения первый товар магазина {first[0]}: {tile_prices(first[1])}")

    out = DATA_DIR / "ozon_lists" / "walk.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({k: list(v.values()) for k, v in results.items()},
                              ensure_ascii=False), encoding="utf-8")
    print(f"\nПлитки сохранены в {out}")
    LOG_FILE.write_text("\n".join(_log_lines), encoding="utf-8")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        # Вывод сохраняем, даже если что-то упало, — чтобы его можно было разобрать
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        LOG_FILE.write_text("\n".join(_log_lines), encoding="utf-8")
