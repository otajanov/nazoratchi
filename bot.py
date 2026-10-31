"""
Topshiriq boshqaruv boti (1 admin + 10-15 hodim)  —  v2
aiogram 3.7+  |  aiosqlite

Ish tartibi:
  Admin topshiriq beradi -> hodim bajaradi (majburiy) -> natijani adminga yuboradi
  -> admin "Qabul qilish" yoki izoh yozib "Rad etish" -> rad etilsa hodim qayta yuboradi.

Ishga tushirish:
    pip install -r requirements.txt
    export BOT_TOKEN="123:ABC..."
    export ADMIN_ID="123456789"
    python bot.py
"""
import asyncio
import html
import logging
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, KeyboardButton, Message, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ============================ SOZLAMALAR ============================
BOT_TOKEN = os.getenv("BOT_TOKEN", "9224212349:AAGk5zsDcCGXtCcHrap8NQWA1g5btkgmM6U")
ADMIN_ID = int(os.getenv("ADMIN_ID", "6611171894"))
TZ = ZoneInfo("Asia/Tashkent")
REMIND_HOUR = 9            # har kuni shu soatda eslatma ketadi
WARN_BEFORE_HOURS = 2      # muddatga necha soat qolganda ogohlantirish
DB_PATH = "tasks.db"
PAGE = 8                   # hisobotda bir sahifada nechta topshiriq
# ====================================================================

STATUS = {
    "new": "🆕 Yangi",
    "in_progress": "🔄 Tayyorlanmoqda",
    "review": "🔎 Admin tekshiruvida",
    "revision": "↩️ Qayta ishlash kerak",
    "done": "✅ Qabul qilindi (tugatildi)",
}
# Hodim bajarishi kerak bo'lgan holatlar
WORK = ("new", "in_progress", "revision")
WORK_SQL = "('new','in_progress','revision')"

FILTERS = {
    "all": ("Hammasi", None),
    "new": ("🆕 Yangi", "t.status='new'"),
    "prog": ("🔄 Jarayonda", "t.status='in_progress'"),
    "rev": ("🔎 Tekshiruvda", "t.status='review'"),
    "redo": ("↩️ Qayta", "t.status='revision'"),
    "done": ("✅ Tugagan", "t.status='done'"),
    "late": ("🔥 Kechikkan", "t.status IN " + WORK_SQL + " AND t.deadline < ?"),
}

KIND_LABEL = {
    "photo": "🖼 Rasm", "document": "📎 Fayl", "voice": "🎤 Ovozli xabar", "video": "🎬 Video",
    "audio": "🎵 Audio", "video_note": "⭕ Video xabar", "location": "📍 Joylashuv",
    "contact": "👤 Kontakt", "sticker": "🏷 Stiker",
}

# ============================ YORDAMCHILAR ============================


def now() -> datetime:
    return datetime.now(TZ).replace(tzinfo=None)


def ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


def fmt(s) -> str:
    if not s:
        return "—"
    return datetime.strptime(s, "%Y-%m-%d %H:%M").strftime("%d.%m.%Y %H:%M")


def esc(s) -> str:
    return html.escape(str(s or ""))


def parse_deadline(s: str):
    s = s.strip()
    for f in ("%d.%m.%Y %H:%M", "%d.%m.%y %H:%M", "%d.%m %H:%M"):
        try:
            d = datetime.strptime(s, f)
            if f == "%d.%m %H:%M":
                d = d.replace(year=now().year)
            return d
        except ValueError:
            continue
    return None


def preview_of(m: Message) -> str:
    text = m.text or m.caption
    if text:
        return text.strip().replace("\n", " ")[:60]
    return KIND_LABEL.get(m.content_type.value, "📨 Xabar")


async def safe(coro):
    try:
        return await coro
    except TelegramAPIError as e:
        logging.warning("Telegram xatosi: %s", e)
        return None


# ============================ BAZA ============================
db: aiosqlite.Connection

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    tg_id INTEGER PRIMARY KEY, full_name TEXT, username TEXT,
    status TEXT DEFAULT 'pending', created_at TEXT);
CREATE TABLE IF NOT EXISTS tasks(
    id INTEGER PRIMARY KEY AUTOINCREMENT, employee_id INTEGER, preview TEXT,
    created_at TEXT, deadline TEXT, status TEXT DEFAULT 'new',
    reject_reason TEXT, started_at TEXT, submitted_at TEXT, done_at TEXT, rejected_at TEXT,
    rev_count INTEGER DEFAULT 0,
    warned INTEGER DEFAULT 0, overdue_sent INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS items(
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER, kind TEXT,
    chat_id INTEGER, message_id INTEGER, round INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""


async def init_db():
    global db
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.executescript(SCHEMA)
    # Eski versiya bazasidan o'tish (xato bersa, ustun bor degani)
    for sql in ("ALTER TABLE tasks ADD COLUMN submitted_at TEXT",
                "ALTER TABLE tasks ADD COLUMN rev_count INTEGER DEFAULT 0",
                "ALTER TABLE items ADD COLUMN round INTEGER DEFAULT 0"):
        try:
            await db.execute(sql)
        except Exception:
            pass
    await db.execute("UPDATE tasks SET status='in_progress' WHERE status='accepted'")
    await db.execute("UPDATE tasks SET status='revision' WHERE status='rejected'")
    await db.commit()


async def run(sql, args=()):
    cur = await db.execute(sql, args)
    await db.commit()
    return cur.lastrowid


async def all_(sql, args=()):
    async with db.execute(sql, args) as cur:
        return await cur.fetchall()


async def one(sql, args=()):
    async with db.execute(sql, args) as cur:
        return await cur.fetchone()


# ============================ KLAVIATURA / MATN ============================


def admin_menu():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="➕ Yangi topshiriq"), KeyboardButton(text="📊 Hisobot")],
                  [KeyboardButton(text="👥 Hodimlar")]],
        resize_keyboard=True)


