import asyncio
import logging
import os
import time
import psycopg2
import psycopg2.extras
import threading
import requests
from io import BytesIO
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.filters.chat_member_updated import ChatMemberUpdatedFilter, IS_MEMBER, IS_NOT_MEMBER
from aiogram.types import Message, BotCommand, BotCommandScopeChat, BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, ChatMemberUpdated
from aiogram.fsm.storage.memory import MemoryStorage

from google import genai
from google.genai import types
from PIL import Image
import cv2
import numpy as np

# ==================== SOZLAMALAR ====================
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("BOT_TOKEN topilmadi!")

ADMIN_ID = 5081583283
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
# Render muhitidan DATABASE_URL ni o'qiymiz
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL topilmadi! Render muhitiga Supabase connection string'ini qo'shing.")

# Kuchli parolni Render muhitidan (Environment Variables) o'qiymiz, bo'lmasa standart parol
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "pirmatov1008_secure_pass")

ai_client = genai.Client(api_key=GEMINI_API_KEY)
MODELS = [
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite",
]

user_last_message_time = {}
SPAM_INTERVAL = 1.3

# Sessiyalar uchun vaqtinchalik xotira (Oddiy xakerlar kirib ololmasligi uchun)
ACTIVE_ADMIN_SESSIONS = set()

# ==================== DATABASE (POSTGRESQL) ====================
def get_db_connection():
    # Supabase / PostgreSQL ulanishi
    conn = psycopg2.connect(DATABASE_URL, sslmode='require')
    return conn

