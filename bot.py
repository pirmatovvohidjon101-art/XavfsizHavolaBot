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

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, BotCommand, BotCommandScopeChat, BufferedInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
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
ADMIN_PANEL_SECRET = os.getenv("ADMIN_PANEL_SECRET", "kiber_secret_2026")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")

ai_client = genai.Client(api_key=GEMINI_API_KEY)
MODEL_NAME = "gemini-2.0-flash"

stats = {
    "checked_count": 0,
    "danger_count": 0,
    "file_danger_count": 0,
    "voice_danger_count": 0,
    "video_danger_count": 0,
    "photo_danger_count": 0,
    "screenshot_count": 0,
    "audit_count": 0,
}

user_last_message_time = {}
SPAM_INTERVAL = 1.3

# ==================== DATABASE ====================
def init_db():
    conn = sqlite3.connect("bot_database.db")
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            language TEXT DEFAULT 'uz',
            reputation INTEGER DEFAULT 100
        )
    """)
    c.execute("CREATE TABLE IF NOT EXISTS blacklist (domain TEXT PRIMARY KEY)")
    c.execute("""
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
        c = conn.cursor()
        c.execute(
            "INSERT INTO activity_logs (user_id, action, details) VALUES (?, ?, ?)",
            (user_id, action, str(details)[:200])
        )
        conn.commit()
        conn.close()
    except Exception:
        pass

def add_user(user_id, username, full_name):
    conn = sqlite3.connect("bot_database.db")
    c = conn.cursor()
    c.execute("""
        INSERT INTO users (user_id, username, full_name)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username = excluded.username,
            full_name = excluded.full_name
    """, (user_id, str(username or "")[:50], str(full_name or "User")[:100]))
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

# ==================== TEXTS ====================
TEXTS = {
    "start": (
        "👋 Assalomu alaykum!\n\n"
        "Men **AI Kiber-Xavfsizlik Botiman**.\n\n"
        "🔹 Havolalar (phishing)\n"
        "🔹 Fayllar (.apk, .exe ...)\n"
        "🔹 Ovozli xabarlar (vishing)\n"
        "🔹 Rasmlar + QR-kod\n"
        "🔹 Videolar (Deepfake)\n"
        "🔹 `/audit` — chuqur tahlil\n"
        "🔹 `/report` — shubhali narsani yuborish\n\n"
        "Xavfsiz bo‘ling!"
    ),
    "help": (
        "ℹ️ **Qo‘llanma**\n\n"
        "• `/start` — Botni ishga tushirish\n"
        "• `/audit <havola>` — Kiber-audit\n"
        "• `/report` — Shubhali narsani yuborish\n"
        "• `/help` — Yordam\n\n"
        "**Avtomatik tekshiradi:**\n"
        "• Havolalar va phishing\n"
        "• `.apk` / `.exe` va xavfli fayllar\n"
        "• Ovoz (Voice Cloning)\n"
        "• Rasm + QR-kod\n"
        "• Video (Deepfake)\n\n"
        "Guruhlarda ham ishlaydi."
    ),
    "spam": "⚠️ Juda tez-tez yuboryapsiz. Biroz kuting.",
    "apk_danger": "🚨 **XAVFLI FAYL!**\n\nBu `.apk` yoki zararli fayl. **Ochmang va o‘rnatmang!**",
    "voice_danger": "🚨 **Vishing / Voice Cloning** aniqlandi!",
    "photo_danger": "🚨 Rasmda **firibgarlik / xavfli QR** alomatlari bor!",
    "video_danger": "🚨 Videoda **Deepfake yoki soxta video** alomatlari bor!",
}

OFFICIAL_DOMAINS = {
    "gov.uz", "my.gov.uz", "lex.uz", "cbu.uz", "soliq.uz", "my.soliq.uz",
    "nbu.uz", "agrobank.uz", "kapitalbank.uz", "hamkorbank.uz", "tbcbank.uz",
    "uzcard.uz", "humocard.uz", "click.uz", "payme.uz", "uzum.uz", "uzumbank.uz",
    "kun.uz", "daryo.uz", "gazeta.uz", "beeline.uz", "ucell.uz", "uztelecom.uz"
}

DANGEROUS_EXTENSIONS = {
    ".apk", ".exe", ".bat", ".cmd", ".scr", ".js", ".vbs",
    ".msi", ".jar", ".com", ".pif", ".hta"
}

