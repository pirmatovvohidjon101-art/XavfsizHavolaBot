import asyncio
import logging
import os
import time
import sqlite3
import threading
from io import BytesIO
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, BotCommand, BotCommandScopeChat, BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.fsm.storage.memory import MemoryStorage

# 4. Asinxron so'rovlar uchun httpx
import httpx

from google import genai
from google.genai import types
from PIL import Image
import cv2
import numpy as np

# ==================== LOGGING & SOZLAMALAR ====================
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("BOT_TOKEN topilmadi!")

ADMIN_ID = 5081583283
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "pirmatov1008_secure_pass")

ai_client = genai.Client(api_key=GEMINI_API_KEY)
MODELS = [
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite",
]

# 1. Rate Limiting uchun xotira va vaqt oralig'i (3 soniya)
user_last_request = {}
RATE_LIMIT_SECONDS = 3.0

# ==================== DATABASE & LOGGING (2-BAND) ====================
def init_db():
    conn = sqlite3.connect("bot_database.db", check_same_thread=False)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT,
        language TEXT DEFAULT 'uz', reputation INTEGER DEFAULT 100)""")
    c.execute("CREATE TABLE IF NOT EXISTS blacklist (domain TEXT PRIMARY KEY)")
    c.execute("""CREATE TABLE IF NOT EXISTS activity_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
        action TEXT, details TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)""")
    # 2-band: Xatoliklarni bazaga yozish uchun jadval
    c.execute("""CREATE TABLE IF NOT EXISTS error_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT,
        error_message TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS stats (
        key TEXT PRIMARY KEY, value INTEGER DEFAULT 0)""")
    c.execute("""CREATE TABLE IF NOT EXISTS pending_blocks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        domain TEXT,
        reason TEXT,
        status TEXT DEFAULT 'pending',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")
    default_stats = [
        ("checked_count", 0), ("danger_count", 0), ("file_danger_count", 0),
        ("voice_danger_count", 0), ("video_danger_count", 0), ("photo_danger_count", 0),
        ("screenshot_count", 0), ("audit_count", 0),
    ]
    c.executemany("INSERT OR IGNORE INTO stats (key, value) VALUES (?, ?)", default_stats)
    conn.commit()
    conn.close()

init_db()

# 2-band: Xatoliklarni log qilish va tokenlarni/shaxsiy ma'lumotlarni yashirish (Masking)
def log_error_to_db(err_msg):
    try:
        clean_msg = str(err_msg).replace(TOKEN, "[TOKEN_HIDDEN]")
        if GEMINI_API_KEY:
            clean_msg = clean_msg.replace(GEMINI_API_KEY, "[API_KEY_HIDDEN]")
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        conn = sqlite3.connect("bot_database.db", check_same_thread=False)
        c = conn.cursor()
        c.execute("INSERT INTO error_logs (timestamp, error_message) VALUES (?, ?)", (timestamp, clean_msg[:300]))
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"Bazaga xatolik yozishda muammo: {e}")

def load_stats() -> dict:
    try:
        conn = sqlite3.connect("bot_database.db", check_same_thread=False)
        c = conn.cursor()
        c.execute("SELECT key, value FROM stats")
        rows = c.fetchall()
        conn.close()
        return {k: v for k, v in rows}
    except Exception as e:
        log_error_to_db(e)
        return {}

def save_stat(key: str, increment: int = 1):
    try:
        conn = sqlite3.connect("bot_database.db", check_same_thread=False)
        c = conn.cursor()
        c.execute("UPDATE stats SET value = value + ? WHERE key = ?", (increment, key))
        conn.commit()
        conn.close()
    except Exception as e:
        log_error_to_db(e)

def log_activity(user_id, action, details):
    try:
        conn = sqlite3.connect("bot_database.db", check_same_thread=False)
        c = conn.cursor()
        c.execute("INSERT INTO activity_logs (user_id, action, details) VALUES (?,?,?)",
                  (user_id, action, str(details)[:200]))
        conn.commit()
        conn.close()
    except Exception as e:
        log_error_to_db(e)

