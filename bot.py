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
from aiogram.types import Message, BotCommand, BufferedInputFile
from google import genai
from PIL import Image

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("BOT_TOKEN topilmadi!")

ADMIN_ID = 5081583283
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ADMIN_PANEL_SECRET = os.getenv("ADMIN_PANEL_SECRET", "kiber_secret_2026")

ai_client = genai.Client(api_key=GEMINI_API_KEY)

stats = {
    "checked_count": 0,
    "danger_count": 0,
    "file_danger_count": 0,
    "voice_danger_count": 0,
    "video_danger_count": 0,
    "photo_danger_count": 0,
    "screenshot_count": 0,
    "audit_count": 0
}

user_last_message_time = {}
SPAM_INTERVAL = 1.3

# ==================== DATABASE ====================
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
    try:
        conn = sqlite3.connect("bot_database.db")
        cursor = conn.cursor()
        cursor.execute("INSERT INTO activity_logs (user_id, action, details) VALUES (?, ?, ?)",
                       (user_id, action, details))
        conn.commit()
        conn.close()
    except:
        pass

def add_user(user_id, username, full_name):
    safe_username = str(username)[:50] if username else ""
    safe_fullname = str(full_name)[:100] if full_name else "Foydalanuvchi"
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO users (user_id, username, full_name, language, reputation)
        VALUES (?, ?, ?, 'uz', 100)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            full_name=excluded.full_name
    """, (user_id, safe_username, safe_fullname))
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

# ==================== TEXTS ====================
TEXTS = {
    'uz': {
        'start': (
            "👋 Assalomu alaykum!\n\n"
            "Men **AI Kiber-Xavfsizlik Botiman**.\n\n"
            "🔹 Havolalar (phishing)\n"
            "🔹 Fayllar (.apk, .exe ...)\n"
            "🔹 Ovozli xabarlar (vishing)\n"
            "🔹 Rasmlar (QR-kod, soxta hujjat)\n"
            "🔹 Videolar (Deepfake)\n"
            "🔹 `/audit` — chuqur tahlil\n"
            "🔹 `/report` — shubhali narsani yuborish\n\n"
            "Xavfsiz bo‘ling!"
        ),
        'help': (
            "ℹ️ **Qo‘llanma**\n\n"
            "• `/start` — Botni ishga tushirish\n"
            "• `/audit <havola yoki kanal>` — Avtonom kiber-audit\n"
            "• `/report` — Shubhali narsani adminga yuborish\n"
            "• `/help` — Ushbu yordam\n\n"
            "**Avtomatik tekshiradi:**\n"
            "• Havolalar va phishing saytlar\n"
            "• `.apk`, `.exe`, `.bat` va boshqa xavfli fayllar\n"
            "• Ovozli xabarlar (Voice Cloning)\n"
            "• Rasmlar (QR-kod, soxta hujjatlar)\n"
            "• Videolar (Deepfake)\n\n"
            "Shunchaki yuboring — bot o‘zi tekshiradi.\n"
            "Guruhlarda ham ishlaydi."
        ),
        'spam': "⚠️ Juda tez-tez yuboryapsiz. Biroz kuting.",
        'apk_danger': (
            "🚨 **DIQQAT! XAVFLI FAYL ANIQLANDI!**\n\n"
            "Bu `.apk` yoki boshqa potensial zararli fayl.\n"
            "Uni **ochmang** va **o‘rnatmang**!\n\n"
            "Ko‘pincha firibgarlar shu orqali telefonni boshqarib oladi."
        ),
        'voice_danger': "🚨 DIQQAT! Ovozli xabarda **Vishing / Voice Cloning** alomatlari aniqlandi!",
        'photo_danger': "🚨 DIQQAT! Rasmda **firibgarlik / QR-kod / soxta hujjat** alomatlari aniqlandi!",
        'video_danger': "🚨 DIQQAT! Videoda **Deepfake yoki soxta video** alomatlari aniqlandi!",
    }
}

OFFICIAL_DOMAINS = {
    'gov.uz', 'my.gov.uz', 'pm.gov.uz', 'lex.uz', 'cbu.uz', 'stat.uz', 'customs.uz',
    'soliq.uz', 'my.soliq.uz', 'uzgidromet.uz', 'mehnat.uz', 'my.mehnat.uz',
    'iiv.uz', 'mfa.uz', 'minjust.uz', 'uzedu.uz', 'ssv.uz',
    'nbu.uz', 'agrobank.uz', 'kapitalbank.uz', 'ipotekabank.uz', 'davrbank.uz',
    'hamkorbank.uz', 'asakabank.uz', 'anorbank.uz', 'tbcbank.uz', 'octobank.uz',
    'uzcard.uz', 'humocard.uz', 'click.uz', 'payme.uz', 'uzum.uz', 'uzumbank.uz',
    'kun.uz', 'gazeta.uz', 'daryo.uz', 'xabar.uz', 'uza.uz',
    'texnomart.uz', 'asaxiy.uz', 'olcha.uz', 'express24.uz',
    'beeline.uz', 'ucell.uz', 'mobi.uz', 'uztelecom.uz', 'uzmobile.uz'
}

DANGEROUS_EXTENSIONS = {
    '.apk', '.exe', '.bat', '.cmd', '.scr', '.js', '.vbs', '.wsf',
    '.msi', '.dmg', '.jar', '.com', '.pif', '.hta', '.cpl'
}

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
dp = Dispatcher()

async def set_default_commands(bot: Bot):
    commands = [
        BotCommand(command="start", description="🚀 Botni ishga tushirish"),
        BotCommand(command="audit", description="🕵️‍♂️ Kiber-Detektiv audit"),
        BotCommand(command="report", description="📢 Shubhali narsani yuborish"),
        BotCommand(command="help", description="ℹ️ Qo‘llanma"),
    ]
    await bot.set_my_commands(commands)

# ==================== ADMIN PANEL ====================
class WebPanelHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        key = query.get("key", [None])[0]

        if parsed.path in ("/", "/health"):
            self.send_response(200)
            self.send_header("Content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Bot is running")
            return

        if parsed.path == "/admin":
            if key != ADMIN_PANEL_SECRET:
                self.send_response(403)
                self.end_headers()
                self.wfile.write(b"403 Forbidden")
                return

            conn = sqlite3.connect("bot_database.db")
            c = conn.cursor()
            c.execute("SELECT user_id, username, full_name, reputation FROM users ORDER BY reputation DESC")
            users = c.fetchall()
            c.execute("SELECT domain FROM blacklist")
            blacklisted = c.fetchall()
            c.execute("SELECT user_id, action, details, timestamp FROM activity_logs ORDER BY id DESC LIMIT 40")
            logs = c.fetchall()
            conn.close()

            users_rows = "".join(
                f"<tr><td>{u[0]}</td><td>@{u[1] or '-'}</td><td>{u[2]}</td><td><b>{u[3]}</b></td></tr>"
                for u in users
            )
            blacklist_rows = "".join(f"<li>{b[0]}</li>" for b in blacklisted) or "<li>Bo‘sh</li>"
            log_rows = "".join(
                f"<tr><td>{l[0]}</td><td>{l[1]}</td><td>{l[2][:70]}</td><td>{l[3]}</td></tr>"
                for l in logs
            )

            html = f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kiber Bot Admin</title>
<style>
body{{font-family:system-ui;background:#0f172a;color:#e2e8f0;margin:0;padding:20px}}
.container{{max-width:1100px;margin:auto}}
.card{{background:#1e293b;padding:20px;margin-bottom:18px;border-radius:12px}}
.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px}}
.stat{{background:#0f172a;padding:14px;border-radius:10px;text-align:center;border:1px solid #334155}}
.stat h3{{margin:0;font-size:26px;color:#38bdf8}}
.stat p{{margin:4px 0 0;font-size:12px;color:#94a3b8}}
table{{width:100%;border-collapse:collapse;font-size:13px;margin-top:10px}}
th,td{{border:1px solid #334155;padding:8px;text-align:left}}
th{{background:#0f172a;color:#94a3b8}}
input,textarea{{width:100%;padding:10px;margin:6px 0 12px;border-radius:8px;border:1px solid #334155;background:#0f172a;color:#e2e8f0}}
button{{background:#0ea5e9;color:#fff;border:none;padding:10px 16px;border-radius:8px;font-weight:600;cursor:pointer}}
</style></head><body>
<div class="container">
<h1>🛡️ Kiber-Xavfsizlik Admin Panel</h1>
<p style="color:#94a3b8">Maxfiy kalit himoyasi faol</p>

<div class="card"><h2>📊 Statistika</h2>
<div class="stats">
<div class="stat"><h3>{stats['checked_count']}</h3><p>Havolalar</p></div>
<div class="stat"><h3>{stats['danger_count']}</h3><p>Phishing</p></div>
<div class="stat"><h3>{stats['file_danger_count']}</h3><p>Xavfli fayl</p></div>
<div class="stat"><h3>{stats['voice_danger_count']}</h3><p>Vishing</p></div>
<div class="stat"><h3>{stats['photo_danger_count']}</h3><p>Rasm xavf</p></div>
<div class="stat"><h3>{stats['video_danger_count']}</h3><p>Video xavf</p></div>
<div class="stat"><h3>{stats['audit_count']}</h3><p>Audit</p></div>
<div class="stat"><h3>{len(users)}</h3><p>Foydalanuvchi</p></div>
</div></div>

<div class="card"><h2>📢 Broadcast</h2>
<form method="POST" action="/broadcast?key={ADMIN_PANEL_SECRET}">
<textarea name="message" rows="3" placeholder="E'lon matni..."></textarea>
<button type="submit">Yuborish</button></form></div>

<div class="card"><h2>🚫 Qora ro‘yxat</h2>
<form method="POST" action="/add_blacklist?key={ADMIN_PANEL_SECRET}">
<input type="text" name="domain" placeholder="domen.uz">
<button type="submit">Qo‘shish</button></form>
<ul>{blacklist_rows}</ul></div>

<div class="card"><h2>⚡ So‘nggi faoliyat</h2>
<table><tr><th>User</th><th>Amal</th><th>Tafsilot</th><th>Vaqt</th></tr>{log_rows}</table></div>

<div class="card"><h2>👥 Foydalanuvchilar</h2>
<table><tr><th>ID</th><th>Username</th><th>Ism</th><th>Karma</th></tr>{users_rows}</table></div>
</div></body></html>"""

            self.send_response(200)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if query.get("key", [None])[0] != ADMIN_PANEL_SECRET:
            self.send_response(403)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", 0))
        data = self.rfile.read(length).decode()
        params = parse_qs(data)

        if parsed.path == "/add_blacklist":
            domain = params.get("domain", [""])[0].strip()
            if domain:
                add_global_blacklist(domain)
                log_activity(ADMIN_ID, "BLACKLIST_ADD", domain)
            self.send_response(303)
            self.send_header("Location", f"/admin?key={ADMIN_PANEL_SECRET}")
            self.end_headers()
            return

        if parsed.path == "/broadcast":
            msg = params.get("message", [""])[0].strip()
            if msg:
                log_activity(ADMIN_ID, "BROADCAST", msg[:40])
                threading.Thread(target=run_broadcast, args=(msg,), daemon=True).start()
            self.send_response(303)
            self.send_header("Location", f"/admin?key={ADMIN_PANEL_SECRET}")
            self.end_headers()
            return

        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        return

