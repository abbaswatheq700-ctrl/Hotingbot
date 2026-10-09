#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
بوت إدارة استضافة بوتات تيليغرام + بوت طلبات الاشتراك.
مهم:
- ضع التوكنات الجديدة في متغيرات بيئة Railway، ولا تكتبها داخل الملف.
- تشغيل كود Python مرفوع من مستخدمين على نفس Railway service ليس عزلاً أمنياً كاملاً.
  للإنتاج متعدد المستخدمين، شغّل كل بوت داخل Container/Service مستقل بصلاحيات وموارد محدودة.
- هذا الملف يشغّل البوتات كعمليات فرعية لأغراض التجربة/الإدارة، ولا يضمن عزلاً ضد كود خبيث.
متغيرات البيئة المطلوبة:
  HOST_BOT_TOKEN       توكن بوت الاستضافة
  REQUEST_BOT_TOKEN    توكن بوت طلبات الاشتراك
اختياري:
  ADMIN_SECRET         الافتراضي ABSIRQ17MAR (غيّره إلى قيمة قوية)
  ADMIN_IDS            معرفات تيليغرام للمديرين مفصولة بفواصل، مثال 12345,67890
  DATA_DIR             مسار البيانات، الافتراضي /data إذا كان موجوداً وإلا مجلد التشغيل
"""

import os
import sys
import json
import time
import signal
import sqlite3
import shutil
import logging
import threading
import subprocess
from pathlib import Path
from datetime import datetime, timedelta, timezone

import telebot
from telebot import types

APP_NAME = "مركز الاستضافة"
BASE_DIR = Path(os.getenv("DATA_DIR", "/data" if Path("/data").exists() else ".")).resolve()
FILES_DIR = BASE_DIR / "hosted_bots"
DB_PATH = BASE_DIR / "hosting.sqlite3"
FILES_DIR.mkdir(parents=True, exist_ok=True)

HOST_TOKEN = os.getenv("HOST_BOT_TOKEN", "8926234274:AAG6IgEYShdTtynxctsRq0hBJnw13GNcmrU").strip()
REQUEST_TOKEN = os.getenv("REQUEST_BOT_TOKEN", "8762696083:AAEFo6wENNadxdOZBcqoEspXrW1Q5IohwuI").strip()
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "ABSIRQ17MAR")
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "8983540044").split(",") if x.strip().isdigit()}

if not HOST_TOKEN or not REQUEST_TOKEN:
    raise SystemExit(
        "أضف HOST_BOT_TOKEN و REQUEST_BOT_TOKEN إلى Variables في Railway، "
        "ثم أعد النشر. لا تضع التوكنات داخل الكود."
    )

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("hosting")

host_bot = telebot.TeleBot(HOST_TOKEN, parse_mode="HTML", threaded=True)
request_bot = telebot.TeleBot(REQUEST_TOKEN, parse_mode="HTML", threaded=True)

# حالة التشغيل داخل الذاكرة؛ تعود البوتات المتوقفة بعد إعادة تشغيل الخدمة يدوياً.
processes = {}
process_lock = threading.RLock()
user_states = {}
state_lock = threading.RLock()


def now_utc():
    return datetime.now(timezone.utc)


def fmt_date(value):
    try:
        return datetime.fromisoformat(value).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return value or "—"


def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            username TEXT DEFAULT '',
            full_name TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS plans(
            plan_id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            price REAL NOT NULL DEFAULT 0,
            currency TEXT NOT NULL DEFAULT 'IQD',
            duration_days INTEGER NOT NULL DEFAULT 30,
            max_bots INTEGER NOT NULL DEFAULT 1,
            active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS requests(
            request_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            plan_id INTEGER NOT NULL,
            note TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            reviewed_at TEXT DEFAULT '',
            invoice_no TEXT DEFAULT '',
            FOREIGN KEY(user_id) REFERENCES users(user_id),
            FOREIGN KEY(plan_id) REFERENCES plans(plan_id)
        );
        CREATE TABLE IF NOT EXISTS subscriptions(
            subscription_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            plan_id INTEGER NOT NULL,
            starts_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            max_bots INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'active'
        );
        CREATE TABLE IF NOT EXISTS hosted_bots(
            bot_id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            file_path TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'stopped',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audit(
            audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_id INTEGER,
            action TEXT NOT NULL,
            details TEXT DEFAULT '',
            created_at TEXT NOT NULL
        );
        """)
        count = con.execute("SELECT COUNT(*) FROM plans").fetchone()[0]
        if count == 0:
            # خطط ابتدائية قابلة للتعديل من لوحة الإدارة، وليست أسعاراً مفروضة.
            con.executemany(
                "INSERT INTO plans(name,price,currency,duration_days,max_bots,active) VALUES(?,?,?,?,?,1)",
                [
                    ("تجريبي", 0, "IQD", 1, 1),
                    ("أسبوعي", 0, "IQD", 7, 2),
                    ("شهري", 0, "IQD", 30, 5),
                ],
            )


