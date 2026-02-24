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

# ==========================================
# تنظیمات اولیه و حیاتی ربات
# ==========================================
# آیدی عددی اکانت تلگرام خودتان را اینجا بگذارید تا همیشه ادمین اصلی (غیرقابل حذف) باشید
ROOT_ADMIN_ID = 7419222963  

# توکن ربات را دقیقاً اینجا قرار دهید (بدون استفاده از متغیر اضافی)
bot = telebot.TeleBot("7632535360:AAElwqtIX521S9n_pAxo0UWRWSPkMVMdjMI")

# ==========================================
# سیستم دیتابیس (ذخیره اطلاعات در فایل JSON)
# ==========================================
DB_FILE = "database.json"

def load_db():
    default_db = {
        "admins": [],
        "channels": ["ProxyMTProto", "v2ray_configs_channel"],
        "settings": {
            "max_limit": 400,
            "delete_batch": 100,
            "scrape_interval_mins": 60,   # زمانبندی بررسی کانال ها (دقیقه)
            "clean_interval_hours": 12    # زمانبندی پاکسازی و آپدیت (ساعت)
        },
        "proxies": [],
        "v2ray": []
    }
    
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                # این حلقه برای این است که اگر دیتابیس قبلی داشتید، کلیدهای جدید به آن اضافه شود و ارور ندهد
                for k, v in default_db["settings"].items():
                    if k not in loaded.get("settings", {}):
                        loaded.setdefault("settings", {})[k] = v
                return loaded
        except:
            pass
            
    return default_db

def save_db(data):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

db = load_db()

# متغیری برای ذخیره وضعیت کاربر (برای دریافت ورودی‌های چند مرحله‌ای)
user_states = {}

# ==========================================
# توابع مربوط به استخراج و مدیریت صف
# ==========================================
PROXY_REGEX = r'(?:https?://t\.me/proxy\?server=|tg://proxy\?server=)[^\s<>"\'\\]+'
V2RAY_REGEX = r'(?:vless|vmess|ss|trojan)://[^\s<>"\'\\]+'

def update_queue(current_list, new_items):
    """
    این تابع پروکسی‌های جدید را به اول صف اضافه می‌کند.
    اگر تعداد از سقف مشخص شده بیشتر شد، از آخر صف (قدیمی‌ها) پاک می‌کند.
    """
    settings = db["settings"]
    max_limit = settings["max_limit"]
    delete_batch = settings["delete_batch"]
    
    added_count = 0
    # آیتم‌های جدید را برعکس می‌خوانیم تا ترتیب آن‌ها در اول صف درست بماند
    for item in reversed(new_items):
        if item not in current_list:
            current_list.insert(0, item) # اضافه کردن به اول صف
            added_count += 1
            
    # بررسی محدودیت و حذف از آخر صف
    if len(current_list) > max_limit:
        keep_amount = max_limit - delete_batch
        if keep_amount < 0:
            keep_amount = 0
        current_list = current_list[:keep_amount] 
        
    return current_list, added_count

def scrape_all_channels():
    print("شروع اسکن خودکار کانال‌ها...")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    new_proxies = []
    new_v2ray = []
    
    for channel in db["channels"]:
        url = f"https://t.me/s/{channel.replace('@', '')}"
        try:
            response = requests.get(url, headers=headers, timeout=10)
            if response.status_code == 200:
                html = response.text
                
                p_links = re.findall(PROXY_REGEX, html)
                for link in p_links:
                    new_proxies.append(link.replace("&amp;", "&").strip())
                    
                v_links = re.findall(V2RAY_REGEX, html)
                for link in v_links:
                    new_v2ray.append(link.replace("&amp;", "&").strip())
        except Exception as e:
            print(f"خطا در اسکن {channel}: {e}")
            
        time.sleep(1)
        
    db["proxies"], p_added = update_queue(db["proxies"], new_proxies)
    db["v2ray"], v_added = update_queue(db["v2ray"], new_v2ray)
    save_db(db)
    
    return p_added, v_added

# ==========================================
# حلقه‌های زمان‌بندی (Threads)
# ==========================================
def auto_scraper_loop():
    """حلقه بررسی مداوم کانال‌ها (بر اساس دقیقه تنظیم شده)"""
    last_run = time.time()
    while True:
        mins = db["settings"].get("scrape_interval_mins", 60)
        # بررسی اینکه آیا زمان مشخص شده گذشته است یا خیر
        if time.time() - last_run >= (mins * 60):
            try:
                scrape_all_channels()
            except Exception as e:
                print(f"خطا در اسکریپر خودکار: {e}")
            last_run = time.time()
        
        time.sleep(10) # هر 10 ثانیه یک چک کوچک انجام میدهد تا اگر تنظیمات تغییر کرد سریع اعمال شود