# ==================== BOT ====================
logging.basicConfig(level=logging.INFO)
storage = MemoryStorage()
bot = Bot(token=TOKEN)
dp = Dispatcher(storage=storage)

class AdminAuth(StatesGroup):
    waiting_password = State()

async def set_commands():
    default_cmds = [
        BotCommand(command="start", description="🚀 Botni ishga tushirish"),
        BotCommand(command="audit", description="🕵️‍♂️ Kiber-audit"),
        BotCommand(command="report", description="📢 Shubhali narsani yuborish"),
        BotCommand(command="help", description="ℹ️ Qo‘llanma"),
    ]
    await bot.set_my_commands(default_cmds)

    # Faqat admin uchun
    admin_cmds = default_cmds + [
        BotCommand(command="panel", description="🔐 Admin Panel"),
    ]
    await bot.set_my_commands(admin_cmds, scope=BotCommandScopeChat(chat_id=ADMIN_ID))

# ==================== HELPERS ====================
def extract_url(text: str):
    if not text:
        return None
    for word in text.split():
        clean = word.strip(".,;:!?()[]{}\"'")
        if any(x in clean.lower() for x in ("t.me/", "http://", "https://", "www.")):
            return clean
        if clean.startswith("@") and len(clean) > 1:
            return f"t.me/{clean[1:]}"
    return None

def get_webpage_screenshot(url: str) -> bytes | None:
    try:
        full = url if url.startswith(("http://", "https://")) else "https://" + url
        r = requests.get(
            f"https://api.microlink.io/?url={full}&screenshot=true&meta=false&embed=screenshot.url",
            timeout=9
        )
        data = r.json()
        if data.get("status") == "success":
            return requests.get(data["data"]["screenshot"]["url"], timeout=9).content
    except Exception:
        pass
    return None

