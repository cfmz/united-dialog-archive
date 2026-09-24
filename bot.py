import asyncio, sqlite3, random, html, logging, time
from datetime import datetime, timezone, timedelta
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, BaseFilter
from aiogram.types import (
    LabeledPrice, PreCheckoutQuery,
    InputRichMessage,

    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    BusinessConnection, BusinessMessagesDeleted, CopyTextButton,
)
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.client.default import DefaultBotProperties
import games
from aiogram.methods.send_rich_message import SendRichMessage

TOKEN = "8824105826:AAFlhSEtTVzGxFY7l3EF6OOAP4QaRzpWJ_Q"
DB = "united_dialog.db"
BRAND = "United Dialog"
BOT_USERNAME = "UnitedDialogBot"
ADMIN_ID = 7113397602
GITHUB_USER = "cfmz"
REPO = "united-dialog-archive"
SUPPORT = "https://t.me/UnitedDialogSupport"

NL = chr(10)
logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("united_dialog")

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# ===== АДМИН-ПАНЕЛЬ (приоритетный хендлер) =====
@dp.message(F.text == "/adm")
@dp.message(F.text == ".adm")
async def cmd_adm(m: Message):
    if m.from_user.id != ADMIN_ID:
        return
    ADMIN_WAIT.pop(m.from_user.id, None)
    await m.answer(
        "🛡 <b>Админ-панель</b>" + NL + NL + q("Выбери действие 👇"),
        reply_markup=kb_admin())


_AFK_LAST = {}
ADMIN_WAIT = {}
_NOTIFY_TS = {}

def now_utc():
    return datetime.now(timezone.utc)

def db():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; return c

def init_db():
    c = db()
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY, username TEXT, full_name TEXT,
        joined TEXT, is_premium INTEGER DEFAULT 0,
        ucoin INTEGER DEFAULT 0, last_seen TEXT, messages INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS connections(
        id TEXT PRIMARY KEY, user_id INTEGER, user_chat_id INTEGER,
        can_reply INTEGER DEFAULT 0, can_read INTEGER DEFAULT 0,
        can_delete INTEGER DEFAULT 0, is_enabled INTEGER DEFAULT 1,
        web_token TEXT, notify_on INTEGER DEFAULT 1, created TEXT
    );
    CREATE TABLE IF NOT EXISTS saved_messages(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        connection_id TEXT, chat_id INTEGER, message_id INTEGER,
        from_id INTEGER, from_name TEXT, from_username TEXT,
        text TEXT, media_type TEXT, file_id TEXT, owner_id INTEGER, created TEXT
    );
    CREATE TABLE IF NOT EXISTS payments(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, plan TEXT, method TEXT,
        amount INTEGER, created TEXT
    );
    CREATE TABLE IF NOT EXISTS promocodes(
        code TEXT PRIMARY KEY, days INTEGER,
        uses INTEGER DEFAULT 0, max_uses INTEGER DEFAULT 1, created TEXT
    );
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS afk(
        owner_id INTEGER, chat_id INTEGER, message TEXT,
        enabled INTEGER DEFAULT 1, created TEXT,
        PRIMARY KEY(owner_id, chat_id)
    );
    """)
    c.commit(); c.close()

def get_setting(key, default=None):
    c = db()
    r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    c.close()
    return r["value"] if r else default

def set_setting(key, value):
    c = db()
    c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
              (key, str(value)))
    c.commit(); c.close()

def maintenance_on():
    return get_setting("maintenance", "0") == "1"

def get_user(uid, uname=None, full_name=None, is_premium=None):
    c = db()
    u = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not u:
        c.execute("INSERT INTO users(id,username,full_name,joined,is_premium) VALUES(?,?,?,?,?)",
                  (uid, uname or "", full_name or "", now_utc().isoformat(),
                   int(bool(is_premium)) if is_premium is not None else 0))
        c.commit()
        u = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    else:
        upd = {}
        if uname and u["username"] != uname: upd["username"] = uname
        if full_name and u["full_name"] != full_name: upd["full_name"] = full_name
        if is_premium is not None and bool(u["is_premium"]) != bool(is_premium):
            upd["is_premium"] = int(bool(is_premium))
        if upd:
            c.execute("UPDATE users SET " + ", ".join(f"{k}=?" for k in upd) + " WHERE id=?",
                      (*upd.values(), uid))
            c.commit()
            u = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    c.close()
    return u

def touch_seen(uid):
    c = db()
    c.execute("UPDATE users SET last_seen=? WHERE id=?", (now_utc().isoformat(), uid))
    c.commit(); c.close()

def is_online(uid, minutes=5):
    c = db()
    r = c.execute("SELECT last_seen FROM users WHERE id=?", (uid,)).fetchone()
    c.close()
    if not r or not r["last_seen"]: return False
    try:
        last = datetime.fromisoformat(r["last_seen"])
        if last.tzinfo is None: last = last.replace(tzinfo=timezone.utc)
        return (now_utc() - last).total_seconds() < minutes * 60
    except: return False

def save_connection(conn):
    c = db()
    enabled = 1 if conn.is_enabled else 0
    c.execute("""INSERT INTO connections(id,user_id,user_chat_id,can_reply,can_read,can_delete,is_enabled,created)
                 VALUES(?,?,?,?,?,?,?,?)
                 ON CONFLICT(id) DO UPDATE SET user_id=excluded.user_id,
                 user_chat_id=excluded.user_chat_id, can_reply=excluded.can_reply,
                 can_read=excluded.can_read, can_delete=excluded.can_delete,
                 is_enabled=excluded.is_enabled""",
        (conn.id, conn.user.id, conn.user_chat_id,
         int(bool(conn.rights and conn.rights.can_reply)),
         int(bool(conn.rights and conn.rights.can_read_messages)),
         int(bool(conn.rights and conn.rights.can_delete_all_messages)),
         enabled, now_utc().isoformat()))
    c.commit(); c.close()

def get_connection(conn_id):
    c = db(); r = c.execute("SELECT * FROM connections WHERE id=?", (conn_id,)).fetchone(); c.close()
    return r

def ensure_web_token(conn_id):
    c = db()
    r = c.execute("SELECT web_token FROM connections WHERE id=?", (conn_id,)).fetchone()
    if r and r["web_token"]:
        c.close(); return r["web_token"]
    import secrets
    tok = secrets.token_urlsafe(12)
    c.execute("UPDATE connections SET web_token=? WHERE id=?", (tok, conn_id))
    c.commit(); c.close()
    return tok

def archive_url(conn_id):
    c = db()
    r = c.execute("SELECT user_id, web_token FROM connections WHERE id=?", (conn_id,)).fetchone()
    tok = r["web_token"] if r else None
    uid = r["user_id"] if r else None
    if not tok and uid:
        r2 = c.execute("SELECT web_token FROM connections WHERE user_id=? AND web_token IS NOT NULL LIMIT 1", (uid,)).fetchone()
        if r2: tok = r2["web_token"]
    c.close()
    if tok: return f"https://{GITHUB_USER}.github.io/{REPO}/u/{tok}/"
    return f"https://github.com/{GITHUB_USER}/{REPO}"

def user_has_active_connection(uid):
    c = db()
    r = c.execute("SELECT COUNT(*) FROM connections WHERE user_id=? AND is_enabled=1 AND web_token IS NOT NULL", (uid,)).fetchone()
    c.close()
    return r[0] > 0

def user_notify_on(uid):
    c = db()
    r = c.execute("SELECT notify_on FROM connections WHERE user_id=? AND is_enabled=1 LIMIT 1", (uid,)).fetchone()
    c.close()
    if r is None: return True
    return bool(r["notify_on"])

def save_message(conn_id, chat_id, message, owner_id):
    text = message.text or message.caption or ""
    mt, fid = None, None
    if message.photo: mt, fid = "photo", message.photo[-1].file_id
    elif message.video: mt, fid = "video", message.video.file_id
    elif message.video_note: mt, fid = "video_note", message.video_note.file_id
    elif message.voice: mt, fid = "voice", message.voice.file_id
    elif message.audio: mt, fid = "audio", message.audio.file_id
    elif message.document: mt, fid = "document", message.document.file_id
    elif message.sticker: mt, fid = "sticker", message.sticker.file_id
    elif message.animation: mt, fid = "animation", message.animation.file_id
    if not text and not mt: return
    if not text:
        text = {"photo":"📷 [Фото]","video":"🎬 [Видео]","video_note":"⭕ [Кружок]",
                "voice":"🎙 [Голосовое]","audio":"🎵 [Аудио]","document":"📎 [Файл]",
                "sticker":"🌟 [Стикер]","animation":"🎞 [GIF]"}.get(mt, "❔ [Вложение]")
    c = db()
    c.execute("""INSERT INTO saved_messages(connection_id,chat_id,message_id,from_id,from_name,from_username,text,media_type,file_id,owner_id,created)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (conn_id, chat_id, message.message_id,
         message.from_user.id if message.from_user else 0,
         message.from_user.full_name if message.from_user else "?",
         message.from_user.username if message.from_user else None,
         text, mt, fid, owner_id, now_utc().isoformat()))
    c.commit(); c.close()

def get_message(conn_id, chat_id, mid):
    c = db()
    r = c.execute("SELECT * FROM saved_messages WHERE chat_id=? AND message_id=? ORDER BY id DESC LIMIT 1",
                  (chat_id, mid)).fetchone()
    c.close(); return r

async def ensure_connection(conn_id):
    conn = get_connection(conn_id)
    if conn:
        if not conn["web_token"]:
            ensure_web_token(conn_id)
            conn = get_connection(conn_id)
        return conn
    try:
        bc = await bot.get_business_connection(conn_id)
        save_connection(bc)
        ensure_web_token(conn_id)
        return get_connection(conn_id)
    except TelegramAPIError as e:
        log.warning(f"ensure_connection: {e}"); return None

