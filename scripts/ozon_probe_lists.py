"""Разведка Ozon: как получить список товаров поиска и магазина.

Тем же способом, что работает для товара (fetch изнутри Chrome), запрашиваем
API страницы поиска и страницы продавца, первую и вторую страницы. Сохраняем
ответы и печатаем, в каких виджетах лежат товары и есть ли ссылка на следующую
страницу.

Запуск (из папки проекта):
    .venv\\Scripts\\python -m scripts.ozon_probe_lists
    .venv\\Scripts\\python -m scripts.ozon_probe_lists "<запрос>" <продавец из ссылки /seller/…/>
"""
import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import quote

from patchright.async_api import async_playwright

DATA_DIR = Path("data")
PROFILE_DIR = DATA_DIR / "ozon_browser_profile"
OUT_DIR = DATA_DIR / "ozon_lists"
API = "https://www.ozon.ru/api/entrypoint-api.bx/page/json/v2?url="


async def api(page, path: str) -> tuple[int, str]:
    result = await page.evaluate(
        """async (url) => {
            const r = await fetch(url, {credentials: "include"});
            return {status: r.status, text: await r.text()};
        }""", API + quote(path, safe="/?=&"))
    return result["status"], result["text"]


def describe(name: str, status: int, text: str) -> None:
    print(f"\n=== {name}: ответ {status}")
    if status != 200:
        print(text[:300])
        return
    data = json.loads(text)
    print(f"Ключи верхнего уровня: {', '.join(data)}")
    for key in ("nextPage", "prevPage", "pageInfo"):
        if key in data:
            print(f"{key}: {str(data[key])[:300]}")
    states = data.get("widgetStates", {})
    print(f"Виджетов: {len(states)}")
    for key, value in states.items():
        try:
            state = json.loads(value) if isinstance(value, str) else value
        except ValueError:
            continue
        items = state.get("items") if isinstance(state, dict) else None
        if isinstance(items, list) and items:
            print(f"  {key}: items = {len(items)}")
            print(f"    пример товара: {json.dumps(items[0], ensure_ascii=False)[:900]}")


async def main(query: str, seller: str) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    paths = {
        "search_1": f"/search/?text={quote(query)}",
        "search_2": f"/search/?text={quote(query)}&page=2",
        "seller_1": f"/seller/{seller}/",
        "seller_2": f"/seller/{seller}/?page=2",
    }
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR), channel="chrome", headless=False,
            no_viewport=True)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            print("Открываю Ozon (окно не трогай)...")
            await page.goto("https://www.ozon.ru/", wait_until="domcontentloaded",
                            timeout=60_000)
            await asyncio.sleep(5)
            for name, path in paths.items():
                status, text = await api(page, path)
                (OUT_DIR / f"{name}.json").write_text(text, encoding="utf-8")
                describe(name, status, text)
                await asyncio.sleep(2.5)
        finally:
            await context.close()
    print(f"\nОтветы сохранены в {OUT_DIR}")


if __name__ == "__main__":
    query = sys.argv[1] if len(sys.argv) > 1 else "стеклянный коврик для мыши"
    seller = sys.argv[2] if len(sys.argv) > 2 else "ridberg"
    asyncio.run(main(query, seller))
