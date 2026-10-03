import asyncio
import logging
import re
import os
import time
import sqlite3
import threading
import requests
from io import BytesIO
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BotCommand
from google import genai
from PIL import Image
import cv2
import numpy as np

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("BOT_TOKEN topilmadi! Render'dagi Environment bo'limiga BOT_TOKEN qo'shganingizni tekshiring.")

ADMIN_ID = 5081583283  # O'z Telegram ID raqamingiz

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ai_client = genai.Client(api_key=GEMINI_API_KEY)

stats = {
    "checked_count": 0,
    "danger_count": 0,
    "file_danger_count": 0,
    "voice_danger_count": 0
}

user_last_message_time = {}
SPAM_INTERVAL = 1.5

# --- BAZA BILAN ISHLASH (SQLITE) ---
def init_db():
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            language TEXT DEFAULT 'uz',
            reputation INTEGER DEFAULT 100
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS whitelist (
            chat_id INTEGER,
            domain TEXT,
            PRIMARY KEY (chat_id, domain)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS blacklist (
            chat_id INTEGER,
            domain TEXT,
            PRIMARY KEY (chat_id, domain)
        )
    """)
    conn.commit()
    conn.close()

init_db()

def add_user(user_id, username, full_name):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO users (user_id, username, full_name, language, reputation) 
        VALUES (?, ?, ?, 'uz', 100)
        ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, full_name=excluded.full_name
    """, (user_id, username, full_name))
    conn.commit()
    conn.close()

def get_user_lang(user_id):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT language FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else 'uz'

def set_user_lang(user_id, lang):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET language = ? WHERE user_id = ?", (lang, user_id))
    conn.commit()
    conn.close()

