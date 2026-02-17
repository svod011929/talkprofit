"""
chat_bot_aiogram.py — бот TalkProfit с интеграцией BotoHub API для ОП
Автор: @KodoDrive
Версия: 5.0 (Refactored)

Изменения v5.0:
- Клавиатура больше не прилипает (is_persistent=False)
- Удалены все дублирующие обработчики и функции
- Удалены избыточные адаптеры (_is_admin, _load_users и т.д.)
- Добавлен обработчик кнопки «❌ Отмена»
- Улучшена навигация админ-панели (единообразные «Назад»)
- Исправлена двойная проверка chat.type в withdraw_entry
- Улучшены сообщения для пользователя и администратора
- Добавлено подтверждение бана/разбана
- Добавлено отображение кол-ва pending-заявок в меню
- Рассылка с задержкой (BROADCAST_DELAY_SEC) для защиты от лимитов Telegram

Файлы:
- users.json
- chats.json
- withdrawals.json
- settings.json
- broadcasts.json (история рассылок)
- botohub_tokens.json (токены и статус верификации для BotoHub API)
"""

import asyncio
import json
import os
import re
import time
import aiohttp
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List, Tuple
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command, Filter
from aiogram.types import (
    Message,
    CallbackQuery,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    SwitchInlineQueryChosenChat,
)

# =========================================================
# Логирование
# =========================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("botohub_api.log"),
        logging.StreamHandler(),
    ],
)

logger = logging.getLogger(__name__)

# =========================================================
# НАСТРОЙКИ (вписать свои)
# =========================================================
TITLE_NAME_BOT = "⛏️ Talk Profit"

ADMIN_USERNAME = "@KodoDrive"

BOT_TOKEN = "7955370010:AAFXpFY_4_5OTdF7L4WXNizLGBsFfgN_gBo"
ADMIN_ID = 8054710484

# BotoHub API
BOTOHUB_API_URL = "https://botohub.me/get-tasks"
BOTOHUB_TOKEN = "c0bd04da-666e-421d-ad24-5a815b555107"

# Экономика
MESSAGE_PAYMENT = 0.05
MESSAGE_DELAY_SEC = 10

NOTIFY_EVERY_COUNTED_MESSAGES = 10

FIRST_WITHDRAW_MIN_MESSAGES = 1
MIN_WITHDRAW_RUB = 10.00
FIRST_WITHDRAW_MIN_RUB = 10

# Рефералы
REFERRAL_PAYMENT = 10.00
REFERRAL_THRESHOLD = 300

# Лимиты
MAX_WARNINGS = 10

BROADCAST_DELAY_SEC = 0.1

# Верификация: время истечения в секундах (5 часов)
VERIFICATION_EXPIRY_SECONDS = 18000

# =========================================================
# HiViews интеграция
# =========================================================
HIVIEWS_API_KEY = "TL4ZGOHID3ZFN3T7GQNCS"
HIVIEWS_URL = "https://hiviews.net/sendMessage"
HIVIEWS_TIMEOUT_SEC = 10


async def hiviews_send_message(user_id: int, message_id: int, user_first_name: str) -> bool:
    """Отправляет данные на HiViews API. Никогда не ломает основной поток."""
    payload = {
        "UserId": int(user_id),
        "MessageId": int(message_id),
        "UserFirstName": str(user_first_name or ""),
    }
    headers = {
        "Authorization": HIVIEWS_API_KEY,
        "Content-Type": "application/json",
    }
    try:
        timeout = aiohttp.ClientTimeout(total=HIVIEWS_TIMEOUT_SEC)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(HIVIEWS_URL, json=payload, headers=headers) as resp:
                body = await resp.text()
                if 200 <= resp.status < 300:
                    logger.info(f"HiViews OK: user_id={user_id}, status={resp.status}")
                    return True
                logger.warning(f"HiViews HTTP {resp.status}: {body[:300]}")
                return False
    except Exception as e:
        logger.warning(f"HiViews error: {e}")
        return False


# Файлы данных
USERS_FILE = "users.json"
CHATS_FILE = "chats.json"
WITHDRAWALS_FILE = "withdrawals.json"
SETTINGS_FILE = "settings.json"
BROADCASTS_FILE = "broadcasts.json"
BOTOHUB_TOKENS_FILE = "botohub_tokens.json"

# =========================================================
# Глобальные
# =========================================================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

BOT_USERNAME: Optional[str] = None

# Простые состояния без FSM
USER_STATE: Dict[int, Dict[str, Any]] = {}
ADMIN_STATE: Dict[int, Dict[str, Any]] = {}

# @username получателя (CryptoBot)
USERNAME_RE = re.compile(r"^@?[A-Za-z0-9_]{5,32}$")


# =========================================================
# Кастомные фильтры режимов
# =========================================================
class UserMode(Filter):
    def __init__(self, mode: str):
        self.mode = mode

    async def __call__(self, message: Message) -> bool:
        return USER_STATE.get(message.from_user.id, {}).get("mode") == self.mode


class PrivateChatOnly(Filter):
    async def __call__(self, message: Message) -> bool:
        return message.chat.type == "private"


class AdminMode(Filter):
    def __init__(self, mode: str):
        self.mode = mode

    async def __call__(self, message: Message) -> bool:
        return ADMIN_STATE.get(message.from_user.id, {}).get("mode") == self.mode