def detect_qr(image_bytes: bytes) -> str | None:
    try:
        nparr = np.frombuffer(image_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if img is None:
            return None
        detector = cv2.QRCodeDetector()
        data, _, _ = detector.detectAndDecode(img)
        return data if data else None
    except Exception as e:
        logging.error(f"QR error: {e}")
        return None

async def notify_admin(text: str):
    try:
        await bot.send_message(ADMIN_ID, text, parse_mode="Markdown")
    except Exception:
        pass

async def analyze_with_gemini(prompt: str, data: bytes, mime: str) -> str:
    try:
        response = ai_client.models.generate_content(
            model=MODEL_NAME,
            contents=[
                prompt,
                types.Part.from_bytes(data=data, mime_type=mime)
            ]
        )
        return response.text or ""
    except Exception as e:
        logging.error(f"Gemini error: {e}")
        return f"ERROR: {str(e)[:150]}"

# ==================== HANDLERS ====================
@dp.message(Command("start"))
async def cmd_start(message: Message):
    add_user(message.from_user.id, message.from_user.username, message.from_user.full_name)
    log_activity(message.from_user.id, "START", "start")
    await message.answer(TEXTS["start"], parse_mode="Markdown")

@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(TEXTS["help"], parse_mode="Markdown")

@dp.message(Command("report"))
async def cmd_report(message: Message):
    await message.answer(
        "📢 **Shubhali narsani yuboring**\n\n"
        "Havola, rasm, video, fayl yoki ovozli xabarni shu yerga yuboring.",
        parse_mode="Markdown"
    )

@dp.message(Command("audit"))
async def cmd_audit(message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("❌ `/audit <havola yoki kanal>`")
        return
    target = args[1].strip()
    stats["audit_count"] += 1
    log_activity(message.from_user.id, "AUDIT", target)
    await message.answer(f"🕵️‍♂️ **Kiber-Detektiv** ishga tushdi...\n`{target}`", parse_mode="Markdown")
    try:
        resp = ai_client.models.generate_content(
            model=MODEL_NAME,
            contents=f"Professional cybersecurity OSINT audit of '{target}'. Full detailed report in Uzbek language."
        )
        await message.answer(f"🛡️ **AUDIT HISOBOTI**\n\n{resp.text}", parse_mode="Markdown")
    except Exception as e:
        await message.answer(f"❌ Audit xatosi: {str(e)[:100]}")

@dp.message(Command("panel"))
async def cmd_panel(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("🔐 **Admin Panel**\n\nParolni kiriting:")
    await state.set_state(AdminAuth.waiting_password)

@dp.message(AdminAuth.waiting_password)
async def process_admin_password(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await state.clear()
        return
    if message.text and message.text.strip() == ADMIN_PASSWORD:
        base = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:10000")
        url = f"{base}/admin?key={ADMIN_PANEL_SECRET}"
        await message.answer(
            f"✅ Kirish muvaffaqiyatli!\n\n"
            f"🔐 **Admin Panel:**\n`{url}`\n\n"
            f"[Ochish]({url})",
            parse_mode="Markdown",
            disable_web_page_preview=True
        )
    else:
        await message.answer("❌ Noto‘g‘ri parol.")
    await state.clear()

# ---------- PHOTO ----------
@dp.message(F.photo)
async def handle_photo(message: Message):
    user = message.from_user
    add_user(user.id, user.username, user.full_name)

    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    data = (await bot.download_file(file.file_path)).read()

    # 1. Mahalliy QR
    qr_data = detect_qr(data)
    if qr_data:
        lower = qr_data.lower()
        is_danger = any(x in lower for x in ("http", "https", "t.me", "wifi", "wifi:", "begin:wifi"))
        msg = f"📷 **QR-kod aniqlandi!**\n\n`{qr_data[:300]}`\n\n"
        if is_danger:
            stats["photo_danger_count"] += 1
            update_user_rep(user.id, -10)
            msg += "⚠️ **Ehtiyot bo‘ling!** Bu QR ichida havola yoki Wi-Fi ma’lumoti bor."
            await notify_admin(f"🚨 QR aniqlandi\nUser: `{user.id}`\nData: `{qr_data[:150]}`")
        else:
            msg += "✅ Oddiy QR ko‘rinadi."
        await message.reply(msg, parse_mode="Markdown")

    # 2. Gemini tahlili
    prompt = (
        "Bu rasmni kiber-xavfsizlik nuqtai nazaridan tahlil qil. "
        "QR-kod, soxta hujjat, firibgarlik, phishing belgilari bormi? "
        "Javobni '🚨 XAVFLI' yoki '✅ XAVFSIZ' bilan boshla, keyin qisqa o‘zbekcha tushuntirish yoz."
    )
    result = await analyze_with_gemini(prompt, data, "image/jpeg")

    if result.startswith("ERROR"):
        await message.reply(f"⚠️ Rasmni tahlil qilib bo‘lmadi.\n`{result}`")
        return

    if any(w in result.upper() for w in ("XAVFLI", "DANGER", "PHISHING", "SCAM")):
        stats["photo_danger_count"] += 1
        update_user_rep(user.id, -12)
        log_activity(user.id, "PHOTO_DANGER", "Rasm xavfli")
        await message.reply(f"{TEXTS['photo_danger']}\n\n{result}")
        await notify_admin(f"🚨 Xavfli rasm\nUser: `{user.id}` (@{user.username or '-'})\n{result[:250]}")
    else:
        update_user_rep(user.id, +2)
        await message.reply(f"✅ Rasm tekshirildi.\n\n{result}")

# ---------- VOICE ----------
@dp.message(F.voice)
async def handle_voice(message: Message):
    user = message.from_user
    add_user(user.id, user.username, user.full_name)

    file = await bot.get_file(message.voice.file_id)
    data = (await bot.download_file(file.file_path)).read()

    prompt = "Analyze this audio for Voice Cloning or vishing. Reply first word: DANGER or SAFE, then short Uzbek explanation."
    result = await analyze_with_gemini(prompt, data, "audio/ogg")

    if result.startswith("ERROR"):
        await message.reply(f"⚠️ Ovozni tahlil qilib bo‘lmadi.\n`{result}`")
        return

    if "DANGER" in result.upper():
        stats["voice_danger_count"] += 1
        update_user_rep(user.id, -15)
        log_activity(user.id, "VOICE_DANGER", "Vishing")
        await message.reply(f"{TEXTS['voice_danger']}\n\n{result}")
        await notify_admin(f"🚨 Vishing\nUser: `{user.id}`")
    else:
        update_user_rep(user.id, +2)
        await message.reply(f"✅ Ovoz xavfsiz.\n\n{result}")

# ---------- VIDEO ----------
@dp.message(F.video)
async def handle_video(message: Message):
    user = message.from_user
    add_user(user.id, user.username, user.full_name)

    if message.video.file_size and message.video.file_size > 18 * 1024 * 1024:
        await message.reply("⚠️ Video juda katta (maks ~18 MB).")
        return

    file = await bot.get_file(message.video.file_id)
    data = (await bot.download_file(file.file_path)).read()

    prompt = "Analyze this video for Deepfake or scam. Reply first word: DANGER or SAFE, then short Uzbek explanation."
    result = await analyze_with_gemini(prompt, data, "video/mp4")

    if result.startswith("ERROR"):
        await message.reply(f"⚠️ Videoni tahlil qilib bo‘lmadi.\n`{result}`")
        return

    if "DANGER" in result.upper():
        stats["video_danger_count"] += 1
        update_user_rep(user.id, -15)
        log_activity(user.id, "VIDEO_DANGER", "Deepfake")
        await message.reply(f"{TEXTS['video_danger']}\n\n{result}")
        await notify_admin(f"🚨 Xavfli video\nUser: `{user.id}`")
    else:
        update_user_rep(user.id, +2)
        await message.reply(f"✅ Video tekshirildi.\n\n{result}")

# ---------- DOCUMENT ----------
@dp.message(F.document)
async def handle_document(message: Message):
    user = message.from_user
    add_user(user.id, user.username, user.full_name)
    doc = message.document
    name = (doc.file_name or "").lower()
    size = doc.file_size or 0

    if size > 25 * 1024 * 1024:
        await message.reply("⚠️ Fayl juda katta.")
        return

    if any(name.endswith(ext) for ext in DANGEROUS_EXTENSIONS):
        stats["file_danger_count"] += 1
        update_user_rep(user.id, -20)
        log_activity(user.id, "FILE_DANGER", name)
        await message.reply(TEXTS["apk_danger"], parse_mode="Markdown")
        await notify_admin(f"🚨 Xavfli fayl: `{name}`\nUser: `{user.id}`")
        return

    await message.reply(
        f"📄 Fayl: `{doc.file_name}`\n⚠️ Noma’lum manbadan ochmang.",
        parse_mode="Markdown"
    )

# ---------- TEXT ----------
@dp.message(F.text)
async def handle_text(message: Message):
    user = message.from_user
    add_user(user.id, user.username, user.full_name)

    now = time.time()
    if now - user_last_message_time.get(user.id, 0) < SPAM_INTERVAL:
        await message.reply(TEXTS["spam"])
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
        await message.reply("🚨 Bu manzil **qora ro‘yxatda**!")
        await notify_admin(f"🚨 Blacklist: `{domain}` | User `{user.id}`")
        return

    if domain.endswith(".gov.uz") or domain in OFFICIAL_DOMAINS:
        update_user_rep(user.id, +1)
        await message.reply("✅ Rasmiy va ishonchli manzil.")
        return

    shot = get_webpage_screenshot(url)
    if shot:
        stats["screenshot_count"] += 1
        prompt = (
            "Bu sayt skrinshotini tahlil qil. Phishing yoki scam bormi? "
            "Javobni '🚨 PHISHING' yoki '✅ XAVFSIZ' bilan boshla, keyin qisqa o‘zbekcha yoz."
        )
        result = await analyze_with_gemini(prompt, shot, "image/jpeg")

        if any(w in result.upper() for w in ("PHISHING", "SCAM", "XAVFLI", "DANGER")):
            stats["danger_count"] += 1
            update_user_rep(user.id, -15)
            log_activity(user.id, "PHISHING", domain)
            await message.reply_photo(
                photo=BufferedInputFile(shot, "shot.jpg"),
                caption=f"🚨 **PHISHING ANIQLANDI!**\n\n{result}",
                parse_mode="Markdown"
            )
            await notify_admin(f"🚨 Phishing: `{domain}` | User `{user.id}`")
        else:
            update_user_rep(user.id, +3)
            await message.reply_photo(
                photo=BufferedInputFile(shot, "shot.jpg"),
                caption=f"✅ Sayt tekshirildi.\n\n{result}",
                parse_mode="Markdown"
            )
    else:
        await message.reply("🔗 Havola qabul qilindi. Skrinshot olinmadi — ehtiyot bo‘ling.")

# ==================== WEB PANEL ====================
class WebPanelHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        key = query.get("key", [None])[0]

        if parsed.path in ("/", "/health"):
            self.send_response(200)
            self.send_header("Content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")
            return

        if parsed.path == "/admin":
            if key != ADMIN_PANEL_SECRET:
                self.send_response(403)
                self.end_headers()
                self.wfile.write(b"403 Forbidden")
                return

            conn = sqlite3.connect("bot_database.db")
            c = conn.cursor()
            c.execute("SELECT user_id, username, full_name, reputation FROM users ORDER BY reputation DESC LIMIT 50")
            users = c.fetchall()
            c.execute("SELECT domain FROM blacklist")
            black = c.fetchall()
            c.execute("SELECT user_id, action, details, timestamp FROM activity_logs ORDER BY id DESC LIMIT 30")
            logs = c.fetchall()
            conn.close()

            users_rows = "".join(
                f"<tr><td>{u[0]}</td><td>@{u[1] or '-'}</td><td>{u[2]}</td><td>{u[3]}</td></tr>"
                for u in users
            )
            black_rows = "".join(f"<li>{b[0]}</li>" for b in black) or "<li>Bo'sh</li>"
            log_rows = "".join(
                f"<tr><td>{l[0]}</td><td>{l[1]}</td><td>{str(l[2])[:60]}</td><td>{l[3]}</td></tr>"
                for l in logs
            )

            html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Admin Panel</title>
<style>
body {{ font-family: system-ui, sans-serif; background: #0f172a; color: #e2e8f0; margin: 0; padding: 20px; }}
.card {{ background: #1e293b; padding: 16px; margin: 12px 0; border-radius: 10px; }}
.stat {{ display: inline-block; background: #0f172a; padding: 10px 14px; margin: 4px; border-radius: 8px; text-align: center; min-width: 80px; }}
table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
th, td {{ border: 1px solid #334155; padding: 6px; text-align: left; }}
th {{ background: #0f172a; color: #94a3b8; }}
input, textarea {{ width: 100%; padding: 8px; margin: 6px 0; border-radius: 6px; border: 1px solid #334155; background: #0f172a; color: #e2e8f0; }}
button {{ background: #0ea5e9; color: white; border: none; padding: 8px 14px; border-radius: 6px; cursor: pointer; }}
</style>
</head>
<body>
<h1>🛡️ Kiber Admin Panel</h1>

<div class="card">
<b>Statistika</b><br>
<div class="stat">Havola<br><b>{stats['checked_count']}</b></div>
<div class="stat">Phishing<br><b>{stats['danger_count']}</b></div>
<div class="stat">Fayl<br><b>{stats['file_danger_count']}</b></div>
<div class="stat">Rasm<br><b>{stats['photo_danger_count']}</b></div>
<div class="stat">Ovoz<br><b>{stats['voice_danger_count']}</b></div>
<div class="stat">Video<br><b>{stats['video_danger_count']}</b></div>
<div class="stat">User<br><b>{len(users)}</b></div>
</div>

<div class="card">
<h3>📢 Broadcast</h3>
<form method="POST" action="/broadcast?key={ADMIN_PANEL_SECRET}">
<textarea name="message" rows="2" placeholder="E'lon matni..."></textarea>
<button type="submit">Yuborish</button>
</form>
</div>

<div class="card">
<h3>🚫 Qora ro'yxat</h3>
<form method="POST" action="/add_blacklist?key={ADMIN_PANEL_SECRET}">
<input type="text" name="domain" placeholder="domen.uz">
<button type="submit">Qo'shish</button>
</form>
<ul>{black_rows}</ul>
</div>

<div class="card">
<h3>⚡ So'nggi loglar</h3>
<table>
<tr><th>User</th><th>Amal</th><th>Info</th><th>Vaqt</th></tr>
{log_rows}
</table>
</div>

<div class="card">
<h3>👥 Foydalanuvchilar</h3>
<table>
<tr><th>ID</th><th>Username</th><th>Ism</th><th>Karma</th></tr>
{users_rows}
</table>
</div>

</body>
</html>"""

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
        params = parse_qs(self.rfile.read(length).decode())

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

    def log_message(self, *args):
        pass

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
            except Exception:
                pass

    loop.run_until_complete(send())

def run_http_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), WebPanelHandler).serve_forever()

# ==================== MAIN ====================
async def main():
    threading.Thread(target=run_http_server, daemon=True).start()
    await bot.delete_webhook(drop_pending_updates=True)
    await set_commands()
    print("✅ Bot muvaffaqiyatli ishga tushdi")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