PLANS = {
    "1m":  {"days": 30,  "stars": 50,  "ucoin": 250,  "label": "1 месяц"},
    "3m":  {"days": 90,  "stars": 125, "ucoin": 625,  "label": "3 месяца"},
    "12m": {"days": 365, "stars": 400, "ucoin": 2000, "label": "12 месяцев"},
}

def grant_sub(uid, days):
    """Продлевает UnitedLove на N дней."""
    c = db()
    r = c.execute("SELECT sub_until FROM users WHERE id=?", (uid,)).fetchone()
    base = now_utc()
    if r and r["sub_until"]:
        try:
            cur = datetime.fromisoformat(r["sub_until"])
            if cur.tzinfo is None: cur = cur.replace(tzinfo=timezone.utc)
            if cur > base: base = cur
        except: pass
    until = base + timedelta(days=days)
    c.execute("UPDATE users SET sub_until=? WHERE id=?", (until.isoformat(), uid))
    c.commit(); c.close()
    return until

def log_payment(uid, plan, method, amount):
    c = db()
    c.execute("INSERT INTO payments(user_id,plan,method,amount,created) VALUES(?,?,?,?,?)",
              (uid, plan, method, amount, now_utc().isoformat()))
    c.commit(); c.close()

def redeem_promo(uid, code):
    c = db()
    r = c.execute("SELECT * FROM promocodes WHERE code=?", (code.upper(),)).fetchone()
    if not r:
        c.close(); return None, "❌ Промокод не найден"
    if r["uses"] >= r["max_uses"]:
        c.close(); return None, "❌ Промокод уже использован"
    c.execute("UPDATE promocodes SET uses=uses+1 WHERE code=?", (code.upper(),))
    c.commit(); c.close()
    until = grant_sub(uid, r["days"])
    return until, None

GAME_COOLDOWN = 30       # секунд между наградами
GAME_DAILY_LIMIT = 50    # наград в сутки

def can_play_game(uid):
    """Возвращает (можно_ли, причина)."""
    c = db()
    r = c.execute("SELECT last_game_ts FROM users WHERE id=?", (uid,)).fetchone()
    if not r:
        c.close(); return False, "no_user"
    ts = r["last_game_ts"] if "last_game_ts" in r.keys() else None
    if ts:
        try:
            last = datetime.fromisoformat(ts)
            if last.tzinfo is None: last = last.replace(tzinfo=timezone.utc)
            diff = (now_utc() - last).total_seconds()
            if diff < GAME_COOLDOWN:
                c.close()
                return False, f"cooldown_{int(GAME_COOLDOWN - diff) + 1}"
        except: pass
    day_ago = (now_utc() - timedelta(hours=24)).isoformat()
    cnt = c.execute(
        "SELECT COUNT(*) FROM transactions WHERE user_id=? AND reason LIKE 'game_%' AND created >= ?",
        (uid, day_ago)).fetchone()[0]
    c.close()
    if cnt >= GAME_DAILY_LIMIT:
        return False, "limit"
    return True, None

def try_game_reward(uid, cmd, base_amount):
    """Возвращает (выдано, суффикс_текста)."""
    if base_amount <= 0:
        # Всё равно ставим cooldown, чтобы не спамили проигрышами
        c = db()
        c.execute("UPDATE users SET last_game_ts=? WHERE id=?", (now_utc().isoformat(), uid))
        c.commit(); c.close()
        return 0, ""
    ok, reason = can_play_game(uid)
    if not ok:
        if reason and reason.startswith("cooldown_"):
            secs = reason.replace("cooldown_", "")
            return 0, chr(10) + chr(10) + "<i>⏳ Следующая награда через " + secs + "с</i>"
        if reason == "limit":
            return 0, chr(10) + chr(10) + "<i>🛑 Дневной лимит наград (50). Приходи через сутки.</i>"
        return 0, ""
    c = db()
    c.execute("UPDATE users SET ucoin = MAX(0, ucoin + ?), last_game_ts=? WHERE id=?",
              (base_amount, now_utc().isoformat(), uid))
    c.execute("INSERT INTO transactions(user_id, amount, reason, created) VALUES(?,?,?,?)",
              (uid, base_amount, "game_" + cmd, now_utc().isoformat()))
    c.commit()
    new_bal = c.execute("SELECT ucoin FROM users WHERE id=?", (uid,)).fetchone()["ucoin"]
    c.close()
    return base_amount, chr(10) + chr(10) + "💰 <b>+" + str(base_amount) + "</b> U-Coin · баланс: <b>" + str(new_bal) + "</b>"

def game_status_text(uid):
    """Краткий статус игр (для .balance)."""
    c = db()
    day_ago = (now_utc() - timedelta(hours=24)).isoformat()
    cnt = c.execute(
        "SELECT COUNT(*) FROM transactions WHERE user_id=? AND reason LIKE 'game_%' AND created >= ?",
        (uid, day_ago)).fetchone()[0]
    c.close()
    return cnt

def has_united_love(uid):
    """Проверяет, активна ли подписка UnitedLove у юзера."""
    c = db()
    r = c.execute("SELECT sub_until FROM users WHERE id=?", (uid,)).fetchone()
    c.close()
    if not r or not r["sub_until"]: return False
    try:
        until = datetime.fromisoformat(r["sub_until"])
        if until.tzinfo is None: until = until.replace(tzinfo=timezone.utc)
        return until > now_utc()
    except: return False

def afk_set(uid, chat_id, text):
    c = db()
    c.execute("""INSERT INTO afk(owner_id,chat_id,message,enabled,created)
                 VALUES(?,?,?,1,?)
                 ON CONFLICT(owner_id,chat_id) DO UPDATE SET message=excluded.message, enabled=1, created=excluded.created""",
        (uid, chat_id, text, now_utc().isoformat()))
    c.commit(); c.close()

def afk_off(uid, chat_id):
    c = db()
    c.execute("DELETE FROM afk WHERE owner_id=? AND chat_id=?", (uid, chat_id))
    c.commit(); c.close()

def afk_get(uid, chat_id):
    c = db()
    r = c.execute("SELECT * FROM afk WHERE owner_id=? AND chat_id=? AND enabled=1", (uid, chat_id)).fetchone()
    c.close(); return r

def B(text, cb=None, url=None, style=None):
    kw = {"text": text}
    if cb: kw["callback_data"] = cb
    if url: kw["url"] = url
    if style:
        try: return InlineKeyboardButton(**kw, style=style)
        except TypeError: pass
    return InlineKeyboardButton(**kw)

def q(t): return f"<blockquote>{t}</blockquote>"
def esc(s): return html.escape(s or "")

def kb_main():
    return InlineKeyboardMarkup(inline_keyboard=[
        [B("🌐 Веб-Архив", "m_archive", style="success")],
        [B("👤 Профиль", "m_profile", style="success")],
        [B("🔄 Функции", "m_features", style="primary"),
         B("📊 Статистика", "m_stats", style="primary")],
        [B("👥 Рефералы", "m_refs", style="primary"),
         B("🎮 Игры", "m_games", style="success")],
        [B("💎 Подписка", "m_sub", style="primary")],
        [B("🎧 Поддержка", "m_support", style="success")],
    ])

def kb_profile():
    return InlineKeyboardMarkup(inline_keyboard=[
        [B("🔔 Уведомления", "m_notify", style="primary")],
        [B("💰 U-Coin", "m_ncoin", style="success"),
         B("🌐 Зеркала", "m_mirrors", style="primary")],
        [B("🛡 United Safe", "m_unisafe", style="success")],
        [B("🗑 Удалить мои данные", "m_delete_data", style="danger")],
        [B("← Назад", "m_main", style="danger")],
    ])

def kb_games():
    return InlineKeyboardMarkup(inline_keyboard=[
        [B("🎲 КНБ с ботом", "game_knb_new", style="success")],
        [B("❌⭕ Крестики-нолики", "g_info_ttt", style="primary"),
         B("💣 Сапёр", "g_info_saper", style="primary")],
        [B("🎰 Слоты", "g_info_slot", style="primary"),
         B("👥 Приватная комната", "g_private", style="success")],
        [B("🏆 Топ игроков", "g_top", style="primary")],
        [B("← Назад", "m_main", style="danger")],
    ])

def kb_admin():
    return InlineKeyboardMarkup(inline_keyboard=[
        [B("📊 Статистика", "adm_stats", style="primary")],
        [B("👤 Найти юзера", "adm_find", style="primary"),
         B("💎 Выдать подписку", "adm_give", style="success")],
        [B("🎁 Промокоды", "adm_promo", style="primary"),
         B("💰 Платежи", "adm_pays", style="primary")],
        [B("📢 Рассылка", "adm_bc", style="success")],
        [B("⚙️ Настройки", "adm_settings", style="danger")],
        [B("← Закрыть", "adm_close", style="danger")],
    ])

def kb_admin_settings():
    m = "🟢 ВКЛ" if maintenance_on() else "🔴 ВЫКЛ"
    return InlineKeyboardMarkup(inline_keyboard=[
        [B("🔧 Тех. работы: " + m, "adm_t_maintenance", style="primary")],
        [B("🗑 Очистка старых", "adm_t_clean", style="danger"),
         B("📊 Экспорт БД", "adm_t_export", style="success")],
        [B("📋 Логи", "adm_t_logs", style="primary"),
         B("💰 Выдать U-Coin", "adm_t_ucoin", style="primary")],
        [B("📢 Юзерам от бота", "adm_t_bc_users", style="success")],
        [B("← Назад в панель", "adm_main", style="danger")],
    ])