def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id BIGINT PRIMARY KEY, username TEXT, full_name TEXT,
        language TEXT DEFAULT 'uz', reputation INTEGER DEFAULT 100)""")
    c.execute("CREATE TABLE IF NOT EXISTS blacklist (domain TEXT PRIMARY KEY)")
    c.execute("""CREATE TABLE IF NOT EXISTS activity_logs (
        id SERIAL PRIMARY KEY, user_id BIGINT,
        action TEXT, details TEXT, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    c.execute("""CREATE TABLE IF NOT EXISTS stats (
        key TEXT PRIMARY KEY, value INTEGER DEFAULT 0)""")
    c.execute("""CREATE TABLE IF NOT EXISTS pending_blocks (
        id SERIAL PRIMARY KEY,
        user_id BIGINT,
        domain TEXT,
        reason TEXT,
        status TEXT DEFAULT 'pending',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    # Guruhlar jadvali
    c.execute("""CREATE TABLE IF NOT EXISTS groups (
        chat_id BIGINT PRIMARY KEY,
        title TEXT,
        username TEXT,
        chat_type TEXT,
        members_count INTEGER DEFAULT 0,
        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        is_active INTEGER DEFAULT 1
    )""")
    # Xavfli havolalar jadvali
    c.execute("""CREATE TABLE IF NOT EXISTS dangerous_urls (
        id SERIAL PRIMARY KEY,
        url TEXT,
        domain TEXT,
        user_id BIGINT,
        reason TEXT,
        source TEXT DEFAULT 'link',
        detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    default_stats = [
        ("checked_count", 0), ("danger_count", 0), ("file_danger_count", 0),
        ("voice_danger_count", 0), ("video_danger_count", 0), ("photo_danger_count", 0),
        ("screenshot_count", 0), ("audit_count", 0), ("groups_count", 0),
    ]
    for key, val in default_stats:
        c.execute("INSERT INTO stats (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING", (key, val))
    conn.commit()
    c.close()
    conn.close()

init_db()

def load_stats() -> dict:
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT key, value FROM stats")
    rows = c.fetchall()
    c.close()
    conn.close()
    return {k: v for k, v in rows}

def save_stat(key: str, increment: int = 1):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE stats SET value = value + %s WHERE key = %s", (increment, key))
    conn.commit()
    c.close()
    conn.close()

def log_activity(user_id, action, details):
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("INSERT INTO activity_logs (user_id, action, details) VALUES (%s,%s,%s)",
                  (user_id, action, str(details)[:200]))
        conn.commit()
        c.close()
        conn.close()
    except: pass

def add_user(user_id, username, full_name, language="uz"):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""INSERT INTO users (user_id, username, full_name, language) VALUES (%s,%s,%s,%s)
                 ON CONFLICT(user_id) DO UPDATE SET 
                 username=EXCLUDED.username, full_name=EXCLUDED.full_name""",
              (user_id, str(username or "")[:50], str(full_name or "User")[:100], language))
    conn.commit()
    c.close()
    conn.close()

def get_user_lang(user_id: int) -> str:
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT language FROM users WHERE user_id = %s", (user_id,))
    row = c.fetchone()
    c.close()
    conn.close()
    return row[0] if row else "uz"

def set_user_lang(user_id: int, lang: str):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE users SET language = %s WHERE user_id = %s", (lang, user_id))
    conn.commit()
    c.close()
    conn.close()

def update_user_rep(user_id, change):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE users SET reputation = reputation + %s WHERE user_id = %s", (change, user_id))
    conn.commit()
    c.close()
    conn.close()

def add_global_blacklist(domain):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT INTO blacklist (domain) VALUES (%s) ON CONFLICT (domain) DO NOTHING", (domain.lower(),))
    conn.commit()
    c.close()
    conn.close()

def is_globally_blacklisted(domain):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT 1 FROM blacklist WHERE domain = %s", (domain.lower(),))
    row = c.fetchone()
    c.close()
    conn.close()
    return bool(row)

def add_pending_block(user_id: int, domain: str, reason: str) -> int:
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT INTO pending_blocks (user_id, domain, reason) VALUES (%s,%s,%s) RETURNING id",
              (user_id, domain.lower(), reason[:300]))
    rid = c.fetchone()[0]
    conn.commit()
    c.close()
    conn.close()
    return rid

def update_pending_status(request_id: int, status: str):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE pending_blocks SET status = %s WHERE id = %s", (status, request_id))
    conn.commit()
    c.close()
    conn.close()

def add_or_update_group(chat_id: int, title: str, username: str = None, chat_type: str = "group", members_count: int = 0):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""INSERT INTO groups (chat_id, title, username, chat_type, members_count, is_active)
                 VALUES (%s,%s,%s,%s,%s,1)
                 ON CONFLICT(chat_id) DO UPDATE SET
                 title=EXCLUDED.title, username=EXCLUDED.username,
                 chat_type=EXCLUDED.chat_type, members_count=EXCLUDED.members_count,
                 is_active=1""",
              (chat_id, (title or "Nomsiz")[:200], (username or "")[:100], chat_type, members_count or 0))
    conn.commit()
    # groups_count ni yangilash
    c.execute("SELECT COUNT(*) FROM groups WHERE is_active=1")
    count = c.fetchone()[0]
    c.execute("INSERT INTO stats (key, value) VALUES ('groups_count', %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (count,))
    conn.commit()
    c.close()
    conn.close()

def deactivate_group(chat_id: int):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE groups SET is_active=0 WHERE chat_id=%s", (chat_id,))
    conn.commit()
    c.execute("SELECT COUNT(*) FROM groups WHERE is_active=1")
    count = c.fetchone()[0]
    c.execute("INSERT INTO stats (key, value) VALUES ('groups_count', %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (count,))
    conn.commit()
    c.close()
    conn.close()

def add_dangerous_url(url: str, user_id: int, reason: str, source: str = "link"):
    try:
        domain = url.lower().replace("https://", "").replace("http://", "").replace("www.", "").split("/")[0]
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("INSERT INTO dangerous_urls (url, domain, user_id, reason, source) VALUES (%s,%s,%s,%s,%s)",
                  (url[:500], domain[:200], user_id, (reason or "")[:400], source))
        conn.commit()
        c.close()
        conn.close()
    except Exception as e:
        logging.error(f"add_dangerous_url xato: {e}")

def get_groups_count() -> int:
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM groups WHERE is_active=1")
    row = c.fetchone()
    c.close()
    conn.close()
    return row[0] if row else 0

def get_all_groups(limit: int = 200):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT chat_id, title, username, chat_type, members_count, added_at FROM groups WHERE is_active=1 ORDER BY added_at DESC LIMIT %s", (limit,))
    rows = c.fetchall()
    c.close()
    conn.close()
    return rows

def get_all_users(limit: int = 500):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT user_id, username, full_name, language, reputation FROM users ORDER BY user_id DESC LIMIT %s", (limit,))
    rows = c.fetchall()
    c.close()
    conn.close()
    return rows

def get_dangerous_urls(limit: int = 300):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id, url, domain, user_id, reason, source, detected_at FROM dangerous_urls ORDER BY detected_at DESC LIMIT %s", (limit,))
    rows = c.fetchall()
    c.close()
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
        "tg_profile": "🔗 **Telegram profile/kanal:** `{clean}`\n\nScreenshot not available.\nDeep check: `/audit {clean}`",
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
    }
}

def t(user_id: int, key: str, **kwargs) -> str:
    lang = get_user_lang(user_id)
    text = TEXTS.get(lang, TEXTS["uz"]).get(key, TEXTS["uz"].get(key, key))
    try:
        return text.format(**kwargs)
    except:
        return text

OFFICIAL_DOMAINS = {
    "gov.uz", "my.gov.uz", "lex.uz", "cbu.uz", "soliq.uz", "my.soliq.uz",
    "nbu.uz", "agrobank.uz", "kapitalbank.uz", "hamkorbank.uz", "tbcbank.uz",
    "uzcard.uz", "humocard.uz", "click.uz", "payme.uz", "uzum.uz", "uzumbank.uz",
    "kun.uz", "daryo.uz", "gazeta.uz", "beeline.uz", "ucell.uz", "uztelecom.uz"
}

DANGEROUS_EXTENSIONS = {".apk", ".exe", ".bat", ".cmd", ".scr", ".js", ".vbs", ".msi", ".jar", ".com", ".pif", ".hta"}

logging.basicConfig(level=logging.INFO)
storage = MemoryStorage()
bot = Bot(token=TOKEN)
dp = Dispatcher(storage=storage)

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
    admin_cmds = default_cmds + [BotCommand(command="panel", description="🔐 Admin Panel")]
    await bot.set_my_commands(admin_cmds, scope=BotCommandScopeChat(chat_id=ADMIN_ID))

# ==================== GROUP TRACKING ====================
@dp.my_chat_member(ChatMemberUpdatedFilter(IS_NOT_MEMBER >> IS_MEMBER))
async def bot_added_to_group(event: ChatMemberUpdated):
    """Bot guruh yoki kanalga qo'shilganda"""
    chat = event.chat
    if chat.type in ("group", "supergroup", "channel"):
        try:
            members = 0
            try:
                members = await bot.get_chat_member_count(chat.id)
            except:
                pass
            add_or_update_group(
                chat_id=chat.id,
                title=chat.title or "Nomsiz guruh",
                username=chat.username,
                chat_type=chat.type,
                members_count=members
            )
            log_activity(event.from_user.id if event.from_user else 0, "BOT_ADDED_TO_GROUP", f"{chat.id}|{chat.title}")
            await notify_admin(
                f"➕ **Bot yangi guruhga qo'shildi!**\n\n"
                f"**Nomi:** `{chat.title}`\n"
                f"**ID:** `{chat.id}`\n"
                f"**Turi:** `{chat.type}`\n"
                f"**Username:** @{chat.username or '-'}"
            )
        except Exception as e:
            logging.error(f"bot_added_to_group xato: {e}")

@dp.my_chat_member(ChatMemberUpdatedFilter(IS_MEMBER >> IS_NOT_MEMBER))
async def bot_removed_from_group(event: ChatMemberUpdated):
    """Bot guruhdan chiqarilganda"""
    chat = event.chat
    if chat.type in ("group", "supergroup", "channel"):
        deactivate_group(chat.id)
        log_activity(event.from_user.id if event.from_user else 0, "BOT_REMOVED_FROM_GROUP", f"{chat.id}|{chat.title}")
        await notify_admin(
            f"➖ **Bot guruhdan chiqarildi**\n\n"
            f"**Nomi:** `{chat.title}`\n"
            f"**ID:** `{chat.id}`"
        )

# ==================== HELPERS ====================
def extract_url(text: str):
    if not text: return None
    for word in text.split():
        clean = word.strip(".,;:!?()[]{}\"'")
        if clean.startswith(("http://", "https://", "www.")): return clean
        if "t.me/" in clean.lower() or "telegram.me/" in clean.lower(): return clean
        if clean.startswith("@") and len(clean) > 1: return f"t.me/{clean[1:]}"
        if "." in clean and len(clean) > 3 and " " not in clean:
            parts = clean.split(".")
            if len(parts) >= 2 and parts[-1].lower() in {"uz","com","net","org","ru","info","xyz","site","online","me","io","co","tv","cc","app","dev"}:
                return clean
    return None

def get_webpage_screenshot(url: str) -> bytes | None:
    try:
        full = url if url.startswith(("http://", "https://")) else "https://" + url
        r = requests.get(f"https://api.microlink.io/?url={full}&screenshot=true&meta=false&embed=screenshot.url", timeout=8)
        data = r.json()
        if data.get("status") == "success":
            return requests.get(data["data"]["screenshot"]["url"], timeout=8).content
    except: pass
    return None

def detect_qr(image_bytes: bytes) -> str | None:
    try:
        nparr = np.frombuffer(image_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if img is None: return None
        detector = cv2.QRCodeDetector()
        data, _, _ = detector.detectAndDecode(img)
        return data if data else None
    except: return None

async def check_community_blacklists(domain: str) -> bool:
    try:
        res = requests.get(f"https://urlhaus.abuse.ch/api/v1/host/{domain}/", timeout=4)
        if res.status_code == 200:
            data = res.json()
            if data.get("query_status") == "ok":
                return True
    except: pass
    return False

async def notify_admin(text: str, reply_markup=None):
    try:
        await bot.send_message(ADMIN_ID, text, parse_mode="Markdown", reply_markup=reply_markup)
    except Exception as e:
        logging.error(f"Admin notify xato: {e}")

async def notify_error(context: str, error: str, user_id: int = None):
    msg = f"⚠️ **Bot xatosi**\n\n**Joy:** `{context}`\n**Xato:** `{error[:300]}`"
    if user_id:
        msg += f"\n**User:** `{user_id}`"
    await notify_admin(msg)

async def analyze_with_gemini(prompt: str, data: bytes, mime: str, user_id: int = None) -> str:
    last_error = ""
    for model in MODELS:
        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(ai_client.models.generate_content, model=model,
                                  contents=[prompt, types.Part.from_bytes(data=data, mime_type=mime)]),
                timeout=12.0)
            return response.text or ""
        except Exception as e:
            last_error = str(e)
            if "503" in str(e) or "high demand" in str(e).lower():
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
                timeout=10.0)
            return response.text or "Bo‘sh javob"
        except Exception as e:
            last_error = str(e)
            if "503" in str(e) or "high demand" in str(e).lower():
                await asyncio.sleep(1)
                continue
    await notify_error("text_with_gemini", last_error, user_id)
    return f"❌ AI hozir band. 1-2 daqiqadan keyin qayta urinib ko‘ring.\n{last_error[:80]}"

# ==================== HANDLERS ====================
@dp.message(Command("start"))
async def cmd_start(message: Message):
    lang = message.from_user.language_code or "uz"
    if lang.startswith("ru"): lang = "ru"
    elif lang.startswith("en"): lang = "en"
    else: lang = "uz"
    add_user(message.from_user.id, message.from_user.username, message.from_user.full_name, lang)
    log_activity(message.from_user.id, "START", "start")
    await message.answer(t(message.from_user.id, "start"), parse_mode="Markdown")

@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(t(message.from_user.id, "help"), parse_mode="Markdown")

@dp.message(Command("report"))
async def cmd_report(message: Message):
    await message.answer(t(message.from_user.id, "report"), parse_mode="Markdown")

@dp.message(Command("lang"))
async def cmd_lang(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇺🇿 O‘zbekcha", callback_data="lang_uz")],
        [InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang_ru")],
        [InlineKeyboardButton(text="🇬🇧 English", callback_data="lang_en")],
    ])
    await message.answer(t(message.from_user.id, "lang_choose"), reply_markup=kb)

@dp.callback_query(F.data.startswith("lang_"))
async def process_lang(callback: CallbackQuery):
    lang = callback.data.split("_")[1]
    set_user_lang(callback.from_user.id, lang)
    await callback.message.edit_text(t(callback.from_user.id, "lang_set"))
    await callback.answer()

@dp.callback_query(F.data == "quick_report")
async def process_quick_report(callback: CallbackQuery):
    await callback.message.answer("📢 Shikoyatingiz qabul qilindi. Admin tez orada ko‘rib chiqadi. Rahmat!")
    await callback.answer("Yuborildi!")

@dp.message(Command("block"))
async def cmd_block(message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer(t(message.from_user.id, "block_usage"))
        return

    raw = args[1].strip()
    domain = raw.lower().replace("https://", "").replace("http://", "").replace("www.", "").split("/")[0].strip()
    
    if not domain or "." not in domain:
        await message.answer(t(message.from_user.id, "block_usage"))
        return

    if is_globally_blacklisted(domain):
        await message.answer(t(message.from_user.id, "block_already"))
        return

    wait = await message.answer("🔍 AI tekshiruv o‘tkazilmoqda...")
    
    analysis = await text_with_gemini(
        f"Is the website/domain '{domain}' likely a phishing, scam, or fraudulent site? "
        f"Reply ONLY with one word: SCAM or SAFE, then a short reason.",
        user_id=message.from_user.id
    )
    
    try: await wait.delete()
    except: pass

    is_scam = "SCAM" in analysis.upper()

    if not is_scam:
        await message.answer(t(message.from_user.id, "block_not_scam"))
        log_activity(message.from_user.id, "BLOCK_REQUEST_REJECTED_AI", domain)
        return

    request_id = add_pending_block(message.from_user.id, domain, analysis)
    log_activity(message.from_user.id, "BLOCK_REQUEST", domain)
    await message.answer(t(message.from_user.id, "block_sent", domain=domain), parse_mode="Markdown")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"approve_block_{request_id}"),
            InlineKeyboardButton(text="❌ Rad etish", callback_data=f"reject_block_{request_id}")
        ]
    ])
    
    admin_text = (
        f"🚫 **Yangi bloklash so‘rovi**\n\n"
        f"**Domen:** `{domain}`\n"
        f"**Foydalanuvchi:** `{message.from_user.id}` (@{message.from_user.username or '-'})\n"
        f"**AI tahlili:**\n{analysis[:400]}\n\n"
        f"Tasdiqlaysizmi?"
    )
    await notify_admin(admin_text, reply_markup=kb)

