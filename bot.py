import asyncio
import hmac
import html as html_lib
import logging
import os
import sqlite3
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import cv2
import numpy as np
import requests
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.filters.chat_member_updated import ChatMemberUpdatedFilter, IS_MEMBER, IS_NOT_MEMBER
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand, BotCommandScopeChat, CallbackQuery, ChatMemberUpdated,
    InlineKeyboardButton, InlineKeyboardMarkup, Message,
)
from google import genai
from google.genai import types

# ==================== SOZLAMALAR ====================
logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("BOT_TOKEN topilmadi! Render -> Environment ga qo'shing.")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY topilmadi! Render -> Environment ga qo'shing.")

ADMIN_ID = int(os.getenv("ADMIN_ID", "5081583283"))
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
if not ADMIN_PASSWORD:
    logging.warning("ADMIN_PASSWORD o'rnatilmagan! Admin panelga kirib bo'lmaydi.")

DB_PATH = "bot_database.db"

ai_client = genai.Client(api_key=GEMINI_API_KEY)
# Model nomlarini Render Environment orqali o'zgartirish mumkin: GEMINI_MODELS="a,b,c"
MODELS = [m.strip() for m in os.getenv(
    "GEMINI_MODELS", "gemini-2.5-flash,gemini-2.0-flash,gemini-2.5-flash-lite"
).split(",") if m.strip()]

user_last_message_time = {}
SPAM_INTERVAL = 1.3

# Admin sessiyalari: token -> yaratilgan vaqt
ACTIVE_ADMIN_SESSIONS = {}
SESSION_TTL = 12 * 3600
SESSIONS_LOCK = threading.Lock()


# ==================== DATABASE ====================
def get_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)


def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT,
        language TEXT DEFAULT 'uz', reputation INTEGER DEFAULT 100)""")
    c.execute("CREATE TABLE IF NOT EXISTS blacklist (domain TEXT PRIMARY KEY)")
    c.execute("""CREATE TABLE IF NOT EXISTS activity_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
        action TEXT, details TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)""")
    c.execute("""CREATE TABLE IF NOT EXISTS stats (
        key TEXT PRIMARY KEY, value INTEGER DEFAULT 0)""")
    c.execute("""CREATE TABLE IF NOT EXISTS pending_blocks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, domain TEXT, reason TEXT,
        status TEXT DEFAULT 'pending',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP)""")
    c.execute("""CREATE TABLE IF NOT EXISTS groups (
        chat_id INTEGER PRIMARY KEY, title TEXT, username TEXT, chat_type TEXT,
        members_count INTEGER DEFAULT 0,
        added_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        is_active INTEGER DEFAULT 1)""")
    c.execute("""CREATE TABLE IF NOT EXISTS dangerous_urls (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        url TEXT, domain TEXT, user_id INTEGER, reason TEXT,
        source TEXT DEFAULT 'link',
        detected_at DATETIME DEFAULT CURRENT_TIMESTAMP)""")
    default_stats = [
        ("checked_count", 0), ("danger_count", 0), ("file_danger_count", 0),
        ("voice_danger_count", 0), ("video_danger_count", 0), ("photo_danger_count", 0),
        ("screenshot_count", 0), ("audit_count", 0), ("groups_count", 0),
    ]
    c.executemany("INSERT OR IGNORE INTO stats (key, value) VALUES (?, ?)", default_stats)
    conn.commit()
    conn.close()


init_db()


def load_stats() -> dict:
    conn = get_conn()
    rows = conn.execute("SELECT key, value FROM stats").fetchall()
    conn.close()
    return {k: v for k, v in rows}


def save_stat(key: str, increment: int = 1):
    conn = get_conn()
    conn.execute("INSERT OR IGNORE INTO stats (key, value) VALUES (?, 0)", (key,))
    conn.execute("UPDATE stats SET value = value + ? WHERE key = ?", (increment, key))
    conn.commit()
    conn.close()


def log_activity(user_id, action, details):
    try:
        conn = get_conn()
        conn.execute("INSERT INTO activity_logs (user_id, action, details) VALUES (?,?,?)",
                     (user_id, action, str(details)[:200]))
        conn.commit()
        conn.close()
    except Exception:
        pass


def add_user(user_id, username, full_name, language=None):
    """Yangi foydalanuvchi qo'shadi. Mavjud bo'lsa tilini O'ZGARTIRMAYDI."""
    conn = get_conn()
    conn.execute(
        """INSERT INTO users (user_id, username, full_name, language) VALUES (?,?,?,?)
           ON CONFLICT(user_id) DO UPDATE SET
           username=excluded.username, full_name=excluded.full_name""",
        (user_id, str(username or "")[:50], str(full_name or "User")[:100], language or "uz"))
    conn.commit()
    conn.close()


def get_user_lang(user_id: int) -> str:
    conn = get_conn()
    row = conn.execute("SELECT language FROM users WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return row[0] if row else "uz"


def set_user_lang(user_id: int, lang: str):
    conn = get_conn()
    conn.execute("UPDATE users SET language = ? WHERE user_id = ?", (lang, user_id))
    conn.commit()
    conn.close()


def update_user_rep(user_id, change):
    conn = get_conn()
    conn.execute("UPDATE users SET reputation = reputation + ? WHERE user_id = ?", (change, user_id))
    conn.commit()
    conn.close()


def add_global_blacklist(domain):
    conn = get_conn()
    conn.execute("INSERT OR IGNORE INTO blacklist (domain) VALUES (?)", (domain.lower(),))
    conn.commit()
    conn.close()


def is_globally_blacklisted(domain):
    conn = get_conn()
    row = conn.execute("SELECT 1 FROM blacklist WHERE domain = ?", (domain.lower(),)).fetchone()
    conn.close()
    return bool(row)


def add_pending_block(user_id: int, domain: str, reason: str) -> int:
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT INTO pending_blocks (user_id, domain, reason) VALUES (?,?,?)",
              (user_id, domain.lower(), reason[:300]))
    conn.commit()
    rid = c.lastrowid
    conn.close()
    return rid


def update_pending_status(request_id: int, status: str):
    conn = get_conn()
    conn.execute("UPDATE pending_blocks SET status = ? WHERE id = ?", (status, request_id))
    conn.commit()
    conn.close()


def _refresh_groups_count(conn):
    count = conn.execute("SELECT COUNT(*) FROM groups WHERE is_active=1").fetchone()[0]
    conn.execute("INSERT OR REPLACE INTO stats (key, value) VALUES ('groups_count', ?)", (count,))
    conn.commit()


def add_or_update_group(chat_id: int, title: str, username: str = None,
                        chat_type: str = "group", members_count: int = 0):
    conn = get_conn()
    conn.execute(
        """INSERT INTO groups (chat_id, title, username, chat_type, members_count, is_active)
           VALUES (?,?,?,?,?,1)
           ON CONFLICT(chat_id) DO UPDATE SET
           title=excluded.title, username=excluded.username,
           chat_type=excluded.chat_type, members_count=excluded.members_count,
           is_active=1""",
        (chat_id, (title or "Nomsiz")[:200], (username or "")[:100], chat_type, members_count or 0))
    conn.commit()
    _refresh_groups_count(conn)
    conn.close()


def deactivate_group(chat_id: int):
    conn = get_conn()
    conn.execute("UPDATE groups SET is_active=0 WHERE chat_id=?", (chat_id,))
    conn.commit()
    _refresh_groups_count(conn)
    conn.close()


def add_dangerous_url(url: str, user_id: int, reason: str, source: str = "link"):
    try:
        domain = url.lower().replace("https://", "").replace("http://", "").replace("www.", "").split("/")[0]
        conn = get_conn()
        conn.execute(
            "INSERT INTO dangerous_urls (url, domain, user_id, reason, source) VALUES (?,?,?,?,?)",
            (url[:500], domain[:200], user_id, (reason or "")[:400], source))
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"add_dangerous_url xato: {e}")