def kb_admin_back():
    return InlineKeyboardMarkup(inline_keyboard=[
        [B("← Назад в панель", "adm_main", style="danger")]])

def kb_back(t="m_main"):
    return InlineKeyboardMarkup(inline_keyboard=[[B("← Назад", t, style="danger")]])

def kb_cmds():
    def btn(label, cmd, style="success"):
        return InlineKeyboardButton(text=label, callback_data="cmd_" + cmd, style=style)
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("🔒 .mute", "mute"), btn("🔓 .unmute", "unmute"), btn("⚠️ .warn", "warn")],
        [btn("❤️ .love", "love"), btn("📢 .spam", "spam"), btn("🔊 .spek", "spek")],
        [btn("🔇 .nomute", "nomute"), btn("🚫 .zero", "zero"), btn("⚡ .status", "status")],
        [btn("ℹ️ .info", "info"), btn("🆔 .id", "id"), btn("✨ .idstik", "idstik")],
        [btn("🎣 .troll", "troll"), btn("🎣 .troll send", "troll_send"), btn("⏳ .afk", "afk")],
        [btn("🌀 .бурмалда", "burm"), btn("💥 .crash", "crash"), btn("🪙 .flip", "flip")],
        [btn("✂️ .rps", "rps"), btn("🆘 .help", "help"), btn("✨ .anim", "anim")],
        [btn("📋 .toda", "toda"), btn("📋 .loma", "loma"), btn("🌪 .торнадо", "tornado")],
        [btn("💾 .save", "save"), btn("🎬 .krom", "krom"), btn("📢 .voicemod", "voicemod", "primary")],
        [btn("🎭 .niks", "niks", "primary"), btn("🖼 .stik", "stik"), btn("🌸 .kawai", "kawai", "primary")],
        [btn("🎬 .mon", "mon"), btn("🗑 .split", "split"), btn("⚫ .del", "del")],
        [btn("🆔 .ld", "ld"), btn("🎨 .title", "title", "primary"), btn("📱 .qr", "qr")],
        [btn("🌐 .перевод", "perevod", "primary"), btn("🎤 .голос", "golos", "primary"), btn("🍺 .sap", "sap")],
        [btn("🔔 .rec", "rec"), btn("🎮 .fup", "fup"), btn("🎲 .kub", "kub")],
        [btn("💥 .shot", "shot"), btn("🍀 .шанс", "chance"), btn("📤 .рассыл", "rassyl", "primary")],
        [btn("🎬 .clone", "clone"), btn("➕ .gif", "gif"), btn("📖 .story", "story", "primary")],
        [btn("⚪ .publik", "publik", "primary"), btn("✏️ .type", "type", "primary"), btn("🔤 .wordle", "wordle")],
        [btn("🎁 .gift", "gift", "primary"), btn("📦 .bay", "bay", "primary")],
        [btn("✉️ Одноразовые сообщения", "oneshot", "success")],
        [B("← Назад", "m_main", style="danger")],
    ])


def kb_peer(peer_id, username=None, archive=None):
    jump = f"https://t.me/{username}" if username else f"tg://user?id={peer_id}"
    arch = archive or f"https://github.com/{GITHUB_USER}/{REPO}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [B("↗ Перейти", url=jump, style="primary"),
         B("📁 Веб-Архив", url=arch, style="success")]])

def start_banner_url():
    return "https://cfmz.github.io/united-dialog-archive/img/photo_2026-09-24_15-16-00.jpg"

def archive_url_for_user(uid):
    c = db()
    r = c.execute("SELECT web_token FROM connections WHERE user_id=? AND web_token IS NOT NULL LIMIT 1", (uid,)).fetchone()
    c.close()
    if r: return f"https://{GITHUB_USER}.github.io/{REPO}/u/{r['web_token']}/"
    return f"https://github.com/{GITHUB_USER}/{REPO}"

def profile_banner_url():
    return "https://cfmz.github.io/united-dialog-archive/img/profile.jpg"

def txt_start_connected():
    return ("🚀 <b>Never Dialog подключён!</b>" + NL + NL
        + q("🔥 <b>Теперь вам доступны</b>" + NL
            + "🗑 просмотр удалённых и изменённых сообщений" + NL
            + "📸 сохранение одноразовых сообщений" + NL
            + "📷 отслеживание фото, видео и кружков" + NL
            + "🆕 все дополнительные функции и команды бота") + NL + NL
        + "😇 <i>Приятного использования!</i>")


def txt_start_new():
    return ("🚀 <b>Добро пожаловать!</b>" + NL + NL
        + q("🤖 Бот полностью бесплатный и готов к работе.") + NL + NL
        + "<b>🔥 Возможности бота</b>" + NL
        + q("🗑 Отслеживание удалённых сообщений" + NL
            + "✏️ Отслеживание изменённых сообщений" + NL
            + "🌙 Сохранение одноразовых сообщений" + NL
            + "🎥 Поддержка кружков, видео и фотографий" + NL
            + "🆕 Уникальные функции и команды") + NL + NL
        + "<b>❓ Как подключить</b>" + NL
        + q("1. Скопируй <code>@" + BOT_USERNAME + "</code>" + NL
            + "2. Открой Настройки → Telegram Business → Чат-боты" + NL
            + "3. Добавь <code>@" + BOT_USERNAME + "</code>" + NL
            + "4. Дай права: Отвечать + Читать сообщения"))

def txt_profile(u, conn, total):
    j = datetime.fromisoformat(u["joined"])
    if j.tzinfo is None: j = j.replace(tzinfo=timezone.utc)
    d = (now_utc() - j).days
    day_word = "дн."
    if d % 10 == 1 and d % 100 != 11: day_word = "день"
    elif d % 10 in (2,3,4) and d % 100 not in (12,13,14): day_word = "дня"
    full_name = u["full_name"] or u["username"] or "Гость"
    uname = u["username"] or "—"
    prem = "✅ <b>Активен</b>" if u["is_premium"] else "❌ <i>Отсутствует</i>"
    con = "🟢 <b>Подключён</b>" if user_has_active_connection(u["id"]) else "🔴 <i>Не подключён</i>"
    return (
        "╭───────────────────╮" + NL
        + "     👤 <b>П Р О Ф И Л Ь</b>" + NL
        + "╰───────────────────╯" + NL + NL
        + q("🆔 <b>ID:</b> <code>" + str(u["id"]) + "</code>" + NL
            + "📛 <b>Имя:</b> <b>" + esc(full_name) + "</b>" + NL
            + "🔗 <b>Username:</b> @" + esc(uname) + NL
            + "⚡ <b>Premium:</b> " + prem) + NL
        + q("🔔 <b>United Dialog:</b> " + con + NL
            + "📅 <b>С нами:</b> <b>" + str(d) + "</b> <i>" + day_word + "</i>" + NL
            + "✉️ <b>Сообщений:</b> <b>" + str(total) + "</b>") + NL
        + q("💰 <b>U-Coin:</b> <b>" + str(u["ucoin"]) + "</b> ⭐") + NL + NL
        + "<i>💝 Спасибо, что пользуетесь </i><b>United Dialog</b>")

def txt_features():
    return ("🔄 <b>Функции</b>" + NL + NL
        + q("🤖 <b>AFK-режим</b>" + NL + "Установи автоответ: <code>.afk текст</code>" + NL + NL
            + "✨ <b>Ник-анимация</b>" + NL + "<code>.anim текст</code>" + NL + NL
            + "🎭 <b>Развлечения</b>" + NL + "<code>.love .roast .kawai</code>" + NL + NL
            + "🛡 <b>United Safe</b>" + NL + "Защита профиля (скоро)"))

def txt_stats(u, conn, total):
    return ("📊 <b>Статистика</b>" + NL + NL
        + q("✉️ <b>Сообщений:</b> " + str(total) + NL
            + "⭐ <b>U-Coin:</b> " + str(u["ucoin"]) + NL
            + "🔔 <b>Активен:</b> " + ("Да" if user_has_active_connection(u["id"]) else "Нет")))

def txt_refs(link, u):
    return ("💼 <b>Реферальная программа</b>" + NL + NL
        + q("🔔 <b>Твоя ссылка</b>" + NL + link) + NL
        + q("🏆 <b>Вознаграждение</b>" + NL
            + "• 1 уровень — 20%" + NL
            + "• 2 уровень — 10%" + NL
            + "• 3 уровень — 5%") + NL
        + q("💰 <b>Баланс:</b> " + str(u["ucoin"]) + " U-Coin" + NL
            + "Минимум для вывода — 100 ⭐"))

def txt_sub(u):
    if has_united_love(u["id"]):
        status = "✅ <b>Активна</b>"
        until = u["sub_until"][:10] if u["sub_until"] else "—"
    else:
        status = "❌ <b>Не активна</b>"
        until = "—"
    return (
        "💎 <b>United Love</b>" + NL + NL
        + q("📊 <b>Статус:</b> " + status + NL
            + "📅 <b>До:</b> " + until) + NL
        + q("🎁 <b>Что даёт подписка:</b>" + NL
            + "• 🚫 Убирает водяные знаки" + NL
            + "• 🎥 Лимит медиа: 100 → 400 МБ" + NL
            + "• 💬 Одноразовые: 10 → 100 в день" + NL
            + "• 🤖 AFK без пометки «автоответ»" + NL
            + "• ⚡ Уникальные команды" + NL
            + "• 🛡 Иммунитет к троллингу") + NL
        + q("💳 <b>Способы оплаты:</b>" + NL
            + "⭐ Telegram Stars" + NL
            + "💰 U-Coin (баланс: " + str(u["ucoin"]) + ")") + NL
        + q("Выбери тариф 👇")
    )

