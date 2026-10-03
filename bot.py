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
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from google import genai
from PIL import Image

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("BOT_TOKEN topilmadi! Render'dagi Environment bo'limiga BOT_TOKEN qo'shganingizni tekshiring.")

ADMIN_ID = 5081583283  # O'z Telegram ID raqamingiz

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ai_client = genai.Client(api_key=GEMINI_API_KEY)

stats = {
    "checked_count": 0,
    "danger_count": 0,
    "audit_count": 0,
    "screenshot_count": 0
}

# --- BAZA BILAN ISHLASH ---
DB_NAME = "bot_database.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            language TEXT DEFAULT 'uz'
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS blacklist (
            domain TEXT PRIMARY KEY,
            added_by INTEGER
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
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO activity_logs (user_id, action, details) VALUES (?, ?, ?)", (user_id, action, details))
    conn.commit()
    conn.close()

def add_user(user_id, username, full_name):
    safe_username = str(username)[:50] if username else ""
    safe_fullname = str(full_name)[:100] if full_name else "Foydalanuvchi"
    
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO users (user_id, username, full_name, language) 
        VALUES (?, ?, ?, 'uz')
        ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, full_name=excluded.full_name
    """, (user_id, safe_username, safe_fullname))
    conn.commit()
    conn.close()

def get_user_lang(user_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT language FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else 'uz'

def set_user_lang(user_id, lang):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET language = ? WHERE user_id = ?", (lang, user_id))
    conn.commit()
    conn.close()

def add_global_blacklist(domain, user_id=ADMIN_ID):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO blacklist (domain, added_by) VALUES (?, ?)", (domain.lower(), user_id))
    conn.commit()
    conn.close()

def is_globally_blacklisted(domain):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM blacklist WHERE domain = ?", (domain.lower(),))
    row = cursor.fetchone()
    conn.close()
    return row is not None

# --- LUG'ATLAR (Ko'p tillilik) ---
TEXTS = {
    'uz': {
        'start': "👋 Assalomu alaykum!\n\nAI kiber-xavfsizlik va phishing havolalarni aniqlovchi botga xush kelibsiz. Shubhali havolani yuboring.",
        'lang_set': "✅ Til o'zbek tiliga o'zgartirildi.",
        'spam': "⚠️ Juda tez-tez xabar yuboryapsiz! Iltimos, 1.5 soniya kuting.",
        'safe_link': "🔗 **Link:** Ofitsial va ishonchli manzil.",
        'danger_link': "🚨 **DIQQAT! PHISHING / SOXTA SAYT ANIQLANDI!**",
        'safe_screenshot': "✅ Sayt skrinshoti tekshirildi. Xavfli alomatlar topilmadi.",
        'report_btn': "🚨 Qora ro'yxatga qo'shishni so'rash",
        'weekly_tip': "🛡️ **Haftalik Kiber-Ogohlik:**\n\nInternetda ehtiyot bo'ling! Shubhali havolalarga kirmang, bank kartasi parollari va SMS-kodlarni hech kimga bermang. Har qanday shubhali havolani tekshirish uchun menga yuborishingiz mumkin!",
        'help': "ℹ️ **Qo'llanma:**\n- `/audit <havola yoki kanal>` — Kiber-audit\n- `/lang` — Tilni o'zgartirish"
    },
    'ru': {
        'start': "👋 Здравствуйте!\n\nДобро пожаловать в бот кибербезопасности и защиты от фишинга. Отправьте подозрительную ссылку.",
        'lang_set': "✅ Язык изменен на русский.",
        'spam': "⚠️ Слишком частые запросы! Пожалуйста, подождите 1.5 секунды.",
        'safe_link': "🔗 **Ссылка:** Официальный и надежный адрес.",
        'danger_link': "🚨 **ВНИМАНИЕ! ОБНАРУЖЕН ФИШИНГ / МОШЕННИЧЕСКИЙ САЙТ!**",
        'safe_screenshot': "✅ Скриншот сайта проверен. Опасных признаков не обнаружено.",
        'report_btn': "🚨 Запросить добавление в черный список",
        'weekly_tip': "🛡️ **Еженедельная кибербезопасность:**\n\nБудьте осторожны в сети! Не переходите по подозрительным ссылкам, никому не сообщайте пароли карт и SMS-коды. Отправляйте любые сомнительные ссылки мне для проверки!",
        'help': "ℹ️ **Помощь:**\n- `/audit <ссылка>` — Кибер-аудит\n- `/lang` — Изменить язык"
    },
    'en': {
        'start': "👋 Hello!\n\nWelcome to the AI Cybersecurity & Anti-Phishing bot. Send a suspicious link to check.",
        'lang_set': "✅ Language changed to English.",
        'spam': "⚠️ You are sending messages too fast! Please wait 1.5 seconds.",
        'safe_link': "🔗 **Link:** Official and trusted address.",
        'danger_link': "🚨 **ATTENTION! PHISHING / FAKE WEBSITE DETECTED!**",
        'safe_screenshot': "✅ Webpage screenshot checked. No dangerous signs found.",
        'report_btn': "🚨 Request to Blacklist",
        'weekly_tip': "🛡️️ **Weekly Cyber Alert:**\n\nStay safe online! Do not click suspicious links, never share your bank card passwords or SMS codes. You can send any suspicious link to me to check!",
        'help': "ℹ️ **Help:**\n- `/audit <link>` — Cyber audit\n- `/lang` — Change language"
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

user_last_time = {}
SPAM_LIMIT = 1.5

# --- WEBHOOK & ADMIN PANEL ---
WEBHOOK_PATH = f"/webhook/{TOKEN}"
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:10000")
WEBHOOK_URL = f"{RENDER_URL}{WEBHOOK_PATH}"

class WebPanelHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed_path = urlparse(self.path)
        if parsed_path.path == "/" or parsed_path.path == "/health":
            self.send_response(200)
            self.send_header("Content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Bot and Chart Admin Panel are running securely!")
            return
            
        if parsed_path.path == "/admin":
            conn = sqlite3.connect(DB_NAME)
            cursor = conn.cursor()
            cursor.execute("SELECT user_id, username, full_name, language FROM users")
            users = cursor.fetchall()
            
            cursor.execute("SELECT domain, added_by FROM blacklist")
            blacklisted = cursor.fetchall()
            
            cursor.execute("SELECT user_id, action, details, timestamp FROM activity_logs ORDER BY id DESC LIMIT 20")
            logs = cursor.fetchall()
            conn.close()
            
            users_rows = "".join([f"<tr><td>{u[0]}</td><td>@{u[1]}</td><td>{u[2]}</td><td>{u[3]}</td></tr>" for u in users])
            blacklist_rows = "".join([f"<li>{b[0]} (Qo'shgan: {b[1]})</li>" for b in blacklisted])
            log_rows = "".join([f"<tr><td>{l[0]}</td><td>{l[1]}</td><td>{l[2]}</td><td>{l[3]}</td></tr>" for l in logs])

            html = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <title>Kiber Bot - SOC Admin Panel</title>
                <meta charset="utf-8">
                <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
                <style>
                    body {{ font-family: Arial, sans-serif; background: #f0f2f5; margin: 0; padding: 20px; color: #333; }}
                    h1, h2 {{ color: #1a73e8; }}
                    .container {{ max-width: 1000px; margin: auto; }}
                    .card {{ background: white; padding: 20px; margin-bottom: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
                    .stats-grid {{ display: flex; gap: 15px; flex-wrap: wrap; }}
                    .stat-box {{ background: #e8f0fe; padding: 15px; border-radius: 6px; flex: 1; min-width: 180px; text-align: center; }}
                    .chart-container {{ width: 100%; max-width: 500px; margin: auto; }}
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
                    <h1>🛡️ Kiber-Xavfsizlik Boshqaruv Markazi</h1>
                    
                    <div class="card">
                        <h2>📊 Asosiy Statistika va Grafik</h2>
                        <div class="stats-grid">
                            <div class="stat-box"><h3>{stats['checked_count']}</h3><p>Tekshirilgan Havolalar</p></div>
                            <div class="stat-box"><h3>{stats['danger_count']}</h3><p>Bloklangan Phishing</p></div>
                            <div class="stat-box"><h3>{stats['audit_count']}</h3><p>Kiber-Auditlar</p></div>
                            <div class="stat-box"><h3>{len(users)}</h3><p>Foydalanuvchilar</p></div>
                        </div>
                        <div class="chart-container" style="margin-top: 20px;">
                            <canvas id="statsChart"></canvas>
                        </div>
                    </div>

                    <div class="card">
                        <h2>📢 Global Xabar Tarqatish (Broadcast)</h2>
                        <form method="POST" action="/broadcast">
                            <textarea name="message" rows="3" placeholder="E'lon matnini kiriting..."></textarea>
                            <button type="submit">Xabarni yuborish</button>
                        </form>
                    </div>

                    <div class="card">
                        <h2>🚫 Qora Ro'yxatdagi Domenlar</h2>
                        <form method="POST" action="/add_blacklist">
                            <input type="text" name="domain" placeholder="shubhali-sayt.uz">
                            <button type="submit">Qora ro'yxatga qo'shish</button>
                        </form>
                        <ul>{blacklist_rows}</ul>
                    </div>

                    <div class="card">
                        <h2>⚡ Jonli Faoliyat Jurnali</h2>
                        <table>
                            <tr><th>User ID</th><th>Amal</th><th>Tafsilot</th><th>Vaqt</th></tr>
                            {log_rows}
                        </table>
                    </div>

                    <div class="card">
                        <h2>👥 Foydalanuvchilar Ro'yxati</h2>
                        <table>
                            <tr><th>ID</th><th>Username</th><th>Ism</th><th>Til</th></tr>
                            {users_rows}
                        </table>
                    </div>
                </div>

                <script>
                    const ctx = document.getElementById('statsChart').getContext('2d');
                    const statsChart = new Chart(ctx, {{
                        type: 'doughnut',
                        data: {{
                            labels: ['Xavfsiz havolalar', 'Bloklangan Phishing', 'Kiber-Auditlar'],
                            datasets: [{{
                                data: [{stats['checked_count'] - stats['danger_count']}, {stats['danger_count']}, {stats['audit_count']}],
                                backgroundColor: ['#34a853', '#ea4335', '#fbbc05']
                            }}]
                        }},
                        options: {{
                            responsive: true,
                            plugins: {{
                                legend: {{ position: 'bottom' }}
                            }}
                        }}
                    }});
                </script>
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
                add_global_blacklist(domain, ADMIN_ID)
                log_activity(ADMIN_ID, "BLACKLIST_ADD", f"Admin qo'shdi: {domain}")
            self.send_response(303)
            self.send_header('Location', '/admin')
            self.end_headers()
            return

        if parsed_path.path == "/broadcast":
            broadcast_msg = params.get("message", [""])[0].strip()
            if broadcast_msg:
                log_activity(ADMIN_ID, "BROADCAST", "Xabar yuborildi")
                threading.Thread(target=run_broadcast, args=(broadcast_msg,), daemon=True).start()
            self.send_response(303)
            self.send_header('Location', '/admin')
            self.end_headers()
            return

        if parsed_path.path == WEBHOOK_PATH:
            self.send_response(200)
            self.end_headers()
            update_data = requests.utils.json.loads(post_data)
            asyncio.run_coroutine_threadsafe(dp.feed_raw_update(bot, update_data), bot_loop)
            return

        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        return

def run_broadcast(text):
    conn = sqlite3.connect(DB_NAME)
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
                await asyncio.sleep(0.04)
            except Exception:
                pass

    loop.run_until_complete(send_all())

# --- HAR HAFTALIK AVTOMATIK OGOHLIK XABARI ---
async def weekly_security_reminder_loop():
    while True:
        # Har 7 kunda bir marta ishlaydi (7 * 24 * 3600 soniya)
        await asyncio.sleep(7 * 24 * 3600)
        
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, language FROM users")
        users = cursor.fetchall()
        conn.close()
        
        for u_id, lang in users:
            l = lang if lang in TEXTS else 'uz'
            try:
                await bot.send_message(u_id, TEXTS[l]['weekly_tip'], parse_mode="Markdown")
                await asyncio.sleep(0.05)
            except Exception:
                pass

bot_loop = None

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

# --- BOT HANDLERLARI ---
@dp.message(Command("start"))
async def cmd_start(message: Message):
    user_id = message.from_user.id
    add_user(user_id, message.from_user.username, message.from_user.full_name)
    log_activity(user_id, "START", "Botni ishga tushirdi")
    lang = get_user_lang(user_id)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇺🇿 O'zbekcha", callback_data="lang_uz"),
         InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang_ru"),
         InlineKeyboardButton(text="🇬🇧 English", callback_data="lang_en")]
    ])
    await message.answer(TEXTS[lang]['start'] + "\n\n🌐 Tilni tanlang / Выберите язык / Choose language:", reply_markup=kb)

@dp.message(Command("lang"))
async def cmd_lang(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇺🇿 O'zbekcha", callback_data="lang_uz"),
         InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang_ru"),
         InlineKeyboardButton(text="🇬🇧 English", callback_data="lang_en")]
    ])
    await message.answer("🌐 Tilni tanlang / Выберите язык / Choose language:", reply_markup=kb)

@dp.callback_query(F.data.startswith("lang_"))
async def callback_set_lang(callback: CallbackQuery):
    user_id = callback.from_user.id
    lang = callback.data.split("_")[1]
    set_user_lang(user_id, lang)
    await callback.message.edit_text(TEXTS[lang]['lang_set'])
    await callback.answer()

@dp.message(Command("audit"))
async def cmd_audit(message: Message):
    user_id = message.from_user.id
    lang = get_user_lang(user_id)
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("❌ Foydalanish: `/audit <havola yoki kanal>`", parse_mode="Markdown")
        return
    target = args[1].strip()
    stats["audit_count"] += 1
    log_activity(user_id, "AUDIT", target)
    await message.answer(f"🕵‍♂️ **Avtonom Kiber-Detektiv Agent** tahlilni boshladi: `{target}`", parse_mode="Markdown")

    try:
        response = ai_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=f"Perform a professional cybersecurity OSINT audit and threat analysis on: '{target}'. Provide report in {lang} language."
        )
        await message.answer(f"🛡️ **KIBER-AUDIT HISOBOTI**\n\n{response.text}", parse_mode="Markdown")
    except Exception:
        await message.answer("❌ Audit jarayonida xatolik yuz berdi.")

@dp.message(Command("web"))
async def cmd_web(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Bu buyruq faqat admin uchun.")
        return
    await message.answer(f"🌐 **Admin Veb-paneli (Grafiklar bilan):**\n\n[Panelni ochish]({RENDER_URL}/admin)", parse_mode="Markdown")

@dp.callback_query(F.data.startswith("req_black:"))
async def callback_request_blacklist(callback: CallbackQuery):
    domain_to_add = callback.data.split(":", 1)[1]
    user = callback.from_user
    
    admin_text = f"🚨 **Yangi Qora Ro'yxat So'rovi!**\n\nFoydalanuvchi: [{user.full_name}](tg://user?id={user.id}) (@{user.username or 'yoq'})\nDomen: `{domain_to_add}`"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"adm_add_bl:{domain_to_add}:{user.id}"),
         InlineKeyboardButton(text="❌ Rad etish", callback_data="adm_rej_bl")]
    ])
    try:
        await bot.send_message(ADMIN_ID, admin_text, reply_markup=keyboard, parse_mode="Markdown")
        await callback.answer("✅ So'rovingiz adminga yuborildi. Rahmat!", show_alert=True)
    except Exception:
        await callback.answer("❌ Xatolik yuz berdi.", show_alert=True)

@dp.callback_query(F.data.startswith("adm_add_bl:"))
async def callback_admin_approve_blacklist(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    parts = callback.data.split(":")
    domain = parts[1]
    user_id = int(parts[2])
    
    add_global_blacklist(domain, user_id)
    log_activity(ADMIN_ID, "USER_BLACKLIST_APPROVE", f"Domen tasdiqlandi: {domain}")
    await callback.message.edit_text(f"✅ Domen qora ro'yxatga qo'shildi: `{domain}`", parse_mode="Markdown")
    try:
        await bot.send_message(user_id, f"🎉 Siz yuborgan `{domain}` manzili admin tomonidan tasdiqlandi va qora ro'yxatga qo'shildi!", parse_mode="Markdown")
    except Exception:
        pass

@dp.callback_query(F.data == "adm_rej_bl")
async def callback_admin_reject_blacklist(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.edit_text("❌ So'rov rad etildi.")

@dp.message(F.text)
async def handle_message(message: Message):
    user_id = message.from_user.id
    
    current_time = time.time()
    last_time = user_last_time.get(user_id, 0)
    lang = get_user_lang(user_id)
    
    if current_time - last_time < SPAM_LIMIT:
        await message.answer(TEXTS[lang]['spam'])
        return
    user_last_time[user_id] = current_time

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
            await message.answer(f"🚨 DIQQAT! Ushbu manzil qora ro'yxatga kiritilgan (firibgar sayt)!")
            return

        if domain.endswith('.gov.uz') or domain in OFFICIAL_DOMAINS:
            await message.answer(TEXTS[lang]['safe_link'])
            return

        screenshot_bytes = get_webpage_screenshot(url)
        if screenshot_bytes:
            stats["screenshot_count"] += 1
            image = Image.open(BytesIO(screenshot_bytes))
            try:
                response = ai_client.models.generate_content(
                    model='gemini-2.5-flash',
                    contents=[
                        f"Analyze this webpage screenshot for phishing, fake bank/login pages, scam schemes, or typosquatting. Reply in {lang} language. "
                        "Start response strictly with '🚨 PHISHING/SCAM' if dangerous, or '✅ SAFE' if legitimate.",
                        image
                    ]
                )
                
                report_kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text=TEXTS[lang]['report_btn'], callback_data=f"req_black:{domain}")]
                ])

                if "PHISHING" in response.text.upper() or "SCAM" in response.text.upper():
                    stats["danger_count"] += 1
                    log_activity(user_id, "PHISHING_DETECTED", domain)
                    await message.answer_photo(
                        photo=BufferedInputFile(screenshot_bytes, filename="screenshot.jpg"),
                        caption=f"{TEXTS[lang]['danger_link']}\n\n{response.text}",
                        reply_markup=report_kb,
                        parse_mode="Markdown"
                    )
                else:
                    await message.answer(
                        f"{TEXTS[lang]['safe_screenshot']}\n\n{response.text}",
                        reply_markup=report_kb,
                        parse_mode="Markdown"
                    )
            except Exception:
                pass

async def main():
    global bot_loop
    bot_loop = asyncio.get_running_loop()
    
    # Har haftalik avtomatik xabar yuborish vazifasini ishga tushirish
    asyncio.create_task(weekly_security_reminder_loop())
    
    threading.Thread(target=run_http_server, daemon=True).start()
    await bot.set_webhook(WEBHOOK_URL)
    print(f"Bot Webhook rejimida ishga tushdi: {WEBHOOK_URL}")
    
    await asyncio.Event().wait()

if __name__ == '__main__':
    asyncio.run(main())