def audit(actor, action, details=""):
    with db() as con:
        con.execute(
            "INSERT INTO audit(actor_id,action,details,created_at) VALUES(?,?,?,?)",
            (actor, action, str(details)[:1000], now_utc().isoformat()),
        )


def is_admin(user_id):
    # ADMIN_IDS أفضل للحماية؛ الكود السري يبقى طبقة دخول إضافية.
    return user_id in ADMIN_IDS or user_id in admin_sessions


admin_sessions = set()


def user_status(user_id):
    with db() as con:
        row = con.execute("SELECT status FROM users WHERE user_id=?", (user_id,)).fetchone()
        return row["status"] if row else "unknown"


def ensure_user(user):
    with db() as con:
        con.execute(
            """INSERT INTO users(user_id,username,full_name,status,created_at)
               VALUES(?,?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,full_name=excluded.full_name""",
            (user.id, user.username or "", " ".join(filter(None, [user.first_name, user.last_name])), "pending", now_utc().isoformat()),
        )


def get_active_subscription(user_id):
    with db() as con:
        return con.execute(
            """SELECT s.*,p.name AS plan_name FROM subscriptions s
               JOIN plans p ON p.plan_id=s.plan_id
               WHERE s.user_id=? AND s.status='active' AND s.expires_at>?
               ORDER BY s.expires_at DESC LIMIT 1""",
            (user_id, now_utc().isoformat()),
        ).fetchone()


def kb(rows):
    markup = types.InlineKeyboardMarkup()
    for row in rows:
        markup.row(*row)
    return markup


def btn(text, data):
    return types.InlineKeyboardButton(text, callback_data=data)


def main_menu(user_id):
    rows = [
        [btn("➕ رفع بوت Python", "upload_help"), btn("🤖 بوتاتي", "my_bots")],
        [btn("💳 الاشتراك", "plans"), btn("👤 حسابي", "account")],
        [btn("📖 المساعدة", "help")],
    ]
    if is_admin(user_id):
        rows.append([btn("🛠 لوحة الإدارة", "admin_home")])
    return kb(rows)


def admin_menu():
    return kb([
        [btn("📊 الإحصائيات", "admin_stats"), btn("👥 المستخدمون", "admin_users")],
        [btn("🧾 طلبات الاشتراك", "admin_requests"), btn("💎 إدارة الخطط", "admin_plans")],
        [btn("🤖 كل البوتات", "admin_bots"), btn("📜 سجل العمليات", "admin_audit")],
        [btn("🏠 الرئيسية", "home")],
    ])


def safe_edit(call, text, markup=None):
    try:
        host_bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup)
    except Exception:
        try:
            host_bot.send_message(call.message.chat.id, text, reply_markup=markup)
        except Exception:
            pass


@host_bot.message_handler(commands=["start", "id"])
def host_start(message):
    if message.text.startswith("/id"):
        host_bot.reply_to(message, f"معرّفك: <code>{message.from_user.id}</code>")
        return
    ensure_user(message.from_user)
    status = user_status(message.from_user.id)
    if status != "approved" and not is_admin(message.from_user.id):
        host_bot.send_message(
            message.chat.id,
            "أهلاً بيك بمركز الاستضافة ✨\nحسابك يحتاج موافقة المدير قبل استخدام خدمات الاستضافة.",
            reply_markup=kb([[btn("📨 طلب الوصول", "access_request")], [btn("🆔 عرض معرّفي", "show_id")]]),
        )
        return
    host_bot.send_message(
        message.chat.id,
        f"أهلاً {message.from_user.first_name or 'صديقي'} 👋\n"
        "إدارة بوتاتك واشتراكاتك من مكان واحد.",
        reply_markup=main_menu(message.from_user.id),
    )


@host_bot.message_handler(commands=["admin"])
def admin_command(message):
    user_states[message.from_user.id] = {"mode": "admin_code"}
    host_bot.reply_to(message, "أرسل رمز دخول الإدارة برسالة خاصة. لا تشاركه مع أحد.")