def kb_sub():
    return InlineKeyboardMarkup(inline_keyboard=[
        [B("📅 1 месяц — 50 ⭐ / 250 💰", "sub_plan_1m", style="success")],
        [B("📅 3 месяца — 125 ⭐ / 625 💰", "sub_plan_3m", style="success")],
        [B("👑 12 месяцев — 400 ⭐ / 2000 💰", "sub_plan_12m", style="success")],
        [B("🎁 Ввести промокод", "sub_promo", style="primary")],
        [B("← Назад", "m_main", style="danger")],
    ])

def kb_plan(plan_key):
    p = PLANS[plan_key]
    return InlineKeyboardMarkup(inline_keyboard=[
        [B("⭐ Оплатить " + str(p["stars"]) + " Stars", "sub_pay_stars_" + plan_key, style="success")],
        [B("💰 Оплатить " + str(p["ucoin"]) + " U-Coin", "sub_pay_ucoin_" + plan_key, style="primary")],
        [B("← Назад", "m_sub", style="danger")],
    ])


def txt_support():
    return ("🎧 <b>Поддержка</b>" + NL + NL
        + q("Нажми <b>Связаться</b> — опиши проблему." + NL
            + "Отвечаем в течение 24 часов."))

def txt_cmds_info():
    return ("📌 <b>Информация</b>" + NL + NL
        + q("<code>.ping</code> — проверка связи" + NL
            + "<code>.id</code> — твой ID" + NL
            + "<code>.info</code> — о собеседнике" + NL
            + "<code>.help</code> — список команд"))

def txt_cmds_fun():
    return ("🎭 <b>Развлечения</b>" + NL + NL
        + q("<code>.love</code> — совместимость" + NL
            + "<code>.flip</code> — монетка" + NL
            + "<code>.rps камень|ножницы|бумага</code>" + NL
            + "<code>.kawai</code> — кавай" + NL
            + "<code>.kub</code> — кубик" + NL
            + "<code>.roast</code> — подкол"))

def txt_cmds_style():
    return ("📝 <b>Стиль</b>" + NL + NL
        + q("<code>.anim текст</code>" + NL
            + "<code>.type текст</code>" + NL
            + "<code>.title текст</code>" + NL
            + "<code>.split сл1 сл2</code>" + NL
            + "<code>.перевод текст</code>"))

def txt_cmds_afk():
    return ("⏳ <b>AFK-режим</b>" + NL + NL
        + q("<code>.afk текст</code> — включить" + NL
            + "<code>.unafk</code> — выключить") + NL
        + q("⚡ <i>Автоответ сработает только когда ты офлайн</i> (5+ мин без активности)."))

class PM(BaseFilter):
    def __init__(self, *n): self.n = {x.lower() for x in n}
    async def __call__(self, m):
        if m.business_connection_id: return False
        if m.chat.type != "private": return False
        t = m.text or ""
        if len(t) < 2 or t[0] not in (".", "/"): return False
        parts = t[1:].split(maxsplit=1)
        if not parts: return False
        return parts[0].split("@", 1)[0].lower() in self.n

@dp.message(CommandStart())
@dp.message(PM("start", "menu", "старт", "меню"))
async def pm_start(m):
    touch_seen(m.from_user.id)
    if user_has_active_connection(m.from_user.id):
        text = txt_start_connected()
        kb = kb_main()
    else:
        text = txt_start_new()
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Скопировать",
                copy_text=CopyTextButton(text=f"@{BOT_USERNAME}"), style="success"),
             InlineKeyboardButton(text="🔘 Подключить",
                url="tg://settings/edit", style="primary")]])
    try:
        await m.answer_photo(photo=start_banner_url(), caption=text, reply_markup=kb)
    except Exception as e:
        log.warning(f"start photo: {e}")
        await m.answer(text, reply_markup=kb)

async def _edit(c, text, kb=None):
    try: await c.message.edit_text(text, reply_markup=kb)
    except TelegramAPIError: await c.message.answer(text, reply_markup=kb)

@dp.callback_query(F.data.startswith("m_"))
async def on_menu_cb(c):
    if c.message.business_connection_id:
        await c.answer(); return
    touch_seen(c.from_user.id)
    d = c.data
    u = get_user(c.from_user.id, c.from_user.username, c.from_user.full_name, int(bool(c.from_user.is_premium)))
    _c = db()
    conn = _c.execute("SELECT * FROM connections WHERE user_id=? AND is_enabled=1", (c.from_user.id,)).fetchone()
    if not conn:
        conn = _c.execute("SELECT * FROM connections WHERE user_id=?", (c.from_user.id,)).fetchone()
    total = _c.execute("SELECT COUNT(*) FROM saved_messages WHERE owner_id=?", (c.from_user.id,)).fetchone()[0]
    _c.close()

    if d == "m_main":
        await _edit(c, txt_start_connected() if user_has_active_connection(u["id"]) else txt_start_new(), kb_main())
    elif d == "m_profile":
        prof_text = txt_profile(u, conn, total)
        kb = kb_profile()
        try: await c.message.delete()
        except Exception: pass
        try:
            await bot.send_photo(chat_id=c.from_user.id, photo=profile_banner_url(),
                                 caption=prof_text, reply_markup=kb)
        except Exception as e:
            log.warning(f"profile photo: {e}")
            await bot.send_message(c.from_user.id, prof_text, reply_markup=kb)
    elif d == "m_features":
        await _edit(c, txt_features(), kb_back("m_main"))
    elif d == "m_stats":
        await _edit(c, txt_stats(u, conn, total), kb_back("m_main"))
    elif d == "m_refs":
        me = await bot.get_me()
        await _edit(c, txt_refs(f"https://t.me/{me.username}?start=ref_{u['id']}", u), kb_back("m_main"))
    elif d == "m_cmds":
        await _edit(c, "✏️ <b>Команды</b>" + NL + NL + q("Отправьте команду в нужный диалог, и она будет выполнена."), kb_cmds())
    elif d == "m_c_info": await _edit(c, txt_cmds_info(), kb_back("m_cmds"))
    elif d == "m_c_fun": await _edit(c, txt_cmds_fun(), kb_back("m_cmds"))
    elif d == "m_c_style": await _edit(c, txt_cmds_style(), kb_back("m_cmds"))
    elif d == "m_c_afk": await _edit(c, txt_cmds_afk(), kb_back("m_cmds"))
    elif d == "m_sub":
        await _edit(c, txt_sub(u), kb_sub())
    elif d.startswith("sub_plan_"):
        plan = d[len("sub_plan_"):]
        if plan in PLANS:
            p = PLANS[plan]
            await _edit(c,
                "💎 <b>" + p["label"] + "</b>" + NL + NL
                + q("📅 Длительность: <b>" + str(p["days"]) + " дней</b>" + NL
                    + "⭐ Stars: <b>" + str(p["stars"]) + "</b>" + NL
                    + "💰 U-Coin: <b>" + str(p["ucoin"]) + "</b>") + NL
                + q("Выбери способ оплаты 👇"), kb_plan(plan))
    elif d.startswith("sub_pay_ucoin_"):
        plan = d[len("sub_pay_ucoin_"):]
        if plan in PLANS:
            p = PLANS[plan]
            if u["ucoin"] < p["ucoin"]:
                await c.answer("❌ Недостаточно U-Coin", show_alert=True)
            else:
                _c = db()
                _c.execute("UPDATE users SET ucoin=ucoin-? WHERE id=?", (p["ucoin"], u["id"]))
                _c.commit(); _c.close()
                until = grant_sub(u["id"], p["days"])
                log_payment(u["id"], plan, "ucoin", p["ucoin"])
                await c.answer("✅ UnitedLove на " + str(p["days"]) + " дней активирована!", show_alert=True)
    elif d.startswith("sub_pay_stars_"):
        plan = d[len("sub_pay_stars_"):]
        if plan in PLANS:
            p = PLANS[plan]
            try:
                await bot.send_invoice(
                    chat_id=c.from_user.id,
                    title="United Love — " + p["label"],
                    description="Подписка на " + str(p["days"]) + " дней",
                    payload="sub_" + plan,
                    provider_token="",
                    currency="XTR",
                    prices=[LabeledPrice(label="United Love", amount=p["stars"])])
                await c.answer()
            except Exception as e:
                log.warning(f"invoice fail: {e}")
                await c.answer("❌ Ошибка оплаты", show_alert=True)
    elif d == "sub_promo":
        await _edit(c,
            "🎁 <b>Промокод</b>" + NL + NL
            + q("Отправь в чат команду <code>.redeem КОД</code> — активирую подписку."),
            kb_back("m_sub"))
    elif d == "m_support":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [B("✈️ Связаться", url=SUPPORT, style="success")],
            [B("← Назад", "m_main", style="danger")]])
        await _edit(c, txt_support(), kb)
    elif d == "m_notify":
        notify = user_notify_on(u["id"])
        status = "✅ <b>включены</b>" if notify else "🔕 <b>выключены</b>"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [B("🔔 Включить", "m_notify_on", style="success"),
             B("🔕 Отключить", "m_notify_off", style="danger")],
            [B("← Назад", "m_profile", style="danger")]])
        await _edit(c, "🔔 <b>Уведомления</b>" + NL + NL
            + q("Статус: " + status + NL + NL
                + "Уведомления приходят тебе в личку, когда:" + NL
                + "🗑 удаляют сообщение" + NL
                + "✏️ изменяют сообщение"), kb)
    elif d == "m_notify_on":
        _c = db(); _c.execute("UPDATE connections SET notify_on=1 WHERE user_id=?", (u["id"],)); _c.commit(); _c.close()
        await c.answer("🔔 Уведомления включены", show_alert=True)
    elif d == "m_notify_off":
        _c = db(); _c.execute("UPDATE connections SET notify_on=0 WHERE user_id=?", (u["id"],)); _c.commit(); _c.close()
        await c.answer("🔕 Уведомления выключены", show_alert=True)
    elif d == "m_ncoin":
        await _edit(c, "💰 <b>U-Coin</b>" + NL + NL
            + q("Баланс: <b>" + str(u["ucoin"]) + "</b> ⭐" + NL + NL
                + "Зарабатывай за:" + NL
                + "👥 Приглашённых друзей" + NL
                + "🎁 Ежедневные бонусы" + NL
                + "🎮 Игры"), kb_back("m_profile"))
    elif d == "m_mirrors":
        await _edit(c, "🌐 <b>Зеркала</b>" + NL + NL
            + q("Основной бот всегда доступен:" + NL
                + "<code>@" + BOT_USERNAME + "</code>"), kb_back("m_profile"))
    elif d == "m_unisafe":
        await _edit(c, "🛡 <b>United Safe</b>" + NL + NL
            + q("Защита твоего аккаунта." + NL + NL
                + "🔒 Блокировка по PIN" + NL
                + "🔑 Резервный ключ" + NL
                + "🚫 Защита от чужих входов") + NL
            + q("⚙️ В разработке"), kb_back("m_profile"))
    elif d == "m_delete_data":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [B("🗑 Да, удалить", "m_delete_confirm", style="danger")],
            [B("← Отмена", "m_profile", style="success")]])
        await _edit(c, "🗑 <b>Удалить мои данные</b>" + NL + NL
            + q("⚠️ Действие необратимо." + NL + NL
                + "Будут удалены:" + NL
                + "• все сохранённые сообщения" + NL
                + "• история уведомлений"), kb)
    elif d == "m_delete_confirm":
        _c = db(); _c.execute("DELETE FROM saved_messages WHERE owner_id=?", (u["id"],)); _c.commit(); _c.close()
        await c.answer("✅ Данные удалены", show_alert=True)
    await c.answer()