def add_user(user_id, username, full_name, language="uz"):
    try:
        conn = sqlite3.connect("bot_database.db", check_same_thread=False)
        c = conn.cursor()
        c.execute("""INSERT INTO users (user_id, username, full_name, language) VALUES (?,?,?,?)
                     ON CONFLICT(user_id) DO UPDATE SET 
                     username=excluded.username, full_name=excluded.full_name""",
                  (user_id, str(username or "")[:50], str(full_name or "User")[:100], language))
        conn.commit()
        conn.close()
    except Exception as e:
        log_error_to_db(e)

def get_user_lang(user_id: int) -> str:
    try:
        conn = sqlite3.connect("bot_database.db", check_same_thread=False)
        c = conn.cursor()
        c.execute("SELECT language FROM users WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        conn.close()
        return row[0] if row else "uz"
    except:
        return "uz"

def set_user_lang(user_id: int, lang: str):
    try:
        conn = sqlite3.connect("bot_database.db", check_same_thread=False)
        c = conn.cursor()
        c.execute("UPDATE users SET language = ? WHERE user_id = ?", (lang, user_id))
        conn.commit()
        conn.close()
    except Exception as e:
        log_error_to_db(e)

def add_global_blacklist(domain):
    try:
        conn = sqlite3.connect("bot_database.db", check_same_thread=False)
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO blacklist (domain) VALUES (?)", (domain.lower(),))
        conn.commit()
        conn.close()
    except Exception as e:
        log_error_to_db(e)

def remove_global_blacklist(domain):
    try:
        conn = sqlite3.connect("bot_database.db", check_same_thread=False)
        c = conn.cursor()
        c.execute("DELETE FROM blacklist WHERE domain = ?", (domain.lower(),))
        conn.commit()
        conn.close()
    except Exception as e:
        log_error_to_db(e)

def get_blacklist_domains():
    try:
        conn = sqlite3.connect("bot_database.db", check_same_thread=False)
        c = conn.cursor()
        c.execute("SELECT domain FROM blacklist")
        rows = c.fetchall()
        conn.close()
        return [r[0] for r in rows]
    except:
        return []

def is_globally_blacklisted(domain):
    try:
        conn = sqlite3.connect("bot_database.db", check_same_thread=False)
        c = conn.cursor()
        c.execute("SELECT 1 FROM blacklist WHERE domain = ?", (domain.lower(),))
        row = c.fetchone()
        conn.close()
        return bool(row)
    except:
        return False

def add_pending_block(user_id: int, domain: str, reason: str) -> int:
    conn = sqlite3.connect("bot_database.db", check_same_thread=False)
    c = conn.cursor()
    c.execute("INSERT INTO pending_blocks (user_id, domain, reason) VALUES (?,?,?)",
              (user_id, domain.lower(), reason[:300]))
    conn.commit()
    rid = c.lastrowid
    conn.close()
    return rid

def update_pending_status(request_id: int, status: str):
    conn = sqlite3.connect("bot_database.db", check_same_thread=False)
    c = conn.cursor()
    c.execute("UPDATE pending_blocks SET status = ? WHERE id = ?", (status, request_id))
    conn.commit()
    conn.close()

stats = load_stats()

# ==================== MULTI-LANGUAGE TEXTS ====================
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
        "spam": "⚠ Juda tez-tez xabar yuboryapsiz. Iltimos, 3 soniya kuting.",
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
        "ai_busy": "❌ AI hozir band. 1-2 daqiqadan keyin qayta urinib ko‘ring.",
        "photo_ok": "✅ Rasm tekshirildi.\n\n{result}",
        "voice_ok": "✅ Ovoz xavfsiz.\n\n{result}",
        "video_ok": "✅ Video tekshirildi.\n\n{result}",
        "audit_start": "🕵️‍♂️ **Kiber-Detektiv** ishga tushdi...\n`{target}`\n\nKuting...",
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
        "spam": "⚠️️ Слишком часто. Пожалуйста, подождите 3 секунды.",
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
        "ai_busy": "❌ AI сейчас перегружен. Попробуйте через 1-2 минуты.",
        "photo_ok": "✅ Фото проверено.\n\n{result}",
        "voice_ok": "✅ Голос безопасен.\n\n{result}",
        "video_ok": "✅ Видео проверено.\n\n{result}",
        "audit_start": "🕵️‍♂️ **Кибер-Детектив** запущен...\n`{target}`\n\nОжидайте...",
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
            "ℹ **Help**\n\n"
            "• `/start` — Start the bot\n"
            "• `/audit <link>` — Cyber audit\n"
            "• `/block <domain>` — Request to block scam site\n"
            "• `/report` — Report suspicious content\n"
            "• `/lang` — Change language\n"
            "• `/help` — Help"
        ),
        "spam": "⚠️ Too fast. Please wait 3 seconds.",
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
        "ai_busy": "❌ AI is currently busy. Try again in 1-2 minutes.",
        "photo_ok": "✅ Photo checked.\n\n{result}",
        "voice_ok": "✅ Voice is safe.\n\n{result}",
        "video_ok": "✅ Video checked.\n\n{result}",
        "audit_start": "🕵️‍♂️ **Cyber Detective** started...\n`{target}`\n\nPlease wait...",
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

