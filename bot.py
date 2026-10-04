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

# ==================== DATABASE ====================
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
    conn = sqlite3.connect("bot_database.db", check_same_thread=False)
    c = conn.cursor()
    c.execute("SELECT key, value FROM stats")
    rows = c.fetchall()
    conn.close()
    return {k: v for k, v in rows}

def save_stat(key: str, increment: int = 1):
    conn = sqlite3.connect("bot_database.db", check_same_thread=False)
    c = conn.cursor()
    c.execute("UPDATE stats SET value = value + ? WHERE key = ?", (increment, key))
    conn.commit()
    conn.close()

def log_activity(user_id, action, details):
    try:
        conn = sqlite3.connect("bot_database.db", check_same_thread=False)
        c = conn.cursor()
        c.execute("INSERT INTO activity_logs (user_id, action, details) VALUES (?,?,?)",
                  (user_id, action, str(details)[:200]))
        conn.commit()
        conn.close()
    except: pass

def add_user(user_id, username, full_name, language="uz"):
    conn = sqlite3.connect("bot_database.db", check_same_thread=False)
    c = conn.cursor()
    c.execute("""INSERT INTO users (user_id, username, full_name, language) VALUES (?,?,?,?)
                 ON CONFLICT(user_id) DO UPDATE SET 
                 username=excluded.username, full_name=excluded.full_name""",
              (user_id, str(username or "")[:50], str(full_name or "User")[:100], language))
    conn.commit()
    conn.close()