@host_bot.message_handler(func=lambda m: user_states.get(m.from_user.id, {}).get("mode") in ("admin_code", "new_plan_name", "new_plan_price", "new_plan_days", "new_plan_bots", "upload_bot_name"))
def host_text_flow(message):
    uid = message.from_user.id
    state = user_states.get(uid, {})
    mode = state.get("mode")
    text = (message.text or "").strip()
    if mode == "admin_code":
        if text == ADMIN_SECRET:
            admin_sessions.add(uid)
            user_states.pop(uid, None)
            audit(uid, "admin_login", "secret accepted")
            host_bot.send_message(message.chat.id, "تم فتح لوحة الإدارة. للأمان، أضف ADMIN_IDS في Railway أيضاً.", reply_markup=admin_menu())
        else:
            user_states.pop(uid, None)
            host_bot.reply_to(message, "الرمز غير صحيح.")
        return
    if not is_admin(uid):
        user_states.pop(uid, None)
        return
    if mode == "new_plan_name":
        state.update(mode="new_plan_price", name=text)
        host_bot.reply_to(message, "أرسل السعر كرقم فقط، مثلاً 5000.")
    elif mode == "new_plan_price":
        try:
            state["price"] = float(text)
            state["mode"] = "new_plan_days"
            host_bot.reply_to(message, "أرسل مدة الاشتراك بالأيام (الدائم اكتب 36500).")
        except ValueError:
            host_bot.reply_to(message, "أرسل السعر كرقم فقط.")
    elif mode == "new_plan_days":
        try:
            state["days"] = max(1, min(int(text), 36500))
            state["mode"] = "new_plan_bots"
            host_bot.reply_to(message, "أرسل أقصى عدد بوتات لهذا الاشتراك.")
        except ValueError:
            host_bot.reply_to(message, "أرسل عدد أيام صحيح.")
    elif mode == "new_plan_bots":
        try:
            max_bots = max(1, min(int(text), 1000))
            with db() as con:
                con.execute(
                    "INSERT INTO plans(name,price,currency,duration_days,max_bots,active) VALUES(?,?,?,?,?,1)",
                    (state["name"], state["price"], "IQD", state["days"], max_bots),
                )
            audit(uid, "plan_created", state["name"])
            user_states.pop(uid, None)
            host_bot.reply_to(message, "✅ انضافت الخطة بنجاح.", reply_markup=admin_menu())
        except ValueError:
            host_bot.reply_to(message, "أرسل عدد بوتات صحيح.")
    elif mode == "upload_bot_name":
        state["name"] = text[:60]
        state["mode"] = "upload_bot_file"
        host_bot.reply_to(message, "هسه أرسل ملف .py كـ Document.")
    elif mode == "upload_bot_file":
        pass


@host_bot.message_handler(content_types=["document"])
def receive_python_file(message):
    uid = message.from_user.id
    if not is_admin(uid) and user_status(uid) != "approved":
        host_bot.reply_to(message, "حسابك مو مفعل. اطلب الوصول أولاً.")
        return
    sub = get_active_subscription(uid)
    if not is_admin(uid) and not sub:
        host_bot.reply_to(message, "ما عندك اشتراك فعال. افتح قسم الاشتراك أولاً.")
        return
    doc = message.document
    filename = (doc.file_name or "").strip()
    if not filename.lower().endswith(".py"):
        host_bot.reply_to(message, "حالياً نقبل ملفات Python بصيغة .py فقط.")
        return
    if doc.file_size and doc.file_size > 1_000_000:
        host_bot.reply_to(message, "حجم الملف أكبر من الحد المسموح (1 MB).")
        return
    state = user_states.get(uid, {})
    if state.get("mode") == "upload_bot_file":
        name = state.get("name", Path(filename).stem)
    else:
        name = Path(filename).stem[:60]
    current_count = 0
    with db() as con:
        current_count = con.execute("SELECT COUNT(*) FROM hosted_bots WHERE owner_id=?", (uid,)).fetchone()[0]
    limit = 1000 if is_admin(uid) else (sub["max_bots"] if sub else 0)
    if current_count >= limit:
        host_bot.reply_to(message, f"وصلت للحد المسموح من البوتات ({limit}).")
        return
    try:
        info = host_bot.get_file(doc.file_id)
        data = host_bot.download_file(info.file_path)
        bot_id = None
        with db() as con:
            cur = con.execute(
                "INSERT INTO hosted_bots(owner_id,name,file_path,status,created_at) VALUES(?,?,?,?,?)",
                (uid, name, "", "stopped", now_utc().isoformat()),
            )
            bot_id = cur.lastrowid
        folder = FILES_DIR / str(bot_id)
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / "bot.py"
        target.write_bytes(data)
        with db() as con:
            con.execute("UPDATE hosted_bots SET file_path=? WHERE bot_id=?", (str(target), bot_id))
        user_states.pop(uid, None)
        audit(uid, "bot_uploaded", f"id={bot_id}; name={name}")
        host_bot.reply_to(
            message,
            f"✅ تم حفظ البوت بنجاح.\n🆔 رقم البوت: <code>{bot_id}</code>\n"
            "تنبيه: تشغيل كود المستخدمين على نفس الخدمة ليس عزلاً أمنياً كاملاً.",
            reply_markup=kb([[btn("▶️ تشغيل", f"bot_start:{bot_id}"), btn("⏹ إيقاف", f"bot_stop:{bot_id}")],
                             [btn("🤖 بوتاتي", "my_bots")]]),
        )
    except Exception as exc:
        log.exception("upload failed")
        host_bot.reply_to(message, f"تعذر حفظ الملف: {type(exc).__name__}")