def auto_clean_loop():
    """حلقه پاکسازی اجباری قدیمی‌ها و افزودن جدیدها (بر اساس ساعت تنظیم شده)"""
    last_run = time.time()
    while True:
        hours = db["settings"].get("clean_interval_hours", 12)
        if time.time() - last_run >= (hours * 3600):
            try:
                print("شروع عملیات پاکسازی اجباری و آپدیت صف...")
                del_batch = db["settings"]["delete_batch"]
                
                # حذف اجباری قدیمی‌ترین‌ها (از آخر صف)
                if len(db["proxies"]) > del_batch:
                    db["proxies"] = db["proxies"][:-del_batch]
                if len(db["v2ray"]) > del_batch:
                    db["v2ray"] = db["v2ray"][:-del_batch]
                
                # اسکن مجدد برای پر کردن جای خالی با جدیدترین‌ها
                scrape_all_channels()
            except Exception as e:
                print(f"خطا در حلقه پاکسازی خودکار: {e}")
            last_run = time.time()
            
        time.sleep(10)

# ==========================================
# سرور Flask (برای لینک‌های ساب ثابت و روشن ماندن رندر)
# ==========================================
app = Flask(__name__)

def get_base_url():
    render_url = os.environ.get("RENDER_EXTERNAL_URL")
    if render_url:
        return render_url
    return "http://localhost:10000"

@app.route('/')
def index():
    return "✅ ربات جمع آوری پروکسی فعال است!"

@app.route('/sub/proxies')
def sub_proxies():
    text_content = "\n".join(db["proxies"])
    return Response(text_content, mimetype='text/plain')

@app.route('/sub/v2ray')
def sub_v2ray():
    text_content = "\n".join(db["v2ray"])
    base64_content = base64.b64encode(text_content.encode('utf-8')).decode('utf-8')
    return Response(base64_content, mimetype='text/plain')

# ==========================================
# کدهای ربات تلگرامی (پنل مدیریت)
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
    markup.add(types.KeyboardButton("📡 افزودن/حذف کانال"))
    return markup

@bot.message_handler(commands=['start'])
def send_welcome(message):
    if not is_admin(message.chat.id):
        bot.reply_to(message, "⛔️ شما اجازه دسترسی به این ربات را ندارید.")
        return
        
    user_states[message.chat.id] = None
    welcome_text = (
        "سلام مدیر عزیز! 🤖\n"
        "به پنل مدیریت سیستم سابسکریپشن خوش آمدید.\n\n"
        "از دکمه‌های زیر برای مدیریت ربات استفاده کنید:"
    )
    bot.reply_to(message, welcome_text, reply_markup=get_main_keyboard())

@bot.message_handler(func=lambda m: is_admin(m.chat.id) and m.text == "🛡 پروکسی ها (MTProto)")
def btn_proxies(message):
    user_states[message.chat.id] = None
    sub_link = f"{get_base_url()}/sub/proxies"
    text = (
        f"🛡 **لینک سابسکریپشن پروکسی‌های تلگرام:**\n"
        f"`{sub_link}`\n\n"
        f"📊 تعداد پروکسی‌های فعلی در صف: {len(db['proxies'])} عدد"
    )
    bot.reply_to(message, text, parse_mode="Markdown")

@bot.message_handler(func=lambda m: is_admin(m.chat.id) and m.text == "⚡️ سرور های V2ray")
def btn_v2ray(message):
    user_states[message.chat.id] = None
    sub_link = f"{get_base_url()}/sub/v2ray"
    text = (
        f"⚡️ **لینک سابسکریپشن سرورهای V2ray:**\n"
        f"`{sub_link}`\n\n"
        f"📊 تعداد سرورهای فعلی در صف: {len(db['v2ray'])} عدد"
    )
    bot.reply_to(message, text, parse_mode="Markdown")

@bot.message_handler(func=lambda m: is_admin(m.chat.id) and m.text == "👥 مدیریت ادمین ها")
def btn_admins(message):
    user_states[message.chat.id] = "waiting_for_admin"
    admins_str = "\n".join([str(a) for a in db["admins"]])
    if not admins_str: admins_str = "هیچ ادمین اضافه‌ای ثبت نشده."
    text = (
        f"لیست ادمین‌های فعلی:\n{admins_str}\n\n"
        "برای افزودن یا حذف یک ادمین، آیدی عددی او را بفرستید. (اگر باشد حذف می‌شود، اگر نباشد اضافه می‌شود).\n"
        "برای لغو، کلمه /start را بزنید."
    )
    bot.reply_to(message, text)

