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
    "screenshot_count": 0,
    "audit_count": 0
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

# --- TARJIMALAR ---
TEXTS = {
    'uz': {
        'start': "👋 Assalomu alaykum!\n\nMen to'liq himoyalangan AI kiber-xavfsizlik botiman. Havolalar, fayllar, ovozli xabarlar, Deepfake videolar, skrinshotlar va Avtonom Kiber-Detektor funksiyalariga egaman.",
        'stats': "📊 **Bot Statistikasi:**\n\n🔍 Tekshirilgan havolalar: {checked}\n🚨 Xavfli havolalar: {danger}\n🕵️‍♂️ Kiber-Auditlar: {audit}\n📸 Skrinshotlar: {screenshot}\n🎙️ Ovozli vishinglar: {voice}\n🎬 Feyk videolar: {video}\n👥 Foydalanuvchilar: {users}",
        'help': "ℹ️ **Qo'llanma:**\n- `/audit <kanal_oki_havola>` — Avtonom detektiv tekshiruvi\n- Istalgan havola, fayl, video yoki ovozli xabar yuboring.",
        'spam': "⚠️ Juda tez-tez xabar yuboryapsiz! Iltimos, biroz kuting.",
        'safe_link': "✅ Bu rasmiy va ishonchli manzil.",
        'danger_link': "🚨 DIQQAT! XAVFLI / PHISHING HAVOLA ANIQLANDI!",
        'voice_danger': "🚨 DIQQAT! Ovozli xabarda Vishing / Voice Cloning (sun'iy ovoz) alomatlari aniqlandi!",
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

BRAND_KEYWORDS = ['muslim', 'fatvo', 'hilol', 'ziyouz', 'uzcard', 'humo', 'soliq', 'mygov', 'agrobank', 'kapitalbank', 'anorbank', 'tbc', 'octobank', 'infinbank', 'uzum', 'beeline', 'ucell', 'mobiuz', 'uztelecom', 'click', 'payme', 'asaxiy', 'olcha', 'texnomart', 'express24']
SCAM_WORDS = ['yutib oldingiz', 'bonus', 'sovg', 'pul ishlang', 'aktsiya', 'keshbek', 'konkurs', 'tekin', 'free money', 'выиграли', 'бонус', 'акция', 'розыгрыш', 'free']

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
dp = Dispatcher()

async def set_default_commands(bot: Bot):
    commands = [
        BotCommand(command="start", description="🚀 Botni ishga tushirish"),
        BotCommand(command="audit", description="🕵️‍♂️ Kiber-Detektiv audit"),
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
        if parsed_path.path == "/" or parsed_path.path == "/health":
            self.send_response(200)
            self.send_header("Content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Bot and Web Panel are running safely!")
            return
            
        if parsed_path.path == "/admin":
            conn = sqlite3.connect("bot_database.db")
            cursor = conn.cursor()
            cursor.execute("SELECT user_id, username, full_name, reputation FROM users")
            users = cursor.fetchall()
            conn.close()
            
            html = f"""
            <!DOCTYPE html>
            <html>
            <head><title>Admin Panel</title><meta charset="utf-8"></head>
            <body style="font-family: Arial; padding: 20px;">
                <h1>🛡️ Bot Admin Boshqaruv Paneli</h1>
                <p>Tekshirilgan havolalar: <b>{stats['checked_count']}</b></p>
                <p>Kiber-Auditlar: <b>{stats['audit_count']}</b></p>
                <p>Bloklangan vishing ovozlar: <b>{stats['voice_danger_count']}</b></p>
                <p>Jami foydalanuvchilar: <b>{len(users)}</b></p>
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
    for word in text.split():
        clean = word.strip(".,;:!?()[]{}\"'")
        if "t.me/" in clean.lower() or "telegram.me/" in clean.lower() or clean.startswith(('http://', 'https://', 'www.')):
            return clean
        if clean.startswith('@') and len(clean) > 1:
            return f"t.me/{clean[1:]}"
    return None

def get_webpage_screenshot(url: str) -> bytes:
    try:
        full_url = url if url.startswith(('http://', 'https://')) else 'https://' + url
        api_url = f"https://api.microlink.io/?url={full_url}&screenshot=true&meta=false&embed=screenshot.url"
        response = requests.get(api_url, timeout=7)
        res_json = response.json()
        if res_json.get('status') == 'success':
            return requests.get(res_json['data']['screenshot']['url'], timeout=7).content
    except Exception:
        pass
    return None

# --- 1. FEATURE: AUTONOMOUS KIBER-DETEKTIV AGENT (/audit) ---
@dp.message(Command("audit"))
async def cmd_audit(message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("❌ Foydalanish: `/audit <kanal_username yoki havola>`\n\nMasalan: `/audit @shubhali_kanal` yoki `/audit https://example.com`", parse_mode="Markdown")
        return
    
    target = args[1].strip()
    stats["audit_count"] += 1
    await message.answer(f"🕵️‍♂️ **Avtonom Kiber-Detektiv Agent** ishga tushdi...\nTarget: `{target}` tahlil qilinmoqda...", parse_mode="Markdown")

    try:
        response = ai_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=f"""
            You are an elite Autonomous Cybersecurity & OSINT Intelligence Agent. 
            Perform a thorough forensic security audit and threat analysis on this target: '{target}'.
            Provide a professional intelligence report in Uzbek language covering:
            1. Xavf darajasi (Risk Level: Xavfsiz, Shubhali yoki Yuqori Xavfli).
            2. Potentsial firibgarlik sxemalari (Scam patterns, fake investment, phishing).
            3. Texnik tahlil va tavsiyalar (Technical insights and safety advice).
            Format it cleanly with Markdown.
            """
        )
        report = f"🛡️ **KIBER-DETEKTIV AUDIT HISOBOTI**\n\n{response.text}"
        await message.answer(report, parse_mode="Markdown")
    except Exception:
        await message.answer("❌ Audit jarayonida xatolik yuz berdi.")

# --- 2. FEATURE: ADVANCED VOICE CLONING / VISHING DETECTOR ---
@dp.message(F.voice)
async def handle_voice(message: Message):
    user_id = message.from_user.id
    add_user(user_id, message.from_user.username, message.from_user.full_name)
    lang = get_user_lang(user_id)
    chat_type = message.chat.type

    voice = message.voice
    if voice.file_size and voice.file_size > 15 * 1024 * 1024:
        await message.answer(TEXTS[lang]['file_too_large'])
        return

    if chat_type == 'private':
        await message.answer("🎙️ Ovozli xabar akustik va sun'iy intellekt (Voice Cloning / Vishing) orqali tahlil qilinmoqda...")

    file = await bot.get_file(voice.file_id)
    file_bytes = await bot.download_file(file.file_path)
    audio_data = file_bytes.read()

    try:
        response = ai_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[
                "Listen to this audio carefully as an expert audio forensics and cybersecurity analyst. "
                "Detect if this voice message contains signs of Voice Cloning (Deepfake voice), artificial speech synthesis, "
                "or vishing (social engineering financial scam asking for money or urgent transfers). "
                "Answer strictly with 'DANGER' if it is synthetic/cloned or a vishing scam, or 'SAFE' if normal.",
                {"mime_type": "audio/ogg", "data": audio_data}
            ]
        )
        
        result_text = response.text.upper()
        if "DANGER" in result_text:
            stats["voice_danger_count"] += 1
            if chat_type in ['group', 'supergroup']:
                try:
                    await message.delete()
                    new_rep = update_user_rep(user_id, -20)
                    await message.answer(f"🚨 DIQQAT! [{message.from_user.full_name}](tg://user?id={user_id}) yuborgan ovozli xabarda Vishing / Voice Cloning aniqlandi va xabar o'chirildi! (Reputation: {new_rep})", parse_mode="Markdown")
                except Exception:
                    pass
                return
            else:
                await message.answer(TEXTS[lang]['voice_danger'])
        else:
            if chat_type == 'private':
                await message.answer("✅ Ovozli xabar tahlil qilindi. Sun'iy klonlash yoki vishing alomatlari topilmadi.")
    except Exception:
        if chat_type == 'private':
            await message.answer("❌ Ovozli xabarni tahlil qilishda xatolik yuz berdi.")

# --- STANDARD COMMANDS & MESSAGES ---
@dp.message(Command("start"))
async def cmd_start(message: Message):
    add_user(message.from_user.id, message.from_user.username, message.from_user.full_name)
    await message.answer(TEXTS['uz']['start'])

@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM users")
    users_count = cursor.fetchone()[0]
    conn.close()
    
    text = TEXTS['uz']['stats'].format(
        checked=stats['checked_count'],
        danger=stats['danger_count'],
        audit=stats['audit_count'],
        screenshot=stats['screenshot_count'],
        voice=stats['voice_danger_count'],
        video=stats['video_danger_count'],
        users=users_count
    )
    await message.answer(text, parse_mode="Markdown")

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

@dp.message(F.text)
async def handle_message(message: Message):
    user_id = message.from_user.id
    add_user(user_id, message.from_user.username, message.from_user.full_name)
    
    current_time = time.time()
    if user_id in user_last_message_time:
        if current_time - user_last_message_time[user_id] < SPAM_INTERVAL:
            await message.answer(TEXTS['uz']['spam'])
            return
    user_last_message_time[user_id] = current_time

    url = extract_url(message.text)
    if url:
        stats["checked_count"] += 1
        parsed = urlparse(url if url.startswith(('http://', 'https://')) else 'https://' + url)
        domain = parsed.netloc.lower()
        if domain.startswith('www.'):
            domain = domain[4:]

        if domain.endswith('.gov.uz') or domain in OFFICIAL_DOMAINS:
            await message.answer(f"🔗 **Link:**\n{TEXTS['uz']['safe_link']}")
            return

        await message.answer("📸 Sayt skrinshoti olinib, AI phishing tekshiruvi bajarilmoqda...")
        screenshot_bytes = get_webpage_screenshot(url)
        
        if screenshot_bytes:
            stats["screenshot_count"] += 1
            image = Image.open(BytesIO(screenshot_bytes))
            try:
                response = ai_client.models.generate_content(
                    model='gemini-2.5-flash',
                    contents=[
                        "Analyze this webpage screenshot carefully. Is this a phishing website, fake login page imitating banks, or a fraudulent scam scheme? "
                        "Start response strictly with '🚨 PHISHING/SCAM' if dangerous, or '✅ SAFE' if legitimate.",
                        image
                    ]
                )
                if "PHISHING" in response.text.upper() or "SCAM" in response.text.upper():
                    stats["danger_count"] += 1
                    await message.answer_photo(
                        photo=BufferedInputFile(screenshot_bytes, filename="screenshot.jpg"),
                        caption=f"🚨 **DIQQAT! SOXTA / PHISHING SAYT ANIQLANDI!**\n\n{response.text}",
                        parse_mode="Markdown"
                    )
                else:
                    await message.answer(f"✅ Sayt skrinshoti tekshirildi. Xavfli alomatlar topilmadi.\n\n{response.text}")
            except Exception:
                await message.answer("❌ Skrinshotni tahlil qilishda xatolik yuz berdi.")

async def main():
    threading.Thread(target=run_http_server, daemon=True).start()
    print("Avtonom Detektiv va Voice Cloning modullari qo'shilgan bot ishga tushdi...")
    await bot.delete_webhook(drop_pending_updates=True)
    await set_default_commands(bot)
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