def emp_menu():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📋 Topshiriqlarim"), KeyboardButton(text="📤 Natija yuborish")]],
        resize_keyboard=True)


def employee_kb(t):
    b = InlineKeyboardBuilder()
    s, i = t["status"], t["id"]
    if s == "new":
        b.button(text="🔄 Tayyorlanmoqda", callback_data=f"prog:{i}")
        b.button(text="📤 Natija yuborish", callback_data=f"sub:{i}")
    elif s == "in_progress":
        b.button(text="📤 Natija yuborish", callback_data=f"sub:{i}")
    elif s == "revision":
        b.button(text="📤 Qayta yuborish", callback_data=f"sub:{i}")
    else:
        return None
    b.adjust(2)
    return b.as_markup()


def review_kb(tid: int):
    b = InlineKeyboardBuilder()
    b.button(text="✅ Qabul qilish", callback_data=f"ok:{tid}")
    b.button(text="❌ Rad etish", callback_data=f"no:{tid}")
    b.adjust(2)
    return b.as_markup()


def task_card(t, emp_name=None) -> str:
    late = t["status"] in WORK and t["deadline"] < ts(now())
    L = [f"📌 <b>Topshiriq #{t['id']}</b>"]
    if emp_name:
        L.append(f"👤 Hodim: <b>{esc(emp_name)}</b>")
    L.append(f"📝 {esc(t['preview'])}")
    L.append(f"📅 Berilgan: {fmt(t['created_at'])}")
    L.append(f"⏰ Muddat: <b>{fmt(t['deadline'])}</b>" + (" 🔥 <b>KECHIKKAN</b>" if late else ""))
    L.append(f"📍 Holat: {STATUS[t['status']]}")
    if t["started_at"]:
        L.append(f"🔄 Boshlangan: {fmt(t['started_at'])}")
    if t["submitted_at"]:
        ontime = "o'z vaqtida" if t["submitted_at"] <= t["deadline"] else "muddatdan kech"
        L.append(f"📤 Oxirgi yuborilgan: {fmt(t['submitted_at'])} ({ontime})")
    if t["rev_count"]:
        L.append(f"🔁 Qaytarilgan: {t['rev_count']} marta")
    if t["done_at"]:
        L.append(f"✅ Qabul qilingan: {fmt(t['done_at'])}")
    if t["status"] == "revision" and t["reject_reason"]:
        L.append(f"💬 <b>Admin izohi:</b> {esc(t['reject_reason'])}")
    return "\n".join(L)


async def send_task_to_employee(bot: Bot, tid: int, header: str) -> bool:
    t = await one("SELECT * FROM tasks WHERE id=?", (tid,))
    to = t["employee_id"]
    try:
        await bot.send_message(to, header)
        for it in await all_("SELECT * FROM items WHERE task_id=? AND kind='task' ORDER BY id", (tid,)):
            await bot.copy_message(to, it["chat_id"], it["message_id"])
        await bot.send_message(to, task_card(t), reply_markup=employee_kb(t))
        return True
    except TelegramAPIError as e:
        logging.warning("Hodimga yuborib bo'lmadi: %s", e)
        return False


async def notify_admin(bot: Bot, text: str):
    await safe(bot.send_message(ADMIN_ID, text))


# ============================ ROUTERLAR ============================
common = Router()
admin = Router()
emp = Router()

admin.message.filter(F.from_user.id == ADMIN_ID)
admin.callback_query.filter(F.from_user.id == ADMIN_ID)


async def active_emp(event) -> bool:
    r = await one("SELECT 1 FROM users WHERE tg_id=? AND status='active'", (event.from_user.id,))
    return bool(r)


emp.message.filter(active_emp)
emp.callback_query.filter(active_emp)


class NewTask(StatesGroup):
    content = State()
    deadline = State()


class ReviewS(StatesGroup):
    comment = State()


class ResultS(StatesGroup):
    sending = State()


# ============================ /start ============================
@common.message(CommandStart())
async def start(m: Message, state: FSMContext, bot: Bot):
    await state.clear()
    uid = m.from_user.id
    if uid == ADMIN_ID:
        await m.answer("👋 Salom, admin!\nQuyidagi menyudan foydalaning.", reply_markup=admin_menu())
        return
    u = await one("SELECT * FROM users WHERE tg_id=?", (uid,))
    if not u:
        await run("INSERT INTO users(tg_id, full_name, username, status, created_at) VALUES(?,?,?,?,?)",
                  (uid, m.from_user.full_name, m.from_user.username, "pending", ts(now())))
        await m.answer("⏳ So'rovingiz adminga yuborildi. Tasdiqlangach xabar olasiz.")
        b = InlineKeyboardBuilder()
        b.button(text="✅ Tasdiqlash", callback_data=f"setuser:{uid}:active")
        b.button(text="🚫 Rad etish", callback_data=f"setuser:{uid}:removed")
        await safe(bot.send_message(
            ADMIN_ID, f"🆕 <b>Yangi hodim so'rovi</b>\n{esc(m.from_user.full_name)}"
                      f" (@{esc(m.from_user.username)})\nID: <code>{uid}</code>", reply_markup=b.as_markup()))
    elif u["status"] == "active":
        await m.answer("👋 Salom! Topshiriqlar shu yerga keladi.", reply_markup=emp_menu())
    elif u["status"] == "pending":
        await m.answer("⏳ Admin tasdiqlashini kuting.")
    else:
        await m.answer("⛔ Sizga ruxsat berilmagan.")


