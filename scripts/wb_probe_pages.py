"""Разведка: откуда WB берёт списки товаров для поиска и для страницы продавца.

Браузер открывает каждую страницу и записывает фоновые запросы сайта.
Ответы, в которых есть список товаров, сохраняются в data/wb_pages/,
чтобы разобрать их адреса и структуру.

Запуск (из папки проекта):
    .venv\\Scripts\\python scripts\\wb_probe_pages.py
"""
import asyncio
import json
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlsplit

from patchright.async_api import async_playwright

# Магазин продавца коврика и поиск — по одной странице, чтобы не дразнить антибот
PAGES = {
    "seller": "https://www.wildberries.ru/seller/250079242",
    "search": "https://www.wildberries.ru/catalog/0/search.aspx?search="
              + quote("стеклянный коврик для мыши"),
}
DATA_DIR = Path("data")
OUT_DIR = DATA_DIR / "wb_pages"
PROFILE_DIR = DATA_DIR / "wb_browser_profile"  # тот же профиль, что у парсера
WAIT_SECONDS = 25


def count_products(body) -> int:
    """Сколько товаров в ответе (ищем список products на первых уровнях)."""
    if not isinstance(body, dict):
        return 0
    for candidate in (body, body.get("data") or {}):
        if isinstance(candidate, dict) and isinstance(candidate.get("products"), list):
            return len(candidate["products"])
    return 0


async def probe_page(context, name: str, url: str) -> None:
    page = await context.new_page()
    responses = []
    page.on("response", lambda r: responses.append(r)
            if r.request.resource_type in ("xhr", "fetch") else None)

    print(f"\n=== {name}: {url}")
    await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    await asyncio.sleep(WAIT_SECONDS)
    # Прокрутка вниз: часть товаров подгружается только при прокрутке
    for _ in range(3):
        await page.mouse.wheel(0, 3000)
        await asyncio.sleep(3)
    await page.screenshot(path=str(OUT_DIR / f"{name}.png"))

    saved = {}
    for r in responses:
        try:
            body = await r.json()
        except Exception:
            continue
        found = count_products(body)
        parts = urlsplit(r.url)
        params = ", ".join(k for k, _ in parse_qsl(parts.query))
        mark = f"  ← товаров: {found}" if found else ""
        print(f"  {r.status} {parts.netloc}{parts.path}  [{params}]{mark}")
        if found:
            saved[r.url] = body
    (OUT_DIR / f"{name}.json").write_text(
        json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  Ответов со списком товаров: {len(saved)}")
    await page.close()


async def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            channel="chrome",
            headless=False,
            no_viewport=True,
        )
        try:
            for name, url in PAGES.items():
                await probe_page(context, name, url)
                await asyncio.sleep(5)  # пауза между страницами, как у человека
        finally:
            await context.close()
    print(f"\nГотово. Результаты в {OUT_DIR}")


if __name__ == "__main__":
    asyncio.run(main())