def run_broadcast(text):
    conn = sqlite3.connect("bot_database.db")
    c = conn.cursor()
    c.execute("SELECT user_id FROM users")
    users = c.fetchall()
    conn.close()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def send_all():
        for u in users:
            try:
                await bot.send_message(u[0], f"📢 **Admin e’loni:**\n\n{text}", parse_mode="Markdown")
                await asyncio.sleep(0.05)
            except:
                pass
    loop.run_until_complete(send_all())

def run_http_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), WebPanelHandler).serve_forever()

# ==================== HELPERS ====================
def extract_url(text: str):
    if not text:
        return None
    for word in text.split():
        clean = word.strip(".,;:!?()[]{}\"'")
        if any(x in clean.lower() for x in ("t.me/", "telegram.me/", "http://", "https://", "www.")):
            return clean
        if clean.startswith("@") and len(clean) > 1:
            return f"t.me/{clean[1:]}"
    return None

def get_webpage_screenshot(url: str) -> bytes:
    try:
        full = url if url.startswith(("http://", "https://")) else "https://" + url
        r = requests.get(f"https://api.microlink.io/?url={full}&screenshot=true&meta=false&embed=screenshot.url", timeout=8)
        data = r.json()
        if data.get("status") == "success":
            return requests.get(data["data"]["screenshot"]["url"], timeout=8).content
    except:
        pass
    return None