# =========================================================
# JSON helpers
# =========================================================
def _load_json(path: str, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default
    return default


def _save_json(path: str, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_users() -> Dict[str, Any]:
    return _load_json(USERS_FILE, {})


def save_users(users: Dict[str, Any]) -> None:
    _save_json(USERS_FILE, users)


def load_chats() -> Dict[str, Any]:
    return _load_json(CHATS_FILE, {})


def save_chats(chats: Dict[str, Any]) -> None:
    _save_json(CHATS_FILE, chats)


def load_withdrawals() -> Dict[str, Any]:
    data = _load_json(WITHDRAWALS_FILE, {"seq": 0, "items": {}})
    data.setdefault("seq", 0)
    data.setdefault("items", {})
    return data


def save_withdrawals(data: Dict[str, Any]) -> None:
    _save_json(WITHDRAWALS_FILE, data)


def load_broadcasts() -> Dict[str, Any]:
    data = _load_json(BROADCASTS_FILE, {"seq": 0, "items": {}})
    data.setdefault("seq", 0)
    data.setdefault("items", {})
    return data


def save_broadcasts(data: Dict[str, Any]) -> None:
    _save_json(BROADCASTS_FILE, data)


def load_settings() -> Dict[str, Any]:
    return _load_json(SETTINGS_FILE, {"rate_rub_per_usdt": 80.0})


def save_settings(data: Dict[str, Any]) -> None:
    _save_json(SETTINGS_FILE, data)


def load_botohub_tokens() -> Dict[str, Any]:
    return _load_json(BOTOHUB_TOKENS_FILE, {})


def save_botohub_tokens(data: Dict[str, Any]) -> None:
    _save_json(BOTOHUB_TOKENS_FILE, data)


# =========================================================
# BotoHub API Integration
# =========================================================
async def check_botohub_sponsors(user_id: int) -> Dict[str, Any]:
    """Проверяет доступные спонсоры через BotoHub API."""
    try:
        headers = {"Content-Type": "application/json", "Auth": BOTOHUB_TOKEN}
        payload = {"chat_id": user_id}

        logger.info(f"BotoHub API запрос для user_id={user_id}")

        async with aiohttp.ClientSession() as session:
            async with session.post(
                BOTOHUB_API_URL,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                logger.info(f"Status Code: {response.status}")
                result = await response.json()
                logger.info(f"Response: {json.dumps(result, ensure_ascii=False)}")

                if response.status == 200:
                    return result
                logger.error(f"BotoHub Error: {response.status} - {result}")
                return {"error": f"API Error: {response.status}"}

    except asyncio.TimeoutError:
        logger.error(f"BotoHub API Timeout для user_id={user_id}")
        return {"error": "API Timeout"}
    except aiohttp.ClientError as e:
        logger.error(f"BotoHub API Connection Error: {e}")
        return {"error": str(e)}
    except Exception as e:
        logger.error(f"BotoHub API Error: {e}")
        return {"error": str(e)}


async def send_botohub_verification_message(user_id: int, chat_id: int) -> None:
    """Отправляет пользователю форму верификации с инструкцией."""
    try:
        result = await check_botohub_sponsors(user_id)

        if result.get("error"):
            await bot.send_message(
                user_id,
                f"❌ Ошибка подключения к системе верификации: {result.get('error')}\n\n"
                "Попробуйте позже или обратитесь в поддержку.",
                parse_mode="HTML",
            )
            return

        tasks = result.get("tasks", [])
        completed = result.get("completed", False)
        skip = result.get("skip", False)

        if completed:
            logger.info(f"User {user_id} уже прошёл ОП (completed=true)")
            mark_user_verified(user_id)
            await bot.send_message(
                user_id,
                "✅ Вы уже прошли верификацию!\n\n"
                "Теперь вы можете писать в чатах и зарабатывать.",
                parse_mode="HTML",
            )
            return

        if skip:
            logger.info(f"User {user_id} — нет спонсоров (skip=true)")
            mark_user_verified(user_id)
            await bot.send_message(
                user_id,
                "⏭️ На данный момент нет доступных спонсоров.\n\n"
                "Вы можете писать в чатах.",
                parse_mode="HTML",
            )
            return

        if not tasks:
            logger.warning(f"User {user_id} — нет ссылок в ответе")
            await bot.send_message(
                user_id,
                "⚠️ Ошибка загрузки спонсоров. Попробуйте позже или напишите /verify",
                parse_mode="HTML",
            )
            return

        logger.info(f"User {user_id} — {len(tasks)} спонсоров для показа")

        kb = []
        for i, task_url in enumerate(tasks, 1):
            kb.append([InlineKeyboardButton(text=f"👉 Спонсор {i}", url=task_url)])

        kb.append([InlineKeyboardButton(text="✅ Я подписался на всех!", callback_data="bh_verify")])

        text = (
            "🔐 <b>ВЕРИФИКАЦИЯ</b>\n\n"
            "📋 <b>Что нужно сделать:</b>\n"
            "1️⃣ Нажмите на каждого спонсора ниже\n"
            "2️⃣ Подпишитесь на канал\n"
            "3️⃣ Вернитесь в бот\n"
            "4️⃣ Нажмите «✅ Я подписался на всех!»\n\n"
            f"📊 Спонсоров: <b>{len(tasks)}</b>\n"
            f"⏱️ Займёт ~2-3 минуты\n\n"
            f"💰 После верификации:\n"
            f"• За сообщение: {fmt_money(MESSAGE_PAYMENT)} ₽\n"
            f"• Интервал: {MESSAGE_DELAY_SEC} сек\n"
        )

        await bot.send_message(
            user_id,
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

        tokens = load_botohub_tokens()
        tokens[str(user_id)] = {
            "chat_id": chat_id,
            "verified": False,
            "requested_at": datetime.now().isoformat(timespec="seconds"),
            "verified_at": None,
        }
        save_botohub_tokens(tokens)

    except Exception as e:
        logger.error(f"Error in send_botohub_verification_message: {e}")
        await bot.send_message(user_id, f"❌ Ошибка: {e}", parse_mode="HTML")


def mark_user_verified(user_id: int) -> None:
    """Отмечает пользователя как верифицированного."""
    tokens = load_botohub_tokens()
    now_iso = datetime.now().isoformat(timespec="seconds")
    tokens.setdefault(str(user_id), {})
    tokens[str(user_id)]["verified"] = True
    tokens[str(user_id)]["verified_at"] = now_iso
    save_botohub_tokens(tokens)
    logger.info(f"User {user_id} marked as verified at {now_iso}")


def is_user_verified(user_id: int) -> bool:
    """Проверяет, верифицирован ли пользователь и не истекла ли верификация."""
    tokens = load_botohub_tokens()
    token_data = tokens.get(str(user_id), {})

    if not token_data.get("verified"):
        return False

    verified_at_str = token_data.get("verified_at")
    if not verified_at_str:
        return False

    try:
        verified_at = datetime.fromisoformat(verified_at_str)
        elapsed = (datetime.now() - verified_at).total_seconds()
        is_valid = elapsed < VERIFICATION_EXPIRY_SECONDS

        if not is_valid:
            logger.info(
                f"User {user_id} verification expired ({elapsed:.0f}s / {VERIFICATION_EXPIRY_SECONDS}s)"
            )

        return is_valid
    except Exception as e:
        logger.error(f"Error parsing verified_at for user {user_id}: {e}")
        return False


def get_verification_time_left(user_id: int) -> int:
    """Возвращает оставшееся время верификации в секундах."""
    tokens = load_botohub_tokens()
    token_data = tokens.get(str(user_id), {})

    if not token_data.get("verified"):
        return 0

    verified_at_str = token_data.get("verified_at")
    if not verified_at_str:
        return 0

    try:
        verified_at = datetime.fromisoformat(verified_at_str)
        elapsed = (datetime.now() - verified_at).total_seconds()
        return max(0, VERIFICATION_EXPIRY_SECONDS - int(elapsed))
    except Exception:
        return 0


# =========================================================
# Утилиты
# =========================================================
def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


def fmt_money(x: float) -> str:
    return f"{x:.2f}"


def parse_amount(s: str) -> Optional[float]:
    s = (s or "").replace(",", ".").strip()
    try:
        v = float(s)
        return v if v > 0 else None
    except Exception:
        return None


def normalize_username(s: str) -> Optional[str]:
    s = (s or "").strip()
    if not s or not USERNAME_RE.match(s):
        return None
    if not s.startswith("@"):
        s = "@" + s
    return s


async def ensure_bot_username() -> str:
    global BOT_USERNAME
    if BOT_USERNAME:
        return BOT_USERNAME
    me = await bot.get_me()
    BOT_USERNAME = me.username
    return BOT_USERNAME or ""


def user_is_first_withdraw(u: Dict[str, Any]) -> bool:
    return float(u.get("total_withdrawn", 0.0)) <= 0.0


def user_display_name(message: Message) -> str:
    if message.from_user.username:
        return "@" + message.from_user.username
    return message.from_user.full_name or f"user_{message.from_user.id}"


def _resolve_user_id(ref: str) -> Optional[int]:
    """Принимает user_id или @username, возвращает числовой ID."""
    s = (ref or "").strip()
    if not s:
        return None

    if s.isdigit():
        return int(s)

    if s.startswith("@"):
        s = s[1:]
    s = s.lower().strip()
    if not s:
        return None

    users = load_users()
    for uid_str, u in users.items():
        u = u or {}
        uname = (u.get("username") or "").strip().lstrip("@").lower()
        if uname and uname == s:
            try:
                return int(uid_str)
            except Exception:
                return None

    return None


def _count_pending_withdrawals() -> int:
    """Считает количество pending-заявок на вывод."""
    wd = load_withdrawals()
    return sum(
        1 for item in (wd.get("items") or {}).values()
        if (item or {}).get("status") == "pending"
    )


# =========================================================
# Пользователи / чаты
# =========================================================
def user_defaults(user_id: int, username: str) -> Dict[str, Any]:
    return {
        "user_id": user_id,
        "username": username or f"user_{user_id}",
        "balance": 0.0,
        "total_earned": 0.0,
        "total_withdrawn": 0.0,
        "messages_count": 0,
        "last_message_ts": 0,
        "warnings": 0,
        "is_banned": False,
        "referrer_id": None,
        "referrals": [],
        "referral_earnings": 0.0,
        "reg_date": datetime.now().isoformat(timespec="seconds"),
        "withdraw_username": None,
    }


def ensure_user(user_id: int, username: str) -> Dict[str, Any]:
    users = load_users()
    uid = str(user_id)

    if uid not in users:
        users[uid] = user_defaults(user_id, username)
        save_users(users)
        return users[uid]

    base = user_defaults(user_id, users[uid].get("username") or username)
    changed = False

    for k, v in base.items():
        if k not in users[uid]:
            users[uid][k] = v
            changed = True

    if changed:
        save_users(users)

    return users[uid]


def get_user(user_id: int) -> Optional[Dict[str, Any]]:
    return load_users().get(str(user_id))


def update_user(user_id: int, patch: Dict[str, Any]) -> Dict[str, Any]:
    users = load_users()
    uid = str(user_id)

    if uid not in users:
        users[uid] = user_defaults(user_id, f"user_{user_id}")

    users[uid].update(patch)
    save_users(users)
    return users[uid]


def is_chat_attached(chat_id: int) -> bool:
    return str(chat_id) in load_chats()


def attach_chat(chat_id: int, title: str, username: Optional[str]) -> None:
    chats = load_chats()
    chats[str(chat_id)] = {
        "chat_id": chat_id,
        "title": title or str(chat_id),
        "username": username,
        "attached_at": datetime.now().isoformat(timespec="seconds"),
        "messages_count": int(chats.get(str(chat_id), {}).get("messages_count", 0)),
    }
    save_chats(chats)


def detach_chat(chat_id: int) -> bool:
    chats = load_chats()
    key = str(chat_id)
    if key in chats:
        del chats[key]
        save_chats(chats)
        return True
    return False


def add_referral(new_user_id: int, referrer_id: int) -> None:
    if new_user_id == referrer_id:
        return

    u = get_user(new_user_id)
    r = get_user(referrer_id)

    if not u or not r:
        return

    if u.get("referrer_id") is not None:
        return

    update_user(new_user_id, {"referrer_id": referrer_id})

    users = load_users()
    rid = str(referrer_id)

    users.setdefault(rid, user_defaults(referrer_id, users.get(rid, {}).get("username", f"user_{referrer_id}")))
    users[rid].setdefault("referrals", [])

    if new_user_id not in users[rid]["referrals"]:
        users[rid]["referrals"].append(new_user_id)

    save_users(users)


# =========================================================
# Обработка активности в группе
# =========================================================
async def process_group_activity(user_id: int, chat_id: int) -> Dict[str, Any]:
    user = get_user(user_id)
    if not user:
        return {"status": "ignored"}

    # Проверяем верификацию (ОП)
    if not is_user_verified(user_id) and not is_admin(user_id):
        logger.info(f"User {user_id} не верифицирован — требуем ОП")
        return {"status": "not_verified"}

    # Админ — начисляем без ограничений
    if is_admin(user_id):
        new_balance = float(user.get("balance", 0.0)) + MESSAGE_PAYMENT
        new_earned = float(user.get("total_earned", 0.0)) + MESSAGE_PAYMENT
        new_messages = int(user.get("messages_count", 0)) + 1
        now = int(time.time())

        update_user(user_id, {
            "balance": new_balance,
            "total_earned": new_earned,
            "messages_count": new_messages,
            "last_message_ts": now,
        })

        chats = load_chats()
        ck = str(chat_id)
        if ck in chats:
            chats[ck]["messages_count"] = int(chats[ck].get("messages_count", 0)) + 1
            save_chats(chats)

        milestone = new_messages % NOTIFY_EVERY_COUNTED_MESSAGES == 0
        return {"status": "counted", "messages_count": new_messages, "balance": new_balance, "milestone": milestone}

    if user.get("is_banned"):
        return {"status": "banned"}

    now = int(time.time())
    last_ts = int(user.get("last_message_ts", 0))

    # --- Антифлуд ---
    if now - last_ts < MESSAGE_DELAY_SEC:
        warnings_count = int(user.get("warnings", 0)) + 1
        patch: Dict[str, Any] = {"warnings": warnings_count}
        banned_now = False

        if warnings_count >= MAX_WARNINGS:
            patch["is_banned"] = True
            banned_now = True

        update_user(user_id, patch)

        if banned_now:
            try:
                await bot.send_message(
                    chat_id=user_id,
                    text=(
                        "⛔ <b>Вы заблокированы</b>\n\n"
                        f"Причина: частые сообщения (чаще {MESSAGE_DELAY_SEC} сек).\n"
                        f"Предупреждения: {warnings_count}/{MAX_WARNINGS}.\n\n"
                        "Если это ошибка — напишите администратору."
                    ),
                    parse_mode="HTML",
                )
            except Exception:
                pass
            return {"status": "banned_now", "warnings": warnings_count}

        try:
            await bot.send_message(
                chat_id=user_id,
                text=(
                    f"⚠️ <b>Предупреждение</b>\n\n"
                    f"Минимум {MESSAGE_DELAY_SEC} сек между сообщениями.\n"
                    f"Предупреждения: {warnings_count}/{MAX_WARNINGS}.\n"
                    f"При {MAX_WARNINGS} — блокировка."
                ),
                parse_mode="HTML",
            )
        except Exception:
            pass

        return {"status": "warned", "warnings": warnings_count}

    # --- Начисление ---
    new_balance = float(user.get("balance", 0.0)) + MESSAGE_PAYMENT
    new_earned = float(user.get("total_earned", 0.0)) + MESSAGE_PAYMENT
    new_messages = int(user.get("messages_count", 0)) + 1

    update_user(user_id, {
        "balance": new_balance,
        "total_earned": new_earned,
        "messages_count": new_messages,
        "last_message_ts": now,
    })

    chats = load_chats()
    ck = str(chat_id)
    if ck in chats:
        chats[ck]["messages_count"] = int(chats[ck].get("messages_count", 0)) + 1
        save_chats(chats)

    user2 = get_user(user_id) or {}
    ref_id = user2.get("referrer_id")
    if ref_id and new_messages == REFERRAL_THRESHOLD:
        ref = get_user(int(ref_id))
        if ref:
            rb = float(ref.get("balance", 0.0)) + REFERRAL_PAYMENT
            re_earn = float(ref.get("referral_earnings", 0.0)) + REFERRAL_PAYMENT
            update_user(int(ref_id), {"balance": rb, "referral_earnings": re_earn})

    milestone = new_messages % NOTIFY_EVERY_COUNTED_MESSAGES == 0
    return {"status": "counted", "messages_count": new_messages, "balance": new_balance, "milestone": milestone}


# =========================================================
# Рассылка (Broadcast)
# =========================================================
async def broadcast_to_users(text: str) -> Dict[str, int]:
    users = load_users()
    success = failed = 0
    for uid_str in users:
        try:
            await bot.send_message(int(uid_str), text, parse_mode="HTML", disable_web_page_preview=True)
            success += 1
        except Exception:
            failed += 1
        await asyncio.sleep(BROADCAST_DELAY_SEC)
    return {"success": success, "failed": failed}


async def broadcast_to_chats(text: str) -> Dict[str, int]:
    chats = load_chats()
    success = failed = 0
    for chat_id_str in chats:
        try:
            await bot.send_message(int(chat_id_str), text, parse_mode="HTML", disable_web_page_preview=True)
            success += 1
        except Exception:
            failed += 1
        await asyncio.sleep(BROADCAST_DELAY_SEC)
    return {"success": success, "failed": failed}


# =========================================================
# Клавиатуры (Reply)
#
# КЛЮЧЕВОЕ ИСПРАВЛЕНИЕ:
#   is_persistent=False — клавиатура скрывается при тапе на экран,
#   не залипает и не перекрывает чат.
# =========================================================
def main_kb_user() -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="👤 Профиль"), KeyboardButton(text="📊 Статистика")],
        [KeyboardButton(text="💰 Заработать"), KeyboardButton(text="💳 Вывод")],
        [KeyboardButton(text="👥 Рефералы"), KeyboardButton(text="ℹ️ Инфо")],
        [KeyboardButton(text="🔗 Подключить чат")],
    ]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=False)


