import asyncio
import logging
import re
import os
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, InlineQuery, InlineQueryResultArticle, InputTextMessageContent
from google import genai

TOKEN = os.environ.get("8963497136:AAF44_6VpG5Uw4rlTjWS7kYUDv1HA8Bp0Jw") # Yoki o'zingizning tokeningiz
ADMIN_ID = 5081583283  # O'z Telegram ID raqamingizni yozing

# Google Gemini AI sozlamasi
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
ai_client = genai.Client(api_key=GEMINI_API_KEY)

stats = {
    "checked_count": 0,
    "danger_count": 0
}

# --- 3-TАKLIF: FLOOD CONTROL (SPAMDAN HIMOYA) UCHUN LUG'AT ---
user_last_message_time = {}
SPAM_INTERVAL = 1.5  # Foydalanuvchi orasida 1.5 soniya farq bo'lishi kerak

SUSPICIOUS_TLDS = [
    '.xyz', '.cc', '.tk', '.buzz', '.top', '.gq', '.ml', '.cf', '.ru.com',
    '.online', '.site', '.club', '.work', '.click', '.link', '.pw', '.su',
    '.bid', '.loan', '.win', '.stream', '.icu', '.cam', '.cfd', '.VIP'
]

OFFICIAL_DOMAINS = {
    'gov.uz', 'my.gov.uz', 'pm.gov.uz', 'lex.uz', 'cbu.uz', 'stat.uz', 'customs.uz',
    'soliq.uz', 'my.soliq.uz', 'uzgidromet.uz', 'mehnat.uz', 'my.mehnat.uz',
    'iiv.uz', 'mfa.uz', 'minjust.uz', 'uzedu.uz', 'ssv.uz', 'tiiame.uz',
    'muslim.uz', 'fatvo.uz', 'quran.uz', 'old.muslim.uz', 'ziyouz.uz', 'buxari.uz',
    'nbu.uz', 'agrobank.uz', 'kapitalbank.uz', 'ipotekabank.uz', 'davrbank.uz',
    'orientfinanzbank.uz', 'hamkorbank.uz', 'asakabank.uz', 'anorbank.uz',
    'tbcbank.uz', 'octobank.uz', 'infinbank.uz', 'ipakyulibank.uz', 'aloqabank.uz',
    'trastbank.uz', 'microcreditbank.uz', 'sqb.uz', 'mkbank.uz', 'ravnaqbank.uz',
    'poytaxtbank.uz', 'universalbank.uz', 'tengebank.uz', 'aab.uz',
    'uzcard.uz', 'humocard.uz', 'click.uz', 'payme.uz', 'uzum.uz', 'uzummarket.uz',
    'paynet.uz', 'zoodmall.uz', 'texnomart.uz', 'elmakon.uz', 'mediapark.uz',
    'asaxiy.uz', 'olcha.uz', 'express24.uz', 'uzpost.uz', 'pochta.uz',
    'kun.uz', 'gazeta.uz', 'daryo.uz', 'upl.uz', 'podrobno.uz', 'uzbekistan24.uz',
    'repost.uz', 'darakchi.uz', 'qalampir.uz', 'sputniknews.uz', 'xabar.uz',
    'uztelecom.uz', 'ucell.uz', 'beeline.uz', 'mobi.uz', 'humans.uz', 'uzdigital.tv'
}

OFFICIAL_TELEGRAM = {
    'muslimuzportal', 'fatvouz', 'ziyouz', 'hilolnashr', 'quron_va_sunnat',
    'shayx_muhammad_sodiq', 'islomuz', 'buxoroislomuz',
    'davxizmat', 'uzgovuz', 'soliquz', 'cbu_uz', 'uztelecomuz',
    'agrobank_uz', 'kapitalbank_uz', 'anorbank', 'tbcbankuz', 'octobank', 
    'infinbank', 'ipakyuli_bank', 'aloqabank_official', 'sqb_official', 'mkbank_uz',
    'kunuzofficial', 'gazetauz', 'daryo', 'upluz', 'repostuz', 'qalampiruz',
    'asaxiy', 'olchouz', 'texnomart', 'uzummarket', 'clickuz', 'payme_uz',
    'uzcard_uz', 'humocard', 'beeline_uz', 'ucell', 'mobiuzofficial', 'express24'
}

