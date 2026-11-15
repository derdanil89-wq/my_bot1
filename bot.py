import asyncio
import html
import os
import random
import re
from datetime import datetime, timedelta, timezone
from math import ceil

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, ReplyKeyboardMarkup, KeyboardButton
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = os.getenv("DB_PATH", "bot.sqlite3")
ADMIN_IDS = {int(x) for x in re.findall(r"\d+", os.getenv("ADMIN_IDS", os.getenv("ADMIN_ID", "")))}
HOLD_HOURS = 48

if not TOKEN:
    raise RuntimeError("В .env не указан BOT_TOKEN")

bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)
db = None
scheduler = AsyncIOScheduler(timezone="UTC")


def now_dt():
    return datetime.now(timezone.utc)


def now_iso():
    return now_dt().isoformat(timespec="seconds")


def fmt_date(value):
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%d.%m.%Y %H:%M")
    except Exception:
        return str(value)


def mention(row):
    username = row["username"] if row["username"] else None
    return "@" + html.escape(username) if username else "ID " + str(row["tg_id"])


def is_admin(user_id):
    return user_id in ADMIN_IDS


def valid_tiktok(value):
    value = value.strip().lstrip("@").strip()
    return bool(re.fullmatch(r"[A-Za-z0-9._]{2,24}", value))


def main_keyboard(advertiser=False):
    rows = [
        [KeyboardButton(text="🎯 Задания"), KeyboardButton(text="🏆 Топ-20")],
        [KeyboardButton(text="🛍 Магазин"), KeyboardButton(text="🏅 Зал Славы")],
        [KeyboardButton(text="ℹ️ О проекте"), KeyboardButton(text="👤 Профиль")],
    ]
    if advertiser:
        rows.append([KeyboardButton(text="📣 Рекламодатель")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def advertiser_keyboard():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="💰 Баланс"), KeyboardButton(text="➕ Добавить канал")],
        [KeyboardButton(text="👥 Кто в холде"), KeyboardButton(text="📊 Статистика")],
        [KeyboardButton(text="⭐ Пополнить звёздами"), KeyboardButton(text="💵 Пополнить рублями")],
        [KeyboardButton(text="⬅ В меню")],
    ], resize_keyboard=True)


class PlayerState(StatesGroup):
    waiting_tiktok = State()


class AdminState(StatesGroup):
    waiting_id = State()
    waiting_amount = State()
    waiting_value = State()
    waiting_broadcast_media = State()
    waiting_broadcast_link = State()
    waiting_broadcast_button = State()


class AdvState(StatesGroup):
    waiting_forward = State()
    waiting_url = State()
    waiting_price = State()
    waiting_stars = State()


async def execute(sql, params=(), commit=True):
    cur = await db.execute(sql, params)
    if commit:
        await db.commit()
    return cur


async def one(sql, params=()):
    cur = await db.execute(sql, params)
    return await cur.fetchone()


async def all_rows(sql, params=()):
    cur = await db.execute(sql, params)
    return await cur.fetchall()