def main_kb_admin_root() -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="👤 Профиль"), KeyboardButton(text="📊 Статистика")],
        [KeyboardButton(text="💰 Заработать"), KeyboardButton(text="💳 Вывод")],
        [KeyboardButton(text="👥 Рефералы"), KeyboardButton(text="ℹ️ Инфо")],
        [KeyboardButton(text="⚙️ Админ-панель"), KeyboardButton(text="🔗 Подключить чат")],
    ]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=False)


def root_kb(user_id: int) -> ReplyKeyboardMarkup:
    return main_kb_admin_root() if is_admin(user_id) else main_kb_user()


def admin_panel_kb() -> ReplyKeyboardMarkup:
    pending = _count_pending_withdrawals()
    wd_label = f"💳 Заявки ({pending})" if pending else "💳 Заявки на вывод"
    rows = [
        [KeyboardButton(text="👤 Пользователи"), KeyboardButton(text="⚙️ Настройки")],
        [KeyboardButton(text=wd_label), KeyboardButton(text="📣 Рассылка")],
        [KeyboardButton(text="⬅️ Назад")],
    ]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=False)


def admin_users_kb() -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="💰 Балансы"), KeyboardButton(text="🔎 Поиск пользователя")],
        [KeyboardButton(text="⛔ Блокировки")],
        [KeyboardButton(text="⬅️ Назад в админку")],
    ]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=False)


def admin_user_blocks_kb() -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="🚫 Забанить"), KeyboardButton(text="✅ Разбанить")],
        [KeyboardButton(text="🧹 Сбросить предупреждения")],
        [KeyboardButton(text="⬅️ Назад в Пользователи")],
    ]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=False)


def admin_settings_kb() -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="💱 Курс USDT"), KeyboardButton(text="🔗 Привязанные чаты")],
        [KeyboardButton(text="⬅️ Назад в админку")],
    ]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=False)


def admin_withdrawals_kb() -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="📥 Открыть заявки")],
        [KeyboardButton(text="⬅️ Назад в админку")],
    ]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=False)


def admin_balances_kb() -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="🔄 Обновить статистику")],
        [KeyboardButton(text="👤 Баланс пользователя")],
        [KeyboardButton(text="➕ Начислить пользователю"), KeyboardButton(text="➖ Списать у пользователя")],
        [KeyboardButton(text="🎯 Установить баланс")],
        [KeyboardButton(text="⬅️ Назад в Пользователи")],
    ]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=False)


# =========================================================
# Клавиатуры (Inline)
# =========================================================
def group_bind_inline(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Привязать", callback_data=f"chat_attach:{chat_id}"),
        InlineKeyboardButton(text="❌ Отвязать", callback_data=f"chat_detach:{chat_id}"),
    ]])


def withdrawal_admin_inline(req_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Одобрить", callback_data=f"wd_approve:{req_id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"wd_decline:{req_id}"),
    ]])


