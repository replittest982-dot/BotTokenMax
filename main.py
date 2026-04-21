import os
import asyncio
import logging
import time
import base64
import json
from dotenv import load_dotenv

# Загружаем переменные из .env
load_dotenv()

# Если ты на Aeza и ставил через playwright install --with-deps, 
# строку ниже можно закомментировать. Если по моему тутору с путями — оставляй.
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/app/pw-browsers"

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("MAX-Bot")

TOKEN = os.getenv("BOT_TOKEN", "")
if not TOKEN:
    raise RuntimeError("❌ Переменная окружения BOT_TOKEN не задана в .env!")

MAX_URL = "https://web.max.ru"

# ─── Глобальные переменные для передачи 2FA ──────────────────────────────────
user_events = {}
user_passwords = {}

class LoginFlow(StatesGroup):
    waiting_for_qr_scan = State()
    waiting_for_2fa = State()

def kb_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_login")]
    ])

# ─── Основная логика Playwright ──────────────────────────────────────────────

async def grab_session(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    await message.answer("🚀 <b>Запускаю браузер...</b>", parse_mode="HTML")

    browser = None  # Важно: инициализируем до блока try
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            context = await browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
            page = await context.new_page()
            await page.goto(MAX_URL, wait_until="domcontentloaded")

            # 1. Ждем QR код
            try:
                await page.wait_for_selector("canvas", timeout=20000)
            except Exception:
                await message.answer("❌ QR-код не загрузился.")
                return

            qr_b64 = await page.evaluate("() => { const c = document.querySelector('canvas'); return c ? c.toDataURL('image/png').split(',')[1] : null; }")
            
            img_bytes = base64.b64decode(qr_b64) if qr_b64 else await page.screenshot()

            await state.set_state(LoginFlow.waiting_for_qr_scan)
            await message.answer_photo(
                photo=BufferedInputFile(img_bytes, filename="qr.png"),
                caption="📱 <b>Отсканируй QR</b>\nЖду авторизации...",
                parse_mode="HTML",
                reply_markup=kb_cancel()
            )

            # 2. Ждем входа ИЛИ запроса 2FA/Пароля
            start_time = time.time()
            auth_success = False

            while time.time() - start_time < 90:
                if "/messenger" in page.url:
                    auth_success = True
                    break
                
                # Проверка на наличие поля пароля
                password_input = page.locator("input[type='password']")
                if await password_input.is_visible():
                    await message.answer("🔒 <b>Сайт запросил пароль или 2FA код!</b>\nОтправь его прямо сюда в чат:", parse_mode="HTML")
                    await state.set_state(LoginFlow.waiting_for_2fa)
                    
                    user_events[user_id] = asyncio.Event()
                    await user_events[user_id].wait() 
                    
                    pwd = user_passwords.get(user_id, "")
                    await password_input.fill(pwd)
                    
                    # Попытка нажать на кнопку входа (замени селектор если нужно)
                    submit_btn = page.locator("button[type='submit']")
                    if await submit_btn.is_visible():
                        await submit_btn.click()
                    else:
                        await page.keyboard.press("Enter")
                    
                    user_events.pop(user_id, None)
                    user_passwords.pop(user_id, None)
                    
                    await message.answer("Проверяю код...")
                    await asyncio.sleep(5)
                
                await asyncio.sleep(1)

            if not auth_success:
                await state.clear()
                await message.answer("⏰ Время вышло или не удалось войти.")
                return

            await asyncio.sleep(2)

            # 3. Извлекаем данные
            data = await page.evaluate("""
                () => {
                    return {
                        auth: localStorage.getItem('__oneme_auth'),
                        device: localStorage.getItem('__oneme_device_id')
                    };
                }
            """)

            if not data.get('auth') or not data.get('device'):
                await message.answer("⚠️ Вход выполнен, но данные в localStorage не найдены.")
                return

            # 4. Формируем твой скрипт
            device_str = data['device']
            auth_str = data['auth']

            transfer_script = (
                "sessionStorage.clear();\n"
                "localStorage.clear();\n"
                f"localStorage.setItem('__oneme_device_id', \"{device_str}\");\n"
                f"localStorage.setItem('__oneme_auth', '{auth_str}');\n"
                "window.location.reload();"
            )

            await state.clear()
            await message.answer_document(
                document=BufferedInputFile(transfer_script.encode('utf-8'), filename=f"session_{user_id}.txt"),
                caption="✅ <b>Сессия успешно сохранена!</b>\n\nСкопируй содержимое файла в консоль (F12).",
                parse_mode="HTML"
            )

        except Exception as e:
            log.exception("Ошибка в grab_session")
            safe_err = str(e).replace("<", "&lt;").replace(">", "&gt;")
            await message.answer(f"💥 <b>Ошибка:</b>\n<code>{safe_err}</code>", parse_mode="HTML")
        finally:
            if browser:
                await browser.close()

# ─── Хендлеры ────────────────────────────────────────────────────────────────

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

@dp.message(Command("start", "login"))
async def cmd_login(message: types.Message, state: FSMContext):
    await grab_session(message, state)

@dp.callback_query(F.data == "cancel_login")
async def cb_cancel(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    user_events.pop(callback.from_user.id, None)
    await callback.message.answer("❌ Отменено.")
    await callback.answer()

@dp.message(LoginFlow.waiting_for_2fa)
async def process_2fa(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    user_passwords[user_id] = message.text
    if user_id in user_events:
        user_events[user_id].set()
    await state.set_state(LoginFlow.waiting_for_qr_scan)

if __name__ == "__main__":
    log.info("Бот запущен...")
    asyncio.run(dp.start_polling(bot))