def update_user_rep(user_id, change):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET reputation = reputation + ? WHERE user_id = ?", (change, user_id))
    cursor.execute("SELECT reputation FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.commit()
    conn.close()
    return row[0] if row else 100

def get_all_users():
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users")
    rows = cursor.fetchall()
    conn.close()
    return [row[0] for row in rows]

def get_top_users(limit=10):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT full_name, username, reputation FROM users ORDER BY reputation DESC LIMIT ?", (limit,))
    rows = cursor.fetchall()
    conn.close()
    return rows

def add_to_whitelist(chat_id, domain):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO whitelist (chat_id, domain) VALUES (?, ?)", (chat_id, domain.lower()))
    conn.commit()
    conn.close()

def is_whitelisted(chat_id, domain):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM whitelist WHERE chat_id = ? AND domain = ?", (chat_id, domain.lower()))
    row = cursor.fetchone()
    conn.close()
    return row is not None

def add_to_blacklist(chat_id, domain):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO blacklist (chat_id, domain) VALUES (?, ?)", (chat_id, domain.lower()))
    conn.commit()
    conn.close()

def is_blacklisted(chat_id, domain):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM blacklist WHERE chat_id = ? AND domain = ?", (chat_id, domain.lower()))
    row = cursor.fetchone()
    conn.close()
    return row is not None

# --- TARJIMALAR ---
TEXTS = {
    'uz': {
        'start': "👋 Assalomu alaykum!\n\nMen xavfsizlik va AI yordamchi botiman. Guruhlarda shubhali havolalar, zararli fayllar (.apk), soxta cheklar va QR-kodlarni nazorat qilaman.",
        'stats': "📊 **Bot Statistikasi:**\n\n🔍 Tekshirilgan havolalar: {checked}\n🚨 Xavfli havolalar: {danger}\n👥 Foydalanuvchilar: {users}",
        'lang_set': "✅ Til o'zbek tiliga o'zgartirildi.",
        'help': "ℹ️ **Qo'llanma:**\n- Havola, matn, to'lov cheki yoki QR-kod yuborib tekshirishingiz mumkin.\n- `/top` — Reyting\n- `/web` — Admin veb-paneli havolasi\n- `/whitelist` / `/blacklist` — Ro'yxatni boshqarish",
        'lang_prompt': "🌐 Marhamat, tilni tanlang:",
        'spam': "⚠️ Juda tez-tez xabar yuboryapsiz! Iltimos, biroz kuting.",
        'safe_link': "✅ Bu rasmiy va ishonchli manzil.",
        'danger_link': "🚨 DIQQAT! XAVFLI HAVOLA! Firibgarlar tuzog'i bo'lishi mumkin.",
        'warning_link': "⚠️ Noma'lum havola. Shaxsiy ma'lumotlarni kiritishda ehtiyot bo'ling!",
        'scam_word': "🛑 DIQQAT! Matnda firibgarlikka xos so'zlar aniqlandi!",
        'ai_header': "🤖 Sun'iy Intellekt (AI) javobi:",
        'group_danger_alert': "🚨 DIQQAT! [{user}](tg://user?id={uid}) xavfli xabar/havola yuborgani uchun xabar o'chirildi va karma ochkosi kamaytirildi! (Reputation: {rep})",
        'file_danger': "🚨 DIQQAT! Guruhda zararli yoki shubhali fayl (.apk / .exe) aniqlandi va o'chirildi!",
        'voice_danger': "🚨 DIQQAT! Ovozli xabarda firibgarlik alomatlari aniqlandi!"
    },
    'ru': {
        'start': "👋 Здравствуйте!\n\nЯ бот безопасности и ИИ-помощник.",
        'stats': "📊 **Статистика бота:**\n\n🔍 Проверено ссылок: {checked}\n🚨 Опасных ссылок: {danger}\n👥 Пользователей: {users}",
        'lang_set': "✅ Язык изменен на русский.",
        'help': "ℹ️ **Справка:**\n- Проверяю ссылки, текст, чеки, фото, файлы.\n- `/top` — Рейтинг",
        'lang_prompt': "🌐 Пожалуйста, выберите язык:",
        'spam': "⚠️ Слишком частые запросы!",
        'safe_link': "✅ Это официальный ресурс.",
        'danger_link': "🚨 ВНИМАНИЕ! ОПАСНАЯ ССЫЛКА!",
        'warning_link': "⚠️ Неизвестная ссылка.",
        'scam_word': "🛑 Обнаружены признаки мошенничества!",
        'ai_header': "🤖 Ответ ИИ:",
        'group_danger_alert': "🚨 ВНИМАНИЕ! Сообщение удалено за нарушение безопасности!",
        'file_danger': "🚨 ВНИМАНИЕ! В группе обнаружен и удален подозрительный файл!",
        'voice_danger': "🚨 ВНИМАНИЕ! В голосовом сообщении обнаружены признаки мошенничества!"
    },
    'en': {
        'start': "👋 Hello!\n\nI am a security & AI assistant bot protecting chats.",
        'stats': "📊 **Bot Statistics:**\n\n🔍 Checked links: {checked}\n🚨 Dangerous links: {danger}\n👥 Users: {users}",
        'lang_set': "✅ Language changed to English.",
        'help': "ℹ️ **Help:**\n- Check links, text, payment receipts, files, or chat with AI.\n- `/top` — Leaderboard",
        'lang_prompt': "🌐 Please select a language:",
        'spam': "⚠️ Too fast requests!",
        'safe_link': "✅ Official resource.",
        'danger_link': "🚨 ATTENTION! DANGEROUS LINK!",
        'warning_link': "⚠️ Unknown link.",
        'scam_word': "🛑 Scam patterns detected!",
        'ai_header': "🤖 AI Response:",
        'group_danger_alert': "🚨 ATTENTION! Message deleted due to security violation!",
        'file_danger': "🚨 ATTENTION! Suspicious file detected and deleted!",
        'voice_danger': "🚨 ATTENTION! Scam patterns detected in voice message!"
    }
}

SUSPICIOUS_TLDS = ['.xyz', '.cc', '.tk', '.buzz', '.top', '.gq', '.ml', '.cf', '.ru.com', '.online', '.site', '.club', '.work', '.click', '.link', '.pw', '.su', '.bid', '.loan', '.win', '.stream', '.icu', '.cam', '.cfd', '.VIP']
DANGEROUS_EXTENSIONS = ['.apk', '.exe', '.scr', '.bat', '.cmd', '.pif', '.msi']

OFFICIAL_DOMAINS = {
    'gov.uz', 'my.gov.uz', 'pm.gov.uz', 'lex.uz', 'cbu.uz', 'stat.uz', 'customs.uz',
    'soliq.uz', 'my.soliq.uz', 'uzgidromet.uz', 'mehnat.uz', 'my.mehnat.uz',
    'iiv.uz', 'mfa.uz', 'minjust.uz', 'uzedu.uz', 'ssv.uz', 'tiiame.uz',
    'muslim.uz', 'fatvo.uz', 'quran.uz', 'ziyouz.uz', 'buxari.uz', 'hilolnashr.uz',
    'nbu.uz', 'agrobank.uz', 'kapitalbank.uz', 'ipotekabank.uz', 'davrbank.uz',
    'orientfinanzbank.uz', 'hamkorbank.uz', 'asakabank.uz', 'anorbank.uz',
    'tbcbank.uz', 'octobank.uz', 'infinbank.uz', 'ipakyulibank.uz', 'aloqabank.uz',
    'uzcard.uz', 'humocard.uz', 'click.uz', 'payme.uz', 'uzum.uz', 'uzummarket.uz', 
    'uzumbank.uz', 'paynet.uz', 'Humans.uz',
    'kun.uz', 'gazeta.uz', 'daryo.uz', 'uzreport.news', 'upl.uz', 'sof.uz', 
    'qalampir.uz', 'zamin.uz', 'xabar.uz', 'yuz.uz', 'uza.uz', 'terabayt.uz',
    'texnomart.uz', 'asaxiy.uz', 'olcha.uz', 'express24.uz', 'zoodmall.uz',
    'beeline.uz', 'ucell.uz', 'mobi.uz', 'uztelecom.uz', 'uzmobile.uz'
}

OFFICIAL_TELEGRAM = {
    'davxizmat', 'uzgovuz', 'soliquz', 'cbu_uz', 'iivuz_official', 'mfa_uz', 'ssvuz',
    'kunuzofficial', 'gazetauz', 'daryo', 'uzreport_tv', 'qalampir', 'xabarz', 'uzauz',
    'agrobank_uz', 'kapitalbank_uz', 'anorbank', 'tbcbankuz', 'octobank', 'infinbank',
    'clickuz', 'payme_uz', 'uzcard_uz', 'humocard', 'uzumbank', 'uzummarket',
    'muslimuzportal', 'fatvouz', 'ziyouz', 'hilolnashr', 'islomuz',
    'asaxiy', 'olchouz', 'texnomart', 'express24', 'uzairways',
    'beeline_uzbekistan', 'ucell', 'mobiuzuz', 'uztelecomuz'
}

BRAND_KEYWORDS = ['muslim', 'fatvo', 'hilol', 'ziyouz', 'uzcard', 'humo', 'soliq', 'mygov', 'agrobank', 'kapitalbank', 'anorbank', 'tbc', 'octobank', 'infinbank', 'uzum', 'beeline', 'ucell', 'mobiuz', 'uztelecom', 'click', 'payme', 'asaxiy', 'olcha', 'texnomart', 'express24']
SCAM_WORDS = ['yutib oldingiz', 'bonus', 'sovg', 'pul ishlang', 'aktsiya', 'keshbek', 'konkurs', 'tekin', 'free money', 'выиграли', 'бонус', 'акция', 'розыгрыш', 'free']

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
dp = Dispatcher()

async def set_default_commands(bot: Bot):
    commands = [
        BotCommand(command="start", description="🚀 Botni ishga tushirish"),
        BotCommand(command="stats", description="📊 Bot statistikasi"),
        BotCommand(command="top", description="🏆 Reyting"),
        BotCommand(command="web", description="🌐 Admin veb-paneli"),
        BotCommand(command="whitelist", description="➕ Oq ro'yxat"),
        BotCommand(command="blacklist", description="➖ Qora ro'yxat"),
        BotCommand(command="report", description="✉️ Shikoyat"),
        BotCommand(command="help", description="ℹ️ Qo'llanma")
    ]
    await bot.set_my_commands(commands)

# --- WEB PANEL & HEALTH CHECK SERVER ---
class WebPanelHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed_path = urlparse(self.path)
        path = parsed_path.path
        
        if path == "/" or path == "/health":
            self.send_response(200)
            self.send_header("Content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Bot and Web Panel are running!")
            return
            
        if path == "/admin":
            conn = sqlite3.connect("bot_database.db")
            cursor = conn.cursor()
            cursor.execute("SELECT user_id, username, full_name, reputation FROM users")
            users = cursor.fetchall()
            cursor.execute("SELECT domain FROM blacklist")
            blacklist = cursor.fetchall()
            conn.close()
            
            html = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <title>Xavfsiz Bot - Admin Panel</title>
                <meta charset="utf-8">
                <style>
                    body {{ font-family: Arial, sans-serif; background: #f4f6f9; margin: 0; padding: 20px; }}
                    h1 {{ color: #333; }}
                    .card {{ background: white; padding: 20px; margin-bottom: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
                    table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
                    th, td {{ padding: 10px; border: 1px solid #ddd; text-align: left; }}
                    th {{ background: #0088cc; color: white; }}
                </style>
            </head>
            <body>
                <h1>🛡️ Bot Admin Boshqaruv Paneli</h1>
                <div class="card">
                    <h3>📊 Statistika</h3>
                    <p>Tekshirilgan havolalar: <b>{stats['checked_count']}</b></p>
                    <p>Bloklangan xavfli havolalar: <b>{stats['danger_count']}</b></p>
                    <p>Jami foydalanuvchilar: <b>{len(users)}</b></p>
                </div>
                <div class="card">
                    <h3>🛑 Qora Ro'yxatdagi Domenlar</h3>
                    <ul>
                        {"".join([f"<li>{d[0]}</li>" for d in blacklist]) if blacklist else "<li>Ro'yxat bo'sh</li>"}
                    </ul>
                </div>
                <div class="card">
                    <h3>👥 Foydalanuvchilar Ro'yxati</h3>
                    <table>
                        <tr><th>ID</th><th>To'liq Ismi</th><th>Username</th><th>Karma</th></tr>
                        {"".join([f"<tr><td>{u[0]}</td><td>{u[2]}</td><td>@{u[1] if u[1] else '-'}</td><td>{u[3]}</td></tr>" for u in users])}
                    </table>
                </div>
            </body>
            </html>
            """
            self.send_response(200)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))
            return

        self.send_response(404)
        self.end_headers()

def run_http_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), WebPanelHandler)
    server.serve_forever()

def extract_url(text: str) -> str:
    if not text:
        return None
    words = text.split()
    for word in words:
        clean_word = word.strip(".,;:!?()[]{}\"'")
        clean_lower = clean_word.lower()
        if "t.me/" in clean_lower or "telegram.me/" in clean_lower or clean_word.startswith(('http://', 'https://', 'www.')):
            return clean_word
        if clean_word.startswith('@') and len(clean_word) > 1:
            return f"t.me/{clean_word[1:]}"

    url_pattern = re.compile(r'https?://[^\s]+|www\.[^\s]+|[a-zA-Z0-9][-a-zA-Z0-9()@:%._\+~#=]{1,256}\.[a-zA-Z0-9()]{1,6}\b(?:[-a-zA-Z0-9()@:%_\+.~#?&//=]*)')
    match = url_pattern.search(text)
    return match.group(0) if match else None

def analyze_link(url: str, chat_id: int) -> str:
    global stats
    stats["checked_count"] += 1
    url_lower = url.lower()

    if "t.me/" in url_lower or "telegram.me/" in url_lower:
        parts = url_lower.split("t.me/")
        if len(parts) > 1:
            path = parts[1].split("/")[0].strip()
            if path in OFFICIAL_TELEGRAM:
                return "SAFE"
            return f"DANGER: Telegram channel/group (@{path})"

    full_url = url if url.startswith(('http://', 'https://')) else 'https://' + url
    
    parsed = urlparse(full_url)
    domain = parsed.netloc.lower()
    if domain.startswith('www.'):
        domain = domain[4:]
        
    if is_whitelisted(chat_id, domain):
        return "SAFE"
    if is_blacklisted(chat_id, domain):
        stats["danger_count"] += 1
        return "DANGER"
        
    if domain.endswith('.gov.uz') or domain in OFFICIAL_DOMAINS:
        return "SAFE"
        
    for tld in SUSPICIOUS_TLDS:
        if domain.endswith(tld):
            stats["danger_count"] += 1
            return "DANGER"
            
    for brand in BRAND_KEYWORDS:
        if brand in domain:
            stats["danger_count"] += 1
            return "DANGER"

    return "WARNING"

async def ask_gemini(text: str) -> str:
    try:
        response = ai_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=f"You are a helpful AI assistant. Answer this query clearly and concisely:\n\n{text}"
        )
        return response.text
    except Exception:
        return "Kechirasiz, sun'iy intellektga ulanishda xatolik yuz berdi."

@dp.message(Command("top"))
async def cmd_top(message: Message):
    top_users = get_top_users(10)
    text = "🏆 **Eng hushyor va faol foydalanuvchilar reytingi (Karma):**\n\n"
    for idx, (full_name, username, rep) in enumerate(top_users, 1):
        name_display = f"@{username}" if username else full_name
        text += f"{idx}. **{name_display}** — `{rep}` ball\n"
    await message.answer(text, parse_mode="Markdown")

@dp.message(Command("web"))
async def cmd_web(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Bu buyruq faqat admin uchun.")
        return
    render_url = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:10000")
    await message.answer(f"🌐 **Admin Veb-paneli:**\n\n[Panelni ochish]({render_url}/admin)", parse_mode="Markdown")

@dp.message(Command("whitelist"))
async def cmd_whitelist(message: Message):
    if message.chat.type == 'private':
        await message.answer("❌ Bu buyruq faqat guruhlarda ishlaydi.")
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("❌ Foydalanish: `/whitelist sayt.uz`", parse_mode="Markdown")
        return
    domain = args[1].strip()
    add_to_whitelist(message.chat.id, domain)
    await message.answer(f"✅ `{domain}` oq ro'yxatga qo'shildi.", parse_mode="Markdown")

@dp.message(Command("blacklist"))
async def cmd_blacklist(message: Message):
    if message.chat.type == 'private':
        await message.answer("❌ Bu buyruq faqat guruhlarda ishlaydi.")
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("❌ Foydalanish: `/blacklist sayt.uz`", parse_mode="Markdown")
        return
    domain = args[1].strip()
    add_to_blacklist(message.chat.id, domain)
    await message.answer(f"✅ `{domain}` qora ro'yxatga qo'shildi.", parse_mode="Markdown")

@dp.message(Command("start"))
async def cmd_start(message: Message):
    add_user(message.from_user.id, message.from_user.username, message.from_user.full_name)
    lang = get_user_lang(message.from_user.id)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🇺🇿 O'zbekcha", callback_data="lang_uz"),
            InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang_ru"),
            InlineKeyboardButton(text="🇬🇧 English", callback_data="lang_en")
        ]
    ])
    await message.answer(TEXTS[lang]['start'], reply_markup=keyboard)

@dp.message(Command("language"))
async def cmd_language(message: Message):
    lang = get_user_lang(message.from_user.id)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🇺🇿 O'zbekcha", callback_data="lang_uz"),
            InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang_ru"),
            InlineKeyboardButton(text="🇬🇧 English", callback_data="lang_en")
        ]
    ])
    await message.answer(TEXTS[lang]['lang_prompt'], reply_markup=keyboard)

@dp.message(Command("help"))
async def cmd_help(message: Message):
    lang = get_user_lang(message.from_user.id)
    await message.answer(TEXTS[lang]['help'])

@dp.callback_query(F.data.startswith("lang_"))
async def change_language(callback: CallbackQuery):
    lang = callback.data.split("_")[1]
    set_user_lang(callback.from_user.id, lang)
    await callback.message.answer(TEXTS[lang]['lang_set'])
    await callback.answer()

@dp.callback_query(F.data.startswith("report_scam_"))
async def report_scam_callback(callback: CallbackQuery):
    domain = callback.data.replace("report_scam_", "")
    add_to_blacklist(callback.message.chat.id, domain)
    update_user_rep(callback.from_user.id, 5)
    await callback.message.edit_text(f"🚨 `{domain}` qora ro'yxatga qo'shildi! Rahmat (+5 karma).", parse_mode="Markdown")
    await callback.answer()

@dp.message(F.photo)
async def handle_photo(message: Message):
    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    file_bytes = await bot.download_file(file.file_path)
    
    # OpenCV orqali QR-kodni qidirish va o'qish
    np_arr = np.frombuffer(file_bytes.read(), np.uint8)
    cv_img = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
    qr_detector = cv2.QRCodeDetector()
    qr_data, _, _ = qr_detector.detectAndDecode(cv_img)
    
    qr_info = ""
    if qr_data:
        qr_info = f"\n\n🔍 **Topilgan QR-kod ma'lumoti:**\n`{qr_data}`"

    image = Image.open(BytesIO(file_bytes.getvalue()))
    await message.answer("🔄 Chek va QR-kod chuqur tahlil qilinmoqda...")
    try:
        response = ai_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[
                "Siz professional moliyaviy xavfsizlik ekspertisiz. "
                "Ushbu rasm to'lov cheki (Click, Payme, Uzum, bank) ekanligini tekshiring. "
                "1) Photoshop yoki montaj izlari bormi? "
                "2) Summa, vaqt va rekvizitlar mantiqiymi? "
                "Natijani: '✅ Haqiqiy chek' yoki '🚨 SOXTA/SHUBHALI CHEK!' deb boshlang.",
                image
            ]
        )
        
        analysis_result = f"🤖 **Tahlil Natijasi:**\n\n{response.text}{qr_info}"
        
        keyboard = None
        if "haqiqiy chek" in response.text.lower():
            analysis_result += "\n\n✅ **To'lov muvaffaqiyatli tasdiqlandi!**"

        await message.answer(analysis_result, parse_mode="Markdown", reply_markup=keyboard)
    except Exception:
        await message.answer("❌ Rasmni tahlil qilishda xatolik yuz berdi.")

@dp.message(F.text)
async def handle_message(message: Message):
    user_id = message.from_user.id
    add_user(user_id, message.from_user.username, message.from_user.full_name)
    lang = get_user_lang(user_id)
    chat_type = message.chat.type
    
    if chat_type == 'private':
        current_time = time.time()
        if user_id in user_last_message_time:
            if current_time - user_last_message_time[user_id] < SPAM_INTERVAL:
                await message.answer(TEXTS[lang]['spam'])
                return
        user_last_message_time[user_id] = current_time

    text = message.text.lower()
    found_scam = any(word in text for word in SCAM_WORDS)
    url = extract_url(message.text)
    
    link_status = "SAFE"
    domain = None
    if url:
        parsed = urlparse(url if url.startswith(('http://', 'https://')) else 'https://' + url)
        domain = parsed.netloc.lower()
        if domain.startswith('www.'):
            domain = domain[4:]
        link_status = analyze_link(url, message.chat.id)

    if chat_type in ['group', 'supergroup']:
        is_dangerous = found_scam or (link_status.startswith("DANGER"))
        if is_dangerous:
            try:
                await message.delete()
                new_rep = update_user_rep(user_id, -15)
                name = message.from_user.full_name
                alert_text = TEXTS[lang]['group_danger_alert'].format(user=name, uid=user_id, rep=new_rep)
                await message.answer(alert_text, parse_mode="Markdown")
                if new_rep <= 0:
                    await bot.ban_chat_member(message.chat.id, user_id)
            except Exception:
                pass
            return
        return

    response_parts = []
    keyboard = None
    if found_scam:
        response_parts.append(TEXTS[lang]['scam_word'])
    if url:
        if link_status.startswith("SAFE"):
            response_parts.append(f"🔗 **Link:**\n{TEXTS[lang]['safe_link']}")
        elif link_status.startswith("DANGER"):
            response_parts.append(f"🔗 **Link:**\n{TEXTS[lang]['danger_link']}")
        else:
            response_parts.append(f"🔗 **Link:**\n{TEXTS[lang]['warning_link']}")
            if domain:
                keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🚨 Qora ro'yxatga qo'shish", callback_data=f"report_scam_{domain}")]
                ])
    
    ai_res = await ask_gemini(message.text)
    if ai_res:
        response_parts.append(f"{TEXTS[lang]['ai_header']}\n{ai_res}")
        
    await message.answer("\n\n".join(response_parts), reply_markup=keyboard)

async def main():
    threading.Thread(target=run_http_server, daemon=True).start()
    print("Bot va veb-panel serveri ishga tushdi...")
    await bot.delete_webhook(drop_pending_updates=True)
    await set_default_commands(bot)
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
