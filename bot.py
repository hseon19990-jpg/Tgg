import asyncio
import html
import logging
import os
import secrets
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyParameters,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("anon-bot")

# ───────────── المتغيرات (تُضبط داخل Railway) ─────────────
BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
LINK_CODE_ENV = os.getenv("LINK_CODE", "").strip()          # كود الرابط الخاص (اختياري)
REQUIRE_LINK = os.getenv("REQUIRE_LINK", "true").lower() in ("1", "true", "yes")
TIMEZONE = os.getenv("TIMEZONE", "UTC")
DB_PATH = os.getenv("DB_PATH", "data/bot.db")
WELCOME_TEXT = os.getenv(
    "WELCOME_TEXT",
    "👋 أهلاً بك!\n\n"
    "يمكنك الآن إرسال رسالتك وستصل إلى المالك <b>بشكل مجهول</b> تماماً 🔒\n\n"
    "يمكنك إرسال نصوص، صور، فيديو أو ملصقات.",
)
NEED_LINK_TEXT = os.getenv(
    "NEED_LINK_TEXT",
    "⚠️ للتواصل مع المالك يجب الدخول عبر رابطه الخاص.",
)

TZ = ZoneInfo(TIMEZONE)

SUPPORTED = {"text", "photo", "video", "sticker", "animation",
             "document", "audio", "voice", "video_note"}
CAPTIONABLE = {"photo", "video", "animation", "document", "audio", "voice"}
LABELS = {
    "sticker": "ملصق", "video_note": "رسالة فيديو", "photo": "صورة",
    "video": "فيديو", "animation": "GIF", "document": "ملف",
    "audio": "ملف صوتي", "voice": "رسالة صوتية",
}


