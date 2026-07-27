import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import re
import time
import telebot
from telebot import types
import threading
from flask import Flask, Response, request, jsonify
import os
import json
import base64
import uuid
import logging
import gc
import socket
import urllib.parse
from io import BytesIO

# ==========================================
# سیستم Logging
# ==========================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("ProxyBot")

# ==========================================
# بهینه‌سازی Requests (Connection Pool & Retry)
# ==========================================
session = requests.Session()
retry = Retry(
    total=5,
    read=5,
    connect=5,
    backoff_factor=0.3,
    status_forcelist=[500, 502, 503, 504]
)
adapter = HTTPAdapter(max_retries=retry, pool_connections=100, pool_maxsize=100)
session.mount('http://', adapter)
session.mount('https://', adapter)

# ==========================================
# تنظیمات اولیه و حیاتی ربات
# ==========================================
ROOT_ADMIN_ID = 7419222963
# توکن ربات تلگرام
bot = telebot.TeleBot("7632535360:AAGDFbNcOJpkCpcGb9N77c9U_IAFZ1f0qck", threaded=True)

# ==========================================
# سیستم دیتابیس
# ==========================================
DB_FILE = "database.json"

def load_db():
    default_db = {
        "admins": [ROOT_ADMIN_ID],
        "channels": ["ProxyMTProto", "v2ray_configs_channel"],
        "settings": {
            "max_limit": 400,
            "delete_batch": 100,
            "scrape_interval_mins": 60,
            "clean_interval_hours": 12,
            "v2ray_rename": {
                "enabled": False,
                "name": "ProxyArc",
                "format": "{flag} {name} {num}",
                "num_type": "001"
            },
            "auto_send": {
                "enabled": False,
                "interval_hours": 24,
                "type": "both",
                "last_sent": 0
            }
        },
        "proxies": [],
        "v2ray": [],
        "subs": {}
    }

    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                for k, v in default_db["settings"].items():
                    if k not in loaded.get("settings", {}):
                        loaded.setdefault("settings", {})[k] = v
                if "subs" not in loaded:
                    loaded["subs"] = {}
                logger.info("Database loaded successfully.")
                return loaded
        except Exception as e:
            logger.error(f"Error loading DB: {e}")

    logger.info("Created default Database.")
    return default_db

def save_db(data):
    try:
        for key in ("proxies", "v2ray"):
            data[key] = deduplicate_list(data[key])
        for sub in data.get("subs", {}).values():
            sub["data"] = deduplicate_list(sub.get("data", []))
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
        logger.info("Database saved.")
    except Exception as e:
        logger.error(f"Error saving DB: {e}")

db = load_db()

def _initial_dedup():
    changed = False
    for key in ("proxies", "v2ray"):
        before = len(db[key])
        db[key] = deduplicate_list(db[key])
        if len(db[key]) != before:
            changed = True
    for sub in db.get("subs", {}).values():
        before = len(sub.get("data", []))
        sub["data"] = deduplicate_list(sub.get("data", []))
        if len(sub["data"]) != before:
            changed = True
    if changed:
        save_db(db)

user_states = {}

def get_state(chat_id):
    return user_states.get(chat_id, {})

def set_state(chat_id, state, data=None):
    user_states[chat_id] = {"state": state, "data": data or {}}

def clear_state(chat_id):
    user_states[chat_id] = {}

# ==========================================
# سیستم هوشمند تشخیص پرچم (با Cache)
# ==========================================
FLAG_CACHE = {}

def get_country_flag(host):
    if not host:
        return "🏳"
    if host in FLAG_CACHE:
        return FLAG_CACHE[host]
    try:
        ip = socket.gethostbyname(host)
        resp = session.get(f"http://ip-api.com/json/{ip}?fields=countryCode", timeout=3)
        if resp.status_code == 200:
            cc = resp.json().get("countryCode", "")
            if cc:
                flag = chr(ord(cc[0]) + 127397) + chr(ord(cc[1]) + 127397)
                FLAG_CACHE[host] = flag
                return flag
    except Exception:
        pass
    
    FLAG_CACHE[host] = "🏳"
    return "🏳"

# ==========================================
# سیستم تغییر نام V2Ray
# ==========================================
def process_v2ray_links(links):
    sett = db.get("settings", {}).get("v2ray_rename", {})
    if not sett.get("enabled", False):
        return links

    base_name = sett.get("name", "ProxyArc")
    fmt = sett.get("format", "{flag} {name} {num}")
    num_type = sett.get("num_type", "001")

    processed = []
    for idx, link in enumerate(links, 1):
        try:
            num_str = f"{idx:03d}" if num_type == "001" else f"#{idx}"
            if link.lower().startswith("vmess://"):
                b64 = link[8:]
                b64 += '=' * (-len(b64) % 4)
                payload = json.loads(base64.b64decode(b64).decode('utf-8', errors='ignore'))
                host = payload.get("add", payload.get("host", ""))
                flag = get_country_flag(host)
                new_name = fmt.replace("{flag}", flag).replace("{name}", base_name).replace("{num}", num_str).strip()
                payload["ps"] = new_name
                new_b64 = base64.b64encode(json.dumps(payload).encode('utf-8')).decode('utf-8')
                processed.append(f"vmess://{new_b64}")
            else:
                parsed = urllib.parse.urlparse(link)
                host = parsed.hostname
                flag = get_country_flag(host)
                new_name = fmt.replace("{flag}", flag).replace("{name}", base_name).replace("{num}", num_str).strip()
                new_link = link.split("#")[0] + "#" + urllib.parse.quote(new_name)
                processed.append(new_link)
        except Exception as e:
            processed.append(link)
    
    return processed

# ==========================================
# الگوهای Regex
# ==========================================
PROXY_REGEX = r'(?:https?://t\.me/proxy\?server=|tg://proxy\?server=)[^\s<>"\'\\]+'
V2RAY_REGEX = r'(?:vless|vmess|ss|trojan)://[^\s<>"\'\\]+'

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# ==========================================
# توابع Scraping
# ==========================================

def extract_configs(text):
    proxies = [l.replace("&amp;", "&").strip() for l in re.findall(PROXY_REGEX, text)]
    v2ray   = [l.replace("&amp;", "&").strip() for l in re.findall(V2RAY_REGEX, text)]
    return proxies, v2ray

def scrape_channel(channel, collect_proxy=True, collect_v2ray=True):
    new_proxies = []
    new_v2ray   = []
    url = f"https://t.me/s/{channel.replace('@', '').strip()}"
    try:
        logger.info(f"Scraping channel: {channel}")
        response = session.get(url, headers=HEADERS, timeout=15)
        if response.status_code == 200:
            html = response.text
            if collect_proxy:
                p, _ = extract_configs(html)
                new_proxies.extend(p)
            if collect_v2ray:
                _, v = extract_configs(html)
                new_v2ray.extend(v)
    except Exception as e:
        logger.error(f"Error scraping {channel}: {e}")
    return new_proxies, new_v2ray

def normalize_link(link: str) -> str:
    link = link.strip()
    try:
        if link.lower().startswith('vmess://'):
            b64 = link[8:]
            b64 += '=' * (-len(b64) % 4)
            try:
                payload = json.loads(base64.b64decode(b64).decode('utf-8', errors='ignore'))
                key = json.dumps({
                    'add':  str(payload.get('add',  payload.get('host', ''))).lower().strip(),
                    'port': str(payload.get('port', '')).strip(),
                    'id':   str(payload.get('id',   '')).strip(),
                    'net':  str(payload.get('net',  '')).lower().strip(),
                    'tls':  str(payload.get('tls',  '')).lower().strip(),
                }, sort_keys=True)
                return 'vmess://' + key
            except Exception:
                pass
        parsed = urllib.parse.urlparse(link)
        scheme = parsed.scheme.lower()
        netloc = parsed.netloc.lower()
        path   = parsed.path.lower()
        qs     = urllib.parse.urlencode(sorted(urllib.parse.parse_qs(parsed.query, keep_blank_values=True).items()))
        return urllib.parse.urlunparse((scheme, netloc, path, '', qs, ''))
    except Exception:
        try:
            return link.lower().split('#')[0].strip()
        except Exception:
            return link.lower()