storage = MemoryStorage()
bot = Bot(token=TOKEN)
dp = Dispatcher(storage=storage)

async def set_commands():
    default_cmds = [
        BotCommand(command="start", description="🚀 Start / Boshlash"),
        BotCommand(command="audit", description="🕵️‍♂ Cyber Audit"),
        BotCommand(command="block", description="🚫 Block scam site"),
        BotCommand(command="report", description="📢 Report"),
        BotCommand(command="lang", description="🌐 Language / Til"),
        BotCommand(command="help", description="ℹ Help / Yordam"),
    ]
    await bot.set_my_commands(default_cmds)
    admin_cmds = default_cmds + [BotCommand(command="panel", description="🔐 Admin Panel")]
    await bot.set_my_commands(admin_cmds, scope=BotCommandScopeChat(chat_id=ADMIN_ID))

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

async def notify_admin(text: str, reply_markup=None):
    try:
        await bot.send_message(ADMIN_ID, text, parse_mode="Markdown", reply_markup=reply_markup)
    except Exception as e:
        log_error_to_db(e)

async def notify_error(context: str, error: str, user_id: int = None):
    msg = f"⚠️ **Bot xatosi**\n\n**Joy:** `{context}`\n**Xato:** `{error[:300]}`"
    if user_id:
        msg += f"\n**User:** `{user_id}`"
    log_error_to_db(error)
    await notify_admin(msg)

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

# ==================== HANDLERS (1-band: Rate Limiting) ====================
@dp.message(Command("start"))
async def cmd_start(message: Message):
    try:
        user_id = message.from_user.id
        current_time = time.time()
        if user_id in user_last_request:
            if current_time - user_last_request[user_id] < RATE_LIMIT_SECONDS:
                await message.answer(t(user_id, "spam"))
                return
        user_last_request[user_id] = current_time

        lang = message.from_user.language_code or "uz"
        if lang.startswith("ru"): lang = "ru"
        elif lang.startswith("en"): lang = "en"
        else: lang = "uz"
        add_user(user_id, message.from_user.username, message.from_user.full_name, lang)
        log_activity(user_id, "START", "start")
        await message.answer(t(user_id, "start"), parse_mode="Markdown")
    except Exception as e:
        log_error_to_db(e)

@dp.message(Command("help"))
async def cmd_help(message: Message):
    try:
        await message.answer(t(message.from_user.id, "help"), parse_mode="Markdown")
    except Exception as e:
        log_error_to_db(e)

