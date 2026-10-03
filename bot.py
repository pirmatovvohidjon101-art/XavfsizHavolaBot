import asyncio
import logging
import re
import os
import time
import sqlite3
import threading
from io import BytesIO
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BotCommand
from google import genai
from PIL import Image

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("BOT_TOKEN topilmadi! Render'dagi Environment bo'limiga BOT_TOKEN qo'shganingizni tekshiring.")

ADMIN_ID = 5081583283  # O'z Telegram ID raqamingizni yozing

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ai_client = genai.Client(api_key=GEMINI_API_KEY)

stats = {
    "checked_count": 0,
    "danger_count": 0
}

user_last_message_time = {}
SPAM_INTERVAL = 1.5

# --- BAZA BILAN ISHLASH (SQLITE) ---
def init_db():
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    # Foydalanuvchilar jadvali (Karma / Reputation tizimi bilan)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            language TEXT DEFAULT 'uz',
            reputation INTEGER DEFAULT 100
        )
    """)
    # Guruhlar uchun Oq ro'yxat (Whitelist) va Qora ro'yxat (Blacklist)
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

def add_user(user_id, username):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO users (user_id, username, language, reputation) VALUES (?, ?, 'uz', 100)", (user_id, username))
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

def get_user_rep(user_id):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT reputation FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else 100

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
        'start': "👋 Assalomu alaykum!\n\nMen xavfsizlik va moderatsiya botiman. Guruhlarda shubhali havolalar, zararli fayllar (.apk), ovozli xabarlar va firibgarliklarni nazorat qilaman.",
        'stats': "📊 **Bot Statistikasi:**\n\n🔍 Tekshirilgan havolalar: {checked}\n🚨 Xavfli havolalar: {danger}\n👥 Foydalanuvchilar: {users}",
        'lang_set': "✅ Til o'zbek tiliga o'zgartirildi.",
        'help': "ℹ️ **Qo'llanma:**\n- Havola, matn, rasm yoki QR-kod yuborib tekshirishingiz mumkin.\n- **Guruhlarda:** APK fayllar, zararli havolalar va scam xabarlar o'chiriladi.\n- `/whitelist domen` — Oq ro'yxatga qo'shish\n- `/blacklist domen` — Qora ro'yxatga qo'shish\n👨‍‍💻 Admin: @thePirmatov",
        'lang_prompt': "🌐 Marhamat, tilni tanlang:",
        'spam': "⚠️ Juda tez-tez xabar yuboryapsiz! Iltimos, biroz kuting.",
        'safe_link': "✅ Bu rasmiy va ishonchli manzil.",
        'danger_link': "🚨 DIQQAT! XAVFLI HAVOLA! Firibgarlar tuzog'i bo'lishi mumkin.",
        'warning_link': "⚠️ Noma'lum havola. Shaxsiy ma'lumotlarni kiritishda ehtiyot bo'ling!",
        'scam_word': "🛑 DIQQAT! Matnda firibgarlikka xos so'zlar aniqlandi!",
        'ai_header': "🤖 Sun'iy Intellekt (AI) xulosasi:",
        'clean': "✅ Matnda xavfli belgilar topilmadi.",
        'group_danger_alert': "🚨 DIQQAT! [{user}](tg://user?id={uid}) xavfli xabar/havola yuborgani uchun xabar o'chirildi va karma ochkosi kamaytirildi! (Reputation: {rep})",
        'file_danger': "🚨 DIQQAT! Guruhda zararli yoki shubhali fayl (.apk / .exe) aniqlandi va o'chirildi!",
        'voice_danger': "🚨 DIQQAT! Ovozli xabarda firibgarlik alomatlari aniqlandi!"
    },
    'ru': {
        'start': "👋 Здравствуйте!\n\nЯ бот безопасности и модерации. Контролирую группы на наличие опасных ссылок, вредоносных файлов (.apk), голосовых и мошенничества.",
        'stats': "📊 **Статистика бота:**\n\n🔍 Проверено ссылок: {checked}\n🚨 Опасных ссылок: {danger}\n👥 Пользователей: {users}",
        'lang_set': "✅ Язык изменен на русский.",
        'help': "ℹ️ **Справка:**\n- Проверяю ссылки, текст, фото, файлы.\n- `/whitelist домен` — добавить в белый список\n- `/blacklist домен` — добавить в черный список",
        'lang_prompt': "🌐 Пожалуйста, выберите язык:",
        'spam': "⚠️ Слишком частые запросы!",
        'safe_link': "✅ Это официальный ресурс.",
        'danger_link': "🚨 ВНИМАНИЕ! ОПАСНАЯ ССЫЛКА!",
        'warning_link': "⚠️ Неизвестная ссылка.",
        'scam_word': "🛑 Обнаружены признаки мошенничества!",
        'ai_header': "🤖 Заключение ИИ:",
        'clean': "✅ Опасных признаков не обнаружено.",
        'group_danger_alert': "🚨 ВНИМАНИЕ! Сообщение от [{user}](tg://user?id={uid}) удалено за нарушение безопасности! (Reputation: {rep})",
        'file_danger': "🚨 ВНИМАНИЕ! В группе обнаружен и удален подозрительный файл (.apk/.exe)!",
        'voice_danger': "🚨 ВНИМАНИЕ! В голосовом сообщении обнаружены признаки мошенничества!"
    },
    'en': {
        'start': "👋 Hello!\n\nI am a security and moderation bot protecting groups from dangerous links, malicious files (.apk), voice scams, and fraud.",
        'stats': "📊 **Bot Statistics:**\n\n🔍 Checked links: {checked}\n🚨 Dangerous links: {danger}\n👥 Users: {users}",
        'lang_set': "✅ Language changed to English.",
        'help': "ℹ️ **Help:**\n- Check links, text, files.\n- `/whitelist domain`\n- `/blacklist domain`",
        'lang_prompt': "🌐 Please select a language:",
        'spam': "⚠️ Too fast requests!",
        'safe_link': "✅ Official resource.",
        'danger_link': "🚨 ATTENTION! DANGEROUS LINK!",
        'warning_link': "⚠️ Unknown link.",
        'scam_word': "🛑 Scam patterns detected!",
        'ai_header': "🤖 AI Analysis:",
        'clean': "✅ No dangerous elements found.",
        'group_danger_alert': "🚨 ATTENTION! Message from [{user}](tg://user?id={uid}) deleted due to security violation! (Reputation: {rep})",
        'file_danger': "🚨 ATTENTION! Suspicious file (.apk/.exe) detected and deleted!",
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
        BotCommand(command="whitelist", description="➕ Oq ro'yxatga qo'shish"),
        BotCommand(command="blacklist", description="➖ Qora ro'yxatga qo'shish"),
        BotCommand(command="language", description="🌐 Tilni o'zgartirish"),
        BotCommand(command="help", description="ℹ️ Qo'llanma")
    ]
    await bot.set_my_commands(commands)

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running!")

def run_http_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
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

    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url
        
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    if domain.startswith('www.'):
        domain = domain[4:]
        
    # 4-band: Guruh uchun Whitelist / Blacklist tekshiruvi
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
        prompt = f"Analyze if this text contains scam, phishing, or fraud patterns. Keep it brief and concise:\n\n\"{text}\""
        response = ai_client.models.generate_content(model='gemini-2.5-flash', contents=prompt)
        return response.text
    except Exception:
        return ""

# --- ADMIN BUYRUQLARI: WHITELIST / BLACKLIST ---
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
    await message.answer(f"✅ `{domain}` ushbu guruhning Oq ro'yxatiga qo'shildi.", parse_mode="Markdown")

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
    await message.answer(f"✅ `{domain}` ushbu guruhning Qora ro'yxatiga qo'shildi.", parse_mode="Markdown")

@dp.message(Command("start"))
async def cmd_start(message: Message):
    add_user(message.from_user.id, message.from_user.username)
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

@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    lang = get_user_lang(message.from_user.id)
    users_count = len(get_all_users())
    text = TEXTS[lang]['stats'].format(checked=stats['checked_count'], danger=stats['danger_count'], users=users_count)
    await message.answer(text)

# --- 2-BAND: FAYLLARNI TEKSHIRISH (.apk, .exe va hokazo) ---
@dp.message(F.document)
async def handle_document(message: Message):
    chat_type = message.chat.type
    user_id = message.from_user.id
    lang = get_user_lang(user_id)
    
    doc = message.document
    file_name = doc.file_name.lower() if doc.file_name else ""
    
    is_dangerous_file = any(file_name.endswith(ext) for ext in DANGEROUS_EXTENSIONS)
    
    if chat_type in ['group', 'supergroup'] and is_dangerous_file:
        try:
            await message.delete()
            new_rep = update_user_rep(user_id, -20)
            alert_text = TEXTS[lang]['file_danger'] + f" (User Karma: {new_rep})"
            await message.answer(alert_text)
            
            # Agar karma juda pasayib ketsa, foydalanuvchini guruhdan cheklash mumkin
            if new_rep <= 0:
                await bot.ban_chat_member(message.chat.id, user_id)
                await message.answer(f"🚫 [{message.from_user.full_name}](tg://user?id={user_id}) karma ochkosi tugagani uchun ban qilindi!", parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Faylni o'chirishda xatolik: {e}")
        return

    if chat_type == 'private':
        if is_dangerous_file:
            await message.answer("🚨 DIQQAT! Bu fayl zararli (.apk/.exe) bo'lishi mumkin. Ochish tavsiya etilmaydi!")
        else:
            await message.answer("✅ Fayl qabul qilindi. Hozircha xavfli belgilar topilmadi.")

# --- 3-BAND: OVOZLI XABARLARNI (VOICE) TAHLIL QILISH ---
@dp.message(F.voice)
async def handle_voice(message: Message):
    chat_type = message.chat.type
    user_id = message.from_user.id
    lang = get_user_lang(user_id)
    
    if chat_type == 'private':
        await message.answer("🔄 Ovozli xabar qabul qilindi (Hozirgi versiyada ovozli transkripsiya ustida ishlanmoqda).")

# --- RASMLARNI TEKSHIRISH ---
@dp.message(F.photo)
async def handle_photo(message: Message):
    user_id = message.from_user.id
    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    file_bytes = await bot.download_file(file.file_path)
    image = Image.open(BytesIO(file_bytes.read()))
    
    await message.answer("🔄 Rasm va QR-kod tahlil qilinmoqda...")
    try:
        response = ai_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=["Ushbu rasm yoki QR-kod ichidagi matn va havolalarni o'qing, ularda firibgarlik (scam/phishing) xavfi bor-yo'qligini tushuntiring:", image]
        )
        await message.answer(f"🤖 **Rasm tahlili natijasi:**\n\n{response.text}")
    except Exception:
        await message.answer("❌ Rasmni tahlil qilishda xatolik.")

# --- MATN VA HAVOLALARNI MODERATSIYA QILISH ---
@dp.message(F.text)
async def handle_message(message: Message):
    user_id = message.from_user.id
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
    if url:
        link_status = analyze_link(url, message.chat.id)

    # --- 1-BAND & GURUH MODERATSIYASI ---
    if chat_type in ['group', 'supergroup']:
        is_dangerous = found_scam or (link_status.startswith("DANGER"))
        if is_dangerous:
            try:
                await message.delete()
                # Karma ochkosini pasaytirish (-15 ball)
                new_rep = update_user_rep(user_id, -15)
                name = message.from_user.full_name
                alert_text = TEXTS[lang]['group_danger_alert'].format(user=name, uid=user_id, rep=new_rep)
                await message.answer(alert_text, parse_mode="Markdown")
                
                if new_rep <= 0:
                    await bot.ban_chat_member(message.chat.id, user_id)
                    await message.answer(f"🚫 [{name}](tg://user?id={user_id}) karma ochkosi 0 dan tushgani uchun guruhdan cheklandi!", parse_mode="Markdown")
            except Exception as e:
                logging.error(f"Guruh xabarini boshqarishda xatolik: {e}")
            return
        return

    # --- SHAXSIY XABARLAR (PM) ---
    response_parts = []
    if found_scam:
        response_parts.append(TEXTS[lang]['scam_word'])
    if url:
        if link_status.startswith("SAFE"):
            response_parts.append(f"🔗 **Link analysis:**\n{TEXTS[lang]['safe_link']}")
        elif link_status.startswith("DANGER"):
            response_parts.append(f"🔗 **Link analysis:**\n{TEXTS[lang]['danger_link']}")
        else:
            response_parts.append(f"🔗 **Link analysis:**\n{TEXTS[lang]['warning_link']}")
    
    ai_res = await ask_geministr = await ask_gemini(message.text)
    if ai_res:
        response_parts.append(f"{TEXTS[lang]['ai_header']}\n{ai_res}")
        
    if not response_parts:
        response_parts.append(TEXTS[lang]['clean'])
        
    await message.answer("\n\n".join(response_parts))

async def main():
    threading.Thread(target=run_http_server, daemon=True).start()
    print("Bot va veb-server ishga tushdi...")
    await bot.delete_webhook(drop_pending_updates=True)
    await set_default_commands(bot)
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