def update_queue(current_list, new_items, max_limit, delete_batch):
    new_items = deduplicate_list(new_items)
    existing_normalized = {normalize_link(x) for x in current_list}
    added_count = 0
    for item in reversed(new_items):
        norm = normalize_link(item)
        if norm not in existing_normalized:
            current_list.insert(0, item)
            existing_normalized.add(norm)
            added_count += 1

    if len(current_list) > max_limit:
        keep = max(0, max_limit - delete_batch)
        current_list = current_list[:keep]

    gc.collect()
    return current_list, added_count

def deduplicate_list(lst: list) -> list:
    seen = set()
    result = []
    for item in lst:
        norm = normalize_link(item)
        if norm not in seen:
            seen.add(norm)
            result.append(item)
    return result

_initial_dedup()

def scrape_all_channels():
    logger.info("Starting global scrape...")
    all_new_proxies = []
    all_new_v2ray   = []

    for ch in db["channels"]:
        p, v = scrape_channel(ch, True, True)
        all_new_proxies.extend(p)
        all_new_v2ray.extend(v)
        time.sleep(1)

    sett = db["settings"]
    db["proxies"], p_added = update_queue(db["proxies"], all_new_proxies, sett["max_limit"], sett["delete_batch"])
    db["v2ray"],   v_added = update_queue(db["v2ray"],   all_new_v2ray, sett["max_limit"], sett["delete_batch"])

    for sub_id, sub in db["subs"].items():
        _update_sub(sub_id)

    save_db(db)
    gc.collect()
    
    return p_added, v_added

def _update_sub(sub_id):
    sub = db["subs"].get(sub_id)
    if not sub:
        return 0

    sub_type  = sub.get("type", "v2ray")
    channels  = sub.get("channels", [])
    sub_sett  = sub.get("settings", db["settings"])
    max_l     = sub_sett.get("max_limit", 400)
    del_b     = sub_sett.get("delete_batch", 100)

    is_proxy  = (sub_type == "proxy")
    is_v2ray  = (sub_type == "v2ray")

    collected = []
    for ch in channels:
        p, v = scrape_channel(ch, is_proxy, is_v2ray)
        if is_proxy:
            collected.extend(p)
        else:
            collected.extend(v)
        time.sleep(0.5)

    sub["data"], added = update_queue(sub.get("data", []), collected, max_l, del_b)
    return added

# ==========================================
# حلقه‌های زمان‌بندی (مقاوم در برابر کرش)
# ==========================================
def resilient_thread(target_func, name):
    def wrapper():
        while True:
            try:
                target_func()
            except Exception as e:
                logger.error(f"Thread {name} crashed: {e}")
                time.sleep(10)
    threading.Thread(target=wrapper, daemon=True).start()

def auto_scraper_loop():
    last_run = time.time()
    while True:
        mins = db["settings"].get("scrape_interval_mins", 60)
        if time.time() - last_run >= (mins * 60):
            scrape_all_channels()
            last_run = time.time()
        time.sleep(10)

def auto_clean_loop():
    last_run = time.time()
    while True:
        hours = db["settings"].get("clean_interval_hours", 12)
        if time.time() - last_run >= (hours * 3600):
            del_b = db["settings"]["delete_batch"]
            if len(db["proxies"]) > del_b:
                db["proxies"] = db["proxies"][:-del_b]
            if len(db["v2ray"]) > del_b:
                db["v2ray"] = db["v2ray"][:-del_b]
            scrape_all_channels()
            last_run = time.time()
        time.sleep(10)

def auto_send_file_loop():
    while True:
        auto_sett = db.get("settings", {}).get("auto_send", {})
        if auto_sett.get("enabled", False):
            last_sent = auto_sett.get("last_sent", 0)
            interval = auto_sett.get("interval_hours", 24) * 3600
            if time.time() - last_sent >= interval:
                logger.info("Executing auto send files...")
                for admin in db["admins"]:
                    try:
                        send_files_to_chat(admin, auto_sett.get("type", "both"))
                    except Exception as e:
                        logger.error(f"Failed to auto-send to admin {admin}: {e}")
                
                db["settings"]["auto_send"]["last_sent"] = time.time()
                save_db(db)
        time.sleep(60)

# ==========================================
# سرور Flask هوشمند و مقاوم
# ==========================================
app = Flask(__name__)

def get_base_url():
    # Render domain
    if os.environ.get("RENDER_EXTERNAL_URL"):
        return os.environ.get("RENDER_EXTERNAL_URL")
    
    envs = [
        "RAILWAY_PUBLIC_DOMAIN", "RAILWAY_STATIC_URL",
        "KOYEB_PUBLIC_DOMAIN", "APP_URL", "BASE_URL", "PUBLIC_URL"
    ]
    for e in envs:
        val = os.environ.get(e)
        if val:
            return val if val.startswith("http") else "https://" + val
            
    if os.environ.get("FLY_APP_NAME"):
        return f"https://{os.environ.get('FLY_APP_NAME')}.fly.dev"
        
    return "https://your-app-domain.onrender.com" # Fallback

@app.route('/')
def index():
    return "✅ ربات جمع آوری پروکسی فعال است!"

@app.route('/health')
def health_check():
    return jsonify({"status": "healthy", "proxies": len(db["proxies"]), "v2ray": len(db["v2ray"])}), 200

@app.route('/sub/proxies')
def sub_proxies():
    clean = deduplicate_list(db["proxies"])
    return Response("\n".join(clean), mimetype='text/plain')

@app.route('/sub/v2ray')
def sub_v2ray():
    clean = deduplicate_list(db["v2ray"])
    processed = process_v2ray_links(clean)
    content = base64.b64encode("\n".join(processed).encode()).decode()
    return Response(content, mimetype='text/plain')

@app.route('/sub/<sub_name>')
def sub_custom(sub_name):
    for sub_id, sub in db["subs"].items():
        if sub.get("name", "").lower() == sub_name.lower():
            clean = deduplicate_list(sub.get("data", []))
            sub_type = sub.get("type", "v2ray")
            if sub_type == "v2ray":
                processed = process_v2ray_links(clean)
                content = base64.b64encode("\n".join(processed).encode()).decode()
            else:
                content = "\n".join(clean)
            return Response(content, mimetype='text/plain')
    return Response("not found", status=404)

# ==========================================
# توابع کمکی ربات
# ==========================================
def is_admin(chat_id):
    return chat_id == ROOT_ADMIN_ID or chat_id in db["admins"]

def get_main_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(
        types.KeyboardButton("🛡 پروکسی ها (MTProto)"),
        types.KeyboardButton("⚡️ سرور های V2ray")
    )
    markup.add(
        types.KeyboardButton("👥 مدیریت ادمین ها"),
        types.KeyboardButton("⚙️ تنظیمات صف")
    )
    markup.add(
        types.KeyboardButton("📡 افزودن/حذف کانال"),
        types.KeyboardButton("➕ افزودن ساب")
    )
    markup.add(
        types.KeyboardButton("📋 لیست ساب ها"),
        types.KeyboardButton("🔄 آپدیت دستی ساب‌ها")
    )
    markup.add(
        types.KeyboardButton("📂 دریافت فایل‌ها"),
        types.KeyboardButton("⏰ ارسال خودکار فایل")
    )
    markup.add(
        types.KeyboardButton("✏️ شخصی سازی سرور")
    )
    return markup