@request_bot.message_handler(commands=["start", "id"])
def request_start(message):
    ensure_user(message.from_user)
    if message.text.startswith("/id"):
        request_bot.reply_to(message, f"معرّفك: <code>{message.from_user.id}</code>")
        return
    request_bot.send_message(
        message.chat.id,
        "أهلاً بيك 👋\nمن هنا تگدر تشوف خطط الاستضافة وترسل طلب اشتراك للمدير.",
        reply_markup=kb([[btn("💎 عرض الاشتراكات", "req_plans")], [btn("🆔 معرّفي", "req_id")]]),
    )


def plans_text():
    with db() as con:
        plans = con.execute("SELECT * FROM plans WHERE active=1 ORDER BY price, duration_days").fetchall()
    if not plans:
        return "ماكو خطط متاحة حالياً.", []
    lines = ["<b>💎 خطط الاستضافة المتاحة</b>\n"]
    buttons = []
    for p in plans:
        lines.append(
            f"• <b>{p['name']}</b>\n"
            f"  السعر: {p['price']:g} {p['currency']}\n"
            f"  المدة: {p['duration_days']} يوم\n"
            f"  الحد: {p['max_bots']} بوت\n"
        )
        buttons.append([btn(f"طلب {p['name']} · {p['price']:g} {p['currency']}", f"request_plan:{p['plan_id']}")])
    return "\n".join(lines), buttons


