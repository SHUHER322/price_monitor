"""Разведка: почему Ozon не пускает с сервера (403).

Открывает главную Ozon и минуту каждые 5 секунд печатает, что на странице
(заголовок, начало текста) и что отвечает API товара. По этому видно:
- проверка «вы не робот» проходит сама за N секунд → боту надо ждать дольше;
- страница «Доступ ограничен» не уходит → Ozon режет IP сервера.

Запуск на сервере (от root, бот остановлен):
    cd /home/monitor/price_monitor && sudo -u monitor -H xvfb-run -a .venv/bin/python -m scripts.ozon_server_probe
"""
import asyncio
from pathlib import Path

from patchright.async_api import async_playwright

PROFILE_DIR = Path("data/ozon_browser_profile")
API = "https://www.ozon.ru/api/entrypoint-api.bx/page/json/v2?url=/product/1291633069/"
STEPS, PAUSE = 12, 5


async def main() -> None:
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR), channel="chrome", headless=False,
            no_viewport=True)
        page = context.pages[0] if context.pages else await context.new_page()
        try:
            response = await page.goto("https://www.ozon.ru/", wait_until="domcontentloaded",
                                       timeout=60_000)
            print(f"Главная: ответ {response.status if response else '?'}")
            for step in range(1, STEPS + 1):
                await asyncio.sleep(PAUSE)
                title = await page.title()
                text = await page.evaluate("document.body ? document.body.innerText : ''")
                api = await page.evaluate(
                    "async (u) => (await fetch(u, {credentials: 'include'})).status", API)
                snippet = " ".join(text.split())[:150]
                print(f"{step * PAUSE:>3} с | API {api} | {page.url[:50]} | {title[:40]!r} | {snippet}")
                if api == 200:
                    print("API пускает — значит, боту нужно просто дольше ждать после открытия.")
                    break
            else:
                print("За минуту API так и не пустил.")
            cookies = await context.cookies("https://www.ozon.ru")
            print("Cookie:", ", ".join(sorted(c["name"] for c in cookies)) or "нет")
        finally:
            await context.close()


if __name__ == "__main__":
    asyncio.run(main())
