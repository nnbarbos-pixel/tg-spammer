import asyncio
import json
import os
import random
from datetime import datetime
from typing import Dict, Set

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from telethon import TelegramClient
from telethon.errors import (
    SessionPasswordNeededError, FloodWaitError, PeerFloodError,
    UserBannedInChannelError, ChatWriteForbiddenError
)
from telethon.tl.types import User, Chat, Channel
from telethon.sessions import StringSession

from config import *

FOOTER = "\n\n━━━━━━━━━━━━━━\n📤 Отправлено через бота\nhttps://t.me/bskkk112"

bot = Bot(BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()

DATA_FILE = "user_data.json"
SESSIONS_DIR = "sessions"
os.makedirs(SESSIONS_DIR, exist_ok=True)

# user_id -> data
USER_DATA: Dict[int, dict] = {}
# phone -> client (для логина, чтобы код не протухал)
LOGIN_CLIENTS: Dict[str, TelegramClient] = {}

class States(StatesGroup):
    waiting_phone = State()
    waiting_code = State()
    waiting_password = State()
    waiting_text = State()
    waiting_interval = State()

def load_data():
    global USER_DATA
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
                USER_DATA = {int(k): v for k, v in raw.items()}
                for u in USER_DATA.values():
                    u["selected_chats"] = set(u.get("selected_chats", []))
                    u.setdefault("spam_tasks", {})
        except Exception:
            USER_DATA = {}

def save_data():
    to_save = {}
    for uid, data in USER_DATA.items():
        d = data.copy()
        d["selected_chats"] = list(d.get("selected_chats", set()))
        d.pop("spam_tasks", None)
        to_save[str(uid)] = d
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(to_save, f, ensure_ascii=False, indent=2)

def get_user(user_id: int) -> dict:
    if user_id not in USER_DATA:
        USER_DATA[user_id] = {
            "accounts": [],
            "text": "Привет! Это тестовое сообщение.",
            "interval": 3600,
            "selected_chats": set(),
            "spam_tasks": {}
        }
    return USER_DATA[user_id]

async def is_subscribed(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(CHANNEL_ID, user_id)
        status = member.status
        print(f"[SUB] user {user_id} status={status}")
        return status in ("member", "administrator", "creator")
    except Exception as e:
        print(f"[SUB] check failed for {user_id}: {type(e).__name__} {e}")
        return False

def main_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Добавить аккаунт"), KeyboardButton(text="📋 Аккаунты")],
            [KeyboardButton(text="📝 Текст"), KeyboardButton(text="⏱ Интервал")],
            [KeyboardButton(text="🎯 Чаты"), KeyboardButton(text="📊 Статус")],
            [KeyboardButton(text="🚀 Старт"), KeyboardButton(text="🛑 Стоп")],
        ],
        resize_keyboard=True
    )

@router.message(CommandStart())
async def cmd_start(message: Message):
    if not await is_subscribed(message.from_user.id):
        await message.answer(
            f"🔒 Для использования бота подпишись на {CHANNEL}\n"
            "После подписки нажми /start"
        )
        return
    get_user(message.from_user.id)
    await message.answer(
        "👋 Добро пожаловать!\n\n"
        "Как пользоваться:\n"
        "1️⃣ Добавь аккаунт\n"
        "2️⃣ Задай текст и интервал\n"
        "3️⃣ Выбери чаты\n"
        "4️⃣ Нажми «Старт»\n\n"
        "Все настройки индивидуальные.",
        reply_markup=main_menu()
    )

# ========== Добавление аккаунта ==========
@router.message(F.text == "➕ Добавить аккаунт")
async def add_account_start(message: Message, state: FSMContext):
    if not await is_subscribed(message.from_user.id):
        return await message.answer("Сначала подпишись на канал.")
    data = get_user(message.from_user.id)
    if len(data["accounts"]) >= MAX_ACCOUNTS:
        return await message.answer(f"Лимит {MAX_ACCOUNTS} аккаунтов.")
    await message.answer("Введи номер в любом формате (+95..., 9596..., с пробелами и т.д.):")
    await state.set_state(States.waiting_phone)

