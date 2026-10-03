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
            domain TEXT PRIMARY KEY
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS activity_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            action TEXT,
            details TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

init_db()

def log_activity(user_id, action, details):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("INSERT INTO activity_logs (user_id, action, details) VALUES (?, ?, ?)", (user_id, action, details))
    conn.commit()
    conn.close()

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

def add_global_blacklist(domain):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO blacklist (domain) VALUES (?)", (domain.lower(),))
    conn.commit()
    conn.close()

def is_globally_blacklisted(domain):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM blacklist WHERE domain = ?", (domain.lower(),))
    row = cursor.fetchone()
    conn.close()
    return row is not None

# --- TARJIMALAR ---
TEXTS = {
    'uz': {
        'start': "👋 Assalomu alaykum!\n\nMen to'liq himoyalangan AI kiber-xavfsizlik botiman. Havolalar, fayllar, ovozli xabarlar, Deepfake videolar, skrinshotlar va Avtonom Kiber-Detektor funksiyalariga egaman.",
        'stats': "📊 **Bot Statistikasi:**\n\n🔍 Tekshirilgan havolalar: {checked}\n🚨 Xavfli havolalar: {danger}\n🕵️‍♂️ Kiber-Auditlar: {audit}\n📸 Skrinshotlar: {screenshot}\n🎙️ Ovozli vishinglar: {voice}\n👥 Foydalanuvchilar: {users}",
        'help': "ℹ️ **Qo'llanma:**\n- `/audit <kanal_oki_havola>` — Avtonom detektiv tekshiruvi\n- Istalgan havola, fayl, video yoki ovozli xabar yuboring.",
        'spam': "⚠️ Juda tez-tez xabar yuboryapsiz! Iltimos, biroz kuting.",
        'safe_link': "✅ Bu rasmiy va ishonchli manzil.",
        'danger_link': "🚨 DIQQAT! XAVFLI / PHISHING HAVOLA ANIQLANDI!",
        'voice_danger': "🚨 DIQQAT! Ovozli xabarda Vishing / Voice Cloning alomatlari aniqlandi!",
        'file_too_large': "⚠️ Fayl hajmi juda katta."
    }
}

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

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
dp = Dispatcher()

async def set_default_commands(bot: Bot):
    commands = [
        BotCommand(command="start", description="🚀 Botni ishga tushirish"),
        BotCommand(command="audit", description="🕵️‍♂️️ Kiber-Detektiv audit"),
        BotCommand(command="stats", description="📊 Bot statistikasi"),
        BotCommand(command="top", description="🏆 Reyting"),
        BotCommand(command="web", description="🌐 Admin veb-paneli"),
        BotCommand(command="help", description="ℹ️ Qo'llanma")
    ]
    await bot.set_my_commands(commands)

