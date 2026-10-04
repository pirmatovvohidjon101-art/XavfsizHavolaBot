import asyncio
import logging
import os
import time
import sqlite3
import threading
import requests
from io import BytesIO
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, BotCommand, BotCommandScopeChat, BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
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
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "pirmatov1008")

ai_client = genai.Client(api_key=GEMINI_API_KEY)
MODELS = ["gemini-2.5-flash-lite", "gemini-3.1-flash-lite", "gemini-2.5-flash"]

user_last_message_time = {}
SPAM_INTERVAL = 1.3

# ==================== DATABASE ====================
def init_db():
    conn = sqlite3.connect("bot_database.db")
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

def load_stats() -> dict:
    conn = sqlite3.connect("bot_database.db")
    c = conn.cursor()
    c.execute("SELECT key, value FROM stats")
    rows = c.fetchall()
    conn.close()
    return {k: v for k, v in rows}

def save_stat(key: str, increment: int = 1):
    conn = sqlite3.connect("bot_database.db")
    c = conn.cursor()
    c.execute("UPDATE stats SET value = value + ? WHERE key = ?", (increment, key))
    conn.commit()
    conn.close()

def log_activity(user_id, action, details):
    try:
        conn = sqlite3.connect("bot_database.db")
        c = conn.cursor()
        c.execute("INSERT INTO activity_logs (user_id, action, details) VALUES (?,?,?)",
                  (user_id, action, str(details)[:200]))
        conn.commit()
        conn.close()
    except: pass

def add_user(user_id, username, full_name, language="uz"):
    conn = sqlite3.connect("bot_database.db")
    c = conn.cursor()
    c.execute("""INSERT INTO users (user_id, username, full_name, language) VALUES (?,?,?,?)
                 ON CONFLICT(user_id) DO UPDATE SET 
                 username=excluded.username, full_name=excluded.full_name""",
              (user_id, str(username or "")[:50], str(full_name or "User")[:100], language))
    conn.commit()
    conn.close()

def get_user_lang(user_id: int) -> str:
    conn = sqlite3.connect("bot_database.db")
    c = conn.cursor()
    c.execute("SELECT language FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else "uz"

def set_user_lang(user_id: int, lang: str):
    conn = sqlite3.connect("bot_database.db")
    c = conn.cursor()
    c.execute("UPDATE users SET language = ? WHERE user_id = ?", (lang, user_id))
    conn.commit()
    conn.close()

def update_user_rep(user_id, change):
    conn = sqlite3.connect("bot_database.db")
    c = conn.cursor()
    c.execute("UPDATE users SET reputation = reputation + ? WHERE user_id = ?", (change, user_id))
    conn.commit()
    conn.close()

def add_global_blacklist(domain):
    conn = sqlite3.connect("bot_database.db")
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO blacklist (domain) VALUES (?)", (domain.lower(),))
    conn.commit()
    conn.close()

def is_globally_blacklisted(domain):
    conn = sqlite3.connect("bot_database.db")
    c = conn.cursor()
    c.execute("SELECT 1 FROM blacklist WHERE domain = ?", (domain.lower(),))
    row = c.fetchone()
    conn.close()
    return bool(row)

def add_pending_block(user_id: int, domain: str, reason: str) -> int:
    conn = sqlite3.connect("bot_database.db")
    c = conn.cursor()
    c.execute("INSERT INTO pending_blocks (user_id, domain, reason) VALUES (?,?,?)",
              (user_id, domain.lower(), reason[:300]))
    conn.commit()
    rid = c.lastrowid
    conn.close()
    return rid

def update_pending_status(request_id: int, status: str):
    conn = sqlite3.connect("bot_database.db")
    c = conn.cursor()
    c.execute("UPDATE pending_blocks SET status = ? WHERE id = ?", (status, request_id))
    conn.commit()
    conn.close()

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
        "file_ok": "📄 Fayl: `{name}`\n⚠️ Noma’lum manbadan ochmang.",
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
        "audit_start": "🕵️‍♂️ **Кибер-Детектив** запущен...\n`{target}`\n\nОжидайте...",
        "audit_result": "🛡️ **ОТЧЁТ АУДИТА**\n\n{result}",
        "block_usage": "❌ Использование: `/block example.com`",
        "block_already": "ℹ️ Этот домен уже в чёрном списке.",
        "block_sent": "✅ Ваш запрос принят!\n\nДомен: `{domain}`\nОжидает подтверждения администратора.",
        "block_not_scam": "ℹ️ AI не считает этот сайт мошенническим. Запрос отклонён.",
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
        BotCommand(command="audit", description="🕵️‍♂️ Cyber Audit"),
        BotCommand(command="block", description="🚫 Block scam site"),
        BotCommand(command="report", description="📢 Report"),
        BotCommand(command="lang", description="🌐 Language / Til"),
        BotCommand(command="help", description="ℹ️ Help / Yordam"),
    ]
    await bot.set_my_commands(default_cmds)
    admin_cmds = default_cmds + [BotCommand(command="panel", description="🔐 Admin Panel")]
    await bot.set_my_commands(admin_cmds, scope=BotCommandScopeChat(chat_id=ADMIN_ID))

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