def get_groups_count() -> int:
    conn = get_conn()
    row = conn.execute("SELECT COUNT(*) FROM groups WHERE is_active=1").fetchone()
    conn.close()
    return row[0] if row else 0


def get_all_groups(limit: int = 200):
    conn = get_conn()
    rows = conn.execute(
        "SELECT chat_id, title, username, chat_type, members_count, added_at "
        "FROM groups WHERE is_active=1 ORDER BY added_at DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return rows


def get_all_users(limit: int = 500):
    conn = get_conn()
    rows = conn.execute(
        "SELECT user_id, username, full_name, language, reputation "
        "FROM users ORDER BY user_id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return rows


def get_dangerous_urls(limit: int = 300):
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, url, domain, user_id, reason, source, detected_at "
        "FROM dangerous_urls ORDER BY detected_at DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return rows


stats = load_stats()

# ==================== MULTI-LANGUAGE ====================
TEXTS = {
    "uz": {
        "start": (
            "👋 Assalomu alaykum!\n\n"
            "Men **AI Kiber-Xavfsizlik Botiman**.\n\n"
            "🔹 Havolalar (phishing)\n🔹 Fayllar (.apk, .exe ...)\n"
            "🔹 Ovozli xabarlar (vishing)\n🔹 Rasmlar + QR-kod\n"
            "🔹 Videolar (Deepfake)\n🔹 `/audit` — chuqur tahlil\n"
            "🔹 `/block <domen>` — firibgar saytni bloklash so‘rovi\n"
            "🔹 `/report` — shubhali narsani yuborish\n"
            "🔹 `/lang` — tilni o‘zgartirish\n\nXavfsiz bo‘ling!"
        ),
        "help": (
            "ℹ️ **Qo‘llanma**\n\n"
            "• `/start` — Botni ishga tushirish\n"
            "• `/audit <havola>` — Kiber-audit\n"
            "• `/block <domen>` — Firibgar saytni bloklash so‘rovi\n"
            "• `/report` — Shubhali narsani yuborish\n"
            "• `/lang` — Tilni o‘zgartirish\n"
            "• `/help` — Yordam"
        ),
        "spam": "⚠️ Juda tez-tez yuboryapsiz. Biroz kuting.",
        "apk_danger": "🚨 **XAVFLI FAYL!**\n\nBu `.apk` yoki zararli fayl. **Ochmang va o‘rnatmang!**",
        "voice_danger": "🚨 **Vishing / Voice Cloning** aniqlandi!",
        "photo_danger": "🚨 Rasmda **firibgarlik / xavfli QR** alomatlari bor!",
        "video_danger": "🚨 Videoda **Deepfake yoki soxta video** alomatlari bor!",
        "lang_choose": "🌐 Tilni tanlang:",
        "lang_set": "✅ Til o‘zgartirildi: O‘zbekcha",
        "report": "📢 **Shubhali narsani yuboring**\n\nHavola, rasm, video, fayl yoki ovozli xabarni shu yerga yuboring.",
        "official": "✅ Rasmiy va ishonchli manzil.",
        "blacklist": "🚨 Bu manzil **qora ro‘yxatda**!",
        "no_screenshot": "🔗 Havola qabul qilindi. Skrinshot olinmadi — ehtiyot bo‘ling.",
        "tg_profile": "🔗 **Telegram profil/kanal:** `{clean}`\n\nSkrinshot olinmaydi.\nChuqur tekshirish: `/audit {clean}`",
        "ai_busy": "❌ AI hozir band. 1-2 daqiqadan keyin qayta urinib ko‘ring.",
        "photo_ok": "✅ Rasm tekshirildi.\n\n{result}",
        "voice_ok": "✅ Ovoz xavfsiz.\n\n{result}",
        "video_ok": "✅ Video tekshirildi.\n\n{result}",
        "file_ok": "📄 Fayl: `{name}`\n⚠ Noma’lum manbadan ochmang.",
        "audit_start": "🕵️ **Kiber-Detektiv** ishga tushdi...\n`{target}`\n\nKuting...",
        "audit_result": "🛡️ **AUDIT HISOBOTI**\n\n{result}",
        "block_usage": "❌ Foydalanish: `/block example.com` yoki `/block https://scam-site.uz`",
        "block_already": "ℹ️ Bu domen allaqachon qora ro‘yxatda.",
        "block_sent": "✅ So‘rovingiz qabul qilindi!\n\nDomen: `{domain}`\nAI tekshiruvi o‘tkazildi. Admin tasdiqlashini kutmoqda.",
        "block_not_scam": "ℹ️ AI bu saytni firibgarlik deb topmadi. So‘rov rad etildi.",
        "block_approved": "✅ Admin tasdiqladi!\n\n`{domain}` qora ro‘yxatga qo‘shildi. Rahmat!",
        "block_rejected": "❌ Admin so‘rovni rad etdi.\n\nDomen: `{domain}`",
    },
    "ru": {
        "start": (
            "👋 Здравствуйте!\n\n"
            "Я **AI Кибер-Безопасность Бот**.\n\n"
            "🔹 Ссылки (фишинг)\n🔹 Файлы (.apk, .exe ...)\n"
            "🔹 Голосовые (вишинг)\n🔹 Фото + QR-код\n"
            "🔹 Видео (Deepfake)\n🔹 `/audit` — глубокий анализ\n"
            "🔹 `/block <домен>` — запрос на блокировку мошеннического сайта\n"
            "🔹 `/report` — отправить подозрительное\n"
            "🔹 `/lang` — сменить язык\n\nБудьте в безопасности!"
        ),
        "help": (
            "ℹ️ **Справка**\n\n"
            "• `/start` — Запустить бота\n"
            "• `/audit <ссылка>` — Кибер-аудит\n"
            "• `/block <домен>` — Запрос на блокировку\n"
            "• `/report` — Отправить подозрительное\n"
            "• `/lang` — Сменить язык\n"
            "• `/help` — Помощь"
        ),
        "spam": "⚠️ Слишком часто. Подождите немного.",
        "apk_danger": "🚨 **ОПАСНЫЙ ФАЙЛ!**\n\nЭто `.apk` или вредоносный файл. **Не открывайте!**",
        "voice_danger": "🚨 **Вишинг / Voice Cloning** обнаружен!",
        "photo_danger": "🚨 На фото признаки **мошенничества / опасного QR**!",
        "video_danger": "🚨 На видео признаки **Deepfake или подделки**!",
        "lang_choose": "🌐 Выберите язык:",
        "lang_set": "✅ Язык изменён: Русский",
        "report": "📢 **Отправьте подозрительное**\n\nСсылку, фото, видео, файл или голосовое сообщение.",
        "official": "✅ Официальный и надёжный адрес.",
        "blacklist": "🚨 Этот адрес в **чёрном списке**!",
        "no_screenshot": "🔗 Ссылка принята. Скриншот не получен — будьте осторожны.",
        "tg_profile": "🔗 **Telegram профиль/канал:** `{clean}`\n\nСкриншот недоступен.\nГлубокая проверка: `/audit {clean}`",
        "ai_busy": "❌ AI сейчас перегружен. Попробуйте через 1-2 минуты.",
        "photo_ok": "✅ Фото проверено.\n\n{result}",
        "voice_ok": "✅ Голос безопасен.\n\n{result}",
        "video_ok": "✅ Видео проверено.\n\n{result}",
        "file_ok": "📄 Файл: `{name}`\n⚠️ Не открывайте из неизвестных источников.",
        "audit_start": "🕵️ **Кибер-Детектив** запущен...\n`{target}`\n\nОжидайте...",
        "audit_result": "🛡️ **ОТЧЁТ АУДИТА**\n\n{result}",
        "block_usage": "❌ Использование: `/block example.com`",
        "block_already": "ℹ️ Этот домен уже в чёрном списке.",
        "block_sent": "✅ Ваш запрос принят!\n\nДомен: `{domain}`\nОжидает подтверждения администратора.",
        "block_not_scam": "ℹ AI не считает этот сайт мошенническим. Запрос отклонён.",
        "block_approved": "✅ Админ подтвердил!\n\n`{domain}` добавлен в чёрный список. Спасибо!",
        "block_rejected": "❌ Админ отклонил запрос.\n\nДомен: `{domain}`",
    },
    "en": {
        "start": (
            "👋 Hello!\n\n"
            "I am an **AI Cybersecurity Bot**.\n\n"
            "🔹 Links (phishing)\n🔹 Files (.apk, .exe ...)\n"
            "🔹 Voice messages (vishing)\n🔹 Photos + QR codes\n"
            "🔹 Videos (Deepfake)\n🔹 `/audit` — deep analysis\n"
            "🔹 `/block <domain>` — request to block scam site\n"
            "🔹 `/report` — report suspicious content\n"
            "🔹 `/lang` — change language\n\nStay safe!"
        ),
        "help": (
            "ℹ️ **Help**\n\n"
            "• `/start` — Start the bot\n"
            "• `/audit <link>` — Cyber audit\n"
            "• `/block <domain>` — Request to block scam site\n"
            "• `/report` — Report suspicious content\n"
            "• `/lang` — Change language\n"
            "• `/help` — Help"
        ),
        "spam": "⚠️ Too fast. Please wait a moment.",
        "apk_danger": "🚨 **DANGEROUS FILE!**\n\nThis is an `.apk` or malicious file. **Do not open!**",
        "voice_danger": "🚨 **Vishing / Voice Cloning** detected!",
        "photo_danger": "🚨 Photo shows signs of **scam / dangerous QR**!",
        "video_danger": "🚨 Video shows signs of **Deepfake or fake**!",
        "lang_choose": "🌐 Choose language:",
        "lang_set": "✅ Language changed: English",
        "report": "📢 **Send suspicious content**\n\nLink, photo, video, file or voice message.",
        "official": "✅ Official and trusted address.",
        "blacklist": "🚨 This address is on the **blacklist**!",
        "no_screenshot": "🔗 Link received. Screenshot failed — be careful.",
        "tg_profile": "🔗 **Telegram profile/channel:** `{clean}`\n\nScreenshot not available.\nDeep check: `/audit {clean}`",
        "ai_busy": "❌ AI is currently busy. Try again in 1-2 minutes.",
        "photo_ok": "✅ Photo checked.\n\n{result}",
        "voice_ok": "✅ Voice is safe.\n\n{result}",
        "video_ok": "✅ Video checked.\n\n{result}",
        "file_ok": "📄 File: `{name}`\n⚠️ Do not open from unknown sources.",
        "audit_start": "🕵️ **Cyber Detective** started...\n`{target}`\n\nPlease wait...",
        "audit_result": "🛡️ **AUDIT REPORT**\n\n{result}",
        "block_usage": "❌ Usage: `/block example.com`",
        "block_already": "ℹ️ This domain is already blacklisted.",
        "block_sent": "✅ Your request has been received!\n\nDomain: `{domain}`\nWaiting for admin approval.",
        "block_not_scam": "ℹ️ AI does not consider this site a scam. Request rejected.",
        "block_approved": "✅ Admin approved!\n\n`{domain}` has been added to the blacklist. Thank you!",
        "block_rejected": "❌ Admin rejected the request.\n\nDomain: `{domain}`",
    },
}


def t(user_id: int, key: str, **kwargs) -> str:
    lang = get_user_lang(user_id)
    text = TEXTS.get(lang, TEXTS["uz"]).get(key, TEXTS["uz"].get(key, key))
    try:
        return text.format(**kwargs)
    except Exception:
        return text


OFFICIAL_DOMAINS = {
    "gov.uz", "my.gov.uz", "lex.uz", "cbu.uz", "soliq.uz", "my.soliq.uz",
    "nbu.uz", "agrobank.uz", "kapitalbank.uz", "hamkorbank.uz", "tbcbank.uz",
    "uzcard.uz", "humocard.uz", "click.uz", "payme.uz", "uzum.uz", "uzumbank.uz",
    "kun.uz", "daryo.uz", "gazeta.uz", "beeline.uz", "ucell.uz", "uztelecom.uz",
}

DANGEROUS_EXTENSIONS = {".apk", ".exe", ".bat", ".cmd", ".scr", ".js", ".vbs", ".msi", ".jar", ".com", ".pif", ".hta"}

storage = MemoryStorage()
bot = Bot(token=TOKEN)
dp = Dispatcher(storage=storage)
bot_loop = None


async def set_commands():
    default_cmds = [
        BotCommand(command="start", description="🚀 Start / Boshlash"),
        BotCommand(command="audit", description="🕵️ Cyber Audit"),
        BotCommand(command="block", description="🚫 Block scam site"),
        BotCommand(command="report", description="📢 Report"),
        BotCommand(command="lang", description="🌐 Language / Til"),
        BotCommand(command="help", description="ℹ️ Help / Yordam"),
    ]
    await bot.set_my_commands(default_cmds)
    try:
        admin_cmds = default_cmds + [BotCommand(command="panel", description="🔐 Admin Panel")]
        await bot.set_my_commands(admin_cmds, scope=BotCommandScopeChat(chat_id=ADMIN_ID))
    except Exception as e:
        logging.warning(f"Admin buyruqlarini o'rnatib bo'lmadi: {e}")


# ==================== HELPERS ====================
async def reply_md(message: Message, text: str, reply_markup=None, **kwargs):
    """Markdown bilan yuboradi; AI matni Markdown'ni buzsa, oddiy matn sifatida qayta yuboradi."""
    try:
        return await message.reply(text, parse_mode="Markdown", reply_markup=reply_markup, **kwargs)
    except TelegramBadRequest:
        return await message.reply(text, reply_markup=reply_markup, **kwargs)


async def notify_admin(text: str, reply_markup=None):
    try:
        await bot.send_message(ADMIN_ID, text, parse_mode="Markdown", reply_markup=reply_markup)
    except TelegramBadRequest:
        try:
            await bot.send_message(ADMIN_ID, text, reply_markup=reply_markup)
        except Exception as e:
            logging.error(f"Admin notify xato: {e}")
    except Exception as e:
        logging.error(f"Admin notify xato: {e}")


async def notify_error(context: str, error: str, user_id: int = None):
    msg = f"⚠️ Bot xatosi\n\nJoy: {context}\nXato: {error[:300]}"
    if user_id:
        msg += f"\nUser: {user_id}"
    try:
        await bot.send_message(ADMIN_ID, msg)
    except Exception as e:
        logging.error(f"notify_error xato: {e}")


def extract_url(text: str):
    if not text:
        return None
    for word in text.split():
        clean = word.strip(".,;:!?()[]{}\"'")
        if clean.startswith(("http://", "https://", "www.")):
            return clean
        if "t.me/" in clean.lower() or "telegram.me/" in clean.lower():
            return clean
        if clean.startswith("@") and len(clean) > 1:
            return f"t.me/{clean[1:]}"
        if "." in clean and len(clean) > 3:
            parts = clean.split(".")
            if len(parts) >= 2 and parts[-1].lower() in {
                "uz", "com", "net", "org", "ru", "info", "xyz", "site", "online",
                "me", "io", "co", "tv", "cc", "app", "dev",
            }:
                return clean
    return None


def get_webpage_screenshot(url: str):
    try:
        full = url if url.startswith(("http://", "https://")) else "https://" + url
        r = requests.get(
            "https://api.microlink.io/",
            params={"url": full, "screenshot": "true", "meta": "false", "embed": "screenshot.url"},
            timeout=8)
        data = r.json()
        if data.get("status") == "success":
            return requests.get(data["data"]["screenshot"]["url"], timeout=8).content
    except Exception:
        pass
    return None


def detect_qr(image_bytes: bytes):
    try:
        nparr = np.frombuffer(image_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if img is None:
            return None
        data, _, _ = cv2.QRCodeDetector().detectAndDecode(img)
        return data if data else None
    except Exception:
        return None


def _check_urlhaus(domain: str) -> bool:
    try:
        res = requests.post("https://urlhaus-api.abuse.ch/v1/host/", data={"host": domain}, timeout=4)
        if res.status_code == 200:
            return res.json().get("query_status") == "ok"
    except Exception:
        pass
    return False


async def check_community_blacklists(domain: str) -> bool:
    return await asyncio.to_thread(_check_urlhaus, domain)


async def analyze_with_gemini(prompt: str, data: bytes, mime: str, user_id: int = None) -> str:
    last_error = ""
    for model in MODELS:
        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(
                    ai_client.models.generate_content, model=model,
                    contents=[prompt, types.Part.from_bytes(data=data, mime_type=mime)]),
                timeout=25.0)
            return response.text or ""
        except Exception as e:
            last_error = f"{model}: {e}"
            logging.warning(f"Gemini xato ({model}): {e}")
            await asyncio.sleep(1)
            continue
    await notify_error("analyze_with_gemini", last_error, user_id)
    return f"ERROR: AI band. {last_error[:100]}"


async def text_with_gemini(prompt: str, user_id: int = None) -> str:
    last_error = "Noma'lum"
    for model in MODELS:
        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(ai_client.models.generate_content, model=model, contents=prompt),
                timeout=20.0)
            return response.text or "Bo‘sh javob"
        except Exception as e:
            last_error = f"{model}: {e}"
            logging.warning(f"Gemini xato ({model}): {e}")
            await asyncio.sleep(1)
            continue
    await notify_error("text_with_gemini", last_error, user_id)
    return f"❌ AI hozir band. 1-2 daqiqadan keyin qayta urinib ko‘ring.\n{last_error[:80]}"


# ==================== GROUP TRACKING ====================
@dp.my_chat_member(ChatMemberUpdatedFilter(IS_NOT_MEMBER >> IS_MEMBER))
async def bot_added_to_group(event: ChatMemberUpdated):
    chat = event.chat
    if chat.type in ("group", "supergroup", "channel"):
        try:
            members = 0
            try:
                members = await bot.get_chat_member_count(chat.id)
            except Exception:
                pass
            add_or_update_group(chat.id, chat.title or "Nomsiz guruh", chat.username, chat.type, members)
            log_activity(event.from_user.id if event.from_user else 0, "BOT_ADDED_TO_GROUP", f"{chat.id}|{chat.title}")
            await notify_admin(
                f"➕ Bot yangi guruhga qo'shildi!\n\n"
                f"Nomi: {chat.title}\nID: {chat.id}\nTuri: {chat.type}\n"
                f"Username: @{chat.username or '-'}"
            )
        except Exception as e:
            logging.error(f"bot_added_to_group xato: {e}")


@dp.my_chat_member(ChatMemberUpdatedFilter(IS_MEMBER >> IS_NOT_MEMBER))
async def bot_removed_from_group(event: ChatMemberUpdated):
    chat = event.chat
    if chat.type in ("group", "supergroup", "channel"):
        deactivate_group(chat.id)
        log_activity(event.from_user.id if event.from_user else 0, "BOT_REMOVED_FROM_GROUP", f"{chat.id}|{chat.title}")
        await notify_admin(f"➖ Bot guruhdan chiqarildi\n\nNomi: {chat.title}\nID: {chat.id}")


# ==================== HANDLERS ====================
@dp.message(Command("start"))
async def cmd_start(message: Message):
    lang = message.from_user.language_code or "uz"
    if lang.startswith("ru"):
        lang = "ru"
    elif lang.startswith("en"):
        lang = "en"
    else:
        lang = "uz"
    add_user(message.from_user.id, message.from_user.username, message.from_user.full_name, lang)
    log_activity(message.from_user.id, "START", "start")
    await reply_md(message, t(message.from_user.id, "start"))


@dp.message(Command("help"))
async def cmd_help(message: Message):
    add_user(message.from_user.id, message.from_user.username, message.from_user.full_name)
    await reply_md(message, t(message.from_user.id, "help"))


@dp.message(Command("report"))
async def cmd_report(message: Message):
    add_user(message.from_user.id, message.from_user.username, message.from_user.full_name)
    await reply_md(message, t(message.from_user.id, "report"))


@dp.message(Command("lang"))
async def cmd_lang(message: Message):
    add_user(message.from_user.id, message.from_user.username, message.from_user.full_name)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇺🇿 O‘zbekcha", callback_data="lang_uz")],
        [InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang_ru")],
        [InlineKeyboardButton(text="🇬🇧 English", callback_data="lang_en")],
    ])
    await message.answer(t(message.from_user.id, "lang_choose"), reply_markup=kb)


