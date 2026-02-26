import requests
import re
import time
import telebot
from telebot import types
import threading
from flask import Flask, Response
import os
import json
import base64
import uuid

# ==========================================
# تنظیمات اولیه و حیاتی ربات
# ==========================================
ROOT_ADMIN_ID = 7419222963

bot = telebot.TeleBot("7632535360:AAElwqtIX521S9n_pAxo0UWRWSPkMVMdjMI")

# ==========================================
# سیستم دیتابیس
# ==========================================
DB_FILE = "database.json"

def load_db():
    default_db = {
        "admins": [],
        "channels": ["ProxyMTProto", "v2ray_configs_channel"],
        "settings": {
            "max_limit": 400,
            "delete_batch": 100,
            "scrape_interval_mins": 60,
            "clean_interval_hours": 12
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
                return loaded
        except:
            pass

    return default_db

def save_db(data):
    # قبل از هر بار ذخیره، تکراری‌ها را حذف کن
    for key in ("proxies", "v2ray"):
        data[key] = deduplicate_list(data[key])
    for sub in data.get("subs", {}).values():
        sub["data"] = deduplicate_list(sub.get("data", []))
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

db = load_db()

def _initial_dedup():
    """حذف تکراری‌های احتمالی از داده‌های ذخیره‌شده هنگام بارگذاری اولیه"""
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

# state هر کاربر — می‌تواند dict با کلیدهای "state" و "data" باشد
user_states = {}

def get_state(chat_id):
    return user_states.get(chat_id, {})

def set_state(chat_id, state, data=None):
    user_states[chat_id] = {"state": state, "data": data or {}}

def clear_state(chat_id):
    user_states[chat_id] = {}

# ==========================================
# الگوهای Regex
# ==========================================
PROXY_REGEX = r'(?:https?://t\.me/proxy\?server=|tg://proxy\?server=)[^\s<>"\'\\]+'
V2RAY_REGEX = r'(?:vless|vmess|ss|trojan)://[^\s<>"\'\\]+'

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# ==========================================
# توابع Scraping
# ==========================================

def extract_configs(text):
    proxies = [l.replace("&amp;", "&").strip() for l in re.findall(PROXY_REGEX, text)]
    v2ray   = [l.replace("&amp;", "&").strip() for l in re.findall(V2RAY_REGEX, text)]
    return proxies, v2ray

def scrape_channel(channel, collect_proxy=True, collect_v2ray=True):
    """یک کانال عمومی را از طریق t.me/s/ اسکن می‌کند و لینک‌های v2ray/proxy را استخراج می‌کند."""
    new_proxies = []
    new_v2ray   = []
    url = f"https://t.me/s/{channel.replace('@', '').strip()}"
    try:
        response = requests.get(url, headers=HEADERS, timeout=10)
        if response.status_code == 200:
            html = response.text
            if collect_proxy:
                p, _ = extract_configs(html)
                new_proxies.extend(p)
            if collect_v2ray:
                _, v = extract_configs(html)
                new_v2ray.extend(v)
    except Exception as e:
        print(f"خطا در اسکن {channel}: {e}")
    return new_proxies, new_v2ray

def normalize_link(link: str) -> str:
    """
    نرمالایز کاملاً هوشمند برای تشخیص سرورهای تکراری:
    - vmess: base64 decode میشه، فقط add/port/id/net/tls مقایسه میشه (ps/نام نادیده)
    - vless/trojan/ss: fragment حذف، query مرتب میشه
    """
    import base64 as _b64, json as _json
    from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
    link = link.strip()
    try:
        if link.lower().startswith('vmess://'):
            b64 = link[8:]
            b64 += '=' * (-len(b64) % 4)
            try:
                payload = _json.loads(_b64.b64decode(b64).decode('utf-8', errors='ignore'))
                key = _json.dumps({
                    'add':  str(payload.get('add',  payload.get('host', ''))).lower().strip(),
                    'port': str(payload.get('port', '')).strip(),
                    'id':   str(payload.get('id',   '')).strip(),
                    'net':  str(payload.get('net',  '')).lower().strip(),
                    'tls':  str(payload.get('tls',  '')).lower().strip(),
                }, sort_keys=True)
                return 'vmess://' + key
            except Exception:
                pass
        parsed = urlparse(link)
        scheme = parsed.scheme.lower()
        netloc = parsed.netloc.lower()
        path   = parsed.path.lower()
        qs     = urlencode(sorted(parse_qs(parsed.query, keep_blank_values=True).items()))
        return urlunparse((scheme, netloc, path, '', qs, ''))
    except Exception:
        try:
            return link.lower().split('#')[0].strip()
        except Exception:
            return link.lower()


# ==========================================
# ✅ تنها تغییر: dedup روی new_items قبل از افزودن
# ==========================================
def update_queue(current_list, new_items, max_limit, delete_batch):
    """
    آیتم‌های جدید را به اول صف اضافه می‌کند.
    ابتدا new_items خودش dedup می‌شود، سپس با لیست فعلی مقایسه می‌شود.
    """
    # مرحله ۱: حذف تکراری از خود new_items (مثلاً لینک یکسان از دو کانال)
    new_items = deduplicate_list(new_items)

    # مرحله ۲: مقایسه با لیست فعلی
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

    return current_list, added_count


def deduplicate_list(lst: list) -> list:
    """
    یک لیست موجود را پاکسازی می‌کند و تمام تکراری‌ها را حذف می‌کند.
    اولین نمونه هر لینک حفظ می‌شود.
    """
    seen = set()
    result = []
    for item in lst:
        norm = normalize_link(item)
        if norm not in seen:
            seen.add(norm)
            result.append(item)
    return result

# اجرای پاکسازی اولیه روی داده‌های بارگذاری‌شده
_initial_dedup()


def scrape_all_channels():
    """اسکن سراسری همه کانال‌های پیش‌فرض + ساب‌های سفارشی"""
    print("شروع اسکن خودکار...")
    all_new_proxies = []
    all_new_v2ray   = []

    for ch in db["channels"]:
        p, v = scrape_channel(ch, True, True)
        all_new_proxies.extend(p)
        all_new_v2ray.extend(v)
        time.sleep(1)

    sett = db["settings"]
    db["proxies"], p_added = update_queue(db["proxies"], all_new_proxies,
                                          sett["max_limit"], sett["delete_batch"])
    db["v2ray"],   v_added = update_queue(db["v2ray"],   all_new_v2ray,
                                          sett["max_limit"], sett["delete_batch"])

    # آپدیت ساب‌های سفارشی
    for sub_id, sub in db["subs"].items():
        _update_sub(sub_id)

    save_db(db)
    return p_added, v_added

def _update_sub(sub_id):
    """یک ساب سفارشی خاص را آپدیت می‌کند."""
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
# حلقه‌های زمان‌بندی
# ==========================================
def auto_scraper_loop():
    last_run = time.time()
    while True:
        mins = db["settings"].get("scrape_interval_mins", 60)
        if time.time() - last_run >= (mins * 60):
            try:
                scrape_all_channels()
            except Exception as e:
                print(f"خطا در اسکریپر خودکار: {e}")
            last_run = time.time()
        time.sleep(10)

def auto_clean_loop():
    last_run = time.time()
    while True:
        hours = db["settings"].get("clean_interval_hours", 12)
        if time.time() - last_run >= (hours * 3600):
            try:
                del_b = db["settings"]["delete_batch"]
                if len(db["proxies"]) > del_b:
                    db["proxies"] = db["proxies"][:-del_b]
                if len(db["v2ray"]) > del_b:
                    db["v2ray"] = db["v2ray"][:-del_b]
                scrape_all_channels()
            except Exception as e:
                print(f"خطا در حلقه پاکسازی: {e}")
            last_run = time.time()
        time.sleep(10)

# ==========================================
# سرور Flask
# ==========================================
app = Flask(__name__)

def get_base_url():
    render_url = os.environ.get("RENDER_EXTERNAL_URL")
    return render_url if render_url else "http://localhost:10000"

@app.route('/')
def index():
    return "✅ ربات جمع آوری پروکسی فعال است!"

@app.route('/sub/proxies')
def sub_proxies():
    clean = deduplicate_list(db["proxies"])
    return Response("\n".join(clean), mimetype='text/plain')

@app.route('/sub/v2ray')
def sub_v2ray():
    clean = deduplicate_list(db["v2ray"])
    content = base64.b64encode("\n".join(clean).encode()).decode()
    return Response(content, mimetype='text/plain')

@app.route('/sub/<sub_name>')
def sub_custom(sub_name):
    for sub_id, sub in db["subs"].items():
        if sub.get("name", "").lower() == sub_name.lower():
            clean = deduplicate_list(sub.get("data", []))
            sub_type = sub.get("type", "v2ray")
            if sub_type == "v2ray":
                content = base64.b64encode("\n".join(clean).encode()).decode()
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
    return markup

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
# دکمه‌های منوی اصلی
# ==========================================
@bot.message_handler(func=lambda m: is_admin(m.chat.id) and m.text == "🛡 پروکسی ها (MTProto)")
def btn_proxies(message):
    clear_state(message.chat.id)
    sub_link = f"{get_base_url()}/sub/proxies"
    bot.reply_to(message,
        f"🛡 **لینک سابسکریپشن پروکسی‌های تلگرام:**\n`{sub_link}`\n\n"
        f"📊 تعداد فعلی: {len(db['proxies'])} عدد",
        parse_mode="Markdown")

@bot.message_handler(func=lambda m: is_admin(m.chat.id) and m.text == "⚡️ سرور های V2ray")
def btn_v2ray(message):
    clear_state(message.chat.id)
    sub_link = f"{get_base_url()}/sub/v2ray"
    bot.reply_to(message,
        f"⚡️ **لینک سابسکریپشن سرورهای V2ray:**\n`{sub_link}`\n\n"
        f"📊 تعداد فعلی: {len(db['v2ray'])} عدد",
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

# ==========================================
# ➕ افزودن ساب — شروع فرآیند
# ==========================================
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

# ==========================================
# 📋 لیست ساب ها
# ==========================================
@bot.message_handler(func=lambda m: is_admin(m.chat.id) and m.text == "📋 لیست ساب ها")
def btn_list_subs(message):
    clear_state(message.chat.id)
    _show_subs_list(message.chat.id, send_new=True)

@bot.message_handler(func=lambda m: is_admin(m.chat.id) and m.text == "🔄 آپدیت دستی ساب‌ها")
def btn_manual_update(message):
    clear_state(message.chat.id)
    _show_manual_update_menu(message.chat.id, send_new=True)

def _show_manual_update_menu(chat_id, send_new=False, msg_id=None):
    """نمایش همه ساب‌ها (شامل ساب‌های پیش‌فرض) برای آپدیت دستی"""
    markup = types.InlineKeyboardMarkup(row_width=1)

    markup.add(
        types.InlineKeyboardButton(
            f"🛡 پروکسی‌های پیش‌فرض  ({len(db['proxies'])} لینک)",
            callback_data="manual_update:__default_proxy__"
        )
    )
    markup.add(
        types.InlineKeyboardButton(
            f"⚡️ V2ray پیش‌فرض  ({len(db['v2ray'])} لینک)",
            callback_data="manual_update:__default_v2ray__"
        )
    )

    for sub_id, sub in db.get("subs", {}).items():
        icon  = "⚡️" if sub.get("type") == "v2ray" else "🛡"
        count = len(sub.get("data", []))
        markup.add(
            types.InlineKeyboardButton(
                f"{icon} {sub['name']}  ({count} لینک)",
                callback_data=f"manual_update:{sub_id}"
            )
        )

    markup.add(types.InlineKeyboardButton("❌ بستن", callback_data="cancel_action"))

    text = (
        "🔄 **آپدیت دستی ساب‌ها**\n\n"
        "روی هر ساب بزنید تا همین الان از کانال‌های تعریف‌شده اسکن شود\n"
        "و لینک‌های جدید اضافه گردد:"
    )
    if send_new:
        bot.send_message(chat_id, text, reply_markup=markup, parse_mode="Markdown")
    else:
        try:
            bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id,
                                  reply_markup=markup, parse_mode="Markdown")
        except:
            bot.send_message(chat_id, text, reply_markup=markup, parse_mode="Markdown")

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
    if send_new:
        bot.send_message(chat_id, text, reply_markup=markup, parse_mode="Markdown")
    else:
        try:
            bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id,
                                  reply_markup=markup, parse_mode="Markdown")
        except:
            bot.send_message(chat_id, text, reply_markup=markup, parse_mode="Markdown")

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
        f"🔗 لینک ساب:\n`{safe_link}`"
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
                bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id,
                                      reply_markup=markup, parse_mode=pm)
            else:
                bot.send_message(chat_id, text, reply_markup=markup, parse_mode=pm)
            return
        except Exception:
            try:
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

    if data == "add_chan":
        set_state(chat_id, "waiting_for_add_chan")
        _edit_with_cancel(chat_id, msg_id,
            "لینک یا آیدی کانال‌ها را بفرستید. (هر خط یک کانال)")

    elif data == "del_chan":
        set_state(chat_id, "waiting_for_del_chan")
        chans = "\n".join(db["channels"]) if db["channels"] else "کانالی وجود ندارد."
        _edit_with_cancel(chat_id, msg_id,
            f"کانال‌های فعلی:\n{chans}\n\nآیدی کانال‌هایی که می‌خواهید حذف شوند را بفرستید.")

    elif data == "force_scan":
        bot.answer_callback_query(call.id, "در حال اسکن...", show_alert=True)
        p, v = scrape_all_channels()
        bot.send_message(chat_id, f"✅ اسکن تمام شد!\n+{p} پروکسی جدید\n+{v} سرور V2ray جدید")

    elif data == "set_limits":
        set_state(chat_id, "waiting_for_limits")
        _edit_with_cancel(chat_id, msg_id,
            "مقادیر جدید سقف و حذفیات را با خط تیره بفرستید.\nمثال: `400-100`")

    elif data == "set_scrape_time":
        set_state(chat_id, "waiting_for_scrape_time")
        _edit_with_cancel(chat_id, msg_id,
            "زمان بررسی کانال‌ها را به **دقیقه** بفرستید. (مثال: `60`)")

    elif data == "set_clean_time":
        set_state(chat_id, "waiting_for_clean_time")
        _edit_with_cancel(chat_id, msg_id,
            "زمان پاکسازی را به **ساعت** بفرستید. (مثال: `12`)")

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
        _edit_with_cancel(chat_id, msg_id,
            "مقادیر جدید سقف و حذفیات را با خط تیره بفرستید.\n"
            "مثال: `400-100`",
            back_data=f"sub_detail:{sub_id}")

    elif data.startswith("sub_edit_scrape:"):
        sub_id = data.split(":", 1)[1]
        set_state(chat_id, "sub_edit_scrape", {"sub_id": sub_id})
        _edit_with_cancel(chat_id, msg_id,
            "زمان بررسی کانال‌های این ساب را به **دقیقه** بفرستید. (مثال: `60`)",
            back_data=f"sub_detail:{sub_id}")

    elif data.startswith("sub_edit_clean:"):
        sub_id = data.split(":", 1)[1]
        set_state(chat_id, "sub_edit_clean", {"sub_id": sub_id})
        _edit_with_cancel(chat_id, msg_id,
            "زمان پاکسازی این ساب را به **ساعت** بفرستید. (مثال: `12`)",
            back_data=f"sub_detail:{sub_id}")

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
        _edit_with_cancel(
            chat_id, msg_id,
            f"📥 **وارد کردن از لینک ساب خارجی**\n\n"
            f"ساب مقصد: {icon} **{sub.get('name','')}**\n"
            f"نوع: {'V2ray' if sub.get('type')=='v2ray' else 'Proxy'}\n\n"
            "لینک ساب خارجی را بفرستید.\n"
            "_(لینک‌های base64 و متنی هر دو پشتیبانی می‌شوند)_",
            back_data=f"sub_detail:{sub_id}"
        )

    elif data.startswith("sub_delete_confirm:"):
        sub_id = data.split(":", 1)[1]
        sub = db["subs"].get(sub_id, {})
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("🗑 بله، حذف شود", callback_data=f"sub_delete_yes:{sub_id}"),
            types.InlineKeyboardButton("◀️ خیر، برگشت",  callback_data=f"sub_detail:{sub_id}")
        )
        try:
            bot.edit_message_text(
                f"⚠️ آیا مطمئن هستید که می‌خواهید ساب **{sub.get('name','')}** را کاملاً حذف کنید؟\n"
                "این عمل قابل بازگشت نیست!",
                chat_id=chat_id, message_id=msg_id,
                reply_markup=markup, parse_mode="Markdown")
        except:
            pass

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
        except:
            pass

    elif data == "new_sub_confirm_settings":
        st2 = get_state(chat_id)
        if st2.get("state") == "add_sub_show_settings":
            _show_new_sub_settings_menu(chat_id, msg_id, st2["data"])

    elif data == "new_sub_set_limits":
        st2 = get_state(chat_id)
        set_state(chat_id, "new_sub_waiting_limits", st2.get("data", {}))
        _edit_with_cancel(chat_id, msg_id,
            "سقف ذخیره و تعداد حذف از آخر را بفرست.\nمثال: `400-100`")

    elif data == "new_sub_set_scrape":
        st2 = get_state(chat_id)
        set_state(chat_id, "new_sub_waiting_scrape", st2.get("data", {}))
        _edit_with_cancel(chat_id, msg_id,
            "زمان بررسی کانال‌ها را به **دقیقه** بفرست. (مثال: `60`)")

    elif data == "new_sub_set_clean":
        st2 = get_state(chat_id)
        set_state(chat_id, "new_sub_waiting_clean", st2.get("data", {}))
        _edit_with_cancel(chat_id, msg_id,
            "زمان پاکسازی اجباری را به **ساعت** بفرست. (مثال: `12`)")

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
            db["proxies"], added = update_queue(db["proxies"], all_p,
                                                sett["max_limit"], sett["delete_batch"])
            save_db(db)
            bot.send_message(
                chat_id,
                f"✅ **آپدیت ساب پروکسی پیش‌فرض**\n\n"
                f"کانال‌های اسکن‌شده: {len(db['channels'])} عدد\n"
                f"لینک‌های جدید: **+{added}** عدد\n"
                f"مجموع در ساب: {len(db['proxies'])} عدد",
                parse_mode="Markdown"
            )

        elif target == "__default_v2ray__":
            all_v = []
            for ch in db["channels"]:
                _, v = scrape_channel(ch, collect_proxy=False, collect_v2ray=True)
                all_v.extend(v)
                time.sleep(0.5)
            sett = db["settings"]
            db["v2ray"], added = update_queue(db["v2ray"], all_v,
                                              sett["max_limit"], sett["delete_batch"])
            save_db(db)
            bot.send_message(
                chat_id,
                f"✅ **آپدیت ساب V2ray پیش‌فرض**\n\n"
                f"کانال‌های اسکن‌شده: {len(db['channels'])} عدد\n"
                f"لینک‌های جدید: **+{added}** عدد\n"
                f"مجموع در ساب: {len(db['v2ray'])} عدد",
                parse_mode="Markdown"
            )

        else:
            sub = db["subs"].get(target)
            if sub:
                added = _update_sub(target)
                save_db(db)
                icon = "⚡️" if sub.get("type") == "v2ray" else "🛡"
                bot.send_message(
                    chat_id,
                    f"✅ **آپدیت ساب {icon} «{sub['name']}»**\n\n"
                    f"کانال‌های اسکن‌شده: {len(sub.get('channels', []))} عدد\n"
                    f"لینک‌های جدید: **+{added}** عدد\n"
                    f"مجموع در ساب: {len(sub.get('data', []))} عدد",
                    parse_mode="Markdown"
                )
            else:
                bot.send_message(chat_id, "⚠️ ساب پیدا نشد.")

        _show_manual_update_menu(chat_id, msg_id=msg_id)

    bot.answer_callback_query(call.id)


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
                bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id,
                                      reply_markup=markup, parse_mode=pm)
            else:
                bot.send_message(chat_id, text, reply_markup=markup, parse_mode=pm)
            return
        except Exception:
            try:
                bot.send_message(chat_id, text, reply_markup=markup, parse_mode=pm)
                return
            except Exception:
                continue


