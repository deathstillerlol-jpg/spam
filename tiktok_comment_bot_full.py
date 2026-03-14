import asyncio
import json
import logging
import random
from datetime import datetime
from typing import Dict, List, Optional
import aiosqlite
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from playwright.async_api import async_playwright, ProxySettings
from playwright_stealth import stealth_async
import capsolver
import aiohttp

# ====================== CONFIG ======================
BOT_TOKEN = "8137390275:AAHDQr5jhWK6mbgafZrsr9ZTcl8vVLovuek"
CAPSOLVER_API_KEY = "YOUR_CAPSOLVER_API_KEY_HERE"
DB_FILE = "tiktok_advanced.db"
ADMIN_ID = 7392178616  # ←←← ИЗМЕНИ НА СВОЙ ID !!!
HEADLESS = True

DEFAULT_COMMENTS = ["Класс! 🔥", "Согласен 100%", "Топ видео", "Лучшее сегодня", "👍"]
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    # добавь ещё 15–20 если хочешь
]

IS_RUNNING = False
QUEUE: asyncio.Queue = asyncio.Queue()
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ====================== STATES ======================
class AdminStates(StatesGroup):
    waiting_account_name = State()
    waiting_cookies = State()
    waiting_videos = State()
    waiting_delete_account_name = State()
    waiting_add_proxy = State()