# ───────────── قاعدة البيانات ─────────────
class DB:
    def __init__(self, path: str):
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        self.c = sqlite3.connect(path, check_same_thread=False)
        self.c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users(
                id INTEGER PRIMARY KEY,
                name TEXT DEFAULT '',
                username TEXT DEFAULT '',
                allowed INTEGER DEFAULT 0,
                blocked INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS msgmap(
                msg_id INTEGER PRIMARY KEY,
                user_id INTEGER
            );
            CREATE TABLE IF NOT EXISTS settings(
                key TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )
        self.c.commit()

    def q(self, sql, args=()):
        cur = self.c.execute(sql, args)
        self.c.commit()
        return cur

    # الإعدادات
    def get_setting(self, key):
        r = self.q("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r[0] if r else None

    def set_setting(self, key, value):
        self.q(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # المستخدمون
    def upsert_user(self, u):
        self.q(
            "INSERT INTO users(id,name,username) VALUES(?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, username=excluded.username",
            (u.id, u.full_name, u.username or ""),
        )

    def allow(self, uid):
        self.q(
            "INSERT INTO users(id,allowed) VALUES(?,1) "
            "ON CONFLICT(id) DO UPDATE SET allowed=1",
            (uid,),
        )

    def is_allowed(self, uid):
        r = self.q("SELECT allowed FROM users WHERE id=?", (uid,)).fetchone()
        return bool(r and r[0])

    def block(self, uid):
        self.q(
            "INSERT INTO users(id,blocked) VALUES(?,1) "
            "ON CONFLICT(id) DO UPDATE SET blocked=1",
            (uid,),
        )

    def unblock(self, uid):
        self.q("UPDATE users SET blocked=0 WHERE id=?", (uid,))

    def is_blocked(self, uid):
        r = self.q("SELECT blocked FROM users WHERE id=?", (uid,)).fetchone()
        return bool(r and r[0])

    def blocked_list(self):
        return self.q("SELECT id,name,username FROM users WHERE blocked=1").fetchall()

    # ربط رسائل المالك بالمرسل
    def map(self, msg_id, uid):
        self.q("INSERT OR REPLACE INTO msgmap(msg_id,user_id) VALUES(?,?)", (msg_id, uid))

    def lookup(self, msg_id):
        r = self.q("SELECT user_id FROM msgmap WHERE msg_id=?", (msg_id,)).fetchone()
        return r[0] if r else None


db = DB(DB_PATH)

# الرسائل الثابتة القابلة للتعديل من المالك
MSG_META = {
    "welcome": ("👋 رسالة الترحيب", WELCOME_TEXT),
    "need_link": ("⚠️ رسالة طلب الرابط الخاص", NEED_LINK_TEXT),
    "sent_ok": ("✅ رسالة تأكيد الإرسال", "✅ تم إرسال رسالتك بشكل مجهول."),
    "unsupported": ("🚫 رسالة النوع غير المدعوم", "⚠️ هذا النوع من الرسائل غير مدعوم."),
    "error": ("❌ رسالة الخطأ", "❌ حدث خطأ أثناء الإرسال، حاول لاحقاً."),
    "new_title": ("💌 عنوان الرسالة الواصلة إليك", "💌 وصلتك رسالة جديدة"),
    "footer": ("📝 السطر الأخير أسفل الرسالة الواصلة (فارغ = بدون)", ""),
}


def get_msg(key: str) -> str:
    return db.get_setting("msg:" + key) or MSG_META[key][1]


def build(body_html: str) -> str:
    """شكل الرسالة الثانية التي تصل للمالك."""
    now = datetime.now(TZ).strftime("%Y/%m/%d - %I:%M:%S %p")
    text = "\n".join(
        [get_msg("new_title"), f"⏱ وقت الرسالة: {now}", "----", body_html, "----"]
    )
    footer = get_msg("footer")
    if footer:
        text += "\n\n" + footer
    return text


# ───────────── أدوات مساعدة ─────────────
_bot_username = None


async def bot_username(bot: Bot) -> str:
    global _bot_username
    if not _bot_username:
        _bot_username = (await bot.get_me()).username
    return _bot_username


def get_code() -> str:
    code = db.get_setting("link_code")
    if not code:
        code = LINK_CODE_ENV or secrets.token_urlsafe(6)
        db.set_setting("link_code", code)
    return code


async def owner_link(bot: Bot) -> str:
    return f"https://t.me/{await bot_username(bot)}?start={get_code()}"


def fit(text: str, limit: int) -> str:
    """يقصّ النص حتى يتسع بعد الـ escape ضمن الحد."""
    text = text or ""
    while len(html.escape(text)) > limit:
        text = text[:-20]
    return html.escape(text)


async def safe_edit(c: CallbackQuery, text: str, kb=None):
    try:
        await c.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass


def btn(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


def kb_of(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows])


def home_kb():
    return kb_of(
        [btn("🔗 رابطي الخاص", "set:link"), btn("♻️ تجديد الرابط", "set:newlink")],
        [btn("🚫 حظر بالأيدي", "set:block"), btn("✅ رفع حظر بالأيدي", "set:unblock")],
        [btn("📋 قائمة المحظورين", "set:list")],
        [btn("✏️ تعديل الرسائل", "set:msgs")],
    )


def back_kb():
    return kb_of([btn("⬅️ رجوع", "set:home")])


async def home_text(bot: Bot) -> str:
    link = await owner_link(bot)
    return (
        "👑 <b>لوحة المالك</b>\n\n"
        f"🔗 رابطك الخاص:\n<code>{link}</code>\n\n"
        "شاركه مع من تريد ليراسلك بشكل مجهول.\n\n"
        "💡 للرد على شخص أو حظره: قم بالرد (Reply) على رسالته."
    )


class S(StatesGroup):
    block = State()
    unblock = State()
    edit = State()


# ───────────── راوتر المالك ─────────────
owner = Router()
owner.message.filter(F.from_user.id == OWNER_ID)
owner.callback_query.filter(F.from_user.id == OWNER_ID)


@owner.message(CommandStart())
@owner.message(Command("settings"))
async def owner_home(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(await home_text(m.bot), reply_markup=home_kb())


@owner.callback_query(F.data == "set:home")
async def cb_home(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(c, await home_text(c.bot), home_kb())
    await c.answer()


@owner.callback_query(F.data == "set:link")
async def cb_link(c: CallbackQuery):
    link = await owner_link(c.bot)
    await safe_edit(c, f"🔗 رابطك الخاص:\n\n<code>{link}</code>", back_kb())
    await c.answer()


@owner.callback_query(F.data == "set:newlink")
async def cb_newlink(c: CallbackQuery):
    db.set_setting("link_code", secrets.token_urlsafe(6))
    link = await owner_link(c.bot)
    await safe_edit(
        c,
        "♻️ تم إنشاء رابط جديد:\n\n"
        f"<code>{link}</code>\n\n"
        "الرابط القديم لن يعمل مع الأشخاص الجدد (من دخل سابقاً يبقى بإمكانه المراسلة).",
        back_kb(),
    )
    await c.answer("تم التجديد")


@owner.callback_query(F.data == "set:block")
async def cb_block(c: CallbackQuery, state: FSMContext):
    await state.set_state(S.block)
    await safe_edit(c, "🚫 أرسل <b>أيدي</b> الشخص الذي تريد حظره:", back_kb())
    await c.answer()


@owner.callback_query(F.data == "set:unblock")
async def cb_unblock(c: CallbackQuery, state: FSMContext):
    await state.set_state(S.unblock)
    await safe_edit(c, "✅ أرسل <b>أيدي</b> الشخص الذي تريد رفع الحظر عنه:", back_kb())
    await c.answer()


def list_view():
    rows = db.blocked_list()
    if not rows:
        return "📋 لا يوجد محظورون حالياً.", back_kb()
    buttons = []
    for uid, name, username in rows[:30]:
        label = name or username or str(uid)
        buttons.append([btn(f"✅ رفع الحظر عن {label}"[:60], f"unban:{uid}:list")])
    buttons.append([btn("⬅️ رجوع", "set:home")])
    text = f"📋 <b>المحظورون ({len(rows)})</b>\n\n" + "\n".join(
        f"• <code>{uid}</code> {html.escape(name or '')}" for uid, name, _ in rows[:30]
    )
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


@owner.callback_query(F.data == "set:list")
async def cb_list(c: CallbackQuery):
    text, kb = list_view()
    await safe_edit(c, text, kb)
    await c.answer()


@owner.callback_query(F.data == "set:msgs")
async def cb_msgs(c: CallbackQuery, state: FSMContext):
    await state.clear()
    rows = [[btn(label, f"msg:{key}")] for key, (label, _) in MSG_META.items()]
    rows.append([btn("⬅️ رجوع", "set:home")])
    await safe_edit(
        c,
        "✏️ <b>تعديل الرسائل</b>\n\nاختر الرسالة التي تريد تغييرها:",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await c.answer()


@owner.callback_query(F.data.startswith("msg:"))
async def cb_msg_pick(c: CallbackQuery, state: FSMContext):
    key = c.data.split(":", 1)[1]
    if key not in MSG_META:
        await c.answer()
        return
    await state.set_state(S.edit)
    await state.update_data(key=key)
    await safe_edit(
        c,
        f"{MSG_META[key][0]}\n\n<b>النص الحالي:</b>\n{get_msg(key) or "(فارغ)"}\n\n"
        "✍️ أرسل النص الجديد الآن (يدعم التنسيق: عريض، مائل...).",
        kb_of(
            [btn("↩️ استعادة الافتراضي", f"msgreset:{key}")],
            [btn("⬅️ رجوع", "set:msgs")],
        ),
    )
    await c.answer()


@owner.callback_query(F.data.startswith("msgreset:"))
async def cb_msg_reset(c: CallbackQuery, state: FSMContext):
    key = c.data.split(":", 1)[1]
    if key in MSG_META:
        db.q("DELETE FROM settings WHERE key=?", ("msg:" + key,))
        await state.clear()
        await safe_edit(
            c,
            "↩️ تمت استعادة النص الافتراضي:\n\n" + (get_msg(key) or "(فارغ)"),
            kb_of([btn("⬅️ رجوع", "set:msgs")]),
        )
    await c.answer("تمت الاستعادة")


@owner.message(S.edit)
async def do_edit(m: Message, state: FSMContext):
    key = (await state.get_data()).get("key")
    if key not in MSG_META or not m.text:
        await m.answer("❌ أرسل نصاً فقط.")
        return
    new = m.html_text
    if len(new) > 3500:
        await m.answer("❌ النص طويل جداً، اختصره قليلاً.")
        return
    db.set_setting("msg:" + key, new)
    await state.clear()
    await m.answer(
        f"✅ تم تحديث «{MSG_META[key][0]}»:\n\n{new}",
        reply_markup=kb_of([btn("⬅️ رجوع", "set:msgs")]),
    )


@owner.message(S.block)
async def do_block(m: Message, state: FSMContext):
    txt = (m.text or "").strip()
    if not txt.isdigit():
        await m.answer("❌ أرسل أيدي صحيح (أرقام فقط).")
        return
    uid = int(txt)
    if uid == OWNER_ID:
        await m.answer("❌ لا يمكنك حظر نفسك.")
        return
    db.block(uid)
    await state.clear()
    await m.answer(f"🚫 تم حظر <code>{uid}</code>", reply_markup=back_kb())


@owner.message(S.unblock)
async def do_unblock(m: Message, state: FSMContext):
    txt = (m.text or "").strip()
    if not txt.isdigit():
        await m.answer("❌ أرسل أيدي صحيح (أرقام فقط).")
        return
    db.unblock(int(txt))
    await state.clear()
    await m.answer(f"✅ تم رفع الحظر عن <code>{txt}</code>", reply_markup=back_kb())


@owner.callback_query(F.data.startswith("ban:"))
async def cb_ban(c: CallbackQuery):
    uid = int(c.data.split(":")[1])
    db.block(uid)
    await safe_edit(
        c,
        f"🚫 تم حظر <code>{uid}</code>",
        kb_of([btn("✅ رفع الحظر", f"unban:{uid}:one")]),
    )
    await c.answer("تم الحظر")


@owner.callback_query(F.data.startswith("unban:"))
async def cb_unban(c: CallbackQuery):
    _, uid, origin = c.data.split(":")
    db.unblock(int(uid))
    if origin == "list":
        text, kb = list_view()
        await safe_edit(c, text, kb)
    else:
        await safe_edit(c, f"✅ تم رفع الحظر عن <code>{uid}</code>")
    await c.answer("تم رفع الحظر")


@owner.callback_query(F.data.startswith("rep:"))
async def cb_reply(c: CallbackQuery):
    _, uid, mid = c.data.split(":")
    try:
        await c.bot.copy_message(int(uid), c.message.chat.id, int(mid))
        await safe_edit(c, "✅ تم إرسال ردك.")
    except TelegramForbiddenError:
        await safe_edit(c, "❌ تعذر الإرسال (المستخدم أوقف البوت).")
    except TelegramBadRequest as e:
        await safe_edit(c, f"❌ تعذر الإرسال: {html.escape(str(e))}")
    await c.answer()


@owner.message(F.reply_to_message)
async def on_owner_reply(m: Message):
    uid = db.lookup(m.reply_to_message.message_id)
    if not uid:
        await m.answer("⚠️ لم أستطع تحديد مرسل هذه الرسالة.")
        return
    # اختصار: كتابة "حظر" كرد تحظر مباشرة
    if (m.text or "").strip() in ("حظر", "/ban", "/block"):
        db.block(uid)
        await m.reply(
            f"🚫 تم حظر <code>{uid}</code>",
            reply_markup=kb_of([btn("✅ رفع الحظر", f"unban:{uid}:one")]),
        )
        return
    await m.reply(
        f"اختر إجراء للمرسل <code>{uid}</code>:",
        reply_markup=kb_of(
            [btn("💬 إرسال هذا الرد للمرسل", f"rep:{uid}:{m.message_id}")],
            [btn("🚫 حظر المرسل", f"ban:{uid}")],
        ),
    )


@owner.message()
async def owner_other(m: Message):
    await m.answer("ℹ️ قم بالرد (Reply) على رسالة مرسل للرد عليه أو حظره، أو افتح /settings")


# ───────────── راوتر المستخدمين ─────────────
users = Router()
users.message.filter(F.chat.type == "private", F.from_user.id != OWNER_ID)


@users.message(CommandStart())
async def user_start(m: Message, command: CommandObject):
    uid = m.from_user.id
    if db.is_blocked(uid):
        return
    payload = (command.args or "").strip()
    if payload and payload == get_code():
        db.allow(uid)
    if REQUIRE_LINK and not db.is_allowed(uid):
        await m.answer(get_msg("need_link"))
        return
    db.upsert_user(m.from_user)
    await m.answer(get_msg("welcome"))


@users.message()
async def user_message(m: Message):
    u = m.from_user
    uid = u.id
    if db.is_blocked(uid):
        return
    if REQUIRE_LINK and not db.is_allowed(uid):
        await m.answer(get_msg("need_link"))
        return

    ct = str(getattr(m.content_type, "value", m.content_type))
    if ct not in SUPPORTED:
        await m.answer(get_msg("unsupported"))
        return

    db.upsert_user(u)
    bot = m.bot
    uname = f"@{u.username}" if u.username else "لا يوجد"
    info = (
        f'👤 <a href="tg://openmessage?user_id={uid}">{html.escape(u.full_name)}</a>\n'
        f"🆔 الأيدي: <code>{uid}</code>\n"
        f"🔗 اليوزر: {html.escape(uname)}"
    )

    try:
        info_msg = await bot.send_message(OWNER_ID, info)
        db.map(info_msg.message_id, uid)
        reply_to = ReplyParameters(message_id=info_msg.message_id)

        if ct == "text":
            body = fit(m.text, max(4000 - len(build("")), 100))
            sent = await bot.send_message(
                OWNER_ID, build(body), reply_parameters=reply_to
            )
            db.map(sent.message_id, uid)
        elif ct in CAPTIONABLE:
            if m.caption:
                caption_text = fit(m.caption, max(1000 - len(build("")), 0))
            else:
                caption_text = f"({LABELS.get(ct, ct)})"
            cap = build(caption_text)
            r = await m.copy_to(
                OWNER_ID, caption=cap, parse_mode=ParseMode.HTML, reply_parameters=reply_to
            )
            db.map(r.message_id, uid)
        else:  # ملصق / رسالة فيديو لا تدعم تعليقاً
            label = LABELS.get(ct, ct)
            sent = await bot.send_message(
                OWNER_ID, build(f"({label})"), reply_parameters=reply_to
            )
            db.map(sent.message_id, uid)
            r = await m.copy_to(
                OWNER_ID, reply_parameters=ReplyParameters(message_id=sent.message_id)
            )
            db.map(r.message_id, uid)
    except Exception:
        log.exception("failed to deliver message")
        await m.answer(get_msg("error"))
        return

    await m.answer(get_msg("sent_ok"))


# ───────────── التشغيل ─────────────
async def main():
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(owner)
    dp.include_router(users)
    get_code()
    await bot.delete_webhook(drop_pending_updates=True)
    log.info("Bot started. Owner link: %s", await owner_link(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
