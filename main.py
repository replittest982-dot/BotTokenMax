import asyncio
import json
import logging
import os
import time
from io import BytesIO

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

# ─── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("MAX-Bot")

# ─── Config ──────────────────────────────────────────────────────────────────
TOKEN = os.getenv("BOT_TOKEN", "")
if not TOKEN:
    raise RuntimeError("❌ Переменная окружения BOT_TOKEN не задана!")

QR_TIMEOUT = 20_000   # мс — ждём появления QR
LOGIN_TIMEOUT = 90_000  # мс — ждём авторизации

MAX_URL = "https://web.max.ru"

# ─── FSM ─────────────────────────────────────────────────────────────────────
class LoginFlow(StatesGroup):
    waiting_for_qr_scan = State()


# ─── JS ──────────────────────────────────────────────────────────────────────
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

# ─── Keyboards ───────────────────────────────────────────────────────────────
def kb_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_login")]
    ])

def kb_after_session() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Новая сессия", callback_data="new_session")],
        [InlineKeyboardButton(text="ℹ️ Помощь", callback_data="help")]
    ])

# ─── Core logic ──────────────────────────────────────────────────────────────
async def grab_session(message: types.Message, state: FSMContext):
    user_id  = message.from_user.id
    username = message.from_user.username or str(user_id)
    log.info(f"[{username}] Starting session grab")

    await message.answer(
        "🚀 <b>Запускаю браузер…</b>\n"
        "Через несколько секунд пришлю QR-код для входа в MAX.",
        parse_mode="HTML",
        reply_markup=kb_cancel()
    )

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = await context.new_page()

        try:
            await page.goto(MAX_URL, wait_until="domcontentloaded")

            # ── Шаг 1: QR-код ────────────────────────────────────────────────
            try:
                await page.wait_for_selector("canvas", timeout=QR_TIMEOUT)
            except Exception:
                await message.answer("❌ QR-код не появился. Попробуй <code>/login</code> ещё раз.", parse_mode="HTML")
                return

            # Сначала пробуем взять base64 прямо с canvas
            qr_b64 = await page.evaluate(JS_QR_BASE64)
            if qr_b64:
                img_bytes = __import__("base64").b64decode(qr_b64)
            else:
                img_bytes = await page.screenshot(full_page=False)

            await state.set_state(LoginFlow.waiting_for_qr_scan)
            await state.update_data(ts=time.time())

            await message.answer_photo(
                photo=BufferedInputFile(img_bytes, filename="qr.png"),
                caption=(
                    "📱 <b>Отсканируй QR в приложении MAX</b>\n\n"
                    f"⏳ У тебя есть <b>{LOGIN_TIMEOUT // 1000} секунд</b>.\n"
                    "После входа бот автоматически заберёт сессию."
                ),
                parse_mode="HTML",
                reply_markup=kb_cancel()
            )

            log.info(f"[{username}] QR sent, waiting for auth…")

            # ── Шаг 2: Ждём авторизации ──────────────────────────────────────
            try:
                await page.wait_for_url("**/messenger**", timeout=LOGIN_TIMEOUT)
            except Exception:
                await state.clear()
                await message.answer(
                    "⏰ <b>Время вышло.</b> Ты не успел войти.\n"
                    "Запусти <code>/login</code> заново.",
                    parse_mode="HTML"
                )
                return

            await asyncio.sleep(2)  # localStorage подгружается

            # ── Шаг 3: Читаем localStorage ────────────────────────────────────
            data      = await page.evaluate(JS_EXTRACTOR)
            auth_raw  = data.get("auth")
            device_id = data.get("device")

            if not auth_raw or not device_id:
                await message.answer(
                    "⚠️ Вход выполнен, но данные сессии не найдены в localStorage.\n"
                    "Попробуй <code>/login</code> ещё раз.",
                    parse_mode="HTML"
                )
                return

            # ── Шаг 4: Парсим и формируем скрипт ─────────────────────────────
            try:
                auth_obj = json.loads(auth_raw)
            except json.JSONDecodeError:
                await message.answer("❌ Не удалось распарсить данные сессии. Попробуй ещё раз.")
                return

            token    = auth_obj.get("token", "")
            is_valid = token.startswith("An")

            transfer_script = (
                "sessionStorage.clear();\n"
                "localStorage.clear();\n"
                f"localStorage.setItem('__oneme_device_id', {json.dumps(device_id)});\n"
                f"localStorage.setItem('__oneme_auth', {json.dumps(auth_raw)});\n"
                "window.location.reload();"
            )

            file_bytes = transfer_script.encode("utf-8")
            doc = BufferedInputFile(file_bytes, filename=f"session_{user_id}.txt")

            status_icon = "✅" if is_valid else "⚠️"
            status_text = (
                "Токен валиден (начинается на <code>An</code>)"
                if is_valid else
                "Токен не начинается на <code>An</code> — проверь данные"
            )

            await state.clear()
            await message.answer_document(
                document=doc,
                caption=(
                    f"{status_icon} <b>{status_text}</b>\n\n"
                    "📄 В файле — скрипт для вставки в консоль (<code>F12 → Console</code>).\n"
                    "Он перенесёт сессию в любой браузер."
                ),
                parse_mode="HTML",
                reply_markup=kb_after_session()
            )
            log.info(f"[{username}] Session grabbed successfully. Token valid: {is_valid}")

        except Exception as e:
            log.exception(f"[{username}] Unexpected error: {e}")
            await state.clear()
            await message.answer(
                f"💥 <b>Неожиданная ошибка:</b>\n<code>{e}</code>",
                parse_mode="HTML"
            )
        finally:
            await browser.close()


# ─── Bot init ────────────────────────────────────────────────────────────────
bot = Bot(token=TOKEN)
dp  = Dispatcher(storage=MemoryStorage())


# ─── Handlers ────────────────────────────────────────────────────────────────
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 <b>MAX Session Grabber</b>\n\n"
        "Команды:\n"
        "• /login — получить сессию через QR\n"
        "• /help — помощь\n",
        parse_mode="HTML"
    )

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(
        "ℹ️ <b>Как пользоваться:</b>\n\n"
        "1. Отправь <code>/login</code>\n"
        "2. Отсканируй QR в приложении MAX\n"
        "3. Получи файл <code>session_*.txt</code>\n"
        "4. Открой нужный сайт MAX в браузере\n"
        "5. Нажми <b>F12 → Console</b>, вставь содержимое файла и нажми Enter\n\n"
        "⚠️ Не передавай файл сессии посторонним!",
        parse_mode="HTML"
    )

@dp.message(Command("login"))
async def cmd_login(message: types.Message, state: FSMContext):
    current = await state.get_state()
    if current == LoginFlow.waiting_for_qr_scan:
        data = await state.get_data()
        elapsed = int(time.time() - data.get("ts", 0))
        await message.answer(
            f"⏳ Уже идёт процесс входа ({elapsed}с назад). "
            "Дождись QR или нажми «Отмена».",
            reply_markup=kb_cancel()
        )
        return
    await grab_session(message, state)

@dp.callback_query(F.data == "cancel_login")
async def cb_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("❌ Отменено.")
    await callback.answer()

@dp.callback_query(F.data == "new_session")
async def cb_new_session(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await grab_session(callback.message, state)

@dp.callback_query(F.data == "help")
async def cb_help(callback: CallbackQuery):
    await callback.answer()
    await cmd_help(callback.message)


# ─── Entry point ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("Bot starting…")
    asyncio.run(dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types()))
