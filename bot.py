import asyncio
import logging
import re
from urllib.parse import urlparse
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message

# Bot tokeningizni shu yerga qo'shtirnoq ichiga yozing
TOKEN = "YOUR_BOT_TOKEN_HERE"

# Maksimal kengaytirilgan shubhali domen oxirlari (TLD)
SUSPICIOUS_TLDS = [
    '.xyz', '.cc', '.tk', '.buzz', '.top', '.gq', '.ml', '.cf', '.ru.com',
    '.online', '.site', '.club', '.work', '.click', '.link', '.pw', '.su',
    '.bid', '.loan', '.win', '.stream', '.icu', '.cam', '.cfd', '.VIP',
    '.CBD', '.TOPS', '.SPACE', '.REST', '.PRO', '.INFO', '.SUPPORT', '.GOP'
]

# O‘zbekistondagi rasmiy domenlar (Oq ro'yxat) - Banklar, davlat, aloqa, pochta va marketpleyslar
OFFICIAL_DOMAINS = {
    # Davlat va Soliq
    'my.gov.uz', 'gov.uz', 'soliq.uz', 'my.soliq.uz', 'pm.gov.uz', 'lex.uz',
    'stat.uz', 'customs.uz', 'mfa.uz', 'uzgidromet.uz', 'miib.uz',
    # To'lov tizimlari va Banklar
    'uzcard.uz', 'humocard.uz', 'agrobank.uz', 'kapitalbank.uz', 'ipotekabank.uz',
    'nbu.uz', 'davrbank.uz', 'orientfinanzbank.uz', 'hamkorbank.uz', 'asakabank.uz',
    'milliybank.uz', 'anorbank.uz', 'tbcbank.uz', 'octobank.uz', 'infinbank.uz',
    'ipakyulibank.uz', 'aloqabank.uz', 'trastbank.uz', 'microcreditbank.uz',
    'sqb.uz', 'mkbank.uz', 'poytaxtbank.uz', 'tengebank.uz', 'ziraatbank.uz',
    'universalbank.uz', 'octobank.uz', 'davrbank.uz', 'octobank.uz',
    # Aloqa va Mobil operatorlar
    'uztelecom.uz', 'ucell.uz', 'beeline.uz', 'mobi.uz', 'uzmobile.uz', 'humans.uz',
    # Pochta va yetkazib berish / Savdo
    'pochta.uz', 'uzposhta.uz', 'express.uz', 'uzum.uz', 'uzummarket.uz',
    'olcha.uz', 'asaxiy.uz', 'zoodmall.uz'
}

# Himoya qilinadigan brend nomlari (Soxtalashtirishni aniqlash uchun)
BRAND_KEYWORDS = [
    'uzcard', 'humo', 'soliq', 'mygov', 'agrobank', 'kapitalbank', 'anorbank', 
    'tbc', 'octobank', 'infinbank', 'ipakyuliy', 'aloqabank', 'trastbank', 'sqb', 
    'mkbank', 'tenge', 'ziraat', 'universal', 'uzum', 'beeline', 'ucell', 
    'mobiuz', 'uztelecom', 'humans', 'pochta', 'uzposhta', 'davlat', 'pasport', 
    'notarius', 'stimul', 'mib', 'kadastr'
]

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
dp = Dispatcher()

def extract_url(text: str) -> str:
    """Matn ichidan havolani ajratib olish"""
    url_pattern = re.compile(r'https?://[^\s]+|www\.[^\s]+|[a-zA-Z0-9][-a-zA-Z0-9@:%._\+~#=]{1,256}\.[a-zA-Z0-9()]{1,6}\b(?:[-a-zA-Z0-9()@:%_\+.~#?&//=]*)')
    match = url_pattern.search(text)
    return match.group(0) if match else None

def analyze_link(url: str) -> dict:
    """Havolani xavfsizlikka tekshirish logikasi"""
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url
        
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    
    if domain.startswith('www.'):
        domain = domain[4:]
        
    if domain in OFFICIAL_DOMAINS:
        return {"status": "safe", "msg": "✅ Bu **rasmiy va xavfsiz** manzil deb topildi."}
        
    for tld in SUSPICIOUS_TLDS:
        if domain.endswith(tld):
            return {"status": "danger", "msg": f"🚨 **DIQQAT! XAVFLI HAVOLA!**\nBu sayt shubhali zona ({tld}) da joylashgan va firibgarlar tomonidan ishlatilishi mumkin."}
            
    for brand in BRAND_KEYWORDS:
        if brand in domain:
            if domain not in OFFICIAL_DOMAINS:
                return {"status": "danger", "msg": f"⚠️️ **OGOHLANTIRISH! Soxtalashtirish alomatlari bor!**\nUshbu havola **{brand}** brendining nomidan foydalanmoqda, lekin rasmiy domen emas. Ochish tavsiya etilmaydi!"}

    return {"status": "warning", "msg": "⚠️ **Noma'lum havola.**\nBu havola bazada yo'q. Ehtiyot bo'ling, shaxsiy ma'lumotlaringizni kiritmang!"}

@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "👋 Assalomu alaykum!\n\n"
        "Men O‘zbekistondagi banklar, soliq, mobil operatorlar va davlat xizmatlari nomidan keladigan **soxta (phishing) havolalarni aniqlovchi botman**.\n\n"
        "🔍 Shubhali havolani menga yuboring, uni darhol tekshirib beraman!"
    )

@dp.message(F.text)
async def handle_message(message: Message):
    url = extract_url(message.text)
    
    if not url:
        await message.answer("❌ Xabarda havola topilmadi. Iltimos, tekshirish uchun to'g'ri havolani yuboring.")
        return
        
    result = analyze_link(url)
    
    response_text = (
        f"🔗 **Tekshirilgan havola:** `{url}`\n\n"
        f"{result['msg']}\n\n"
        f"📌 *Maslahat:* Shubhali xabarlardagi havolalarga hech qachon parol yoki karta ma'lumotlarini kiritmang!"
    )
    
    await message.answer(response_text, parse_mode="Markdown")

async def main():
    print("Bot ishga tushdi...")
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
