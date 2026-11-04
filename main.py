import asyncio
import json
import os
import random
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
from pyrogram import Client
from pyrogram.errors import (
    SessionPasswordNeeded, FloodWait, PeerFlood, UserBannedInChannel,
    ChatWriteForbidden, PhoneCodeExpired, PhoneCodeInvalid
)

from config import *

FOOTER = "\n\n━━━━━━━━━━━━━━\n📤 Отправлено через бота\nhttps://t.me/bskkk112"

bot = Bot(BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()

DATA_FILE = "user_data.json"
SESSIONS_DIR = "sessions"
os.makedirs(SESSIONS_DIR, exist_ok=True)

USER_DATA: Dict[int, dict] = {}
LOGIN_CLIENTS: Dict[str, Client] = {}

class States(StatesGroup):
    waiting_phone = State()
    waiting_code = State()
    waiting_password = State()
    waiting_text = State()
    waiting_interval = State()
    select_account_text = State()
    select_account_interval = State()
    select_account_chats = State()
    select_account_delete = State()

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
            "spam_tasks": {}
        }
    return USER_DATA[user_id]

def get_account(user_id: int, phone: str) -> dict:
    data = get_user(user_id)
    for acc in data["accounts"]:
        if acc["phone"] == phone:
            return acc
    return None

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
            [KeyboardButton(text="🗑 Удалить аккаунт")],
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

# ========== Добавление аккаунта (Pyrogram) ==========
@router.message(F.text == "➕ Добавить аккаунт")
async def add_account_start(message: Message, state: FSMContext):
    if not await is_subscribed(message.from_user.id):
        return await message.answer("Сначала подпишись на канал.")
    data = get_user(message.from_user.id)
    if len(data["accounts"]) >= MAX_ACCOUNTS:
        return await message.answer(f"Лимит {MAX_ACCOUNTS} аккаунтов.")
    await message.answer("Введи номер в любом формате (+95..., 9596..., с пробелами и т.д.):")
    await state.set_state(States.waiting_phone)

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
    session_name = f"login_{phone.replace('+', '')}"
    client = Client(
        session_name,
        api_id=API_ID,
        api_hash=API_HASH,
        workdir=SESSIONS_DIR,
        device_model="SpamBot",
        system_version="1.0",
        app_version="1.0"
    )
    await client.connect()
    try:
        sent = await client.send_code(phone)
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
        await message.answer("Сессия потеряна. Начни добавление заново.")
        await state.clear()
        return
    try:
        await client.sign_in(phone, data["phone_code_hash"], message.text.strip())
        session_string = await client.export_session_string()
        user_data = get_user(message.from_user.id)
        user_data["accounts"].append({
            "phone": phone,
            "session": session_string,
            "text": "Привет! Это тестовое сообщение.",
            "interval": 3600,
            "selected_chats": []
        })
        save_data()
        await message.answer(f"✅ Аккаунт {phone} добавлен.")
        await state.clear()
    except SessionPasswordNeeded:
        await message.answer("Введи пароль 2FA:")
        await state.set_state(States.waiting_password)
        return
    except (PhoneCodeExpired, PhoneCodeInvalid) as e:
        await message.answer(f"Код неверный или протух: {e}\nНачни добавление заново.")
        await state.clear()
    except Exception as e:
        await message.answer(f"Ошибка входа: {e}")
        await state.clear()
    finally:
        if phone in LOGIN_CLIENTS and not isinstance(locals().get('e'), SessionPasswordNeeded):
            try:
                await client.disconnect()
            except:
                pass
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
        await client.check_password(message.text)
        session_string = await client.export_session_string()
        user_data = get_user(message.from_user.id)
        user_data["accounts"].append({
            "phone": phone,
            "session": session_string,
            "text": "Привет! Это тестовое сообщение.",
            "interval": 3600,
            "selected_chats": []
        })
        save_data()
        await message.answer(f"✅ Аккаунт {phone} добавлен (2FA).")
    except Exception as e:
        await message.answer(f"Ошибка: {e}")
    finally:
        await state.clear()
        if phone in LOGIN_CLIENTS:
            try:
                await client.disconnect()
            except:
                pass
            LOGIN_CLIENTS.pop(phone, None)