def get_user_lang(user_id: int) -> str:
    conn = sqlite3.connect("bot_database.db", check_same_thread=False)
    c = conn.cursor()
    c.execute("SELECT language FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else "uz"

def set_user_lang(user_id: int, lang: str):
    conn = sqlite3.connect("bot_database.db", check_same_thread=False)
    c = conn.cursor()
    c.execute("UPDATE users SET language = ? WHERE user_id = ?", (lang, user_id))
    conn.commit()
    conn.close()

def update_user_rep(user_id, change):
    conn = sqlite3.connect("bot_database.db", check_same_thread=False)
    c = conn.cursor()
    c.execute("UPDATE users SET reputation = reputation + ? WHERE user_id = ?", (change, user_id))
    conn.commit()
    conn.close()

def add_global_blacklist(domain):
    conn = sqlite3.connect("bot_database.db", check_same_thread=False)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO blacklist (domain) VALUES (?)", (domain.lower(),))
    conn.commit()
    conn.close()

def is_globally_blacklisted(domain):
    conn = sqlite3.connect("bot_database.db", check_same_thread=False)
    c = conn.cursor()
    c.execute("SELECT 1 FROM blacklist WHERE domain = ?", (domain.lower(),))
    row = c.fetchone()
    conn.close()
    return bool(row)

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

@dp.callback_query(F.data.startswith("reject_block_"))
async def reject_block(callback: CallbackQuery):
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
    
    user_lang = get_user_lang(message.from_user.id)
    lang_name = "Uzbek" if user_lang == "uz" else ("Russian" if user_lang == "ru" else "English")

    try:
        result = await text_with_gemini(
            f"Professional cybersecurity OSINT audit of '{target}'. Write a clear structured report. Include risks, legitimacy, and recommendations. "
            f"IMPORTANT: You MUST write the entire report in {lang_name} language.",
            user_id=message.from_user.id)
    except Exception as e:
        result = f"❌ Xato: {str(e)[:80]}"
        await notify_error("cmd_audit", str(e), message.from_user.id)
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
            await message.answer(t(message.from_user.id, "audit_result", result=result), parse_mode="Markdown", reply_markup=kb)

@dp.message(Command("panel"))
async def cmd_panel(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    base = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:10000")
    await message.answer(
        f"🔐 **Himoyalangan Admin Panel**\n\nParol orqali kirish uchun quyidagi havoladan foydalaning:\n`{base}/admin`\n\n[Panelni ochish]({base}/admin)",
        parse_mode="Markdown", disable_web_page_preview=True
    )

# ---------- MEDIA HANDLERS ----------
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
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🚨 Shikoyat qilish", callback_data="quick_report")]])
        if any(w in result.upper() for w in ("XAVFLI", "DANGER", "PHISHING", "SCAM")):
            stats["photo_danger_count"] = stats.get("photo_danger_count", 0) + 1
            save_stat("photo_danger_count")
            update_user_rep(user.id, -12)
            await message.reply(t(user.id, "photo_danger") + f"\n\n{result}", reply_markup=kb)
            await notify_admin(f"🚨 Rasm\nUser: `{user.id}`")
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
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🚨 Shikoyat qilish", callback_data="quick_report")]])
        if "DANGER" in result.upper():
            stats["voice_danger_count"] = stats.get("voice_danger_count", 0) + 1
            save_stat("voice_danger_count")
            update_user_rep(user.id, -15)
            await message.reply(t(user.id, "voice_danger") + f"\n\n{result}", reply_markup=kb)
            await notify_admin(f"🚨 Vishing\nUser: `{user.id}`")
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
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🚨 Shikoyat qilish", callback_data="quick_report")]])
        if "DANGER" in result.upper():
            stats["video_danger_count"] = stats.get("video_danger_count", 0) + 1
            save_stat("video_danger_count")
            update_user_rep(user.id, -15)
            await message.reply(t(user.id, "video_danger") + f"\n\n{result}", reply_markup=kb)
            await notify_admin(f"🚨 Video\nUser: `{user.id}`")
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
    if (doc.file_size or 0) > 25*1024*1024:
        await message.reply("⚠️ Fayl juda katta.")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🚨 Shikoyat qilish", callback_data="quick_report")]])
    if any(name.endswith(ext) for ext in DANGEROUS_EXTENSIONS):
        stats["file_danger_count"] = stats.get("file_danger_count", 0) + 1
        save_stat("file_danger_count")
        update_user_rep(user.id, -20)
        await message.reply(t(user.id, "apk_danger"), parse_mode="Markdown", reply_markup=kb)
        await notify_admin(f"🚨 Fayl: `{name}`\nUser: `{user.id}`")
        return

    try:
        file = await bot.get_file(doc.file_id)
        data = (await bot.download_file(file.file_path)).read()
        result = await analyze_with_gemini(
            f"Analyze this document/file named '{doc.file_name}' for malware, phishing, or malicious script. Reply first: DANGER or SAFE.",
            data, doc.mime_type or "application/octet-stream", user.id
        )
        if result.startswith("ERROR"):
            await message.reply(f"⚠️ {result}")
            return
        if "DANGER" in result.upper():
            stats["file_danger_count"] = stats.get("file_danger_count", 0) + 1
            save_stat("file_danger_count")
            update_user_rep(user.id, -20)
            await message.reply(f"🚨 **ZARARLI FAYL ANIQLANDI!**\n\n{result}", parse_mode="Markdown", reply_markup=kb)
            await notify_admin(f"🚨 Zararli fayl: `{doc.file_name}`\nUser: `{user.id}`")
        else:
            update_user_rep(user.id, +2)
            await message.reply(t(user.id, "file_ok", name=doc.file_name) + f"\n\n{result}", reply_markup=kb)
    except Exception as e:
        await notify_error("handle_document", str(e), user.id)
        await message.reply("⚠️ Fayl tahlilida xato.")

@dp.message(F.text & ~F.text.startswith("/"))
async def handle_text(message: Message):
    user = message.from_user
    add_user(user.id, user.username, user.full_name)
    
    now = time.time()
    last_time = user_last_message_time.get(user.id, 0)
    if now - last_time < SPAM_INTERVAL:
        await message.reply(t(user.id, "spam"))
        return
    user_last_message_time[user.id] = now

    text = message.text
    url = extract_url(text)

    if not url:
        user_lang = get_user_lang(user.id)
        lang_name = "Uzbek" if user_lang == "uz" else ("Russian" if user_lang == "ru" else "English")
        try:
            ai_reply = await text_with_gemini(
                f"You are a helpful cybersecurity assistant in a Telegram bot. User message: {text}. Reply in {lang_name} language.",
                user_id=user.id
            )
            await message.reply(ai_reply)
        except Exception as e:
            await notify_error("handle_text", str(e), user.id)
        return

    stats["checked_count"] = stats.get("checked_count", 0) + 1
    save_stat("checked_count")
    log_activity(user.id, "CHECK_URL", url)

    clean_url = url.lower().replace("https://", "").replace("http://", "").replace("www.", "").split("/")[0]

    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🚨 Shikoyat qilish / Report", callback_data="quick_report")]])

    if clean_url in OFFICIAL_DOMAINS or any(clean_url.endswith("." + d) for d in OFFICIAL_DOMAINS):
        update_user_rep(user.id, +1)
        await message.reply(t(user.id, "official"), reply_markup=kb)
        return

    is_community_danger = await check_community_blacklists(clean_url)

    if is_globally_blacklisted(clean_url) or is_community_danger:
        stats["danger_count"] = stats.get("danger_count", 0) + 1
        save_stat("danger_count")
        update_user_rep(user.id, -15)
        await message.reply(t(user.id, "blacklist") + f"\n\n🔗 `{url}`", parse_mode="Markdown", reply_markup=kb)
        await notify_admin(f"🚨 Qora ro'yxatdagi havola!\nUser: `{user.id}`\nUrl: `{url}`")
        return

    if "t.me/" in url.lower() or "telegram.me/" in url.lower() or url.startswith("@"):
        await message.reply(t(user.id, "tg_profile", clean=clean_url), parse_mode="Markdown", reply_markup=kb)
        return

    wait_msg = await message.reply("🔍 Havola tekshirilmoqda...")
    screenshot_bytes = get_webpage_screenshot(url)

    if screenshot_bytes:
        stats["screenshot_count"] = stats.get("screenshot_count", 0) + 1
        save_stat("screenshot_count")
        
        analysis = await analyze_with_gemini(
            f"Analyze this webpage screenshot for URL: '{url}'. Is it a phishing, scam, fake login, or fraudulent website? "
            f"Reply first with: SCAM or SAFE, followed by a concise explanation.",
            screenshot_bytes, "image/png", user.id
        )

        try: await wait_msg.delete()
        except: pass

        if "SCAM" in analysis.upper():
            stats["danger_count"] = stats.get("danger_count", 0) + 1
            save_stat("danger_count")
            update_user_rep(user.id, -20)
            await message.reply(f"🚨 **PHISHING / FIRIBGARlik ANIQLANDI!**\n\n🔗 `{url}`\n\n{analysis}", parse_mode="Markdown", reply_markup=kb)
            await notify_admin(f"🚨 Xavfli havola (Screenshot):\nUser: `{user.id}`\nUrl: `{url}`")
        else:
            update_user_rep(user.id, +2)
            await message.reply(f"✅ **Xavfsiz ko'rinadi**\n\n🔗 `{url}`\n\n{analysis}", parse_mode="Markdown", reply_markup=kb)
    else:
        try: await wait_msg.delete()
        except: pass
        
        analysis = await text_with_gemini(
            f"Analyze if the URL/domain '{url}' is safe or a phishing/scam site. Reply first with SCAM or SAFE.",
            user_id=user.id
        )
        if "SCAM" in analysis.upper():
            stats["danger_count"] = stats.get("danger_count", 0) + 1
            save_stat("danger_count")
            update_user_rep(user.id, -15)
            await message.reply(f"🚨 **XAVFLI BO'lishi mumkin!**\n\n🔗 `{url}`\n\n{analysis}", parse_mode="Markdown", reply_markup=kb)
        else:
            await message.reply(t(user.id, "no_screenshot") + f"\n\n{analysis}", reply_markup=kb)

# ==================== WEB SERVER & SECURITY ====================
async def send_broadcast_message(text: str):
    conn = sqlite3.connect("bot_database.db", check_same_thread=False)
    c = conn.cursor()
    c.execute("SELECT user_id FROM users")
    users = c.fetchall()
    conn.close()

    success = 0
    failed = 0
    for u in users:
        uid = u[0]
        try:
            await bot.send_message(uid, text)
            success += 1
            await asyncio.sleep(0.05)
        except:
            failed += 1
    await notify_admin(f"📢 **Xabar yuborish yakunlandi!**\n\n✅ Muvaffaqiyatli: {success}\n❌ Xato (bloklaganlar): {failed}")

class SimpleHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/admin/login":
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length).decode('utf-8')
            params = parse_qs(post_data)
            password = params.get("password", [""])[0]

            if password == ADMIN_PASSWORD:
                session_token = f"sess_{int(time.time())}_{os.urandom(4).hex()}"
                ACTIVE_ADMIN_SESSIONS.add(session_token)
                
                self.send_response(303)
                self.send_header("Location", "/admin")
                self.send_header("Set-Cookie", f"admin_session={session_token}; Path=/; HttpOnly; SameSite=Lax")
                self.end_headers()
            else:
                self.send_response(401)
                self.send_header("Content-type", "text/html; charset=utf-8")
                self.end_headers()
            self.wfile.write("<h1>❌ Noto'g'ri parol!</h1><a href='/admin'>Qayta urinish</a>".encode('utf-8'))

        elif parsed.path == "/admin/broadcast":
            # Cookie orqali sessiyani tekshiramiz (Xakerlar kirmasligi uchun)
            cookie_header = self.headers.get("Cookie", "")
            if not any(f"admin_session={s}" in cookie_header for s in ACTIVE_ADMIN_SESSIONS):
                self.send_response(403)
                self.end_headers()
                self.wfile.write(b"Forbidden: Ruxsat etilmagan!")
                return

            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length).decode('utf-8')
            params = parse_qs(post_data)
            msg_text = params.get("message", [""])[0]

            if msg_text:
                asyncio.run_coroutine_threadsafe(send_broadcast_message(msg_text), bot_loop)
                self.send_response(200)
                self.send_header("Content-type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write("<h1>✅ Xabar yuborish boshlandi!</h1><p><a href='/admin'>Orqaga qaytish</a></p>".encode("utf-8"))
            else:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Xabar matni bosh bolishi mumkin emas!")
        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/" or parsed.path == "/health":
            self.send_response(200)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("🤖 Bot ishlayapti! (Secure AI Cyber-Security Bot 24/7)".encode("utf-8"))
        elif parsed.path == "/admin":
            cookie_header = self.headers.get("Cookie", "")
            is_authenticated = any(f"admin_session={s}" in cookie_header for s in ACTIVE_ADMIN_SESSIONS)

            self.send_response(200)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()

            if not is_authenticated:
                # Login формаси
                login_html = """
                <!DOCTYPE html>
                <html>
                <head>
                    <title>Admin Login - Cyber Bot</title>
                    <meta charset="utf-8">
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
                </html>
                """
                self.wfile.write(login_html.encode("utf-8"))
                return

            # Agar parol to'g'ri kiritilgan bo'lsa, haqiqiy panel ochiladi
            conn = sqlite3.connect("bot_database.db", check_same_thread=False)
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM users")
            user_row = c.fetchone()
            user_count = user_row[0] if user_row else 0

            c.execute("SELECT user_id, username, full_name, language, reputation FROM users ORDER BY user_id DESC LIMIT 50")
            all_users = c.fetchall()

            c.execute("SELECT domain FROM blacklist")
            blacklist_domains = [row[0] for row in c.fetchall()]
            c.execute("SELECT id, user_id, domain, reason, created_at FROM pending_blocks WHERE status='pending'")
            pendings = c.fetchall()
            conn.close()

            html = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <title>Secure Admin Panel - Cyber Bot</title>
                <meta charset="utf-8">
                <style>
                    body {{ font-family: Arial, sans-serif; background: #0f172a; color: #f8fafc; padding: 20px; }}
                    .card {{ background: #1e293b; padding: 20px; border-radius: 10px; margin-bottom: 20px; box-shadow: 0 4px 6px rgba(0,0,0,0.3); }}
                    h1, h2 {{ color: #38bdf8; }}
                    table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
                    th, td {{ border: 1px solid #334155; padding: 10px; text-align: left; font-size: 14px; }}
                    th {{ background: #334155; }}
                    .stat-box {{ display: inline-block; background: #334155; padding: 15px; border-radius: 8px; margin-right: 10px; margin-bottom: 10px; }}
                    textarea {{ width: 100%; height: 100px; background: #0f172a; color: #fff; border: 1px solid #334155; padding: 10px; border-radius: 5px; }}
                    button {{ background: #38bdf8; color: #0f172a; border: none; padding: 10px 20px; font-weight: bold; border-radius: 5px; cursor: pointer; margin-top: 10px; }}
                    button:hover {{ background: #0ea5e9; }}
                </style>
            </head>
            <body>
                <h1>🔐 Himoyalangan Admin Panel</h1>
                <div class="card">
                    <h2>📊 Umumiy Statistika</h2>
                    <div class="stat-box">Foydalanuvchilar: <b>{user_count}</b></div>
                    <div class="stat-box">Tekshirilganlar: <b>{stats.get('checked_count', 0)}</b></div>
                    <div class="stat-box">Xavfli havolalar: <b>{stats.get('danger_count', 0)}</b></div>
                    <div class="stat-box">Zararli fayllar: <b>{stats.get('file_danger_count', 0)}</b></div>
                </div>
                <div class="card">
                    <h2>📢 Barcha foydalanuvchilarga xabar yuborish (Broadcast)</h2>
                    <form action="/admin/broadcast" method="POST">
                        <textarea name="message" placeholder="Barcha foydalanuvchilarga yuboriladigan xabarni yozing..."></textarea><br>
                        <button type="submit">Xabarni yuborish 🚀</button>
                    </form>
                </div>
                <div class="card">
                    <h2>🚫 Tasdiqlashni kutayotgan domenlar ({len(pendings)})</h2>
                    <table>
                        <tr><th>ID</th><th>User ID</th><th>Domen</th><th>Sabab</th><th>Vaqt</th></tr>
            """
            for p in pendings:
                html += f"<tr><td>{p[0]}</td><td>{p[1]}</td><td><b>{p[2]}</b></td><td>{p[3]}</td><td>{p[4]}</td></tr>"
            html += f"""
                    </table>
                </div>
                <div class="card">
                    <h2>👥 So'nggi foydalanuvchilar ro'yxati (Oxirgi 50 ta)</h2>
                    <table>
                        <tr><th>User ID</th><th>Username</th><th>Ismi</th><th>Til</th><th>Reyting</th></tr>
            """
            for u in all_users:
                html += f"<tr><td>{u[0]}</td><td>@{u[1] or '-'}</td><td>{u[2]}</td><td>{u[3]}</td><td><b>{u[4]}</b></td></tr>"
            html += f"""
                    </table>
                </div>
                <div class="card">
                    <h2>🛡️ Qora ro'yxatdagi domenlar ({len(blacklist_domains)})</h2>
                    <p>{', '.join(blacklist_domains) if blacklist_domains else 'Hozircha bo\'sh'}</p>
                </div>
            </body>
            </html>
            """
            self.wfile.write(html.encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")

    def log_message(self, format, *args):
        pass

def run_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), SimpleHandler)
    server.serve_forever()

# ==================== MAIN ====================
bot_loop = None

async def main():
    global bot_loop
    bot_loop = asyncio.get_running_loop()

    t = threading.Thread(target=run_server, daemon=True)
    t.start()
    
    await set_commands()
    logging.info("Xavfsiz Bot ishga tushdi...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    stats = load_stats()
    asyncio.run(main())