# храним активные клиенты для логина
LOGIN_CLIENTS: Dict[str, TelegramClient] = {}

@router.message(States.waiting_phone)
async def process_phone(message: Message, state: FSMContext):
    raw = message.text or ""
    phone = "".join(c for c in raw if c.isdigit() or c == "+")
    if not phone:
        return await message.answer("Не вижу номера. Введи ещё раз.")
    if not phone.startswith("+"):
        phone = "+" + phone
    if len(phone) < 8:
        return await message.answer("Слишком короткий номер. Попробуй ещё раз.")
    
    await state.update_data(phone=phone)
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()
    try:
        sent = await client.send_code_request(phone)
        # держим клиент живым до ввода кода
        LOGIN_CLIENTS[phone] = client
        await state.update_data(phone_code_hash=sent.phone_code_hash)
        await message.answer(f"Код отправлен на {phone}. Введи код:")
        await state.set_state(States.waiting_code)
    except Exception as e:
        await message.answer(f"Ошибка отправки кода: {e}\nПопробуй другой формат или номер.")
        await state.clear()
        await client.disconnect()

@router.message(States.waiting_code)
async def process_code(message: Message, state: FSMContext):
    data = await state.get_data()
    phone = data["phone"]
    client = LOGIN_CLIENTS.get(phone)
    if not client:
        await message.answer("Сессия потеряна (бот перезапустился). Начни заново.")
        await state.clear()
        return
    need_2fa = False
    try:
        await client.sign_in(phone, message.text.strip(), phone_code_hash=data["phone_code_hash"])
        final_session = client.session.save()
        user_data = get_user(message.from_user.id)
        user_data["accounts"].append({
            "phone": phone,
            "session": final_session
        })
        save_data()
        await message.answer(f"✅ Аккаунт {phone} добавлен.")
        await state.clear()
    except SessionPasswordNeededError:
        need_2fa = True
        await message.answer("Введи пароль 2FA:")
        await state.set_state(States.waiting_password)
    except Exception as e:
        await message.answer(f"Ошибка входа: {e}")
        await state.clear()
    finally:
        if not need_2fa and phone in LOGIN_CLIENTS:
            await client.disconnect()
            LOGIN_CLIENTS.pop(phone, None)

@router.message(States.waiting_password)
async def process_password(message: Message, state: FSMContext):
    data = await state.get_data()
    phone = data["phone"]
    client = LOGIN_CLIENTS.get(phone)
    if not client:
        await message.answer("Сессия потеряна. Начни заново.")
        await state.clear()
        return
    try:
        await client.sign_in(password=message.text)
        final_session = client.session.save()
        user_data = get_user(message.from_user.id)
        user_data["accounts"].append({
            "phone": phone,
            "session": final_session
        })
        save_data()
        await message.answer(f"✅ Аккаунт {phone} добавлен (2FA).")
    except Exception as e:
        await message.answer(f"Ошибка: {e}")
    finally:
        await state.clear()
        if phone in LOGIN_CLIENTS:
            await client.disconnect()
            LOGIN_CLIENTS.pop(phone, None)

# ========== Текст / Интервал ==========
@router.message(F.text == "📝 Текст")
async def set_text(message: Message, state: FSMContext):
    await message.answer("Отправь текст рассылки (можно с HTML, но лучше plain):")
    await state.set_state(States.waiting_text)

@router.message(States.waiting_text)
async def save_text(message: Message, state: FSMContext):
    get_user(message.from_user.id)["text"] = message.text or ""
    save_data()
    await message.answer("Текст сохранён.")
    await state.clear()

@router.message(F.text == "⏱ Интервал")
async def set_interval(message: Message, state: FSMContext):
    await message.answer(
        "Интервал в минутах между рассылками.\n\n"
        "Например:\n"
        "• 1 = раз в минуту\n"
        "• 5 = раз в 5 минут\n"
        "• 60 = раз в час\n\n"
        "Как работает:\n"
        "Все выбранные чаты получат сообщение раз в этот интервал.\n"
        "Между сообщениями пауза 3-8 сек (антибан).\n"
        "Можно юзать одновременно с другими — у каждого свои настройки."
    )
    await state.set_state(States.waiting_interval)