@dp.message(Command("report"))
async def cmd_report(message: Message):
    try:
        await message.answer(t(message.from_user.id, "report"), parse_mode="Markdown")
    except Exception as e:
        log_error_to_db(e)

@dp.message(Command("lang"))
async def cmd_lang(message: Message):
    try:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🇺🇿 O‘zbekcha", callback_data="lang_uz")],
            [InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang_ru")],
            [InlineKeyboardButton(text="🇬🇧 English", callback_data="lang_en")],
        ])
        await message.answer(t(message.from_user.id, "lang_choose"), reply_markup=kb)
    except Exception as e:
        log_error_to_db(e)

@dp.callback_query(F.data.startswith("lang_"))
async def process_lang(callback: CallbackQuery):
    try:
        lang = callback.data.split("_")[1]
        set_user_lang(callback.from_user.id, lang)
        await callback.message.edit_text(t(callback.from_user.id, "lang_set"))
        await callback.answer()
    except Exception as e:
        log_error_to_db(e)

@dp.callback_query(F.data == "quick_report")
async def process_quick_report(callback: CallbackQuery):
    try:
        await callback.message.answer("📢 Shikoyatingiz qabul qilindi. Admin tez orada ko‘rib chiqadi. Rahmat!")
        await callback.answer("Yuborildi!")
    except Exception as e:
        log_error_to_db(e)

@dp.message(Command("block"))
async def cmd_block(message: Message):
    try:
        user_id = message.from_user.id
        args = message.text.split(maxsplit=1)
        if len(args) < 2:
            await message.answer(t(user_id, "block_usage"))
            return

        raw = args[1].strip()
        domain = raw.lower().replace("https://", "").replace("http://", "").replace("www.", "").split("/")[0].strip()
        
        if not domain or "." not in domain:
            await message.answer(t(user_id, "block_usage"))
            return

        if is_globally_blacklisted(domain):
            await message.answer(t(user_id, "block_already"))
            return

        wait = await message.answer("🔍 AI tekshiruv o‘tkazilmoqda...")
        
        analysis = await text_with_gemini(
            f"Is the website/domain '{domain}' likely a phishing, scam, or fraudulent site? "
            f"Reply ONLY with one word: SCAM or SAFE, then a short reason.",
            user_id=user_id
        )
        
        try: await wait.delete()
        except: pass

        is_scam = "SCAM" in analysis.upper()

        if not is_scam:
            await message.answer(t(user_id, "block_not_scam"))
            log_activity(user_id, "BLOCK_REQUEST_REJECTED_AI", domain)
            return

        request_id = add_pending_block(user_id, domain, analysis)
        log_activity(user_id, "BLOCK_REQUEST", domain)
        await message.answer(t(user_id, "block_sent", domain=domain), parse_mode="Markdown")

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"approve_block_{request_id}"),
                InlineKeyboardButton(text="❌ Rad etish", callback_data=f"reject_block_{request_id}")
            ]
        ])
        
        admin_text = (
            f"🚫 **Yangi bloklash so‘rovi**\n\n"
            f"**Domen:** `{domain}`\n"
            f"**Foydalanuvchi:** `{user_id}` (@{message.from_user.username or '-'})\n"
            f"**AI tahlili:**\n{analysis[:400]}\n\n"
            f"Tasdiqlaysizmi?"
        )
        await notify_admin(admin_text, reply_markup=kb)
    except Exception as e:
        log_error_to_db(e)

@dp.callback_query(F.data.startswith("approve_block_"))
async def approve_block(callback: CallbackQuery):
    try:
        if callback.from_user.id != ADMIN_ID:
            await callback.answer("Faqat admin!", show_alert=True)
            return
        
        request_id = int(callback.data.split("_")[-1])
        
        conn = sqlite3.connect("bot_database.db", check_same_thread=False)
        c = conn.cursor()
        c.execute("SELECT user_id, domain, status FROM pending_blocks WHERE id = ?", (request_id,))
        row = c.fetchone()
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
    except Exception as e:
        log_error_to_db(e)