async def send_rich(chat_id, html_text, kb=None):
    try:
        await bot(SendRichMessage(
            chat_id=chat_id,
            rich_message=InputRichMessage(html=html_text),
            reply_markup=kb))
    except Exception as e:
        log.warning(f"send_rich failed: {e}")
        plain = (html_text
            .replace("<details open>", "").replace("<details>", "").replace("</details>", "")
            .replace("<summary>", "<b>").replace("</summary>", "</b>")
            .replace("<h2>", "<b>").replace("</h2>", "</b>")
            .replace("<p>", "").replace("</p>", ""))
        await bot.send_message(chat_id, plain, reply_markup=kb)


@dp.callback_query(F.data.startswith("cmd_"))
async def on_cmd_cb(c):
    cmd = c.data[4:]
    texts = {
        "help": ("🆘 <b>.help</b>", "Показывает список всех команд."),
        "afk": ("⏳ <b>.afk</b>", "Автоответ: <code>.afk текст</code> · выключить: <code>.unafk</code>."),
        "love": ("❤️ <b>.love</b>", "Совместимость с собеседником (ответом на сообщение)."),
        "flip": ("🪙 <b>.flip</b>", "Орёл или решка."),
        "rps": ("✂️ <b>.rps</b>", "Камень-ножницы-бумага: <code>.rps камень</code>."),
        "kawai": ("🌸 <b>.kawai</b>", "Случайный кавайный смайл."),
        "kub": ("🎲 <b>.kub</b>", "Бросает кубик 1-6."),
        "anim": ("✨ <b>.anim</b>", "Анимированный ник: <code>.anim текст</code>."),
        "type": ("✏️ <b>.type</b>", "L33t-текст: <code>.type текст</code>."),
        "title": ("🎨 <b>.title</b>", "Красивый заголовок: <code>.title текст</code>."),
        "split": ("🗑 <b>.split</b>", "Разбить текст: <code>.split слово1 слово2</code>."),
        "perevod": ("🌐 <b>.перевод</b>", "Транслит RU→EN: <code>.перевод текст</code>."),
        "id": ("🆔 <b>.id</b>", "Показывает твой ID и ID чата."),
        "info": ("ℹ️ <b>.info</b>", "Информация о собеседнике."),
        "status": ("⚡ <b>.status</b>", "Показывает статус бота."),
        "oneshot": ("✉️ <b>Одноразовые сообщения</b>", "Скоро — автоудаление через N секунд."),
    }
    if cmd in texts:
        title, desc = texts[cmd]
        await _edit(c, title + NL + NL + q(desc), kb_back("m_cmds"))
    else:
        await c.answer("⚙️ В разработке", show_alert=True)
    await c.answer()

@dp.pre_checkout_query()
async def on_pre_checkout(q: PreCheckoutQuery):
    await q.answer(ok=True)

@dp.message(F.successful_payment)
async def on_success_pay(m: Message):
    payload = m.successful_payment.invoice_payload or ""
    if payload.startswith("sub_"):
        plan = payload[4:]
        if plan in PLANS:
            p = PLANS[plan]
            until = grant_sub(m.from_user.id, p["days"])
            log_payment(m.from_user.id, plan, "stars", p["stars"])
            await m.answer("✅ <b>UnitedLove активирована!</b>" + NL + NL
                + q("Подписка на " + str(p["days"]) + " дней до " + until.strftime("%d.%m.%Y")))