# ========== Удаление аккаунта ==========
@router.message(F.text == "🗑 Удалить аккаунт")
async def delete_account_start(message: Message, state: FSMContext):
    data = get_user(message.from_user.id)
    if not data["accounts"]:
        return await message.answer("Нет аккаунтов.")
    kb = [[InlineKeyboardButton(text=acc["phone"], callback_data=f"del:{acc['phone']}")] for acc in data["accounts"]]
    await message.answer("Выбери аккаунт для удаления:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.callback_query(F.data.startswith("del:"))
async def delete_account_confirm(callback: CallbackQuery):
    phone = callback.data.split(":")[1]
    data = get_user(callback.from_user.id)
    data["accounts"] = [a for a in data["accounts"] if a["phone"] != phone]
    if phone in data["spam_tasks"]:
        data["spam_tasks"][phone].cancel()
        data["spam_tasks"].pop(phone, None)
    save_data()
    await callback.message.edit_text(f"Аккаунт {phone} удалён.")
    await callback.answer()

# ========== Текст / Интервал (выбор аккаунта) ==========
@router.message(F.text == "📝 Текст")
async def set_text_start(message: Message, state: FSMContext):
    data = get_user(message.from_user.id)
    if not data["accounts"]:
        return await message.answer("Сначала добавь аккаунт.")
    kb = [[InlineKeyboardButton(text=acc["phone"], callback_data=f"textacc:{acc['phone']}")] for acc in data["accounts"]]
    await message.answer("Для какого аккаунта задать текст?", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.callback_query(F.data.startswith("textacc:"))
async def set_text_select(callback: CallbackQuery, state: FSMContext):
    phone = callback.data.split(":")[1]
    await state.update_data(edit_phone=phone)
    await callback.message.edit_text(f"Отправь текст для {phone}:")
    await state.set_state(States.waiting_text)
    await callback.answer()

@router.message(States.waiting_text)
async def save_text(message: Message, state: FSMContext):
    data = await state.get_data()
    phone = data.get("edit_phone")
    acc = get_account(message.from_user.id, phone)
    if acc:
        acc["text"] = message.text or ""
        save_data()
        await message.answer(f"Текст для {phone} сохранён.")
    await state.clear()

@router.message(F.text == "⏱ Интервал")
async def set_interval_start(message: Message, state: FSMContext):
    data = get_user(message.from_user.id)
    if not data["accounts"]:
        return await message.answer("Сначала добавь аккаунт.")
    kb = [[InlineKeyboardButton(text=acc["phone"], callback_data=f"intacc:{acc['phone']}")] for acc in data["accounts"]]
    await message.answer("Для какого аккаунта задать интервал?", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.callback_query(F.data.startswith("intacc:"))
async def set_interval_select(callback: CallbackQuery, state: FSMContext):
    phone = callback.data.split(":")[1]
    await state.update_data(edit_phone=phone)
    await callback.message.edit_text(
        f"Интервал в минутах для {phone}:\n\n"
        "• 1 = раз в минуту\n"
        "• 5 = раз в 5 минут\n"
        "• 60 = раз в час"
    )
    await state.set_state(States.waiting_interval)
    await callback.answer()

@router.message(States.waiting_interval)
async def save_interval(message: Message, state: FSMContext):
    data = await state.get_data()
    phone = data.get("edit_phone")
    try:
        minutes = int(message.text.strip())
        if minutes < 1:
            return await message.answer("Минимум 1 минута.")
        acc = get_account(message.from_user.id, phone)
        if acc:
            acc["interval"] = minutes * 60
            save_data()
            await message.answer(f"Интервал для {phone}: {minutes} мин.")
    except ValueError:
        await message.answer("Нужно целое число.")
    await state.clear()

# ========== Выбор чатов ==========
@router.message(F.text == "🎯 Чаты")
async def select_chats_start(message: Message):
    data = get_user(message.from_user.id)
    if not data["accounts"]:
        return await message.answer("Сначала добавь аккаунт.")
    kb = [[InlineKeyboardButton(text=acc["phone"], callback_data=f"chatsacc:{acc['phone']}")] for acc in data["accounts"]]
    await message.answer("Для какого аккаунта выбрать чаты?", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.callback_query(F.data.startswith("chatsacc:"))
async def select_chats(callback: CallbackQuery):
    phone = callback.data.split(":")[1]
    data = get_user(callback.from_user.id)
    acc = get_account(callback.from_user.id, phone)
    if not acc:
        return await callback.answer("Аккаунт не найден")

    session_str = acc["session"]
    client = Client(
        f"temp_{phone.replace('+', '')}",
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=session_str,
        device_model="SpamBot",
        system_version="1.0",
        app_version="1.0"
    )
    await client.start()
    try:
        dialogs = []
        async for dialog in client.get_dialogs(limit=100):
            dialogs.append(dialog)
    except Exception as e:
        await client.stop()
        return await callback.message.edit_text(f"Не удалось получить чаты: {e}")

    kb = []
    selected = set(acc.get("selected_chats", []))
    for d in dialogs:
        chat = d.chat
        if chat.type in ("private", "bot"):
            continue
        title = (chat.title or "Unknown")[:35]
        chat_id = chat.id
        mark = "🟢" if chat_id in selected else "⚪"
        kb.append([InlineKeyboardButton(
            text=f"{mark} {title}",
            callback_data=f"toggle:{phone}:{chat_id}"
        )])
    if not kb:
        await client.stop()
        return await callback.message.edit_text("Нет доступных групп/каналов.")
    kb.append([InlineKeyboardButton(text="✅ Готово", callback_data=f"chatsdone:{phone}")])

    await callback.message.edit_text(
        f"Чаты аккаунта {phone}\nВыбери куда спамить (только группы):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb)
    )
    await client.stop()
    await callback.answer()

@router.callback_query(F.data.startswith("toggle:"))
async def toggle_chat(callback: CallbackQuery):
    parts = callback.data.split(":")
    phone = parts[1]
    chat_id = int(parts[2])
    acc = get_account(callback.from_user.id, phone)
    if not acc:
        return await callback.answer("Аккаунт не найден")
    selected = set(acc.get("selected_chats", []))
    if chat_id in selected:
        selected.discard(chat_id)
    else:
        selected.add(chat_id)
    acc["selected_chats"] = list(selected)
    save_data()

    kb = []
    for row in callback.message.reply_markup.inline_keyboard:
        for btn in row:
            if btn.callback_data and btn.callback_data.startswith("toggle:"):
                cid = int(btn.callback_data.split(":")[2])
                title = btn.text.split(" ", 1)[-1] if " " in btn.text else btn.text
                mark = "🟢" if cid in selected else "⚪"
                kb.append([InlineKeyboardButton(text=f"{mark} {title}", callback_data=btn.callback_data)])
            else:
                kb.append([btn])
    await callback.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await callback.answer()

@router.callback_query(F.data.startswith("chatsdone:"))
async def chats_done(callback: CallbackQuery):
    phone = callback.data.split(":")[1]
    acc = get_account(callback.from_user.id, phone)
    count = len(acc.get("selected_chats", [])) if acc else 0
    await callback.message.edit_text(f"Для {phone} выбрано чатов: {count}")
    await callback.answer()

# ========== Спам воркер ==========
async def spam_worker(user_id: int, phone: str, session_str: str):
    data = get_user(user_id)
    acc = get_account(user_id, phone)
    if not acc:
        return
    client = Client(
        f"spam_{phone.replace('+', '')}",
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=session_str,
        device_model="SpamBot",
        system_version="1.0",
        app_version="1.0"
    )
    await client.start()
    print(f"[{phone}] spam worker started")
    while True:
        try:
            chats = list(acc.get("selected_chats", []))
            text = acc.get("text", "").strip()
            interval = max(acc.get("interval", 3600), 30)

            if not chats or not text:
                await asyncio.sleep(10)
                continue

            random.shuffle(chats)
            for chat_id in chats:
                try:
                    full_text = text + FOOTER
                    await client.send_message(chat_id, full_text)
                    print(f"[{phone}] sent to {chat_id}")
                except FloodWait as e:
                    print(f"[{phone}] FloodWait {e.value}s")
                    await asyncio.sleep(e.value + random.randint(5, 15))
                except (PeerFlood, UserBannedInChannel, ChatWriteForbidden) as e:
                    print(f"[{phone}] skip chat {chat_id}: {e}")
                    if chat_id in acc.get("selected_chats", []):
                        acc["selected_chats"].remove(chat_id)
                        save_data()
                except Exception as e:
                    print(f"[{phone}] error {chat_id}: {type(e).__name__} {e}")

                delay = random.uniform(3, 8)
                await asyncio.sleep(delay)

            cycle_delay = interval + random.randint(-int(interval*0.1), int(interval*0.15))
            print(f"[{phone}] cycle done, sleep {cycle_delay}s")
            await asyncio.sleep(max(cycle_delay, 30))

        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[{phone}] worker error: {e}")
            await asyncio.sleep(30)

    await client.stop()
    print(f"[{phone}] worker stopped")

@router.message(F.text == "🚀 Старт")
async def start_spam(message: Message):
    data = get_user(message.from_user.id)
    if not data["accounts"]:
        return await message.answer("Нет аккаунтов.")

    started = 0
    for acc in data["accounts"]:
        phone = acc["phone"]
        if phone in data["spam_tasks"]:
            continue
        if not acc.get("selected_chats") or not acc.get("text"):
            continue
        task = asyncio.create_task(spam_worker(message.from_user.id, phone, acc["session"]))
        data["spam_tasks"][phone] = task
        started += 1

    if started == 0:
        return await message.answer("Нет аккаунтов с настроенными чатами и текстом.")
    await message.answer(f"Запущено воркеров: {started}")

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
    lines = []
    for a in accs:
        chats = len(a.get("selected_chats", []))
        interval = a.get("interval", 3600) // 60
        lines.append(f"• {a['phone']} — чатов: {chats}, интервал: {interval} мин")
    await message.answer(f"Аккаунты ({len(accs)}/{MAX_ACCOUNTS}):\n" + "\n".join(lines))

@router.message(F.text == "📊 Статус")
async def status(message: Message):
    data = get_user(message.from_user.id)
    tasks = data.get("spam_tasks", {})
    running = len(tasks)
    lines = [f"Воркеров запущено: {running}\n"]
    for acc in data["accounts"]:
        chats = len(acc.get("selected_chats", []))
        interval = acc.get("interval", 3600) // 60
        txt = acc.get("text", "")
        preview = (txt[:30] + "...") if len(txt) > 30 else (txt or "не задан")
        lines.append(f"{acc['phone']}: чатов {chats}, интервал {interval} мин, текст: {preview}")
    await message.answer("\n".join(lines) if lines else "Нет аккаунтов.")

dp.include_router(router)

async def main():
    load_data()
    print("Bot started")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