# =========================================================
# /start
# =========================================================
@dp.message(CommandStart())
async def on_start(message: Message):
    if message.chat.type != "private":
        return

    await ensure_bot_username()

    username = message.from_user.username or message.from_user.full_name or f"user_{message.from_user.id}"
    u = ensure_user(message.from_user.id, username)

    # HiViews
    try:
        await hiviews_send_message(
            user_id=message.from_user.id,
            message_id=message.message_id,
            user_first_name=message.from_user.first_name or "",
        )
    except Exception:
        pass

    # /start <ref_id>
    if message.text:
        parts = message.text.split(maxsplit=1)
        if len(parts) == 2 and parts[1].isdigit():
            ref_id = int(parts[1])
            if ref_id != message.from_user.id:
                ensure_user(ref_id, f"user_{ref_id}")
                add_referral(message.from_user.id, ref_id)

    rate = float(load_settings().get("rate_rub_per_usdt", 80.0))
    balance = float(u.get("balance", 0.0))
    msg_count = int(u.get("messages_count", 0))
    warnings = int(u.get("warnings", 0))
    banned = bool(u.get("is_banned", False))

    verified = is_user_verified(message.from_user.id)
    time_left = get_verification_time_left(message.from_user.id)

    need = max(0, FIRST_WITHDRAW_MIN_MESSAGES - msg_count)
    progress_pct = (
        min(100, int(msg_count * 100 / FIRST_WITHDRAW_MIN_MESSAGES))
        if FIRST_WITHDRAW_MIN_MESSAGES > 0
        else 100
    )

    status_line = "⛔ Заблокирован" if banned else "✅ Активен"
    verify_line = (
        f"✅ Пройдена (ещё {time_left // 60} мин)"
        if verified
        else "⚠️ Требуется — /verify"
    )

    text = (
        f"✨ <b>Добро пожаловать в {TITLE_NAME_BOT}</b>\n"
        "<blockquote>"
        "Зарабатывайте за активность в чатах: пишите сообщения, проходите верификацию и выводите баланс."
        "</blockquote>\n\n"
        "👤 <b>Ваш профиль</b>\n"
        f"• Баланс: <code>{fmt_money(balance)} ₽</code>\n"
        f"• Сообщений: <code>{msg_count}</code>\n"
        f"• Предупреждений: <code>{warnings}/{MAX_WARNINGS}</code>\n"
        f"• Статус: <code>{status_line}</code>\n"
        f"• Верификация: <code>{verify_line}</code>\n\n"
        "🏁 <b>Первый вывод</b>\n"
        "<blockquote expandable>"
        f"• Нужно сообщений: <code>{FIRST_WITHDRAW_MIN_MESSAGES}</code>\n"
        f"• Осталось: <code>{need}</code>\n"
        f"• Прогресс: <code>{progress_pct}%</code>"
        "</blockquote>\n\n"
        "💬 <b>Заработок</b>\n"
        f"• Оплата: <code>{fmt_money(MESSAGE_PAYMENT)} ₽</code> за сообщение\n"
        f"• Антифлуд: <code>{MESSAGE_DELAY_SEC}</code> сек\n"
        f"• Уведомление: каждые <code>{NOTIFY_EVERY_COUNTED_MESSAGES}</code> сообщений\n\n"
        "🔐 <b>Верификация</b>\n"
        "<blockquote expandable>"
        f"• Повторно каждые <code>{VERIFICATION_EXPIRY_SECONDS // 60} мин</code>\n"
        "• Команда: <code>/verify</code>"
        "</blockquote>\n\n"
        "💳 <b>Вывод</b>\n"
        "<blockquote expandable>"
        f"• Далее минимум: <code>{fmt_money(MIN_WITHDRAW_RUB)} ₽</code>\n"
        f"• Курс: <code>1 USDT = {fmt_money(rate)} ₽</code>"
        "</blockquote>\n\n"
        "<tg-spoiler>Выберите действие в меню ниже.</tg-spoiler>"
    )

    await message.answer(
        text,
        reply_markup=root_kb(message.from_user.id),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


# =========================================================
# /verify
# =========================================================
@dp.message(Command("verify"))
async def on_verify_command(message: Message):
    if message.chat.type != "private":
        await message.answer("📝 Команда /verify работает только в личных сообщениях с ботом.")
        return

    user_id = message.from_user.id

    if is_user_verified(user_id):
        time_left = get_verification_time_left(user_id)
        await message.answer(
            f"✅ Вы уже верифицированы!\n\n"
            f"Действительна ещё {time_left // 60} мин.\n"
            "Когда истечёт — пройдите верификацию снова.",
        )
        return

    await message.answer("🔐 Загружаю форму верификации...")

    try:
        await hiviews_send_message(
            user_id=message.from_user.id,
            message_id=message.message_id,
            user_first_name=message.from_user.first_name or "",
        )
    except Exception:
        pass

    await send_botohub_verification_message(user_id, message.chat.id)


# =========================================================
# /cancel + кнопка «❌ Отмена»
# =========================================================
@dp.message(Command("cancel"))
@dp.message(F.chat.type == "private", F.text == "❌ Отмена")
async def cancel_any(message: Message):
    if message.chat.type != "private":
        return

    had_admin = message.from_user.id in ADMIN_STATE
    had_user = message.from_user.id in USER_STATE

    USER_STATE.pop(message.from_user.id, None)
    ADMIN_STATE.pop(message.from_user.id, None)

    if is_admin(message.from_user.id) and had_admin:
        await message.answer("✅ Отменено.", reply_markup=admin_panel_kb())
        return

    if had_user:
        await message.answer("✅ Отменено.", reply_markup=root_kb(message.from_user.id))
        return

    await message.answer("Нечего отменять.", reply_markup=root_kb(message.from_user.id))


# =========================================================
# Пользовательское меню
# =========================================================
@dp.message(F.chat.type == "private", F.text == "👤 Профиль")
async def profile(message: Message):
    u = ensure_user(
        message.from_user.id,
        message.from_user.username or message.from_user.full_name or "user",
    )

    is_banned = bool(u.get("is_banned"))
    status = "⛔ Заблокирован" if is_banned else "✅ Активен"
    wu = u.get("withdraw_username") or "не указан"

    verified = is_user_verified(message.from_user.id)
    time_left = get_verification_time_left(message.from_user.id)
    verify_info = f"✅ Да (ещё {time_left // 60} мин)" if verified else "⚠️ Нет — /verify"

    is_first = user_is_first_withdraw(u)
    withdraw_rule = (
        f"Первый вывод: после <code>{FIRST_WITHDRAW_MIN_MESSAGES}</code> сообщений"
        if is_first
        else f"Мин. сумма: <code>{fmt_money(MIN_WITHDRAW_RUB)} ₽</code>"
    )

    text = (
        "👤 <b>Профиль</b>\n"
        "<blockquote>Ваши данные, статистика и статус.</blockquote>\n\n"
        "🪪 <b>Данные</b>\n"
        f"• ID: <code>{u['user_id']}</code>\n"
        f"• Имя: <code>{u['username']}</code>\n"
        f"• Регистрация: <code>{str(u.get('reg_date', ''))[:10]}</code>\n"
        f"• Верификация: <code>{verify_info}</code>\n\n"
        "📊 <b>Статистика</b>\n"
        f"• Сообщений: <code>{int(u.get('messages_count', 0))}</code>\n"
        f"• Баланс: <code>{fmt_money(float(u.get('balance', 0.0)))} ₽</code>\n"
        f"• Заработано: <code>{fmt_money(float(u.get('total_earned', 0.0)))} ₽</code>\n"
        f"• Выведено: <code>{fmt_money(float(u.get('total_withdrawn', 0.0)))} ₽</code>\n\n"
        "💳 <b>Вывод</b>\n"
        "<blockquote expandable>"
        f"• {withdraw_rule}\n"
        f"• CryptoBot: <code>{wu}</code>"
        "</blockquote>\n\n"
        "🛡️ <b>Безопасность</b>\n"
        f"• Предупреждений: <code>{int(u.get('warnings', 0))}/{MAX_WARNINGS}</code>\n"
        f"• Статус: <code>{status}</code>"
    )

    await message.answer(text, reply_markup=root_kb(message.from_user.id), parse_mode="HTML")


@dp.message(F.chat.type == "private", F.text == "📊 Статистика")
async def stats(message: Message):
    users = load_users()
    chats = load_chats()

    total_users = len(users)
    total_msgs = sum(int(u.get("messages_count", 0)) for u in users.values())
    total_earned = sum(float(u.get("total_earned", 0.0)) for u in users.values())
    attached_chats = len(chats)

    me = get_user(message.from_user.id) or {}
    me_id = str(message.from_user.id)
    me_msgs = int(me.get("messages_count", 0))
    me_balance = float(me.get("balance", 0.0))
    me_earned = float(me.get("total_earned", 0.0))

    avg_msgs = (total_msgs / total_users) if total_users else 0

    def display_name(u: dict) -> str:
        name = (u.get("username") or "").strip()
        uid = u.get("user_id") or ""
        return name if name else f"user{uid}"

    top_by_msgs = sorted(users.values(), key=lambda u: int(u.get("messages_count", 0)), reverse=True)[:3]
    top_by_earned = sorted(users.values(), key=lambda u: float(u.get("total_earned", 0.0)), reverse=True)[:3]

    top_msgs_lines = "\n".join(
        f"{i+1}) <code>{display_name(u)}</code> — <code>{int(u.get('messages_count', 0))}</code>"
        for i, u in enumerate(top_by_msgs)
        if int(u.get("messages_count", 0)) > 0
    ) or "<i>Пока нет данных</i>"

    top_earned_lines = "\n".join(
        f"{i+1}) <code>{display_name(u)}</code> — <code>{fmt_money(float(u.get('total_earned', 0.0)))} ₽</code>"
        for i, u in enumerate(top_by_earned)
        if float(u.get("total_earned", 0.0)) > 0
    ) or "<i>Пока нет данных</i>"

    # Позиция текущего пользователя
    rank_by_msgs = None
    for idx, (uid, u) in enumerate(
        sorted(users.items(), key=lambda kv: int(kv[1].get("messages_count", 0)), reverse=True),
        start=1,
    ):
        if uid == me_id:
            rank_by_msgs = idx
            break

    rank_by_earned = None
    for idx, (uid, u) in enumerate(
        sorted(users.items(), key=lambda kv: float(kv[1].get("total_earned", 0.0)), reverse=True),
        start=1,
    ):
        if uid == me_id:
            rank_by_earned = idx
            break

    place_msgs = f"#{rank_by_msgs} из {total_users}" if rank_by_msgs else "—"
    place_earned = f"#{rank_by_earned} из {total_users}" if rank_by_earned else "—"

    text = (
        "📊 <b>Статистика</b>\n"
        "<blockquote>Сводка по проекту и ваши показатели.</blockquote>\n\n"
        "🌐 <b>Общая</b>\n"
        f"• Пользователей: <code>{total_users}</code>\n"
        f"• Привязанных чатов: <code>{attached_chats}</code>\n"
        f"• Сообщений: <code>{total_msgs}</code>\n"
        f"• Среднее/польз.: <code>{avg_msgs:.1f}</code>\n"
        f"• Заработано: <code>{fmt_money(total_earned)} ₽</code>\n\n"
        "🏆 <b>ТОП-3 по сообщениям</b>\n"
        "<blockquote expandable>"
        f"{top_msgs_lines}"
        "</blockquote>\n\n"
        "💎 <b>ТОП-3 по заработку</b>\n"
        "<blockquote expandable>"
        f"{top_earned_lines}"
        "</blockquote>\n\n"
        "🙋 <b>Вы</b>\n"
        f"• Сообщения: <code>{me_msgs}</code> (место: <code>{place_msgs}</code>)\n"
        f"• Заработок: <code>{fmt_money(me_earned)} ₽</code> (место: <code>{place_earned}</code>)\n"
        f"• Баланс: <code>{fmt_money(me_balance)} ₽</code>"
    )

    await message.answer(text, reply_markup=root_kb(message.from_user.id), parse_mode="HTML")


@dp.message(F.chat.type == "private", F.text == "💰 Заработать")
async def earn(message: Message):
    verified = is_user_verified(message.from_user.id)
    time_left = get_verification_time_left(message.from_user.id)

    verify_status = (
        f"✅ Активна (ещё {time_left // 60} мин)"
        if verified
        else "⚠️ Не активна — /verify"
    )

    chats = load_chats()

    buttons: list[list[InlineKeyboardButton]] = []
    for c in chats.values():
        uname = (c.get("username") or "").strip()
        if not uname:
            continue
        title = (c.get("title") or uname).strip()
        buttons.append([InlineKeyboardButton(text=title, url=f"https://t.me/{uname}")])

    chats_kb = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    chats_text = (
        "<i>Публичных привязанных чатов пока нет.</i>"
        if not buttons
        else "<i>Нажмите на чат ниже, чтобы перейти.</i>"
    )

    text = (
        "💰 <b>Заработок</b>\n"
        "<blockquote>Условия начислений и верификация.</blockquote>\n\n"
        "🧾 <b>Условия</b>\n"
        f"• Оплата: <code>{fmt_money(MESSAGE_PAYMENT)} ₽</code> за сообщение\n"
        f"• Антифлуд: <code>{MESSAGE_DELAY_SEC}</code> сек\n"
        f"• Уведомление: каждые <code>{NOTIFY_EVERY_COUNTED_MESSAGES}</code> сообщений\n\n"
        "🔐 <b>Верификация</b>\n"
        f"• Статус: <code>{verify_status}</code>\n"
        f"• Повтор каждые <code>{VERIFICATION_EXPIRY_SECONDS // 60} мин</code>\n\n"
        "📌 <b>Привязанные чаты</b>\n"
        f"{chats_text}"
    )

    await message.answer(text, reply_markup=chats_kb, parse_mode="HTML", disable_web_page_preview=True)


@dp.message(F.chat.type == "private", F.text == "👥 Рефералы")
async def referrals(message: Message):
    uname = await ensure_bot_username()
    u = ensure_user(
        message.from_user.id,
        message.from_user.username or message.from_user.full_name or "user",
    )

    invited = len(u.get("referrals", []))
    earned = float(u.get("referral_earnings", 0.0))

    link = f"https://t.me/{uname}?start={u['user_id']}" if uname else f"t.me/?start={u['user_id']}"

    text = (
        "👥 <b>Реферальная программа</b>\n"
        "<blockquote>Приглашайте друзей и получайте бонус.</blockquote>\n\n"
        "🎁 <b>Условия</b>\n"
        f"• Бонус: <code>{fmt_money(REFERRAL_PAYMENT)} ₽</code>\n"
        f"• После <code>{REFERRAL_THRESHOLD}</code> сообщений реферала\n\n"
        "📊 <b>Ваша статистика</b>\n"
        f"• Приглашено: <code>{invited}</code>\n"
        f"• Заработано: <code>{fmt_money(earned)} ₽</code>\n\n"
        "🔗 <b>Ваша ссылка</b>\n"
        f"<code>{link}</code>"
    )

    share_text = f"Присоединяйся: {link}"
    ikb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="📤 Поделиться",
            switch_inline_query_chosen_chat=SwitchInlineQueryChosenChat(
                query=share_text,
                allow_user_chats=True,
                allow_group_chats=True,
                allow_channel_chats=True,
                allow_bot_chats=False,
            ),
        )]
    ])

    await message.answer(text, reply_markup=root_kb(message.from_user.id), parse_mode="HTML", disable_web_page_preview=True)
    await message.answer("Нажмите, чтобы выбрать чат:", reply_markup=ikb, disable_web_page_preview=True)