@router.message(States.waiting_interval)
async def save_interval(message: Message, state: FSMContext):
    try:
        minutes = int(message.text.strip())
        if minutes < 1:
            return await message.answer("Минимум 1 минута.")
        sec = minutes * 60
        get_user(message.from_user.id)["interval"] = sec
        save_data()
        await message.answer(f"Интервал установлен: {minutes} мин.")
    except ValueError:
        await message.answer("Нужно целое число (минуты).")
    await state.clear()

# ========== Выбор чатов ==========
@router.message(F.text == "🎯 Чаты")
async def select_chats(message: Message):
    data = get_user(message.from_user.id)
    if not data["accounts"]:
        return await message.answer("Сначала добавь аккаунт.")

    # берём первый аккаунт
    acc = data["accounts"][0]
    phone = acc["phone"]
    session_str = acc["session"]

    client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        return await message.answer("Аккаунт не авторизован. Добавь заново.")

    try:
        dialogs = await client.get_dialogs(limit=100)
    except Exception as e:
        await client.disconnect()
        return await message.answer(f"Не удалось получить чаты: {e}")

    kb = []
    selected = data["selected_chats"]
    for d in dialogs:
        entity = d.entity
        # только группы и каналы (не лички и не Telegram)
        if isinstance(entity, User):
            continue
        if entity.id == 777000:
            continue
        title = (getattr(entity, "title", None) or "Unknown")[:35]
        chat_id = entity.id
        mark = "🟢" if chat_id in selected else "⚪"
        kb.append([InlineKeyboardButton(
            text=f"{mark} {title}",
            callback_data=f"toggle:{chat_id}"
        )])
    if not kb:
        await client.disconnect()
        return await message.answer("Нет доступных групп/каналов.")
    kb.append([InlineKeyboardButton(text="✅ Готово", callback_data="chats_done")])

    await message.answer(
        f"Чаты аккаунта {phone}\nВыбери куда спамить (только группы):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb)
    )
    await client.disconnect()

@router.callback_query(F.data.startswith("toggle:"))
async def toggle_chat(callback: CallbackQuery):
    chat_id = int(callback.data.split(":")[1])
    data = get_user(callback.from_user.id)
    if chat_id in data["selected_chats"]:
        data["selected_chats"].discard(chat_id)
    else:
        data["selected_chats"].add(chat_id)
    save_data()

    # перерисовываем кнопки
    kb = []
    for row in callback.message.reply_markup.inline_keyboard:
        for btn in row:
            if btn.callback_data and btn.callback_data.startswith("toggle:"):
                cid = int(btn.callback_data.split(":")[1])
                title = btn.text.split(" ", 1)[-1] if " " in btn.text else btn.text
                mark = "🟢" if cid in data["selected_chats"] else "⚪"
                kb.append([InlineKeyboardButton(text=f"{mark} {title}", callback_data=btn.callback_data)])
            else:
                kb.append([btn])
    await callback.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await callback.answer()

@router.callback_query(F.data == "chats_done")
async def chats_done(callback: CallbackQuery):
    count = len(get_user(callback.from_user.id)["selected_chats"])
    await callback.message.edit_text(f"Выбрано чатов: {count}")
    await callback.answer()