@host_bot.callback_query_handler(func=lambda c: True)
def host_callbacks(call):
    uid, data = call.from_user.id, call.data or ""
    host_bot.answer_callback_query(call.id)
    if data == "show_id":
        safe_edit(call, f"معرّفك: <code>{uid}</code>", kb([[btn("رجوع", "home")]]))
    elif data == "access_request":
        ensure_user(call.from_user)
        with db() as con:
            con.execute("UPDATE users SET status='pending' WHERE user_id=?", (uid,))
        safe_edit(call, "📨 تم تسجيل طلب الوصول. انتظر موافقة المدير.")
        for admin_id in ADMIN_IDS:
            try:
                host_bot.send_message(admin_id, f"طلب وصول جديد من <code>{uid}</code>.", reply_markup=kb([[btn("✅ قبول", f"user_approve:{uid}"), btn("❌ رفض", f"user_reject:{uid}")]]))
            except Exception:
                pass
    elif data == "home":
        safe_edit(call, "🏠 القائمة الرئيسية", main_menu(uid))
    elif data == "help":
        safe_edit(call, "📖 المساعدة\n1) اطلب الوصول.\n2) احصل على موافقة المدير.\n3) اطلب اشتراكاً.\n4) ارفع ملف .py.\n5) استخدم أزرار التشغيل والإيقاف.")
    elif data == "upload_help":
        if user_status(uid) != "approved" and not is_admin(uid):
            safe_edit(call, "حسابك يحتاج موافقة المدير أولاً.")
        else:
            user_states[uid] = {"mode": "upload_bot_name"}
            safe_edit(call, "أرسل اسم البوت برسالة، وبعدها ارفع ملف .py.")
    elif data == "my_bots":
        with db() as con:
            rows = con.execute("SELECT * FROM hosted_bots WHERE owner_id=? ORDER BY bot_id DESC", (uid,)).fetchall()
        if not rows:
            safe_edit(call, "ما عندك بوتات مرفوعة حالياً.", kb([[btn("رفع بوت", "upload_help")], [btn("رجوع", "home")]]))
        else:
            out = ["<b>🤖 بوتاتي</b>\n"]
            buttons = []
            for row in rows[:30]:
                running = is_running(row["bot_id"])
                out.append(f"#{row['bot_id']} — <b>{row['name']}</b> — {'🟢 يعمل' if running else '🔴 متوقف'}")
                buttons.append([btn(f"▶️ #{row['bot_id']}", f"bot_start:{row['bot_id']}"), btn(f"⏹ #{row['bot_id']}", f"bot_stop:{row['bot_id']}"), btn("🗑", f"bot_delete:{row['bot_id']}")])
            buttons.append([btn("رجوع", "home")])
            safe_edit(call, "\n".join(out), kb(buttons))
    elif data == "plans":
        text, buttons = plans_text()
        buttons.append([btn("رجوع", "home")])
        safe_edit(call, text, kb(buttons))
    elif data == "account":
        sub = get_active_subscription(uid)
        status = user_status(uid)
        text = f"👤 <b>حسابك</b>\nالمعرف: <code>{uid}</code>\nالحالة: {status}"
        if sub:
            text += f"\nالخطة: {sub['plan_name']}\nينتهي: {fmt_date(sub['expires_at'])}\nحد البوتات: {sub['max_bots']}"
        else:
            text += "\nما عندك اشتراك فعال."
        safe_edit(call, text, kb([[btn("الاشتراكات", "plans")], [btn("رجوع", "home")]]))
    elif data == "admin_home":
        if not is_admin(uid):
            safe_edit(call, "غير مصرح.")
        else:
            safe_edit(call, "🛠 <b>لوحة الإدارة</b>\nاختار القسم:", admin_menu())
    elif data == "admin_stats":
        if not is_admin(uid): return
        with db() as con:
            users = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            bots = con.execute("SELECT COUNT(*) FROM hosted_bots").fetchone()[0]
            reqs = con.execute("SELECT COUNT(*) FROM requests WHERE status='pending'").fetchone()[0]
            subs = con.execute("SELECT COUNT(*) FROM subscriptions WHERE status='active' AND expires_at>?", (now_utc().isoformat(),)).fetchone()[0]
        safe_edit(call, f"📊 <b>الإحصائيات</b>\nالمستخدمون: {users}\nالبوتات المسجلة: {bots}\nالاشتراكات الفعالة: {subs}\nالطلبات المعلقة: {reqs}", admin_menu())
    elif data == "admin_users":
        if not is_admin(uid): return
        with db() as con:
            rows = con.execute("SELECT * FROM users ORDER BY created_at DESC LIMIT 20").fetchall()
        lines = ["👥 <b>آخر المستخدمين</b>"]
        buttons = []
        for row in rows:
            lines.append(f"• <code>{row['user_id']}</code> — {row['full_name'][:25]} — {row['status']}")
            if row["status"] != "approved":
                buttons.append([btn(f"قبول {row['user_id']}", f"user_approve:{row['user_id']}"), btn("رفض", f"user_reject:{row['user_id']}")])
        buttons.append([btn("رجوع", "admin_home")])
        safe_edit(call, "\n".join(lines), kb(buttons))
    elif data == "admin_plans":
        if not is_admin(uid): return
        with db() as con:
            rows = con.execute("SELECT * FROM plans ORDER BY plan_id").fetchall()
        lines = ["💎 <b>إدارة الخطط</b>"]
        buttons = [[btn("➕ إضافة خطة", "plan_add")]]
        for p in rows:
            lines.append(f"#{p['plan_id']} {p['name']} — {p['price']:g} {p['currency']} — {p['duration_days']} يوم — {p['max_bots']} بوت — {'فعالة' if p['active'] else 'معطلة'}")
            buttons.append([btn(f"{'تعطيل' if p['active'] else 'تفعيل'} #{p['plan_id']}", f"plan_toggle:{p['plan_id']}")])
        buttons.append([btn("رجوع", "admin_home")])
        safe_edit(call, "\n".join(lines), kb(buttons))
    elif data == "plan_add":
        if not is_admin(uid): return
        user_states[uid] = {"mode": "new_plan_name"}
        safe_edit(call, "أرسل اسم الخطة الجديدة.")
    elif data.startswith("plan_toggle:"):
        if not is_admin(uid): return
        plan_id = int(data.split(":")[1])
        with db() as con:
            con.execute("UPDATE plans SET active=1-active WHERE plan_id=?", (plan_id,))
        safe_edit(call, "تم تحديث حالة الخطة.", admin_menu())
    elif data == "admin_requests":
        if not is_admin(uid): return
        with db() as con:
            rows = con.execute(
                """SELECT r.*,u.full_name,p.name AS plan_name,p.price,p.currency
                   FROM requests r JOIN users u ON u.user_id=r.user_id JOIN plans p ON p.plan_id=r.plan_id
                   WHERE r.status='pending' ORDER BY r.request_id LIMIT 15"""
            ).fetchall()
        if not rows:
            safe_edit(call, "ماكو طلبات معلقة حالياً.", admin_menu())
        else:
            lines = ["🧾 <b>طلبات الاشتراك المعلقة</b>"]
            buttons = []
            for r in rows:
                lines.append(f"#{r['request_id']} | {r['full_name']} ({r['user_id']})\n{r['plan_name']} — {r['price']:g} {r['currency']}")
                buttons.append([btn(f"✅ موافقة #{r['request_id']}", f"req_approve:{r['request_id']}"), btn("❌ رفض", f"req_reject:{r['request_id']}")])
            buttons.append([btn("رجوع", "admin_home")])
            safe_edit(call, "\n".join(lines), kb(buttons))
    elif data.startswith("user_approve:") or data.startswith("user_reject:"):
        if not is_admin(uid): return
        target = int(data.split(":")[1])
        status = "approved" if data.startswith("user_approve:") else "rejected"
        with db() as con:
            con.execute("UPDATE users SET status=? WHERE user_id=?", (status, target))
        audit(uid, "user_status", f"{target}:{status}")
        try:
            host_bot.send_message(target, "✅ تمت الموافقة على وصولك." if status == "approved" else "تم رفض طلب الوصول.")
        except Exception:
            pass
        safe_edit(call, f"تم تحديث حالة المستخدم {target}: {status}", admin_menu())
    elif data.startswith("req_approve:") or data.startswith("req_reject:"):
        if not is_admin(uid): return
        req_id = int(data.split(":")[1])
        with db() as con:
            req = con.execute(
                """SELECT r.*,p.name AS plan_name,p.price,p.currency,p.duration_days,p.max_bots
                   FROM requests r JOIN plans p ON p.plan_id=r.plan_id WHERE r.request_id=?""",
                (req_id,),
            ).fetchone()
            if not req or req["status"] != "pending":
                safe_edit(call, "الطلب غير موجود أو تمت معالجته مسبقاً.", admin_menu())
                return
            if data.startswith("req_reject:"):
                con.execute("UPDATE requests SET status='rejected',reviewed_at=? WHERE request_id=?", (now_utc().isoformat(), req_id))
                decision = "rejected"
            else:
                start = now_utc()
                end = start + timedelta(days=req["duration_days"])
                invoice = f"INV-{start.strftime('%Y%m%d')}-{req_id:06d}"
                con.execute("UPDATE requests SET status='approved',reviewed_at=?,invoice_no=? WHERE request_id=?", (start.isoformat(), invoice, req_id))
                con.execute(
                    "INSERT INTO subscriptions(user_id,plan_id,starts_at,expires_at,max_bots,status) VALUES(?,?,?,?,?,'active')",
                    (req["user_id"], req["plan_id"], start.isoformat(), end.isoformat(), req["max_bots"]),
                )
                decision = "approved"
        audit(uid, "subscription_request", f"{req_id}:{decision}")
        if decision == "approved":
            invoice_text = (
                "🧾 <b>فاتورة اشتراك الاستضافة</b>\n"
                "━━━━━━━━━━━━━━\n"
                f"رقم الفاتورة: <code>{invoice}</code>\n"
                f"الخطة: <b>{req['plan_name']}</b>\n"
                f"المبلغ: <b>{req['price']:g} {req['currency']}</b>\n"
                f"عدد البوتات: <b>{req['max_bots']}</b>\n"
                f"بداية الاشتراك: {fmt_date(start.isoformat())}\n"
                f"نهاية الاشتراك: {fmt_date(end.isoformat())}\n"
                "الحالة: ✅ تمت الموافقة\n"
                "━━━━━━━━━━━━━━\n"
                "احتفظ بهذه الفاتورة للرجوع إليها."
            )
            try:
                host_bot.send_message(req["user_id"], invoice_text)
            except Exception:
                try:
                    request_bot.send_message(req["user_id"], invoice_text)
                except Exception:
                    pass
        else:
            try:
                host_bot.send_message(req["user_id"], f"تم رفض طلب الاشتراك رقم {req_id}.")
            except Exception:
                pass
        safe_edit(call, f"تمت معالجة الطلب #{req_id}: {decision}", admin_menu())
    elif data == "admin_bots":
        if not is_admin(uid): return
        with db() as con:
            rows = con.execute("SELECT * FROM hosted_bots ORDER BY bot_id DESC LIMIT 20").fetchall()
        lines = ["🤖 <b>آخر البوتات المسجلة</b>"]
        for r in rows:
            lines.append(f"#{r['bot_id']} — {r['name']} — المالك {r['owner_id']} — {'🟢 يعمل' if is_running(r['bot_id']) else '🔴 متوقف'}")
        safe_edit(call, "\n".join(lines), admin_menu())
    elif data == "admin_audit":
        if not is_admin(uid): return
        with db() as con:
            rows = con.execute("SELECT * FROM audit ORDER BY audit_id DESC LIMIT 15").fetchall()
        lines = ["📜 <b>آخر العمليات</b>"]
        for r in rows:
            lines.append(f"#{r['audit_id']} | {r['action']} | {r['actor_id']} | {r['details'][:80]}")
        safe_edit(call, "\n".join(lines), admin_menu())
    elif data.startswith("bot_start:"):
        bot_id = int(data.split(":")[1])
        result = start_bot(uid, bot_id)
        safe_edit(call, result, kb([[btn("بوتاتي", "my_bots")], [btn("الرئيسية", "home")]]))
    elif data.startswith("bot_stop:"):
        bot_id = int(data.split(":")[1])
        result = stop_bot(uid, bot_id)
        safe_edit(call, result, kb([[btn("بوتاتي", "my_bots")], [btn("الرئيسية", "home")]]))
    elif data.startswith("bot_delete:"):
        bot_id = int(data.split(":")[1])
        if not owns_bot(uid, bot_id):
            safe_edit(call, "ما عندك صلاحية على هذا البوت.")
            return
        stop_bot(uid, bot_id)
        with db() as con:
            row = con.execute("SELECT file_path FROM hosted_bots WHERE bot_id=?", (bot_id,)).fetchone()
            con.execute("DELETE FROM hosted_bots WHERE bot_id=?", (bot_id,))
        if row:
            shutil.rmtree(Path(row["file_path"]).parent, ignore_errors=True)
        audit(uid, "bot_deleted", str(bot_id))
        safe_edit(call, "🗑 تم حذف سجل البوت وملفاته.", kb([[btn("بوتاتي", "my_bots")]]))
    elif data.startswith("request_plan:"):
        # يمكن طلب الاشتراك من بوت الاستضافة أيضاً.
        plan_id = int(data.split(":")[1])
        with db() as con:
            p = con.execute("SELECT * FROM plans WHERE plan_id=? AND active=1", (plan_id,)).fetchone()
            if not p:
                safe_edit(call, "الخطة غير متاحة حالياً.")
                return
            cur = con.execute(
                "INSERT INTO requests(user_id,plan_id,note,status,created_at) VALUES(?,?,?,'pending',?)",
                (uid, plan_id, "طلب من بوت الاستضافة", now_utc().isoformat()),
            )
            req_id = cur.lastrowid
        for admin_id in ADMIN_IDS:
            try:
                host_bot.send_message(admin_id, f"طلب اشتراك جديد #{req_id}\nالمستخدم: <code>{uid}</code>\nالخطة: {p['name']}\nالسعر: {p['price']:g} {p['currency']}", reply_markup=kb([[btn("✅ موافقة", f"req_approve:{req_id}"), btn("❌ رفض", f"req_reject:{req_id}")]]))
            except Exception:
                pass
        safe_edit(call, f"📨 تم إرسال طلبك بنجاح. رقم الطلب: #{req_id}.\nسيصلك إشعار بعد المراجعة.")
    else:
        safe_edit(call, "هذا الخيار غير معروف.", main_menu(uid))


