import asyncio
import json
import os
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from playwright.async_api import async_playwright

TOKEN = "ВАШ_ТЕЛЕГРАМ_ТОКЕН"
bot = Bot(token=TOKEN)
dp = Dispatcher()

# JS-скрипт для извлечения данных в твоем формате
JS_EXTRACTOR = """
() => {
    let auth = localStorage.getItem('__oneme_auth');
    let device = localStorage.getItem('__oneme_device_id');
    return { auth, device };
}
"""

async def capture_max_session(message: types.Message):
    async with async_playwright() as p:
        # Запускаем браузер (в Docker используем headless=True)
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        await page.goto("https://web.max.ru") # Уточни точный URL (ru или com)

        # 1. Ждем появления QR-кода на странице
        try:
            await page.wait_for_selector("canvas", timeout=15000)
            qr_screenshot = await page.screenshot()
            await message.answer_photo(
                photo=BufferedInputFile(qr_screenshot, filename="login_qr.png"),
                caption="🚀 **QR-код готов!**\nОтсканируй его в приложении MAX. У тебя есть 60 секунд."
            )
        except Exception:
            await message.answer("❌ Не удалось найти QR-код. Попробуй еще раз.")
            await browser.close()
            return

        # 2. Ждем авторизации (ждем, пока URL изменится на внутреннюю страницу)
        try:
            await page.wait_for_url("**/messenger**", timeout=60000)
            await asyncio.sleep(2) # Даем прогрузиться localStorage

            # 3. Вытаскиваем данные
            data = await page.evaluate(JS_EXTRACTOR)
            auth_raw = data.get('auth')
            device_id = data.get('device')

            if auth_raw and device_id:
                auth_data = json.loads(auth_raw)
                token = auth_data.get('token', '')

                # Проверяем, начинается ли на An
                status_token = "✅ Токен валиден (начинается на An)" if token.startswith("An") else "⚠️ Токен не начинается на An, проверь данные"

                # Форматируем строку для переноса, как ты просил
                transfer_script = (
                    f"sessionStorage.clear();localStorage.clear();\n"
                    f"localStorage.setItem('__oneme_device_id', \"{device_id}\");\n"
                    f"localStorage.setItem('__oneme_auth', JSON.stringify({auth_raw}));\n"
                    f"window.location.reload();"
                )

                # 4. Создаем файл token.txt в памяти
                file_content = transfer_script.encode('utf-8')
                text_file = BufferedInputFile(file_content, filename=f"token_{message.from_user.id}.txt")

                await message.answer_document(
                    document=text_file,
                    caption=f"{status_token}\n\nФайл с кодом переноса готов. Используй его в консоли (F12)."
                )
            else:
                await message.answer("❌ Авторизация прошла, но данные в localStorage не найдены.")

        except Exception as e:
            await message.answer(f"❌ Ошибка ожидания входа или парсинга: {e}")
        
        await browser.close()

@dp.message(Command("login"))
async def start_login(message: types.Message):
    await message.answer("Запускаю браузер для входа в MAX...")
    await capture_max_session(message)

if __name__ == "__main__":
    asyncio.run(dp.start_polling(bot))