async def notify_admin(text: str):
    try:
        await bot.send_message(ADMIN_ID, text, parse_mode="Markdown")
    except:
        pass

# ==================== HANDLERS ====================
@dp.message(Command("start"))
async def cmd_start(message: Message):
    add_user(message.from_user.id, message.from_user.username, message.from_user.full_name)
    log_activity(message.from_user.id, "START", "Bot ishga tushirildi")
    await message.answer(TEXTS['uz']['start'], parse_mode="Markdown")

@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(TEXTS['uz']['help'], parse_mode="Markdown")

@dp.message(Command("audit"))
async def cmd_audit(message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("❌ Foydalanish: `/audit <havola yoki kanal>`", parse_mode="Markdown")
        return
    target = args[1].strip()
    stats["audit_count"] += 1
    log_activity(message.from_user.id, "AUDIT", target)
    await message.answer(f"🕵️‍♂️ **Kiber-Detektiv** ishga tushdi: `{target}`", parse_mode="Markdown")
    try:
        resp = ai_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=f"Professional cybersecurity OSINT audit of: '{target}'. Write full detailed report in Uzbek."
        )
        await message.answer(f"🛡️ **KIBER-AUDIT HISOBOTI**\n\n{resp.text}", parse_mode="Markdown")
    except:
        await message.answer("❌ Audit xatosi yuz berdi.")