@dp.message(Command("block"))
async def cmd_block(message: Message):
    """Foydalanuvchi firibgar saytni bloklash so‘rovi"""
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer(t(message.from_user.id, "block_usage"))
        return

    raw = args[1].strip()
    # Domenni tozalash
    domain = raw.lower().replace("https://", "").replace("http://", "").replace("www.", "").split("/")[0].strip()
    
    if not domain or "." not in domain:
        await message.answer(t(message.from_user.id, "block_usage"))
        return

    if is_globally_blacklisted(domain):
        await message.answer(t(message.from_user.id, "block_already"))
        return

    # AI tekshiruvi
    wait = await message.answer("🔍 AI tekshiruv o‘tkazilmoqda...")
    
    analysis = await text_with_gemini(
        f"Is the website/domain '{domain}' likely a phishing, scam, or fraudulent site? "
        f"Reply ONLY with one word: SCAM or SAFE, then a short reason in English.",
        user_id=message.from_user.id
    )
    
    try: await wait.delete()
    except: pass

    is_scam = "SCAM" in analysis.upper()

    if not is_scam:
        await message.answer(t(message.from_user.id, "block_not_scam"))
        log_activity(message.from_user.id, "BLOCK_REQUEST_REJECTED_AI", domain)
        return

    # So‘rovni saqlash
    request_id = add_pending_block(message.from_user.id, domain, analysis)
    log_activity(message.from_user.id, "BLOCK_REQUEST", domain)

    # Foydalanuvchiga javob
    await message.answer(t(message.from_user.id, "block_sent", domain=domain), parse_mode="Markdown")

    # Adminga tasdiqlash so‘rovi
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Tasdiqlash / Approve", callback_data=f"approve_block_{request_id}"),
            InlineKeyboardButton(text="❌ Rad etish / Reject", callback_data=f"reject_block_{request_id}")
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
    
    conn = sqlite3.connect("bot_database.db")
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