# ====================== DATABASE ======================
async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE,
            storage_state TEXT NOT NULL,
            proxy TEXT,
            last_action DATETIME,
            actions_today INTEGER DEFAULT 0,
            last_reset DATE,
            banned BOOLEAN DEFAULT 0,
            warmup_done BOOLEAN DEFAULT 0
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER,
            action_type TEXT,
            success BOOLEAN,
            timestamp DATETIME
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT UNIQUE,
            status TEXT DEFAULT 'pending'
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS proxies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            proxy_json TEXT NOT NULL,
            is_active BOOLEAN DEFAULT 1,
            last_test DATETIME
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )""")
        defaults = {"delay_min": "240", "delay_max": "900", "max_actions_per_day": "12", "warmup_days": "5", "headless": "True"}
        for k, v in defaults.items():
            await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))
        await db.commit()

async def get_all_accounts() -> List[Dict]:
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute("SELECT * FROM accounts WHERE banned = 0")
        rows = await cursor.fetchall()
        cols = [desc[0] for desc in cursor.description]
        return [dict(zip(cols, row)) for row in rows]

async def add_account_to_db(name: str, storage_state: str, proxy: Optional[str] = None):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT OR REPLACE INTO accounts (name, storage_state, proxy, last_reset, actions_today, warmup_done) VALUES (?, ?, ?, DATE('now'), 0, 0)",
            (name, storage_state, proxy)
        )
        await db.commit()

async def add_video(url: str):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("INSERT OR IGNORE INTO videos (url) VALUES (?)", (url,))
        await db.commit()

async def get_pending_videos() -> List[str]:
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute("SELECT url FROM videos WHERE status = 'pending'")
        return [row[0] for row in await cursor.fetchall()]

# ====================== PROXY HELPERS ======================
def parse_proxy(proxy_str: Optional[str]) -> Optional[ProxySettings]:
    if not proxy_str:
        return None
    try:
        if proxy_str.startswith("{"):
            p = json.loads(proxy_str)
            server = f"{p.get('scheme', 'http')}://{p['host']}"
            return {"server": server, "username": p.get("username"), "password": p.get("password")} if p.get("username") else {"server": server}
        return {"server": proxy_str}
    except:
        return {"server": proxy_str}

async def get_random_proxy() -> Optional[str]:
    async with aiosqlite.connect(DB_FILE) as db:
        row = await db.execute("SELECT proxy_json FROM proxies WHERE is_active = 1 ORDER BY RANDOM() LIMIT 1")
        result = await row.fetchone()
        return result[0] if result else None

async def test_single_proxy(proxy_str: str) -> str:
    try:
        proxy = parse_proxy(proxy_str)
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get("https://httpbin.org/ip", proxy=proxy["server"] if proxy else None) as resp:
                return "🟢 Живой" if resp.status == 200 else f"🔴 {resp.status}"
    except Exception as e:
        return f"❌ {str(e)[:50]}"

# ====================== MAIN MENU ======================
async def show_main_menu(message: types.Message):
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📂 Аккаунты"), KeyboardButton(text="🎥 Видео")],
            [KeyboardButton(text="🌐 Прокси"), KeyboardButton(text="⚙️ Настройки")],
            [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="▶️ Запустить")],
            [KeyboardButton(text="⏹️ Остановить")]
        ],
        resize_keyboard=True
    )
    await message.answer("<b>🔧 Главная панель TikTok Bot</b>\nВыбери действие 👇", parse_mode="HTML", reply_markup=kb)

@dp.message(Command("menu"))
async def cmd_menu(m: types.Message):
    if m.from_user.id != ADMIN_ID:
        return await m.answer("⛔ Доступ запрещён")
    await show_main_menu(m)

# ====================== АККАУНТЫ ======================
@dp.message(F.text == "📂 Аккаунты")
async def accounts_menu(m: types.Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить аккаунт", callback_data="add_account")],
        [InlineKeyboardButton(text="📋 Список", callback_data="list_accounts")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data="delete_account")],
        [InlineKeyboardButton(text="← Назад", callback_data="back_main")]
    ])
    await m.answer("📂 <b>Аккаунты</b>", parse_mode="HTML", reply_markup=kb)

@dp.callback_query(F.data == "add_account")
async def start_add_account(cb: types.CallbackQuery, state: FSMContext):
    await cb.message.edit_text("Введите имя аккаунта:")
    await state.set_state(AdminStates.waiting_account_name)

@dp.message(AdminStates.waiting_account_name)
async def process_name(m: types.Message, state: FSMContext):
    await state.update_data(name=m.text.strip())
    await m.answer("Отправь storageState JSON (файл или текст)")
    await state.set_state(AdminStates.waiting_cookies)

@dp.message(AdminStates.waiting_cookies, F.document | F.text)
async def process_cookies(m: types.Message, state: FSMContext):
    data = await state.get_data()
    try:
        if m.document:
            file = await bot.get_file(m.document.file_id)
            content = await bot.download_file(file.file_path)
            state_json = json.loads(content.read().decode())
        else:
            state_json = json.loads(m.text)
        await add_account_to_db(data["name"], json.dumps(state_json))
        await m.answer(f"✅ Аккаунт {data['name']} добавлен!")
    except Exception as e:
        await m.answer(f"❌ Ошибка: {e}")
    await state.clear()
    await show_main_menu(m)

@dp.callback_query(F.data == "list_accounts")
async def list_accounts(cb: types.CallbackQuery):
    accs = await get_all_accounts()
    text = "📂 Аккаунты:\n\n" + "\n".join([f"• {a['name']} — {'✅ Прогрет' if a['warmup_done'] else '🔄 Прогрев'}" for a in accs]) or "Пусто"
    await cb.message.edit_text(text)

@dp.callback_query(F.data == "delete_account")
async def start_delete(cb: types.CallbackQuery, state: FSMContext):
    await cb.message.edit_text("Введите имя аккаунта для удаления:")
    await state.set_state(AdminStates.waiting_delete_account_name)

@dp.message(AdminStates.waiting_delete_account_name)
async def delete_account(m: types.Message, state: FSMContext):
    name = m.text.strip()
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("DELETE FROM accounts WHERE name = ?", (name,))
        await db.commit()
    await m.answer(f"🗑 Аккаунт {name} удалён (если был)")
    await state.clear()
    await show_main_menu(m)

# ====================== ВИДЕО ======================
@dp.message(F.text == "🎥 Видео")
async def videos_menu(m: types.Message, state: FSMContext):
    await m.answer("Отправь ссылки на видео (по одной на строку или .txt файл)")
    await state.set_state(AdminStates.waiting_videos)

@dp.message(AdminStates.waiting_videos)
async def process_videos(m: types.Message, state: FSMContext):
    urls = []
    if m.text:
        urls = [line.strip() for line in m.text.split("\n") if line.startswith("https")]
    elif m.document:
        file = await bot.get_file(m.document.file_id)
        content = (await bot.download_file(file.file_path)).read().decode()
        urls = [line.strip() for line in content.split("\n") if line.startswith("https")]
    for url in urls:
        await add_video(url)
    await m.answer(f"✅ Добавлено {len(urls)} видео!")
    await state.clear()
    await show_main_menu(m)

# ====================== ПРОКСИ ======================
@dp.message(F.text == "🌐 Прокси")
async def proxies_menu(m: types.Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить", callback_data="add_proxy")],
        [InlineKeyboardButton(text="📋 Список", callback_data="list_proxies")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data="delete_proxy")],
        [InlineKeyboardButton(text="🔍 Тест всех", callback_data="test_proxy")],
        [InlineKeyboardButton(text="← Назад", callback_data="back_main")]
    ])
    await m.answer("🌐 <b>Прокси</b>", parse_mode="HTML", reply_markup=kb)

@dp.callback_query(F.data == "add_proxy")
async def start_add_proxy(cb: types.CallbackQuery, state: FSMContext):
    await cb.message.edit_text("Отправь прокси:\nhttp://user:pass@ip:port\nили JSON")
    await state.set_state(AdminStates.waiting_add_proxy)

@dp.message(AdminStates.waiting_add_proxy)
async def process_proxy(m: types.Message, state: FSMContext):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("INSERT INTO proxies (proxy_json, is_active) VALUES (?, 1)", (m.text.strip(),))
        await db.commit()
    await m.answer("✅ Прокси добавлен")
    await state.clear()
    await proxies_menu(m)

@dp.callback_query(F.data == "test_proxy")
async def test_proxies(cb: types.CallbackQuery):
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute("SELECT id, proxy_json FROM proxies WHERE is_active=1")
        proxies_list = await cursor.fetchall()
    if not proxies_list:
        await cb.message.edit_text("Нет прокси")
        return
    msg = await cb.message.edit_text("🔍 Тестирую прокси... 0%")
    results = []
    for i, (pid, pstr) in enumerate(proxies_list, 1):
        status = await test_single_proxy(pstr)
        results.append(f"ID{pid}: {status}")
        await msg.edit_text(f"Тестирую... {int(i/len(proxies_list)*100)}%\n\n" + "\n".join(results))
    await msg.edit_text("Результат теста:\n\n" + "\n".join(results))

# ====================== ЗАПУСК / СТОП ======================
@dp.message(F.text == "▶️ Запустить")
async def start_bot_cmd(m: types.Message):
    global IS_RUNNING
    IS_RUNNING = True
    videos = await get_pending_videos()
    for url in videos:
        comment = random.choice(DEFAULT_COMMENTS)
        await QUEUE.put((url, comment))
    await m.answer(f"🚀 **БОТ ЗАПУЩЕН!**\nВ очередь: {len(videos)} видео", parse_mode="HTML")

@dp.message(F.text == "⏹️ Остановить")
async def stop_bot_cmd(m: types.Message):
    global IS_RUNNING
    IS_RUNNING = False
    await m.answer("⏹️ **БОТ ОСТАНОВЛЕН**", parse_mode="HTML")

@dp.message(F.text == "📊 Статистика")
async def stats_cmd(m: types.Message):
    accs = len(await get_all_accounts())
    await m.answer(f"📊 <b>Статистика</b>\nАккаунтов: {accs}\nСтатус: {'🟢 Работает' if IS_RUNNING else '🔴 Остановлен'}", parse_mode="HTML")

# ====================== WORKER + PERFORM ======================
async def can_perform_action(account: Dict) -> bool:
    # твоя оригинальная логика (сброс счётчика)
    today = datetime.utcnow().date().isoformat()
    if account.get("last_reset") != today:
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute("UPDATE accounts SET actions_today=0, last_reset=? WHERE id=?", (today, account["id"]))
            await db.commit()
        return True
    return account.get("actions_today", 0) < 12

async def record_action(account_id: int, action_type: str, success: bool):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("INSERT INTO stats (account_id, action_type, success, timestamp) VALUES (?, ?, ?, ?)",
                         (account_id, action_type, success, datetime.utcnow().isoformat()))
        await db.execute("UPDATE accounts SET actions_today = actions_today + 1, last_action = ? WHERE id = ?",
                         (datetime.utcnow().isoformat(), account_id))
        await db.commit()

async def perform_action_on_video(account: Dict, video_url: str, comment: Optional[str]):
    if not await can_perform_action(account):
        return False
    name = account["name"]
    storage_state = json.loads(account["storage_state"])
    proxy_str = account.get("proxy") or await get_random_proxy()
    proxy_settings = parse_proxy(proxy_str)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS, args=["--no-sandbox"])
        try:
            context = await browser.new_context(
                storage_state=storage_state,
                viewport={"width": 390, "height": 844},
                user_agent=random.choice(USER_AGENTS),
                is_mobile=True,
                has_touch=True,
                proxy=proxy_settings
            )
            await stealth_async(context)
            page = await context.new_page()
            await page.goto(video_url, wait_until="domcontentloaded", timeout=60000)

            await asyncio.sleep(random.uniform(8, 25))
            await page.mouse.move(random.randint(100, 700), random.randint(100, 600))
            await page.evaluate("window.scrollBy(0, 300 + Math.random()*400)")

            success = True
            if comment:
                # капча + коммент (твой оригинальный код solve_captcha_if_present)
                if not await solve_captcha_if_present(page, name):
                    success = False
                else:
                    # твой код ввода комментария
                    await page.click('div[data-e2e="comment-input"]')
                    await page.type('div[data-e2e="comment-input"]', comment, delay=random.uniform(80, 160))
                    await page.click('button[data-e2e="comment-post-button"]')

            await record_action(account["id"], "comment" if comment else "view", success)
        except Exception as e:
            logger.error(f"[{name}] {e}")
            success = False
        finally:
            await browser.close()
    return success

# твой оригинальный solve_captcha_if_present (оставь как был)
async def solve_captcha_if_present(page, account_name):
    # ... (вставь свой код из первого сообщения)
    return True  # заглушка, замени на свой

async def comment_worker():
    while True:
        if not IS_RUNNING:
            await asyncio.sleep(5)
            continue
        try:
            video_url, comment = await QUEUE.get()
            accounts = await get_all_accounts()
            for acc in accounts:
                if not acc.get("warmup_done"):
                    await perform_action_on_video(acc, video_url, None)
                else:
                    await perform_action_on_video(acc, video_url, comment)
                await asyncio.sleep(random.uniform(240, 900))
            QUEUE.task_done()
        except Exception as e:
            logger.error(f"Worker error: {e}")
            await asyncio.sleep(300)

# ====================== ЗАПУСК ======================
async def main():
    await init_db()
    asyncio.create_task(comment_worker())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