@request_bot.callback_query_handler(func=lambda c: True)
def request_callbacks(call):
    uid, data = call.from_user.id, call.data or ""
    request_bot.answer_callback_query(call.id)
    if data == "req_id":
        try:
            request_bot.edit_message_text(f"معرّفك: <code>{uid}</code>", call.message.chat.id, call.message.message_id)
        except Exception:
            pass
    elif data == "req_plans":
        text, buttons = plans_text()
        buttons.append([btn("🏠 الرئيسية", "req_home")])
        try:
            request_bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb(buttons))
        except Exception:
            request_bot.send_message(call.message.chat.id, text, reply_markup=kb(buttons))
    elif data == "req_home":
        try:
            request_bot.edit_message_text("القائمة الرئيسية", call.message.chat.id, call.message.message_id, reply_markup=kb([[btn("💎 عرض الاشتراكات", "req_plans")], [btn("🆔 معرّفي", "req_id")]]))
        except Exception:
            pass
    elif data.startswith("request_plan:"):
        plan_id = int(data.split(":")[1])
        with db() as con:
            p = con.execute("SELECT * FROM plans WHERE plan_id=? AND active=1", (plan_id,)).fetchone()
            if not p:
                request_bot.send_message(uid, "الخطة غير متاحة حالياً.")
                return
            cur = con.execute(
                "INSERT INTO requests(user_id,plan_id,note,status,created_at) VALUES(?,?,?,'pending',?)",
                (uid, plan_id, "طلب من بوت الاشتراكات", now_utc().isoformat()),
            )
            req_id = cur.lastrowid
        for admin_id in ADMIN_IDS:
            try:
                host_bot.send_message(
                    admin_id,
                    f"🧾 <b>طلب اشتراك جديد</b>\nرقم الطلب: #{req_id}\nالمستخدم: <code>{uid}</code>\nالخطة: {p['name']}\nالسعر: {p['price']:g} {p['currency']}",
                    reply_markup=kb([[btn("✅ موافقة", f"req_approve:{req_id}"), btn("❌ رفض", f"req_reject:{req_id}")]]),
                )
            except Exception:
                pass
        request_bot.send_message(uid, f"📨 استلمنا طلبك رقم #{req_id}.\nراح يوصلك إشعار بعد مراجعة المدير.")