@dp.callback_query(F.data.startswith("adm_"))
async def adm_cb(c):
    if c.from_user.id != ADMIN_ID:
        await c.answer("❌ Нет доступа", show_alert=True); return
    d = c.data
    uid = c.from_user.id

    try:
        if d == "adm_close":
            ADMIN_WAIT.pop(uid, None)
            try: await c.message.delete()
            except Exception: pass
            await c.answer(); return

        if d == "adm_main":
            ADMIN_WAIT.pop(uid, None)
            await c.message.edit_text(
                "🛡 <b>Админ-панель</b>" + NL + NL + q("Выбери действие 👇"),
                reply_markup=kb_admin())
            await c.answer(); return

        if d == "adm_stats":
            _c = db()
            users_total = _c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            msgs_total = _c.execute("SELECT COUNT(*) FROM saved_messages").fetchone()[0]
            subs_active = _c.execute("SELECT COUNT(*) FROM users WHERE sub_until IS NOT NULL").fetchone()[0]
            pays_total = _c.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
            today = now_utc().date().isoformat()
            new_today = _c.execute("SELECT COUNT(*) FROM users WHERE joined LIKE ?", (today+"%",)).fetchone()[0]
            _c.close()
            await c.message.edit_text(
                "📊 <b>Статистика</b>" + NL + NL
                + q("👥 Юзеров: <b>" + str(users_total) + "</b>" + NL
                    + "🆕 Сегодня: <b>" + str(new_today) + "</b>" + NL
                    + "💬 Сообщений: <b>" + str(msgs_total) + "</b>" + NL
                    + "💎 Подписок: <b>" + str(subs_active) + "</b>" + NL
                    + "💰 Платежей: <b>" + str(pays_total) + "</b>"),
                reply_markup=kb_admin_back())
            await c.answer(); return

        if d == "adm_find":
            ADMIN_WAIT[uid] = "adm_find"
            await c.message.edit_text(
                "👤 <b>Поиск юзера</b>" + NL + NL + q("Отправь ID юзера:"),
                reply_markup=kb_admin_back())
            await c.answer(); return

        if d == "adm_give":
            ADMIN_WAIT[uid] = "adm_give"
            await c.message.edit_text(
                "💎 <b>Выдать подписку</b>" + NL + NL + q("Формат: <code>ID ДНЕЙ</code>"),
                reply_markup=kb_admin_back())
            await c.answer(); return

        if d == "adm_promo":
            await c.message.edit_text(
                "🎁 <b>Промокоды</b>" + NL + NL + q("Управление кодами 👇"),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [B("🎁 Создать", "adm_promo_create", style="success"),
                     B("📋 Список", "adm_promo_list", style="primary")],
                    [B("← Назад", "adm_main", style="danger")]]))
            await c.answer(); return

        if d == "adm_promo_create":
            ADMIN_WAIT[uid] = "adm_promo_create"
            await c.message.edit_text(
                "🎁 <b>Создать промокод</b>" + NL + NL
                + q("Формат: <code>КОД ДНЕЙ ЛИМИТ</code>"),
                reply_markup=kb_admin_back())
            await c.answer(); return

        if d == "adm_promo_list":
            _c = db()
            rows = _c.execute("SELECT code, days, uses, max_uses FROM promocodes ORDER BY created DESC LIMIT 30").fetchall()
            _c.close()
            if not rows:
                txt = "📋 <b>Промокоды</b>" + NL + NL + q("Список пуст")
            else:
                lines = [f"<code>{r['code']}</code> — {r['days']}д · {r['uses']}/{r['max_uses']}" for r in rows]
                txt = "📋 <b>Промокоды</b>" + NL + NL + q(NL.join(lines))
            await c.message.edit_text(txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [B("🎁 Создать", "adm_promo_create", style="success")],
                [B("← Назад", "adm_main", style="danger")]]))
            await c.answer(); return

        if d == "adm_pays":
            _c = db()
            rows = _c.execute("SELECT user_id, plan, method, amount, created FROM payments ORDER BY id DESC LIMIT 15").fetchall()
            _c.close()
            if not rows:
                txt = "💰 <b>Платежи</b>" + NL + NL + q("Пока нет оплат")
            else:
                lines = [f"<code>{r['user_id']}</code> · {r['plan']} · {r['method']} · {r['amount']}" for r in rows]
                txt = "💰 <b>Платежи</b>" + NL + NL + q(NL.join(lines))
            await c.message.edit_text(txt, reply_markup=kb_admin_back())
            await c.answer(); return

        if d == "adm_bc":
            ADMIN_WAIT[uid] = "adm_bc"
            await c.message.edit_text(
                "📢 <b>Рассылка</b>" + NL + NL + q("Отправь текст."),
                reply_markup=kb_admin_back())
            await c.answer(); return

        if d == "adm_settings":
            ADMIN_WAIT.pop(uid, None)
            await c.message.edit_text(
                "⚙️ <b>Настройки</b>" + NL + NL + q("Управление ботом 👇"),
                reply_markup=kb_admin_settings())
            await c.answer(); return

        if d == "adm_t_maintenance":
            new_val = "0" if maintenance_on() else "1"
            set_setting("maintenance", new_val)
            state = "включены 🟢" if new_val == "1" else "выключены 🔴"
            await c.message.edit_text(
                "⚙️ <b>Настройки</b>" + NL + NL + q("Тех. работы: " + state),
                reply_markup=kb_admin_settings())
            await c.answer(); return

        if d == "adm_t_clean":
            await c.message.edit_text(
                "🗑 <b>Очистка БД</b>" + NL + NL + q("Удалить сообщения:"),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [B("🗑 Старше 30 дней", "adm_clean_30", style="danger")],
                    [B("🗑 Старше 60 дней", "adm_clean_60", style="danger")],
                    [B("🗑 Старше 90 дней", "adm_clean_90", style="danger")],
                    [B("🔥 УДАЛИТЬ ВСЁ", "adm_clean_all", style="danger")],
                    [B("← Назад", "adm_settings", style="primary")]]))
            await c.answer(); return

        if d.startswith("adm_clean_"):
            mode = d[len("adm_clean_"):]
            _c = db()
            if mode == "all":
                _c.execute("DELETE FROM saved_messages")
            else:
                days = int(mode)
                cutoff = (now_utc() - timedelta(days=days)).isoformat()
                _c.execute("DELETE FROM saved_messages WHERE created < ?", (cutoff,))
            deleted = _c.total_changes
            _c.commit(); _c.close()
            await c.answer(f"✅ Удалено: {deleted}", show_alert=True)
            await c.message.edit_text(
                "⚙️ <b>Настройки</b>" + NL + NL + q(f"🗑 Удалено: <b>{deleted}</b>"),
                reply_markup=kb_admin_settings())
            return

        if d == "adm_t_export":
            try:
                from aiogram.types import FSInputFile
                await bot.send_document(chat_id=uid, document=FSInputFile(DB),
                    caption="💾 <b>Экспорт БД</b>")
                await c.answer("📤 Отправил")
            except Exception as e:
                await c.answer(f"❌ {e}", show_alert=True)
            return

        if d == "adm_t_logs":
            try:
                with open("/home/maya/united/bot.log", "r", encoding="utf-8", errors="ignore") as f:
                    lines = f.readlines()[-50:]
                text = "".join(lines)
                if len(text) > 3900: text = text[-3900:]
                await c.message.edit_text(
                    "📋 <b>Логи (50 строк)</b>" + NL + NL + q(f"<code>{esc(text)}</code>"),
                    reply_markup=kb_admin_back())
            except Exception as e:
                await c.message.edit_text(f"❌ {e}", reply_markup=kb_admin_back())
            await c.answer(); return

        if d == "adm_t_ucoin":
            ADMIN_WAIT[uid] = "adm_ucoin"
            await c.message.edit_text(
                "💰 <b>Выдать U-Coin</b>" + NL + NL + q("Формат: <code>ID СУММА</code>"),
                reply_markup=kb_admin_back())
            await c.answer(); return

        if d == "adm_t_bc_users":
            ADMIN_WAIT[uid] = "adm_bc_users"
            await c.message.edit_text(
                "📢 <b>Рассылка юзерам</b>" + NL + NL + q("Отправь текст."),
                reply_markup=kb_admin_back())
            await c.answer(); return

        if d.startswith("adm_sub_"):
            parts = d.split("_")
            target_id = int(parts[2]); days = int(parts[3])
            until = grant_sub(target_id, days)
            await c.answer(f"✅ +{days} дн.", show_alert=True); return

        if d.startswith("adm_unsub_"):
            target_id = int(d.split("_")[2])
            _c = db(); _c.execute("UPDATE users SET sub_until=NULL WHERE id=?", (target_id,)); _c.commit(); _c.close()
            await c.answer("🚫 Обнулено", show_alert=True); return

        await c.answer()
    except Exception as e:
        log.exception(f"adm_cb error: {e}")
        await c.answer(f"❌ {e}", show_alert=True)


@dp.message(F.text & ~F.text.startswith((".", "/")))
async def adm_input(m):
    if m.from_user.id != ADMIN_ID: return
    if m.chat.type != "private": return
    state = ADMIN_WAIT.get(m.from_user.id)
    if not state: return

    text = (m.text or "").strip()

    if state == "adm_find":
        if not text.isdigit():
            await m.answer("❌ Нужен ID числом"); return
        _c = db()
        r = _c.execute("SELECT * FROM users WHERE id=?", (int(text),)).fetchone()
        msgs = _c.execute("SELECT COUNT(*) FROM saved_messages WHERE owner_id=?", (int(text),)).fetchone()[0]
        _c.close()
        if not r:
            await m.answer("❌ Не найден"); ADMIN_WAIT.pop(m.from_user.id, None); return
        sub = r["sub_until"][:10] if r["sub_until"] else "—"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [B("💎 +30 дней", f"adm_sub_{r['id']}_30", style="success"),
             B("💎 +7 дней", f"adm_sub_{r['id']}_7", style="success")],
            [B("🎁 +1 день", f"adm_sub_{r['id']}_1", style="primary"),
             B("🚫 Обнулить", f"adm_unsub_{r['id']}", style="danger")],
            [B("← Назад", "adm_main", style="danger")]])
        await m.answer(
            "👤 <b>Юзер</b>" + NL + NL
            + q(f"🆔 <code>{r['id']}</code>" + NL
                + f"📛 {esc(r['full_name'] or '—')}" + NL
                + f"🔗 @{esc(r['username'] or '—')}" + NL
                + f"💰 U-Coin: {r['ucoin']}" + NL
                + f"💎 До: {sub}" + NL
                + f"✉️ Сообщений: {msgs}"),
            reply_markup=kb)
        ADMIN_WAIT.pop(m.from_user.id, None); return

    if state == "adm_give":
        parts = text.split()
        if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
            await m.answer("❌ Формат: ID ДНЕЙ"); return
        until = grant_sub(int(parts[0]), int(parts[1]))
        await m.answer("✅ До " + until.strftime("%d.%m.%Y"))
        ADMIN_WAIT.pop(m.from_user.id, None); return

    if state == "adm_promo_create":
        parts = text.split()
        if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
            await m.answer("❌ Формат: КОД ДНЕЙ ЛИМИТ"); return
        _c = db()
        try:
            _c.execute("INSERT INTO promocodes(code,days,max_uses,created) VALUES(?,?,?,?)",
                       (parts[0].upper(), int(parts[1]), int(parts[2]), now_utc().isoformat()))
            _c.commit()
            await m.answer(f"✅ <code>{parts[0].upper()}</code> создан")
        except Exception as e:
            await m.answer(f"❌ {e}")
        _c.close()
        ADMIN_WAIT.pop(m.from_user.id, None); return

    if state == "adm_ucoin":
        parts = text.split()
        if len(parts) != 2 or not parts[0].isdigit() or not parts[1].lstrip("-").isdigit():
            await m.answer("❌ Формат: ID СУММА"); return
        tid = int(parts[0]); amt = int(parts[1])
        _c = db()
        _c.execute("UPDATE users SET ucoin = MAX(0, ucoin + ?) WHERE id=?", (amt, tid))
        _c.commit()
        nb = _c.execute("SELECT ucoin FROM users WHERE id=?", (tid,)).fetchone()
        _c.close()
        await m.answer(f"✅ Выдано {amt}. Баланс: {nb[0] if nb else '?'}")
        ADMIN_WAIT.pop(m.from_user.id, None); return

    if state in ("adm_bc", "adm_bc_users"):
        _c = db()
        users = _c.execute("SELECT id FROM users").fetchall()
        _c.close()
        sent, failed = 0, 0
        await m.answer(f"📢 Рассылка на {len(users)}...")
        for row in users:
            try:
                await bot.send_message(row["id"], text)
                sent += 1
                await asyncio.sleep(0.05)
            except Exception:
                failed += 1
        await m.answer(f"✅ Готово. Отправлено: {sent}. Ошибок: {failed}")
        ADMIN_WAIT.pop(m.from_user.id, None); return

@dp.callback_query(F.data.startswith("play_"))
async def play_cb(c):
    cmd = c.data[len("play_"):]
    if cmd == "ttt":
        # Запускаем через cmd_tictactoe в личке
        try:
            await c.message.delete()
        except Exception: pass
        await c.message.answer("🎮 Отправь <code>.ttt</code> в личке бота — создашь дуэль.")
    elif cmd == "saper":
        try: await c.message.delete()
        except Exception: pass
        await c.message.answer("💣 Отправь <code>.saper</code> в личке бота.")
    elif cmd == "slot":
        try: await c.message.delete()
        except Exception: pass
        await c.message.answer("🎰 Отправь <code>.slot</code> в личке бота.")
    await c.answer()