def _show_new_sub_settings_menu(chat_id, msg_id, data):
    name     = data.get("name", "")
    sub_type = data.get("type", "v2ray")
    channels = data.get("channels", [])
    sett     = data.get("settings", {
        "max_limit": 400,
        "delete_batch": 100,
        "scrape_interval_mins": 60,
        "clean_interval_hours": 12
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

    try:
        bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id,
                              reply_markup=markup, parse_mode="Markdown")
    except:
        bot.send_message(chat_id, text, reply_markup=markup, parse_mode="Markdown")


def _finalize_new_sub(chat_id, msg_id, data):
    name     = data.get("name", f"sub_{int(time.time())}")
    sub_type = data.get("type", "v2ray")
    channels = data.get("channels", [])
    sett     = data.get("settings", {
        "max_limit": 400,
        "delete_batch": 100,
        "scrape_interval_mins": 60,
        "clean_interval_hours": 12
    })

    sub_id = str(uuid.uuid4())[:8]
    db["subs"][sub_id] = {
        "name": name,
        "type": sub_type,
        "channels": channels,
        "settings": sett,
        "data": []
    }
    save_db(db)

    sub_link = f"{get_base_url()}/sub/{name}"
    icon = "⚡️" if sub_type == "v2ray" else "🛡"
    clear_state(chat_id)

    text = (
        f"✅ **ساب «{name}» با موفقیت ساخته شد!** {icon}\n\n"
        f"🔗 لینک ساب شما:\n`{sub_link}`\n\n"
        f"📡 کانال‌ها: {len(channels)} عدد\n"
        "ربات از این کانال‌ها لینک جمع‌آوری می‌کند و ساب را آپدیت نگه می‌دارد.\n\n"
        "_(برای مدیریت ساب از بخش «📋 لیست ساب ها» استفاده کن)_"
    )

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔄 آپدیت فوری", callback_data=f"sub_force_update:{sub_id}"))

    try:
        bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id,
                              reply_markup=markup, parse_mode="Markdown")
    except:
        bot.send_message(chat_id, text, reply_markup=markup, parse_mode="Markdown")


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
        channels = []
        for ch in raw_chans:
            clean = ch.replace("https://t.me/", "").replace("@", "").strip()
            if clean:
                channels.append(clean)
        if not channels:
            bot.reply_to(message, "⚠️ حداقل یک کانال باید وارد کنی.")
            return

        data["channels"] = channels
        data.setdefault("settings", {
            "max_limit": 400,
            "delete_batch": 100,
            "scrape_interval_mins": 60,
            "clean_interval_hours": 12
        })
        set_state(chat_id, "add_sub_show_settings", data)

        icon = "⚡️" if data.get("type") == "v2ray" else "🛡"
        sett = data["settings"]
        text = (
            f"✅ {len(channels)} کانال ثبت شد.\n\n"
            f"✨ **تنظیمات ساب «{data['name']}»** {icon}\n\n"
            f"⚙️ سقف ذخیره: {sett['max_limit']} | حذف از آخر: {sett['delete_batch']}\n"
            f"⏱ بررسی: هر {sett['scrape_interval_mins']} دقیقه\n"
            f"🧹 پاکسازی: هر {sett['clean_interval_hours']} ساعت\n\n"
            "می‌توانید تنظیمات را تغییر دهید یا همین الان ساب را بسازید:"
        )
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
        bot.reply_to(message, text, reply_markup=markup, parse_mode="Markdown")

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
            bot.reply_to(message, f"✅ زمان بررسی روی {text_in} دقیقه تنظیم شد.")
            _show_new_sub_settings_menu(chat_id, None, data)
        except:
            bot.reply_to(message, "⚠️ فقط عدد بفرستید.")

    elif state == "new_sub_waiting_clean":
        try:
            data.setdefault("settings", {})["clean_interval_hours"] = int(text_in)
            set_state(chat_id, "add_sub_show_settings", data)
            bot.reply_to(message, f"✅ زمان پاکسازی روی {text_in} ساعت تنظیم شد.")
            _show_new_sub_settings_menu(chat_id, None, data)
        except:
            bot.reply_to(message, "⚠️ فقط عدد بفرستید.")

    elif state == "sub_edit_chan":
        sub_id = data.get("sub_id")
        raw_chans = text_in.split("\n")
        channels = []
        for ch in raw_chans:
            clean = ch.replace("https://t.me/", "").replace("@", "").strip()
            if clean:
                channels.append(clean)
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
            bot.reply_to(message, "⚠️ فرمت اشتباه. مثال: `400-100`")
        clear_state(chat_id)
        _show_sub_detail(chat_id, sub_id)

    elif state == "sub_edit_scrape":
        sub_id = data.get("sub_id")
        try:
            db["subs"][sub_id].setdefault("settings", {})["scrape_interval_mins"] = int(text_in)
            save_db(db)
            bot.reply_to(message, f"✅ زمان بررسی روی {text_in} دقیقه تنظیم شد.")
        except:
            bot.reply_to(message, "⚠️ فقط عدد بفرستید.")
        clear_state(chat_id)
        _show_sub_detail(chat_id, sub_id)

    elif state == "sub_edit_clean":
        sub_id = data.get("sub_id")
        try:
            db["subs"][sub_id].setdefault("settings", {})["clean_interval_hours"] = int(text_in)
            save_db(db)
            bot.reply_to(message, f"✅ زمان پاکسازی روی {text_in} ساعت تنظیم شد.")
        except:
            bot.reply_to(message, "⚠️ فقط عدد بفرستید.")
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
            resp = requests.get(url, headers=HEADERS, timeout=15)
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

            if sub_type == "proxy":
                links = proxies_found
            else:
                links = v2ray_found

            if not links:
                bot.edit_message_text(
                    "⚠️ هیچ لینک معتبری در این ساب پیدا نشد.\n"
                    f"_(نوع ساب: {'V2ray' if sub_type=='v2ray' else 'Proxy'})_",
                    chat_id=chat_id, message_id=wait_msg.message_id,
                    parse_mode="Markdown"
                )
                clear_state(chat_id)
                return

            sub_sett = sub.get("settings", db["settings"])
            sub["data"], added = update_queue(
                sub.get("data", []), links,
                sub_sett["max_limit"], sub_sett["delete_batch"]
            )
            save_db(db)

            icon = "⚡️" if sub_type == "v2ray" else "🛡"
            bot.edit_message_text(
                f"✅ **وارد کردن از لینک ساب انجام شد!**\n\n"
                f"ساب: {icon} **{sub['name']}**\n"
                f"لینک‌های یافت‌شده: {len(links)} عدد\n"
                f"لینک‌های جدید اضافه‌شده: **+{added}** عدد\n"
                f"مجموع در ساب: {len(sub['data'])} عدد",
                chat_id=chat_id, message_id=wait_msg.message_id,
                parse_mode="Markdown"
            )

        except requests.exceptions.RequestException as e:
            bot.edit_message_text(
                f"❌ خطا در دریافت لینک:\n`{e}`",
                chat_id=chat_id, message_id=wait_msg.message_id,
                parse_mode="Markdown"
            )
        except Exception as e:
            bot.edit_message_text(
                f"❌ خطای ناشناخته:\n`{e}`",
                chat_id=chat_id, message_id=wait_msg.message_id,
                parse_mode="Markdown"
            )

        clear_state(chat_id)
        _show_sub_detail(chat_id, sub_id)


# ==========================================
# اجرای ربات
# ==========================================
def run_telegram_bot():
    print("ربات تلگرام شروع به کار کرد...")
    bot.infinity_polling(timeout=10, long_polling_timeout=5)


if __name__ == "__main__":
    threading.Thread(target=auto_scraper_loop, daemon=True).start()
    threading.Thread(target=auto_clean_loop,   daemon=True).start()
    threading.Thread(target=run_telegram_bot,  daemon=True).start()

    port = int(os.environ.get("PORT", 10000))
    print(f"سرور وب روی پورت {port} استارت شد...")
    app.run(host='0.0.0.0', port=port)
