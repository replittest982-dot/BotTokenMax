import os
import asyncio
import base64
import json
import logging
import time

# Принудительно задаем путь к браузерам
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/app/pw-browsers"

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)
from playwright.async_api import async_playwright

# ─── Настройка логирования ───────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("MAX-Bot")

TOKEN = os.getenv("BOT_TOKEN", "")
QR_TIMEOUT    = 20_000
LOGIN_TIMEOUT = 90_000
MAX_URL       = "https://web.max.ru"

class LoginFlow(StatesGroup):
    waiting_for_qr_scan = State()

JS_EXTRACTOR = """
() => {
    const auth   = localStorage.getItem('__oneme_auth');
    const device = localStorage.getItem('__oneme_device_id');
    return { auth, device };
}
"""

JS_QR_BASE64 = """
() => {
    const canvas = document.querySelector('canvas');
    if (!canvas) return null;
    return canvas.toDataURL('image/png').split(',')[1];
}
"""

def kb_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_login")]
    ])

def kb_after_session() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Новая сессия", callback_data="new_session")],
        [InlineKeyboardButton(text="ℹ️ Помощь",       callback_data="help")]
    ])

async def grab_session(message: types.Message, state: FSMContext):
    user_id  = message.from_user.id
    username = message.from_user.username or str(user_id)
    log.info(f"[{username}] Starting session grab")

    await message.answer(
        "🚀 <b>Запускаю браузер…</b>\n"
        "Через несколько секунд пришлю QR-код.",
        parse_mode="HTML",
        reply_markup=kb_cancel()
    )

    browser = None
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

            try:
                await page.wait_for_selector("canvas", timeout=QR_TIMEOUT)
            except Exception:
                await message.answer("❌ QR-код не появился. Попробуй ещё раз.")
                return

            qr_b64 = await page.evaluate(JS_QR_BASE64)
            img_bytes = base64.b64decode(qr_b64) if qr_b64 else await page.screenshot()

            await state.set_state(LoginFlow.waiting_for_qr_scan)
            await state.update_data(ts=time.time())

            await message.answer_photo(
                photo=BufferedInputFile(img_bytes, filename="qr.png"),
                caption=f"📱 <b>Отсканируй QR</b>\n⏳ У тебя есть {LOGIN_TIMEOUT // 1000} сек.",
                parse_mode="HTML",
                reply_markup=kb_cancel()
            )

            try:
                await page.wait_for_url("**/messenger**", timeout=LOGIN_TIMEOUT)
            except Exception:
                await state.clear()
                await message.answer("⏰ Время вышло.")
                return

            await asyncio.sleep(2)
            data = await page.evaluate(JS_EXTRACTOR)
            
            if not data.get("auth") or not data.get("device"):
                await message.answer("⚠️ Данные не найдены.")
                return

            transfer_script = (
                f"localStorage.setItem('__oneme_device_id', {json.dumps(data['device'])});\n"
                f"localStorage.setItem('__oneme_auth', {json.dumps(data['auth'])});\n"
                "window.location.reload();"
            )

            await state.clear()
            await message.answer_document(
                document=BufferedInputFile(transfer_script.encode(), filename=f"session_{user_id}.txt"),
                caption="✅ Сессия получена!",
                reply_markup=kb_after_session()
            )

        except Exception as e:
            log.exception("Grab error")
            # ГЛАВНОЕ: Экранируем ошибку для Telegram
            safe_err = str(e).replace("<", "&lt;").replace(">", "&gt;")
            await message.answer(f"💥 <b>Ошибка:</b>\n<code>{safe_err}</code>", parse_mode="HTML")
        finally:
            if browser:
                await browser.close()

# ─── Стандартные хендлеры ───────────────────────────────────────────────────

bot = Bot(token=TOKEN)
dp  = Dispatcher(storage=MemoryStorage())

@dp.message(Command("start"))
async def cmd_start(m: types.Message):
    await m.answer("Напиши /login для входа.")

@dp.message(Command("login"))
async def cmd_login(m: types.Message, state: FSMContext):
    await grab_session(m, state)

@dp.callback_query(F.data == "cancel_login")
async def cb_cancel(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.answer("❌ Отменено.")

if __name__ == "__main__":
    asyncio.run(dp.start_polling(bot))