@dp.message(Command("report"))
async def cmd_report(message: Message):
    await message.answer(
        "📢 **Shubhali narsani yuboring**\n\n"
        "Havola, rasm, video, fayl yoki ovozli xabarni shu yerga yuboring.\n"
        "Men uni adminga yetkazaman va tahlil qilaman.",
        parse_mode="Markdown"
    )

@dp.message(Command("web"))
async def cmd_web(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    base = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:10000")
    url = f"{base}/admin?key={ADMIN_PANEL_SECRET}"
    await message.answer(f"🔐 **Admin panel:**\n\n`{url}`", parse_mode="Markdown", disable_web_page_preview=True)

# ---------- VOICE ----------
@dp.message(F.voice)
async def handle_voice(message: Message):
    user = message.from_user
    add_user(user.id, user.username, user.full_name)

    file = await bot.get_file(message.voice.file_id)
    data = (await bot.download_file(file.file_path)).read()

    try:
        resp = ai_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[
                "Analyze this audio for Voice Cloning or vishing scam. Reply with only one word: DANGER or SAFE.",
                {"mime_type": "audio/ogg", "data": data}
            ]
        )
        if "DANGER" in resp.text.upper():
            stats["voice_danger_count"] += 1
            update_user_rep(user.id, -15)
            log_activity(user.id, "VOICE_DANGER", "Vishing aniqlandi")
            await message.reply(TEXTS['uz']['voice_danger'])
            await notify_admin(
                f"🚨 **Vishing aniqlandi**\n"
                f"User: `{user.id}` (@{user.username or '-'})\n"
                f"Chat: `{message.chat.id}`"
            )
        else:
            update_user_rep(user.id, +2)
            await message.reply("✅ Ovozli xabar xavfsiz ko‘rinadi.")
    except:
        await message.reply("⚠️ Ovozni tahlil qilib bo‘lmadi.")