def send_files_to_chat(chat_id, file_type="both"):
    if file_type in ["proxy", "both"] and db["proxies"]:
        clean = deduplicate_list(db["proxies"])
        f_proxy = BytesIO("\n".join(clean).encode())
        f_proxy.name = "Proxy.txt"
        bot.send_document(chat_id, f_proxy, caption=f"🛡 فایل پروکسی ها ({len(clean)} عدد)")
        
    if file_type in ["v2ray", "both"] and db["v2ray"]:
        clean = deduplicate_list(db["v2ray"])
        processed = process_v2ray_links(clean)
        f_v2ray = BytesIO("\n".join(processed).encode())
        f_v2ray.name = "V2Ray.txt"
        bot.send_document(chat_id, f_v2ray, caption=f"⚡️ فایل V2Ray ({len(processed)} عدد)")

# ==========================================
# هندلرهای دستوری
# ==========================================

@bot.message_handler(commands=['start'])
def send_welcome(message):
    if not is_admin(message.chat.id):
        bot.reply_to(message, "⛔️ شما اجازه دسترسی ندارید.")
        return
    clear_state(message.chat.id)
    bot.reply_to(
        message,
        "سلام مدیر عزیز! 🤖\nبه پنل مدیریت سیستم سابسکریپشن خوش آمدید.",
        reply_markup=get_main_keyboard()
    )

# ==========================================
# دکمه‌های منوی اصلی و امکانات جدید
# ==========================================
@bot.message_handler(func=lambda m: is_admin(m.chat.id) and m.text == "🛡 پروکسی ها (MTProto)")
def btn_proxies(message):
    clear_state(message.chat.id)
    sub_link = f"{get_base_url()}/sub/proxies"
    
    bot.reply_to(message,
        f"🛡 **لینک سابسکریپشن پروکسی‌های تلگرام:**\n`{sub_link}`\n\n"
        f"📊 تعداد فعلی: {len(db['proxies'])} عدد\n"
        f"*(فایل مستقیماً از سرور شما سرویس‌دهی می‌شود)*",
        parse_mode="Markdown")

@bot.message_handler(func=lambda m: is_admin(m.chat.id) and m.text == "⚡️ سرور های V2ray")
def btn_v2ray(message):
    clear_state(message.chat.id)
    sub_link = f"{get_base_url()}/sub/v2ray"
    
    bot.reply_to(message,
        f"⚡️ **لینک سابسکریپشن سرورهای V2ray:**\n`{sub_link}`\n\n"
        f"📊 تعداد فعلی: {len(db['v2ray'])} عدد\n"
        f"*(فایل مستقیماً از سرور شما سرویس‌دهی می‌شود)*",
        parse_mode="Markdown")

@bot.message_handler(func=lambda m: is_admin(m.chat.id) and m.text == "👥 مدیریت ادمین ها")
def btn_admins(message):
    set_state(message.chat.id, "waiting_for_admin")
    admins_str = "\n".join([str(a) for a in db["admins"]]) or "هیچ ادمینی ثبت نشده."
    bot.reply_to(message,
        f"لیست ادمین‌های فعلی:\n{admins_str}\n\n"
        "آیدی عددی ادمین جدید را بفرستید. (اگر باشد حذف، اگر نباشد اضافه می‌شود)\n"
        "برای لغو /start را بزنید.")

@bot.message_handler(func=lambda m: is_admin(m.chat.id) and m.text == "⚙️ تنظیمات صف")
def btn_settings(message):
    clear_state(message.chat.id)
    _show_settings(message.chat.id, message.message_id, send_new=True)

@bot.message_handler(func=lambda m: is_admin(m.chat.id) and m.text == "📂 دریافت فایل‌ها")
def btn_get_files(message):
    clear_state(message.chat.id)
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("📄 دریافت فایل MTProto", callback_data="dl_file_proxy"),
        types.InlineKeyboardButton("📄 دریافت فایل V2Ray", callback_data="dl_file_v2ray"),
        types.InlineKeyboardButton("❌ بستن", callback_data="cancel_action")
    )
    bot.reply_to(message, "فایل مورد نظر را برای دانلود انتخاب کنید:", reply_markup=markup)

@bot.message_handler(func=lambda m: is_admin(m.chat.id) and m.text == "⏰ ارسال خودکار فایل")
def btn_auto_send(message):
    clear_state(message.chat.id)
    _show_auto_send_menu(message.chat.id, send_new=True)

@bot.message_handler(func=lambda m: is_admin(m.chat.id) and m.text == "✏️ شخصی سازی سرور")
def btn_rename_servers(message):
    clear_state(message.chat.id)
    _show_rename_menu(message.chat.id, send_new=True)

def _show_rename_menu(chat_id, msg_id=None, send_new=False):
    sett = db["settings"].get("v2ray_rename", {})
    status = "✅ فعال" if sett.get("enabled", False) else "❌ غیرفعال"
    name = sett.get("name", "ProxyArc")
    fmt = sett.get("format", "{flag} {name} {num}")
    num_type = sett.get("num_type", "001")
    
    text = (
        f"✏️ **تنظیمات شخصی‌سازی نام سرورهای V2ray:**\n\n"
        f"وضعیت: {status}\n"
        f"نام پایه: `{name}`\n"
        f"نوع شماره‌گذاری: `{num_type}`\n"
        f"فرمت نام: `{fmt}`\n\n"
        f"*(متغیرها: `{{flag}}` برای پرچم، `{{name}}` برای نام، `{{num}}` برای شماره)*"
    )
    
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("تغییر وضعیت (فعال/غیرفعال)", callback_data="rename_toggle"),
        types.InlineKeyboardButton("✏️ تغییر نام پایه", callback_data="rename_set_name"),
        types.InlineKeyboardButton("🔢 تغییر نوع شماره گذاری", callback_data="rename_set_num"),
        types.InlineKeyboardButton("📝 شخصی سازی فرمت", callback_data="rename_set_fmt"),
        types.InlineKeyboardButton("❌ بستن", callback_data="cancel_action")
    )
    
    _safe_edit_or_send(chat_id, text, markup, msg_id, send_new)

def _show_auto_send_menu(chat_id, msg_id=None, send_new=False):
    auto = db["settings"].get("auto_send", {})
    status = "✅ فعال" if auto.get("enabled", False) else "❌ غیرفعال"
    interval = auto.get("interval_hours", 24)
    file_type = auto.get("type", "both")
    t_map = {"both": "هر دو فایل", "proxy": "پروکسی", "v2ray": "سرور V2Ray"}
    
    text = (
        f"⏰ **تنظیمات ارسال خودکار فایل:**\n\n"
        f"وضعیت فعلی: {status}\n"
        f"زمان ارسال: هر {interval} ساعت\n"
        f"نوع فایل: {t_map.get(file_type, file_type)}"
    )
    
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("تغییر وضعیت (فعال/غیرفعال)", callback_data="auto_send_toggle"),
        types.InlineKeyboardButton("⏱ ویرایش زمان ارسال", callback_data="auto_send_time"),
        types.InlineKeyboardButton("📄 تغییر نوع فایل", callback_data="auto_send_type"),
        types.InlineKeyboardButton("❌ بستن", callback_data="cancel_action")
    )
    
    _safe_edit_or_send(chat_id, text, markup, msg_id, send_new)