# --- KENGAYTirilgan KENG ADMIN VEB-PANEL ---
class WebPanelHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed_path = urlparse(self.path)
        if parsed_path.path == "/" or parsed_path.path == "/health":
            self.send_response(200)
            self.send_header("Content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Bot and Enhanced Web Panel are running safely!")
            return
            
        if parsed_path.path == "/admin":
            conn = sqlite3.connect("bot_database.db")
            cursor = conn.cursor()
            cursor.execute("SELECT user_id, username, full_name, reputation FROM users")
            users = cursor.fetchall()
            
            cursor.execute("SELECT domain FROM blacklist")
            blacklisted = cursor.fetchall()
            
            cursor.execute("SELECT user_id, action, details, timestamp FROM activity_logs ORDER BY id DESC LIMIT 20")
            logs = cursor.fetchall()
            conn.close()
            
            users_rows = "".join([f"<tr><td>{u[0]}</td><td>@{u[1]}</td><td>{u[2]}</td><td><b>{u[3]}</b></td></tr>" for u in users])
            blacklist_rows = "".join([f"<li>{b[0]}</li>" for b in blacklisted])
            log_rows = "".join([f"<tr><td>{l[0]}</td><td>{l[1]}</td><td>{l[2]}</td><td>{l[3]}</td></tr>" for l in logs])

            html = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <title>Kiber Bot - Admin Panel</title>
                <meta charset="utf-8">
                <style>
                    body {{ font-family: Arial, sans-serif; background: #f0f2f5; margin: 0; padding: 20px; color: #333; }}
                    h1, h2 {{ color: #1a73e8; }}
                    .container {{ max-width: 1000px; margin: auto; }}
                    .card {{ background: white; padding: 20px; margin-bottom: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
                    .stats-grid {{ display: flex; gap: 15px; flex-wrap: wrap; }}
                    .stat-box {{ background: #e8f0fe; padding: 15px; border-radius: 6px; flex: 1; min-width: 180px; text-align: center; }}
                    table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
                    th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; font-size: 14px; }}
                    th {{ background: #f8f9fa; }}
                    input[type="text"], textarea {{ width: 100%; padding: 8px; margin-top: 5px; margin-bottom: 10px; border: 1px solid #ccc; border-radius: 4px; }}
                    button {{ background: #1a73e8; color: white; border: none; padding: 10px 15px; border-radius: 4px; cursor: pointer; }}
                    button:hover {{ background: #1557b0; }}
                </style>
            </head>
            <body>
                <div class="container">
                    <h1>🛡️ Kiber-Xavfsizlik Boshqaruv Paneli</h1>
                    
                    <div class="card">
                        <h2>📊 Asosiy Statistika</h2>
                        <div class="stats-grid">
                            <div class="stat-box"><h3>{stats['checked_count']}</h3><p>Tekshirilgan Havolalar</p></div>
                            <div class="stat-box"><h3>{stats['danger_count']}</h3><p>Bloklangan Phishing</p></div>
                            <div class="stat-box"><h3>{stats['audit_count']}</h3><p>Kiber-Auditlar</p></div>
                            <div class="stat-box"><h3>{len(users)}</h3><p>Foydalanuvchilar</p></div>
                        </div>
                    </div>

                    <div class="card">
                        <h2>📢 Global Xabar Tarqatish (Broadcast)</h2>
                        <form method="POST" action="/broadcast">
                            <label>Barcha foydalanuvchilarga yuborish uchun xabar matni:</label>
                            <textarea name="message" rows="3" placeholder="E'lon matnini kiriting..."></textarea>
                            <button type="submit">Xabarni yuborish</button>
                        </form>
                    </div>

                    <div class="card">
                        <h2>🚫 Domenlarni Qora Ro'yxatga Qo'shish</h2>
                        <form method="POST" action="/add_blacklist">
                            <label>Domen nomi (masalan: scam-site.xyz):</label>
                            <input type="text" name="domain" placeholder="domen.uz">
                            <button type="submit">Qora ro'yxatga qo'shish</button>
                        </form>
                        <ul>{blacklist_rows}</ul>
                    </div>

                    <div class="card">
                        <h2>⚡ Jonli Faoliyat Jurnali (Logs)</h2>
                        <table>
                            <tr><th>User ID</th><th>Amal (Action)</th><th>Tafsilot</th><th>Vaqt</th></tr>
                            {log_rows}
                        </table>
                    </div>

                    <div class="card">
                        <h2>👥 Foydalanuvchilar Ro'yxati</h2>
                        <table>
                            <tr><th>ID</th><th>Username</th><th>Ism</th><th>Karma</th></tr>
                            {users_rows}
                        </table>
                    </div>
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

    def do_POST(self):
        parsed_path = urlparse(self.path)
        content_length = int(self.headers.get('Content-Length', 0))
        post_data = self.rfile.read(content_length).decode('utf-8')
        params = parse_qs(post_data)

        if parsed_path.path == "/add_blacklist":
            domain = params.get("domain", [""])[0].strip()
            if domain:
                add_global_blacklist(domain)
                log_activity(ADMIN_ID, "BLACKLIST_ADD", f"Domen qo'shildi: {domain}")
            self.send_response(303)
            self.send_header('Location', '/admin')
            self.end_headers()
            return

        if parsed_path.path == "/broadcast":
            broadcast_msg = params.get("message", [""])[0].strip()
            if broadcast_msg:
                log_activity(ADMIN_ID, "BROADCAST", f"Xabar yuborildi: {broadcast_msg[:30]}...")
                # Asinxron xabar tarqatishni fonda ishga tushiramiz
                threading.Thread(target=run_broadcast, args=(broadcast_msg,), daemon=True).start()
            self.send_response(303)
            self.send_header('Location', '/admin')
            self.end_headers()
            return

        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        return

def run_broadcast(text):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users")
    users = cursor.fetchall()
    conn.close()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    async def send_all():
        for u in users:
            try:
                await bot.send_message(u[0], f"📢 **Admin e'loni:**\n\n{text}", parse_mode="Markdown")
                await asyncio.sleep(0.05)
            except Exception:
                pass

    loop.run_until_complete(send_all())

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

# --- HANDLERLAR ---
@dp.message(Command("audit"))
async def cmd_audit(message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("❌ Foydalanish: `/audit <kanal_username yoki havola>`", parse_mode="Markdown")
        return
    target = args[1].strip()
    stats["audit_count"] += 1
    log_activity(message.from_user.id, "AUDIT", target)
    await message.answer(f"🕵️️‍♂️ **Avtonom Kiber-Detektiv Agent** tahlilni boshladi: `{target}`", parse_mode="Markdown")

    try:
        response = ai_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=f"Perform a professional cybersecurity OSINT audit and threat analysis on: '{target}'. Provide report in Uzbek language."
        )
        await message.answer(f"🛡️ **KIBER-AUDIT HISOBOTI**\n\n{response.text}", parse_mode="Markdown")
    except Exception:
        await message.answer("❌ Audit jarayonida xatolik yuz berdi.")

@dp.message(F.voice)
async def handle_voice(message: Message):
    user_id = message.from_user.id
    add_user(user_id, message.from_user.username, message.from_user.full_name)
    file = await bot.get_file(message.voice.file_id)
    file_bytes = await bot.download_file(file.file_path)
    audio_data = file_bytes.read()

    try:
        response = ai_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[
                "Analyze this audio for Voice Cloning or vishing scam. Answer strictly with 'DANGER' or 'SAFE'.",
                {"mime_type": "audio/ogg", "data": audio_data}
            ]
        )
        if "DANGER" in response.text.upper():
            stats["voice_danger_count"] += 1
            log_activity(user_id, "VOICE_DANGER", "Vishing / Voice cloning aniqlandi")
            await message.answer("🚨 DIQQAT! Ovozli xabarda Vishing alomatlari aniqlandi!")
    except Exception:
        pass

@dp.message(Command("start"))
async def cmd_start(message: Message):
    add_user(message.from_user.id, message.from_user.username, message.from_user.full_name)
    log_activity(message.from_user.id, "START", "Botni ishga tushirdi")
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
        users=users_count
    )
    await message.answer(text, parse_mode="Markdown")

@dp.message(Command("top"))
async def cmd_top(message: Message):
    top_users = get_top_users(10)
    text = "🏆 **Eng hushyor foydalanuvchilar reytingi:**\n\n"
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
    
    url = extract_url(message.text)
    if url:
        stats["checked_count"] += 1
        parsed = urlparse(url if url.startswith(('http://', 'https://')) else 'https://' + url)
        domain = parsed.netloc.lower()
        if domain.startswith('www.'):
            domain = domain[4:]

        if is_globally_blacklisted(domain):
            stats["danger_count"] += 1
            log_activity(user_id, "BLACKLIST_HIT", domain)
            await message.answer("🚨 DIQQAT! Ushbu manzil qora ro'yxatga kiritilgan!")
            return

        if domain.endswith('.gov.uz') or domain in OFFICIAL_DOMAINS:
            await message.answer(f"🔗 **Link:**\n{TEXTS['uz']['safe_link']}")
            return

        screenshot_bytes = get_webpage_screenshot(url)
        if screenshot_bytes:
            stats["screenshot_count"] += 1
            image = Image.open(BytesIO(screenshot_bytes))
            try:
                response = ai_client.models.generate_content(
                    model='gemini-2.5-flash',
                    contents=[
                        "Analyze this webpage screenshot. Is this phishing or a scam? Start response with '🚨 PHISHING/SCAM' or '✅ SAFE'.",
                        image
                    ]
                )
                if "PHISHING" in response.text.upper() or "SCAM" in response.text.upper():
                    stats["danger_count"] += 1
                    log_activity(user_id, "PHISHING_DETECTED", domain)
                    await message.answer_photo(
                        photo=BufferedInputFile(screenshot_bytes, filename="screenshot.jpg"),
                        caption=f"🚨 **DIQQAT! PHISHING SAYT ANIQLANDI!**\n\n{response.text}",
                        parse_mode="Markdown"
                    )
                else:
                    await message.answer(f"✅ Sayt skrinshoti tekshirildi.\n\n{response.text}")
            except Exception:
                pass

async def main():
    threading.Thread(target=run_http_server, daemon=True).start()
    print("Kuchaytirilgan admin panel va bot ishga tushdi...")
    await bot.delete_webhook(drop_pending_updates=True)
    await set_default_commands(bot)
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