async def init_db():
    global db
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys=ON")
    await db.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        tg_id INTEGER PRIMARY KEY,
        username TEXT,
        tiktok TEXT,
        is_advertiser INTEGER NOT NULL DEFAULT 0,
        is_banned INTEGER NOT NULL DEFAULT 0,
        balance_rub REAL NOT NULL DEFAULT 0,
        referrer_id INTEGER,
        ref_bonus_given INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS channels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_tg_id INTEGER NOT NULL,
        chat_id TEXT NOT NULL,
        title TEXT NOT NULL,
        url TEXT NOT NULL,
        price_per_sub REAL NOT NULL DEFAULT 0.8 CHECK(price_per_sub >= 0.8),
        is_active INTEGER NOT NULL DEFAULT 2
    );
    CREATE TABLE IF NOT EXISTS subscriptions (
        user_id INTEGER NOT NULL,
        channel_id INTEGER NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('hold','paid','unsubscribed','returned')),
        hold_until TEXT,
        UNIQUE(user_id, channel_id),
        FOREIGN KEY(user_id) REFERENCES users(tg_id) ON DELETE CASCADE,
        FOREIGN KEY(channel_id) REFERENCES channels(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        amount INTEGER NOT NULL,
        reason TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(tg_id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS hall_of_fame (id INTEGER PRIMARY KEY AUTOINCREMENT, winners TEXT NOT NULL, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS payments (charge_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, kind TEXT NOT NULL, amount INTEGER NOT NULL, created_at TEXT NOT NULL);
    """)
    await db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('golden_tickets_remaining','2')")
    await db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('season_name','Текущий сезон')")
    await db.commit()


async def ensure_user(tg_user, referrer_id=None):
    existing = await one("SELECT * FROM users WHERE tg_id=?", (tg_user.id,))
    if existing is None:
        if referrer_id == tg_user.id:
            referrer_id = None
        await execute("INSERT INTO users(tg_id,username,referrer_id) VALUES(?,?,?)", (tg_user.id, tg_user.username, referrer_id))
        return await one("SELECT * FROM users WHERE tg_id=?", (tg_user.id,))
    await execute("UPDATE users SET username=? WHERE tg_id=?", (tg_user.username, tg_user.id))
    return await one("SELECT * FROM users WHERE tg_id=?", (tg_user.id,))


async def is_banned(user_id):
    row = await one("SELECT is_banned FROM users WHERE tg_id=?", (user_id,))
    return bool(row and row["is_banned"])


async def points(user_id):
    row = await one("SELECT COALESCE(SUM(amount),0) AS n FROM ledger WHERE user_id=?", (user_id,))
    return int(row["n"] or 0)


async def add_points(user_id, amount, reason):
    amount = int(amount)
    if amount:
        await execute("INSERT INTO ledger(user_id,amount,reason,created_at) VALUES(?,?,?,?)", (user_id, amount, reason, now_iso()))


async def get_setting(key, default=None):
    row = await one("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else default


async def set_setting(key, value):
    await execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


async def reward_task(user_id, channel_id):
    await add_points(user_id, 1, f"task_reward:channel_{channel_id}")
    user = await one("SELECT referrer_id,ref_bonus_given FROM users WHERE tg_id=?", (user_id,))
    first = await one("SELECT COUNT(*) AS n FROM ledger WHERE user_id=? AND reason LIKE 'task_reward:%'", (user_id,))
    if user and user["referrer_id"] and not user["ref_bonus_given"] and first and first["n"] == 1:
        ref = await one("SELECT is_banned FROM users WHERE tg_id=?", (user["referrer_id"],))
        if ref and not ref["is_banned"]:
            await add_points(user["referrer_id"], 2, f"referral_bonus:user_{user_id}")
        await execute("UPDATE users SET ref_bonus_given=1 WHERE tg_id=?", (user_id,))
    ticket = await get_setting("golden_tickets_remaining", "2")
    if random.random() < 0.01 and int(ticket) > 0:
        await set_setting("golden_tickets_remaining", int(ticket) - 1)
        await add_points(user_id, 10, "golden_ticket")
        return True
    return False


async def chat_member_exists(chat_id, user_id):
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        if member.status in ("creator", "administrator", "member"):
            return True
        return bool(getattr(member, "is_member", False))
    except (TelegramBadRequest, TelegramForbiddenError):
        return False


async def active_task(user_id):
    return await one("""
        SELECT c.* FROM channels c
        LEFT JOIN subscriptions s ON s.channel_id=c.id AND s.user_id=?
        WHERE c.is_active=1 AND (s.status IS NULL OR s.status NOT IN ('paid','returned'))
        ORDER BY CASE WHEN s.status='hold' THEN 0 ELSE 1 END, RANDOM() LIMIT 1
    """, (user_id,))


async def task_text(channel, sub=None):
    text = f"<b>Задание</b>\nПодпишитесь на канал: <b>{html.escape(channel['title'])}</b>\nПосле подписки нажмите «Проверить».\n\nЗа выполнение: +1 шанс. Холдинг подписки: {HOLD_HOURS} часов."
    if sub and sub["status"] == "hold":
        text += f"\n\nВаша подписка уже на проверке до: <b>{fmt_date(sub['hold_until'])}</b>."
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📣 Подписаться", url=channel["url"])],
        [InlineKeyboardButton(text="✅ Проверить", callback_data=f"task_check:{channel['id']}")],
    ])
    return text, kb


async def send_task(message):
    ch = await active_task(message.from_user.id)
    if not ch:
        await message.answer("Сейчас нет доступных заданий. Загляните позже.")
        return
    sub = await one("SELECT * FROM subscriptions WHERE user_id=? AND channel_id=?", (message.from_user.id, ch["id"]))
    text, kb = await task_text(ch, sub)
    await message.answer(text, reply_markup=kb)


async def show_profile(message):
    user = await one("SELECT * FROM users WHERE tg_id=?", (message.from_user.id,))
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{message.from_user.id}"
    await message.answer(
        f"<b>Профиль</b>\nTikTok: @{html.escape(user['tiktok']) if user['tiktok'] else 'не указан'}\nШансы: <b>{await points(message.from_user.id)}</b>\n\nРеферальная ссылка:\n<code>{link}</code>",
        reply_markup=main_keyboard(bool(user["is_advertiser"]))
    )


async def show_top(message, limit=20):
    rows = await all_rows("""
        SELECT u.tg_id,u.username,COALESCE(SUM(l.amount),0) AS n
        FROM users u LEFT JOIN ledger l ON l.user_id=u.tg_id
        WHERE u.is_banned=0 GROUP BY u.tg_id ORDER BY n DESC, u.tg_id LIMIT ?
    """, (limit,))
    lines = ["<b>Топ-20 игроков</b>"]
    for i, row in enumerate(rows, 1):
        lines.append(f"{i}. {mention(row)} — {int(row['n'])} ш." )
    await message.answer("\n".join(lines) if len(lines) > 1 else "Пока никто не набрал шансы.")


async def show_hall(message):
    rows = await all_rows("SELECT winners,created_at FROM hall_of_fame ORDER BY id DESC LIMIT 10")
    if not rows:
        await message.answer("Зал Славы пока пуст.")
        return
    text = "<b>Зал Славы</b>\n\n" + "\n\n".join(f"<b>{fmt_date(r['created_at'])}</b>\n{html.escape(r['winners'])}" for r in rows)
    await message.answer(text)


async def advertiser_stats(user_id):
    rows = await all_rows("""
        SELECT c.id,c.title,c.price_per_sub,c.is_active,
        (SELECT COUNT(*) FROM subscriptions s WHERE s.channel_id=c.id AND s.status='hold') AS holds,
        (SELECT COUNT(*) FROM subscriptions s WHERE s.channel_id=c.id AND s.status='paid') AS paid
        FROM channels c WHERE c.owner_tg_id=? ORDER BY c.id DESC
    """, (user_id,))
    lines = ["<b>Статистика рекламодателя</b>"]
    for r in rows:
        status = {0: "отклонён", 1: "активен", 2: "на проверке"}.get(r["is_active"], "неизвестно")
        lines.append(f"\n<b>{html.escape(r['title'])}</b> — {status}\nХолд: {r['holds']}, оплачено: {r['paid']}, цена: {r['price_per_sub']:.2f} ₽")
    await bot.send_message(user_id, "\n".join(lines) if len(lines) > 1 else "У вас пока нет каналов.", reply_markup=advertiser_keyboard())


async def show_balance(message):
    row = await one("SELECT balance_rub FROM users WHERE tg_id=?", (message.from_user.id,))
    await message.answer(f"Ваш рекламный баланс: <b>{row['balance_rub']:.2f} ₽</b>\nПополнение в рублях выполняется администратором командой /addbal ID сумма.", reply_markup=advertiser_keyboard())


async def hold_channels(message):
    rows = await all_rows("SELECT id,title FROM channels WHERE owner_tg_id=? AND is_active=1 ORDER BY id", (message.from_user.id,))
    if not rows:
        await message.answer("У вас нет активных каналов.", reply_markup=advertiser_keyboard())
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=str(r["title"]), callback_data=f"holdchoose:{r['id']}")] for r in rows])
    await message.answer("Выберите канал:", reply_markup=kb)


async def admin_required(message):
    if not is_admin(message.from_user.id):
        await message.answer("Команда доступна только администратору.")
        return False
    return True


@router.message.outer_middleware()
async def message_ban_middleware(handler, event, data):
    if getattr(event, "from_user", None) and await is_banned(event.from_user.id) and not is_admin(event.from_user.id):
        return None
    return await handler(event, data)


@router.callback_query.outer_middleware()
async def callback_ban_middleware(handler, event, data):
    if getattr(event, "from_user", None) and await is_banned(event.from_user.id) and not is_admin(event.from_user.id):
        await event.answer("Доступ ограничен.", show_alert=True)
        return None
    return await handler(event, data)


@router.message(CommandStart())
async def start(message: types.Message, state: FSMContext, command: CommandObject):
    await state.clear()
    ref = None
    if command.args and command.args.startswith("ref_"):
        try:
            ref = int(command.args[4:])
        except ValueError:
            ref = None
    user = await ensure_user(message.from_user, ref)
    if user["is_banned"]:
        return
    if not user["tiktok"]:
        await state.set_state(PlayerState.waiting_tiktok)
        await message.answer("Добро пожаловать! Укажите ваш TikTok без символа @ (латинские буквы, цифры, точка или подчёркивание).")
        return
    await message.answer("Главное меню:", reply_markup=main_keyboard(bool(user["is_advertiser"])))


@router.message(PlayerState.waiting_tiktok)
async def save_tiktok(message: types.Message, state: FSMContext):
    if not message.text or message.text.startswith("/"):
        await state.clear()
        await message.answer("Состояние сброшено. Повторите команду.")
        return
    value = message.text.strip().lstrip("@")
    if not valid_tiktok(value):
        await message.answer("Некорректный TikTok. Используйте 2–24 символа: латинские буквы, цифры, точку или подчёркивание.")
        return
    await execute("UPDATE users SET tiktok=? WHERE tg_id=?", (value, message.from_user.id))
    await state.clear()
    user = await one("SELECT is_advertiser FROM users WHERE tg_id=?", (message.from_user.id,))
    await message.answer("TikTok сохранён.", reply_markup=main_keyboard(bool(user["is_advertiser"])))


@router.message(F.text == "🎯 Задания")
async def menu_tasks(message: types.Message):
    await send_task(message)


@router.message(F.text == "🏆 Топ-20")
async def menu_top(message: types.Message):
    await show_top(message)


@router.message(F.text == "🏅 Зал Славы")
async def menu_hall(message: types.Message):
    await show_hall(message)


@router.message(F.text == "👤 Профиль")
async def menu_profile(message: types.Message):
    await show_profile(message)


@router.message(F.text == "ℹ️ О проекте")
async def about(message: types.Message):
    await message.answer("Это проект с заданиями на подписку. За подтверждённое задание начисляется шанс. Подписка проходит 48-часовой холдинг: если отписаться раньше, начисление отменяется.")


@router.message(F.text == "🛍 Магазин")
async def shop(message: types.Message):
    await message.answer("В магазине можно купить шансы за Telegram Stars по курсу 1 ⭐ = 1 шанс.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Купить шансы", callback_data="shop_buy")]]))


@router.callback_query(F.data == "shop_buy")
async def shop_buy(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdvState.waiting_stars)
    await state.update_data(payment_kind="points")
    await callback.message.answer("Введите целое количество Stars для покупки шансов:")
    await callback.answer()


@router.message(F.text == "📣 Рекламодатель")
async def advertiser_menu(message: types.Message):
    user = await one("SELECT is_advertiser FROM users WHERE tg_id=?", (message.from_user.id,))
    if not user or not user["is_advertiser"]:
        await message.answer("Статус рекламодателя ещё не подключён администратором.")
        return
    await message.answer("Меню рекламодателя:", reply_markup=advertiser_keyboard())


@router.message(F.text == "⬅ В меню")
async def back_menu(message: types.Message):
    user = await one("SELECT is_advertiser FROM users WHERE tg_id=?", (message.from_user.id,))
    await message.answer("Главное меню:", reply_markup=main_keyboard(bool(user and user["is_advertiser"])))


@router.callback_query(F.data.startswith("task_check:"))
async def check_task(callback: types.CallbackQuery):
    channel_id = int(callback.data.split(":", 1)[1])
    channel = await one("SELECT * FROM channels WHERE id=? AND is_active=1", (channel_id,))
    if not channel:
        await callback.answer("Канал больше недоступен.", show_alert=True)
        return
    sub = await one("SELECT * FROM subscriptions WHERE user_id=? AND channel_id=?", (callback.from_user.id, channel_id))
    subscribed = await chat_member_exists(channel["chat_id"], callback.from_user.id)
    if sub and sub["status"] == "paid":
        await callback.answer("Это задание уже оплачено.", show_alert=True)
        return
    if subscribed:
        if not sub or sub["status"] == "unsubscribed":
            until = (now_dt() + timedelta(hours=HOLD_HOURS)).isoformat(timespec="seconds")
            await execute("INSERT INTO subscriptions(user_id,channel_id,status,hold_until) VALUES(?,?,?,?) ON CONFLICT(user_id,channel_id) DO UPDATE SET status='hold',hold_until=excluded.hold_until", (callback.from_user.id, channel_id, "hold", until))
            ticket = await reward_task(callback.from_user.id, channel_id)
            msg = "Подписка подтверждена. Начислен 1 шанс. Холдинг до " + fmt_date(until) + "."
            if ticket:
                msg += " Вам выпал золотой билет: ещё +10 шансов!"
            await callback.answer(msg, show_alert=True)
        elif sub["status"] == "hold":
            await callback.answer(f"Подписка уже на проверке до {fmt_date(sub['hold_until'])}.", show_alert=True)
        else:
            await callback.answer("Подписка подтверждена.", show_alert=True)
    else:
        if sub and sub["status"] == "hold":
            await add_points(callback.from_user.id, -1, f"hold_return:channel_{channel_id}")
            await execute("UPDATE subscriptions SET status='unsubscribed',hold_until=NULL WHERE user_id=? AND channel_id=?", (callback.from_user.id, channel_id))
            await callback.answer("Вы отписались. Начисленный шанс отменён. Подпишитесь снова и проверьте задание ещё раз.", show_alert=True)
        else:
            await execute("INSERT INTO subscriptions(user_id,channel_id,status,hold_until) VALUES(?,?,?,NULL) ON CONFLICT(user_id,channel_id) DO UPDATE SET status='unsubscribed',hold_until=NULL", (callback.from_user.id, channel_id, "unsubscribed"))
            await callback.answer("Подписка не найдена. После подписки нажмите «Проверить».", show_alert=True)


@router.message(F.text == "📊 Статистика")
async def adv_stats(message: types.Message):
    user = await one("SELECT is_advertiser FROM users WHERE tg_id=?", (message.from_user.id,))
    if user and user["is_advertiser"]:
        await advertiser_stats(message.from_user.id)


@router.message(F.text == "💰 Баланс")
async def adv_balance(message: types.Message):
    await show_balance(message)


@router.message(F.text == "➕ Добавить канал")
async def add_channel_start(message: types.Message, state: FSMContext):
    user = await one("SELECT is_advertiser FROM users WHERE tg_id=?", (message.from_user.id,))
    if not user or not user["is_advertiser"]:
        await message.answer("Сначала получите статус рекламодателя у администратора.")
        return
    await state.set_state(AdvState.waiting_forward)
    await message.answer("Перешлите сюда любой пост из рекламируемого канала.")


@router.message(AdvState.waiting_forward)
async def add_channel_forward(message: types.Message, state: FSMContext):
    if message.text and message.text.startswith("/"):
        await state.clear()
        await message.answer("Состояние добавления канала сброшено.")
        return
    origin = getattr(message, "forward_origin", None)
    chat = getattr(origin, "chat", None) if origin else getattr(message, "forward_from_chat", None)
    chat_id = getattr(chat, "id", None)
    title = getattr(chat, "title", None) or "Новый канал"
    await state.update_data(channel_chat_id=str(chat_id) if chat_id is not None else "", channel_title=title)
    await state.set_state(AdvState.waiting_url)
    await message.answer("Отправьте ссылку на канал в формате https://t.me/имя или @имя.")


@router.message(AdvState.waiting_url)
async def add_channel_url(message: types.Message, state: FSMContext):
    if not message.text or message.text.startswith("/"):
        await state.clear()
        await message.answer("Состояние добавления канала сброшено.")
        return
    url = message.text.strip()
    if not (url.startswith("https://t.me/") or url.startswith("http://t.me/") or url.startswith("@")):
        await message.answer("Нужна ссылка вида https://t.me/имя или @имя.")
        return
    await state.update_data(channel_url=url)
    await state.set_state(AdvState.waiting_price)
    await message.answer("Введите цену за одного подписчика в рублях. Минимум — 0.8.")


@router.message(AdvState.waiting_price)
async def add_channel_price(message: types.Message, state: FSMContext):
    if not message.text or message.text.startswith("/"):
        await state.clear()
        await message.answer("Состояние добавления канала сброшено.")
        return
    try:
        price = float(message.text.replace(",", "."))
    except ValueError:
        await message.answer("Введите число, например 0.8 или 1.5.")
        return
    if price < 0.8:
        await message.answer("Цена не может быть меньше 0.8 ₽.")
        return
    data = await state.get_data()
    chat_id = data.get("channel_chat_id") or data["channel_url"]
    await execute("INSERT INTO channels(owner_tg_id,chat_id,title,url,price_per_sub,is_active) VALUES(?,?,?,?,?,2)", (message.from_user.id, chat_id, data.get("channel_title", "Канал"), data["channel_url"], price))
    await state.clear()
    await message.answer("Канал отправлен на проверку администратору.", reply_markup=advertiser_keyboard())


@router.message(F.text == "👥 Кто в холде")
async def adv_holds(message: types.Message):
    await hold_channels(message)


@router.callback_query(F.data.startswith("holdchoose:"))
async def hold_choose(callback: types.CallbackQuery):
    channel_id = int(callback.data.split(":", 1)[1])
    channel = await one("SELECT id,title FROM channels WHERE id=? AND owner_tg_id=?", (channel_id, callback.from_user.id))
    if not channel:
        await callback.answer("Канал не найден.", show_alert=True)
        return
    await render_holds(callback.message, callback.from_user.id, channel_id, 1, edit=True)
    await callback.answer()


async def render_holds(message, owner_id, channel_id, page, edit=False):
    channel = await one("SELECT title FROM channels WHERE id=? AND owner_tg_id=?", (channel_id, owner_id))
    if not channel:
        return
    total_row = await one("SELECT COUNT(*) AS n FROM subscriptions WHERE channel_id=? AND status='hold'", (channel_id,))
    total = int(total_row["n"])
    pages = max(1, ceil(total / 20))
    page = max(1, min(page, pages))
    rows = await all_rows("""
        SELECT u.tg_id,u.username,s.hold_until FROM subscriptions s JOIN users u ON u.tg_id=s.user_id
        WHERE s.channel_id=? AND s.status='hold' ORDER BY s.hold_until LIMIT 20 OFFSET ?
    """, (channel_id, (page - 1) * 20))
    lines = [f"<b>{html.escape(channel['title'])}</b>", f"Стр. {page} из {pages}. Всего: {total}"]
    lines += [f"{mention(r)} — до {fmt_date(r['hold_until'])}" for r in rows]
    if not rows:
        lines.append("Холдов пока нет.")
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="⬅ Пред", callback_data=f"holdpage:{channel_id}:{page-1}"))
    if page < pages:
        nav.append(InlineKeyboardButton(text="След ➡", callback_data=f"holdpage:{channel_id}:{page+1}"))
    kb = InlineKeyboardMarkup(inline_keyboard=[nav] if nav else [])
    if edit:
        try:
            await message.edit_text("\n".join(lines), reply_markup=kb)
        except TelegramBadRequest:
            await message.answer("\n".join(lines), reply_markup=kb)
    else:
        await message.answer("\n".join(lines), reply_markup=kb)


@router.callback_query(F.data.startswith("holdpage:"))
async def hold_page(callback: types.CallbackQuery):
    _, channel_id, page = callback.data.split(":")
    await render_holds(callback.message, callback.from_user.id, int(channel_id), int(page), edit=True)
    await callback.answer()


@router.message(F.text == "⭐ Пополнить звёздами")
async def adv_stars_start(message: types.Message, state: FSMContext):
    await state.set_state(AdvState.waiting_stars)
    await state.update_data(payment_kind="balance")
    await message.answer("Введите целое количество Stars для пополнения рекламного баланса. 1 ⭐ = 1 ₽:")


@router.message(F.text == "💵 Пополнить рублями")
async def adv_rub_start(message: types.Message):
    await message.answer("Для пополнения в рублях передайте администратору ваш ID и сумму. Администратор использует команду /addbal.")


@router.message(AdvState.waiting_stars)
async def make_invoice(message: types.Message, state: FSMContext):
    if not message.text or message.text.startswith("/"):
        await state.clear()
        await message.answer("Состояние оплаты сброшено.")
        return
    try:
        amount = int(message.text)
    except ValueError:
        await message.answer("Введите целое положительное число.")
        return
    if amount < 1 or amount > 10000:
        await message.answer("Количество Stars должно быть от 1 до 10000.")
        return
    data = await state.get_data()
    kind = data.get("payment_kind", "points")
    payload = f"{kind}:{message.from_user.id}:{amount}"
    await state.clear()
    await bot.send_invoice(chat_id=message.chat.id, title="Пополнение", description="Оплата Telegram Stars", payload=payload, currency="XTR", prices=[LabeledPrice(label="Stars", amount=amount)])


@router.pre_checkout_query()
async def pre_checkout(query: types.PreCheckoutQuery):
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def successful_payment(message: types.Message):
    payment = message.successful_payment
    charge = payment.telegram_payment_charge_id
    if await one("SELECT 1 FROM payments WHERE charge_id=?", (charge,)):
        return
    try:
        kind, uid, amount = payment.invoice_payload.split(":")
        uid, amount = int(uid), int(amount)
    except (ValueError, AttributeError):
        return
    if uid != message.from_user.id:
        return
    await execute("INSERT INTO payments(charge_id,user_id,kind,amount,created_at) VALUES(?,?,?,?,?)", (charge, uid, kind, amount, now_iso()))
    if kind == "points":
        await add_points(uid, amount, "shop_stars")
        await message.answer(f"Оплата получена. Начислено {amount} шансов.")
    elif kind == "balance":
        await execute("UPDATE users SET balance_rub=balance_rub+? WHERE tg_id=?", (amount, uid))
        await message.answer(f"Баланс пополнен на {amount} ₽.")


async def admin_start(message, state, action):
    if not await admin_required(message):
        return
    await state.clear()
    await state.update_data(admin_action=action)
    await state.set_state(AdminState.waiting_id)
    await message.answer("Введите числовой ID пользователя:")


for command_name, action_name in (("ban", "ban"), ("unban", "unban"), ("addadv", "addadv"), ("settiktok", "settiktok"), ("setvideo", "setvideo"), ("addbal", "addbal"), ("givepoints", "givepoints"), ("removepoints", "removepoints")):
    async def command_handler(message: types.Message, state: FSMContext, _action=action_name):
        await admin_start(message, state, _action)
    router.message.register(command_handler, Command(command_name))


@router.message(AdminState.waiting_id)
async def admin_id(message: types.Message, state: FSMContext):
    if message.text and message.text.startswith("/"):
        await state.clear()
        await message.answer("Состояние сброшено. Выполните команду ещё раз.")
        return
    try:
        uid = int((message.text or "").strip())
    except ValueError:
        await message.answer("Нужен числовой ID.")
        return
    data = await state.get_data()
    action = data.get("admin_action")
    if action == "ban":
        await execute("UPDATE users SET is_banned=1 WHERE tg_id=?", (uid,))
        await state.clear(); await message.answer("Пользователь заблокирован."); return
    if action == "unban":
        await execute("UPDATE users SET is_banned=0 WHERE tg_id=?", (uid,))
        await state.clear(); await message.answer("Пользователь разблокирован."); return
    if action == "addadv":
        await execute("UPDATE users SET is_advertiser=1 WHERE tg_id=?", (uid,))
        await state.clear(); await message.answer("Статус рекламодателя выдан."); return
    await state.update_data(target_id=uid)
    if action in ("addbal", "givepoints", "removepoints"):
        await state.set_state(AdminState.waiting_amount)
        await message.answer("Введите сумму/количество:")
    else:
        await state.set_state(AdminState.waiting_value)
        await message.answer("Введите значение:")


@router.message(AdminState.waiting_amount)
async def admin_amount(message: types.Message, state: FSMContext):
    if message.text and message.text.startswith("/"):
        await state.clear(); await message.answer("Состояние сброшено. Выполните команду ещё раз."); return
    data = await state.get_data()
    try:
        value = float((message.text or "").replace(",", ".")) if data.get("admin_action") == "addbal" else int((message.text or "").strip())
    except ValueError:
        await message.answer("Введите корректное число."); return
    if value <= 0:
        await message.answer("Значение должно быть положительным."); return
    uid = data["target_id"]
    action = data["admin_action"]
    if action == "addbal":
        await execute("UPDATE users SET balance_rub=balance_rub+? WHERE tg_id=?", (value, uid))
        answer = f"Баланс пользователя пополнен на {value:.2f} ₽."
    else:
        current = await points(uid)
        delta = value if action == "givepoints" else -min(current, value)
        await add_points(uid, delta, "admin_give" if delta >= 0 else "admin_remove")
        answer = f"Изменение шансов: {delta}. Текущий баланс: {await points(uid)}."
    await state.clear(); await message.answer(answer)


@router.message(AdminState.waiting_value)
async def admin_value(message: types.Message, state: FSMContext):
    if message.text and message.text.startswith("/"):
        await state.clear(); await message.answer("Состояние сброшено. Выполните команду ещё раз."); return
    data = await state.get_data(); uid = data["target_id"]
    value = (message.text or "").strip()
    if data["admin_action"] == "settiktok":
        if not valid_tiktok(value):
            await message.answer("Некорректный TikTok."); return
        await execute("UPDATE users SET tiktok=? WHERE tg_id=?", (value.lstrip("@"), uid))
        answer = "TikTok установлен."
    else:
        if len(value) > 500:
            await message.answer("Значение слишком длинное."); return
        await set_setting(f"video_{uid}", value)
        answer = "Видео/ссылка сохранены."
    await state.clear(); await message.answer(answer)


@router.message(Command("broadcast"))
async def broadcast_start(message: types.Message, state: FSMContext):
    if not await admin_required(message): return
    await state.clear(); await state.set_state(AdminState.waiting_broadcast_media)
    await message.answer("Отправьте фото, видео или текст для рассылки одним сообщением.")


@router.message(AdminState.waiting_broadcast_media)
async def broadcast_media(message: types.Message, state: FSMContext):
    if message.text and message.text.startswith("/"):
        await state.clear(); await message.answer("Состояние рассылки сброшено."); return
    await state.update_data(source_chat_id=message.chat.id, source_message_id=message.message_id)
    await state.set_state(AdminState.waiting_broadcast_link)
    await message.answer("Отправьте ссылку для кнопки или напишите «нет».")


@router.message(AdminState.waiting_broadcast_link)
async def broadcast_link(message: types.Message, state: FSMContext):
    if message.text and message.text.startswith("/"):
        await state.clear(); await message.answer("Состояние рассылки сброшено."); return
    link = (message.text or "").strip()
    if link.lower() != "нет" and not re.match(r"^https?://", link):
        await message.answer("Нужна ссылка с http:// или https:// либо слово «нет»."); return
    await state.update_data(link=None if link.lower() == "нет" else link)
    await state.set_state(AdminState.waiting_broadcast_button)
    await message.answer("Введите текст кнопки или напишите «нет».")


@router.message(AdminState.waiting_broadcast_button)
async def broadcast_button(message: types.Message, state: FSMContext):
    if message.text and message.text.startswith("/"):
        await state.clear(); await message.answer("Состояние рассылки сброшено."); return
    data = await state.get_data(); button_text = (message.text or "").strip()
    if button_text.lower() == "нет": button_text = None
    link = data.get("link")
    if bool(link) != bool(button_text):
        await message.answer("Ссылку и текст кнопки нужно указать вместе либо оба заменить на «нет»."); return
    users = await all_rows("SELECT tg_id FROM users WHERE is_banned=0")
    sent = 0
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=button_text, url=link)]]) if link else None
    for user in users:
        try:
            await bot.copy_message(chat_id=user["tg_id"], from_chat_id=data["source_chat_id"], message_id=data["source_message_id"], reply_markup=kb)
            await add_points(user["tg_id"], 1, "broadcast_bonus")
            sent += 1
        except (TelegramBadRequest, TelegramForbiddenError):
            pass
    await state.clear(); await message.answer(f"Рассылка завершена. Доставлено: {sent}. Каждому получателю начислен 1 шанс.")


@router.message(Command("addvideo"))
async def add_video(message: types.Message, state: FSMContext):
    if not await admin_required(message): return
    rows = await top_rows(10)
    if not rows:
        await message.answer("Недостаточно данных для формирования Топ-10."); return
    text = "<b>Подтвердите итоги сезона</b>\n\n" + "\n".join(f"{i}. {mention(r)} — {int(r['n'])} ш." for i, r in enumerate(rows, 1))
    await state.update_data(video_pending=True)
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Подтвердить", callback_data="video_confirm")]]))


async def top_rows(limit=50):
    return await all_rows("""
        SELECT u.tg_id,u.username,COALESCE(SUM(l.amount),0) AS n FROM users u
        LEFT JOIN ledger l ON l.user_id=u.tg_id WHERE u.is_banned=0
        GROUP BY u.tg_id ORDER BY n DESC,u.tg_id LIMIT ?
    """, (limit,))


@router.callback_query(F.data == "video_confirm")
async def video_confirm(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True); return
    data = await state.get_data()
    if not data.get("video_pending"):
        await callback.answer("Итоги уже обработаны.", show_alert=True); return
    rows = await top_rows(50)
    winners = "\n".join(f"{i}. {mention(r)} — {int(r['n'])} ш." for i, r in enumerate(rows[:10], 1))
    await execute("INSERT INTO hall_of_fame(winners,created_at) VALUES(?,?)", (winners, now_iso()))
    all_users = await all_rows("SELECT tg_id FROM users WHERE is_banned=0")
    ranking = {r["tg_id"]: (i + 1) for i, r in enumerate(rows)}
    for user in all_users:
        rank = ranking.get(user["tg_id"], 999999)
        current = await points(user["tg_id"])
        if 11 <= rank <= 20:
            await add_points(user["tg_id"], 2, "season_bonus_11_20")
        elif 21 <= rank <= 50:
            await add_points(user["tg_id"], 1, "season_bonus_21_50")
        elif rank > 10 and current:
            await add_points(user["tg_id"], -current, "season_reset")
    await set_setting("golden_tickets_remaining", 2)
    await state.clear()
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("Итоги сезона подтверждены. Топ-10 добавлен в Зал Славы, бонусы начислены, остальные шансы сброшены. Золотые билеты восстановлены: 2.")
    await callback.answer()


@router.message(Command("approve"))
async def approve_channel(message: types.Message):
    if not await admin_required(message): return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: /approve ID_канала"); return
    await execute("UPDATE channels SET is_active=1 WHERE id=?", (int(parts[1]),))
    await message.answer("Канал активирован.")


@router.message(Command("reject"))
async def reject_channel(message: types.Message):
    if not await admin_required(message): return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: /reject ID_канала"); return
    await execute("UPDATE channels SET is_active=0 WHERE id=?", (int(parts[1]),))
    await message.answer("Канал отклонён.")


@router.message(Command("stats"))
async def stats(message: types.Message):
    if not await admin_required(message): return
    users = await one("SELECT COUNT(*) AS n FROM users")
    banned = await one("SELECT COUNT(*) AS n FROM users WHERE is_banned=1")
    channels = await one("SELECT COUNT(*) AS n FROM channels WHERE is_active=1")
    holds = await one("SELECT COUNT(*) AS n FROM subscriptions WHERE status='hold'")
    total = await one("SELECT COALESCE(SUM(amount),0) AS n FROM ledger")
    await message.answer(f"Пользователи: {users['n']}\nБаны: {banned['n']}\nАктивные каналы: {channels['n']}\nХолды: {holds['n']}\nСумма изменений шансов: {total['n']}")


@router.message(Command("checkall"))
async def checkall(message: types.Message):
    if not await admin_required(message): return
    rows = await all_rows("SELECT s.user_id,s.channel_id,s.status,c.chat_id FROM subscriptions s JOIN channels c ON c.id=s.channel_id WHERE s.status='hold'")
    removed = paid = 0
    for r in rows:
        member = await chat_member_exists(r["chat_id"], r["user_id"])
        if not member:
            await add_points(r["user_id"], -1, f"hold_return:channel_{r['channel_id']}")
            await execute("UPDATE subscriptions SET status='unsubscribed',hold_until=NULL WHERE user_id=? AND channel_id=?", (r["user_id"], r["channel_id"]))
            removed += 1
        elif r["status"] == "hold":
            sub = await one("SELECT hold_until FROM subscriptions WHERE user_id=? AND channel_id=?", (r["user_id"], r["channel_id"]))
            if sub and sub["hold_until"] and sub["hold_until"] <= now_iso():
                await execute("UPDATE subscriptions SET status='paid' WHERE user_id=? AND channel_id=?", (r["user_id"], r["channel_id"]))
                paid += 1
    await message.answer(f"Проверка завершена. Отписок: {removed}, холдов переведено в оплаченные: {paid}.")


@router.message(Command("portfolio"))
async def portfolio(message: types.Message):
    if not await admin_required(message): return
    rows = await all_rows("""
        SELECT c.id,c.title,u.tg_id AS owner,c.price_per_sub,
        SUM(CASE WHEN s.status='hold' THEN 1 ELSE 0 END) AS holds,
        SUM(CASE WHEN s.status='paid' THEN 1 ELSE 0 END) AS paid
        FROM channels c JOIN users u ON u.tg_id=c.owner_tg_id LEFT JOIN subscriptions s ON s.channel_id=c.id
        WHERE c.is_active=1 GROUP BY c.id ORDER BY c.id
    """)
    if not rows: await message.answer("Активных каналов нет."); return
    text = "<b>Портфель каналов</b>\n" + "\n".join(f"#{r['id']} {html.escape(r['title'])}: холд {r['holds'] or 0}, оплачено {r['paid'] or 0}, цена {r['price_per_sub']:.2f} ₽" for r in rows)
    await message.answer(text)


@router.message(Command("adstats"))
async def adstats(message: types.Message):
    if not await admin_required(message): return
    rows = await all_rows("SELECT COUNT(*) AS channels,COALESCE(SUM(balance_rub),0) AS balance FROM channels c JOIN users u ON u.tg_id=c.owner_tg_id WHERE c.is_active=1")
    holds = await one("SELECT COUNT(*) AS n FROM subscriptions WHERE status='hold'")
    paid = await one("SELECT COUNT(*) AS n FROM subscriptions WHERE status='paid'")
    await message.answer(f"Активных рекламных каналов: {rows[0]['channels']}\nБаланс рекламодателей: {rows[0]['balance']:.2f} ₽\nХолдов: {holds['n']}\nОплачено: {paid['n']}")


@router.message()
async def fallback(message: types.Message, state: FSMContext):
    if message.text and message.text.startswith("/"):
        await state.clear()
        await message.answer("Состояние сброшено. Неизвестная команда.")
        return
    user = await one("SELECT is_advertiser FROM users WHERE tg_id=?", (message.from_user.id,))
    await message.answer("Выберите действие в меню.", reply_markup=main_keyboard(bool(user and user["is_advertiser"])))


async def settle_holds():
    rows = await all_rows("SELECT s.user_id,s.channel_id,s.hold_until,c.chat_id FROM subscriptions s JOIN channels c ON c.id=s.channel_id WHERE s.status='hold' AND s.hold_until IS NOT NULL AND s.hold_until<=?", (now_iso(),))
    for r in rows:
        member = await chat_member_exists(r["chat_id"], r["user_id"])
        if member:
            await execute("UPDATE subscriptions SET status='paid' WHERE user_id=? AND channel_id=?", (r["user_id"], r["channel_id"]))
        else:
            await add_points(r["user_id"], -1, f"hold_return:channel_{r['channel_id']}")
            await execute("UPDATE subscriptions SET status='unsubscribed',hold_until=NULL WHERE user_id=? AND channel_id=?", (r["user_id"], r["channel_id"]))


async def main():
    await init_db()
    scheduler.add_job(settle_holds, "interval", minutes=10, id="settle_holds", replace_existing=True)
    scheduler.start()
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
