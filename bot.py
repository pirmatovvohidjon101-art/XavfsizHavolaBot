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
from urllib.parse import urlparse
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BotCommand, BufferedInputFile
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
    "voice_danger_count": 0,
    "video_danger_count": 0,
    "screenshot_count": 0
}

user_last_message_time = {}
SPAM_INTERVAL = 1.2

# --- BAZA BILAN ISHLASH ---
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
    safe_username = str(username)[:50] if username else ""
    safe_fullname = str(full_name)[:100] if full_name else "Foydalanuvchi"
    
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO users (user_id, username, full_name, language, reputation) 
        VALUES (?, ?, ?, 'uz', 100)
        ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, full_name=excluded.full_name
    """, (user_id, safe_username, safe_fullname))
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
        'start': "👋 Assalomu alaykum!\n\nMen to'liq himoyalangan AI xavfsizlik botiman. Havolalar, fayllar, cheklar, ovozli xabarlar, Deepfake videolar va sayt skrinshotlarini tahlil qilaman.",
        'stats': "📊 **Bot Statistikasi:**\n\n🔍 Tekshirilgan havolalar: {checked}\n🚨 Xavfli havolalar: {danger}\n📸 Sayt skrinshotlari: {screenshot}\n📦 Xavfli fayllar: {file_danger}\n🎙️ Xavfli ovozlar: {voice}\n🎬 Feyk videolar: {video}\n👥 Foydalanuvchilar: {users}",
        'lang_set': "✅ Til o'zbek tiliga o'zgartirildi.",
        'help': "ℹ️ **Qo'llanma:**\n- Havola yuborsangiz, saytning skrinshoti olinib AI orqali tekshiriladi.\n- `/top` — Reyting\n- `/web` — Admin veb-paneli",
        'lang_prompt': "🌐 Marhamat, tilni tanlang:",
        'spam': "⚠️ Juda tez-tez xabar yuboryapsiz! Iltimos, biroz kuting.",
        'safe_link': "✅ Bu rasmiy va ishonchli manzil.",
        'danger_link': "🚨 DIQQAT! XAVFLI / PHISHING HAVOLA ANIQLANDI!",
        'warning_link': "⚠️ Noma'lum havola. Skrinshot va tahlil qilinmoqda...",
        'scam_word': "🛑 DIQQAT! Matnda firibgarlikka xos so'zlar aniqlandi!",
        'ai_header': "🤖 Sun'iy Intellekt (AI) xulosasi:",
        'group_danger_alert': "🚨 DIQQAT! [{user}](tg://user?id={uid}) xavfli kontent yuborgani uchun xabar o'chirildi va karma kamaytirildi! (Reputation: {rep})",
        'voice_danger': "🚨 DIQQAT! Ovozli xabarda firibgarlik alomatlari aniqlandi!",
        'file_danger': "🚨 DIQQAT! Zararli fayl (APK/Malware) bloklandi!",
        'video_danger': "🚨 DIQQAT! Ushbu videoda Deepfake / soxta montaj aniqlandi!",
        'file_too_large': "⚠️ Fayl hajmi juda katta."
    }
}

SUSPICIOUS_TLDS = ['.xyz', '.cc', '.tk', '.buzz', '.top', '.gq', '.ml', '.cf', '.ru.com', '.online', '.site', '.club', '.work', '.click', '.link', '.pw', '.su', '.bid', '.loan', '.win', '.stream', '.icu', '.cam', '.cfd', '.VIP']
DANGEROUS_FILE_EXTENSIONS = ['.apk', '.exe', '.bat', '.scr', '.js', '.vbs', '.cmd', '.msi', '.pif', '.com']

OFFICIAL_DOMAINS = {
    'gov.uz', 'my.gov.uz', 'pm.gov.uz', 'lex.uz', 'cbu.uz', 'stat.uz', 'customs.uz',
    'soliq.uz', 'my.soliq.uz', 'uzgidromet.uz', 'mehnat.uz', 'my.mehnat.uz',
    'iiv.uz', 'mfa.uz', 'minjust.uz', 'uzedu.uz', 'ssv.uz', 'tiiame.uz',
    'muslim.uz', 'fatvo.uz', 'quran.uz', 'ziyouz.uz', 'buxari.uz', 'hilolnashr.uz',
    'nbu.uz', 'agrobank.uz', 'kapitalbank.uz', 'ipotekabank.uz', 'davrbank.uz',
    'orientfinanzbank.uz', 'hamkorbank.uz', 'asakabank.uz', 'anorbank.uz',
    'tbcbank.uz', 'octobank.uz', 'infinbank.uz', 'ipakyulibank.uz', 'aloqabank.uz',
    'uzcard.uz', 'humocard.uz', 'click.uz', 'payme.uz', 'uzum.uz', 'uzummarket.uz', 
    'uzumbank.uz', 'paynet.uz', 'humans.uz',
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
        BotCommand(command="help", description="ℹ️ Qo'llanma")
    ]
    await bot.set_my_commands(commands)

# --- WEB PANEL ---
class WebPanelHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed_path = urlparse(self.path)
        path = parsed_path.path
        
        if path == "/" or path == "/health":
            self.send_response(200)
            self.send_header("Content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Bot and Web Panel are running safely!")
            return
            
        if path == "/admin":
            conn = sqlite3.connect("bot_database.db")
            cursor = conn.cursor()
            cursor.execute("SELECT user_id, username, full_name, reputation FROM users")
            users = cursor.fetchall()
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
                </style>
            </head>
            <body>
                <h1>🛡️ Bot Admin Boshqaruv Paneli</h1>
                <div class="card">
                    <h3>📊 Statistika</h3>
                    <p>Tekshirilgan havolalar: <b>{stats['checked_count']}</b></p>
                    <p>Bloklangan xavfli havolalar: <b>{stats['danger_count']}</b></p>
                    <p>Olingan skrinshotlar: <b>{stats['screenshot_count']}</b></p>
                    <p>Jami foydalanuvchilar: <b>{len(users)}</b></p>
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

    def log_message(self, format, *args):
        return

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
    return None

# --- SKRINSHOT OLISH MODULI ---
def get_webpage_screenshot(url: str) -> bytes:
    try:
        full_url = url if url.startswith(('http://', 'https://')) else 'https://' + url
        api_url = f"https://api.microlink.io/?url={full_url}&screenshot=true&meta=false&embed=screenshot.url"
        response = requests.get(api_url, timeout=7)
        res_json = response.json()
        if res_json.get('status') == 'success':
            img_url = res_json['data']['screenshot']['url']
            img_data = requests.get(img_url, timeout=7).content
            return img_data
    except Exception:
        pass
    return None

@dp.message(Command("start"))
async def cmd_start(message: Message):
    add_user(message.from_user.id, message.from_user.username, message.from_user.full_name)
    await message.answer(TEXTS['uz']['start'])

@dp.message(Command("top"))
async def cmd_top(message: Message):
    top_users = get_top_users(10)
    text = "🏆 **Eng hushyor foydalanuvchilar reytingi (Karma):**\n\n"
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

# --- MATN VA HAVOLALARNI TEKSHIRISH + SKRINSHOT ---
@dp.message(F.text)
async def handle_message(message: Message):
    user_id = message.from_user.id
    add_user(user_id, message.from_user.username, message.from_user.full_name)
    lang = get_user_lang(user_id)
    chat_type = message.chat.type
    
    current_time = time.time()
    if user_id in user_last_message_time:
        if current_time - user_last_message_time[user_id] < SPAM_INTERVAL:
            await message.answer(TEXTS[lang]['spam'])
            return
    user_last_message_time[user_id] = current_time

    text = message.text.lower()
    found_scam = any(word in text for word in SCAM_WORDS)
    url = extract_url(message.text)
    
    if chat_type in ['group', 'supergroup']:
        if found_scam:
            try:
                await message.delete()
                new_rep = update_user_rep(user_id, -15)
                alert_text = TEXTS[lang]['group_danger_alert'].format(user=message.from_user.full_name, uid=user_id, rep=new_rep)
                await message.answer(alert_text, parse_mode="Markdown")
            except Exception:
                pass
            return

    if url:
        stats["checked_count"] += 1
        parsed = urlparse(url if url.startswith(('http://', 'https://')) else 'https://' + url)
        domain = parsed.netloc.lower()
        if domain.startswith('www.'):
            domain = domain[4:]

        if domain.endswith('.gov.uz') or domain in OFFICIAL_DOMAINS:
            await message.answer(f"🔗 **Link:**\n{TEXTS[lang]['safe_link']}")
            return

        # Skrinshot olish va Gemini orqali tahlil qilish
        await message.answer("📸 Sayt skrinshoti olinib, AI orqali phishing tekshiruvi bajarilmoqda...")
        screenshot_bytes = get_webpage_screenshot(url)
        
        if screenshot_bytes:
            stats["screenshot_count"] += 1
            image = Image.open(BytesIO(screenshot_bytes))
            try:
                response = ai_client.models.generate_content(
                    model='gemini-2.5-flash',
                    contents=[
                        "You are a cybersecurity expert. Analyze this webpage screenshot carefully. "
                        "Is this a phishing website, fake login page imitating banks/payment systems (Click, Payme, Uzum), "
                        "or a fraudulent scam scheme? "
                        "Start your response strictly with '🚨 PHISHING/SCAM' if it is dangerous, or '✅ SAFE' if it looks legitimate.",
                        image
                    ]
                )
                ai_analysis = response.text
                if "PHISHING" in ai_analysis.upper() or "SCAM" in ai_analysis.upper():
                    stats["danger_count"] += 1
                    input_file = BufferedInputFile(screenshot_bytes, filename="screenshot.jpg")
                    await message.answer_photo(
                        photo=input_file,
                        caption=f"🚨 **DIQQAT! SOXTA / PHISHING SAYT ANIQLANDI!**\n\n{ai_analysis}",
                        parse_mode="Markdown"
                    )
                else:
                    await message.answer(f"✅ Sayt skrinshoti tekshirildi. Xavfli alomatlar topilmadi.\n\n{ai_analysis}")
            except Exception:
                await message.answer("❌ Skrinshotni AI yordamida tahlil qilishda xatolik yuz berdi.")
        else:
            await message.answer("⚠️ Saytga kirib bo'lmadi yoki skrinshot olish imkoni bo'lmadi (sayt himoyalangan bo'lishi mumkin).")

async def main():
    threading.Thread(target=run_http_server, daemon=True).start()
    print("Skrinshot va Phishing tahlil moduli qo'shilgan bot ishga tushdi...")
    await bot.delete_webhook(drop_pending_updates=True)
    await set_default_commands(bot)
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