# ============================ ADMIN: MENYU ============================
@admin.message(F.text == "➕ Yangi topshiriq")
async def new_task(m: Message, state: FSMContext):
    await state.clear()
    emps = await all_("SELECT * FROM users WHERE status='active' ORDER BY full_name")
    if not emps:
        await m.answer("Hozircha tasdiqlangan hodim yo'q. Hodimlar botga /start yuborishi kerak.")
        return
    b = InlineKeyboardBuilder()
    for e in emps:
        b.button(text=e["full_name"], callback_data=f"emp:{e['tg_id']}")
    b.adjust(2)
    b.row(InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel"))
    await m.answer("👤 Kimga topshiriq berasiz?", reply_markup=b.as_markup())


@admin.message(F.text == "👥 Hodimlar")
async def employees(m: Message, state: FSMContext):
    await state.clear()
    text, kb = await render_employees()
    await m.answer(text, reply_markup=kb)


async def render_employees():
    rows = await all_("SELECT * FROM users ORDER BY status, full_name")
    if not rows:
        return "Hodimlar yo'q.", None
    names = {"active": "faol", "pending": "tasdiq kutmoqda", "removed": "o'chirilgan"}
    L = ["👥 <b>Hodimlar</b>\n"]
    b = InlineKeyboardBuilder()
    for r in rows:
        L.append(f"• {esc(r['full_name'])} — {names.get(r['status'], r['status'])}")
        if r["status"] == "active":
            b.button(text=f"🗑 {r['full_name']}", callback_data=f"setuser:{r['tg_id']}:removed")
        elif r["status"] == "pending":
            b.button(text=f"✅ {r['full_name']}", callback_data=f"setuser:{r['tg_id']}:active")
        else:
            b.button(text=f"♻️ {r['full_name']}", callback_data=f"setuser:{r['tg_id']}:active")
    b.adjust(2)
    return "\n".join(L), b.as_markup()


@admin.callback_query(F.data.startswith("setuser:"))
async def set_user(c: CallbackQuery, bot: Bot):
    _, uid, status = c.data.split(":")
    uid = int(uid)
    await run("UPDATE users SET status=? WHERE tg_id=?", (status, uid))
    if status == "active":
        await safe(bot.send_message(uid, "✅ Sizga ruxsat berildi! Topshiriqlar shu yerga keladi.",
                                    reply_markup=emp_menu()))
    text, kb = await render_employees()
    await safe(c.message.edit_text(text, reply_markup=kb))
    await c.answer("Bajarildi")


# ============================ ADMIN: /t{id} ============================
@admin.message(F.text.regexp(r"^/t\d+"))
async def admin_task_view(m: Message, state: FSMContext, bot: Bot):
    tid = int(re.match(r"/t(\d+)", m.text).group(1))
    t = await one("SELECT * FROM tasks WHERE id=?", (tid,))
    if not t:
        await m.answer("Topshiriq topilmadi.")
        return
    u = await one("SELECT full_name FROM users WHERE tg_id=?", (t["employee_id"],))
    await m.answer(task_card(t, u["full_name"] if u else "?"),
                   reply_markup=review_kb(tid) if t["status"] == "review" else None)
    items = await all_("SELECT * FROM items WHERE task_id=? AND kind='task' ORDER BY id", (tid,))
    if items:
        await m.answer("📝 <b>Topshiriq mazmuni:</b>")
        for it in items:
            await safe(bot.copy_message(ADMIN_ID, it["chat_id"], it["message_id"]))
    cur_round = None
    for it in await all_("SELECT * FROM items WHERE task_id=? AND kind='result' ORDER BY round, id", (tid,)):
        if it["round"] != cur_round:
            cur_round = it["round"]
            await m.answer(f"📎 <b>Hodim natijasi — {cur_round + 1}-yuborish:</b>")
        await safe(bot.copy_message(ADMIN_ID, it["chat_id"], it["message_id"]))


# ============================ ADMIN: TEKSHIRUV (qabul / rad) ============================
@admin.callback_query(F.data.startswith("ok:"))
async def review_ok(c: CallbackQuery, bot: Bot):
    tid = int(c.data.split(":")[1])
    t = await one("SELECT * FROM tasks WHERE id=?", (tid,))
    if not t or t["status"] != "review":
        await c.answer("Bu topshiriq allaqachon ko'rib chiqilgan", show_alert=True)
        return
    await run("UPDATE tasks SET status='done', done_at=? WHERE id=?", (ts(now()), tid))
    t = await one("SELECT * FROM tasks WHERE id=?", (tid,))
    u = await one("SELECT full_name FROM users WHERE tg_id=?", (t["employee_id"],))
    await safe(c.message.edit_text(task_card(t, u["full_name"] if u else "?")))
    await safe(bot.send_message(t["employee_id"],
                                f"🎉 <b>Topshiriq #{tid} qabul qilindi!</b>\n📝 {esc(t['preview'])}\nRahmat!"))
    await c.answer("Qabul qilindi")


@admin.callback_query(F.data.startswith("no:"))
async def review_no(c: CallbackQuery, state: FSMContext):
    tid = int(c.data.split(":")[1])
    t = await one("SELECT * FROM tasks WHERE id=?", (tid,))
    if not t or t["status"] != "review":
        await c.answer("Bu topshiriq allaqachon ko'rib chiqilgan", show_alert=True)
        return
    await state.set_state(ReviewS.comment)
    await state.update_data(task_id=tid, chat_id=c.message.chat.id, msg_id=c.message.message_id)
    await c.message.answer(f"❌ #{tid} topshiriq bo'yicha kamchiliklarni yozing.\n"
                           f"Izoh hodimga yuboriladi va u qayta yuboradi:")
    await c.answer()


@admin.message(ReviewS.comment, F.text)
async def review_comment(m: Message, state: FSMContext, bot: Bot):
    d = await state.get_data()
    tid = d["task_id"]
    t = await one("SELECT * FROM tasks WHERE id=?", (tid,))
    if not t or t["status"] != "review":
        await state.clear()
        await m.answer("Bu topshiriq allaqachon ko'rib chiqilgan.")
        return
    await run("UPDATE tasks SET status='revision', reject_reason=?, rejected_at=?, rev_count=rev_count+1 "
              "WHERE id=?", (m.text, ts(now()), tid))
    await state.clear()
    t = await one("SELECT * FROM tasks WHERE id=?", (tid,))
    u = await one("SELECT full_name FROM users WHERE tg_id=?", (t["employee_id"],))
    await safe(bot.edit_message_text(task_card(t, u["full_name"] if u else "?"),
                                     chat_id=d["chat_id"], message_id=d["msg_id"]))
    await m.answer("✅ Izoh hodimga yuborildi.")
    await safe(bot.send_message(
        t["employee_id"],
        f"↩️ <b>Topshiriq #{tid} qayta ishlash uchun qaytarildi.</b>\n\n💬 <b>Admin izohi:</b>\n{esc(m.text)}"))
    await safe(bot.send_message(t["employee_id"], task_card(t), reply_markup=employee_kb(t)))


@admin.message(ReviewS.comment)
async def review_comment_wrong(m: Message):
    await m.answer("❗ Iltimos, izohni matn bilan yozing.")


# ============================ ADMIN: HISOBOT ============================
async def render_report(flt: str, emp_id: int, page: int):
    n = ts(now())
    emp_sql = " AND employee_id=?" if emp_id else ""
    emp_args = [emp_id] if emp_id else []

    st = {r["status"]: r["c"] for r in await all_(
        f"SELECT status, COUNT(*) c FROM tasks WHERE 1=1{emp_sql} GROUP BY status", emp_args)}
    late_cnt = (await one(
        f"SELECT COUNT(*) c FROM tasks WHERE status IN {WORK_SQL} AND deadline < ?{emp_sql}",
        [n] + emp_args))["c"]

    where, args = [], []
    cond = FILTERS[flt][1]
    if cond:
        where.append(cond)
        if flt == "late":
            args.append(n)
    if emp_id:
        where.append("t.employee_id=?")
        args.append(emp_id)
    wsql = ("WHERE " + " AND ".join(where)) if where else ""
    total = (await one(f"SELECT COUNT(*) c FROM tasks t {wsql}", args))["c"]
    rows = await all_(
        f"SELECT t.*, u.full_name FROM tasks t LEFT JOIN users u ON u.tg_id=t.employee_id "
        f"{wsql} ORDER BY t.id DESC LIMIT ? OFFSET ?", args + [PAGE, page * PAGE])

    who = "Barcha hodimlar"
    if emp_id:
        u = await one("SELECT full_name FROM users WHERE tg_id=?", (emp_id,))
        who = u["full_name"] if u else "?"

    L = [f"📊 <b>Hisobot — {esc(who)}</b>",
         f"🆕 {st.get('new', 0)} | 🔄 {st.get('in_progress', 0)} | 🔎 {st.get('review', 0)} | "
         f"↩️ {st.get('revision', 0)} | ✅ {st.get('done', 0)} | 🔥 {late_cnt}",
         f"<i>Filtr: {FILTERS[flt][0]} — jami {total} ta</i>\n"]
    if not rows:
        L.append("Topshiriq yo'q.")
    for r in rows:
        late = r["status"] in WORK and r["deadline"] < n
        s = (f"<b>#{r['id']}</b> · 👤 {esc(r['full_name'])} · {STATUS[r['status']]}{' 🔥' if late else ''}\n"
             f"📝 {esc(r['preview'])}\n⏰ Muddat: {fmt(r['deadline'])}")
        if r["status"] == "review":
            s += f"\n📤 Yuborilgan: {fmt(r['submitted_at'])}"
        if r["status"] == "revision":
            s += f"\n💬 {esc(r['reject_reason'])}"
        if r["status"] == "done":
            s += f"\n✅ Qabul: {fmt(r['done_at'])}"
        L.append(s + f"\n/t{r['id']}\n")

    b = InlineKeyboardBuilder()
    for key, (label, _) in FILTERS.items():
        b.button(text=("• " if key == flt else "") + label, callback_data=f"rep:{key}:{emp_id}:0")
    b.adjust(3)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"rep:{flt}:{emp_id}:{page - 1}"))
    if (page + 1) * PAGE < total:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"rep:{flt}:{emp_id}:{page + 1}"))
    if nav:
        b.row(*nav)
    b.row(InlineKeyboardButton(text="👥 Hodimlar bo'yicha", callback_data="rep_emp"))
    if emp_id:
        b.row(InlineKeyboardButton(text="🌐 Hammasi", callback_data="rep:all:0:0"))
    return "\n".join(L), b.as_markup()