OFFICIAL_INSTAGRAM = {
    'muslimuz', 'fatvo_uz', 'ziyouz', 'hilolnashr', 'islomuz_official',
    'mygovuz', 'soliq.uz', 'cbu.uz', 'agrobank_uz', 'kapitalbank_uz', 'anorbank',
    'tbcbankuz', 'octobank.uz', 'infinbank', 'ipakyulibank', 'aloqabank.uz',
    'sqb.uz', 'mkbank.uz',
    'kunuz', 'gazetauz', 'daryo_uz', 'asaxiyuz', 'olchouz', 'texnomart',
    'uzum.market', 'express24_uz', 'click.uz', 'payme.uz', 'uzcard.uz',
    'humocard', 'beeline_uz', 'ucell_uz', 'mobiuz.uz', 'uztelecom_uz'
}

BRAND_KEYWORDS = [
    'muslim', 'fatvo', 'hilol', 'ziyouz', 'uzcard', 'humo', 'soliq', 'mygov', 
    'agrobank', 'kapitalbank', 'anorbank', 'tbc', 'octobank', 'infinbank', 
    'ipakyuliy', 'aloqabank', 'trastbank', 'sqb', 'mkbank', 'uzum', 'beeline', 
    'ucell', 'mobiuz', 'uztelecom', 'humans', 'click', 'payme', 'asaxiy', 'olcha', 'texnomart', 'express24'
]

SCAM_WORDS = [
    'yutib oldingiz', 'bonus', 'sovg', 'pul ishlang', 'aktsiya', 'keshbek', 
    'konkurs', 'sovg\'a', 'qur’a', 'qura', 'tekin', 'free money'
]

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
dp = Dispatcher()

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

def analyze_link(url: str) -> dict:
    global stats
    stats["checked_count"] += 1
    
    url_lower = url.lower()

    if "t.me/" in url_lower or "telegram.me/" in url_lower:
        parts = url_lower.split("t.me/")
        if len(parts) > 1:
            path = parts[1].split("/")[0].strip()
            if path in OFFICIAL_TELEGRAM:
                return {"status": "safe", "msg": "✅ Bu **rasmiy va tasdiqlangan Telegram** manzil."}
            return {"status": "warning", "msg": f"⚠️ **Telegram havola aniqlandi.** (@{path})\nFiribgarlar brend, OAV yoki diniy idora nomini o'xshatib soxta kanal ochgan bo'lishi mumkin, ehtiyot bo'ling!"}

    if "instagram.com/" in url_lower:
        parts = url_lower.split("instagram.com/")
        if len(parts) > 1:
            path = parts[1].split("/")[0].strip()
            if path in OFFICIAL_INSTAGRAM:
                return {"status": "safe", "msg": f"✅ Bu **rasmiy Instagram** sahifasi (@{path})."}
            return {"status": "warning", "msg": f"⚠️ **Instagram sahifa aniqlandi.** (@{path})\nBu sahifa rasmiy bazada yo'q. Soxta profil bo'lishi mumkin!"}

    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url
        
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    if domain.startswith('www.'):
        domain = domain[4:]
        
    if domain in OFFICIAL_DOMAINS:
        return {"status": "safe", "msg": "✅ Bu **rasmiy va ishonchli** O'zbekiston sayti."}
        
    for tld in SUSPICIOUS_TLDS:
        if domain.endswith(tld):
            stats["danger_count"] += 1
            return {"status": "danger", "msg": f"🚨 **DIQQAT! XAVFLI HAVOLA!**\nShubhali zona ({tld}) aniqlandi. Firibgarlar tuzog'i bo'lishi mumkin."}
            
    for brand in BRAND_KEYWORDS:
        if brand in domain:
            stats["danger_count"] += 1
            return {"status": "danger", "msg": f"⚠ **OGOHLANTIRISH! Soxtalashtirish alomatlari bor!**\nUshbu havola **{brand}** brendini yoki rasmiy nomni niqob qilib olgan."}

    return {"status": "warning", "msg": "⚠️ **Noma'lum havola.**\nBazada yo'q, shaxsiy ma'lumotlarni kiritishda ehtiyot bo'ling!"}