@dp.callback_query(F.data.in_({"lang_uz", "lang_ru", "lang_en"}))
async def process_lang(callback: CallbackQuery):
    lang = callback.data.split("_")[1]
    add_user(callback.from_user.id, callback.from_user.username, callback.from_user.full_name)
    set_user_lang(callback.from_user.id, lang)
    await callback.message.edit_text(t(callback.from_user.id, "lang_set"))
    await callback.answer()


@dp.callback_query(F.data == "quick_report")
async def process_quick_report(callback: CallbackQuery):
    await callback.message.answer("📢 Shikoyatingiz qabul qilindi. Admin tez orada ko‘rib chiqadi. Rahmat!")
    await callback.answer("Yuborildi!")
    u = callback.from_user
    await notify_admin(f"📢 Yangi shikoyat\nUser: {u.id} (@{u.username or '-'})")


@dp.message(Command("block"))
async def cmd_block(message: Message):
    user = message.from_user
    add_user(user.id, user.username, user.full_name)
    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        await reply_md(message, t(user.id, "block_usage"))
        return

    raw = args[1].strip()
    domain = raw.lower().replace("https://", "").replace("http://", "").replace("www.", "").split("/")[0].strip()

    if not domain or "." not in domain:
        await reply_md(message, t(user.id, "block_usage"))
        return

    if is_globally_blacklisted(domain):
        await message.answer(t(user.id, "block_already"))
        return

    wait = await message.answer("🔍 AI tekshiruv o‘tkazilmoqda...")

    analysis = await text_with_gemini(
        f"Is the website/domain '{domain}' likely a phishing, scam, or fraudulent site? "
        f"Reply ONLY with one word: SCAM or SAFE, then a short reason.",
        user_id=user.id)

    try:
        await wait.delete()
    except Exception:
        pass

    if analysis.startswith("❌"):
        await message.answer(t(user.id, "ai_busy"))
        return

    is_scam = analysis.strip().upper().startswith("SCAM")

    if not is_scam:
        await message.answer(t(user.id, "block_not_scam"))
        log_activity(user.id, "BLOCK_REQUEST_REJECTED_AI", domain)
        return

    request_id = add_pending_block(user.id, domain, analysis)
    log_activity(user.id, "BLOCK_REQUEST", domain)
    await reply_md(message, t(user.id, "block_sent", domain=domain))

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"approve_block_{request_id}"),
        InlineKeyboardButton(text="❌ Rad etish", callback_data=f"reject_block_{request_id}"),
    ]])

    admin_text = (
        f"🚫 Yangi bloklash so‘rovi\n\n"
        f"Domen: {domain}\n"
        f"Foydalanuvchi: {user.id} (@{user.username or '-'})\n"
        f"AI tahlili:\n{analysis[:400]}\n\n"
        f"Tasdiqlaysizmi?"
    )
    try:
        await bot.send_message(ADMIN_ID, admin_text, reply_markup=kb)
    except Exception as e:
        logging.error(f"Admin notify xato: {e}")