@dp.callback_query(F.data.startswith("approve_block_"))
async def approve_block(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Faqat admin!", show_alert=True)
        return
    
    request_id = int(callback.data.split("_")[-1])
    
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT user_id, domain, status FROM pending_blocks WHERE id = %s", (request_id,))
    row = c.fetchone()
    c.close()
    conn.close()
    
    if not row or row[2] != "pending":
        await callback.answer("So‘rov topilmadi yoki allaqachon ko‘rilgan", show_alert=True)
        return
    
    user_id, domain, _ = row
    add_global_blacklist(domain)
    update_pending_status(request_id, "approved")
    log_activity(ADMIN_ID, "BLOCK_APPROVED", domain)
    
    await callback.message.edit_text(
        f"✅ **Tasdiqlandi!**\n\n`{domain}` qora ro‘yxatga qo‘shildi.",
        parse_mode="Markdown"
    )
    await callback.answer("Tasdiqlandi!")
    
    try:
        await bot.send_message(user_id, t(user_id, "block_approved", domain=domain), parse_mode="Markdown")
    except: pass

@dp.callback_query(F.data.startswith("reject_block_"))
async def reject_block(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Faqat admin!", show_alert=True)
        return
    
    request_id = int(callback.data.split("_")[-1])
    
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT user_id, domain, status FROM pending_blocks WHERE id = %s", (request_id,))
    row = c.fetchone()
    c.close()
    conn.close()
    
    if not row or row[2] != "pending":
        await callback.answer("So‘rov topilmadi yoki allaqachon ko‘rilgan", show_alert=True)
        return
    
    user_id, domain, _ = row
    update_pending_status(request_id, "rejected")
    log_activity(ADMIN_ID, "BLOCK_REJECTED", domain)
    
    await callback.message.edit_text(
        f"❌ **Rad etildi**\n\n`{domain}` qora ro‘yxatga qo‘shilmadi.",
        parse_mode="Markdown"
    )
    await callback.answer("Rad etildi")
    
    try:
        await bot.send_message(user_id, t(user_id, "block_rejected", domain=domain), parse_mode="Markdown")
    except: pass