async def ask_gemini(text: str) -> str:
    try:
        prompt = f"""
        Siz O'zbekistondagi kiberxavfsizlik va firibgarlikka qarshi kurashish bo'yicha yordamchi AI botisiz.
        Foydalanuvchi quyidagi matnni yubordi. Bu matnda firibgarlik (scam, phishing, yolg'on yutuqlar, soxta aksiya) alomatlari bor-yo'qligini qisqacha, lo'nda va o'zbek tilida tahlil qilib ber:
        
        Matn: "{text}"
        """
        response = ai_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
        )
        return response.text
    except Exception as e:
        return ""

@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "👋 Assalomu alaykum!\n\n"
        "Men O‘zbekistondagi rasmiy saytlar, banklar, OAV va diniy-ma'rifiy kanallarni tekshiruvchi hamda **Sun'iy Intellekt (Gemini AI)** bilan jihozlangan xavfsizlik botiman.\n\n"
        "🔍 Menga havola yoki shubhali xabar yuboring!"
    )

@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    await message.answer(
        f"📊 **Bot Statistikasi:**\n\n"
        f"🔍 Tekshirilgan havolalar: {stats['checked_count']}\n"
        f"🚨 Aniqlangan xavfli havolalar: {stats['danger_count']}"
    )

@dp.message(Command("report"))
async def cmd_report(message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("❌ Iltimos, /report buyrug'idan keyin shubhali havolani yozing.")
        return
    reported_url = args[1]
    user_info = f"@{message.from_user.username}" if message.from_user.username else message.from_user.full_name
    if ADMIN_ID:
        try:
            await bot.send_message(ADMIN_ID, f"🚨 **Yangi shikoyat!**\nKimdan: {user_info}\nHavola: {reported_url}")
        except Exception:
            pass
    await message.answer("✅ Shikoyatingiz adminga yuborildi. Rahmat!")

@dp.inline_query()
async def inline_checker(query: InlineQuery):
    text = query.query.strip()
    if not text:
        return
    result = analyze_link(text)
    articles = [
        InlineQueryResultArticle(
            id="1",
            title="Havola xavfsizligini tekshirish",
            input_message_content=InputTextMessageContent(
                message_text=f"🔍 Tekshirilgan manzil: `{text}`\n\n{result['msg']}",
                parse_mode="Markdown"
            )
        )
    ]
    await query.answer(articles, cache_time=1)

@dp.message(F.text)
async def handle_message(message: Message):
    user_id = message.from_user.id
    current_time = time.time()
    
    # --- FLOOD CONTROL TEKshiruvi ---
    if user_id in user_last_message_time:
        elapsed = current_time - user_last_message_time[user_id]
        if elapsed < SPAM_INTERVAL:
            await message.answer("⚠️ Juda tez-tez xabar yuboryapsiz! Iltimos, biroz kuting.")
            return
            
    user_last_message_time[user_id] = current_time
    # ---------------------------------

    text = message.text.lower()
    found_scam_word = any(word in text for word in SCAM_WORDS)
    url = extract_url(message.text)
    
    response_parts = []
    
    if found_scam_word:
        response_parts.append("🛑 **DIQQAT! Matnda firibgarlikka xos so'zlar aniqlandi!**")
        
    if url:
        result = analyze_link(url)
        response_parts.append(f"🔗 **Havola tahlili:**\n{result['msg']}")
    
    ai_analysis = await ask_gemini(message.text)
    if ai_analysis:
        response_parts.append(f"🤖 **Sun'iy Intellekt (AI) xulosasi:**\n{ai_analysis}")
        
    if not response_parts:
        response_parts.append("✅ Matnda xavfli belgilar topilmadi, lekin baribir hushyor bo'ling.")
        
    await message.answer("\n\n".join(response_parts))

async def main():
    threading.Thread(target=run_http_server, daemon=True).start()
    print("Bot va veb-server ishga tushdi...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