def _get_pending(request_id: int):
    conn = get_conn()
    row = conn.execute("SELECT user_id, domain, status FROM pending_blocks WHERE id = ?", (request_id,)).fetchone()
    conn.close()
    return row


@dp.callback_query(F.data.startswith("approve_block_"))
async def approve_block(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Faqat admin!", show_alert=True)
        return

    request_id = int(callback.data.split("_")[-1])
    row = _get_pending(request_id)
    if not row or row[2] != "pending":
        await callback.answer("So‘rov topilmadi yoki allaqachon ko‘rilgan", show_alert=True)
        return

    user_id, domain, _ = row
    add_global_blacklist(domain)
    update_pending_status(request_id, "approved")
    log_activity(ADMIN_ID, "BLOCK_APPROVED", domain)

    await callback.message.edit_text(f"✅ Tasdiqlandi!\n\n{domain} qora ro‘yxatga qo‘shildi.")
    await callback.answer("Tasdiqlandi!")

    try:
        await bot.send_message(user_id, t(user_id, "block_approved", domain=domain), parse_mode="Markdown")
    except Exception:
        pass


@dp.callback_query(F.data.startswith("reject_block_"))
async def reject_block(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Faqat admin!", show_alert=True)
        return

    request_id = int(callback.data.split("_")[-1])
    row = _get_pending(request_id)
    if not row or row[2] != "pending":
        await callback.answer("So‘rov topilmadi yoki allaqachon ko‘rilgan", show_alert=True)
        return

    user_id, domain, _ = row
    update_pending_status(request_id, "rejected")
    log_activity(ADMIN_ID, "BLOCK_REJECTED", domain)

    await callback.message.edit_text(f"❌ Rad etildi\n\n{domain} qora ro‘yxatga qo‘shilmadi.")
    await callback.answer("Rad etildi")

    try:
        await bot.send_message(user_id, t(user_id, "block_rejected", domain=domain), parse_mode="Markdown")
    except Exception:
        pass


@dp.message(Command("audit"))
async def cmd_audit(message: Message):
    user = message.from_user
    add_user(user.id, user.username, user.full_name)
    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        await reply_md(message, "❌ `/audit <havola yoki kanal>`")
        return
    target = args[1].strip()[:200]
    stats["audit_count"] = stats.get("audit_count", 0) + 1
    save_stat("audit_count")
    log_activity(user.id, "AUDIT", target)

    wait_msg = await reply_md(message, t(user.id, "audit_start", target=target))

    user_lang = get_user_lang(user.id)
    lang_name = "Uzbek" if user_lang == "uz" else ("Russian" if user_lang == "ru" else "English")

    try:
        result = await text_with_gemini(
            f"Professional cybersecurity OSINT audit of '{target}'. Write a clear structured report. "
            f"Include risks, legitimacy, and recommendations. "
            f"IMPORTANT: You MUST write the entire report in {lang_name} language.",
            user_id=user.id)
    except Exception as e:
        result = f"❌ Xato: {str(e)[:80]}"
        await notify_error("cmd_audit", str(e), user.id)
    try:
        await wait_msg.delete()
    except Exception:
        pass

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🚨 Shikoyat qilish / Report", callback_data="quick_report")]])

    if result.startswith("ERROR") or result.startswith("❌"):
        await message.answer(result)
    else:
        full = t(user.id, "audit_result", result=result)
        chunks = [full[i:i + 4000] for i in range(0, len(full), 4000)]
        for idx, chunk in enumerate(chunks):
            markup = kb if idx == len(chunks) - 1 else None
            await reply_md(message, chunk, reply_markup=markup)


@dp.message(Command("panel"))
async def cmd_panel(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    base = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:10000")
    await message.answer(
        f"🔐 Himoyalangan Admin Panel\n\nParol orqali kirish uchun:\n{base}/admin",
        disable_web_page_preview=True)


# ---------- MEDIA HANDLERS ----------
@dp.message(F.photo)
async def handle_photo(message: Message):
    user = message.from_user
    add_user(user.id, user.username, user.full_name)
    try:
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        data = (await bot.download_file(file.file_path)).read()

        qr_data = await asyncio.to_thread(detect_qr, data)
        if qr_data:
            lower = qr_data.lower()
            is_danger = any(x in lower for x in ("http", "https", "t.me", "wifi", "begin:wifi"))
            msg = f"📷 QR aniqlandi!\n{qr_data[:200]}\n\n"
            if is_danger:
                stats["photo_danger_count"] = stats.get("photo_danger_count", 0) + 1
                save_stat("photo_danger_count")
                update_user_rep(user.id, -10)
                msg += "⚠️ Ehtiyot!"
                await notify_admin(f"🚨 QR\nUser: {user.id}")
            else:
                msg += "✅ Oddiy QR."
            await message.reply(msg)

        result = await analyze_with_gemini(
            "Analyze this image for phishing/scam/dangerous QR. Start with '🚨 XAVFLI' or '✅ XAVFSIZ'.",
            data, "image/jpeg", user.id)
        if result.startswith("ERROR"):
            await message.reply(f"⚠️ {result}")
            return
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🚨 Shikoyat qilish", callback_data="quick_report")]])
        if any(w in result.upper() for w in ("XAVFLI", "DANGER", "PHISHING", "SCAM")) and "XAVFSIZ" not in result.upper()[:20]:
            stats["photo_danger_count"] = stats.get("photo_danger_count", 0) + 1
            save_stat("photo_danger_count")
            update_user_rep(user.id, -12)
            await message.reply(t(user.id, "photo_danger") + f"\n\n{result}", reply_markup=kb)
            await notify_admin(f"🚨 Rasm\nUser: {user.id}")
        else:
            update_user_rep(user.id, +2)
            await message.reply(t(user.id, "photo_ok", result=result), reply_markup=kb)
    except Exception as e:
        await notify_error("handle_photo", str(e), user.id)
        await message.reply("⚠️ Rasm tahlilida xato.")


@dp.message(F.voice)
async def handle_voice(message: Message):
    user = message.from_user
    add_user(user.id, user.username, user.full_name)
    try:
        file = await bot.get_file(message.voice.file_id)
        data = (await bot.download_file(file.file_path)).read()
        result = await analyze_with_gemini(
            "Analyze this audio for Voice Cloning or vishing. Reply first: DANGER or SAFE.",
            data, "audio/ogg", user.id)
        if result.startswith("ERROR"):
            await message.reply(f"⚠️ {result}")
            return
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🚨 Shikoyat qilish", callback_data="quick_report")]])
        if result.strip().upper().startswith("DANGER"):
            stats["voice_danger_count"] = stats.get("voice_danger_count", 0) + 1
            save_stat("voice_danger_count")
            update_user_rep(user.id, -15)
            await message.reply(t(user.id, "voice_danger") + f"\n\n{result}", reply_markup=kb)
            await notify_admin(f"🚨 Vishing\nUser: {user.id}")
        else:
            update_user_rep(user.id, +2)
            await message.reply(t(user.id, "voice_ok", result=result), reply_markup=kb)
    except Exception as e:
        await notify_error("handle_voice", str(e), user.id)
        await message.reply("⚠️ Ovoz tahlilida xato.")


@dp.message(F.video)
async def handle_video(message: Message):
    user = message.from_user
    add_user(user.id, user.username, user.full_name)
    try:
        if message.video.file_size and message.video.file_size > 18 * 1024 * 1024:
            await message.reply("⚠️ Video juda katta.")
            return
        file = await bot.get_file(message.video.file_id)
        data = (await bot.download_file(file.file_path)).read()
        result = await analyze_with_gemini(
            "Analyze this video for Deepfake or scam. Reply first: DANGER or SAFE.",
            data, "video/mp4", user.id)
        if result.startswith("ERROR"):
            await message.reply(f"⚠️ {result}")
            return
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🚨 Shikoyat qilish", callback_data="quick_report")]])
        if result.strip().upper().startswith("DANGER"):
            stats["video_danger_count"] = stats.get("video_danger_count", 0) + 1
            save_stat("video_danger_count")
            update_user_rep(user.id, -15)
            await message.reply(t(user.id, "video_danger") + f"\n\n{result}", reply_markup=kb)
            await notify_admin(f"🚨 Video\nUser: {user.id}")
        else:
            update_user_rep(user.id, +2)
            await message.reply(t(user.id, "video_ok", result=result), reply_markup=kb)
    except Exception as e:
        await notify_error("handle_video", str(e), user.id)
        await message.reply("⚠️ Video tahlilida xato.")


@dp.message(F.document)
async def handle_document(message: Message):
    user = message.from_user
    add_user(user.id, user.username, user.full_name)
    doc = message.document
    name = (doc.file_name or "").lower()
    if (doc.file_size or 0) > 20 * 1024 * 1024:  # Telegram Bot API yuklab olish limiti ~20MB
        await message.reply("⚠️ Fayl juda katta.")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🚨 Shikoyat qilish", callback_data="quick_report")]])
    if any(name.endswith(ext) for ext in DANGEROUS_EXTENSIONS):
        stats["file_danger_count"] = stats.get("file_danger_count", 0) + 1
        save_stat("file_danger_count")
        update_user_rep(user.id, -20)
        await reply_md(message, t(user.id, "apk_danger"), reply_markup=kb)
        await notify_admin(f"🚨 Fayl: {name}\nUser: {user.id}")
        return

    try:
        file = await bot.get_file(doc.file_id)
        data = (await bot.download_file(file.file_path)).read()
        result = await analyze_with_gemini(
            f"Analyze this document/file named '{doc.file_name}' for malware, phishing, or malicious script. "
            f"Reply first: DANGER or SAFE.",
            data, doc.mime_type or "application/octet-stream", user.id)
        if result.startswith("ERROR"):
            await message.reply(f"⚠️ {result}")
            return
        if result.strip().upper().startswith("DANGER"):
            stats["file_danger_count"] = stats.get("file_danger_count", 0) + 1
            save_stat("file_danger_count")
            update_user_rep(user.id, -20)
            await message.reply(f"🚨 ZARARLI FAYL ANIQLANDI!\n\n{result}", reply_markup=kb)
            await notify_admin(f"🚨 Zararli fayl: {doc.file_name}\nUser: {user.id}")
        else:
            update_user_rep(user.id, +2)
            await reply_md(message, t(user.id, "file_ok", name=doc.file_name) + f"\n\n{result}", reply_markup=kb)
    except Exception as e:
        await notify_error("handle_document", str(e), user.id)
        await message.reply("⚠️ Fayl tahlilida xato.")


@dp.message(F.text & ~F.text.startswith("/"))
async def handle_text(message: Message):
    user = message.from_user
    add_user(user.id, user.username, user.full_name)

    if message.chat.type in ("group", "supergroup", "channel"):
        try:
            members = 0
            try:
                members = await bot.get_chat_member_count(message.chat.id)
            except Exception:
                pass
            add_or_update_group(message.chat.id, message.chat.title or "Nomsiz",
                                message.chat.username, message.chat.type, members)
        except Exception:
            pass

    now = time.time()
    last_time = user_last_message_time.get(user.id, 0)
    if now - last_time < SPAM_INTERVAL:
        await message.reply(t(user.id, "spam"))
        return
    user_last_message_time[user.id] = now

    text = message.text
    url = extract_url(text)

    if not url:
        # Guruhlarda oddiy suhbatga javob bermaymiz
        if message.chat.type != "private":
            return
        user_lang = get_user_lang(user.id)
        lang_name = "Uzbek" if user_lang == "uz" else ("Russian" if user_lang == "ru" else "English")
        try:
            ai_reply = await text_with_gemini(
                f"You are a helpful cybersecurity assistant in a Telegram bot. "
                f"User message: {text}. Reply in {lang_name} language.",
                user_id=user.id)
            await message.reply(ai_reply)
        except Exception as e:
            await notify_error("handle_text", str(e), user.id)
        return

    stats["checked_count"] = stats.get("checked_count", 0) + 1
    save_stat("checked_count")
    log_activity(user.id, "CHECK_URL", url)

    clean_url = url.lower().replace("https://", "").replace("http://", "").replace("www.", "").split("/")[0]

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🚨 Shikoyat qilish / Report", callback_data="quick_report")]])

    if clean_url in OFFICIAL_DOMAINS or any(clean_url.endswith("." + d) for d in OFFICIAL_DOMAINS):
        update_user_rep(user.id, +1)
        await message.reply(t(user.id, "official"), reply_markup=kb)
        return

    is_community_danger = await check_community_blacklists(clean_url)

    if is_globally_blacklisted(clean_url) or is_community_danger:
        stats["danger_count"] = stats.get("danger_count", 0) + 1
        save_stat("danger_count")
        update_user_rep(user.id, -15)
        add_dangerous_url(url, user.id, "Qora ro'yxat / Community blacklist", "blacklist")
        await reply_md(message, t(user.id, "blacklist") + f"\n\n🔗 `{url}`", reply_markup=kb)
        await notify_admin(f"🚨 Qora ro'yxatdagi havola!\nUser: {user.id}\nUrl: {url}")
        return

    if "t.me/" in url.lower() or "telegram.me/" in url.lower() or url.startswith("@"):
        await reply_md(message, t(user.id, "tg_profile", clean=clean_url), reply_markup=kb)
        return

    wait_msg = await message.reply("🔍 Havola tekshirilmoqda...")
    screenshot_bytes = await asyncio.to_thread(get_webpage_screenshot, url)

    if screenshot_bytes:
        stats["screenshot_count"] = stats.get("screenshot_count", 0) + 1
        save_stat("screenshot_count")

        analysis = await analyze_with_gemini(
            f"Analyze this webpage screenshot for URL: '{url}'. Is it a phishing, scam, fake login, "
            f"or fraudulent website? Reply first with: SCAM or SAFE, followed by a concise explanation.",
            screenshot_bytes, "image/png", user.id)

        try:
            await wait_msg.delete()
        except Exception:
            pass

        if analysis.startswith("ERROR"):
            await message.reply(t(user.id, "ai_busy"))
            return

        if analysis.strip().upper().startswith("SCAM"):
            stats["danger_count"] = stats.get("danger_count", 0) + 1
            save_stat("danger_count")
            update_user_rep(user.id, -20)
            add_dangerous_url(url, user.id, analysis[:300], "screenshot")
            await reply_md(message, f"🚨 **PHISHING / FIRIBGARLIK ANIQLANDI!**\n\n🔗 `{url}`\n\n{analysis}", reply_markup=kb)
            await notify_admin(f"🚨 Xavfli havola (Screenshot):\nUser: {user.id}\nUrl: {url}")
        else:
            update_user_rep(user.id, +2)
            await reply_md(message, f"✅ **Xavfsiz ko'rinadi**\n\n🔗 `{url}`\n\n{analysis}", reply_markup=kb)
    else:
        try:
            await wait_msg.delete()
        except Exception:
            pass

        analysis = await text_with_gemini(
            f"Analyze if the URL/domain '{url}' is safe or a phishing/scam site. Reply first with SCAM or SAFE.",
            user_id=user.id)
        if analysis.startswith("❌"):
            await message.reply(t(user.id, "ai_busy"))
            return
        if analysis.strip().upper().startswith("SCAM"):
            stats["danger_count"] = stats.get("danger_count", 0) + 1
            save_stat("danger_count")
            update_user_rep(user.id, -15)
            add_dangerous_url(url, user.id, analysis[:300], "text_ai")
            await reply_md(message, f"🚨 **XAVFLI BO'LISHI MUMKIN!**\n\n🔗 `{url}`\n\n{analysis}", reply_markup=kb)
        else:
            await message.reply(t(user.id, "no_screenshot") + f"\n\n{analysis}", reply_markup=kb)


# ==================== WEB SERVER & SECURITY ====================
async def send_broadcast_message(text: str):
    conn = get_conn()
    users = conn.execute("SELECT user_id FROM users").fetchall()
    conn.close()

    success = 0
    failed = 0
    for u in users:
        try:
            await bot.send_message(u[0], text)
            success += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1
    await notify_admin(f"📢 Xabar yuborish yakunlandi!\n\n✅ Muvaffaqiyatli: {success}\n❌ Xato (bloklaganlar): {failed}")


def esc(value) -> str:
    return html_lib.escape("" if value is None else str(value))


def create_session() -> str:
    token = os.urandom(24).hex()
    with SESSIONS_LOCK:
        now = time.time()
        for k in [k for k, v in ACTIVE_ADMIN_SESSIONS.items() if now - v > SESSION_TTL]:
            ACTIVE_ADMIN_SESSIONS.pop(k, None)
        ACTIVE_ADMIN_SESSIONS[token] = now
    return token


COMMON_CSS = """
    body { font-family: Arial, sans-serif; background: #0f172a; color: #f8fafc; padding: 20px; margin: 0; }
    .card { background: #1e293b; padding: 20px; border-radius: 10px; margin-bottom: 20px; box-shadow: 0 4px 6px rgba(0,0,0,0.3); overflow-x: auto; }
    h1, h2 { color: #38bdf8; }
    table { width: 100%; border-collapse: collapse; margin-top: 10px; }
    th, td { border: 1px solid #334155; padding: 10px; text-align: left; font-size: 14px; word-break: break-all; }
    th { background: #334155; }
    .stat-box { display: inline-block; background: #334155; padding: 15px 20px; border-radius: 8px; margin-right: 10px; margin-bottom: 10px; text-decoration: none; color: #f8fafc; }
    a.stat-box:hover { background: #475569; }
    .stat-box b { color: #38bdf8; font-size: 1.2em; }
    textarea { width: 100%; height: 100px; background: #0f172a; color: #fff; border: 1px solid #334155; padding: 10px; border-radius: 5px; box-sizing: border-box; }
    button { background: #38bdf8; color: #0f172a; border: none; padding: 10px 20px; font-weight: bold; border-radius: 5px; cursor: pointer; margin-top: 10px; }
    button:hover { background: #0ea5e9; }
    .nav { margin-bottom: 20px; }
    .nav a { color: #94a3b8; margin-right: 15px; text-decoration: none; }
    .nav a:hover { color: #38bdf8; }
"""

NAV_HTML = """
    <div class="nav">
        <a href="/admin">Asosiy</a>
        <a href="/admin/users">Foydalanuvchilar</a>
        <a href="/admin/groups">Guruhlar</a>
        <a href="/admin/dangers">Xavfli havolalar</a>
        <a href="/admin/logout">Chiqish</a>
    </div>
"""


def render_page(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html>
<head>
    <title>{esc(title)}</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>{COMMON_CSS}</style>
</head>
<body>
    {NAV_HTML}
    {body}
</body>
</html>"""


LOGIN_HTML = """<!DOCTYPE html>
<html>
<head>
    <title>Admin Login - Cyber Bot</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body { font-family: Arial, sans-serif; background: #0f172a; color: #f8fafc; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }
        .login-card { background: #1e293b; padding: 30px; border-radius: 10px; width: 320px; box-shadow: 0 4px 10px rgba(0,0,0,0.5); text-align: center; }
        input { width: 100%; padding: 10px; margin: 15px 0; background: #0f172a; border: 1px solid #334155; color: #fff; border-radius: 5px; box-sizing: border-box; }
        button { background: #38bdf8; color: #0f172a; border: none; padding: 10px 20px; font-weight: bold; width: 100%; border-radius: 5px; cursor: pointer; }
        button:hover { background: #0ea5e9; }
        h2 { color: #38bdf8; margin-bottom: 10px; }
    </style>
</head>
<body>
    <div class="login-card">
        <h2>🔐 Admin Panel</h2>
        <p style="font-size: 13px; color: #94a3b8;">Xavfsizlik uchun parolni kiriting</p>
        <form action="/admin/login" method="POST">
            <input type="password" name="password" placeholder="Parol..." required>
            <button type="submit">Kirish</button>
        </form>
    </div>
</body>
</html>"""


class SimpleHandler(BaseHTTPRequestHandler):
    # ---------- helpers ----------
    def _session_token(self):
        raw = self.headers.get("Cookie", "")
        if not raw:
            return None
        try:
            cookie = SimpleCookie()
            cookie.load(raw)
            morsel = cookie.get("admin_session")
            return morsel.value if morsel else None
        except Exception:
            return None

    def _is_authenticated(self) -> bool:
        token = self._session_token()
        if not token:
            return False
        with SESSIONS_LOCK:
            created = ACTIVE_ADMIN_SESSIONS.get(token)
            if created is None:
                return False
            if time.time() - created > SESSION_TTL:
                ACTIVE_ADMIN_SESSIONS.pop(token, None)
                return False
        return True

    def _send_html(self, html: str, status: int = 200):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str, extra_headers=None):
        self.send_response(303)
        self.send_header("Location", location)
        for k, v in (extra_headers or []):
            self.send_header(k, v)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _read_form(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length > 100_000:
            return {}
        return parse_qs(self.rfile.read(length).decode("utf-8", errors="replace"))

    # ---------- HEAD ----------
    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---------- POST ----------
    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/admin/login":
            password = self._read_form().get("password", [""])[0]
            if ADMIN_PASSWORD and hmac.compare_digest(password.encode(), ADMIN_PASSWORD.encode()):
                token = create_session()
                is_https = self.headers.get("X-Forwarded-Proto", "") == "https"
                cookie = f"admin_session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_TTL}"
                if is_https:
                    cookie += "; Secure"
                self._redirect("/admin", [("Set-Cookie", cookie)])
            else:
                time.sleep(1)  # brute-force'ni sekinlashtirish
                self._send_html("<h1>❌ Noto'g'ri parol!</h1><a href='/admin'>Qayta urinish</a>", 401)
            return

        if path == "/admin/broadcast":
            if not self._is_authenticated():
                self._send_html("<h1>Forbidden</h1>", 403)
                return
            msg_text = self._read_form().get("message", [""])[0].strip()
            if msg_text and bot_loop is not None:
                asyncio.run_coroutine_threadsafe(send_broadcast_message(msg_text), bot_loop)
                self._send_html("<h1>✅ Xabar yuborish boshlandi!</h1><p><a href='/admin'>Orqaga qaytish</a></p>")
            else:
                self._send_html("<h1>Xabar matni bo'sh bo'lishi mumkin emas!</h1><a href='/admin'>Orqaga</a>", 400)
            return

        self._send_html("Not Found", 404)

    # ---------- GET ----------
    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/") or "/"

        if path in ("/", "/health"):
            self._send_html("🤖 Bot ishlayapti! (Secure AI Cyber-Security Bot 24/7)")
            return

        if not path.startswith("/admin"):
            self._send_html("Not Found", 404)
            return

        if path == "/admin/logout":
            token = self._session_token()
            if token:
                with SESSIONS_LOCK:
                    ACTIVE_ADMIN_SESSIONS.pop(token, None)
            self._redirect("/admin", [("Set-Cookie", "admin_session=; Path=/; Max-Age=0")])
            return

        if not self._is_authenticated():
            if path == "/admin":
                self._send_html(LOGIN_HTML)
            else:
                self._redirect("/admin")
            return

        try:
            if path == "/admin":
                self._page_main()
            elif path == "/admin/users":
                self._page_users()
            elif path == "/admin/groups":
                self._page_groups()
            elif path == "/admin/dangers":
                self._page_dangers()
            else:
                self._send_html("Not Found", 404)
        except Exception as e:
            logging.error(f"Admin sahifa xatosi: {e}")
            self._send_html(f"<h1>Server xatosi</h1><p>{esc(e)}</p>", 500)

    # ---------- pages ----------
    def _page_main(self):
        current_stats = load_stats()
        groups_count = get_groups_count()
        conn = get_conn()
        user_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        blacklist_domains = [r[0] for r in conn.execute("SELECT domain FROM blacklist").fetchall()]
        pendings = conn.execute(
            "SELECT id, user_id, domain, reason, created_at FROM pending_blocks WHERE status='pending'").fetchall()
        danger_urls_count = conn.execute("SELECT COUNT(*) FROM dangerous_urls").fetchone()[0] or 0
        conn.close()

        rows = "".join(
            f"<tr><td>{esc(p[0])}</td><td>{esc(p[1])}</td><td><b>{esc(p[2])}</b></td>"
            f"<td>{esc((p[3] or '')[:80])}</td><td>{esc(p[4])}</td></tr>" for p in pendings)
        bl = esc(", ".join(blacklist_domains)) if blacklist_domains else "Hozircha bo'sh"
        g = current_stats.get

        body = f"""
        <h1>🔐 Himoyalangan Admin Panel</h1>
        <div class="card">
            <h2>📊 Bot Statistikasi</h2>
            <a href="/admin/users" class="stat-box">👥 Foydalanuvchilar<br><b>{user_count}</b></a>
            <a href="/admin/groups" class="stat-box">🏘 Guruhlar<br><b>{groups_count}</b></a>
            <div class="stat-box">🔍 Tekshirilganlar<br><b>{g('checked_count', 0)}</b></div>
            <a href="/admin/dangers" class="stat-box">🚨 Xavfli havolalar<br><b>{g('danger_count', 0)}</b> <small>({danger_urls_count} saqlangan)</small></a>
            <div class="stat-box">📄 Zararli fayllar<br><b>{g('file_danger_count', 0)}</b></div>
            <div class="stat-box">🎤 Ovoz xavfi<br><b>{g('voice_danger_count', 0)}</b></div>
            <div class="stat-box">🎬 Video xavfi<br><b>{g('video_danger_count', 0)}</b></div>
            <div class="stat-box">🖼 Rasm xavfi<br><b>{g('photo_danger_count', 0)}</b></div>
            <div class="stat-box">📸 Skrinshotlar<br><b>{g('screenshot_count', 0)}</b></div>
            <div class="stat-box">🕵️ Auditlar<br><b>{g('audit_count', 0)}</b></div>
        </div>

        <div class="card">
            <h2>📢 Barcha foydalanuvchilarga xabar yuborish (Broadcast)</h2>
            <form action="/admin/broadcast" method="POST">
                <textarea name="message" placeholder="Barcha foydalanuvchilarga yuboriladigan xabarni yozing..." required></textarea><br>
                <button type="submit">Xabarni yuborish 🚀</button>
            </form>
        </div>

        <div class="card">
            <h2>🚫 Tasdiqlashni kutayotgan domenlar ({len(pendings)})</h2>
            <p style="color:#94a3b8">Tasdiqlash/rad etish Telegram'dagi bot xabaridagi tugmalar orqali amalga oshiriladi.</p>
            <table>
                <tr><th>ID</th><th>User ID</th><th>Domen</th><th>Sabab</th><th>Vaqt</th></tr>
                {rows}
            </table>
        </div>

        <div class="card">
            <h2>🛡️ Qora ro'yxatdagi domenlar ({len(blacklist_domains)})</h2>
            <p>{bl}</p>
        </div>
        """
        self._send_html(render_page("Secure Admin Panel - Cyber Bot", body))

    def _page_users(self):
        users = get_all_users(limit=1000)
        rows = "".join(
            f"<tr><td>{i}</td><td>{esc(u[0])}</td><td>@{esc(u[1] or '-')}</td>"
            f"<td>{esc(u[2])}</td><td>{esc(u[3])}</td><td><b>{esc(u[4])}</b></td></tr>"
            for i, u in enumerate(users, 1))
        body = f"""
        <h1>👥 Barcha foydalanuvchilar ({len(users)})</h1>
        <div class="card"><table>
            <tr><th>#</th><th>User ID</th><th>Username</th><th>Ismi</th><th>Til</th><th>Reyting</th></tr>
            {rows}
        </table></div>"""
        self._send_html(render_page("Foydalanuvchilar - Admin", body))

    def _page_groups(self):
        groups = get_all_groups(limit=500)
        rows = "".join(
            f"<tr><td>{i}</td><td>{esc(g[0])}</td><td><b>{esc(g[1])}</b></td>"
            f"<td>{esc('@' + g[2]) if g[2] else '-'}</td><td>{esc(g[3])}</td>"
            f"<td>{esc(g[4])}</td><td>{esc(g[5])}</td></tr>"
            for i, g in enumerate(groups, 1))
        if not groups:
            rows = "<tr><td colspan='7'>Hozircha guruhlar yo'q. Bot guruhga qo'shilganda avtomatik yoziladi.</td></tr>"
        body = f"""
        <h1>🏘 Bot qo'shilgan guruhlar ({len(groups)})</h1>
        <div class="card"><table>
            <tr><th>#</th><th>Chat ID</th><th>Nomi</th><th>Username</th><th>Turi</th><th>A'zolar</th><th>Qo'shilgan vaqt</th></tr>
            {rows}
        </table></div>"""
        self._send_html(render_page("Guruhlar - Admin", body))

    def _page_dangers(self):
        dangers = get_dangerous_urls(limit=500)
        rows = "".join(
            f"<tr><td>{i}</td><td>{esc(d[1])}</td><td><b>{esc(d[2])}</b></td><td>{esc(d[3])}</td>"
            f"<td>{esc(d[5])}</td><td>{esc((d[4] or '')[:100])}</td><td>{esc(d[6])}</td></tr>"
            for i, d in enumerate(dangers, 1))
        if not dangers:
            rows = "<tr><td colspan='7'>Hozircha xavfli havolalar saqlanmagan.</td></tr>"
        body = f"""
        <h1>🚨 Xavfli deb topilgan havolalar ({len(dangers)})</h1>
        <div class="card"><table>
            <tr><th>#</th><th>URL</th><th>Domen</th><th>User ID</th><th>Manba</th><th>Sabab</th><th>Vaqt</th></tr>
            {rows}
        </table></div>"""
        self._send_html(render_page("Xavfli havolalar - Admin", body))

    def log_message(self, format, *args):
        pass


def run_server():
    port = int(os.environ.get("PORT", 10000))
    server = ThreadingHTTPServer(("0.0.0.0", port), SimpleHandler)
    logging.info(f"Web server {port}-portda ishga tushdi")
    server.serve_forever()


# ==================== MAIN ====================
async def main():
    global bot_loop
    bot_loop = asyncio.get_running_loop()

    # Web serverni darhol ishga tushiramiz (Render port tekshiruvi uchun)
    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()

    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        logging.warning(f"delete_webhook xato: {e}")

    try:
        await set_commands()
    except Exception as e:
        logging.warning(f"set_commands xato: {e}")

    logging.info("Xavfsiz Bot ishga tushdi...")
    await dp.start_polling(
        bot, allowed_updates=["message", "callback_query", "my_chat_member", "chat_member"])


if __name__ == "__main__":
    stats = load_stats()
    asyncio.run(main())