@dp.callback_query(F.data.startswith("m_games"))
async def cb_games(c):
    touch_seen(c.from_user.id)
    await _edit(c,
        "🎮 <b>Игровой центр</b>" + NL + NL
        + q("Играй, соревнуйся и зарабатывай U-Coin." + NL
            + "Игры работают в личке с ботом." + NL + NL
            + "🎲 <b>КНБ с ботом</b> — 5 раундов" + NL
            + "❌⭕ <b>Крестики-нолики</b> — дуэль с другом" + NL
            + "💣 <b>Сапёр</b> — соло" + NL
            + "🎰 <b>Слоты</b> — крути барабаны" + NL
            + "👥 <b>Комната</b> — играй с друзьями"),
        kb_games())
    await c.answer()


@dp.callback_query(F.data.startswith("g_info_"))
async def game_info(c):
    game = c.data[len("g_info_"):]
    info = {
        "ttt": ("❌⭕ <b>Крестики-нолики</b>",
                "Дуэль двух игроков на поле 3×3." + NL + NL
                + "🎁 Победа: <b>+15</b> 💰" + NL
                + "🤝 Ничья: <b>+5</b> 💰" + NL
                + "😢 Проигрыш: <b>−5</b> 💰" + NL + NL
                + "Отправь команду <code>.ttt</code> в личке бота — бот создаст дуэль. Перешли сообщение другу или дай ему ссылку."),
        "saper": ("💣 <b>Сапёр</b>",
                  "Поле 5×5, спрятано 5 мин." + NL + NL
                  + "🎁 Победа: <b>+50</b> 💰" + NL
                  + "💥 Мина: <b>−20</b> 💰" + NL + NL
                  + "Отправь <code>.saper</code> боту в личке."),
        "slot": ("🎰 <b>Слоты</b>",
                 "Три барабана, 6 символов. Ставки от 20 до 200 💰." + NL + NL
                 + "🍒×3 → x3" + NL
                 + "🍋×3 → x4" + NL
                 + "🍇×3 → x6" + NL
                 + "💎×3 → x10" + NL
                 + "⭐×3 → x15" + NL
                 + "7️⃣×3 → x25" + NL
                 + "Пара → x2" + NL + NL
                 + "Отправь <code>.slot</code> боту в личке."),
    }
    if game in info:
        title, desc = info[game]
        await _edit(c, title + NL + NL + q(desc), kb_back("m_games"))
    await c.answer()


@dp.callback_query(F.data == "g_private")
async def g_private(c):
    await _edit(c,
        "👥 <b>Приватная комната</b>" + NL + NL
        + q("Создай комнату — получишь код." + NL
            + "Друг откроет бота и введёт <code>.join КОД</code>." + NL + NL
            + "Пока в разработке — используй крестики-нолики для дуэли:" + NL
            + "<code>.ttt</code> в личке бота"),
        kb_back("m_games"))
    await c.answer()


@dp.callback_query(F.data == "g_top")
async def g_top(c):
    _c = db()
    rows = _c.execute("SELECT full_name, username, ucoin FROM users ORDER BY ucoin DESC LIMIT 10").fetchall()
    _c.close()
    medals = ["🥇","🥈","🥉"] + ["▪️"]*7
    lines = []
    for i, r in enumerate(rows):
        name = r["full_name"] or r["username"] or "User"
        lines.append(f"{medals[i]} <b>{esc(name[:20])}</b> — {r['ucoin']} 💰")
    await _edit(c, "🏆 <b>Топ игроков</b>" + NL + NL + q(NL.join(lines) if lines else "Пока пусто"),
                kb_back("m_games"))
    await c.answer()

@dp.business_connection()
async def on_bc(conn):
    was = user_has_active_connection(conn.user.id)
    save_connection(conn)
    ensure_web_token(conn.id)
    c = db()
    if conn.is_enabled:
        c.execute("UPDATE connections SET is_enabled=0 WHERE user_id=? AND id!=?", (conn.user.id, conn.id))
    else:
        c.execute("UPDATE connections SET is_enabled=0 WHERE user_id=?", (conn.user.id,))
    c.commit(); c.close()
    now = conn.is_enabled
    log.info(f"{'OK' if now else 'OFF'} conn {conn.id} was={was} now={now}")
    if was == now: return
    key = (conn.user.id, now)
    if time.time() - _NOTIFY_TS.get(key, 0) < 15: return
    _NOTIFY_TS[key] = time.time()
    try:
        if now:
            text = ("✅ <b>Спасибо, что выбрали нас</b>" + NL + NL
                + q("Теперь вам доступны все возможности:" + NL + NL
                    + "🗑 Просматривать удалённые" + NL
                    + "✏️ Просматривать изменённые" + NL
                    + "🌙 Сохранять одноразовые" + NL
                    + "🔔 Уведомления в реальном времени" + NL
                    + "🚀 Уникальные команды") + NL + NL
                + q("Для перехода в меню нажми 🚀 Начать."))
            kb = InlineKeyboardMarkup(inline_keyboard=[[B("🚀 Начать", "m_main", style="success")]])
            await bot.send_message(conn.user_chat_id, text, reply_markup=kb)
        else:
            text = ("🚫 <b>Вы отключили United Dialog</b>" + NL + NL
                + q("🥺 Нам очень жаль." + NL + NL
                    + "Теперь недоступны:" + NL
                    + "🗑 Удалённые сообщения" + NL
                    + "✏️ Изменённые сообщения" + NL
                    + "🌙 Одноразовые сообщения" + NL
                    + "🎥 Отслеживание медиа") + NL + NL
                + q("Если передумаете — нажми «🔘 Подключить»."))
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [B("🔘 Подключить", url="tg://settings/edit", style="success")],
                [B("⭐ Оставить отзыв", url=SUPPORT, style="danger")]])
            await bot.send_message(conn.user_chat_id, text, reply_markup=kb)
    except Exception as e:
        log.warning(f"bc notify: {e}")

KNOWN = {"ping","id","info","help","love","flip","rps","kawai","kub",
         "roast","anim","type","title","split","перевод","translit",
         "afk","unafk", "ttt", "saper", "slot"}