# ========== Спам воркер ==========
async def spam_worker(user_id: int, phone: str, session_str: str):
    data = get_user(user_id)
    client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        print(f"[{phone}] not authorized")
        await client.disconnect()
        return

    print(f"[{phone}] spam worker started")
    while True:
        try:
            chats = list(data.get("selected_chats", set()))
            text = data.get("text", "").strip()
            interval = max(data.get("interval", 3600), MIN_INTERVAL)

            if not chats or not text:
                await asyncio.sleep(10)
                continue

            random.shuffle(chats)
            # получаем актуальные диалоги для резолва
            try:
                dialogs = await client.get_dialogs(limit=200)
                entity_map = {d.entity.id: d.entity for d in dialogs}
            except Exception as e:
                print(f"[{phone}] dialogs error: {e}")
                entity_map = {}

            for chat_id in chats:
                if chat_id == 777000:
                    data["selected_chats"].discard(chat_id)
                    continue
                try:
                    entity = entity_map.get(chat_id)
                    if not entity:
                        entity = await client.get_entity(chat_id)
                    if isinstance(entity, User):
                        data["selected_chats"].discard(chat_id)
                        save_data()
                        continue
                    full_text = text + FOOTER
                    await client.send_message(entity, full_text)
                    print(f"[{phone}] sent to {getattr(entity, 'title', chat_id)}")
                except FloodWaitError as e:
                    print(f"[{phone}] FloodWait {e.seconds}s")
                    await asyncio.sleep(e.seconds + random.randint(5, 15))
                except (PeerFloodError, UserBannedInChannelError, ChatWriteForbiddenError, ValueError) as e:
                    print(f"[{phone}] skip chat {chat_id}: {e}")
                    data["selected_chats"].discard(chat_id)
                    save_data()
                except Exception as e:
                    print(f"[{phone}] error {chat_id}: {type(e).__name__} {e}")

                delay = random.uniform(*FLOOD_SAFE_DELAY)
                await asyncio.sleep(delay)

            # пауза между циклами + рандом
            cycle_delay = interval + random.randint(-int(interval*0.1), int(interval*0.15))
            print(f"[{phone}] cycle done, sleep {cycle_delay}s")
            await asyncio.sleep(max(cycle_delay, MIN_INTERVAL))

        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[{phone}] worker error: {e}")
            await asyncio.sleep(30)

    await client.disconnect()
    print(f"[{phone}] worker stopped")

@router.message(F.text == "🚀 Старт")
async def start_spam(message: Message):
    data = get_user(message.from_user.id)
    if not data["accounts"]:
        return await message.answer("Нет аккаунтов.")
    if not data["selected_chats"]:
        return await message.answer("Не выбраны чаты.")
    if not data.get("text"):
        return await message.answer("Нет текста.")

    started = 0
    for acc in data["accounts"]:
        phone = acc["phone"]
        if phone in data["spam_tasks"]:
            continue
        task = asyncio.create_task(spam_worker(message.from_user.id, phone, acc["session"]))
        data["spam_tasks"][phone] = task
        started += 1

    await message.answer(f"Запущено воркеров: {started}\nИнтервал: {data.get('interval', 3600)//60} мин")

@router.message(F.text == "🛑 Стоп")
async def stop_spam(message: Message):
    data = get_user(message.from_user.id)
    for task in list(data["spam_tasks"].values()):
        task.cancel()
    data["spam_tasks"].clear()
    await message.answer("Все воркеры остановлены.")

@router.message(F.text == "📋 Аккаунты")
async def list_accounts(message: Message):
    accs = get_user(message.from_user.id)["accounts"]
    if not accs:
        return await message.answer("Нет аккаунтов. Добавь через «➕ Добавить аккаунт».")
    text = "\n".join([f"• {a['phone']}" for a in accs])
    await message.answer(f"Аккаунты ({len(accs)}/{MAX_ACCOUNTS}):\n{text}")

@router.message(F.text == "📊 Статус")
async def status(message: Message):
    data = get_user(message.from_user.id)
    tasks = data.get("spam_tasks", {})
    running = len(tasks)
    chats = len(data.get("selected_chats", set()))
    interval_min = data.get("interval", 3600) // 60
    txt = data.get("text", "")
    text_preview = (txt[:40] + "...") if len(txt) > 40 else (txt or "не задан")
    await message.answer(
        f"Статус:\n\n"
        f"Аккаунтов: {len(data['accounts'])}\n"
        f"Выбрано чатов: {chats}\n"
        f"Интервал: {interval_min} мин\n"
        f"Текст: {text_preview}\n"
        f"Воркеров запущено: {running}"
    )

dp.include_router(router)

async def main():
    load_data()
    print("Bot started")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
