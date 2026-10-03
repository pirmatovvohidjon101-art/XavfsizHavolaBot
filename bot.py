# We'll draft and syntax-check the complete code before creating the user-visible file.
code = r'''
import asyncio
import base64
import hashlib
import html
import io
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import requests
from PIL import Image
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    BotCommand,
    BufferedInputFile,
)
from google import genai
from google.genai import types


# ============================================================
# CONFIG
# ============================================================

TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID", "5081583283"))

# Admin panel credentials. REQUIRED for a public deployment.
ADMIN_WEB_USER = os.getenv("ADMIN_WEB_USER")
ADMIN_WEB_PASSWORD = os.getenv("ADMIN_WEB_PASSWORD")

# Optional VirusTotal API key.
# WARNING: files submitted to the public VT API may be shared with
# the VirusTotal dataset/community. Do not enable this for confidential files.
VIRUSTOTAL_API_KEY = os.getenv("VIRUSTOTAL_API_KEY")

# Keep the Gemini model configurable so it can be changed without editing code.
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# Telegram Bot API currently limits bot downloads; keep a conservative limit.
MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE", str(20 * 1024 * 1024)))

# Rate limiting.
SPAM_INTERVAL = 1.2
user_last_message_time = {}

# Runtime statistics. These are not exposed through a user command.
stats = {
    "checked_count": 0,
    "danger_count": 0,
    "file_checked_count": 0,
    "file_danger_count": 0,
    "voice_checked_count": 0,
    "voice_danger_count": 0,
    "video_checked_count": 0,
    "video_danger_count": 0,
    "screenshot_count": 0,
    "audit_count": 0,
    "url_vt_count": 0,
    "file_vt_count": 0,
}

DB_PATH = "bot_database.db"

if not TOKEN:
    raise ValueError("BOT_TOKEN topilmadi! Render Environment bo'limiga BOT_TOKEN qo'shing.")

if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY topilmadi! Render Environment bo'limiga GEMINI_API_KEY qo'shing.")

if not ADMIN_WEB_USER or not ADMIN_WEB_PASSWORD:
    raise ValueError(
        "ADMIN_WEB_USER va ADMIN_WEB_PASSWORD ham Render Environment'da bo'lishi kerak."
    )

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("cyber-bot")

bot = Bot(token=TOKEN)
dp = Dispatcher()
ai_client = genai.Client(api_key=GEMINI_API_KEY)


# ============================================================
# DATABASE
# ============================================================

def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = db_connect()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            language TEXT DEFAULT 'uz',
            reputation INTEGER DEFAULT 100,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            last_seen DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS whitelist (
            chat_id INTEGER,
            domain TEXT,
            PRIMARY KEY (chat_id, domain)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS blacklist (
            domain TEXT PRIMARY KEY
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS activity_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            action TEXT,
            details TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS file_scans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            filename TEXT,
            sha256 TEXT,
            size INTEGER,
            verdict TEXT,
            details TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()
    conn.close()


init_db()


def log_activity(user_id, action, details):
    try:
        conn = db_connect()
        conn.execute(
            "INSERT INTO activity_logs (user_id, action, details) VALUES (?, ?, ?)",
            (user_id, action, str(details)[:4000]),
        )
        conn.commit()
        conn.close()
    except Exception:
        logger.exception("log_activity failed")


def add_user(user_id, username, full_name):
    safe_username = str(username)[:50] if username else ""
    safe_fullname = str(full_name)[:100] if full_name else "Foydalanuvchi"

    conn = db_connect()
    conn.execute("""
        INSERT INTO users (
            user_id, username, full_name, language, reputation, last_seen
        )
        VALUES (?, ?, ?, 'uz', 100, CURRENT_TIMESTAMP)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            full_name=excluded.full_name,
            last_seen=CURRENT_TIMESTAMP
    """, (user_id, safe_username, safe_fullname))
    conn.commit()
    conn.close()


def add_global_blacklist(domain):
    domain = normalize_domain(domain)
    if not domain:
        return
    conn = db_connect()
    conn.execute(
        "INSERT OR IGNORE INTO blacklist (domain) VALUES (?)",
        (domain.lower(),),
    )
    conn.commit()
    conn.close()


def remove_global_blacklist(domain):
    domain = normalize_domain(domain)
    conn = db_connect()
    conn.execute("DELETE FROM blacklist WHERE domain = ?", (domain.lower(),))
    conn.commit()
    conn.close()


def is_globally_blacklisted(domain):
    domain = normalize_domain(domain)
    conn = db_connect()
    row = conn.execute(
        "SELECT 1 FROM blacklist WHERE domain = ?",
        (domain.lower(),),
    ).fetchone()
    conn.close()
    return row is not None


# ============================================================
# TEXTS / MENUS
# ============================================================

TEXTS = {
    "start": (
        "👋 Assalomu alaykum!\n\n"
        "🛡 Men AI yordamidagi kiber-xavfsizlik botiman.\n\n"
        "Men siz yuborgan:\n"
        "• 🔗 havolalarni\n"
        "• 📄 fayllarni\n"
        "• 📦 APK va arxivlarni\n"
        "• 🎙 ovozli xabarlarni\n"
        "• 🎥 videolarni\n"
        "• 📸 skrinshotlarni\n"
        "tekshirishga yordam beraman.\n\n"
        "⚠️ Muhim: hech qanday avtomatik skaner 100% kafolat bermaydi. "
        "Shubhali fayl yoki havolani ochmaslik eng xavfsiz yo'ldir.\n\n"
        "Batafsil ma'lumot: /help"
    ),
    "help": (
        "ℹ️ <b>KIBER-XAVFSIZLIK BOTI — QO'LLANMA</b>\n\n"
        "🔗 <b>Havola:</b>\n"
        "Istalgan havolani shu chatga yuboring. Bot domen, qora ro'yxat, "
        "sahifa ko'rinishi va AI tahlili orqali xavfni baholaydi.\n\n"
        "📦 <b>APK / EXE / MSI va boshqa fayllar:</b>\n"
        "Faylni Telegram orqali yuboring. Bot fayl hajmi, kengaytmasi, "
        "fayl imzosi, SHA-256, arxiv tarkibi va mavjud bo'lsa VirusTotal "
        "natijasini tekshiradi.\n\n"
        "🎙 <b>Ovozli xabar:</b>\n"
        "Vishing va ovoz klonlashga xos belgilarni AI yordamida tahlil qiladi.\n\n"
        "🎥 <b>Video:</b>\n"
        "Video yuborilganda hajmi va formati tekshiriladi; mavjud AI "
        "tahlil qatlamlari ishlatiladi.\n\n"
        "📸 <b>Skrinshot:</b>\n"
        "Shubhali login, to'lov yoki phishing sahifalari bo'yicha tahlil qilish mumkin.\n\n"
        "🕵️ <b>/audit username yoki havola</b>\n"
        "Berilgan obyekt bo'yicha AI asosidagi OSINT/kiber-audit.\n\n"
        "🛡 <b>Himoya tavsiyalari:</b>\n"
        "• SMS/Telegramdagi shoshilinch linklarni ochmang.\n"
        "• Bank/parol kodlarini hech kimga bermang.\n"
        "• Noma'lum APK/EXE fayllarni o'rnatmang.\n"
        "• Fayl xavfsiz deb topilsa ham, manbasini tekshiring.\n"
        "• 2FA/passkey ishlating.\n\n"
        "⚠️ Bot natijasi xavf bahosi, antivirus kafolati emas."
    ),
}


OFFICIAL_DOMAINS = {
    "gov.uz", "my.gov.uz", "pm.gov.uz", "lex.uz", "cbu.uz",
    "stat.uz", "customs.uz", "soliq.uz", "my.soliq.uz",
    "uzgidromet.uz", "mehnat.uz", "my.mehnat.uz", "iiv.uz",
    "mfa.uz", "minjust.uz", "uzedu.uz", "ssv.uz", "tiiame.uz",
    "muslim.uz", "fatvo.uz", "quran.uz", "ziyouz.uz", "buxari.uz",
    "hilolnashr.uz", "nbu.uz", "agrobank.uz", "kapitalbank.uz",
    "ipotekabank.uz", "davrbank.uz", "orientfinanzbank.uz",
    "hamkorbank.uz", "asakabank.uz", "anorbank.uz", "tbcbank.uz",
    "octobank.uz", "infinbank.uz", "ipakyulibank.uz", "aloqabank.uz",
    "uzcard.uz", "humocard.uz", "click.uz", "payme.uz", "uzum.uz",
    "uzummarket.uz", "uzumbank.uz", "paynet.uz", "humans.uz",
    "kun.uz", "gazeta.uz", "daryo.uz", "uzreport.news", "upl.uz",
    "sof.uz", "qalampir.uz", "zamin.uz", "xabar.uz", "yuz.uz",
    "uza.uz", "terabayt.uz", "texnomart.uz", "asaxiy.uz", "olcha.uz",
    "express24.uz", "zoodmall.uz", "beeline.uz", "ucell.uz",
    "mobi.uz", "uztelecom.uz", "uzmobile.uz",
}


# ============================================================
# GENERAL HELPERS
# ============================================================

DANGEROUS_EXTENSIONS = {
    ".apk", ".aab", ".exe", ".msi", ".msp", ".com", ".scr", ".pif",
    ".bat", ".cmd", ".ps1", ".vbs", ".vbe", ".js", ".jse", ".wsf",
    ".wsh", ".hta", ".jar", ".dll", ".sys", ".iso", ".img",
    ".lnk", ".reg", ".chm",
    ".docm", ".xlsm", ".pptm", ".xlam", ".xltm",
}

ARCHIVE_EXTENSIONS = {
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz",
}

DOUBLE_EXTENSION_RE = re.compile(
    r"\.(pdf|docx|xlsx|jpg|jpeg|png|txt|mp3|mp4)\.(exe|scr|bat|cmd|apk|js|vbs)$",
    re.I,
)


def normalize_domain(domain):
    domain = (domain or "").strip().lower()
    domain = re.sub(r"^https?://", "", domain)
    domain = domain.split("/")[0].split(":")[0].strip(".")
    if domain.startswith("www."):
        domain = domain[4:]
    return domain


def is_rate_limited(user_id):
    now = time.monotonic()
    previous = user_last_message_time.get(user_id, 0)
    if now - previous < SPAM_INTERVAL:
        return True
    user_last_message_time[user_id] = now
    return False


def extract_url(text):
    if not text:
        return None

    pattern = re.compile(
        r"(?i)(https?://[^\s<>]+|www\.[^\s<>]+|(?:t\.me|telegram\.me)/[^\s<>]+)"
    )
    match = pattern.search(text)
    if match:
        return match.group(1).rstrip(".,;:!?)]}")

    for word in text.split():
        clean = word.strip(".,;:!?()[]{}\"'")
        if clean.startswith("@") and len(clean) > 1:
            return f"https://t.me/{clean[1:]}"
    return None


def safe_filename(filename):
    filename = os.path.basename(filename or "unknown_file")
    filename = re.sub(r"[^A-Za-z0-9._()\- ]+", "_", filename)
    return filename[:200] or "unknown_file"


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def file_extension(filename):
    return os.path.splitext(filename.lower())[1]


def detect_file_signature(data):
    if data.startswith(b"MZ"):
        return "PE/Windows executable (MZ)"
    if data.startswith(b"\x7fELF"):
        return "ELF executable"
    if data.startswith(b"%PDF-"):
        return "PDF"
    if data.startswith(b"PK\x03\x04"):
        return "ZIP/OOXML/APK/JAR-like archive"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG image"
    if data.startswith(b"\xff\xd8\xff"):
        return "JPEG image"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "WEBP image"
    if data.startswith(b"ID3"):
        return "MP3 audio"
    return "Unknown"


def suspicious_archive_members(data):
    findings = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = z.namelist()
            lower_names = [n.lower() for n in names]

            for name in names:
                lower = name.lower()
                if lower.endswith(tuple(DANGEROUS_EXTENSIONS)):
                    findings.append(f"Arxiv ichida xavfli turdagi fayl: {name}")

            if "androidmanifest.xml" in lower_names and any(
                n.endswith("classes.dex") for n in lower_names
            ):
                findings.append("APK/Android ilovasi tuzilmasi aniqlandi.")

            if "vbaProject.bin".lower() in lower_names:
                findings.append("Office makrosiga o'xshash VBA obyekt aniqlandi.")

            if len(names) > 10000:
                findings.append("Arxiv ichida juda ko'p obyekt mavjud.")

            total_uncompressed = 0
            for info in z.infolist():
                total_uncompressed += info.file_size
                if info.file_size > 100 * 1024 * 1024:
                    findings.append(
                        f"Arxiv ichida juda katta fayl: {info.filename}"
                    )

            compressed = max(len(data), 1)
            if total_uncompressed / compressed > 100:
                findings.append(
                    "Juda yuqori siqilish nisbati: archive-bomb ehtimoli mavjud."
                )

    except zipfile.BadZipFile:
        pass
    except Exception as exc:
        findings.append(f"Arxiv tarkibini tekshirishda xato: {type(exc).__name__}")

    return findings[:20]


# ============================================================
# VIRUSTOTAL (OPTIONAL)
# ============================================================

def vt_headers():
    return {"x-apikey": VIRUSTOTAL_API_KEY}


def vt_get_file_report(sha256):
    if not VIRUSTOTAL_API_KEY:
        return None

    try:
        response = requests.get(
            f"https://www.virustotal.com/api/v3/files/{sha256}",
            headers=vt_headers(),
            timeout=12,
        )

        if response.status_code == 404:
            return None

        response.raise_for_status()
        data = response.json().get("data", {})
        attributes = data.get("attributes", {})
        stats_data = attributes.get("last_analysis_stats", {})

        return {
            "found": True,
            "malicious": int(stats_data.get("malicious", 0)),
            "suspicious": int(stats_data.get("suspicious", 0)),
            "undetected": int(stats_data.get("undetected", 0)),
            "harmless": int(stats_data.get("harmless", 0)),
            "reputation": attributes.get("reputation"),
        }

    except Exception:
        logger.exception("VirusTotal file report failed")
        return None


def vt_scan_file(data, filename):
    """
    Optional public VirusTotal upload.

    IMPORTANT:
    Public VT submissions can enter the VirusTotal dataset.
    Do not enable VIRUSTOTAL_API_KEY if users may send confidential files.
    """
    if not VIRUSTOTAL_API_KEY or len(data) > 32 * 1024 * 1024:
        return None

    try:
        response = requests.post(
            "https://www.virustotal.com/api/v3/files",
            headers=vt_headers(),
            files={"file": (filename, data)},
            timeout=30,
        )
        response.raise_for_status()
        analysis_id = response.json()["data"]["id"]

        for _ in range(8):
            time.sleep(3)
            analysis = requests.get(
                f"https://www.virustotal.com/api/v3/analyses/{analysis_id}",
                headers=vt_headers(),
                timeout=12,
            )
            if analysis.status_code != 200:
                break

            attributes = analysis.json().get("data", {}).get("attributes", {})
            if attributes.get("status") == "completed":
                return attributes.get("stats", {})

        return {"status": "pending"}

    except Exception:
        logger.exception("VirusTotal file upload failed")
        return None


# ============================================================
# GEMINI HELPERS
# ============================================================

def gemini_text(prompt):
    try:
        response = ai_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
        )
        return response.text or ""
    except Exception:
        logger.exception("Gemini text request failed")
        return ""


def gemini_analyze_bytes(prompt, data, mime_type):
    try:
        part = types.Part.from_bytes(data=data, mime_type=mime_type)
        response = ai_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[prompt, part],
        )
        return response.text or ""
    except Exception:
        logger.exception("Gemini binary analysis failed")
        return ""


def mime_supported_for_gemini(mime):
    if not mime:
        return False

    allowed_prefixes = (
        "image/",
        "audio/",
        "video/",
        "text/",
    )
    allowed_exact = {
        "application/pdf",
        "application/json",
        "application/xml",
    }

    return mime.startswith(allowed_prefixes) or mime in allowed_exact


# ============================================================
# FILE ANALYSIS
# ============================================================

def analyze_file_static(filename, data, mime_type=""):
    ext = file_extension(filename)
    signature = detect_file_signature(data)
    sha256 = sha256_bytes(data)

    findings = []
    danger = False

    if ext in DANGEROUS_EXTENSIONS:
        danger = True
        findings.append(f"Xavfli/yuqori-risk fayl kengaytmasi: {ext}")

    if DOUBLE_EXTENSION_RE.search(filename):
        danger = True
        findings.append("Ikki tomonlama fayl kengaytmasi aniqlangan.")

    if ext == ".apk":
        if not data.startswith(b"PK"):
            danger = True
            findings.append("APK kengaytmasi bor, lekin ZIP/APK imzosi mos kelmadi.")
        else:
            findings.append("APK fayli aniqlandi.")
            findings.extend(suspicious_archive_members(data))

    if ext in ARCHIVE_EXTENSIONS or data.startswith(b"PK"):
        archive_findings = suspicious_archive_members(data)
        findings.extend(archive_findings)
        if archive_findings:
            danger = True

    if signature in {"PE/Windows executable (MZ)", "ELF executable"}:
        danger = True
        findings.append(f"Ijro etiluvchi fayl imzosi aniqlandi: {signature}")

    if ext in {".docm", ".xlsm", ".pptm", ".xlam", ".xltm"}:
        danger = True
        findings.append("Makro qo'llab-quvvatlaydigan Office fayli.")

    if len(data) == 0:
        danger = True
        findings.append("Fayl bo'sh.")

    return {
        "sha256": sha256,
        "signature": signature,
        "extension": ext or "yo'q",
        "mime_type": mime_type or "noma'lum",
        "findings": list(dict.fromkeys(findings)),
        "danger": danger,
    }


def build_file_verdict(static_result, vt_result=None, ai_result=""):
    danger = bool(static_result["danger"])
    strong_danger = False

    if vt_result:
        malicious = int(vt_result.get("malicious", 0) or 0)
        suspicious = int(vt_result.get("suspicious", 0) or 0)

        if malicious > 0:
            danger = True
            strong_danger = True
        elif suspicious > 0:
            danger = True

    if ai_result:
        upper = ai_result.upper()
        if "MALICIOUS" in upper or "DANGEROUS" in upper or "PHISHING" in upper:
            danger = True

    if strong_danger:
        verdict = "DANGER"
    elif danger:
        verdict = "SUSPICIOUS"
    else:
        verdict = "NO_OBVIOUS_THREAT"

    return verdict


def save_file_scan(user_id, filename, result, verdict, details):
    conn = db_connect()
    conn.execute(
        """
        INSERT INTO file_scans
        (user_id, filename, sha256, size, verdict, details)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            filename,
            result["sha256"],
            details.get("size", 0),
            verdict,
            json.dumps(details, ensure_ascii=False)[:8000],
        ),
    )
    conn.commit()
    conn.close()


# ============================================================
# URL / SCREENSHOT
# ============================================================

def get_webpage_screenshot(url):
    try:
        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        api_url = "https://api.microlink.io/"
        response = requests.get(
            api_url,
            params={
                "url": url,
                "screenshot": "true",
                "meta": "false",
                "embed": "screenshot.url",
            },
            timeout=10,
        )
        response.raise_for_status()

        result = response.json()
        screenshot_url = (
            result.get("data", {})
            .get("screenshot", {})
            .get("url")
        )

        if not screenshot_url:
            return None

        image_response = requests.get(screenshot_url, timeout=10)
        image_response.raise_for_status()
        return image_response.content

    except Exception:
        logger.exception("Screenshot failed")
        return None


def vt_url_report(url):
    """
    Optional URL reputation lookup through VT.
    The public API has strict rate limits.
    """
    if not VIRUSTOTAL_API_KEY:
        return None

    try:
        url_id = base64.urlsafe_b64encode(url.encode()).decode().strip("=")
        response = requests.get(
            f"https://www.virustotal.com/api/v3/urls/{url_id}",
            headers=vt_headers(),
            timeout=12,
        )

        if response.status_code != 200:
            return None

        attrs = response.json().get("data", {}).get("attributes", {})
        stats_data = attrs.get("last_analysis_stats", {})
        return {
            "malicious": int(stats_data.get("malicious", 0)),
            "suspicious": int(stats_data.get("suspicious", 0)),
            "harmless": int(stats_data.get("harmless", 0)),
            "undetected": int(stats_data.get("undetected", 0)),
        }

    except Exception:
        logger.exception("VirusTotal URL report failed")
        return None


# ============================================================
# ADMIN AUTH
# ============================================================

def check_basic_auth(handler):
    auth = handler.headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        return False

    try:
        raw = base64.b64decode(auth[6:]).decode("utf-8")
        username, password = raw.split(":", 1)
        return secrets.compare_digest(username, ADMIN_WEB_USER) and secrets.compare_digest(
            password, ADMIN_WEB_PASSWORD
        )
    except Exception:
        return False


def send_auth_required(handler):
    handler.send_response(401)
    handler.send_header("WWW-Authenticate", 'Basic realm="Cyber Bot Admin"')
    handler.send_header("Content-Type", "text/plain; charset=utf-8")
    handler.end_headers()
    handler.wfile.write("Admin authentication required.".encode("utf-8"))


def html_escape(value):
    return html.escape(str(value or ""), quote=True)


def admin_dashboard_html():
    conn = db_connect()

    users = conn.execute(
        """
        SELECT user_id, username, full_name, reputation, last_seen
        FROM users
        ORDER BY last_seen DESC
        LIMIT 200
        """
    ).fetchall()

    blacklist = conn.execute(
        "SELECT domain FROM blacklist ORDER BY domain"
    ).fetchall()

    logs = conn.execute(
        """
        SELECT user_id, action, details, timestamp
        FROM activity_logs
        ORDER BY id DESC
        LIMIT 100
        """
    ).fetchall()

    file_scans = conn.execute(
        """
        SELECT user_id, filename, sha256, size, verdict, timestamp
        FROM file_scans
        ORDER BY id DESC
        LIMIT 100
        """
    ).fetchall()

    user_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    danger_logs = conn.execute(
        """
        SELECT COUNT(*) FROM activity_logs
        WHERE action LIKE '%DANGER%'
           OR action LIKE '%PHISHING%'
           OR action LIKE '%BLACKLIST%'
        """
    ).fetchone()[0]

    conn.close()

    def rows(items):
        return "".join(
            "<tr>" + "".join(f"<td>{html_escape(x)}</td>" for x in row) + "</tr>"
            for row in items
        )

    users_rows = rows(users)
    logs_rows = rows(logs)
    scans_rows = rows(file_scans)
    blacklist_items = "".join(
        f"""
        <li>
            <code>{html_escape(x[0])}</code>
            <form class="inline" method="post" action="/remove_blacklist">
                <input type="hidden" name="domain" value="{html_escape(x[0])}">
                <button class="danger" type="submit">Olib tashlash</button>
            </form>
        </li>
        """
        for x in blacklist
    )

    return f"""<!doctype html>
<html lang="uz">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cyber Security Bot — Admin</title>
<style>
body {{
    font-family: Inter, Arial, sans-serif;
    margin:0;
    background:#f4f7fb;
    color:#172033;
}}
header {{
    background:#111827;
    color:white;
    padding:24px;
}}
.container {{
    max-width:1400px;
    margin:auto;
    padding:20px;
}}
.grid {{
    display:grid;
    grid-template-columns:repeat(auto-fit,minmax(220px,1fr));
    gap:14px;
}}
.card {{
    background:white;
    border-radius:14px;
    padding:18px;
    margin-bottom:18px;
    box-shadow:0 3px 15px rgba(0,0,0,.07);
}}
.metric {{
    font-size:30px;
    font-weight:800;
}}
.small {{
    color:#667085;
    font-size:13px;
}}
table {{
    width:100%;
    border-collapse:collapse;
    font-size:13px;
}}
th,td {{
    padding:9px;
    border-bottom:1px solid #e5e7eb;
    text-align:left;
    vertical-align:top;
}}
th {{
    background:#f8fafc;
}}
input,textarea {{
    width:100%;
    box-sizing:border-box;
    padding:10px;
    margin:7px 0 12px;
    border:1px solid #d0d5dd;
    border-radius:8px;
}}
button {{
    border:0;
    border-radius:8px;
    padding:9px 13px;
    background:#2563eb;
    color:white;
    cursor:pointer;
}}
button.danger {{
    background:#dc2626;
}}
.inline {{
    display:inline;
}}
ul {{
    line-height:2;
}}
.badge {{
    display:inline-block;
    padding:3px 8px;
    border-radius:999px;
    background:#eef2ff;
}}
.scroll {{
    overflow:auto;
    max-height:500px;
}}
</style>
</head>
<body>
<header>
    <div class="container">
        <h1>🛡 Cyber Security Bot — Admin Panel</h1>
        <div class="small" style="color:#cbd5e1">
            Faqat administrator uchun. Foydalanuvchi menyusida bu panel ko'rsatilmaydi.
        </div>
    </div>
</header>

<main class="container">

<div class="grid">
    <div class="card"><div class="metric">{stats["checked_count"]}</div>URL tekshiruvlari</div>
    <div class="card"><div class="metric">{stats["danger_count"]}</div>Xavfli URL</div>
    <div class="card"><div class="metric">{stats["file_checked_count"]}</div>Fayl tekshiruvlari</div>
    <div class="card"><div class="metric">{stats["file_danger_count"]}</div>Xavfli/shubhali fayllar</div>
    <div class="card"><div class="metric">{stats["voice_checked_count"]}</div>Ovoz tekshiruvlari</div>
    <div class="card"><div class="metric">{stats["audit_count"]}</div>Auditlar</div>
    <div class="card"><div class="metric">{user_count}</div>Foydalanuvchilar</div>
    <div class="card"><div class="metric">{danger_logs}</div>Xavf loglari</div>
</div>

<div class="card">
<h2>📢 Broadcast</h2>
<form method="post" action="/broadcast">
<textarea name="message" rows="4" placeholder="Barcha foydalanuvchilarga yuboriladigan xabar..." required></textarea>
<button type="submit">Xabar yuborish</button>
</form>
</div>

<div class="card">
<h2>🚫 Global blacklist</h2>
<form method="post" action="/add_blacklist">
<input name="domain" placeholder="example.com" required>
<button type="submit">Domen qo'shish</button>
</form>
<ul>{blacklist_items}</ul>
</div>

<div class="card">
<h2>📄 Fayl skanlari</h2>
<div class="scroll">
<table>
<tr><th>User</th><th>Fayl</th><th>SHA-256</th><th>Hajm</th><th>Verdict</th><th>Vaqt</th></tr>
{scans_rows}
</table>
</div>
</div>

<div class="card">
<h2>👥 Foydalanuvchilar</h2>
<div class="scroll">
<table>
<tr><th>ID</th><th>Username</th><th>Ism</th><th>Reputation</th><th>Oxirgi faollik</th></tr>
{users_rows}
</table>
</div>
</div>

<div class="card">
<h2>📜 Activity logs</h2>
<div class="scroll">
<table>
<tr><th>User</th><th>Action</th><th>Details</th><th>Vaqt</th></tr>
{logs_rows}
</table>
</div>
</div>

</main>
</body>
</html>"""


class WebPanelHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        return

    def do_GET(self):
        if not check_basic_auth(self):
            send_auth_required(self)
            return

        path = urlparse(self.path).path

        if path in {"/", "/health"}:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Cyber Security Bot is running.")
            return

        if path == "/admin":
            page = admin_dashboard_html()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(page.encode("utf-8"))
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        if not check_basic_auth(self):
            send_auth_required(self)
            return

        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", "0"))

        if length > 100_000:
            self.send_response(413)
            self.end_headers()
            return

        body = self.rfile.read(length).decode("utf-8", errors="replace")
        params = parse_qs(body)

        if path == "/add_blacklist":
            domain = params.get("domain", [""])[0].strip()
            if domain:
                domain = normalize_domain(domain)
                add_global_blacklist(domain)
                log_activity(ADMIN_ID, "BLACKLIST_ADD", domain)

        elif path == "/remove_blacklist":
            domain = params.get("domain", [""])[0].strip()
            if domain:
                domain = normalize_domain(domain)
                remove_global_blacklist(domain)
                log_activity(ADMIN_ID, "BLACKLIST_REMOVE", domain)

        elif path == "/broadcast":
            message_text = params.get("message", [""])[0].strip()
            if message_text:
                log_activity(
                    ADMIN_ID,
                    "BROADCAST",
                    f"Broadcast yuborildi: {message_text[:100]}",
                )
                threading.Thread(
                    target=run_broadcast,
                    args=(message_text,),
                    daemon=True,
                ).start()

        else:
            self.send_response(404)
            self.end_headers()
            return

        self.send_response(303)
        self.send_header("Location", "/admin")
        self.end_headers()


def run_broadcast(text):
    try:
        conn = db_connect()
        users = conn.execute("SELECT user_id FROM users").fetchall()
        conn.close()

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def send_all():
            for (user_id,) in users:
                try:
                    await bot.send_message(
                        user_id,
                        f"📢 <b>Admin e'loni:</b>\n\n{html.escape(text)}",
                        parse_mode="HTML",
                    )
                    await asyncio.sleep(0.07)
                except Exception:
                    pass

        loop.run_until_complete(send_all())
        loop.close()

    except Exception:
        logger.exception("Broadcast failed")


def run_http_server():
    port = int(os.environ.get("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), WebPanelHandler)
    logger.info("Admin web panel started on port %s", port)
    server.serve_forever()


# ============================================================
# COMMANDS
# ============================================================

async def set_default_commands(bot_instance: Bot):
    # /stats, /top and /web are deliberately NOT listed.
    commands = [
        BotCommand(command="start", description="🚀 Botni ishga tushirish"),
        BotCommand(command="audit", description="🕵️ Kiber-audit"),
        BotCommand(command="help", description="ℹ️ Qo'llanma"),
    ]
    await bot_instance.set_my_commands(commands)


@dp.message(Command("start"))
async def cmd_start(message: Message):
    add_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.full_name,
    )
    log_activity(message.from_user.id, "START", "Bot ishga tushirildi")
    await message.answer(TEXTS["start"])


@dp.message(Command("help"))
async def cmd_help(message: Message):
    add_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.full_name,
    )
    await message.answer(TEXTS["help"], parse_mode="HTML")


@dp.message(Command("audit"))
async def cmd_audit(message: Message):
    add_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.full_name,
    )

    if is_rate_limited(message.from_user.id):
        await message.answer("⚠️ Juda tez-tez so'rov yuboryapsiz. Biroz kuting.")
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer(
            "❌ Foydalanish: <code>/audit @username</code> yoki "
            "<code>/audit https://example.com</code>",
            parse_mode="HTML",
        )
        return

    target = args[1].strip()
    stats["audit_count"] += 1
    log_activity(message.from_user.id, "AUDIT", target[:500])

    await message.answer(
        f"🕵️ <b>Avtonom kiber-audit</b> boshlandi:\n"
        f"<code>{html.escape(target)}</code>",
        parse_mode="HTML",
    )

    prompt = f"""
You are a cybersecurity analyst.
Perform a defensive OSINT/threat assessment of this target:
{target}

Return the report in Uzbek.
Do not claim access to private accounts or systems.
Clearly separate:
1. Observable/public indicators
2. Potential risks
3. What cannot be verified
4. Safe recommendations
"""
    report = gemini_text(prompt)

    if not report:
        report = "❌ AI audit vaqtida texnik xatolik yuz berdi."

    await message.answer(
        "🛡 <b>KIBER-AUDIT HISOBOTI</b>\n\n" + html.escape(report),
        parse_mode="HTML",
    )


# ============================================================
# URL HANDLER
# ============================================================

@dp.message(F.text)
async def handle_text(message: Message):
    add_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.full_name,
    )

    if is_rate_limited(message.from_user.id):
        await message.answer("⚠️ Juda tez-tez xabar yuboryapsiz. Iltimos, biroz kuting.")
        return

    url = extract_url(message.text)
    if not url:
        return

    stats["checked_count"] += 1

    parsed = urlparse(
        url if url.startswith(("http://", "https://")) else "https://" + url
    )
    domain = normalize_domain(parsed.netloc)

    if not domain:
        await message.answer("❌ Havola domenini aniqlab bo'lmadi.")
        return

    if is_globally_blacklisted(domain):
        stats["danger_count"] += 1
        log_activity(message.from_user.id, "BLACKLIST_HIT", domain)
        await message.answer(
            "🚨 <b>DIQQAT!</b>\n\n"
            "Ushbu domen administratorning global qora ro'yxatida.",
            parse_mode="HTML",
        )
        return

    # Official-domain status is only a positive signal, never a guarantee.
    official = domain in OFFICIAL_DOMAINS or domain.endswith(".gov.uz")

    vt_result = None
    if VIRUSTOTAL_API_KEY:
        vt_result = vt_url_report(url)
        if vt_result:
            stats["url_vt_count"] += 1

    screenshot_bytes = get_webpage_screenshot(url)

    if screenshot_bytes:
        stats["screenshot_count"] += 1

    ai_result = ""
    if screenshot_bytes:
        try:
            image = Image.open(io.BytesIO(screenshot_bytes))
            image.load()

            response = ai_client.models.generate_content(
                model=GEMINI_MODEL,
                contents=[
                    (
                        "Analyze this webpage screenshot defensively for phishing/scam "
                        "indicators. Return Uzbek. Start with exactly one of: "
                        "'PHISHING', 'SUSPICIOUS', or 'SAFE'. Mention uncertainty."
                    ),
                    image,
                ],
            )
            ai_result = response.text or ""
        except Exception:
            logger.exception("Webpage image analysis failed")

    malicious = int((vt_result or {}).get("malicious", 0))
    suspicious = int((vt_result or {}).get("suspicious", 0))

    danger = (
        malicious > 0
        or suspicious > 0
        or "PHISHING" in ai_result.upper()
        or "SCAM" in ai_result.upper()
    )

    if danger:
        stats["danger_count"] += 1
        log_activity(
            message.from_user.id,
            "PHISHING_DETECTED",
            f"{domain} | VT={vt_result} | AI={ai_result[:500]}",
        )

        caption = (
            "🚨 <b>DIQQAT! Shubhali yoki phishing belgilar aniqlandi.</b>\n\n"
            f"🌐 <code>{html.escape(url)}</code>\n"
        )

        if vt_result:
            caption += (
                f"\nVirusTotal: malicious={malicious}, "
                f"suspicious={suspicious}"
            )

        if ai_result:
            caption += f"\n\nAI tahlili:\n{html.escape(ai_result[:2500])}"

        if screenshot_bytes:
            await message.answer_photo(
                photo=BufferedInputFile(screenshot_bytes, filename="website.jpg"),
                caption=caption,
                parse_mode="HTML",
            )
        else:
            await message.answer(caption, parse_mode="HTML")
        return

    if official:
        await message.answer(
            "🔗 <b>Rasmiy domen sifatida ro'yxatda.</b>\n\n"
            "Bu faqat domen bo'yicha ijobiy signal; sahifa yoki havolaning "
            "har bir elementi avtomatik ravishda xavfsiz degani emas.",
            parse_mode="HTML",
        )
        return

    response_text = (
        "🟢 <b>Hozircha aniq xavf belgisi topilmadi.</b>\n\n"
        "Bu natija 100% xavfsizlik kafolati emas."
    )

    if vt_result:
        response_text += (
            f"\nVirusTotal: malicious={malicious}, "
            f"suspicious={suspicious}"
        )

    if ai_result:
        response_text += f"\n\nAI tahlili:\n{html.escape(ai_result[:2500])}"

    await message.answer(response_text, parse_mode="HTML")


# ============================================================
# FILE HANDLER
# ============================================================

@dp.message(F.document)
async def handle_document(message: Message):
    user_id = message.from_user.id
    add_user(
        user_id,
        message.from_user.username,
        message.from_user.full_name,
    )

    if is_rate_limited(user_id):
        await message.answer("⚠️ Juda tez-tez fayl yuboryapsiz. Biroz kuting.")
        return

    document = message.document
    filename = safe_filename(document.file_name or "unknown_file")
    size = document.file_size or 0
    mime = document.mime_type or ""

    stats["file_checked_count"] += 1

    if size > MAX_FILE_SIZE:
        await message.answer(
            f"⚠️ Fayl juda katta.\n\n"
            f"Maksimal hajm: {MAX_FILE_SIZE // (1024 * 1024)} MB."
        )
        log_activity(
            user_id,
            "FILE_TOO_LARGE",
            f"{filename} | {size} bytes",
        )
        return

    await message.answer(
        f"🔍 <b>Fayl tekshirilmoqda...</b>\n\n"
        f"📄 {html.escape(filename)}\n"
        f"📦 {size / 1024 / 1024:.2f} MB",
        parse_mode="HTML",
    )

    try:
        file_obj = await bot.get_file(document.file_id)
        file_data = await bot.download_file(file_obj.file_path)
        data = file_data.read()
    except Exception:
        logger.exception("Telegram file download failed")
        await message.answer("❌ Faylni yuklab olishda xatolik yuz berdi.")
        return

    static_result = analyze_file_static(filename, data, mime)

    vt_result = vt_get_file_report(static_result["sha256"])
    if vt_result:
        stats["file_vt_count"] += 1
    else:
        # Optional upload. Disabled unless VIRUSTOTAL_API_KEY is configured.
        vt_scan = vt_scan_file(data, filename)
        if vt_scan:
            stats["file_vt_count"] += 1
            vt_result = {
                "malicious": int(vt_scan.get("malicious", 0) or 0),
                "suspicious": int(vt_scan.get("suspicious", 0) or 0),
            }

    ai_result = ""
    # Do not send executable/APK/binary files to Gemini merely because
    # their extension says so. AI analysis is only used for supported media/docs.
    if mime_supported_for_gemini(mime) and not static_result["danger"]:
        ai_result = gemini_analyze_bytes(
            """
Analyze this user-submitted file for cybersecurity risk.
This is a defensive analysis, not malware certification.
Return Uzbek and clearly state uncertainty.
Look for phishing/social-engineering content, malicious instructions,
suspicious links, credential theft indicators, or dangerous embedded content.
""",
            data,
            mime,
        )

    verdict = build_file_verdict(static_result, vt_result, ai_result)

    if verdict != "NO_OBVIOUS_THREAT":
        stats["file_danger_count"] += 1

    details = {
        "size": size,
        "mime": mime,
        "signature": static_result["signature"],
        "extension": static_result["extension"],
        "findings": static_result["findings"],
        "vt": vt_result,
        "ai": ai_result[:2500],
    }

    save_file_scan(user_id, filename, static_result, verdict, details)

    if verdict == "DANGER":
        log_activity(
            user_id,
            "FILE_DANGER",
            f"{filename} | {static_result['sha256']}",
        )
        title = "🚨 <b>XAVFLI FAYL BO'LISHI MUMKIN!</b>"
    elif verdict == "SUSPICIOUS":
        log_activity(
            user_id,
            "FILE_SUSPICIOUS",
            f"{filename} | {static_result['sha256']}",
        )
        title = "⚠️ <b>SHUBHALI FAYL</b>"
    else:
        title = "✅ <b>ANIQ XAVF BELGISI TOPILMADI</b>"

    lines = [
        title,
        "",
        f"📄 Fayl: <code>{html.escape(filename)}</code>",
        f"🔐 SHA-256: <code>{static_result['sha256']}</code>",
        f"🧩 Signature: {html.escape(static_result['signature'])}",
        f"📦 MIME: <code>{html.escape(mime or 'nomaʼlum')}</code>",
    ]

    if static_result["findings"]:
        lines.append("\n🔎 <b>Topilmalar:</b>")
        for finding in static_result["findings"][:10]:
            lines.append(f"• {html.escape(finding)}")

    if vt_result:
        lines.append(
            "\n🦠 <b>VirusTotal:</b> "
            f"malicious={vt_result.get('malicious', 0)}, "
            f"suspicious={vt_result.get('suspicious', 0)}"
        )

    if ai_result:
        lines.append(
            "\n🤖 <b>AI:</b>\n" + html.escape(ai_result[:1800])
        )

    lines.append(
        "\n⚠️ <b>Eslatma:</b> bu avtomatik risk bahosi. "
        "Noma'lum APK/EXE faylni o'rnatmang yoki ishga tushirmang."
    )

    await message.answer("\n".join(lines), parse_mode="HTML")


# ============================================================
# VOICE HANDLER
# ============================================================

@dp.message(F.voice)
async def handle_voice(message: Message):
    user_id = message.from_user.id
    add_user(
        user_id,
        message.from_user.username,
        message.from_user.full_name,
    )

    if is_rate_limited(user_id):
        await message.answer("⚠️ Juda tez-tez xabar yuboryapsiz. Biroz kuting.")
        return

    stats["voice_checked_count"] += 1

    try:
        file_obj = await bot.get_file(message.voice.file_id)
        file_bytes = await bot.download_file(file_obj.file_path)
        audio_data = file_bytes.read()

        result = gemini_analyze_bytes(
            """
Analyze this audio defensively for possible vishing, impersonation,
voice-cloning indicators, social engineering, requests for OTP/password,
urgent payment requests, or other scam indicators.

Answer in Uzbek.
Start with exactly one of: DANGER, SUSPICIOUS, SAFE.
Do not claim that audio alone can prove a voice was cloned.
""",
            audio_data,
            "audio/ogg",
        )

        if "DANGER" in result.upper():
            stats["voice_danger_count"] += 1
            log_activity(
                user_id,
                "VOICE_DANGER",
                result[:1000],
            )

            await message.answer(
                "🚨 <b>Ovozli xabarda xavfli/vishing alomatlari bo'lishi mumkin.</b>\n\n"
                + html.escape(result[:2500]),
                parse_mode="HTML",
            )
        else:
            await message.answer(
                "🎙 <b>Ovoz tahlili:</b>\n\n"
                + html.escape(result[:2500]),
                parse_mode="HTML",
            )

    except Exception:
        logger.exception("Voice analysis failed")
        await message.answer(
            "⚠️ Ovozli xabarni tahlil qilib bo'lmadi. "
            "Noma'lum qo'ng'iroqdagi kod/parolni bermang."
        )


# ============================================================
# VIDEO HANDLER
# ============================================================

@dp.message(F.video)
async def handle_video(message: Message):
    user_id = message.from_user.id
    add_user(
        user_id,
        message.from_user.username,
        message.from_user.full_name,
    )

    if is_rate_limited(user_id):
        await message.answer("⚠️ Juda tez-tez xabar yuboryapsiz. Biroz kuting.")
        return

    stats["video_checked_count"] += 1
    size = message.video.file_size or 0

    if size > MAX_FILE_SIZE:
        await message.answer(
            f"⚠️ Video juda katta. Maksimal hajm: "
            f"{MAX_FILE_SIZE // (1024 * 1024)} MB."
        )
        return

    await message.answer(
        "🎥 Video qabul qilindi.\n\n"
        "Hozircha bot videoni to'liq antivirus sifatida tekshirmaydi. "
        "Shubhali video bilan birga kelgan link, APK yoki faylni ham yuboring."
    )


# ============================================================
# ADMIN-ONLY /WEB
# ============================================================

@dp.message(Command("web"))
async def cmd_web(message: Message):
    if message.from_user.id != ADMIN_ID:
        # Deliberately generic so ordinary users do not learn admin details.
        return

    render_url = os.environ.get("RENDER_EXTERNAL_URL")
    if not render_url:
        await message.answer(
            "⚠️ RENDER_EXTERNAL_URL sozlanmagan. "
            "Render URL manzilini Environment'ga qo'shing."
        )
        return

    await message.answer(
        "🔐 <b>Admin panel:</b>\n\n"
        f"{html.escape(render_url.rstrip('/') + '/admin')}\n\n"
        "Panel Basic Auth bilan himoyalangan.",
        parse_mode="HTML",
    )


# ============================================================
# FALLBACK
# ============================================================

@dp.message()
async def fallback(message: Message):
    add_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.full_name,
    )
    await message.answer(
        "🤖 Men havola, fayl, APK, ovozli xabar va boshqa xavfsizlik "
        "obyektlarini tekshirishga yordam beraman.\n\n"
        "ℹ️ /help"
    )


# ============================================================
# MAIN
# ============================================================

async def main():
    threading.Thread(
        target=run_http_server,
        daemon=True,
        name="admin-web",
    ).start()

    logger.info("Cyber Security Bot starting...")
    await bot.delete_webhook(drop_pending_updates=True)
    await set_default_commands(bot)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
'''
import ast, textwrap
ast.parse(code)
len(code.splitlines()), len(code)