@dp.business_message()
async def on_bm(message):
    conn_id = message.business_connection_id
    if not conn_id: return
    conn = await ensure_connection(conn_id)
    if not conn: return
    chat_id = message.chat.id
    from_id = message.from_user.id if message.from_user else 0
    owner_id = conn["user_id"]
    if from_id == owner_id:
        touch_seen(owner_id)
    save_message(conn_id, chat_id, message, owner_id)
    if from_id != owner_id:
        log.info(f"[in] {message.text or message.caption}")
        afk = afk_get(owner_id, chat_id)
        if afk and not is_online(owner_id, minutes=5):
            key = (owner_id, chat_id)
            if time.time() - _AFK_LAST.get(key, 0) >= 300:
                _AFK_LAST[key] = time.time()
                msg_text = esc(afk["message"] or "Сейчас не в сети")
                if has_united_love(owner_id):
                    # С подпиской — как от лица владельца
                    afk_reply = msg_text
                else:
                    # Без подписки — с пометкой автоответа
                    afk_reply = "🤖 <b>Автоответ</b>" + NL + NL + q(msg_text)
                try:
                    await bot.send_message(chat_id=chat_id,
                        text=afk_reply, business_connection_id=conn_id)
                except TelegramAPIError as e:
                    log.warning(f"afk reply: {e}")
        return
    text = (message.text or "").strip()
    if not text or text[0] not in (".", "/"): return
    parts = text[1:].split(maxsplit=1)
    if not parts: return
    cmd = parts[0].split("@", 1)[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    # --- АДМИН-ПАНЕЛЬ ---
    if cmd == "adm":
        log.info(f"[adm panel from {owner_id}]")
        if owner_id == ADMIN_ID:
            try:
                ADMIN_WAIT.pop(owner_id, None)
                await bot.send_message(owner_id,
                    "🛡 <b>Админ-панель</b>" + NL + NL + q("Выбери действие 👇"),
                    reply_markup=kb_admin())
            except Exception as e:
                log.warning(f"adm panel: {e}")
        return
    # ---------------------

    if cmd not in KNOWN:
        log.info(f"[unknown .{cmd} ignored]")
        return
    log.info(f"[cmd .{cmd}]")
    try:
        await bot.delete_business_messages(business_connection_id=conn_id, message_ids=[message.message_id])
    except TelegramAPIError: pass
    await biz_dispatch(cmd, arg, message, conn_id, chat_id, owner_id)

async def biz_send(chat_id, conn_id, text):
    try:
        await bot.send_message(chat_id=chat_id, text=text, business_connection_id=conn_id)
    except TelegramAPIError as e:
        log.warning(f"biz_send: {e}")

async def biz_dispatch(cmd, arg, message, conn_id, chat_id, owner_id):
    if cmd == "ping":
        await biz_send(chat_id, conn_id, "🏓 <b>Понг!</b> Бот онлайн."); return
    if cmd == "id":
        await biz_send(chat_id, conn_id, "🆔 " + q(f"<b>Чат:</b> <code>{chat_id}</code>" + NL + f"<b>Ты:</b> <code>{owner_id}</code>")); return
    if cmd == "info":
        c = db()
        p = c.execute("SELECT * FROM saved_messages WHERE chat_id=? AND from_id!=? ORDER BY id DESC LIMIT 1",
                      (chat_id, owner_id)).fetchone()
        c.close()
        if p:
            uname = f" (@{p['from_username']})" if p["from_username"] else ""
            await biz_send(chat_id, conn_id, "👤 <b>Собеседник</b>" + NL
                + q(f"Имя: <b>{esc(p['from_name'])}</b>{uname}" + NL + f"ID: <code>{p['from_id']}</code>"))
        else:
            await biz_send(chat_id, conn_id, "ℹ️ " + q("Нет данных о собеседнике."))
        return
    if cmd == "help":
        await biz_send(chat_id, conn_id,
            "✏️ <b>Команды</b>" + NL + NL
            + "<b>📌 Инфо</b>" + NL
            + q("<code>.ping</code> · <code>.id</code> · <code>.info</code>") + NL
            + "<b>🎭 Fun</b>" + NL
            + q("<code>.love .flip .rps .kawai .kub .roast</code>") + NL
            + "<b>📝 Стиль</b>" + NL
            + q("<code>.anim .type .title .split .перевод</code>") + NL
            + "<b>⏳ AFK</b>" + NL
            + q("<code>.afk текст</code> · <code>.unafk</code>"))
        return
    if cmd == "afk":
        text = (arg or "").strip()
        if not text:
            await biz_send(chat_id, conn_id,
                "⏳ <b>AFK-режим</b>" + NL + NL
                + q("Установи автоответ, чтобы собеседник знал, что ты не в сети." + NL + NL
                    + "📝 <b>Как использовать</b>" + NL
                    + "<code>.afk текст</code> — включить" + NL
                    + "<code>.unafk</code> — выключить" + NL + NL
                    + "⚡ <i>Автоответ сработает только когда ты офлайн</i> (5+ мин без активности)."))
            return
        afk_set(owner_id, chat_id, text)
        await biz_send(chat_id, conn_id,
            "⏳ <b>AFK активирован</b>" + NL + NL
            + q("Автоответ: <i>" + esc(text) + "</i>" + NL + NL
                + "⚡ Сработает, только когда ты офлайн." + NL
                + "<i>Выключить:</i> <code>.unafk</code>"))
        return
    if cmd == "unafk":
        afk_off(owner_id, chat_id)
        await biz_send(chat_id, conn_id, "✅ <b>AFK отключён</b>" + NL + NL
            + q("Теперь сообщения приходят как обычно."))
        return
    if cmd == "love":
        pct = random.randint(1, 100)
        bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
        _, suffix = try_game_reward(owner_id, "love", 3)
        await biz_send(chat_id, conn_id, "❤️ <b>Совместимость</b>" + NL + NL + q(f"{bar}" + NL + f"<b>{pct}%</b>") + suffix); return
    if cmd == "flip":
        r = random.choice(['Орёл','Решка'])
        _, suffix = try_game_reward(owner_id, "flip", 2)
        await biz_send(chat_id, conn_id, "🪙 " + q(f"<b>{r}</b>") + suffix); return
    if cmd == "rps":
        a = arg.lower()
        if a not in ("камень","ножницы","бумага"):
            await biz_send(chat_id, conn_id, "❌ " + q("Использование: <code>.rps камень|ножницы|бумага</code>")); return
        opts = ["камень","ножницы","бумага"]; b = random.choice(opts)
        beats = {"камень":"ножницы","ножницы":"бумага","бумага":"камень"}
        if a == b:
            res = "Ничья 🤝"; base = 3
        elif beats[a] == b:
            res = "Победа 🎉"; base = 8
        else:
            res = "Проигрыш 🤖"; base = 0
        _, suffix = try_game_reward(owner_id, "rps", base)
        await biz_send(chat_id, conn_id,
            "🎮 <b>КНБ</b>" + NL + NL
            + q(f"<b>Ты:</b> {a}" + NL + f"<b>Бот:</b> {b}" + NL + NL + f"<b>{res}</b>")
            + suffix)
        return
    if cmd == "kawai":
        r = random.choice(['(≧◡≦)','(づ｡◕‿‿◕｡)づ','(っ˘ω˘ς)','(*/ω＼*)'])
        _, suffix = try_game_reward(owner_id, "kawai", 1)
        await biz_send(chat_id, conn_id, "✨ " + q(f"<b>{r}</b>") + suffix); return
    if cmd == "kub":
        n = random.randint(1,6)
        _, suffix = try_game_reward(owner_id, "kub", n)
        await biz_send(chat_id, conn_id, "🎲 " + q(f"<b>{n}</b>") + suffix); return
    if cmd == "roast":
        r = random.choice([
            "Ты как WiFi без пароля — все хотят, но толку мало 😏",
            "Ты как капча — вроде нужен, но раздражаешь 😄",
            "Ты как баг в проде — все знают, но никто не трогает 🐛"])
        _, suffix = try_game_reward(owner_id, "roast", 2)
        await biz_send(chat_id, conn_id, "🎭 " + q(r) + suffix); return
    if cmd == "anim":
        l, r = random.choice([("★","★"),("『","』"),("⚡","⚡"),("◤","◢"),("✧","✧")])
        await biz_send(chat_id, conn_id, "✨ " + q(f"<b>{l} {esc(arg or 'текст')} {r}</b>")); return
    if cmd == "type":
        if not arg:
            await biz_send(chat_id, conn_id, "❌ " + q("Использование: <code>.type текст</code>")); return
        l33t = str.maketrans({"a":"4","e":"3","i":"1","o":"0","s":"5","t":"7","A":"4","E":"3","I":"1","O":"0","S":"5","T":"7"})
        await biz_send(chat_id, conn_id, "📝 " + q(f"<code>{esc(arg.translate(l33t))}</code>")); return
    if cmd == "title":
        t = esc(arg or BRAND); line = "═" * (len(t) + 4)
        await biz_send(chat_id, conn_id, "📐 " + q(f"<pre>╔{line}╗" + NL + f"║  {t}  ║" + NL + f"╚{line}╝</pre>")); return
    if cmd == "split":
        if not arg:
            await biz_send(chat_id, conn_id, "❌ " + q("Использование: <code>.split сл1 сл2</code>")); return
        await biz_send(chat_id, conn_id, "📋 " + q(NL.join(f"▫️ {esc(p)}" for p in arg.split()))); return
    if cmd in ("перевод", "translit"):
        if not arg:
            await biz_send(chat_id, conn_id, "❌ " + q("Использование: <code>.перевод текст</code>")); return
        tr = {"а":"a","б":"b","в":"v","г":"g","д":"d","е":"e","ё":"e","ж":"zh","з":"z","и":"i","й":"y","к":"k","л":"l","м":"m","н":"n","о":"o","п":"p","р":"r","с":"s","т":"t","у":"u","ф":"f","х":"h","ц":"c","ч":"ch","ш":"sh","щ":"sch","ъ":"","ы":"y","ь":"","э":"e","ю":"yu","я":"ya"}
        out = []
        for ch in arg:
            low = ch.lower(); mp = tr.get(low, ch)
            if ch.isupper() and mp: mp = mp[0].upper() + mp[1:]
            out.append(mp)
        await biz_send(chat_id, conn_id, "🔤 " + q(f"<code>{esc(''.join(out))}</code>")); return

@dp.edited_business_message()
async def on_edited(message):
    conn_id = message.business_connection_id
    if not conn_id: return
    await ensure_connection(conn_id)
    conn = get_connection(conn_id)
    if not conn: return
    if not user_notify_on(conn["user_id"]): return
    old = get_message(conn_id, message.chat.id, message.message_id)
    if not old: return
    old_text = old["text"] or ""
    new_text = message.text or message.caption or ""
    if old_text == new_text: return
    od = dict(old)
    name = esc(od.get("from_name") or "?")
    uname = od.get("from_username")
    uname_txt = f" (@{uname})" if uname else ""

    rich_html = (
        "<h2>✏️ Изменённое сообщение</h2>"
        + f"От <b>{name}</b>{uname_txt}" + chr(10) + chr(10)
        + "<details open>"
        + "<summary><b>Прошлое сообщение</b></summary>"
        + f"<p>{esc(old_text)}</p>"
        + "</details>" + chr(10) + chr(10)
        + "<details open>"
        + "<summary><b>Новое сообщение</b></summary>"
        + f"<p>{esc(new_text)}</p>"
        + "</details>"
    )
    await send_rich(conn["user_chat_id"], rich_html,
                    kb_peer(message.chat.id, uname, archive_url(conn_id)))
    c = db(); c.execute("UPDATE saved_messages SET text=? WHERE id=?", (new_text, od["id"])); c.commit(); c.close()


@dp.deleted_business_messages()
async def on_deleted(event):
    conn_id = event.business_connection_id
    if not conn_id: return
    conn = await ensure_connection(conn_id)
    if not conn: return
    if not user_notify_on(conn["user_id"]): return
    for mid in event.message_ids:
        s = get_message(conn_id, event.chat.id, mid)
        if s:
            sd = dict(s)
            name = esc(sd.get("from_name") or "?")
            uname = sd.get("from_username")
            uname_txt = f" (@{uname})" if uname else ""
            body = esc(sd.get("text") or "—")
        else:
            name, uname_txt, body, uname = "Неизвестно", "", "текст не сохранён", None

        rich_html = (
            "<h2>⚡ Удалённое сообщение</h2>"
            + f"От <b>{name}</b>{uname_txt}" + chr(10) + chr(10)
            + "<details open>"
            + "<summary><b>Текст сообщения</b></summary>"
            + f"<p>{body}</p>"
            + "</details>"
        )
        await send_rich(conn["user_chat_id"], rich_html,
                        kb_peer(event.chat.id, uname, archive_url(conn_id)))


@dp.error()
async def on_error(event):
    log.exception(f"Error: {event.exception}"); return True

async def main():
    init_db()
    dp.include_router(games.router)
    log.info(f"{BRAND} запущен.")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    asyncio.run(main())