def _show_settings(chat_id, msg_id=None, send_new=False):
    sett = db["settings"]
    text = (
        f"⚙️ **تنظیمات فعلی ربات:**\n\n"
        f"🔹 سقف ذخیره: {sett['max_limit']} عدد\n"
        f"🔹 حذفیات از آخر: {sett['delete_batch']} عدد\n"
        f"⏱ بررسی کانال‌ها: هر {sett['scrape_interval_mins']} دقیقه\n"
        f"🧹 پاکسازی و آپدیت: هر {sett['clean_interval_hours']} ساعت"
    )
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("⚙️ تغییر سقف و حذفیات", callback_data="set_limits"),
        types.InlineKeyboardButton("⏱ تغییر زمان بررسی (دقیقه)", callback_data="set_scrape_time"),
        types.InlineKeyboardButton("🧹 تغییر زمان پاکسازی (ساعت)", callback_data="set_clean_time"),
        types.InlineKeyboardButton("❌ بستن", callback_data="cancel_action")
    )
    _safe_edit_or_send(chat_id, text, markup, msg_id, send_new)

def _safe_edit_or_send(chat_id, text, markup, msg_id, send_new):
    if send_new:
        bot.send_message(chat_id, text, reply_markup=markup, parse_mode="Markdown")
    else:
        try:
            bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id,
                                  reply_markup=markup, parse_mode="Markdown")
        except:
            bot.send_message(chat_id, text, reply_markup=markup, parse_mode="Markdown")

@bot.message_handler(func=lambda m: is_admin(m.chat.id) and m.text == "📡 افزودن/حذف کانال")
def btn_channels(message):
    clear_state(message.chat.id)
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("➕ افزودن کانال", callback_data="add_chan"))
    markup.add(types.InlineKeyboardButton("➖ حذف کانال", callback_data="del_chan"))
    markup.add(types.InlineKeyboardButton("🔄 اسکن دستی همین الان", callback_data="force_scan"))
    bot.reply_to(message, "بخش مدیریت کانال‌های پیش‌فرض:", reply_markup=markup)

@bot.message_handler(func=lambda m: is_admin(m.chat.id) and m.text == "➕ افزودن ساب")
def btn_add_sub(message):
    clear_state(message.chat.id)
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("🛡  پروکسی", callback_data="new_sub_proxy"),
        types.InlineKeyboardButton("⚡️ V2ray",   callback_data="new_sub_v2ray")
    )
    markup.add(types.InlineKeyboardButton("❌ کنسل", callback_data="cancel_action"))
    bot.reply_to(message,
        "✨ **ساخت ساب جدید**\n\nاین ساب برای چه نوع لینکی است؟",
        reply_markup=markup, parse_mode="Markdown")

@bot.message_handler(func=lambda m: is_admin(m.chat.id) and m.text == "📋 لیست ساب ها")
def btn_list_subs(message):
    clear_state(message.chat.id)
    _show_subs_list(message.chat.id, send_new=True)

@bot.message_handler(func=lambda m: is_admin(m.chat.id) and m.text == "🔄 آپدیت دستی ساب‌ها")
def btn_manual_update(message):
    clear_state(message.chat.id)
    _show_manual_update_menu(message.chat.id, send_new=True)

def _show_manual_update_menu(chat_id, send_new=False, msg_id=None):
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton(f"🛡 پروکسی‌های پیش‌فرض ({len(db['proxies'])} لینک)", callback_data="manual_update:__default_proxy__")
    )
    markup.add(
        types.InlineKeyboardButton(f"⚡️ V2ray پیش‌فرض ({len(db['v2ray'])} لینک)", callback_data="manual_update:__default_v2ray__")
    )

    for sub_id, sub in db.get("subs", {}).items():
        icon  = "⚡️" if sub.get("type") == "v2ray" else "🛡"
        count = len(sub.get("data", []))
        markup.add(
            types.InlineKeyboardButton(f"{icon} {sub['name']} ({count} لینک)", callback_data=f"manual_update:{sub_id}")
        )
    markup.add(types.InlineKeyboardButton("❌ بستن", callback_data="cancel_action"))
    text = "🔄 **آپدیت دستی ساب‌ها**\n\nروی هر ساب بزنید تا همین الان از کانال‌های تعریف‌شده اسکن شود:"
    _safe_edit_or_send(chat_id, text, markup, msg_id, send_new)

def _show_subs_list(chat_id, send_new=False, msg_id=None):
    subs = db["subs"]
    if not subs:
        text = "📋 هیچ ساب سفارشی‌ای وجود ندارد.\nبا دکمه «➕ افزودن ساب» یک ساب بسازید."
        if send_new:
            bot.send_message(chat_id, text)
        else:
            try:
                bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id)
            except:
                bot.send_message(chat_id, text)
        return

    markup = types.InlineKeyboardMarkup(row_width=1)
    for sub_id, sub in subs.items():
        icon = "⚡️" if sub.get("type") == "v2ray" else "🛡"
        count = len(sub.get("data", []))
        label = f"{icon} {sub['name']}  ({count} لینک)"
        markup.add(types.InlineKeyboardButton(label, callback_data=f"sub_detail:{sub_id}"))
    markup.add(types.InlineKeyboardButton("❌ بستن", callback_data="cancel_action"))
    text = "📋 **لیست ساب‌های سفارشی:**\nروی هر ساب بزنید تا مدیریت کنید."
    _safe_edit_or_send(chat_id, text, markup, msg_id, send_new)

def _show_sub_detail(chat_id, sub_id, msg_id=None):
    sub = db["subs"].get(sub_id)
    if not sub:
        bot.send_message(chat_id, "⚠️ ساب پیدا نشد.")
        return

    sett     = sub.get("settings", {})
    chans    = sub.get("channels", [])
    count    = len(sub.get("data", []))
    icon     = "⚡️" if sub.get("type") == "v2ray" else "🛡"
    
    sub_link = f"{get_base_url()}/sub/{sub['name']}"

    safe_name  = _escape_md(sub['name'])
    safe_link  = _escape_md(sub_link)
    safe_chans = "\n".join([f"• {_escape_md(c)}" for c in chans]) if chans else "• ندارد"

    text = (
        f"{icon} *ساب: {safe_name}*\n"
        f"نوع: {'V2ray' if sub['type']=='v2ray' else 'Proxy'}\n\n"
        f"📡 کانال‌ها ({len(chans)}):\n{safe_chans}\n\n"
        f"⚙️ سقف ذخیره: {sett.get('max_limit', 400)}\n"
        f"🗑 حذف از آخر: {sett.get('delete_batch', 100)}\n"
        f"⏱ بررسی: هر {sett.get('scrape_interval_mins', 60)} دقیقه\n"
        f"🧹 پاکسازی: هر {sett.get('clean_interval_hours', 12)} ساعت\n\n"
        f"📊 لینک‌های فعلی: {count} عدد\n"
        f"🔗 لینک ساب:\n`{safe_link}`\n"
        f"*(فایل مستقیماً از سرور شما سرویس‌دهی می‌شود)*"
    )

    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("📡 تغییر کانال‌ها", callback_data=f"sub_edit_chan:{sub_id}"),
        types.InlineKeyboardButton("⚙️ تغییر سقف/حذف",  callback_data=f"sub_edit_limits:{sub_id}")
    )
    markup.add(
        types.InlineKeyboardButton("⏱ زمان بررسی",    callback_data=f"sub_edit_scrape:{sub_id}"),
        types.InlineKeyboardButton("🧹 زمان پاکسازی", callback_data=f"sub_edit_clean:{sub_id}")
    )
    markup.add(
        types.InlineKeyboardButton("🔄 آپدیت دستی",       callback_data=f"sub_force_update:{sub_id}"),
        types.InlineKeyboardButton("📥 وارد از لینک ساب", callback_data=f"sub_import_url:{sub_id}")
    )
    markup.add(
        types.InlineKeyboardButton("🗑 حذف این ساب", callback_data=f"sub_delete_confirm:{sub_id}")
    )
    markup.add(types.InlineKeyboardButton("◀️ بازگشت به لیست", callback_data="back_to_subs"))

    for pm in ["Markdown", None]:
        try:
            if msg_id:
                bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id, reply_markup=markup, parse_mode=pm)
            else:
                bot.send_message(chat_id, text, reply_markup=markup, parse_mode=pm)
            return
        except Exception:
            continue