@bot.message_handler(func=lambda m: is_admin(m.chat.id) and m.text == "⚙️ تنظیمات صف")
def btn_settings(message):
    user_states[message.chat.id] = None
    sett = db["settings"]
    
    text = (
        f"⚙️ **تنظیمات فعلی ربات:**\n\n"
        f"🔹 سقف ذخیره در هر لینک: {sett['max_limit']} عدد\n"
        f"🔹 تعداد حذفیات از آخر صف: {sett['delete_batch']} عدد\n"
        f"⏱ زمانبندی بررسی کانال‌ها: هر {sett['scrape_interval_mins']} دقیقه\n"
        f"🧹 زمانبندی پاکسازی و آپدیت: هر {sett['clean_interval_hours']} ساعت\n\n"
        "برای تغییر هر بخش، از دکمه‌های زیر استفاده کنید:"
    )
    
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("⚙️ تغییر سقف و حذفیات", callback_data="set_limits"),
        types.InlineKeyboardButton("⏱ تغییر زمان بررسی (دقیقه)", callback_data="set_scrape_time"),
        types.InlineKeyboardButton("🧹 تغییر زمان پاکسازی (ساعت)", callback_data="set_clean_time"),
        types.InlineKeyboardButton("❌ بستن منو", callback_data="cancel_action")
    )
    
    bot.reply_to(message, text, reply_markup=markup, parse_mode="Markdown")

@bot.message_handler(func=lambda m: is_admin(m.chat.id) and m.text == "📡 افزودن/حذف کانال")
def btn_channels(message):
    user_states[message.chat.id] = None
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("➕ افزودن کانال (تکی یا گروهی)", callback_data="add_chan"))
    markup.add(types.InlineKeyboardButton("➖ حذف کانال", callback_data="del_chan"))
    markup.add(types.InlineKeyboardButton("🔄 اسکن دستی (همین الان)", callback_data="force_scan"))
    bot.reply_to(message, "بخش مدیریت کانال‌ها. چه کاری می‌خواهید انجام دهید؟", reply_markup=markup)

# ==========================================
# هندلر دکمه‌های شیشه‌ای (Inline Buttons)
# ==========================================
@bot.callback_query_handler(func=lambda call: is_admin(call.message.chat.id))
def callback_inline(call):
    chat_id = call.message.chat.id
    msg_id = call.message.message_id
    
    # --- دکمه های مدیریت کانال ---
    if call.data == "add_chan":
        user_states[chat_id] = "waiting_for_add_chan"
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("❌ کنسل و برگشت", callback_data="cancel_action"))
        bot.edit_message_text("لینک کانال یا آیدی آن‌ها را بفرستید. (می‌توانید چند تا را در خطوط مختلف بفرستید)", 
                              chat_id=chat_id, message_id=msg_id, reply_markup=markup)
        
    elif call.data == "del_chan":
        user_states[chat_id] = "waiting_for_del_chan"
        chans = "\n".join(db["channels"])
        if not chans: chans = "کانالی وجود ندارد."
        text = f"لیست کانال‌های فعلی:\n\n{chans}\n\nبرای حذف، لینک یا آیدی کانال‌هایی که می‌خواهید حذف شوند را بفرستید."
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("❌ کنسل و برگشت", callback_data="cancel_action"))
        bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id, reply_markup=markup)
        
    elif call.data == "force_scan":
        bot.answer_callback_query(call.id, "در حال اسکن کانال‌ها... این کار ممکن است کمی طول بکشد.", show_alert=True)
        p_count, v_count = scrape_all_channels()
        bot.send_message(chat_id, f"✅ اسکن دستی تمام شد!\n{p_count} پروکسی جدید و {v_count} سرور V2ray جدید اضافه شد.")
        
    # --- دکمه های تنظیمات ---
    elif call.data == "set_limits":
        user_states[chat_id] = "waiting_for_limits"
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("❌ کنسل و برگشت", callback_data="cancel_action"))
        text = "مقادیر جدید سقف و حذفیات را با خط تیره بفرستید.\nمثال: `400-100`"
        bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id, reply_markup=markup, parse_mode="Markdown")
        
    elif call.data == "set_scrape_time":
        user_states[chat_id] = "waiting_for_scrape_time"
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("❌ کنسل و برگشت", callback_data="cancel_action"))
        text = "لطفاً زمان بررسی کانال‌ها را به **دقیقه** ارسال کنید. (مثلاً: `60` برای یک ساعت)"
        bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id, reply_markup=markup, parse_mode="Markdown")
        
    elif call.data == "set_clean_time":
        user_states[chat_id] = "waiting_for_clean_time"
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("❌ کنسل و برگشت", callback_data="cancel_action"))
        text = "لطفاً زمان پاکسازی اجباری و آپدیت صف را به **ساعت** ارسال کنید. (مثلاً: `12` برای دوازده ساعت)"
        bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id, reply_markup=markup, parse_mode="Markdown")
        
    # --- دکمه لغو ---
    elif call.data == "cancel_action":
        user_states[chat_id] = None
        bot.edit_message_text("عملیات لغو شد. به منوی اصلی برگشتیم.", chat_id=chat_id, message_id=msg_id)