@dp.message(F.chat.type == "private", F.text == "ℹ️ Инфо")
async def info(message: Message):
    rate = float(load_settings().get("rate_rub_per_usdt", 80.0))
    chats = load_chats()

    buttons: list[list[InlineKeyboardButton]] = []
    for c in chats.values():
        uname = (c.get("username") or "").strip()
        if not uname:
            continue
        title = (c.get("title") or uname).strip()
        buttons.append([InlineKeyboardButton(text=title, url=f"https://t.me/{uname}")])

    chats_kb = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    chats_text = (
        "<i>Публичных привязанных чатов пока нет.</i>"
        if not buttons
        else "<i>Нажмите на чат ниже.</i>"
    )

    text = (
        "ℹ️ <b>Инфо</b>\n"
        "<blockquote>Правила, условия вывода, верификация.</blockquote>\n\n"
        "📌 <b>Правила</b>\n"
        f"• 1 сообщение = <code>{fmt_money(MESSAGE_PAYMENT)} ₽</code>\n"
        f"• Антифлуд: <code>{MESSAGE_DELAY_SEC}</code> сек\n"
        f"• <code>{MAX_WARNINGS}</code> предупреждений = бан\n\n"
        "💳 <b>Вывод</b>\n"
        f"• Первый: после <code>{FIRST_WITHDRAW_MIN_MESSAGES}</code> сообщений\n"
        f"• Далее минимум: <code>{fmt_money(MIN_WITHDRAW_RUB)} ₽</code>\n"
        f"• Курс: <code>1 USDT = {fmt_money(rate)} ₽</code>\n\n"
        "🔐 <b>Верификация</b>\n"
        f"• Повтор каждые <code>{VERIFICATION_EXPIRY_SECONDS // 60} мин</code>\n"
        "• Команда: <code>/verify</code>\n\n"
        "📌 <b>Привязанные чаты</b>\n"
        f"{chats_text}"
    )

    await message.answer(text, reply_markup=chats_kb, parse_mode="HTML", disable_web_page_preview=True)


@dp.message(F.chat.type == "private", F.text == "🔗 Подключить чат")
async def connect_chat_info(message: Message):
    bot_username = await ensure_bot_username()
    add_url = f"https://t.me/{bot_username}?startgroup=true"

    text = (
        "💬 <b>Подключите бота к чату</b>\n"
        "<blockquote>Бот считает сообщения: участники получают вознаграждение.</blockquote>\n\n"
        "✅ <b>Выгода</b>\n"
        "• Больше вовлечённости и живого общения\n"
        "• Для владельца <b>бесплатно</b>\n\n"
        "🔧 <b>Как подключить</b>\n"
        "1) Нажмите кнопку ниже\n"
        "2) Назначьте бота администратором\n"
        f"3) Напишите {ADMIN_USERNAME} для привязки\n"
        "4) Сообщения засчитываются только в привязанных чатах"
    )

    ikb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить бота в чат", url=add_url)]
    ])

    await message.answer(text, reply_markup=ikb, parse_mode="HTML", disable_web_page_preview=True)


# =========================================================
# Вывод (пользователь) — создаём заявку
# =========================================================
@dp.message(F.chat.type == "private", F.text == "💳 Вывод")
async def withdraw_entry(message: Message):
    u = ensure_user(message.from_user.id, message.from_user.username or message.from_user.full_name or "user")

    if u.get("is_banned"):
        await message.answer("⛔ Вы заблокированы и не можете выводить средства.", reply_markup=root_kb(message.from_user.id))
        return

    is_first = user_is_first_withdraw(u)
    messages_count = int(u.get("messages_count", 0))

    if is_first and messages_count < FIRST_WITHDRAW_MIN_MESSAGES:
        need = max(0, FIRST_WITHDRAW_MIN_MESSAGES - messages_count)
        await message.answer(
            "⛔ <b>Первый вывод пока недоступен</b>\n\n"
            f"Нужно сообщений: <code>{FIRST_WITHDRAW_MIN_MESSAGES}</code>\n"
            f"У вас: <code>{messages_count}</code>\n"
            f"Осталось: <code>{need}</code>",
            reply_markup=root_kb(message.from_user.id),
            parse_mode="HTML",
        )
        return

    bal = float(u.get("balance", 0.0))

    if bal <= 0:
        await message.answer("💰 Баланс пуст.", reply_markup=root_kb(message.from_user.id))
        return

    min_amount = FIRST_WITHDRAW_MIN_RUB if is_first else MIN_WITHDRAW_RUB

    USER_STATE[message.from_user.id] = {
        "mode": "withdraw_amount",
        "min_amount": float(min_amount),
        "is_first": bool(is_first),
    }

    prompt = (
        f"💳 {'Первый вывод' if is_first else 'Вывод'}\n\n"
        f"Введите сумму (в ₽).\n"
        f"Доступно: {fmt_money(bal)} ₽\n"
        f"Минимум: {fmt_money(min_amount)} ₽\n\n"
        "Отмена: /cancel"
    )

    await message.answer(prompt, reply_markup=root_kb(message.from_user.id))


@dp.message(F.chat.type == "private", F.text, UserMode("withdraw_amount"))
async def withdraw_amount_input(message: Message):
    uid = message.from_user.id
    st = USER_STATE.get(uid, {})
    min_amount = float(st.get("min_amount", FIRST_WITHDRAW_MIN_RUB))

    amount = parse_amount(message.text)
    u = ensure_user(uid, message.from_user.username or message.from_user.full_name or "user")
    bal = float(u.get("balance", 0.0))

    if amount is None:
        await message.answer("Сумма некорректна. Пример: 10 или 10.5")
        return

    if amount < min_amount:
        await message.answer(f"Минимальная сумма: {fmt_money(min_amount)} ₽")
        return

    if amount > bal:
        await message.answer(f"Недостаточно средств. Баланс: {fmt_money(bal)} ₽")
        return

    USER_STATE[uid] = {"mode": "withdraw_username", "amount_rub": float(amount)}

    prev = u.get("withdraw_username")
    hint = f"\nСохранённый username: {prev}" if prev else ""

    await message.answer(
        "Введите @username для CryptoBot-чека.\n"
        f"Пример: @myusername{hint}\n\n"
        "Отмена: /cancel"
    )


@dp.message(F.chat.type == "private", F.text, UserMode("withdraw_username"))
async def withdraw_username_input(message: Message):
    uid = message.from_user.id
    st = USER_STATE.get(uid, {})
    amount_rub = float(st.get("amount_rub", 0.0))

    wuname = normalize_username(message.text)
    if not wuname:
        await message.answer("Некорректный @username. Пример: @myusername")
        return

    USER_STATE.pop(uid, None)
    update_user(uid, {"withdraw_username": wuname})

    settings = load_settings()
    rate = float(settings.get("rate_rub_per_usdt", 80.0)) or 80.0
    amount_usdt = amount_rub / rate

    wd = load_withdrawals()
    wd["seq"] = int(wd.get("seq", 0)) + 1
    req_id = wd["seq"]

    item = {
        "id": req_id,
        "user_id": uid,
        "amount_rub": amount_rub,
        "rate_rub_per_usdt": rate,
        "amount_usdt": amount_usdt,
        "withdraw_username": wuname,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "status": "pending",
        "check_link": None,
        "admin_note": None,
    }

    wd["items"][str(req_id)] = item
    save_withdrawals(wd)

    await message.answer(
        f"✅ <b>Заявка #{req_id} создана</b>\n\n"
        f"Сумма: {fmt_money(amount_rub)} ₽ ({amount_usdt:.4f} USDT)\n"
        f"Курс: 1 USDT = {fmt_money(rate)} ₽\n"
        f"CryptoBot: {wuname}\n\n"
        "Ожидайте решения администратора.",
        reply_markup=root_kb(uid),
        parse_mode="HTML",
    )

    await bot.send_message(
        ADMIN_ID,
        f"🧾 <b>Новая заявка #{req_id}</b>\n\n"
        f"User ID: <code>{uid}</code>\n"
        f"Сумма: {fmt_money(amount_rub)} ₽ ({amount_usdt:.4f} USDT)\n"
        f"Курс: 1 USDT = {fmt_money(rate)} ₽\n"
        f"CryptoBot: {wuname}",
        reply_markup=withdrawal_admin_inline(req_id),
        parse_mode="HTML",
    )