# ==========================================
# هندلر Callback Query — مرکزی
# ==========================================
@bot.callback_query_handler(func=lambda call: is_admin(call.message.chat.id))
def callback_inline(call):
    chat_id = call.message.chat.id
    msg_id  = call.message.message_id
    data    = call.data

    try:
        if data == "dl_file_proxy":
            bot.answer_callback_query(call.id, "در حال تولید فایل...")
            send_files_to_chat(chat_id, "proxy")
            return
        elif data == "dl_file_v2ray":
            bot.answer_callback_query(call.id, "در حال تولید فایل...")
            send_files_to_chat(chat_id, "v2ray")
            return
            
        elif data == "rename_toggle":
            sett = db["settings"].setdefault("v2ray_rename", {})
            sett["enabled"] = not sett.get("enabled", False)
            save_db(db)
            _show_rename_menu(chat_id, msg_id)
            return
        elif data == "rename_set_name":
            set_state(chat_id, "waiting_for_rename_name")
            _edit_with_cancel(chat_id, msg_id, "نام پایه جدید سرورها را وارد کنید (مثلا: ProxyArc):", back_data="back_to_rename")
            return
        elif data == "rename_set_num":
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("مدل 001", callback_data="set_num_001"))
            markup.add(types.InlineKeyboardButton("مدل #1", callback_data="set_num_hash"))
            markup.add(types.InlineKeyboardButton("برگشت", callback_data="back_to_rename"))
            try:
                bot.edit_message_text("نوع شماره‌گذاری را انتخاب کنید:", chat_id=chat_id, message_id=msg_id, reply_markup=markup)
            except: pass
            return
        elif data in ["set_num_001", "set_num_hash"]:
            db["settings"].setdefault("v2ray_rename", {})["num_type"] = "001" if data == "set_num_001" else "#1"
            save_db(db)
            _show_rename_menu(chat_id, msg_id)
            return
        elif data == "rename_set_fmt":
            set_state(chat_id, "waiting_for_rename_fmt")
            _edit_with_cancel(chat_id, msg_id, 
                "فرمت دلخواه را بفرستید.\nمتغیرها:\n`{flag}` - پرچم کشور\n`{name}` - نام سرور\n`{num}` - شماره\n\nمثال: `{flag} {name} {num}`", 
                back_data="back_to_rename")
            return
        elif data == "back_to_rename":
            clear_state(chat_id)
            _show_rename_menu(chat_id, msg_id)
            return
            
        elif data == "auto_send_toggle":
            auto = db["settings"].setdefault("auto_send", {})
            auto["enabled"] = not auto.get("enabled", False)
            save_db(db)
            _show_auto_send_menu(chat_id, msg_id)
            return
        elif data == "auto_send_time":
            markup = types.InlineKeyboardMarkup(row_width=3)
            btn_list = [1, 3, 6, 12, 24]
            buttons = [types.InlineKeyboardButton(f"{h} ساعت", callback_data=f"set_auto_time_{h}") for h in btn_list]
            markup.add(*buttons)
            markup.add(types.InlineKeyboardButton("برگشت", callback_data="back_to_auto"))
            try:
                bot.edit_message_text("زمان ارسال را انتخاب کنید:", chat_id=chat_id, message_id=msg_id, reply_markup=markup)
            except: pass
            return
        elif data.startswith("set_auto_time_"):
            db["settings"].setdefault("auto_send", {})["interval_hours"] = int(data.split("_")[-1])
            save_db(db)
            _show_auto_send_menu(chat_id, msg_id)
            return
        elif data == "auto_send_type":
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("هر دو فایل", callback_data="set_auto_type_both"))
            markup.add(types.InlineKeyboardButton("فقط پروکسی", callback_data="set_auto_type_proxy"))
            markup.add(types.InlineKeyboardButton("فقط V2Ray", callback_data="set_auto_type_v2ray"))
            markup.add(types.InlineKeyboardButton("برگشت", callback_data="back_to_auto"))
            try:
                bot.edit_message_text("نوع فایل ارسالی را انتخاب کنید:", chat_id=chat_id, message_id=msg_id, reply_markup=markup)
            except: pass
            return
        elif data.startswith("set_auto_type_"):
            db["settings"].setdefault("auto_send", {})["type"] = data.split("_")[-1]
            save_db(db)
            _show_auto_send_menu(chat_id, msg_id)
            return
        elif data == "back_to_auto":
            clear_state(chat_id)
            _show_auto_send_menu(chat_id, msg_id)
            return

        if data == "add_chan":
            set_state(chat_id, "waiting_for_add_chan")
            _edit_with_cancel(chat_id, msg_id, "لینک یا آیدی کانال‌ها را بفرستید. (هر خط یک کانال)")

        elif data == "del_chan":
            set_state(chat_id, "waiting_for_del_chan")
            chans = "\n".join(db["channels"]) if db["channels"] else "کانالی وجود ندارد."
            _edit_with_cancel(chat_id, msg_id, f"کانال‌های فعلی:\n{chans}\n\nآیدی کانال‌هایی که می‌خواهید حذف شوند را بفرستید.")

        elif data == "force_scan":
            bot.answer_callback_query(call.id, "در حال اسکن...", show_alert=True)
            p, v = scrape_all_channels()
            bot.send_message(chat_id, f"✅ اسکن تمام شد!\n+{p} پروکسی جدید\n+{v} سرور V2ray جدید")

        elif data == "set_limits":
            set_state(chat_id, "waiting_for_limits")
            _edit_with_cancel(chat_id, msg_id, "مقادیر جدید سقف و حذفیات را با خط تیره بفرستید.\nمثال: `400-100`")

        elif data == "set_scrape_time":
            set_state(chat_id, "waiting_for_scrape_time")
            _edit_with_cancel(chat_id, msg_id, "زمان بررسی کانال‌ها را به **دقیقه** بفرستید. (مثال: `60`)")

        elif data == "set_clean_time":
            set_state(chat_id, "waiting_for_clean_time")
            _edit_with_cancel(chat_id, msg_id, "زمان پاکسازی را به **ساعت** بفرستید. (مثال: `12`)")

        elif data in ("new_sub_proxy", "new_sub_v2ray"):
            sub_type = "proxy" if data == "new_sub_proxy" else "v2ray"
            set_state(chat_id, "add_sub_name", {"type": sub_type})
            icon = "🛡" if sub_type == "proxy" else "⚡️"
            _edit_with_cancel(chat_id, msg_id,
                f"{icon} نوع ساب: **{'Proxy' if sub_type=='proxy' else 'V2ray'}**\n\n"
                "حالا یک **اسم** برای این ساب بنویس.\n"
                "_(فقط حروف انگلیسی، اعداد و خط تیره — این اسم در لینک ساب استفاده می‌شود)_")

        elif data.startswith("sub_detail:"):
            sub_id = data.split(":", 1)[1]
            _show_sub_detail(chat_id, sub_id, msg_id)

        elif data.startswith("sub_edit_chan:"):
            sub_id = data.split(":", 1)[1]
            set_state(chat_id, "sub_edit_chan", {"sub_id": sub_id})
            sub = db["subs"].get(sub_id, {})
            chans = "\n".join(sub.get("channels", [])) or "ندارد"
            _edit_with_cancel(chat_id, msg_id,
                f"کانال‌های فعلی:\n{chans}\n\n"
                "لیست **جدید** کانال‌ها را بفرستید. (هر خط یک کانال)\n"
                "⚠️ این جایگزین کانال‌های قبلی می‌شود.",
                back_data=f"sub_detail:{sub_id}")

        elif data.startswith("sub_edit_limits:"):
            sub_id = data.split(":", 1)[1]
            set_state(chat_id, "sub_edit_limits", {"sub_id": sub_id})
            _edit_with_cancel(chat_id, msg_id, "مقادیر جدید سقف و حذفیات را با خط تیره بفرستید.\nمثال: `400-100`", back_data=f"sub_detail:{sub_id}")

        elif data.startswith("sub_edit_scrape:"):
            sub_id = data.split(":", 1)[1]
            set_state(chat_id, "sub_edit_scrape", {"sub_id": sub_id})
            _edit_with_cancel(chat_id, msg_id, "زمان بررسی کانال‌های این ساب را به **دقیقه** بفرستید. (مثال: `60`)", back_data=f"sub_detail:{sub_id}")

        elif data.startswith("sub_edit_clean:"):
            sub_id = data.split(":", 1)[1]
            set_state(chat_id, "sub_edit_clean", {"sub_id": sub_id})
            _edit_with_cancel(chat_id, msg_id, "زمان پاکسازی این ساب را به **ساعت** بفرستید. (مثال: `12`)", back_data=f"sub_detail:{sub_id}")

        elif data.startswith("sub_force_update:"):
            sub_id = data.split(":", 1)[1]
            bot.answer_callback_query(call.id, "در حال آپدیت ساب...", show_alert=True)
            added = _update_sub(sub_id)
            save_db(db)
            sub = db["subs"].get(sub_id, {})
            bot.send_message(chat_id, f"✅ ساب «{sub.get('name','')}» آپدیت شد.\n+{added} لینک جدید اضافه شد.")
            _show_sub_detail(chat_id, sub_id, msg_id)

        elif data.startswith("sub_import_url:"):
            sub_id = data.split(":", 1)[1]
            sub    = db["subs"].get(sub_id, {})
            icon   = "⚡️" if sub.get("type") == "v2ray" else "🛡"
            set_state(chat_id, "sub_import_url", {"sub_id": sub_id})
            _edit_with_cancel(chat_id, msg_id,
                f"📥 **وارد کردن از لینک ساب خارجی**\n\n"
                f"ساب مقصد: {icon} **{sub.get('name','')}**\n"
                f"نوع: {'V2ray' if sub.get('type')=='v2ray' else 'Proxy'}\n\n"
                "لینک ساب خارجی را بفرستید.",
                back_data=f"sub_detail:{sub_id}")

        elif data.startswith("sub_delete_confirm:"):
            sub_id = data.split(":", 1)[1]
            sub = db["subs"].get(sub_id, {})
            markup = types.InlineKeyboardMarkup(row_width=2)
            markup.add(
                types.InlineKeyboardButton("🗑 بله، حذف شود", callback_data=f"sub_delete_yes:{sub_id}"),
                types.InlineKeyboardButton("◀️ خیر، برگشت",  callback_data=f"sub_detail:{sub_id}")
            )
            try:
                bot.edit_message_text(f"⚠️ آیا مطمئن هستید که می‌خواهید ساب **{sub.get('name','')}** را کاملاً حذف کنید؟", chat_id=chat_id, message_id=msg_id, reply_markup=markup, parse_mode="Markdown")
            except: pass

        elif data.startswith("sub_delete_yes:"):
            sub_id = data.split(":", 1)[1]
            sub = db["subs"].pop(sub_id, {})
            save_db(db)
            bot.answer_callback_query(call.id, f"ساب {sub.get('name','')} حذف شد.", show_alert=True)
            _show_subs_list(chat_id, msg_id=msg_id)

        elif data == "back_to_subs":
            clear_state(chat_id)
            _show_subs_list(chat_id, msg_id=msg_id)

        elif data == "cancel_action":
            clear_state(chat_id)
            try:
                bot.edit_message_text("عملیات لغو شد. ✅", chat_id=chat_id, message_id=msg_id)
            except: pass

        elif data == "new_sub_confirm_settings":
            st2 = get_state(chat_id)
            if st2.get("state") == "add_sub_show_settings":
                _show_new_sub_settings_menu(chat_id, msg_id, st2["data"])

        elif data == "new_sub_set_limits":
            st2 = get_state(chat_id)
            set_state(chat_id, "new_sub_waiting_limits", st2.get("data", {}))
            _edit_with_cancel(chat_id, msg_id, "سقف ذخیره و تعداد حذف از آخر را بفرست.\nمثال: `400-100`")

        elif data == "new_sub_set_scrape":
            st2 = get_state(chat_id)
            set_state(chat_id, "new_sub_waiting_scrape", st2.get("data", {}))
            _edit_with_cancel(chat_id, msg_id, "زمان بررسی کانال‌ها را به **دقیقه** بفرست. (مثال: `60`)")

        elif data == "new_sub_set_clean":
            st2 = get_state(chat_id)
            set_state(chat_id, "new_sub_waiting_clean", st2.get("data", {}))
            _edit_with_cancel(chat_id, msg_id, "زمان پاکسازی اجباری را به **ساعت** بفرست. (مثال: `12`)")

        elif data == "new_sub_create":
            st2 = get_state(chat_id)
            _finalize_new_sub(chat_id, msg_id, st2.get("data", {}))

        elif data.startswith("manual_update:"):
            target = data.split(":", 1)[1]
            bot.answer_callback_query(call.id, "⏳ در حال اسکن...", show_alert=False)

            if target == "__default_proxy__":
                all_p = []
                for ch in db["channels"]:
                    p, _ = scrape_channel(ch, collect_proxy=True, collect_v2ray=False)
                    all_p.extend(p)
                    time.sleep(0.5)
                sett = db["settings"]
                db["proxies"], added = update_queue(db["proxies"], all_p, sett["max_limit"], sett["delete_batch"])
                save_db(db)
                bot.send_message(chat_id, f"✅ **آپدیت ساب پروکسی پیش‌فرض**\n\nلینک‌های جدید: **+{added}** عدد", parse_mode="Markdown")

            elif target == "__default_v2ray__":
                all_v = []
                for ch in db["channels"]:
                    _, v = scrape_channel(ch, collect_proxy=False, collect_v2ray=True)
                    all_v.extend(v)
                    time.sleep(0.5)
                sett = db["settings"]
                db["v2ray"], added = update_queue(db["v2ray"], all_v, sett["max_limit"], sett["delete_batch"])
                save_db(db)
                bot.send_message(chat_id, f"✅ **آپدیت ساب V2ray پیش‌فرض**\n\nلینک‌های جدید: **+{added}** عدد", parse_mode="Markdown")

            else:
                sub = db["subs"].get(target)
                if sub:
                    added = _update_sub(target)
                    save_db(db)
                    bot.send_message(chat_id, f"✅ **آپدیت ساب «{sub['name']}»**\n\nلینک‌های جدید: **+{added}** عدد", parse_mode="Markdown")
                else:
                    bot.send_message(chat_id, "⚠️ ساب پیدا نشد.")
            _show_manual_update_menu(chat_id, msg_id=msg_id)

        bot.answer_callback_query(call.id)
    except Exception as e:
        logger.error(f"Callback error: {e}")