@dp.callback_query(F.data.startswith("reject_block_"))
async def reject_block(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Faqat admin!", show_alert=True)
        return
    
    request_id = int(callback.data.split("_")[-1])
    
    conn = sqlite3.connect("bot_database.db")
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

@dp.message(Command("audit"))
async def cmd_audit(message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("❌ `/audit <havola yoki kanal>`")
        return
    target = args[1].strip()
    stats["audit_count"] = stats.get("audit_count", 0) + 1
    save_stat("audit_count")
    log_activity(message.from_user.id, "AUDIT", target)

    wait_msg = await message.answer(t(message.from_user.id, "audit_start", target=target), parse_mode="Markdown")
    try:
        result = await text_with_gemini(
            f"Professional cybersecurity OSINT audit of '{target}'. Write clear structured report. Include risks, legitimacy, recommendations.",
            user_id=message.from_user.id)
    except Exception as e:
        result = f"❌ Xato: {str(e)[:80]}"
        await notify_error("cmd_audit", str(e), message.from_user.id)
    try: await wait_msg.delete()
    except: pass

    if result.startswith("ERROR") or result.startswith("❌"):
        await message.answer(result)
    else:
        if len(result) > 4000:
            for i in range(0, len(result), 4000):
                await message.answer(result[i:i+4000])
        else:
            await message.answer(t(message.from_user.id, "audit_result", result=result), parse_mode="Markdown")

@dp.message(Command("panel"))
async def cmd_panel(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    base = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:10000")
    await message.answer(
        f"🔐 **Admin Web Panel**\n\n`{base}/admin`\n\n[Panelni ochish]({base}/admin)",
        parse_mode="Markdown", disable_web_page_preview=True
    )

# ---------- MEDIA HANDLERS (qisqartirilgan, oldingi kabi ishlaydi) ----------
@dp.message(F.photo)
async def handle_photo(message: Message):
    user = message.from_user
    add_user(user.id, user.username, user.full_name)
    try:
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        data = (await bot.download_file(file.file_path)).read()

        qr_data = detect_qr(data)
        if qr_data:
            lower = qr_data.lower()
            is_danger = any(x in lower for x in ("http", "https", "t.me", "wifi", "begin:wifi"))
            msg = f"📷 **QR aniqlandi!**\n`{qr_data[:200]}`\n\n"
            if is_danger:
                stats["photo_danger_count"] = stats.get("photo_danger_count", 0) + 1
                save_stat("photo_danger_count")
                update_user_rep(user.id, -10)
                msg += "⚠️ Ehtiyot!"
                await notify_admin(f"🚨 QR\nUser: `{user.id}`")
            else:
                msg += "✅ Oddiy QR."
            await message.reply(msg, parse_mode="Markdown")

        result = await analyze_with_gemini(
            "Analyze this image for phishing/scam/dangerous QR. Start with '🚨 XAVFLI' or '✅ XAVFSIZ'.",
            data, "image/jpeg", user.id)
        if result.startswith("ERROR"):
            await message.reply(f"⚠️ {result}")
            return
        if any(w in result.upper() for w in ("XAVFLI", "DANGER", "PHISHING", "SCAM")):
            stats["photo_danger_count"] = stats.get("photo_danger_count", 0) + 1
            save_stat("photo_danger_count")
            update_user_rep(user.id, -12)
            await message.reply(t(user.id, "photo_danger") + f"\n\n{result}")
            await notify_admin(f"🚨 Rasm\nUser: `{user.id}`")
        else:
            update_user_rep(user.id, +2)
            await message.reply(t(user.id, "photo_ok", result=result))
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
        if "DANGER" in result.upper():
            stats["voice_danger_count"] = stats.get("voice_danger_count", 0) + 1
            save_stat("voice_danger_count")
            update_user_rep(user.id, -15)
            await message.reply(t(user.id, "voice_danger") + f"\n\n{result}")
            await notify_admin(f"🚨 Vishing\nUser: `{user.id}`")
        else:
            update_user_rep(user.id, +2)
            await message.reply(t(user.id, "voice_ok", result=result))
    except Exception as e:
        await notify_error("handle_voice", str(e), user.id)
        await message.reply("⚠️ Ovoz tahlilida xato.")

@dp.message(F.video)
async def handle_video(message: Message):
    user = message.from_user
    add_user(user.id, user.username, user.full_name)
    try:
        if message.video.file_size and message.video.file_size > 18*1024*1024:
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
        if "DANGER" in result.upper():
            stats["video_danger_count"] = stats.get("video_danger_count", 0) + 1
            save_stat("video_danger_count")
            update_user_rep(user.id, -15)
            await message.reply(t(user.id, "video_danger") + f"\n\n{result}")
            await notify_admin(f"🚨 Video\nUser: `{user.id}`")
        else:
            update_user_rep(user.id, +2)
            await message.reply(t(user.id, "video_ok", result=result))
    except Exception as e:
        await notify_error("handle_video", str(e), user.id)
        await message.reply("⚠️ Video tahlilida xato.")

@dp.message(F.document)
async def handle_document(message: Message):
    user = message.from_user
    add_user(user.id, user.username, user.full_name)
    doc = message.document
    name = (doc.file_name or "").lower()
    if (doc.file_size or 0) > 25*1024*1024:
        await message.reply("⚠️ Fayl juda katta.")
        return
    if any(name.endswith(ext) for ext in DANGEROUS_EXTENSIONS):
        stats["file_danger_count"] = stats.get("file_danger_count", 0) + 1
        save_stat("file_danger_count")
        update_user_rep(user.id, -20)
        await message.reply(t(user.id, "apk_danger"), parse_mode="Markdown")
        await notify_admin(f"🚨 Fayl: `{name}`\nUser: `{user.id}`")
        return
    await message.reply(t(user.id, "file_ok", name=doc.file_name), parse_mode="Markdown")

@dp.message(F.text)
async def handle_text(message: Message):
    user = message.from_user
    add_user(user.id, user.username, user.full_name)
    now = time.time()
    if now - user_last_message_time.get(user.id, 0) < SPAM_INTERVAL:
        await message.reply(t(user.id, "spam"))
        return
    user_last_message_time[user.id] = now

    url = extract_url(message.text)
    if not url: return
    
    stats["checked_count"] = stats.get("checked_count", 0) + 1
    save_stat("checked_count")

    if url.startswith("t.me/") or "t.me/" in url or "telegram.me/" in url:
        clean = url.replace("https://","").replace("http://","").replace("t.me/","").replace("telegram.me/","").strip("/")
        await message.reply(t(user.id, "tg_profile", clean=clean), parse_mode="Markdown")
        return

    full_url = url if url.startswith(("http://","https://")) else "https://" + url
    parsed = urlparse(full_url)
    domain = parsed.netloc.lower().removeprefix("www.")

    if is_globally_blacklisted(domain):
        stats["danger_count"] = stats.get("danger_count", 0) + 1
        save_stat("danger_count")
        update_user_rep(user.id, -10)
        await message.reply(t(user.id, "blacklist"))
        return
    if domain.endswith(".gov.uz") or domain in OFFICIAL_DOMAINS:
        update_user_rep(user.id, +1)
        await message.reply(t(user.id, "official"))
        return

    try:
        shot = get_webpage_screenshot(full_url)
        if shot:
            stats["screenshot_count"] = stats.get("screenshot_count", 0) + 1
            save_stat("screenshot_count")
            result = await analyze_with_gemini(
                "Analyze this website screenshot for phishing or scam. Start with '🚨 PHISHING' or '✅ XAVFSIZ'.",
                shot, "image/jpeg", user.id)
            if any(w in result.upper() for w in ("PHISHING","SCAM","XAVFLI","DANGER")):
                stats["danger_count"] = stats.get("danger_count", 0) + 1
                save_stat("danger_count")
                update_user_rep(user.id, -15)
                await message.reply_photo(BufferedInputFile(shot, "shot.jpg"),
                                          caption=f"🚨 **PHISHING!**\n\n{result}", parse_mode="Markdown")
                await notify_admin(f"🚨 Phishing: `{domain}`")
            else:
                update_user_rep(user.id, +3)
                await message.reply_photo(BufferedInputFile(shot, "shot.jpg"),
                                          caption=f"✅ Checked.\n\n{result}", parse_mode="Markdown")
        else:
            await message.reply(t(user.id, "no_screenshot"))
    except Exception as e:
        await notify_error("handle_text", str(e), user.id)
        await message.reply(t(user.id, "no_screenshot"))

# ==================== WEB PANEL ====================
class WebPanelHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/health"):
            self.send_response(200)
            self.send_header("Content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")
            return
        if parsed.path == "/admin":
            html = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Admin Login</title>
<style>
body{font-family:system-ui;background:#0f172a;color:#e2e8f0;display:flex;justify-content:center;align-items:center;height:100vh;margin:0}
.card{background:#1e293b;padding:32px;border-radius:16px;width:340px;text-align:center}
input{width:100%;padding:14px;margin:12px 0;border-radius:10px;border:1px solid #334155;background:#0f172a;color:#fff;box-sizing:border-box}
button{background:#0ea5e9;color:#fff;border:none;padding:14px;border-radius:10px;cursor:pointer;width:100%;font-weight:600}
</style></head><body>
<div class="card"><h2>🔐 Admin Panel</h2>
<form method="POST" action="/admin">
<input type="password" name="password" placeholder="Password" required autofocus>
<button type="submit">Login</button></form></div></body></html>"""
            self.send_response(200)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/admin":
            length = int(self.headers.get("Content-Length", 0))
            params = parse_qs(self.rfile.read(length).decode())
            if params.get("password", [""])[0] != ADMIN_PASSWORD:
                self.send_response(200)
                self.send_header("Content-type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"<h2 style='color:red;text-align:center;margin-top:80px'>Wrong password!</h2><p style='text-align:center'><a href='/admin'>Try again</a></p>")
                return

            current_stats = load_stats()
            conn = sqlite3.connect("bot_database.db")
            c = conn.cursor()
            c.execute("SELECT user_id, username, full_name, reputation, language FROM users ORDER BY reputation DESC LIMIT 50")
            users = c.fetchall()
            c.execute("SELECT domain FROM blacklist")
            black = c.fetchall()
            c.execute("SELECT user_id, action, details, timestamp FROM activity_logs ORDER BY id DESC LIMIT 30")
            logs = c.fetchall()
            c.execute("SELECT id, user_id, domain, status, created_at FROM pending_blocks ORDER BY id DESC LIMIT 20")
            pending = c.fetchall()
            conn.close()

            users_rows = "".join(f"<tr><td>{u[0]}</td><td>@{u[1] or '-'}</td><td>{u[2]}</td><td>{u[3]}</td><td>{u[4]}</td></tr>" for u in users)
            black_rows = "".join(f"<li>{b[0]}</li>" for b in black) or "<li>Empty</li>"
            log_rows = "".join(f"<tr><td>{l[0]}</td><td>{l[1]}</td><td>{str(l[2])[:40]}</td><td>{l[3]}</td></tr>" for l in logs)
            pending_rows = "".join(f"<tr><td>{p[0]}</td><td>{p[1]}</td><td>{p[2]}</td><td>{p[3]}</td><td>{p[4]}</td></tr>" for p in pending)

            html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Admin Panel</title>
<style>
body{{font-family:system-ui;background:#0f172a;color:#e2e8f0;margin:0;padding:20px}}
.card{{background:#1e293b;padding:16px;margin:12px 0;border-radius:10px}}
.stat{{display:inline-block;background:#0f172a;padding:10px 14px;margin:4px;border-radius:8px;text-align:center;min-width:70px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th,td{{border:1px solid #334155;padding:6px}}
th{{background:#0f172a;color:#94a3b8}}
input,textarea{{width:100%;padding:8px;margin:6px 0;border-radius:6px;border:1px solid #334155;background:#0f172a;color:#e2e8f0;box-sizing:border-box}}
button{{background:#0ea5e9;color:white;border:none;padding:8px 14px;border-radius:6px;cursor:pointer}}
</style></head><body>
<h1>🛡️ Kiber Admin Panel</h1>
<p style="color:#94a3b8">{datetime.now().strftime('%Y-%m-%d %H:%M')}</p>
<div class="card"><b>Statistics</b><br>
<div class="stat">Links<br><b>{current_stats.get('checked_count',0)}</b></div>
<div class="stat">Phishing<br><b>{current_stats.get('danger_count',0)}</b></div>
<div class="stat">Files<br><b>{current_stats.get('file_danger_count',0)}</b></div>
<div class="stat">Photos<br><b>{current_stats.get('photo_danger_count',0)}</b></div>
<div class="stat">Voice<br><b>{current_stats.get('voice_danger_count',0)}</b></div>
<div class="stat">Video<br><b>{current_stats.get('video_danger_count',0)}</b></div>
<div class="stat">Audit<br><b>{current_stats.get('audit_count',0)}</b></div>
<div class="stat">Users<br><b>{len(users)}</b></div>
</div>
<div class="card"><h3>📢 Broadcast</h3>
<form method="POST" action="/broadcast"><textarea name="message" rows="2"></textarea>
<button type="submit">Send</button></form></div>
<div class="card"><h3>🚫 Blacklist</h3>
<form method="POST" action="/add_blacklist"><input type="text" name="domain" placeholder="domain.uz">
<button type="submit">Add</button></form><ul>{black_rows}</ul></div>
<div class="card"><h3>⏳ Pending Block Requests</h3>
<table><tr><th>ID</th><th>User</th><th>Domain</th><th>Status</th><th>Time</th></tr>{pending_rows}</table></div>
<div class="card"><h3>⚡ Logs</h3>
<table><tr><th>User</th><th>Action</th><th>Info</th><th>Time</th></tr>{log_rows}</table></div>
<div class="card"><h3>👥 Users</h3>
<table><tr><th>ID</th><th>Username</th><th>Name</th><th>Karma</th><th>Lang</th></tr>{users_rows}</table></div>
</body></html>"""
            self.send_response(200)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))
            return

        if parsed.path == "/add_blacklist":
            length = int(self.headers.get("Content-Length", 0))
            params = parse_qs(self.rfile.read(length).decode())
            domain = params.get("domain", [""])[0].strip()
            if domain:
                add_global_blacklist(domain)
                log_activity(ADMIN_ID, "BLACKLIST_ADD", domain)
            self.send_response(303)
            self.send_header("Location", "/admin")
            self.end_headers()
            return

        if parsed.path == "/broadcast":
            length = int(self.headers.get("Content-Length", 0))
            params = parse_qs(self.rfile.read(length).decode())
            msg = params.get("message", [""])[0].strip()
            if msg:
                log_activity(ADMIN_ID, "BROADCAST", msg[:40])
                threading.Thread(target=run_broadcast, args=(msg,), daemon=True).start()
            self.send_response(303)
            self.send_header("Location", "/admin")
            self.end_headers()
            return

        self.send_response(404)
        self.end_headers()

    def log_message(self, *args): pass

def run_broadcast(text):
    conn = sqlite3.connect("bot_database.db")
    users = conn.execute("SELECT user_id FROM users").fetchall()
    conn.close()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    async def send():
        for u in users:
            try:
                await bot.send_message(u[0], f"📢 **Admin:**\n\n{text}", parse_mode="Markdown")
                await asyncio.sleep(0.05)
            except: pass
    loop.run_until_complete(send())

def run_http_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), WebPanelHandler).serve_forever()

async def keep_alive():
    while True:
        try:
            base = os.environ.get("RENDER_EXTERNAL_URL")
            if base:
                requests.get(f"{base}/health", timeout=5)
        except: pass
        await asyncio.sleep(600)

async def main():
    threading.Thread(target=run_http_server, daemon=True).start()
    asyncio.create_task(keep_alive())
    await bot.delete_webhook(drop_pending_updates=True)
    await set_commands()
    print("✅ Bot ishga tushdi — /block + Multi-lang + Error notify")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