# ==========================================
# هندلر دریافت ورودی‌های متنی (State Machine)
# ==========================================
@bot.message_handler(func=lambda m: is_admin(m.chat.id) and user_states.get(m.chat.id) is not None)
def handle_states(message):
    state = user_states[message.chat.id]
    
    if state == "waiting_for_admin":
        try:
            new_id = int(message.text.strip())
            if new_id in db["admins"]:
                db["admins"].remove(new_id)
                bot.reply_to(message, f"✅ ادمین {new_id} حذف شد.")
            else:
                db["admins"].append(new_id)
                bot.reply_to(message, f"✅ ادمین {new_id} اضافه شد.")
            save_db(db)
        except:
            bot.reply_to(message, "⚠️ لطفا فقط عدد ارسال کنید.")
        user_states[message.chat.id] = None
            
    elif state == "waiting_for_limits":
        try:
            parts = message.text.split("-")
            max_l = int(parts[0].strip())
            del_b = int(parts[1].strip())
            db["settings"]["max_limit"] = max_l
            db["settings"]["delete_batch"] = del_b
            save_db(db)
            bot.reply_to(message, "✅ تنظیمات سقف و حذفیات با موفقیت ذخیره شد.")
        except:
            bot.reply_to(message, "⚠️ فرمت اشتباه است. لطفا به شکل عدد-عدد بفرستید. مثال: 400-100")
        user_states[message.chat.id] = None
        
    elif state == "waiting_for_scrape_time":
        try:
            mins = int(message.text.strip())
            db["settings"]["scrape_interval_mins"] = mins
            save_db(db)
            bot.reply_to(message, f"✅ زمان بررسی کانال‌ها روی هر {mins} دقیقه تنظیم شد.")
        except:
            bot.reply_to(message, "⚠️ لطفاً فقط یک عدد صحیح بفرستید.")
        user_states[message.chat.id] = None
        
    elif state == "waiting_for_clean_time":
        try:
            hours = int(message.text.strip())
            db["settings"]["clean_interval_hours"] = hours
            save_db(db)
            bot.reply_to(message, f"✅ زمان پاکسازی اجباری روی هر {hours} ساعت تنظیم شد.")
        except:
            bot.reply_to(message, "⚠️ لطفاً فقط یک عدد صحیح بفرستید.")
        user_states[message.chat.id] = None
            
    elif state == "waiting_for_add_chan":
        new_channels = message.text.split("\n")
        added = 0
        for ch in new_channels:
            clean_ch = ch.replace("https://t.me/", "").replace("@", "").strip()
            if clean_ch and clean_ch not in db["channels"]:
                db["channels"].append(clean_ch)
                added += 1
        save_db(db)
        bot.reply_to(message, f"✅ تعداد {added} کانال به لیست اضافه شد.")
        user_states[message.chat.id] = None
        
    elif state == "waiting_for_del_chan":
        del_channels = message.text.split("\n")
        removed = 0
        for ch in del_channels:
            clean_ch = ch.replace("https://t.me/", "").replace("@", "").strip()
            if clean_ch in db["channels"]:
                db["channels"].remove(clean_ch)
                removed += 1
        save_db(db)
        bot.reply_to(message, f"✅ تعداد {removed} کانال از لیست حذف شد.")
        user_states[message.chat.id] = None


def run_telegram_bot():
    print("ربات تلگرام شروع به کار کرد...")
    bot.infinity_polling(timeout=10, long_polling_timeout=5)


if __name__ == "__main__":
    # اجرای حلقه‌های زمان‌بندی در نَخ‌های (Threads) جداگانه
    threading.Thread(target=auto_scraper_loop, daemon=True).start()
    threading.Thread(target=auto_clean_loop, daemon=True).start()
    
    # اجرای ربات در یک Thread جداگانه 
    threading.Thread(target=run_telegram_bot, daemon=True).start()
    
    # اجرای سرور وب برای تایید سلامت برنامه در سایت رندر و ساخت لینک سابسکریپشن
    port = int(os.environ.get("PORT", 10000))
    print(f"سرور وب روی پورت {port} استارت شد...")
    app.run(host='0.0.0.0', port=port)