# ---------- DOCUMENT (APK va boshqalar) ----------
@dp.message(F.document)
async def handle_document(message: Message):
    user = message.from_user
    add_user(user.id, user.username, user.full_name)
    doc = message.document
    name = (doc.file_name or "").lower()
    size = doc.file_size or 0

    if size > 25 * 1024 * 1024:
        await message.reply("⚠️ Fayl juda katta (maks. 25 MB).")
        return

    if any(name.endswith(ext) for ext in DANGEROUS_EXTENSIONS):
        stats["file_danger_count"] += 1
        update_user_rep(user.id, -20)
        log_activity(user.id, "FILE_DANGER", name)
        await message.reply(TEXTS['uz']['apk_danger'], parse_mode="Markdown")
        await notify_admin(
            f"🚨 **Xavfli fayl**\n"
            f"Fayl: `{name}`\n"
            f"User: `{user.id}` (@{user.username or '-'})\n"
            f"Chat: `{message.chat.id}`"
        )
        return

    await message.reply(
        f"📄 Fayl qabul qilindi: `{doc.file_name}`\n"
        f"Hajmi: {round(size/1024,1)} KB\n\n"
        f"⚠️ Noma’lum manbadan kelgan fayllarni ochmang.",
        parse_mode="Markdown"
    )

# ---------- PHOTO ----------
@dp.message(F.photo)
async def handle_photo(message: Message):
    user = message.from_user
    add_user(user.id, user.username, user.full_name)

    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    data = (await bot.download_file(file.file_path)).read()

    try:
        img = Image.open(BytesIO(data))
        resp = ai_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[
                "Analyze this image carefully for: phishing, QR-code scam, fake documents, fake bank cards, or any social engineering. "
                "Reply starting with exactly '🚨 DANGER' or '✅ SAFE', then short explanation in Uzbek.",
                img
            ]
        )
        text = resp.text
        if "DANGER" in text.upper():
            stats["photo_danger_count"] += 1
            update_user_rep(user.id, -12)
            log_activity(user.id, "PHOTO_DANGER", "Rasmda firibgarlik")
            await message.reply(f"{TEXTS['uz']['photo_danger']}\n\n{text}")
            await notify_admin(
                f"🚨 **Xavfli rasm**\n"
                f"User: `{user.id}` (@{user.username or '-'})\n"
                f"Chat: `{message.chat.id}`\n\n{text[:300]}"
            )
        else:
            update_user_rep(user.id, +2)
            await message.reply(f"✅ Rasm tekshirildi.\n\n{text}")
    except:
        await message.reply("⚠️ Rasmni tahlil qilib bo‘lmadi.")

# ---------- VIDEO ----------
@dp.message(F.video)
async def handle_video(message: Message):
    user = message.from_user
    add_user(user.id, user.username, user.full_name)

    if message.video.file_size and message.video.file_size > 20 * 1024 * 1024:
        await message.reply("⚠️ Video juda katta. Qisqaroq video yuboring.")
        return

    file = await bot.get_file(message.video.file_id)
    data = (await bot.download_file(file.file_path)).read()

    try:
        resp = ai_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[
                "Analyze this video for Deepfake, fake person, or social engineering scam. "
                "Reply with only one word first: DANGER or SAFE, then short explanation in Uzbek.",
                {"mime_type": "video/mp4", "data": data}
            ]
        )
        text = resp.text
        if "DANGER" in text.upper():
            stats["video_danger_count"] += 1
            update_user_rep(user.id, -15)
            log_activity(user.id, "VIDEO_DANGER", "Deepfake aniqlandi")
            await message.reply(f"{TEXTS['uz']['video_danger']}\n\n{text}")
            await notify_admin(
                f"🚨 **Xavfli video (Deepfake?)**\n"
                f"User: `{user.id}` (@{user.username or '-'})\n"
                f"Chat: `{message.chat.id}`"
            )
        else:
            update_user_rep(user.id, +2)
            await message.reply(f"✅ Video tekshirildi.\n\n{text}")
    except:
        await message.reply("⚠️ Videoni tahlil qilib bo‘lmadi.")

