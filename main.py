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
PROXY_REGEX   = r'(?:https?://t\.me/proxy\?server=|tg://proxy\?server=)[^\s<>"\'\\]+'
V2RAY_REGEX   = r'(?:vless|vmess|ss|trojan)://[^\s<>"\'\\]+'
TXT_CDN_REGEX = r'(https://cdn\d*\.telegram\.org/file/[A-Za-z0-9_\-]+)'

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

def try_download_txt_files(html):
    """
    سعی می‌کند فایل‌های .txt از CDN تلگرام را دانلود کند
    و سرورهای v2ray داخل آن‌ها را برگرداند.
    """
    v2ray_found = []
    cdn_links = list(set(re.findall(TXT_CDN_REGEX, html)))

    # فقط لینک‌هایی که احتمال txt بودن دارند (نزدیک به کلمه .txt در html)
    for link in cdn_links[:15]:
        try:
            r = requests.get(link, headers=HEADERS, timeout=8)
            if r.status_code == 200:
                content_type = r.headers.get("Content-Type", "")
                # فقط فایل‌های متنی یا بدون نوع مشخص را بخوان
                if "text" in content_type or "octet-stream" in content_type or content_type == "":
                    if len(r.content) < 5 * 1024 * 1024:  # حداکثر 5 مگابایت
                        _, v2 = extract_configs(r.text)
                        v2ray_found.extend(v2)
        except:
            pass

    return v2ray_found

def scrape_channel(channel, collect_proxy=True, collect_v2ray=True):
    """یک کانال را اسکن می‌کند و لینک‌های پیداشده را برمی‌گرداند."""
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
                # بررسی فایل‌های txt داخل کانال
                txt_servers = try_download_txt_files(html)
                new_v2ray.extend(txt_servers)

    except Exception as e:
        print(f"خطا در اسکن {channel}: {e}")

    return new_proxies, new_v2ray

def normalize_link(link: str) -> str:
    """
    لینک را نرمالایز می‌کند تا مقایسه تکراری بودن دقیق‌تر باشد.
    - به lowercase تبدیل می‌کند (برای بخش پروتکل و هاست)
    - فاصله‌های اضافه را حذف می‌کند
    - برای لینک‌های دارای query string پارامترها را مرتب می‌کند
    """
    link = link.strip()
    try:
        from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
        parsed = urlparse(link)
        # scheme و netloc را lowercase کن
        scheme   = parsed.scheme.lower()
        netloc   = parsed.netloc.lower()
        path     = parsed.path
        # پارامترها را مرتب کن تا ترتیب متفاوت = تکراری شناخته شود
        params   = urlencode(sorted(parse_qs(parsed.query, keep_blank_values=True).items()))
        fragment = parsed.fragment
        return urlunparse((scheme, netloc, path, parsed.params, params, fragment))
    except Exception:
        return link.lower()


def update_queue(current_list, new_items, max_limit, delete_batch):
    """
    آیتم‌های جدید را به اول صف اضافه می‌کند.
    از نرمالایز کردن لینک‌ها برای جلوگیری از ورود هر نوع تکراری استفاده می‌کند.
    """
    # یک set از نسخه نرمالایزشده آیتم‌های فعلی برای مقایسه سریع
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
    return Response("\n".join(db["proxies"]), mimetype='text/plain')

@app.route('/sub/v2ray')
def sub_v2ray():
    content = base64.b64encode("\n".join(db["v2ray"]).encode()).decode()
    return Response(content, mimetype='text/plain')

@app.route('/sub/<sub_name>')
def sub_custom(sub_name):
    for sub_id, sub in db["subs"].items():
        if sub.get("name", "").lower() == sub_name.lower():
            data = sub.get("data", [])
            sub_type = sub.get("type", "v2ray")
            if sub_type == "v2ray":
                content = base64.b64encode("\n".join(data).encode()).decode()
            else:
                content = "\n".join(data)
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
    markup.add(types.KeyboardButton("📋 لیست ساب ها"))
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

    sett  = sub.get("settings", {})
    chans = sub.get("channels", [])
    count = len(sub.get("data", []))
    icon  = "⚡️" if sub.get("type") == "v2ray" else "🛡"
    sub_link = f"{get_base_url()}/sub/{sub['name']}"

    text = (
        f"{icon} **ساب: {sub['name']}**\n"
        f"نوع: {'V2ray' if sub['type']=='v2ray' else 'Proxy'}\n\n"
        f"📡 کانال‌ها ({len(chans)}):\n" +
        ("\n".join([f"• {c}" for c in chans]) if chans else "• ندارد") +
        f"\n\n"
        f"⚙️ سقف ذخیره: {sett.get('max_limit', 400)}\n"
        f"🗑 حذف از آخر: {sett.get('delete_batch', 100)}\n"
        f"⏱ بررسی: هر {sett.get('scrape_interval_mins', 60)} دقیقه\n"
        f"🧹 پاکسازی: هر {sett.get('clean_interval_hours', 12)} ساعت\n\n"
        f"📊 لینک‌های فعلی: {count} عدد\n"
        f"🔗 لینک ساب:\n`{sub_link}`"
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
        types.InlineKeyboardButton("🔄 آپدیت دستی",  callback_data=f"sub_force_update:{sub_id}"),
        types.InlineKeyboardButton("🗑 حذف این ساب", callback_data=f"sub_delete_confirm:{sub_id}")
    )
    markup.add(types.InlineKeyboardButton("◀️ بازگشت به لیست", callback_data="back_to_subs"))

    if msg_id:
        try:
            bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id,
                                  reply_markup=markup, parse_mode="Markdown")
            return
        except:
            pass
    bot.send_message(chat_id, text, reply_markup=markup, parse_mode="Markdown")