@dp.callback_query(F.data.startswith("reject_block_"))
async def reject_block(callback: CallbackQuery):
    try:
        if callback.from_user.id != ADMIN_ID:
            await callback.answer("Faqat admin!", show_alert=True)
            return
        
        request_id = int(callback.data.split("_")[-1])
        
        conn = sqlite3.connect("bot_database.db", check_same_thread=False)
        c = conn.cursor()
        c.execute("SELECT user_id, domain, status FROM pending_blocks WHERE id = ?", (request_id,))
        row = c.fetchone()
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
    except Exception as e:
        log_error_to_db(e)

@dp.message(Command("audit"))
async def cmd_audit(message: Message):
    try:
        user_id = message.from_user.id
        args = message.text.split(maxsplit=1)
        if len(args) < 2:
            await message.answer("❌ `/audit <havola yoki kanal>`")
            return
        target = args[1].strip()
        stats["audit_count"] = stats.get("audit_count", 0) + 1
        save_stat("audit_count")
        log_activity(user_id, "AUDIT", target)

        wait_msg = await message.answer(t(user_id, "audit_start", target=target), parse_mode="Markdown")
        
        user_lang = get_user_lang(user_id)
        lang_name = "Uzbek" if user_lang == "uz" else ("Russian" if user_lang == "ru" else "English")

        try:
            result = await text_with_gemini(
                f"Professional cybersecurity OSINT audit of '{target}'. Write a clear structured report. Include risks, legitimacy, and recommendations. "
                f"IMPORTANT: You MUST write the entire report in {lang_name} language.",
                user_id=user_id)
        except Exception as e:
            result = f"❌ Xato: {str(e)[:80]}"
            await notify_error("cmd_audit", str(e), user_id)
        
        try: await wait_msg.delete()
        except: pass

        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🚨 Shikoyat qilish / Report", callback_data="quick_report")]])

        if result.startswith("ERROR") or result.startswith("❌"):
            await message.answer(result)
        else:
            if len(result) > 4000:
                for i in range(0, len(result), 4000):
                    await message.answer(result[i:i+4000])
            else:
                await message.answer(t(user_id, "audit_result", result=result), parse_mode="Markdown", reply_markup=kb)
    except Exception as e:
        log_error_to_db(e)

@dp.message(Command("panel"))
async def cmd_panel(message: Message):
    try:
        if message.from_user.id != ADMIN_ID:
            return
        base = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:10000")
        await message.answer(
            f"🔐 **Himoyalangan Admin Panel**\n\nParol orqali kirish uchun quyidagi havoladan foydalaning:\n`{base}/admin`\n\n[Panelni ochish]({base}/admin)",
            parse_mode="Markdown", disable_web_page_preview=True
        )
    except Exception as e:
        log_error_to_db(e)