@admin.message(F.text == "📊 Hisobot")
async def report_menu(m: Message, state: FSMContext):
    await state.clear()
    text, kb = await render_report("all", 0, 0)
    await m.answer(text, reply_markup=kb)


@admin.callback_query(F.data.startswith("rep:"))
async def report_cb(c: CallbackQuery):
    _, flt, emp_id, page = c.data.split(":")
    text, kb = await render_report(flt, int(emp_id), int(page))
    await safe(c.message.edit_text(text, reply_markup=kb))
    await c.answer()


@admin.callback_query(F.data == "rep_emp")
async def report_emp(c: CallbackQuery):
    n = ts(now())
    rows = await all_(
        "SELECT u.tg_id, u.full_name, "
        f"SUM(CASE WHEN t.status IN {WORK_SQL} THEN 1 ELSE 0 END) act, "
        f"SUM(CASE WHEN t.status IN {WORK_SQL} AND t.deadline < ? THEN 1 ELSE 0 END) late, "
        "SUM(CASE WHEN t.status='review' THEN 1 ELSE 0 END) rev, "
        "SUM(CASE WHEN t.status='done' THEN 1 ELSE 0 END) done "
        "FROM users u LEFT JOIN tasks t ON t.employee_id=u.tg_id "
        "WHERE u.status='active' GROUP BY u.tg_id ORDER BY u.full_name", (n,))
    L = ["👥 <b>Hodimlar kesimida</b>\n"]
    b = InlineKeyboardBuilder()
    for r in rows:
        L.append(f"• {esc(r['full_name'])}: ishlayapti {r['act'] or 0}, tekshiruvda {r['rev'] or 0}, "
                 f"kechikkan {r['late'] or 0}, tugatgan {r['done'] or 0}")
        b.button(text=r["full_name"], callback_data=f"rep:all:{r['tg_id']}:0")
    b.adjust(2)
    b.row(InlineKeyboardButton(text="⬅️ Orqaga", callback_data="rep:all:0:0"))
    await safe(c.message.edit_text("\n".join(L), reply_markup=b.as_markup()))
    await c.answer()