# ==========================================
# هندلر Callback Query — مرکزی
# ==========================================
@bot.callback_query_handler(func=lambda call: is_admin(call.message.chat.id))
def callback_inline(call):
    chat_id = call.message.chat.id
    msg_id  = call.message.message_id
    data    = call.data

    # ================== کانال پیش‌فرض ==================
    if data == "add_chan":
        set_state(chat_id, "waiting_for_add_chan")
        _edit_with_cancel(chat_id, msg_id,
            "لینک یا آیدی کانال‌ها را بفرستید. (هر خط یک کانال)")

    elif data == "del_chan":
        set_state(chat_id, "waiting_for_del_chan")
        chans = "\n".join(db["channels"]) or "کانالی وجود ندارد."
        _edit_with_cancel(chat_id, msg_id,
            f"کانال‌های فعلی:\n{chans}\n\nآیدی کانال‌هایی که می‌خواهید حذف شوند را بفرستید.")

    elif data == "force_scan":
        bot.answer_callback_query(call.id, "در حال اسکن...", show_alert=True)
        p, v = scrape_all_channels()
        bot.send_message(chat_id, f"✅ اسکن تمام شد!\n+{p} پروکسی جدید\n+{v} سرور V2ray جدید")

    # ================== تنظیمات ==================
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

    # ================== ساخت ساب جدید ==================
    elif data in ("new_sub_proxy", "new_sub_v2ray"):
        sub_type = "proxy" if data == "new_sub_proxy" else "v2ray"
        set_state(chat_id, "add_sub_name", {"type": sub_type})
        icon = "🛡" if sub_type == "proxy" else "⚡️"
        _edit_with_cancel(chat_id, msg_id,
            f"{icon} نوع ساب: **{'Proxy' if sub_type=='proxy' else 'V2ray'}**\n\n"
            "حالا یک **اسم** برای این ساب بنویس.\n"
            "_(فقط حروف انگلیسی، اعداد و خط تیره — این اسم در لینک ساب استفاده می‌شود)_")

    # ================== جزئیات و ویرایش ساب ==================
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

    # ================== بعد از وارد کردن کانال‌ها در ساخت ساب جدید ==================
    elif data == "new_sub_confirm_settings":
        st = get_state(chat_id)
        if st.get("state") == "add_sub_show_settings":
            _show_new_sub_settings_menu(chat_id, msg_id, st["data"])

    bot.answer_callback_query(call.id)


def _edit_with_cancel(chat_id, msg_id, text, back_data=None):
    markup = types.InlineKeyboardMarkup()
    if back_data:
        markup.add(types.InlineKeyboardButton("◀️ برگشت", callback_data=back_data))
    markup.add(types.InlineKeyboardButton("❌ کنسل", callback_data="cancel_action"))
    try:
        bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id,
                              reply_markup=markup, parse_mode="Markdown")
    except:
        bot.send_message(chat_id, text, reply_markup=markup, parse_mode="Markdown")


def _show_new_sub_settings_menu(chat_id, msg_id, data):
    """نمایش منوی تنظیمات هنگام ساخت ساب جدید"""
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


# هندلر callback برای تنظیمات ساخت ساب جدید (باید مجزا باشد)
@bot.callback_query_handler(func=lambda call: is_admin(call.message.chat.id) and
                             call.data in ("new_sub_set_limits", "new_sub_set_scrape",
                                           "new_sub_set_clean", "new_sub_create"))
def callback_new_sub_settings(call):
    chat_id = call.message.chat.id
    msg_id  = call.message.message_id
    data    = call.data
    st      = get_state(chat_id)

    if data == "new_sub_set_limits":
        set_state(chat_id, "new_sub_waiting_limits", st.get("data", {}))
        _edit_with_cancel(chat_id, msg_id,
            "سقف ذخیره و تعداد حذف از آخر را بفرست.\nمثال: `400-100`")

    elif data == "new_sub_set_scrape":
        set_state(chat_id, "new_sub_waiting_scrape", st.get("data", {}))
        _edit_with_cancel(chat_id, msg_id,
            "زمان بررسی کانال‌ها را به **دقیقه** بفرست. (مثال: `60`)")

    elif data == "new_sub_set_clean":
        set_state(chat_id, "new_sub_waiting_clean", st.get("data", {}))
        _edit_with_cancel(chat_id, msg_id,
            "زمان پاکسازی اجباری را به **ساعت** بفرست. (مثال: `12`)")

    elif data == "new_sub_create":
        _finalize_new_sub(chat_id, msg_id, st.get("data", {}))

    bot.answer_callback_query(call.id)


def _finalize_new_sub(chat_id, msg_id, data):
    """ساب را ذخیره و لینک را نمایش می‌دهد."""
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

    # ===== تنظیمات پیش‌فرض =====
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

    # ===== ساخت ساب جدید — مرحله اسم =====
    elif state == "add_sub_name":
        # اعتبارسنجی اسم
        clean_name = re.sub(r'[^a-zA-Z0-9\-_]', '', text_in.strip())
        if not clean_name:
            bot.reply_to(message, "⚠️ اسم باید فقط شامل حروف انگلیسی، اعداد و - باشد.")
            return
        # بررسی تکراری نبودن
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

    # ===== ساخت ساب جدید — مرحله کانال‌ها =====
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

        # ارسال پیام جدید با منوی تنظیمات
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

    # ===== تنظیمات هنگام ساخت ساب جدید =====
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

    # ===== ویرایش ساب موجود =====
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