# ---------- TEXT (havolalar) ----------
@dp.message(F.text)
async def handle_text(message: Message):
    user = message.from_user
    add_user(user.id, user.username, user.full_name)

    # Spam himoya
    now = time.time()
    if now - user_last_message_time.get(user.id, 0) < SPAM_INTERVAL:
        await message.reply(TEXTS['uz']['spam'])
        return
    user_last_message_time[user.id] = now

    url = extract_url(message.text)
    if not url:
        return

    stats["checked_count"] += 1
    parsed = urlparse(url if url.startswith(("http://", "https://")) else "https://" + url)
    domain = parsed.netloc.lower().removeprefix("www.")

    if is_globally_blacklisted(domain):
        stats["danger_count"] += 1
        update_user_rep(user.id, -10)
        log_activity(user.id, "BLACKLIST_HIT", domain)
        await message.reply("🚨 Bu manzil **qora ro‘yxatda**!")
        await notify_admin(f"🚨 Qora ro‘yxat hit: `{domain}` | User `{user.id}`")
        return

    if domain.endswith(".gov.uz") or domain in OFFICIAL_DOMAINS:
        update_user_rep(user.id, +1)
        await message.reply("✅ Bu rasmiy va ishonchli manzil.")
        return

    # Skrinshot + AI
    shot = get_webpage_screenshot(url)
    if shot:
        stats["screenshot_count"] += 1
        try:
            img = Image.open(BytesIO(shot))
            resp = ai_client.models.generate_content(
                model="gemini-2.5-flash",
                contents=[
                    "Analyze this webpage screenshot. Is it phishing/scam/fake login? "
                    "Start with '🚨 PHISHING/SCAM' or '✅ SAFE', then short Uzbek explanation.",
                    img
                ]
            )
            text = resp.text
            if "PHISHING" in text.upper() or "SCAM" in text.upper():
                stats["danger_count"] += 1
                update_user_rep(user.id, -15)
                log_activity(user.id, "PHISHING", domain)
                await message.reply_photo(
                    photo=BufferedInputFile(shot, "shot.jpg"),
                    caption=f"🚨 **PHISHING / SCAM ANIQLANDI!**\n\n{text}",
                    parse_mode="Markdown"
                )
                await notify_admin(
                    f"🚨 **Phishing aniqlandi**\n"
                    f"Domain: `{domain}`\n"
                    f"User: `{user.id}` (@{user.username or '-'})"
                )
            else:
                update_user_rep(user.id, +3)
                await message.reply_photo(
                    photo=BufferedInputFile(shot, "shot.jpg"),
                    caption=f"✅ Sayt tekshirildi.\n\n{text}",
                    parse_mode="Markdown"
                )
        except:
            await message.reply("⚠️ Skrinshot tahlilida xato. Havolani ehtiyot bilan oching.")
    else:
        await message.reply(f"🔗 Havola qabul qilindi.\nSkrinshot olinmadi — ochishdan oldin ehtiyot bo‘ling.")

# ==================== MAIN ====================
async def main():
    threading.Thread(target=run_http_server, daemon=True).start()
    print("✅ Bot + himoyalangan admin panel ishga tushdi")
    await bot.delete_webhook(drop_pending_updates=True)
    await set_default_commands(bot)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