# ============================ ADMIN: YANGI TOPSHIRIQ (FSM) ============================
def content_kb():
    b = InlineKeyboardBuilder()
    b.button(text="✅ Tayyor", callback_data="items_done")
    b.button(text="❌ Bekor qilish", callback_data="cancel")
    b.adjust(2)
    return b.as_markup()


@admin.callback_query(F.data == "cancel")
async def cancel_cb(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe(c.message.edit_text("❌ Bekor qilindi."))
    await c.answer()


@admin.callback_query(F.data.startswith("emp:"))
async def pick_emp(c: CallbackQuery, state: FSMContext):
    emp_id = int(c.data.split(":")[1])
    await state.set_state(NewTask.content)
    await state.update_data(emp_id=emp_id, items=[], preview=None)
    await c.message.edit_text(
        "📝 Topshiriq mazmunini yuboring: matn, rasm, fayl, ovozli xabar, video va h.k.\n"
        "Bir nechta xabar yuborishingiz mumkin. Tugagach <b>«✅ Tayyor»</b> tugmasini bosing.",
        reply_markup=content_kb())
    await c.answer()


@admin.message(NewTask.content)
async def collect(m: Message, state: FSMContext):
    d = await state.get_data()
    items = d["items"]
    items.append((m.chat.id, m.message_id))
    preview = d.get("preview") or preview_of(m)
    await state.update_data(items=items, preview=preview)
    await m.answer(f"✔️ Qo'shildi ({len(items)} ta xabar). Yana yuboring yoki «Tayyor» bosing.",
                   reply_markup=content_kb())


@admin.callback_query(NewTask.content, F.data == "items_done")
async def items_done(c: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    if not d.get("items"):
        await c.answer("Avval topshiriq mazmunini yuboring", show_alert=True)
        return
    await state.set_state(NewTask.deadline)
    n = now()
    opts = [
        ("Bugun 18:00", n.replace(hour=18, minute=0)),
        ("Ertaga 12:00", (n + timedelta(days=1)).replace(hour=12, minute=0)),
        ("Ertaga 18:00", (n + timedelta(days=1)).replace(hour=18, minute=0)),
        ("3 kundan keyin 18:00", (n + timedelta(days=3)).replace(hour=18, minute=0)),
        ("1 haftadan keyin 18:00", (n + timedelta(days=7)).replace(hour=18, minute=0)),
    ]
    b = InlineKeyboardBuilder()
    for label, dt in opts:
        if dt > n:
            b.button(text=label, callback_data=f"dl:{dt.strftime('%Y%m%d%H%M')}")
    b.adjust(2)
    b.row(InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel"))
    await c.message.edit_text(
        "⏰ Muddatni tanlang yoki o'zingiz yozing:\n<code>25.12.2026 18:00</code>",
        reply_markup=b.as_markup())
    await c.answer()


async def confirm_text(state: FSMContext) -> str:
    d = await state.get_data()
    u = await one("SELECT full_name FROM users WHERE tg_id=?", (d["emp_id"],))
    return (f"📋 <b>Tasdiqlang</b>\n\n👤 Hodim: <b>{esc(u['full_name'])}</b>\n"
            f"📝 {esc(d['preview'])}\n📎 Xabarlar soni: {len(d['items'])}\n"
            f"⏰ Muddat: <b>{fmt(d['deadline'])}</b>")


def confirm_kb():
    b = InlineKeyboardBuilder()
    b.button(text="🚀 Yuborish", callback_data="send_task")
    b.button(text="❌ Bekor qilish", callback_data="cancel")
    b.adjust(2)
    return b.as_markup()


@admin.callback_query(NewTask.deadline, F.data.startswith("dl:"))
async def deadline_btn(c: CallbackQuery, state: FSMContext):
    dt = datetime.strptime(c.data.split(":")[1], "%Y%m%d%H%M")
    if dt <= now():
        await c.answer("Bu vaqt o'tib ketgan", show_alert=True)
        return
    await state.update_data(deadline=ts(dt))
    await c.message.edit_text(await confirm_text(state), reply_markup=confirm_kb())
    await c.answer()


@admin.message(NewTask.deadline, F.text)
async def deadline_text(m: Message, state: FSMContext):
    dt = parse_deadline(m.text)
    if not dt:
        await m.answer("❗ Format noto'g'ri. Misol: <code>25.12.2026 18:00</code>")
        return
    if dt <= now():
        await m.answer("❗ Bu vaqt o'tib ketgan. Kelajakdagi vaqtni kiriting.")
        return
    await state.update_data(deadline=ts(dt))
    await m.answer(await confirm_text(state), reply_markup=confirm_kb())


@admin.callback_query(NewTask.deadline, F.data == "send_task")
async def send_task(c: CallbackQuery, state: FSMContext, bot: Bot):
    d = await state.get_data()
    tid = await run("INSERT INTO tasks(employee_id, preview, created_at, deadline) VALUES(?,?,?,?)",
                    (d["emp_id"], d["preview"], ts(now()), d["deadline"]))
    for chat_id, mid in d["items"]:
        await run("INSERT INTO items(task_id, kind, chat_id, message_id) VALUES(?, 'task', ?, ?)",
                  (tid, chat_id, mid))
    await state.clear()
    ok = await send_task_to_employee(
        bot, tid, "🔔 <b>Sizga yangi topshiriq berildi!</b>\n"
                  "Bajarib bo'lgach «📤 Natija yuborish» orqali adminga yuboring.")
    if ok:
        await c.message.edit_text(f"✅ Topshiriq #{tid} hodimga yuborildi. /t{tid}")
    else:
        await c.message.edit_text(f"⚠️ Topshiriq #{tid} saqlandi, lekin hodimga yuborib bo'lmadi "
                                  f"(hodim botni to'xtatgan bo'lishi mumkin).")
    await c.answer()


# ============================ HODIM ============================
@emp.message(F.text == "📋 Topshiriqlarim")
async def my_tasks(m: Message, state: FSMContext):
    await state.clear()
    rows = await all_(
        "SELECT * FROM tasks WHERE employee_id=? AND status IN ('new','in_progress','revision','review') "
        "ORDER BY deadline", (m.from_user.id,))
    if not rows:
        await m.answer("🎉 Faol topshiriqlar yo'q.")
        return
    n = ts(now())
    L = ["📋 <b>Topshiriqlaringiz:</b>\n"]
    for t in rows:
        late = " 🔥 KECHIKKAN" if t["status"] in WORK and t["deadline"] < n else ""
        L.append(f"<b>#{t['id']}</b> {esc(t['preview'])}\n⏰ {fmt(t['deadline'])}{late}\n"
                 f"{STATUS[t['status']]} · /t{t['id']}\n")
    await m.answer("\n".join(L))


@emp.message(F.text.regexp(r"^/t\d+"))
async def emp_task_view(m: Message, state: FSMContext, bot: Bot):
    await state.clear()
    tid = int(re.match(r"/t(\d+)", m.text).group(1))
    t = await one("SELECT * FROM tasks WHERE id=? AND employee_id=?", (tid, m.from_user.id))
    if not t:
        await m.answer("Topshiriq topilmadi.")
        return
    await send_task_to_employee(bot, tid, f"📌 Topshiriq #{tid} mazmuni:")


# ---- natija yuborish: bir nechta topshiriq bo'lsa tanlash ----
async def begin_submit(msg: Message, state: FSMContext, t):
    d_cnt = (await one("SELECT COUNT(*) c FROM items WHERE task_id=? AND kind='result' AND round=?",
                       (t["id"], t["rev_count"])))["c"]
    await state.set_state(ResultS.sending)
    await state.update_data(task_id=t["id"])
    text = f"📤 <b>Topshiriq #{t['id']}</b> — {esc(t['preview'])}\n"
    if t["status"] == "revision" and t["reject_reason"]:
        text += f"\n💬 <b>Admin izohi:</b> {esc(t['reject_reason'])}\n"
    text += ("\nNatijani yuboring: fayl, rasm, matn, ovozli xabar va h.k. (bir nechta bo'lishi mumkin).\n"
             "Tugagach <b>«📤 Adminga yuborish»</b> tugmasini bosing.")
    if d_cnt:
        text += f"\n\n<i>Tayyorlangan xabarlar: {d_cnt} ta</i>"
    await msg.answer(text, reply_markup=submit_kb(t["id"]))


def submit_kb(tid: int):
    b = InlineKeyboardBuilder()
    b.button(text="📤 Adminga yuborish", callback_data=f"res_send:{tid}")
    b.button(text="↩️ Bekor", callback_data="res_cancel")
    b.adjust(2)
    return b.as_markup()


@emp.message(F.text == "📤 Natija yuborish")
async def submit_menu(m: Message, state: FSMContext):
    await state.clear()
    rows = await all_(f"SELECT * FROM tasks WHERE employee_id=? AND status IN {WORK_SQL} ORDER BY deadline",
                      (m.from_user.id,))
    if not rows:
        await m.answer("Hozir natija yuborishingiz kerak bo'lgan topshiriq yo'q.")
        return
    if len(rows) == 1:
        await begin_submit(m, state, rows[0])
        return
    b = InlineKeyboardBuilder()
    for t in rows:
        prev = t["preview"][:25]
        d = datetime.strptime(t["deadline"], "%Y-%m-%d %H:%M").strftime("%d.%m %H:%M")
        mark = "↩️ " if t["status"] == "revision" else ""
        b.button(text=f"{mark}#{t['id']} · {prev} · ⏰{d}", callback_data=f"sub:{t['id']}")
    b.adjust(1)
    await m.answer("📤 Qaysi topshiriq bo'yicha natija yubormoqchisiz?", reply_markup=b.as_markup())


async def my_task_or_alert(c: CallbackQuery):
    tid = int(c.data.split(":")[1])
    t = await one("SELECT * FROM tasks WHERE id=? AND employee_id=?", (tid, c.from_user.id))
    if not t:
        await c.answer("Topshiriq topilmadi", show_alert=True)
        return None
    return t


@emp.callback_query(F.data.startswith("sub:"))
async def submit_pick(c: CallbackQuery, state: FSMContext):
    t = await my_task_or_alert(c)
    if not t:
        return
    if t["status"] not in WORK:
        await c.answer("Bu topshiriq hozir sizdan natija kutmayapti", show_alert=True)
        return
    await begin_submit(c.message, state, t)
    await c.answer()


@emp.callback_query(F.data.startswith("prog:"))
async def in_progress(c: CallbackQuery, bot: Bot):
    t = await my_task_or_alert(c)
    if not t:
        return
    if t["status"] != "new":
        await c.answer("Holat o'zgargan", show_alert=True)
        return
    await run("UPDATE tasks SET status='in_progress', started_at=? WHERE id=?", (ts(now()), t["id"]))
    t = await one("SELECT * FROM tasks WHERE id=?", (t["id"],))
    await safe(c.message.edit_text(task_card(t), reply_markup=employee_kb(t)))
    await notify_admin(bot, f"🔄 <b>{esc(c.from_user.full_name)}</b> #{t['id']} topshiriqni "
                            f"tayyorlashni boshladi.\n📝 {esc(t['preview'])}\n/t{t['id']}")
    await c.answer("Holat yangilandi")


@emp.message(ResultS.sending)
async def result_msg(m: Message, state: FSMContext):
    d = await state.get_data()
    t = await one("SELECT * FROM tasks WHERE id=?", (d["task_id"],))
    if not t or t["status"] not in WORK:
        await state.clear()
        await m.answer("Bu topshiriq hozir natija kutmayapti.")
        return
    await run("INSERT INTO items(task_id, kind, chat_id, message_id, round) VALUES(?, 'result', ?, ?, ?)",
              (t["id"], m.chat.id, m.message_id, t["rev_count"]))
    cnt = (await one("SELECT COUNT(*) c FROM items WHERE task_id=? AND kind='result' AND round=?",
                     (t["id"], t["rev_count"])))["c"]
    await m.answer(f"✔️ Qabul qilindi ({cnt} ta xabar). Yana yuborishingiz yoki "
                   f"«Adminga yuborish» bosishingiz mumkin.", reply_markup=submit_kb(t["id"]))


@emp.callback_query(F.data == "res_cancel")
async def result_cancel(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe(c.message.edit_text("↩️ Bekor qilindi. Tayyorlangan xabarlar saqlanib qoladi."))
    await c.answer()


@emp.callback_query(F.data.startswith("res_send:"))
async def result_send(c: CallbackQuery, state: FSMContext, bot: Bot):
    t = await my_task_or_alert(c)
    if not t:
        return
    if t["status"] not in WORK:
        await c.answer("Bu topshiriq allaqachon yuborilgan", show_alert=True)
        return
    results = await all_("SELECT * FROM items WHERE task_id=? AND kind='result' AND round=? ORDER BY id",
                         (t["id"], t["rev_count"]))
    if not results:
        await c.answer("Avval natijani (fayl, rasm yoki matn) yuboring", show_alert=True)
        return
    await run("UPDATE tasks SET status='review', submitted_at=? WHERE id=?", (ts(now()), t["id"]))
    await state.clear()
    t = await one("SELECT * FROM tasks WHERE id=?", (t["id"],))
    await safe(c.message.edit_text(f"✅ #{t['id']} natijasi adminga yuborildi. Admin tekshirib javob beradi."))
    # adminga: sarlavha + natija xabarlari + qabul/rad tugmalari
    await notify_admin(bot, f"📤 <b>{esc(c.from_user.full_name)}</b> #{t['id']} bo'yicha natija yubordi"
                            f"{' (qayta yuborish)' if t['rev_count'] else ''}:")
    for it in results:
        await safe(bot.copy_message(ADMIN_ID, it["chat_id"], it["message_id"]))
    await safe(bot.send_message(ADMIN_ID, task_card(t, c.from_user.full_name), reply_markup=review_kb(t["id"])))
    await c.answer("Yuborildi")


# ============================ ESLATMALAR ============================
async def daily_reminders(bot: Bot):
    n = ts(now())
    emps = await all_(
        f"SELECT DISTINCT u.* FROM users u JOIN tasks t ON t.employee_id=u.tg_id "
        f"WHERE u.status='active' AND t.status IN {WORK_SQL}")
    for u in emps:
        rows = await all_(f"SELECT * FROM tasks WHERE employee_id=? AND status IN {WORK_SQL} ORDER BY deadline",
                          (u["tg_id"],))
        L = ["☀️ <b>Xayrli tong! Bajarilishi kerak bo'lgan topshiriqlaringiz:</b>\n"]
        for t in rows:
            late = " 🔥 <b>KECHIKKAN</b>" if t["deadline"] < n else ""
            L.append(f"<b>#{t['id']}</b> {esc(t['preview'])}\n⏰ {fmt(t['deadline'])}{late}\n"
                     f"{STATUS[t['status']]} · /t{t['id']}\n")
        L.append("Tayyor bo'lsa «📤 Natija yuborish» tugmasini bosing.")
        await safe(bot.send_message(u["tg_id"], "\n".join(L)))

    stats = await one(f"SELECT COUNT(*) c, SUM(CASE WHEN deadline < ? THEN 1 ELSE 0 END) late "
                      f"FROM tasks WHERE status IN {WORK_SQL}", (n,))
    rev = (await one("SELECT COUNT(*) c FROM tasks WHERE status='review'"))["c"]
    if stats["c"] or rev:
        await notify_admin(bot, f"☀️ <b>Kunlik xulosa</b>\nIshlanayotgan: {stats['c']}\n"
                                f"🔥 Kechikkan: {stats['late'] or 0}\n"
                                f"🔎 Sizning tekshiruvingizni kutayotgan: {rev}\n\n📊 Hisobot menyusini oching.")


async def deadline_checks(bot: Bot):
    n = now()
    soon = await all_(
        f"SELECT t.* FROM tasks t WHERE t.status IN {WORK_SQL} AND t.warned=0 "
        f"AND t.deadline > ? AND t.deadline <= ?",
        (ts(n), ts(n + timedelta(hours=WARN_BEFORE_HOURS))))
    for t in soon:
        await run("UPDATE tasks SET warned=1 WHERE id=?", (t["id"],))
        await safe(bot.send_message(
            t["employee_id"], f"⏳ <b>Muddat yaqinlashmoqda!</b>\n#{t['id']} {esc(t['preview'])}\n"
                              f"⏰ {fmt(t['deadline'])}\n/t{t['id']}"))
    late = await all_(
        f"SELECT t.*, u.full_name FROM tasks t LEFT JOIN users u ON u.tg_id=t.employee_id "
        f"WHERE t.status IN {WORK_SQL} AND t.overdue_sent=0 AND t.deadline <= ?", (ts(n),))
    for t in late:
        await run("UPDATE tasks SET overdue_sent=1 WHERE id=?", (t["id"],))
        await safe(bot.send_message(
            t["employee_id"], f"🔥 <b>Muddat o'tdi!</b>\n#{t['id']} {esc(t['preview'])}\n"
                              f"⏰ {fmt(t['deadline'])}\n/t{t['id']}"))
        await notify_admin(bot, f"🔥 <b>Muddat o'tdi:</b> #{t['id']} — {esc(t['full_name'])}\n"
                                f"📝 {esc(t['preview'])}\n📍 {STATUS[t['status']]}\n/t{t['id']}")


async def scheduler(bot: Bot):
    while True:
        try:
            n = now()
            today = n.strftime("%Y-%m-%d")
            last = await one("SELECT value FROM meta WHERE key='last_remind'")
            if n.hour >= REMIND_HOUR and (not last or last["value"] != today):
                await run("INSERT OR REPLACE INTO meta(key, value) VALUES('last_remind', ?)", (today,))
                await daily_reminders(bot)
            await deadline_checks(bot)
        except Exception:
            logging.exception("Scheduler xatosi")
        await asyncio.sleep(60)


# ============================ ISHGA TUSHIRISH ============================
async def main():
    logging.basicConfig(level=logging.INFO)
    await init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_routers(common, admin, emp)
    task = asyncio.create_task(scheduler(bot))
    try:
        await dp.start_polling(bot)
    finally:
        task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