def _escape_md(text: str) -> str:
    for ch in ['_', '*', '`', '[']:
        text = text.replace(ch, f'\\{ch}')
    return text

def _edit_with_cancel(chat_id, msg_id, text, back_data=None):
    markup = types.InlineKeyboardMarkup()
    if back_data:
        markup.add(types.InlineKeyboardButton("◀️ برگشت", callback_data=back_data))
    markup.add(types.InlineKeyboardButton("❌ کنسل", callback_data="cancel_action"))
    for pm in ["Markdown", None]:
        try:
            if msg_id:
                bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id, reply_markup=markup, parse_mode=pm)
            else:
                bot.send_message(chat_id, text, reply_markup=markup, parse_mode=pm)
            return
        except Exception:
            continue

def _show_new_sub_settings_menu(chat_id, msg_id, data):
    name     = data.get("name", "")
    sub_type = data.get("type", "v2ray")
    channels = data.get("channels", [])
    sett     = data.get("settings", {
        "max_limit": 400, "delete_batch": 100, "scrape_interval_mins": 60, "clean_interval_hours": 12
    })
    icon = "⚡️" if sub_type == "v2ray" else "🛡"
    text = (
        f"✨ **تنظیمات ساب جدید: {name}** {icon}\n\n"
        f"📡 کانال‌ها: {len(channels)} عدد\n"
        f"⚙️ سقف ذخیره: {sett['max_limit']} | حذف از آخر: {sett['delete_batch']}\n"
        f"⏱ بررسی: هر {sett['scrape_interval_mins']} دقیقه\n"
        f"🧹 پاکسازی: هر {sett['clean_interval_hours']} ساعت\n\n"
        "می‌توانید تنظیمات را تغییر دهید یا همین الان ساب را بسازید:"
    )
    set_state(chat_id, "add_sub_show_settings", data)
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("⚙️ سقف/حذفیات",   callback_data="new_sub_set_limits"),
        types.InlineKeyboardButton("⏱ زمان بررسی",    callback_data="new_sub_set_scrape")
    )
    markup.add(
        types.InlineKeyboardButton("🧹 زمان پاکسازی", callback_data="new_sub_set_clean"),
        types.InlineKeyboardButton("✅ ساخت ساب!",     callback_data="new_sub_create")
    )
    markup.add(types.InlineKeyboardButton("❌ کنسل", callback_data="cancel_action"))
    _safe_edit_or_send(chat_id, text, markup, msg_id, False)

