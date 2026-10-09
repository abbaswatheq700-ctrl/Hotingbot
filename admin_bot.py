import os
import time
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton

# Configure these as Railway Variables. Never commit tokens to GitHub.
ADMIN_BOT_TOKEN = os.getenv("ADMIN_BOT_TOKEN", "").strip()
HOSTING_BOT_TOKEN = os.getenv("HOSTING_BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "8983540044"))
DB_PATH = os.getenv("DB_PATH", "hosting_admin.sqlite3")

if not ADMIN_BOT_TOKEN:
    raise RuntimeError("Missing ADMIN_BOT_TOKEN environment variable.")
if not HOSTING_BOT_TOKEN:
    raise RuntimeError("Missing HOSTING_BOT_TOKEN environment variable.")

admin_bot = telebot.TeleBot(ADMIN_BOT_TOKEN, parse_mode="HTML")
customer_bot = telebot.TeleBot(HOSTING_BOT_TOKEN, parse_mode="HTML")
state = {}
db_lock = threading.RLock()


def connect():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def now():
    return datetime.now(timezone.utc)


def dt_text(value):
    try:
        return datetime.fromisoformat(value).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return value


def init_db():
    with db_lock:
        con = connect()
        cur = con.cursor()
        cur.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, username TEXT DEFAULT '',
            first_name TEXT DEFAULT '', joined_at TEXT NOT NULL
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS plans (
            plan_key TEXT PRIMARY KEY, title TEXT NOT NULL, price TEXT NOT NULL,
            duration_days INTEGER NOT NULL, enabled INTEGER NOT NULL DEFAULT 1
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            plan_key TEXT NOT NULL, starts_at TEXT NOT NULL, ends_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active', notified_1day INTEGER NOT NULL DEFAULT 0
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            amount TEXT NOT NULL, description TEXT NOT NULL, created_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'sent'
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS menu_buttons (
            button_key TEXT PRIMARY KEY, label TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, value TEXT NOT NULL
        )""")
        plans = [
            ("daily", "يومي", "1000", 1, 1),
            ("weekly", "أسبوعي", "5000", 7, 1),
            ("monthly", "شهري", "15000", 30, 1),
            ("six_months", "6 أشهر", "70000", 180, 1),
            ("yearly", "سنوي", "120000", 365, 1),
            ("permanent", "دائم", "200000", 36500, 1),
        ]
        for row in plans:
            cur.execute("INSERT OR IGNORE INTO plans VALUES(?,?,?,?,?)", row)
        buttons = [
            ("plans", "💳 الاشتراكات", 1),
            ("my_bots", "🤖 بوتاتي", 1),
            ("upload", "📤 رفع بوت", 1),
            ("help", "📋 التعليمات", 1),
        ]
        for row in buttons:
            cur.execute("INSERT OR IGNORE INTO menu_buttons VALUES(?,?,?)", row)
        cur.execute("INSERT OR IGNORE INTO settings VALUES('welcome_text', ?)",
                    ("أهلاً بك في بوت الاستضافة. اختار من الأزرار أدناه.",))
        con.commit()
        con.close()


def allowed(message):
    return bool(message.from_user and message.from_user.id == ADMIN_ID)


def admin_fixed_keyboard():
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(KeyboardButton("🛠 لوحة التحكم"), KeyboardButton("📊 الإحصائيات"))
    kb.row(KeyboardButton("➕ إضافة مستخدم"), KeyboardButton("📤 استيراد آيديات"))
    kb.row(KeyboardButton("❓ المساعدة"))
    return kb


def inline_home():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(InlineKeyboardButton("💰 أسعار الاشتراكات", callback_data="prices"),
           InlineKeyboardButton("🧩 تعديل أزرار المستخدمين", callback_data="buttons"))
    kb.add(InlineKeyboardButton("🧾 إرسال فاتورة", callback_data="invoice"),
           InlineKeyboardButton("📅 إضافة/تجديد اشتراك", callback_data="subscription"))
    kb.add(InlineKeyboardButton("📣 إذاعة", callback_data="broadcast"),
           InlineKeyboardButton("👥 قائمة المستخدمين", callback_data="users"))
    kb.add(InlineKeyboardButton("⏳ اشتراكات قريبة من الانتهاء", callback_data="expiring"),
           InlineKeyboardButton("✏️ نص الترحيب", callback_data="welcome"))
    kb.add(InlineKeyboardButton("📥 تصدير آيديات المستخدمين", callback_data="export_users"))
    return kb


def home(chat_id, message_id=None):
    text = "🛠 <b>لوحة إدارة بوت الاستضافة</b>\nالأزرار الثابتة بالأسفل للاختصارات، والشفافة للتحكم التفصيلي."
    if message_id:
        admin_bot.edit_message_text(text, chat_id, message_id, reply_markup=inline_home())
    else:
        admin_bot.send_message(chat_id, text, reply_markup=inline_home())


def plans_keyboard():
    con = connect()
    rows = con.execute("SELECT * FROM plans ORDER BY duration_days").fetchall()
    con.close()
    kb = InlineKeyboardMarkup(row_width=1)
    for p in rows:
        status = "✅" if p["enabled"] else "⛔"
        kb.add(InlineKeyboardButton(
            f"{status} {p['title']} — {p['price']} د.ع",
            callback_data=f"plan:{p['plan_key']}"
        ))
    kb.add(InlineKeyboardButton("⬅️ رجوع", callback_data="home"))
    return kb


def buttons_keyboard():
    con = connect()
    rows = con.execute("SELECT * FROM menu_buttons ORDER BY button_key").fetchall()
    con.close()
    kb = InlineKeyboardMarkup(row_width=1)
    for b in rows:
        visible = "ظاهر" if b["enabled"] else "مخفي"
        kb.add(InlineKeyboardButton(f"{b['label']} — {visible}", callback_data=f"button:{b['button_key']}"))
    kb.add(InlineKeyboardButton("✏️ تغيير اسم زر", callback_data="rename_button"))
    kb.add(InlineKeyboardButton("⬅️ رجوع", callback_data="home"))
    return kb


def send_safe(user_id, text):
    try:
        customer_bot.send_message(user_id, text)
        return True
    except Exception:
        return False


@admin_bot.message_handler(commands=["start", "admin"])
def start(message):
    if not allowed(message):
        admin_bot.reply_to(message, "⛔ هذا البوت خاص بالمدير فقط.")
        return
    admin_bot.send_message(message.chat.id, "أهلاً بيك بلوحة إدارة الاستضافة.", reply_markup=admin_fixed_keyboard())
    home(message.chat.id)


@admin_bot.message_handler(func=lambda m: m.text == "🛠 لوحة التحكم")
def fixed_home(message):
    if allowed(message):
        home(message.chat.id)


@admin_bot.message_handler(func=lambda m: m.text == "📊 الإحصائيات")
def stats(message):
    if not allowed(message):
        return
    con = connect()
    users = con.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
    subs = con.execute("SELECT COUNT(*) n FROM subscriptions WHERE status='active'").fetchone()["n"]
    invoices = con.execute("SELECT COUNT(*) n FROM invoices").fetchone()["n"]
    con.close()
    admin_bot.send_message(message.chat.id, f"📊 <b>الإحصائيات</b>\nالمستخدمون المسجلون: {users}\nالاشتراكات الفعالة: {subs}\nالفواتير المنشأة: {invoices}")


@admin_bot.message_handler(func=lambda m: m.text == "➕ إضافة مستخدم")
def fixed_add_user(message):
    if not allowed(message):
        return
    state[ADMIN_ID] = {"action": "add_user"}
    admin_bot.send_message(message.chat.id, "أرسل آيدي المستخدم. لازم يكون المستخدم قد بدأ المحادثة مع بوت الاستضافة حتى يگدر يستلم رسائل.")


@admin_bot.message_handler(func=lambda m: m.text == "📤 استيراد آيديات")
def fixed_import(message):
    if not allowed(message):
        return
    state[ADMIN_ID] = {"action": "import_users"}
    admin_bot.send_message(message.chat.id, "أرسل آيديات المستخدمين، كل آيدي بسطر منفصل. راح تنضاف للقائمة حتى تجرب الإرسال إلهم.")


@admin_bot.message_handler(func=lambda m: m.text == "❓ المساعدة")
def help_cmd(message):
    if allowed(message):
        admin_bot.send_message(message.chat.id,
            "الاستخدام:\n/start لفتح اللوحة\n"
            "الأزرار الثابتة للاختصارات، والأزرار الشفافة للإعدادات التفصيلية.\n"
            "مهم: حتى تظهر كل مستخدمي بوت الاستضافة بالإذاعة، لازم بوت الاستضافة يسجل آيدياتهم في نفس قاعدة البيانات أو تستورد الآيديات هنا.")


@admin_bot.callback_query_handler(func=lambda c: True)
def callbacks(call):
    if not call.from_user or call.from_user.id != ADMIN_ID:
        admin_bot.answer_callback_query(call.id, "غير مخوّل", show_alert=True)
        return
    admin_bot.answer_callback_query(call.id)
    data = call.data or ""
    chat_id = call.message.chat.id
    msg_id = call.message.message_id

    if data == "home":
        home(chat_id, msg_id)
    elif data == "prices":
        admin_bot.edit_message_text("💰 اختار خطة لتعديل سعرها أو تفعيلها/إيقافها:", chat_id, msg_id, reply_markup=plans_keyboard())
    elif data.startswith("plan:"):
        key = data.split(":", 1)[1]
        con = connect()
        p = con.execute("SELECT * FROM plans WHERE plan_key=?", (key,)).fetchone()
        con.close()
        if not p:
            return
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("💵 تعديل السعر", callback_data=f"price:{key}"))
        kb.add(InlineKeyboardButton("🔁 تفعيل/إيقاف الخطة", callback_data=f"toggleplan:{key}"))
        kb.add(InlineKeyboardButton("⬅️ رجوع", callback_data="prices"))
        admin_bot.edit_message_text(f"الخطة: <b>{p['title']}</b>\nالسعر: <b>{p['price']} د.ع</b>\nالمدة: {p['duration_days']} يوم", chat_id, msg_id, reply_markup=kb)
    elif data.startswith("price:"):
        key = data.split(":", 1)[1]
        state[ADMIN_ID] = {"action": "edit_price", "plan_key": key}
        admin_bot.send_message(chat_id, "أرسل السعر الجديد بالأرقام فقط (دينار عراقي).")
    elif data.startswith("toggleplan:"):
        key = data.split(":", 1)[1]
        con = connect()
        p = con.execute("SELECT enabled FROM plans WHERE plan_key=?", (key,)).fetchone()
        if p:
            con.execute("UPDATE plans SET enabled=? WHERE plan_key=?", (0 if p["enabled"] else 1, key))
            con.commit()
        con.close()
        admin_bot.send_message(chat_id, "✅ تم تغيير حالة الخطة.")
    elif data == "buttons":
        admin_bot.edit_message_text("🧩 اضغط الزر لإظهاره أو إخفائه. تغيير الأسماء من خيار تغيير اسم زر.", chat_id, msg_id, reply_markup=buttons_keyboard())
    elif data.startswith("button:"):
        key = data.split(":", 1)[1]
        con = connect()
        b = con.execute("SELECT enabled FROM menu_buttons WHERE button_key=?", (key,)).fetchone()
        if b:
            con.execute("UPDATE menu_buttons SET enabled=? WHERE button_key=?", (0 if b["enabled"] else 1, key))
            con.commit()
        con.close()
        admin_bot.edit_message_reply_markup(chat_id, msg_id, reply_markup=buttons_keyboard())
    elif data == "rename_button":
        state[ADMIN_ID] = {"action": "rename_button"}
        admin_bot.send_message(chat_id, "أرسل مفتاح الزر ثم الاسم الجديد بهذا الشكل:\nplans | 💳 الباقات")
    elif data == "invoice":
        state[ADMIN_ID] = {"action": "invoice_user"}
        admin_bot.send_message(chat_id, "أرسل آيدي المستخدم الذي تريد إرسال فاتورة إله.")
    elif data == "subscription":
        state[ADMIN_ID] = {"action": "sub_user"}
        admin_bot.send_message(chat_id, "أرسل آيدي المستخدم لإضافة أو تجديد اشتراكه.")
    elif data == "broadcast":
        state[ADMIN_ID] = {"action": "broadcast"}
        admin_bot.send_message(chat_id, "أرسل نص الإذاعة. ستصل للمستخدمين المسجلين في قاعدة بيانات هذا البوت.")
    elif data == "users":
        con = connect()
        rows = con.execute("SELECT user_id, username, first_name FROM users ORDER BY joined_at DESC LIMIT 50").fetchall()
        total = con.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        con.close()
        lines = [f"👥 <b>المستخدمون</b> — العدد: {total}"]
        for r in rows:
            name = (r["first_name"] or r["username"] or "بدون اسم").replace("<", "").replace(">", "")
            lines.append(f"• {name} — <code>{r['user_id']}</code>")
        admin_bot.send_message(chat_id, "\n".join(lines)[:4000])
    elif data == "expiring":
        con = connect()
        rows = con.execute("""SELECT s.*, u.first_name, u.username FROM subscriptions s
            LEFT JOIN users u ON u.user_id=s.user_id WHERE s.status='active' ORDER BY s.ends_at""").fetchall()
        con.close()
        upcoming = []
        for r in rows:
            try:
                end = datetime.fromisoformat(r["ends_at"])
                if now() <= end <= now() + timedelta(days=1, hours=1):
                    upcoming.append(r)
            except Exception:
                pass
        if not upcoming:
            admin_bot.send_message(chat_id, "ماكو اشتراكات تنتهي خلال الـ24 ساعة الجاية.")
        else:
            lines = ["⏳ <b>اشتراكات قربت تنتهي:</b>"]
            for r in upcoming:
                name = r["first_name"] or r["username"] or str(r["user_id"])
                lines.append(f"• {name} — {dt_text(r['ends_at'])} — ID <code>{r['user_id']}</code>")
            admin_bot.send_message(chat_id, "\n".join(lines)[:4000])
    elif data == "welcome":
        con = connect()
        row = con.execute("SELECT value FROM settings WHERE key='welcome_text'").fetchone()
        con.close()
        state[ADMIN_ID] = {"action": "welcome"}
        admin_bot.send_message(chat_id, "النص الحالي:\n" + (row["value"] if row else "") + "\n\nأرسل النص الجديد.")
    elif data == "export_users":
        con = connect()
        ids = [str(r["user_id"]) for r in con.execute("SELECT user_id FROM users ORDER BY user_id").fetchall()]
        con.close()
        admin_bot.send_message(chat_id, "آيديات المستخدمين:\n" + ("\n".join(ids) if ids else "القائمة فارغة")[:3900])
    elif data.startswith("subplan:"):
        try:
            _, plan_key, uid_text = data.split(":", 2)
            user_id = int(uid_text)
        except Exception:
            return
        con = connect()
        plan = con.execute("SELECT * FROM plans WHERE plan_key=? AND enabled=1", (plan_key,)).fetchone()
        user = con.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not user:
            con.execute("INSERT INTO users(user_id,username,first_name,joined_at) VALUES(?,?,?,?)",
                        (user_id, "", "مضاف من الاشتراك", now().isoformat()))
        if not plan:
            con.close()
            admin_bot.send_message(chat_id, "الخطة غير متاحة.")
            return
        start_dt = now()
        end_dt = start_dt + timedelta(days=int(plan["duration_days"]))
        con.execute("INSERT INTO subscriptions(user_id,plan_key,starts_at,ends_at,status,notified_1day) VALUES(?,?,?,?,?,0)",
                    (user_id, plan_key, start_dt.isoformat(), end_dt.isoformat(), "active"))
        con.commit()
        con.close()
        send_safe(user_id, f"✅ تم تفعيل اشتراكك: <b>{plan['title']}</b>\\nينتهي بتاريخ: <b>{dt_text(end_dt.isoformat())}</b>")
        admin_bot.send_message(chat_id, f"✅ أُضيف اشتراك {plan['title']} للمستخدم {user_id}.\\nينتهي: {dt_text(end_dt.isoformat())}")


@admin_bot.message_handler(content_types=["text"])
def text_actions(message):
    if not allowed(message):
        return
    text = (message.text or "").strip()
    current = state.get(ADMIN_ID)
    if not current:
        return
    action = current.get("action")

    if action == "edit_price":
        if not text.isdigit():
            admin_bot.reply_to(message, "أرسل السعر كأرقام فقط.")
            return
        con = connect()
        con.execute("UPDATE plans SET price=? WHERE plan_key=?", (text, current["plan_key"]))
        con.commit()
        con.close()
        state.pop(ADMIN_ID, None)
        admin_bot.reply_to(message, "✅ تم تعديل السعر.")
        home(message.chat.id)
    elif action == "rename_button":
        parts = text.split("|", 1)
        if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
            admin_bot.reply_to(message, "الصيغة: plans | الاسم الجديد")
            return
        con = connect()
        cur = con.execute("UPDATE menu_buttons SET label=? WHERE button_key=?", (parts[1].strip()[:60], parts[0].strip()))
        con.commit()
        con.close()
        state.pop(ADMIN_ID, None)
        admin_bot.reply_to(message, "✅ تم تحديث اسم الزر." if cur.rowcount else "❌ مفتاح الزر غير موجود.")
        home(message.chat.id)
    elif action == "welcome":
        con = connect()
        con.execute("INSERT INTO settings(key,value) VALUES('welcome_text',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (text[:1500],))
        con.commit()
        con.close()
        state.pop(ADMIN_ID, None)
        admin_bot.reply_to(message, "✅ تم حفظ نص الترحيب. ملاحظة: بوت المستخدمين لازم يقرأ النص من قاعدة البيانات حتى يتغير عندهم.")
        home(message.chat.id)
    elif action == "invoice_user":
        if not text.isdigit():
            admin_bot.reply_to(message, "آيدي المستخدم لازم يكون أرقام.")
            return
        state[ADMIN_ID] = {"action": "invoice_amount", "user_id": int(text)}
        admin_bot.reply_to(message, "أرسل مبلغ الفاتورة بالأرقام فقط.")
    elif action == "invoice_amount":
        if not text.isdigit():
            admin_bot.reply_to(message, "أرسل المبلغ كأرقام فقط.")
            return
        state[ADMIN_ID] = {"action": "invoice_desc", "user_id": current["user_id"], "amount": text}
        admin_bot.reply_to(message, "أرسل وصف الفاتورة أو سبب الدفع.")
    elif action == "invoice_desc":
        user_id = current["user_id"]
        amount = current["amount"]
        desc = text[:500]
        con = connect()
        cur = con.execute("INSERT INTO invoices(user_id,amount,description,created_at) VALUES(?,?,?,?)",
                          (user_id, amount, desc, now().isoformat()))
        invoice_id = cur.lastrowid
        con.commit()
        con.close()
        sent = send_safe(user_id, f"🧾 <b>فاتورة استضافة #{invoice_id}</b>\nالمبلغ: <b>{amount} د.ع</b>\nالتفاصيل: {desc}\n\nراسل الإدارة لإكمال الدفع.")
        state.pop(ADMIN_ID, None)
        admin_bot.reply_to(message, "✅ تم إرسال الفاتورة." if sent else "⚠️ انحفظت الفاتورة، لكن ما كدرت أرسلها. تأكد أن المستخدم بدأ بوت الاستضافة وأن التوكن صحيح.")
        home(message.chat.id)
    elif action == "sub_user":
        if not text.isdigit():
            admin_bot.reply_to(message, "آيدي المستخدم لازم يكون أرقام.")
            return
        state[ADMIN_ID] = {"action": "sub_plan", "user_id": int(text)}
        con = connect()
        plans = con.execute("SELECT * FROM plans WHERE enabled=1 ORDER BY duration_days").fetchall()
        con.close()
        kb = InlineKeyboardMarkup(row_width=1)
        for p in plans:
            kb.add(InlineKeyboardButton(f"{p['title']} ({p['duration_days']} يوم)", callback_data=f"subplan:{p['plan_key']}:{int(text)}"))
        admin_bot.send_message(message.chat.id, "اختار مدة الاشتراك:", reply_markup=kb)
    elif action == "broadcast":
        con = connect()
        ids = [r["user_id"] for r in con.execute("SELECT user_id FROM users").fetchall()]
        con.close()
        ok = fail = 0
        for uid in ids:
            if send_safe(uid, text):
                ok += 1
            else:
                fail += 1
            time.sleep(0.04)
        state.pop(ADMIN_ID, None)
        admin_bot.reply_to(message, f"📣 انتهت الإذاعة.\nوصلت: {ok}\nفشلت: {fail}\nالمجموع: {len(ids)}")
        home(message.chat.id)
    elif action == "add_user":
        if not text.isdigit():
            admin_bot.reply_to(message, "أرسل آيدي رقمي.")
            return
        add_user(int(text), "", "مضاف يدوياً")
        state.pop(ADMIN_ID, None)
        admin_bot.reply_to(message, "✅ انضاف الآيدي للقائمة. ملاحظة: ما يگدر يستلم رسالة إلا إذا بدأ بوت الاستضافة سابقاً.")
    elif action == "import_users":
        ids = [line.strip() for line in text.splitlines() if line.strip()]
        added = 0
        for uid in ids:
            if uid.isdigit():
                add_user(int(uid), "", "مستورد")
                added += 1
        state.pop(ADMIN_ID, None)
        admin_bot.reply_to(message, f"✅ تمت إضافة {added} آيدي.")
    elif action == "sub_plan":
        # Subscription is normally created from the inline plan callback below.
        admin_bot.reply_to(message, "اختار الخطة من الأزرار التي ظهرت لك.")


def add_user(user_id, username="", first_name=""):
    with db_lock:
        con = connect()
        con.execute("""INSERT INTO users(user_id,username,first_name,joined_at) VALUES(?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name""",
            (user_id, username or "", first_name or "", now().isoformat()))
        con.commit()
        con.close()


# Subscription plan selection callbacks are handled separately.
@admin_bot.callback_query_handler(func=lambda c: (c.data or "").startswith("subplan:"))
def subscription_plan_callback(call):
    if not call.from_user or call.from_user.id != ADMIN_ID:
        admin_bot.answer_callback_query(call.id, "غير مخوّل", show_alert=True)
        return
    try:
        _, plan_key, uid_text = call.data.split(":", 2)
        user_id = int(uid_text)
    except Exception:
        admin_bot.answer_callback_query(call.id, "بيانات غير صحيحة", show_alert=True)
        return
    con = connect()
    plan = con.execute("SELECT * FROM plans WHERE plan_key=? AND enabled=1", (plan_key,)).fetchone()
    user = con.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    if not user:
        con.execute("INSERT INTO users(user_id,username,first_name,joined_at) VALUES(?,?,?,?)",
                    (user_id, "", "مضاف من الاشتراك", now().isoformat()))
    if not plan:
        con.close()
        admin_bot.answer_callback_query(call.id, "الخطة غير متاحة", show_alert=True)
        return
    start_dt = now()
    end_dt = start_dt + timedelta(days=int(plan["duration_days"]))
    con.execute("INSERT INTO subscriptions(user_id,plan_key,starts_at,ends_at,status,notified_1day) VALUES(?,?,?,?,?,0)",
                (user_id, plan_key, start_dt.isoformat(), end_dt.isoformat(), "active"))
    con.commit()
    con.close()
    send_safe(user_id, f"✅ تم تفعيل اشتراكك: <b>{plan['title']}</b>\nينتهي بتاريخ: <b>{dt_text(end_dt.isoformat())}</b>")
    admin_bot.answer_callback_query(call.id, "تم تفعيل الاشتراك")
    admin_bot.send_message(call.message.chat.id, f"✅ أُضيف اشتراك {plan['title']} للمستخدم {user_id}.\nينتهي: {dt_text(end_dt.isoformat())}")


def reminder_worker():
    while True:
        try:
            current = now()
            lower = current + timedelta(hours=23)
            upper = current + timedelta(hours=25)
            con = connect()
            rows = con.execute("""SELECT s.*, u.username, u.first_name FROM subscriptions s
                LEFT JOIN users u ON u.user_id=s.user_id
                WHERE s.status='active' AND s.notified_1day=0""").fetchall()
            for r in rows:
                try:
                    end = datetime.fromisoformat(r["ends_at"])
                    if lower <= end <= upper:
                        display = r["first_name"] or r["username"] or str(r["user_id"])
                        admin_bot.send_message(ADMIN_ID,
                            f"⏰ <b>تنبيه انتهاء اشتراك</b>\nالمستخدم: {display}\nآيدي: <code>{r['user_id']}</code>\nينتهي: {dt_text(r['ends_at'])}")
                        con.execute("UPDATE subscriptions SET notified_1day=1 WHERE id=?", (r["id"],))
                except Exception:
                    continue
            con.commit()
            con.close()
        except Exception as exc:
            print("Reminder worker error:", repr(exc))
        time.sleep(300)


init_db()
threading.Thread(target=reminder_worker, daemon=True).start()

print("Admin bot started. Only ADMIN_ID can use its controls.")
admin_bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
