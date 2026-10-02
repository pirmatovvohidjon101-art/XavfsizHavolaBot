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
from aiogram.types import Message, InlineQuery, InlineQueryResultArticle, InputTextMessageContent, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from google import genai
from PIL import Image

TOKEN = os.environ.get("8963497136:AAF44_6VpG5Uw4rlTjWS7kYUDv1HA8Bp0Jw")
ADMIN_ID = 5081583283  # O'z Telegram ID raqamingizni yozing

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
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
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            language TEXT DEFAULT 'uz'
        )
    """)
    conn.commit()
    conn.close()

init_db()

def add_user(user_id, username):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO users (user_id, username, language) VALUES (?, ?, 'uz')", (user_id, username))
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

def get_all_users():
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users")
    rows = cursor.fetchall()
    conn.close()
    return [row[0] for row in rows]

# --- TARJIMALAR ---
TEXTS = {
    'uz': {
        'start': "👋 Assalomu alaykum!\n\nMen O‘zbekistondagi rasmiy saytlar, banklar, OAV va shubhali havolalarni tekshiruvchi **Sun'iy Intellekt (Gemini AI)** bilan jihozlangan xavfsizlik botiman.\n\n🔍 Menga havola yoki xabar matnini yuboring!",
        'stats': "📊 **Bot Statistikasi:**\n\n🔍 Tekshirilgan havolalar: {checked}\n🚨 Xavfli havolalar: {danger}\n👥 Foydalanuvchilar: {users}",
        'lang_set': "✅ Til o'zbek tiliga o'zgartirildi.",
        'spam': "⚠️ Juda tez-tez xabar yuboryapsiz! Iltimos, biroz kuting.",
        'safe_link': "✅ Bu **rasmiy va ishonchli** manzil.",
        'danger_link': "🚨 **DIQQAT! XAVFLI HAVOLA!** Firibgarlar tuzog'i bo'lishi mumkin.",
        'warning_link': "⚠️ **Noma'lum havola.** Shaxsiy ma'lumotlarni kiritishda ehtiyot bo'ling!",
        'scam_word': "🛑 **DIQQAT! Matnda firibgarlikka xos so'zlar aniqlandi!**",
        'ai_header': "🤖 **Sun'iy Intellekt (AI) xulosasi:**",
        'clean': "✅ Matnda xavfli belgilar topilmadi, lekin baribir hushyor bo'ling."
    },
    'ru': {
        'start': "👋 Здравствуйте!\n\nЯ бот кибербезопасности с **ИИ (Gemini AI)** для проверки официальных сайтов Узбекистана, ссылок и подозрительных сообщений.\n\n🔍 Отправьте мне ссылку или текст!",
        'stats': "📊 **Статистика бота:**\n\n🔍 Проверено ссылок: {checked}\n🚨 Опасных ссылок: {danger}\n👥 Пользователей: {users}",
        'lang_set': "✅ Язык изменен на русский.",
        'spam': "⚠️ Слишком частые запросы! Пожалуйста, подождите.",
        'safe_link': "✅ Это **официальный и надежный** ресурс.",
        'danger_link': "🚨 **ВНИМАНИЕ! ОПАСНАЯ ССЫЛКА!** Возможно это мошенники.",
        'warning_link': "⚠️ **Неизвестная ссылка.** Будьте осторожны при вводе данных.",
        'scam_word': "🛑 **ВНИМАНИЕ! Обнаружены признаки мошенничества в тексте!**",
        'ai_header': "🤖 **Заключение ИИ:**",
        'clean': "✅ Опасных признаков не обнаружено, но будьте бдительны."
    },
    'en': {
        'start': "👋 Hello!\n\nI am a cybersecurity bot powered by **AI (Gemini AI)** to check official websites, links, and suspicious messages in Uzbekistan.\n\n🔍 Send me a link or text!",
        'stats': "📊 **Bot Statistics:**\n\n🔍 Checked links: {checked}\n🚨 Dangerous links: {danger}\n👥 Users: {users}",
        'lang_set': "✅ Language changed to English.",
        'spam': "⚠️ You are sending messages too fast! Please wait.",
        'safe_link': "✅ This is an **official and trusted** resource.",
        'danger_link': "🚨 **ATTENTION! DANGEROUS LINK!** This might be a scam.",
        'warning_link': "⚠️️ **Unknown link.** Be careful when entering your personal data.",
        'scam_word': "🛑 **ATTENTION! Scam patterns detected in the text!**",
        'ai_header': "🤖 **AI Analysis:**",
        'clean': "✅ No dangerous elements found, but stay vigilant."
    }
}

SUSPICIOUS_TLDS = ['.xyz', '.cc', '.tk', '.buzz', '.top', '.gq', '.ml', '.cf', '.ru.com', '.online', '.site', '.club', '.work', '.click', '.link', '.pw', '.su', '.bid', '.loan', '.win', '.stream', '.icu', '.cam', '.cfd', '.VIP']

OFFICIAL_DOMAINS = {
    'gov.uz', 'my.gov.uz', 'pm.gov.uz', 'lex.uz', 'cbu.uz', 'stat.uz', 'customs.uz',
    'soliq.uz', 'my.soliq.uz', 'uzgidromet.uz', 'mehnat.uz', 'my.mehnat.uz',
    'iiv.uz', 'mfa.uz', 'minjust.uz', 'uzedu.uz', 'ssv.uz', 'tiiame.uz',
    'muslim.uz', 'fatvo.uz', 'quran.uz', 'ziyouz.uz', 'buxari.uz',
    'nbu.uz', 'agrobank.uz', 'kapitalbank.uz', 'ipotekabank.uz', 'davrbank.uz',
    'orientfinanzbank.uz', 'hamkorbank.uz', 'asakabank.uz', 'anorbank.uz',
    'tbcbank.uz', 'octobank.uz', 'infinbank.uz', 'ipakyulibank.uz', 'aloqabank.uz',
    'trastbank.uz', 'sqb.uz', 'mkbank.uz', 'uzcard.uz', 'humocard.uz', 
    'click.uz', 'payme.uz', 'uzum.uz', 'uzummarket.uz', 'paynet.uz',
    'texnomart.uz', 'asaxiy.uz', 'olcha.uz', 'express24.uz', 'kun.uz', 'gazeta.uz', 'daryo.uz'
}

OFFICIAL_TELEGRAM = {'muslimuzportal', 'fatvouz', 'ziyouz', 'hilolnashr', 'islomuz', 'davxizmat', 'uzgovuz', 'soliquz', 'cbu_uz', 'agrobank_uz', 'kapitalbank_uz', 'anorbank', 'tbcbankuz', 'octobank', 'infinbank', 'kunuzofficial', 'gazetauz', 'daryo', 'asaxiy', 'olchouz', 'texnomart', 'uzummarket', 'clickuz', 'payme_uz', 'uzcard_uz'}

BRAND_KEYWORDS = ['muslim', 'fatvo', 'hilol', 'ziyouz', 'uzcard', 'humo', 'soliq', 'mygov', 'agrobank', 'kapitalbank', 'anorbank', 'tbc', 'octobank', 'infinbank', 'uzum', 'beeline', 'ucell', 'mobiuz', 'uztelecom', 'click', 'payme', 'asaxiy', 'olcha', 'texnomart', 'express24']

SCAM_WORDS = ['yutib oldingiz', 'bonus', 'sovg', 'pul ishlang', 'aktsiya', 'keshbek', 'konkurs', 'tekin', 'free money', 'выиграли', 'бонус', 'акция', 'розыгрыш', 'free']

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
dp = Dispatcher()

# Render port talab qilgani uchun oddiy HTTP server
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
    url_pattern = re.compile(r'https?://[^\s]+|www\.[^\s]+|[a-zA-Z0-9][-a-zA-Z0-9()@:%._\+~#=]{1,256}\.[a-zA-Z0-9()]{1,6}\b(?:[-a-zA-Z0-9()@:%_\+.~#?&//=]*)')
    match = url_pattern.search(text)
    return match.group(0) if match else None

def analyze_link(url: str, lang: str) -> str:
    global stats
    stats["checked_count"] += 1
    url_lower = url.lower()

    if "t.me/" in url_lower or "telegram.me/" in url_lower:
        parts = url_lower.split("t.me/")
        if len(parts) > 1:
            path = parts[1].split("/")[0].strip()
            if path in OFFICIAL_TELEGRAM:
                return TEXTS[lang]['safe_link'] + f" (@{path})"
            return f"⚠️ Telegram channel/group (@{path}). Be careful, it might be fake!"

    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url
        
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    if domain.startswith('www.'):
        domain = domain[4:]
        
    if domain in OFFICIAL_DOMAINS:
        return TEXTS[lang]['safe_link']
        
    for tld in SUSPICIOUS_TLDS:
        if domain.endswith(tld):
            stats["danger_count"] += 1
            return TEXTS[lang]['danger_link']
            
    for brand in BRAND_KEYWORDS:
        if brand in domain:
            stats["danger_count"] += 1
            return TEXTS[lang]['danger_link']

    return TEXTS[lang]['warning_link']

async def ask_gemini(text: str) -> str:
    try:
        prompt = f"Analyze if this text contains scam, phishing, or fraud patterns. Keep it brief and concise:\n\n\"{text}\""
        response = ai_client.models.generate_content(model='gemini-2.5-flash', contents=prompt)
        return response.text
    except Exception:
        return ""

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

@dp.message(Command("broadcast"))
async def cmd_broadcast(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("❌ Xabar matnini kiriting: /broadcast Xabar...")
        return
    broadcast_text = args[1]
    users = get_all_users()
    success = 0
    for uid in users:
        try:
            await bot.send_message(uid, f"📢 **Muhim xabar:**\n\n{broadcast_text}")
            success += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass
    await message.answer(f"✅ Xabar {success} ta foydalanuvchiga yuborildi.")

@dp.message(F.text)
async def handle_message(message: Message):
    user_id = message.from_user.id
    lang = get_user_lang(user_id)
    current_time = time.time()
    
    if user_id in user_last_message_time:
        if current_time - user_last_message_time[user_id] < SPAM_INTERVAL:
            await message.answer(TEXTS[lang]['spam'])
            return
    user_last_message_time[user_id] = current_time

    text = message.text.lower()
    found_scam = any(word in text for word in SCAM_WORDS)
    url = extract_url(message.text)
    
    response_parts = []
    if found_scam:
        response_parts.append(TEXTS[lang]['scam_word'])
    if url:
        res = analyze_link(url, lang)
        response_parts.append(f"🔗 **Link analysis:**\n{res}")
    
    ai_res = await ask_gemini(message.text)
    if ai_res:
        response_parts.append(f"{TEXTS[lang]['ai_header']}\n{ai_res}")
        
    if not response_parts:
        response_parts.append(TEXTS[lang]['clean'])
        
    await message.answer("\n\n".join(response_parts))

async def main():
    # Render port talabini qondirish uchun HTTP serverni alohida oqimda (thread) ishga tushiramiz
    threading.Thread(target=run_http_server, daemon=True).start()
    print("Bot va veb-server ishga tushdi...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