def _finalize_new_sub(chat_id, msg_id, data):
    name     = data.get("name", f"sub_{int(time.time())}")
    sub_type = data.get("type", "v2ray")
    channels = data.get("channels", [])
    sett     = data.get("settings", {
        "max_limit": 400, "delete_batch": 100, "scrape_interval_mins": 60, "clean_interval_hours": 12
    })
    sub_id = str(uuid.uuid4())[:8]
    db["subs"][sub_id] = {
        "name": name, "type": sub_type, "channels": channels, "settings": sett, "data": []
    }
    save_db(db)
    
    sub_link = f"{get_base_url()}/sub/{name}"
    icon = "⚡️" if sub_type == "v2ray" else "🛡"
    clear_state(chat_id)
    text = (
        f"✅ **ساب «{name}» با موفقیت ساخته شد!** {icon}\n\n"
        f"🔗 لینک ساب شما:\n`{sub_link}`\n\n"
        "ربات از این کانال‌ها لینک جمع‌آوری می‌کند."
    )
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔄 آپدیت فوری", callback_data=f"sub_force_update:{sub_id}"))
    _safe_edit_or_send(chat_id, text, markup, msg_id, False)

# ==========================================
# هندلر پیام‌های متنی (State Machine)
# ==========================================
@bot.message_handler(func=lambda m: is_admin(m.chat.id) and bool(get_state(m.chat.id)))
def handle_states(message):
    chat_id  = message.chat.id
    st       = get_state(chat_id)
    state    = st.get("state", "")
    data     = st.get("data", {})
    text_in  = message.text.strip()

    try:
        if state == "waiting_for_rename_name":
            db["settings"].setdefault("v2ray_rename", {})["name"] = text_in
            save_db(db)
            bot.reply_to(message, "✅ نام سرور ثبت شد.")
            clear_state(chat_id)
            _show_rename_menu(chat_id, send_new=True)
            return
        elif state == "waiting_for_rename_fmt":
            db["settings"].setdefault("v2ray_rename", {})["format"] = text_in
            save_db(db)
            bot.reply_to(message, "✅ فرمت جدید ثبت شد.")
            clear_state(chat_id)
            _show_rename_menu(chat_id, send_new=True)
            return

        if state == "waiting_for_admin":
            try:
                new_id = int(text_in)
                if new_id in db["admins"]:
                    db["admins"].remove(new_id)
                    bot.reply_to(message, f"✅ ادمین {new_id} حذف شد.")
                else:
                    db["admins"].append(new_id)
                    bot.reply_to(message, f"✅ ادمین {new_id} اضافه شد.")
                save_db(db)
            except:
                bot.reply_to(message, "⚠️ فقط عدد بفرستید.")
            clear_state(chat_id)

        elif state == "waiting_for_limits":
            try:
                p = text_in.split("-")
                db["settings"]["max_limit"]   = int(p[0])
                db["settings"]["delete_batch"] = int(p[1])
                save_db(db)
                bot.reply_to(message, "✅ تنظیمات ذخیره شد.")
            except:
                bot.reply_to(message, "⚠️ فرمت اشتباه. مثال: `400-100`")
            clear_state(chat_id)

        elif state == "waiting_for_scrape_time":
            try:
                db["settings"]["scrape_interval_mins"] = int(text_in)
                save_db(db)
                bot.reply_to(message, f"✅ زمان بررسی روی {text_in} دقیقه تنظیم شد.")
            except:
                bot.reply_to(message, "⚠️ فقط عدد بفرستید.")
            clear_state(chat_id)

        elif state == "waiting_for_clean_time":
            try:
                db["settings"]["clean_interval_hours"] = int(text_in)
                save_db(db)
                bot.reply_to(message, f"✅ زمان پاکسازی روی {text_in} ساعت تنظیم شد.")
            except:
                bot.reply_to(message, "⚠️ فقط عدد بفرستید.")
            clear_state(chat_id)

        elif state == "waiting_for_add_chan":
            new_channels = text_in.split("\n")
            added = 0
            for ch in new_channels:
                clean = ch.replace("https://t.me/", "").replace("@", "").strip()
                if clean and clean not in db["channels"]:
                    db["channels"].append(clean)
                    added += 1
            save_db(db)
            bot.reply_to(message, f"✅ {added} کانال اضافه شد.")
            clear_state(chat_id)

        elif state == "waiting_for_del_chan":
            del_channels = text_in.split("\n")
            removed = 0
            for ch in del_channels:
                clean = ch.replace("https://t.me/", "").replace("@", "").strip()
                if clean in db["channels"]:
                    db["channels"].remove(clean)
                    removed += 1
            save_db(db)
            bot.reply_to(message, f"✅ {removed} کانال حذف شد.")
            clear_state(chat_id)

        elif state == "add_sub_name":
            clean_name = re.sub(r'[^a-zA-Z0-9\-_]', '', text_in.strip())
            if not clean_name:
                bot.reply_to(message, "⚠️ اسم باید فقط شامل حروف انگلیسی، اعداد و - باشد.")
                return
            for s in db["subs"].values():
                if s.get("name", "").lower() == clean_name.lower():
                    bot.reply_to(message, "⚠️ این اسم قبلاً استفاده شده. یک اسم دیگر بفرست.")
                    return

            data["name"] = clean_name
            set_state(chat_id, "add_sub_channels", data)
            bot.reply_to(message,
                f"✅ اسم ساب: **{clean_name}**\n\n"
                "حالا لیست کانال‌هایی که می‌خواهی از آن‌ها لینک جمع‌آوری شود را بفرست.\n"
                "_(هر خط یک کانال — آیدی یا لینک t.me قبول می‌شود)_",
                parse_mode="Markdown")

        elif state == "add_sub_channels":
            raw_chans = text_in.split("\n")
            channels = [ch.replace("https://t.me/", "").replace("@", "").strip() for ch in raw_chans if ch.replace("https://t.me/", "").replace("@", "").strip()]
            if not channels:
                bot.reply_to(message, "⚠️ حداقل یک کانال باید وارد کنی.")
                return

            data["channels"] = channels
            data.setdefault("settings", {"max_limit": 400, "delete_batch": 100, "scrape_interval_mins": 60, "clean_interval_hours": 12})
            set_state(chat_id, "add_sub_show_settings", data)
            _show_new_sub_settings_menu(chat_id, None, data)

        elif state == "new_sub_waiting_limits":
            try:
                p = text_in.split("-")
                data.setdefault("settings", {})["max_limit"]    = int(p[0])
                data["settings"]["delete_batch"] = int(p[1])
                set_state(chat_id, "add_sub_show_settings", data)
                bot.reply_to(message, "✅ تنظیم شد.")
                _show_new_sub_settings_menu(chat_id, None, data)
            except:
                bot.reply_to(message, "⚠️ فرمت اشتباه. مثال: `400-100`")

        elif state == "new_sub_waiting_scrape":
            try:
                data.setdefault("settings", {})["scrape_interval_mins"] = int(text_in)
                set_state(chat_id, "add_sub_show_settings", data)
                bot.reply_to(message, f"✅ تنظیم شد.")
                _show_new_sub_settings_menu(chat_id, None, data)
            except:
                bot.reply_to(message, "⚠️ فقط عدد بفرستید.")

        elif state == "new_sub_waiting_clean":
            try:
                data.setdefault("settings", {})["clean_interval_hours"] = int(text_in)
                set_state(chat_id, "add_sub_show_settings", data)
                bot.reply_to(message, f"✅ تنظیم شد.")
                _show_new_sub_settings_menu(chat_id, None, data)
            except:
                bot.reply_to(message, "⚠️ فقط عدد بفرستید.")

        elif state == "sub_edit_chan":
            sub_id = data.get("sub_id")
            raw_chans = text_in.split("\n")
            channels = [ch.replace("https://t.me/", "").replace("@", "").strip() for ch in raw_chans if ch.replace("https://t.me/", "").replace("@", "").strip()]
            if not channels:
                bot.reply_to(message, "⚠️ حداقل یک کانال وارد کن.")
                return
            db["subs"][sub_id]["channels"] = channels
            save_db(db)
            bot.reply_to(message, f"✅ کانال‌های ساب آپدیت شد. ({len(channels)} کانال)")
            clear_state(chat_id)
            _show_sub_detail(chat_id, sub_id)

        elif state == "sub_edit_limits":
            sub_id = data.get("sub_id")
            try:
                p = text_in.split("-")
                db["subs"][sub_id].setdefault("settings", {})["max_limit"]    = int(p[0])
                db["subs"][sub_id]["settings"]["delete_batch"] = int(p[1])
                save_db(db)
                bot.reply_to(message, "✅ تنظیمات سقف ذخیره شد.")
            except:
                bot.reply_to(message, "⚠️ فرمت اشتباه.")
            clear_state(chat_id)
            _show_sub_detail(chat_id, sub_id)

        elif state == "sub_edit_scrape":
            sub_id = data.get("sub_id")
            try:
                db["subs"][sub_id].setdefault("settings", {})["scrape_interval_mins"] = int(text_in)
                save_db(db)
                bot.reply_to(message, f"✅ زمان بررسی روی {text_in} دقیقه تنظیم شد.")
            except: bot.reply_to(message, "⚠️ فقط عدد بفرستید.")
            clear_state(chat_id)
            _show_sub_detail(chat_id, sub_id)

        elif state == "sub_edit_clean":
            sub_id = data.get("sub_id")
            try:
                db["subs"][sub_id].setdefault("settings", {})["clean_interval_hours"] = int(text_in)
                save_db(db)
                bot.reply_to(message, f"✅ زمان پاکسازی روی {text_in} ساعت تنظیم شد.")
            except: bot.reply_to(message, "⚠️ فقط عدد بفرستید.")
            clear_state(chat_id)
            _show_sub_detail(chat_id, sub_id)

        elif state == "sub_import_url":
            sub_id   = data.get("sub_id")
            sub      = db["subs"].get(sub_id)
            if not sub:
                bot.reply_to(message, "⚠️ ساب پیدا نشد.")
                clear_state(chat_id)
                return

            url      = text_in.strip()
            sub_type = sub.get("type", "v2ray")
            wait_msg = bot.reply_to(message, "⏳ در حال دریافت و پردازش لینک ساب...")

            try:
                resp = session.get(url, headers=HEADERS, timeout=15)
                resp.raise_for_status()
                raw = resp.text.strip()
                decoded = ""
                try:
                    padded = raw + "=" * (-len(raw) % 4)
                    decoded = base64.b64decode(padded).decode("utf-8", errors="ignore")
                except Exception:
                    decoded = ""

                content = decoded if decoded and re.search(V2RAY_REGEX, decoded) else raw
                proxies_found, v2ray_found = extract_configs(content)
                links = proxies_found if sub_type == "proxy" else v2ray_found

                if not links:
                    bot.edit_message_text("⚠️ هیچ لینک معتبری در این ساب پیدا نشد.", chat_id=chat_id, message_id=wait_msg.message_id)
                    clear_state(chat_id)
                    return

                sub_sett = sub.get("settings", db["settings"])
                sub["data"], added = update_queue(sub.get("data", []), links, sub_sett["max_limit"], sub_sett["delete_batch"])
                save_db(db)

                bot.edit_message_text(f"✅ **وارد کردن از لینک ساب انجام شد!**\n\nلینک‌های جدید: **+{added}** عدد", chat_id=chat_id, message_id=wait_msg.message_id, parse_mode="Markdown")

            except Exception as e:
                bot.edit_message_text(f"❌ خطا:\n`{e}`", chat_id=chat_id, message_id=wait_msg.message_id, parse_mode="Markdown")

            clear_state(chat_id)
            _show_sub_detail(chat_id, sub_id)

    except Exception as e:
        logger.error(f"Error handling state {state}: {e}")
        bot.reply_to(message, "❌ خطایی رخ داد.")
        clear_state(chat_id)

# ==========================================
# اجرای ربات و سرور با مقاومت بالا
# ==========================================
def run_telegram_bot():
    logger.info("ربات تلگرام شروع به کار کرد...")
    bot.infinity_polling(timeout=10, long_polling_timeout=5, logger_level=logging.ERROR)

if __name__ == "__main__":
    
    # حلقه‌های زمان‌بندی و هوشمند
    resilient_thread(auto_scraper_loop, "Scraper")
    resilient_thread(auto_clean_loop, "Cleaner")
    resilient_thread(auto_send_file_loop, "AutoSender")
    resilient_thread(run_telegram_bot, "TelegramBot")

    # سرور وب Flask حفظ شده تا در سرویس‌های هاست ابری (مانند Render/Railway) زنده بماند
    port = int(os.environ.get("PORT", 10000))
    logger.info(f"سرور وب روی پورت {port} استارت شد...")
    app.run(host='0.0.0.0', port=port, threaded=True)