def owns_bot(user_id, bot_id):
    with db() as con:
        row = con.execute("SELECT owner_id FROM hosted_bots WHERE bot_id=?", (bot_id,)).fetchone()
    return bool(row and (row["owner_id"] == user_id or is_admin(user_id)))


def is_running(bot_id):
    with process_lock:
        proc = processes.get(bot_id)
        return bool(proc and proc.poll() is None)


def start_bot(user_id, bot_id):
    if not owns_bot(user_id, bot_id):
        return "❌ ما عندك صلاحية على هذا البوت."
    if is_running(bot_id):
        return f"🟢 البوت #{bot_id} يعمل بالفعل."
    with db() as con:
        row = con.execute("SELECT * FROM hosted_bots WHERE bot_id=?", (bot_id,)).fetchone()
    if not row:
        return "البوت غير موجود."
    file_path = Path(row["file_path"])
    if not file_path.is_file():
        return "ملف البوت غير موجود. ارفعه من جديد."
    try:
        # لا نستخدم shell=True. هذا ليس عزلًا أمنياً ضد كود خبيث.
        log_path = file_path.parent / "runtime.log"
        log_file = open(log_path, "ab")
        env = {
            "PATH": os.getenv("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "PYTHONUNBUFFERED": "1",
            "HOME": str(file_path.parent),
        }
        # حدود موارد أساسية على Linux إن توفرت.
        def limits():
            try:
                import resource
                resource.setrlimit(resource.RLIMIT_CPU, (300, 300))
                resource.setrlimit(resource.RLIMIT_FSIZE, (20 * 1024 * 1024, 20 * 1024 * 1024))
                resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
            except Exception:
                pass
        proc = subprocess.Popen(
            [sys.executable, "-I", str(file_path)],
            cwd=str(file_path.parent),
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
            preexec_fn=limits if os.name == "posix" else None,
        )
        with process_lock:
            processes[bot_id] = proc
        with db() as con:
            con.execute("UPDATE hosted_bots SET status='running' WHERE bot_id=?", (bot_id,))
        audit(user_id, "bot_started", str(bot_id))
        return f"🚀 تم إرسال أمر تشغيل البوت #{bot_id}.\nPID: <code>{proc.pid}</code>\nالسجلات: runtime.log داخل مجلد البوت."
    except Exception as exc:
        log.exception("start bot failed")
        return f"❌ فشل التشغيل: {type(exc).__name__}"


def stop_bot(user_id, bot_id):
    if not owns_bot(user_id, bot_id):
        return "❌ ما عندك صلاحية على هذا البوت."
    with process_lock:
        proc = processes.get(bot_id)
        if not proc or proc.poll() is not None:
            processes.pop(bot_id, None)
            with db() as con:
                con.execute("UPDATE hosted_bots SET status='stopped' WHERE bot_id=?", (bot_id,))
            return f"البوت #{bot_id} متوقف بالفعل."
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
            processes.pop(bot_id, None)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
    with db() as con:
        con.execute("UPDATE hosted_bots SET status='stopped' WHERE bot_id=?", (bot_id,))
    audit(user_id, "bot_stopped", str(bot_id))
    return f"⏹ تم إيقاف البوت #{bot_id}."


def monitor_loop():
    while True:
        time.sleep(20)
        with process_lock:
            dead = [bot_id for bot_id, proc in processes.items() if proc.poll() is not None]
            for bot_id in dead:
                processes.pop(bot_id, None)
                with db() as con:
                    con.execute("UPDATE hosted_bots SET status='stopped' WHERE bot_id=?", (bot_id,))
        # نراقب انتهاء الاشتراكات، لكن لا نحذف ملفات المستخدمين تلقائياً.
        with db() as con:
            expired = con.execute(
                "SELECT DISTINCT user_id FROM subscriptions WHERE status='active' AND expires_at<=?",
                (now_utc().isoformat(),),
            ).fetchall()
            con.execute(
                "UPDATE subscriptions SET status='expired' WHERE status='active' AND expires_at<=?",
                (now_utc().isoformat(),),
            )
        for row in expired:
            try:
                host_bot.send_message(row["user_id"], "⏰ انتهى اشتراكك. تگدر تقدم طلب تجديد من قسم الاشتراكات.")
            except Exception:
                pass


def run_bot(bot, name):
    while True:
        try:
            log.info("Starting polling: %s", name)
            bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=25)
        except Exception:
            log.exception("Polling crashed: %s", name)
            time.sleep(5)


def main():
    init_db()
    threading.Thread(target=monitor_loop, daemon=True, name="monitor").start()
    t1 = threading.Thread(target=run_bot, args=(host_bot, "host"), daemon=True)
    t2 = threading.Thread(target=run_bot, args=(request_bot, "requests"), daemon=True)
    t1.start()
    t2.start()
    log.info("Hosting manager started. Data directory: %s", BASE_DIR)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