# =========================================================
# Админ-панель
# =========================================================
@dp.message(F.chat.type == "private", F.text == "⚙️ Админ-панель")
async def admin_panel(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет доступа.", reply_markup=root_kb(message.from_user.id))
        return

    ADMIN_STATE.pop(message.from_user.id, None)

    rate = float(load_settings().get("rate_rub_per_usdt", 80.0))
    pending = _count_pending_withdrawals()
    users_count = len(load_users())
    chats_count = len(load_chats())

    text = (
        "⚙️ <b>Админ-панель</b>\n\n"
        f"👤 Пользователей: <code>{users_count}</code>\n"
        f"💬 Привязанных чатов: <code>{chats_count}</code>\n"
        f"💳 Заявок (pending): <code>{pending}</code>\n"
        f"💱 Курс: <code>1 USDT = {fmt_money(rate)} ₽</code>\n\n"
        "Выберите раздел:"
    )

    await message.answer(text, reply_markup=admin_panel_kb(), parse_mode="HTML")


# =========================================================
# Навигация «Назад»
# =========================================================
@dp.message(F.chat.type == "private", F.text == "⬅️ Назад")
async def nav_back_to_main(message: Message):
    ADMIN_STATE.pop(message.from_user.id, None)
    USER_STATE.pop(message.from_user.id, None)
    await message.answer("Главное меню.", reply_markup=root_kb(message.from_user.id))


@dp.message(F.chat.type == "private", F.text == "⬅️ Назад в админку")
async def nav_back_to_admin(message: Message):
    if not is_admin(message.from_user.id):
        return
    ADMIN_STATE.pop(message.from_user.id, None)
    await message.answer("⚙️ <b>Админ-панель</b>", reply_markup=admin_panel_kb(), parse_mode="HTML")


@dp.message(F.chat.type == "private", F.text == "⬅️ Назад в Пользователи")
async def nav_back_to_users(message: Message):
    if not is_admin(message.from_user.id):
        return
    ADMIN_STATE.pop(message.from_user.id, None)
    await message.answer("👤 <b>Пользователи</b>", reply_markup=admin_users_kb(), parse_mode="HTML")


@dp.message(F.chat.type == "private", F.text == "⬅️ Назад в Настройки")
async def nav_back_to_settings(message: Message):
    if not is_admin(message.from_user.id):
        return
    ADMIN_STATE.pop(message.from_user.id, None)
    await message.answer("⚙️ <b>Настройки</b>", reply_markup=admin_settings_kb(), parse_mode="HTML")


@dp.message(F.chat.type == "private", F.text == "⬅️ Назад в Заявки")
async def nav_back_to_withdrawals(message: Message):
    if not is_admin(message.from_user.id):
        return
    ADMIN_STATE.pop(message.from_user.id, None)
    await message.answer("💳 <b>Заявки на вывод</b>", reply_markup=admin_withdrawals_kb(), parse_mode="HTML")


# =========================================================
# Админ: Пользователи
# =========================================================
@dp.message(F.chat.type == "private", F.text == "👤 Пользователи")
async def admin_users_menu(message: Message):
    if not is_admin(message.from_user.id):
        return
    ADMIN_STATE.pop(message.from_user.id, None)

    users = load_users()
    banned_count = sum(1 for u in users.values() if u.get("is_banned"))

    await message.answer(
        "👤 <b>Пользователи</b>\n\n"
        f"Всего: <code>{len(users)}</code>\n"
        f"Забанено: <code>{banned_count}</code>\n\n"
        "Выберите действие:",
        reply_markup=admin_users_kb(),
        parse_mode="HTML",
    )


@dp.message(F.chat.type == "private", F.text == "🔎 Поиск пользователя")
async def admin_user_lookup_menu(message: Message):
    if not is_admin(message.from_user.id):
        return
    ADMIN_STATE[message.from_user.id] = {"mode": "user_lookup"}
    await message.answer(
        "🔎 Отправьте <code>user_id</code> или <code>@username</code>.\n"
        "Отмена: /cancel",
        reply_markup=admin_users_kb(),
        parse_mode="HTML",
    )


@dp.message(F.chat.type == "private", F.text, AdminMode("user_lookup"))
async def admin_user_lookup_input(message: Message):
    if not is_admin(message.from_user.id):
        return

    uid = _resolve_user_id(message.text or "")
    if not uid:
        await message.answer("❌ Не удалось определить пользователя.", parse_mode="HTML")
        return

    u = get_user(uid)
    if not u:
        await message.answer("❌ Пользователь не найден.", reply_markup=admin_users_kb())
        ADMIN_STATE.pop(message.from_user.id, None)
        return

    bal = float(u.get("balance", 0.0))
    msgs = int(u.get("messages_count", 0))
    earned = float(u.get("total_earned", 0.0))
    withdrawn = float(u.get("total_withdrawn", 0.0))
    warnings = int(u.get("warnings", 0))
    banned = bool(u.get("is_banned", False))
    reg = u.get("reg_date")
    wuname = u.get("withdraw_username")

    verified = is_user_verified(uid)
    vtime = get_verification_time_left(uid)
    v_info = f"✅ Да ({vtime // 60} мин)" if verified else "❌ Нет"

    text = (
        "👤 <b>Карточка пользователя</b>\n\n"
        f"ID: <code>{uid}</code>\n"
        f"Username: <code>{u.get('username')}</code>\n"
        f"Баланс: <code>{fmt_money(bal)} ₽</code>\n"
        f"Сообщений: <code>{msgs}</code>\n"
        f"Заработано: <code>{fmt_money(earned)} ₽</code>\n"
        f"Выведено: <code>{fmt_money(withdrawn)} ₽</code>\n"
        f"Предупреждений: <code>{warnings}/{MAX_WARNINGS}</code>\n"
        f"Бан: <code>{'да' if banned else 'нет'}</code>\n"
        f"Верификация: <code>{v_info}</code>\n"
        f"CryptoBot: <code>{wuname or '—'}</code>\n"
        f"Регистрация: <code>{reg}</code>"
    )

    ADMIN_STATE.pop(message.from_user.id, None)
    await message.answer(text, reply_markup=admin_users_kb(), parse_mode="HTML")


# =========================================================
# Админ: Блокировки
# =========================================================
@dp.message(F.chat.type == "private", F.text == "⛔ Блокировки")
async def admin_blocks_menu(message: Message):
    if not is_admin(message.from_user.id):
        return
    ADMIN_STATE.pop(message.from_user.id, None)
    await message.answer(
        "⛔ <b>Блокировки</b>\n\n"
        "Действия по <code>user_id</code> или <code>@username</code>.\n"
        "Отмена: /cancel",
        reply_markup=admin_user_blocks_kb(),
        parse_mode="HTML",
    )


@dp.message(F.chat.type == "private", F.text == "🚫 Забанить")
async def admin_ban_mode(message: Message):
    if not is_admin(message.from_user.id):
        return
    ADMIN_STATE[message.from_user.id] = {"mode": "ban_user"}
    await message.answer(
        "Введите <code>user_id</code> или <code>@username</code> для бана.\nОтмена: /cancel",
        reply_markup=admin_user_blocks_kb(),
        parse_mode="HTML",
    )


@dp.message(F.chat.type == "private", F.text == "✅ Разбанить")
async def admin_unban_mode(message: Message):
    if not is_admin(message.from_user.id):
        return
    ADMIN_STATE[message.from_user.id] = {"mode": "unban_user"}
    await message.answer(
        "Введите <code>user_id</code> или <code>@username</code> для разбана.\nОтмена: /cancel",
        reply_markup=admin_user_blocks_kb(),
        parse_mode="HTML",
    )


@dp.message(F.chat.type == "private", F.text == "🧹 Сбросить предупреждения")
async def admin_reset_warn_mode(message: Message):
    if not is_admin(message.from_user.id):
        return
    ADMIN_STATE[message.from_user.id] = {"mode": "reset_warnings"}
    await message.answer(
        "Введите <code>user_id</code> или <code>@username</code> для сброса.\nОтмена: /cancel",
        reply_markup=admin_user_blocks_kb(),
        parse_mode="HTML",
    )


@dp.message(F.chat.type == "private", F.text, AdminMode("ban_user"))
async def admin_ban_input(message: Message):
    if not is_admin(message.from_user.id):
        return
    uid = _resolve_user_id(message.text or "")
    if not uid or not get_user(uid):
        await message.answer("❌ Пользователь не найден.", reply_markup=admin_user_blocks_kb())
        return

    update_user(uid, {"is_banned": True})
    ADMIN_STATE.pop(message.from_user.id, None)

    # Уведомляем пользователя
    try:
        await bot.send_message(uid, "⛔ Вы были заблокированы администратором.")
    except Exception:
        pass

    await message.answer(f"✅ Пользователь <code>{uid}</code> забанен.", reply_markup=admin_user_blocks_kb(), parse_mode="HTML")


@dp.message(F.chat.type == "private", F.text, AdminMode("unban_user"))
async def admin_unban_input(message: Message):
    if not is_admin(message.from_user.id):
        return
    uid = _resolve_user_id(message.text or "")
    if not uid or not get_user(uid):
        await message.answer("❌ Пользователь не найден.", reply_markup=admin_user_blocks_kb())
        return

    update_user(uid, {"is_banned": False, "warnings": 0})
    ADMIN_STATE.pop(message.from_user.id, None)

    try:
        await bot.send_message(uid, "✅ Вы разблокированы. Предупреждения сброшены.")
    except Exception:
        pass

    await message.answer(
        f"✅ Пользователь <code>{uid}</code> разбанен, предупреждения сброшены.",
        reply_markup=admin_user_blocks_kb(),
        parse_mode="HTML",
    )


@dp.message(F.chat.type == "private", F.text, AdminMode("reset_warnings"))
async def admin_reset_warn_input(message: Message):
    if not is_admin(message.from_user.id):
        return
    uid = _resolve_user_id(message.text or "")
    if not uid or not get_user(uid):
        await message.answer("❌ Пользователь не найден.", reply_markup=admin_user_blocks_kb())
        return

    update_user(uid, {"warnings": 0})
    ADMIN_STATE.pop(message.from_user.id, None)
    await message.answer(f"✅ Предупреждения <code>{uid}</code> сброшены.", reply_markup=admin_user_blocks_kb(), parse_mode="HTML")


# =========================================================
# Админ: Настройки
# =========================================================
@dp.message(F.chat.type == "private", F.text == "⚙️ Настройки")
async def admin_settings_menu(message: Message):
    if not is_admin(message.from_user.id):
        return
    ADMIN_STATE.pop(message.from_user.id, None)

    rate = float(load_settings().get("rate_rub_per_usdt", 80.0))

    await message.answer(
        "⚙️ <b>Настройки</b>\n\n"
        f"💱 Курс: <code>1 USDT = {fmt_money(rate)} ₽</code>\n"
        f"🔗 Привязанных чатов: <code>{len(load_chats())}</code>",
        reply_markup=admin_settings_kb(),
        parse_mode="HTML",
    )


@dp.message(F.chat.type == "private", F.text == "💱 Курс USDT")
async def admin_usdt_rate_show(message: Message):
    if not is_admin(message.from_user.id):
        return

    rate = float(load_settings().get("rate_rub_per_usdt", 80.0))
    ADMIN_STATE[message.from_user.id] = {"mode": "set_usdt_rate"}
    await message.answer(
        f"💱 <b>Курс USDT</b>\n\n"
        f"Текущий: <code>1 USDT = {fmt_money(rate)} ₽</code>\n\n"
        "Отправьте новый курс (например: <code>92.5</code>).\n"
        "Отмена: /cancel",
        reply_markup=admin_settings_kb(),
        parse_mode="HTML",
    )


@dp.message(F.chat.type == "private", F.text, AdminMode("set_usdt_rate"))
async def admin_usdt_rate_set(message: Message):
    if not is_admin(message.from_user.id):
        return

    val = parse_amount(message.text)
    if val is None:
        await message.answer("❌ Некорректное число. Пример: <code>92.5</code>", parse_mode="HTML")
        return

    st = load_settings()
    old_rate = float(st.get("rate_rub_per_usdt", 80.0))
    st["rate_rub_per_usdt"] = float(val)
    save_settings(st)

    ADMIN_STATE.pop(message.from_user.id, None)
    await message.answer(
        f"✅ Курс обновлён: {fmt_money(old_rate)} → {fmt_money(val)} ₽/USDT",
        reply_markup=admin_settings_kb(),
    )


@dp.message(F.chat.type == "private", F.text == "🔗 Привязанные чаты")
async def admin_attached_chats(message: Message):
    if not is_admin(message.from_user.id):
        return

    chats = load_chats()
    if not chats:
        await message.answer(
            "🔗 Привязанных чатов нет.\n"
            "Привязка: /bind в группе.",
            reply_markup=admin_settings_kb(),
        )
        return

    lines: List[str] = []
    kb_rows: List[List[InlineKeyboardButton]] = []

    for chat_id_str, info in chats.items():
        info = info or {}
        title = info.get("title") or chat_id_str
        uname = info.get("username")
        msgs = int(info.get("messages_count", 0))
        lines.append(
            f"• <code>{chat_id_str}</code> — {title}"
            + (f" (@{uname})" if uname else "")
            + f" [{msgs} msg]"
        )
        kb_rows.append([InlineKeyboardButton(text=f"❌ Отвязать: {title}", callback_data=f"chat_detach:{chat_id_str}")])

    await message.answer(
        "🔗 <b>Привязанные чаты</b>\n\n" + "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
        parse_mode="HTML",
    )


# =========================================================
# Админ: Заявки на вывод
# =========================================================
@dp.message(F.chat.type == "private", F.text.startswith("💳 Заявки"))
async def admin_withdrawals_menu(message: Message):
    if not is_admin(message.from_user.id):
        return
    ADMIN_STATE.pop(message.from_user.id, None)

    pending = _count_pending_withdrawals()

    await message.answer(
        f"💳 <b>Заявки на вывод</b>\n\n"
        f"Pending: <code>{pending}</code>\n\n"
        "Нажмите «📥 Открыть заявки» для просмотра.",
        reply_markup=admin_withdrawals_kb(),
        parse_mode="HTML",
    )


@dp.message(F.chat.type == "private", F.text == "📥 Открыть заявки")
async def admin_open_withdrawals(message: Message):
    if not is_admin(message.from_user.id):
        return

    wd = load_withdrawals()
    items = wd.get("items") or {}

    pending: List[Tuple[int, Dict[str, Any]]] = []
    for req_id_str, item in items.items():
        if (item or {}).get("status") == "pending":
            try:
                pending.append((int(req_id_str), item))
            except Exception:
                pass

    if not pending:
        await message.answer("✅ Pending-заявок нет.", reply_markup=admin_withdrawals_kb())
        return

    pending.sort(key=lambda x: x[0])

    await message.answer(f"📋 Найдено заявок: <b>{len(pending)}</b>", parse_mode="HTML")

    for req_id, item in pending[:50]:
        user_id = item.get("user_id")
        amount_rub = item.get("amount_rub")
        amount_usdt = item.get("amount_usdt")
        wuname = item.get("withdraw_username")
        created = item.get("created_at")

        text = (
            f"💳 <b>Заявка #{req_id}</b>\n"
            f"User ID: <code>{user_id}</code>\n"
            f"Сумма: <code>{amount_rub}</code> ₽ ({amount_usdt:.4f if amount_usdt else '?'} USDT)\n"
            f"CryptoBot: <code>{wuname}</code>\n"
            f"Создано: <code>{created}</code>"
        )
        await message.answer(text, reply_markup=withdrawal_admin_inline(req_id), parse_mode="HTML")


@dp.message(F.chat.type == "private", F.text, AdminMode("await_check_link"))
async def admin_withdraw_check_link(message: Message):
    if not is_admin(message.from_user.id):
        return

    st = ADMIN_STATE.get(message.from_user.id) or {}
    req_id = st.get("req_id")
    if not req_id:
        ADMIN_STATE.pop(message.from_user.id, None)
        await message.answer("❌ Не найден req_id.", reply_markup=admin_withdrawals_kb())
        return

    link = (message.text or "").strip()

    wd = load_withdrawals()
    item = (wd.get("items") or {}).get(str(req_id))
    if not item:
        ADMIN_STATE.pop(message.from_user.id, None)
        await message.answer("❌ Заявка не найдена.", reply_markup=admin_withdrawals_kb())
        return

    item["check_link"] = link
    item["status"] = "approved"
    wd["items"][str(req_id)] = item
    save_withdrawals(wd)

    ADMIN_STATE.pop(message.from_user.id, None)

    user_id = int(item.get("user_id"))
    try:
        await bot.send_message(
            user_id,
            f"✅ <b>Заявка #{req_id} одобрена!</b>\n\nСсылка на чек: {link}",
            parse_mode="HTML",
        )
    except Exception:
        pass

    await message.answer(f"✅ Заявка #{req_id} закрыта, чек отправлен.", reply_markup=admin_withdrawals_kb())


# =========================================================
# Админ: Балансы
# =========================================================
async def _admin_send_balances_stats(message: Message) -> None:
    users = load_users()
    total_users = len(users)
    total_balance = sum(float((u or {}).get("balance", 0.0)) for u in users.values())
    total_earned = sum(float((u or {}).get("total_earned", 0.0)) for u in users.values())
    total_withdrawn = sum(float((u or {}).get("total_withdrawn", 0.0)) for u in users.values())

    await message.answer(
        "💰 <b>Балансы</b>\n\n"
        f"Пользователей: <code>{total_users}</code>\n"
        f"Суммарный баланс: <code>{fmt_money(total_balance)} ₽</code>\n"
        f"Заработано: <code>{fmt_money(total_earned)} ₽</code>\n"
        f"Выведено: <code>{fmt_money(total_withdrawn)} ₽</code>",
        reply_markup=admin_balances_kb(),
        parse_mode="HTML",
    )


@dp.message(F.chat.type == "private", F.text == "💰 Балансы")
async def admin_balances_menu(message: Message):
    if not is_admin(message.from_user.id):
        return
    ADMIN_STATE.pop(message.from_user.id, None)
    await _admin_send_balances_stats(message)


@dp.message(F.chat.type == "private", F.text == "🔄 Обновить статистику")
async def admin_balances_refresh(message: Message):
    if not is_admin(message.from_user.id):
        return
    ADMIN_STATE.pop(message.from_user.id, None)
    await _admin_send_balances_stats(message)


@dp.message(F.chat.type == "private", F.text == "👤 Баланс пользователя")
async def admin_balance_user_lookup_menu(message: Message):
    if not is_admin(message.from_user.id):
        return
    ADMIN_STATE[message.from_user.id] = {"mode": "bal_lookup_user"}
    await message.answer(
        "Отправьте <code>user_id</code> или <code>@username</code>.\nОтмена: /cancel",
        reply_markup=admin_balances_kb(),
        parse_mode="HTML",
    )


@dp.message(F.chat.type == "private", F.text, AdminMode("bal_lookup_user"))
async def admin_balance_user_lookup_input(message: Message):
    if not is_admin(message.from_user.id):
        return

    uid = _resolve_user_id(message.text or "")
    if not uid:
        await message.answer("❌ Не найден.", parse_mode="HTML")
        return

    u = get_user(uid)
    if not u:
        await message.answer("❌ Пользователь не найден.", reply_markup=admin_balances_kb())
        ADMIN_STATE.pop(message.from_user.id, None)
        return

    bal = float(u.get("balance", 0.0))
    ADMIN_STATE.pop(message.from_user.id, None)

    await message.answer(
        f"👤 <code>{uid}</code> (@{u.get('username', '—')})\n"
        f"Баланс: <code>{fmt_money(bal)} ₽</code>",
        reply_markup=admin_balances_kb(),
        parse_mode="HTML",
    )


@dp.message(F.chat.type == "private", F.text.in_({"➕ Начислить пользователю", "➖ Списать у пользователя", "🎯 Установить баланс"}))
async def admin_balance_op_menu(message: Message):
    if not is_admin(message.from_user.id):
        return

    action = {
        "➕ Начислить пользователю": "add",
        "➖ Списать у пользователя": "sub",
        "🎯 Установить баланс": "set",
    }.get(message.text)

    ADMIN_STATE[message.from_user.id] = {"mode": "bal_op_user", "action": action}

    await message.answer(
        "Шаг 1/2: отправьте <code>user_id</code> или <code>@username</code>.\n"
        "Отмена: /cancel",
        reply_markup=admin_balances_kb(),
        parse_mode="HTML",
    )


@dp.message(F.chat.type == "private", F.text, AdminMode("bal_op_user"))
async def admin_balance_op_user_input(message: Message):
    if not is_admin(message.from_user.id):
        return

    st = ADMIN_STATE.get(message.from_user.id, {})
    action = st.get("action")

    uid = _resolve_user_id(message.text or "")
    if not uid:
        await message.answer("❌ Не найден.", parse_mode="HTML")
        return

    u = get_user(uid)
    if not u:
        await message.answer("❌ Пользователь не найден.", reply_markup=admin_balances_kb())
        return

    ADMIN_STATE[message.from_user.id] = {"mode": "bal_op_amount", "action": action, "uid": uid}

    cur = float(u.get("balance", 0.0))
    op_label = {"add": "Начислить", "sub": "Списать", "set": "Установить"}.get(action, "Операция")

    await message.answer(
        f"💳 <b>{op_label}</b>\n\n"
        f"Пользователь: <code>{uid}</code> (@{u.get('username', '—')})\n"
        f"Текущий баланс: <code>{fmt_money(cur)} ₽</code>\n\n"
        "Шаг 2/2: отправьте сумму.\nОтмена: /cancel",
        reply_markup=admin_balances_kb(),
        parse_mode="HTML",
    )


@dp.message(F.chat.type == "private", F.text, AdminMode("bal_op_amount"))
async def admin_balance_op_amount_input(message: Message):
    if not is_admin(message.from_user.id):
        return

    st = ADMIN_STATE.get(message.from_user.id, {})
    action = st.get("action")
    uid = int(st.get("uid") or 0)

    amount = parse_amount(message.text or "")
    if amount is None:
        await message.answer("❌ Некорректная сумма.", parse_mode="HTML")
        return

    u = get_user(uid) or {}
    cur = float(u.get("balance", 0.0))

    if action == "add":
        new_bal = cur + amount
    elif action == "sub":
        new_bal = max(0.0, cur - amount)
    elif action == "set":
        new_bal = amount
    else:
        ADMIN_STATE.pop(message.from_user.id, None)
        await message.answer("❌ Неизвестная операция.", reply_markup=admin_balances_kb())
        return

    update_user(uid, {"balance": float(new_bal)})
    ADMIN_STATE.pop(message.from_user.id, None)

    await message.answer(
        f"✅ Готово.\n\n"
        f"User: <code>{uid}</code>\n"
        f"Было: <code>{fmt_money(cur)} ₽</code>\n"
        f"Стало: <code>{fmt_money(new_bal)} ₽</code>",
        reply_markup=admin_balances_kb(),
        parse_mode="HTML",
    )


# =========================================================
# Админ: Рассылка
# =========================================================
@dp.message(F.chat.type == "private", F.text == "📣 Рассылка")
async def admin_broadcast_menu(message: Message):
    if not is_admin(message.from_user.id):
        return

    users_count = len(load_users())
    chats_count = len(load_chats())

    ADMIN_STATE[message.from_user.id] = {"mode": "broadcast_wait_text"}
    await message.answer(
        "📣 <b>Рассылка</b>\n\n"
        f"Будет отправлено: {users_count} юзерам + {chats_count} чатам.\n\n"
        "Отправьте текст рассылки одним сообщением.\n"
        "Отмена: /cancel",
        reply_markup=admin_panel_kb(),
        parse_mode="HTML",
    )


@dp.message(F.chat.type == "private", F.text, AdminMode("broadcast_wait_text"))
async def admin_broadcast_input(message: Message):
    if not is_admin(message.from_user.id):
        return

    text = message.text or ""
    ADMIN_STATE.pop(message.from_user.id, None)

    started = time.perf_counter()

    # Отправляем юзерам
    users = load_users()
    ok_u = fail_u = 0
    for uid_str in users:
        try:
            await bot.send_message(int(uid_str), text, disable_web_page_preview=True)
            ok_u += 1
        except Exception:
            fail_u += 1
        await asyncio.sleep(BROADCAST_DELAY_SEC)

    # Отправляем в чаты
    chats = load_chats()
    ok_c = fail_c = 0
    for chat_id_str in chats:
        try:
            await bot.send_message(int(chat_id_str), text, disable_web_page_preview=True)
            ok_c += 1
        except Exception:
            fail_c += 1
        await asyncio.sleep(BROADCAST_DELAY_SEC)

    elapsed = time.perf_counter() - started

    total_all = ok_u + fail_u + ok_c + fail_c
    ok_all = ok_u + ok_c
    success_pct = int((ok_all / total_all) * 100) if total_all else 0

    bar_len = 10
    filled = max(0, min(bar_len, int(round((success_pct / 100) * bar_len))))
    progress_bar = "█" * filled + "░" * (bar_len - filled)

    await message.answer(
        "📣 <b>Рассылка завершена</b>\n\n"
        f"📬 Доставлено: <code>{ok_all}</code>, ошибок: <code>{ok_u + ok_c - ok_all + fail_u + fail_c}</code>\n"
        f"📈 Успешность: <code>{progress_bar} {success_pct}%</code>\n"
        f"⏱ Время: <code>{elapsed:.1f} сек</code>\n\n"
        f"👤 Юзеры: {ok_u}✅ {fail_u}❌\n"
        f"💬 Чаты: {ok_c}✅ {fail_c}❌",
        reply_markup=admin_panel_kb(),
        parse_mode="HTML",
    )


# =========================================================
# Callback handlers
# =========================================================
@dp.callback_query(F.data.startswith("chat_attach:"))
async def cb_chat_attach(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Нет доступа", show_alert=True)
        return

    try:
        chat_id = int(call.data.split(":", 1)[1])
    except Exception:
        await call.answer("Ошибка данных", show_alert=True)
        return

    chat = await bot.get_chat(chat_id)
    attach_chat(chat_id, chat.title or str(chat_id), chat.username)

    await call.answer("✅ Чат привязан", show_alert=False)
    await call.message.edit_text(f"✅ Чат <b>{chat.title}</b> привязан к боту.", parse_mode="HTML")


@dp.callback_query(F.data.startswith("chat_detach:") | F.data.startswith("chatdetach:"))
async def cb_chat_detach(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Нет доступа", show_alert=True)
        return

    try:
        chat_id = int(call.data.split(":", 1)[1])
    except Exception:
        await call.answer("Ошибка данных", show_alert=True)
        return

    chats = load_chats()
    key = str(chat_id)
    title = (chats.get(key) or {}).get("title", str(chat_id))

    if detach_chat(chat_id):
        await call.answer("✅ Чат отвязан", show_alert=False)
        try:
            await call.message.edit_text(f"✅ Чат <b>{title}</b> отвязан.", parse_mode="HTML")
        except Exception:
            pass
    else:
        await call.answer("Чат не найден", show_alert=True)


@dp.callback_query(F.data == "bh_verify")
async def cb_bh_verify(call: CallbackQuery):
    """Проверка верификации (ОП)."""
    user_id = call.from_user.id

    try:
        await call.answer("⏳ Проверяю подписки...", show_alert=False)

        result = await check_botohub_sponsors(user_id)

        if result.get("error"):
            error_msg = result.get("error")
            await call.message.edit_text(
                f"❌ <b>Ошибка проверки</b>\n\n"
                f"<code>{error_msg}</code>\n\n"
                "Попробуйте /verify",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🔄 Попробовать снова", callback_data="bh_verify")]
                ]),
            )
            return

        completed = result.get("completed", False)
        skip = result.get("skip", False)

        if completed or skip:
            mark_user_verified(user_id)

            await call.answer("✅ Верификация пройдена!", show_alert=True)
            await call.message.edit_text(
                "✅ <b>ВЕРИФИКАЦИЯ ПРОЙДЕНА!</b>\n\n"
                "Теперь вы можете писать в привязанные чаты и зарабатывать.\n\n"
                f"💰 За сообщение: +{fmt_money(MESSAGE_PAYMENT)} ₽\n"
                f"⏱ Интервал: {MESSAGE_DELAY_SEC} сек\n"
                f"⏰ Верификация действует {VERIFICATION_EXPIRY_SECONDS // 60} мин\n\n"
                "🚀 Удачи!",
                parse_mode="HTML",
            )

            logger.info(f"User {user_id} successfully verified")

        else:
            await call.answer(
                "⏳ Вы ещё не на всех подписались.\n"
                "Подпишитесь на всех спонсоров и нажмите снова.",
                show_alert=True,
            )

    except Exception as e:
        logger.error(f"Error in cb_bh_verify: {e}")
        await call.answer(f"❌ Ошибка: {e}", show_alert=True)


@dp.callback_query(F.data.startswith("wd_approve:"))
async def cb_wd_approve(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Нет доступа", show_alert=True)
        return

    try:
        req_id = int(call.data.split(":", 1)[1])
    except Exception:
        await call.answer("Ошибка данных", show_alert=True)
        return

    wd = load_withdrawals()
    item = wd["items"].get(str(req_id))

    if not item:
        await call.answer("Заявка не найдена", show_alert=True)
        return

    if item.get("status") != "pending":
        await call.answer(f"Нельзя одобрить (status={item.get('status')})", show_alert=True)
        return

    user_id = int(item["user_id"])
    u = get_user(user_id) or {}
    bal = float(u.get("balance", 0.0))
    amount_rub = float(item["amount_rub"])

    if bal < amount_rub:
        item["status"] = "declined"
        item["admin_note"] = "Недостаточно средств"
        wd["items"][str(req_id)] = item
        save_withdrawals(wd)

        await call.answer("Недостаточно средств", show_alert=True)
        try:
            await bot.send_message(user_id, f"❌ Заявка #{req_id} отклонена (недостаточно средств).")
        except Exception:
            pass
        return

    new_bal = bal - amount_rub
    tw = float(u.get("total_withdrawn", 0.0)) + amount_rub
    update_user(user_id, {"balance": new_bal, "total_withdrawn": tw})

    item["status"] = "approved_wait_check"
    wd["items"][str(req_id)] = item
    save_withdrawals(wd)

    ADMIN_STATE[call.from_user.id] = {"mode": "await_check_link", "req_id": req_id}

    await call.answer("✅ Одобрено", show_alert=False)
    await call.message.edit_text(
        f"✅ Заявка <b>#{req_id}</b> одобрена.\n\n"
        f"Списано: <code>{fmt_money(amount_rub)} ₽</code>\n"
        f"Баланс юзера: <code>{fmt_money(new_bal)} ₽</code>\n\n"
        "📎 Отправьте ссылку на чек:",
        parse_mode="HTML",
    )


@dp.callback_query(F.data.startswith("wd_decline:"))
async def cb_wd_decline(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Нет доступа", show_alert=True)
        return

    try:
        req_id = int(call.data.split(":", 1)[1])
    except Exception:
        await call.answer("Ошибка данных", show_alert=True)
        return

    wd = load_withdrawals()
    item = wd["items"].get(str(req_id))

    if not item:
        await call.answer("Заявка не найдена", show_alert=True)
        return

    item["status"] = "declined"
    wd["items"][str(req_id)] = item
    save_withdrawals(wd)

    user_id = int(item["user_id"])
    try:
        await bot.send_message(user_id, f"❌ Заявка #{req_id} отклонена.")
    except Exception:
        pass

    await call.answer("Отклонено", show_alert=False)
    await call.message.edit_text(f"❌ Заявка <b>#{req_id}</b> отклонена.", parse_mode="HTML")


# =========================================================
# Group handlers
# =========================================================
@dp.message(Command("bind"))
async def on_bind(message: Message):
    if message.chat.type not in ["group", "supergroup"]:
        await message.answer("Эта команда работает только в группах.")
        return

    if not is_admin(message.from_user.id):
        await message.answer("⛔ Доступ запрещен.")
        return

    await message.answer(
        "Нажмите кнопку, чтобы привязать или отвязать этот чат:",
        reply_markup=group_bind_inline(message.chat.id),
    )


@dp.message(F.chat.type.in_(["group", "supergroup"]))
async def on_group_message(message: Message):
    chat_id = message.chat.id

    if not is_chat_attached(chat_id):
        return

    user_id = message.from_user.id
    ensure_user(user_id, message.from_user.username or message.from_user.full_name or f"user_{user_id}")

    result = await process_group_activity(user_id, chat_id)

    logger.info(f"Group msg: user={user_id}, chat={chat_id}, result={result['status']}")

    if result["status"] == "not_verified":
        try:
            await bot.send_message(
                user_id,
                "⚠️ <b>Требуется верификация</b>\n\n"
                "Подпишитесь на спонсоров, чтобы сообщения засчитывались.\n"
                "Форма верификации — ниже.",
                parse_mode="HTML",
            )
            await send_botohub_verification_message(user_id, chat_id)
        except Exception as e:
            logger.error(f"Error sending verification: {e}")

        try:
            await bot.send_message(
                chat_id,
                f"⚠️ @{message.from_user.username or 'пользователь'}, сообщение не засчитано. "
                f"Пройдите верификацию в ЛС бота.",
                reply_to_message_id=message.message_id,
            )
        except Exception:
            pass
        return

    if result["status"] == "counted" and result.get("milestone"):
        display_name_str = user_display_name(message)
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    f"🎉 {display_name_str} — {result['messages_count']} сообщений!\n"
                    f"💰 Баланс: {fmt_money(result['balance'])} ₽"
                ),
                reply_to_message_id=message.message_id,
                parse_mode="HTML",
            )
        except Exception:
            pass


# =========================================================
# Main
# =========================================================
async def main():
    print(f"🤖 {TITLE_NAME_BOT} бот запущен...")
    print(f"Admin ID: {ADMIN_ID}")
    print(f"BotoHub API: {BOTOHUB_API_URL}")
    print(f"Верификация: {VERIFICATION_EXPIRY_SECONDS // 60} мин")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