# ==================== 5-BAND: KEEP-ALIVE & FULL ADMIN PANEL ====================
class KeepAliveHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            parsed_path = urlparse(self.path)
            path = parsed_path.path

            if path == "/admin":
                self.send_response(200)
                self.send_header("Content-type", "text/html; charset=utf-8")
                self.end_headers()
                
                html = """
                <html>
                <head><title>Admin Panel - Login</title></head>
                <body style="font-family: Arial; padding: 40px; background: #f4f6f9;">
                    <div style="max-width: 400px; margin: auto; background: white; padding: 30px; border-radius: 8px; box-shadow: 0 4px 10px rgba(0,0,0,0.1);">
                        <h2>🔐 Admin Panelga Kirish</h2>
                        <form method="POST" action="/admin">
                            <input type="password" name="password" placeholder="Parolni kiriting" style="padding: 10px; width: 100%; margin-bottom: 15px; border: 1px solid #ccc; border-radius: 4px; box-sizing: border-box;">
                            <button type="submit" style="padding: 10px 20px; width: 100%; background: #007bff; color: white; border: none; border-radius: 4px; cursor: pointer;">Kirish</button>
                        </form>
                    </div>
                </body>
                </html>
                """
                self.wfile.write(html.encode("utf-8"))
            elif path == "/":
                self.send_response(200)
                self.send_header("Content-type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"Bot is alive and running smoothly!")
            else:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"Not Found")
        except Exception as e:
            log_error_to_db(e)

    def do_POST(self):
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length).decode('utf-8')
            params = parse_qs(post_data)

            if self.path == "/admin":
                password = params.get("password", [""])[0]
                self.send_response(200)
                self.send_header("Content-type", "text/html; charset=utf-8")
                self.end_headers()

                if password != ADMIN_PASSWORD:
                    self.wfile.write("<h3>❌ Noto'g'ri parol!</h3><a href='/admin'>Qaytadan urinish</a>".encode("utf-8"))
                    return

                stats_data = load_stats()
                blacklist = get_blacklist_domains()
                bl_html = "".join([f"<li>{d} <a href='/remove-domain?domain={d}' style='color:red;'>[O'chirish]</a></li>" for d in blacklist])

                html = f"""
                <html>
                <head><title>Admin Dashboard</title></head>
                <body style="font-family: Arial; padding: 30px; background: #f4f6f9;">
                    <div style="max-width: 800px; margin: auto; background: white; padding: 30px; border-radius: 8px; box-shadow: 0 4px 10px rgba(0,0,0,0.1);">
                        <h2>📊 Bot Statistikasi va Boshqaruv</h2>
                        <ul>
                            <li><b>Jami tekshiruvlar:</b> {stats_data.get('checked_count', 0)}</li>
                            <li><b>Xavfli havolalar:</b> {stats_data.get('danger_count', 0)}</li>
                            <li><b>Fayl xavflari:</b> {stats_data.get('file_danger_count', 0)}</li>
                            <li><b>Ovozli xavflar:</b> {stats_data.get('voice_danger_count', 0)}</li>
                            <li><b>Rasm xavflari:</b> {stats_data.get('photo_danger_count', 0)}</li>
                            <li><b>Video xavflari:</b> {stats_data.get('video_danger_count', 0)}</li>
                            <li><b>Auditlar soni:</b> {stats_data.get('audit_count', 0)}</li>
                        </ul>
                        <hr>
                        <h3>➕ Qora ro'yxatga domen qo'shish</h3>
                        <form method="POST" action="/add-domain">
                            <input type="text" name="domain" placeholder="example.com" style="padding: 8px; width: 250px;">
                            <button type="submit" style="padding: 8px 15px; background: #28a745; color: white; border: none; border-radius: 4px;">Qo'shish</button>
                        </form>
                        <hr>
                        <h3>📋 Qora ro'yxatdagi domenlar</h3>
                        <ul>{bl_html if bl_html else "Ro'yxat bo'sh"}</ul>
                        <br><a href="/admin">Yangilash</a>
                    </div>
                </body>
                </html>
                """
                self.wfile.write(html.encode("utf-8"))

            elif self.path == "/add-domain":
                domain = params.get("domain", [""])[0].strip().lower()
                if domain:
                    add_global_blacklist(domain)
                self.send_response(303)
                self.send_header("Location", "/admin")
                self.end_headers()

            elif self.path == "/remove-domain":
                # Get query params for remove
                query = urlparse(self.path).query
                # handled via GET helper below if needed, or parse from self.path
        except Exception as e:
            log_error_to_db(e)

    def log_message(self, format, *args):
        pass

def run_http_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), KeepAliveHandler)
    server.serve_forever()

async def main():
    server_thread = threading.Thread(target=run_http_server, daemon=True)
    server_thread.start()
    logging.info("Keep-Alive HTTP server va to'liq Admin panel ishga tushdi.")

    await set_commands()
    logging.info("Bot polling boshlanmoqda...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
