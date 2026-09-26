import asyncio, os, sys, sqlite3
import qrcode
from deep_translator import GoogleTranslator
import cv2
from pyzbar.pyzbar import decode as qr_decode

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import random, html, logging, time, secrets
from matplotlib import dates as mdates
from datetime import datetime, timezone, timedelta
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, BaseFilter
from aiogram.types import (
    FSInputFile,
    LabeledPrice, PreCheckoutQuery, Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton, CopyTextButton,
)
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.client.default import DefaultBotProperties

try:
    from aiogram.dispatcher.event.bases import SkipHandler
except Exception:
    class SkipHandler(Exception):
        pass

# Rich-сообщения есть не во всех версиях aiogram — если нет, шлём обычным текстом
try:
    from aiogram.types import InputRichMessage
    from aiogram.methods.send_rich_message import SendRichMessage
except Exception:
    InputRichMessage = SendRichMessage = None

# ============ НАСТРОЙКИ ============
# ТОКЕН БОЛЬШЕ НЕ В КОДЕ: export BOT_TOKEN="123:abc"
# --- автозагрузка .env ---
_env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_file):
    with open(_env_file, "r", encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())
# ------------------------

TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not TOKEN:
    sys.exit("Токен не задан. Проверь .env файл рядом с bot.py")
DB = os.getenv("DB_PATH", "united_dialog.db")
LOG_FILE = os.getenv("LOG_FILE", "bot.log")
BRAND = "United Dialog"
BOT_USERNAME = "UnitedDialogBot"
ADMIN_ID = 7113397602
GITHUB_USER = "cfmz"
REPO = "united-dialog-web"
SUPPORT = "https://t.me/UnitedDialogSupport"
REF_BONUS = 50            # U-Coin за приглашённого друга
NOTIFY_OWN = True        # уведомлять об удалении/правке СВОИХ сообщений
GAME_COOLDOWN = 30
GAME_DAILY_LIMIT = 50

NL = chr(10)
logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(LOG_FILE, encoding="utf-8")])
log = logging.getLogger("united_dialog")

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

_AFK_LAST = {}
TYPE_TASKS = {}
ADMIN_WAIT = {}
SUPPORT_WAIT = set()
_NOTIFY_TS = {}
_BOT_DELETED = set()      # сообщения, которые удалил сам бот (команды)
_RIGHTS_HINT = {}

# ============ БД ============
def now_utc():
    return datetime.now(timezone.utc)

def db():
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    return c

def _add_col(c, table, col, ddl):
    cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
    if col not in cols:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")

def init_db():
    c = db()
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY, username TEXT, full_name TEXT,
        joined TEXT, is_premium INTEGER DEFAULT 0,
        ucoin INTEGER DEFAULT 0, last_seen TEXT, messages INTEGER DEFAULT 0,
        sub_until TEXT, last_game_ts TEXT, referrer_id INTEGER
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
        user_id INTEGER, plan TEXT, method TEXT, amount INTEGER, created TEXT
    );
    CREATE TABLE IF NOT EXISTS promocodes(
        code TEXT PRIMARY KEY, days INTEGER,
        uses INTEGER DEFAULT 0, max_uses INTEGER DEFAULT 1, created TEXT
    );
    CREATE TABLE IF NOT EXISTS promo_used(
        code TEXT, user_id INTEGER, PRIMARY KEY(code, user_id)
    );
    CREATE TABLE IF NOT EXISTS transactions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, amount INTEGER, reason TEXT, created TEXT
    );
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS support_threads(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, created TEXT, updated TEXT,
        status TEXT DEFAULT 'open',
        last_msg TEXT
    );
    CREATE TABLE IF NOT EXISTS support_messages(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        thread_id INTEGER, from_admin INTEGER DEFAULT 0,
        text TEXT, media_type TEXT, file_id TEXT,
        created TEXT
    );
    CREATE TABLE IF NOT EXISTS support_links(
        admin_msg_id INTEGER PRIMARY KEY,
        thread_id INTEGER, user_id INTEGER
    );
    CREATE TABLE IF NOT EXISTS mutes(
        owner_id INTEGER, chat_id INTEGER,
        until_ts TEXT, muted_at TEXT,
        PRIMARY KEY(owner_id, chat_id)
    );
    CREATE TABLE IF NOT EXISTS afk(
        owner_id INTEGER, chat_id INTEGER, message TEXT,
        enabled INTEGER DEFAULT 1, created TEXT,
        PRIMARY KEY(owner_id, chat_id)
    );
    """)
    # миграции для СТАРОЙ базы — именно их не хватало
    _add_col(c, "users", "sub_until", "TEXT")
    _add_col(c, "users", "last_game_ts", "TEXT")
    _add_col(c, "users", "referrer_id", "INTEGER")
    _add_col(c, "users", "messages", "INTEGER DEFAULT 0")
    _add_col(c, "connections", "notify_on", "INTEGER DEFAULT 1")
    _add_col(c, "connections", "web_token", "TEXT")
    _add_col(c, "connections", "created", "TEXT")
    c.execute("CREATE INDEX IF NOT EXISTS ix_sm_lookup ON saved_messages(owner_id, chat_id, message_id)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_tx_user ON transactions(user_id, created)")
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
    except Exception:
        return False

def _right(conn, *names):
    """Права бизнес-бота: работает и для старого API (поля в conn), и для нового (conn.rights)."""
    r = getattr(conn, "rights", None)
    for n in names:
        for src in (r, conn):
            if src is not None and getattr(src, n, None) is not None:
                return int(bool(getattr(src, n)))
    return 0

def save_connection(conn):
    c = db()
    c.execute("""INSERT INTO connections(id,user_id,user_chat_id,can_reply,can_read,can_delete,is_enabled,created)
                 VALUES(?,?,?,?,?,?,?,?)
                 ON CONFLICT(id) DO UPDATE SET user_id=excluded.user_id,
                 user_chat_id=excluded.user_chat_id, can_reply=excluded.can_reply,
                 can_read=excluded.can_read, can_delete=excluded.can_delete,
                 is_enabled=excluded.is_enabled""",
        (conn.id, conn.user.id, conn.user_chat_id,
         _right(conn, "can_reply"),
         _right(conn, "can_read_messages"),
         _right(conn, "can_delete_all_messages", "can_delete_sent_messages"),
         1 if conn.is_enabled else 0, now_utc().isoformat()))
    c.commit(); c.close()

def get_connection(conn_id):
    c = db(); r = c.execute("SELECT * FROM connections WHERE id=?", (conn_id,)).fetchone(); c.close()
    return r

def ensure_web_token(conn_id):
    c = db()
    r = c.execute("SELECT web_token, web_password FROM connections WHERE id=?", (conn_id,)).fetchone()
    if r and r["web_token"] and r["web_password"]:
        c.close(); return r["web_token"]
    tok = r["web_token"] if (r and r["web_token"]) else secrets.token_urlsafe(12)
    pwd = r["web_password"] if (r and r["web_password"]) else secrets.token_urlsafe(9)
    c.execute("UPDATE connections SET web_token=?, web_password=? WHERE id=?", (tok, pwd, conn_id))
    c.commit(); c.close()
    return tok

def get_web_password(uid):
    c = db()
    r = c.execute("SELECT web_password FROM connections WHERE user_id=? AND web_password IS NOT NULL LIMIT 1", (uid,)).fetchone()
    c.close()
    return r["web_password"] if r else None

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

def archive_url_for_user(uid):
    c = db()
    r = c.execute("SELECT web_token FROM connections WHERE user_id=? AND web_token IS NOT NULL LIMIT 1", (uid,)).fetchone()
    c.close()
    if r: return f"https://{GITHUB_USER}.github.io/{REPO}/u/{r['web_token']}/"
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
    if r is None or r["notify_on"] is None: return True
    return bool(r["notify_on"])

def count_msgs(uid):
    c = db()
    n = c.execute("SELECT COUNT(*) FROM saved_messages WHERE owner_id=?", (uid,)).fetchone()[0]
    c.close(); return n

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

def get_message(owner_id, chat_id, mid):
    # Сначала ищем строго по owner_id, потом без него
    # (у собеседника может быть свой бизнес-бот, который тоже сохраняет)
    c = db()
    r = c.execute("SELECT * FROM saved_messages WHERE owner_id=? AND chat_id=? AND message_id=? ORDER BY id DESC LIMIT 1",
                  (owner_id, chat_id, mid)).fetchone()
    if not r:
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
        get_user(bc.user.id, bc.user.username, bc.user.full_name)
        ensure_web_token(conn_id)
        return get_connection(conn_id)
    except TelegramAPIError as e:
        log.warning(f"ensure_connection: {e}"); return None

# ============ ПОДПИСКА / ПРОМО / ИГРЫ ============
PLANS = {
    "1m":  {"days": 30,  "stars": 50,  "ucoin": 250,  "label": "1 месяц"},
    "3m":  {"days": 90,  "stars": 125, "ucoin": 625,  "label": "3 месяца"},
    "12m": {"days": 365, "stars": 400, "ucoin": 2000, "label": "12 месяцев"},
}

def grant_sub(uid, days):
    get_user(uid)  # чтобы UPDATE не ушёл в пустоту
    c = db()
    r = c.execute("SELECT sub_until FROM users WHERE id=?", (uid,)).fetchone()
    base = now_utc()
    if r and r["sub_until"]:
        try:
            cur = datetime.fromisoformat(r["sub_until"])
            if cur.tzinfo is None: cur = cur.replace(tzinfo=timezone.utc)
            if cur > base: base = cur
        except Exception: pass
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
    code = (code or "").strip().upper()
    if not code:
        return None, "❌ Укажи код: <code>.redeem КОД</code>"
    c = db()
    r = c.execute("SELECT * FROM promocodes WHERE code=?", (code,)).fetchone()
    if not r:
        c.close(); return None, "❌ Промокод не найден"
    if c.execute("SELECT 1 FROM promo_used WHERE code=? AND user_id=?", (code, uid)).fetchone():
        c.close(); return None, "❌ Ты уже использовал этот промокод"
    cur = c.execute("UPDATE promocodes SET uses=uses+1 WHERE code=? AND uses<max_uses", (code,))
    if cur.rowcount == 0:
        c.close(); return None, "❌ Промокод уже исчерпан"
    c.execute("INSERT INTO promo_used(code,user_id) VALUES(?,?)", (code, uid))
    c.commit(); c.close()
    until = grant_sub(uid, r["days"])
    return until, None

def can_play_game(uid):
    get_user(uid)
    c = db()
    r = c.execute("SELECT last_game_ts FROM users WHERE id=?", (uid,)).fetchone()
    if not r:
        c.close(); return False, "no_user"
    ts = r["last_game_ts"]
    if ts:
        try:
            last = datetime.fromisoformat(ts)
            if last.tzinfo is None: last = last.replace(tzinfo=timezone.utc)
            diff = (now_utc() - last).total_seconds()
            if diff < GAME_COOLDOWN:
                c.close()
                return False, f"cooldown_{int(GAME_COOLDOWN - diff) + 1}"
        except Exception: pass
    day_ago = (now_utc() - timedelta(hours=24)).isoformat()
    cnt = c.execute("SELECT COUNT(*) FROM transactions WHERE user_id=? AND reason LIKE 'game_%' AND created >= ?",
                    (uid, day_ago)).fetchone()[0]
    c.close()
    if cnt >= GAME_DAILY_LIMIT:
        return False, "limit"
    return True, None

def try_game_reward(uid, cmd, base_amount):
    get_user(uid)
    if base_amount <= 0:
        c = db()
        c.execute("UPDATE users SET last_game_ts=? WHERE id=?", (now_utc().isoformat(), uid))
        c.commit(); c.close()
        return 0, ""
    ok, reason = can_play_game(uid)
    if not ok:
        if reason and reason.startswith("cooldown_"):
            return 0, NL + NL + "<i>⏳ Следующая награда через " + reason.replace("cooldown_", "") + "с</i>"
        if reason == "limit":
            return 0, NL + NL + "<i>🛑 Дневной лимит наград (" + str(GAME_DAILY_LIMIT) + "). Приходи через сутки.</i>"
        return 0, ""
    c = db()
    c.execute("UPDATE users SET ucoin = MAX(0, ucoin + ?), last_game_ts=? WHERE id=?",
              (base_amount, now_utc().isoformat(), uid))
    c.execute("INSERT INTO transactions(user_id, amount, reason, created) VALUES(?,?,?,?)",
              (uid, base_amount, "game_" + cmd, now_utc().isoformat()))
    c.commit()
    new_bal = c.execute("SELECT ucoin FROM users WHERE id=?", (uid,)).fetchone()["ucoin"]
    c.close()
    return base_amount, NL + NL + "💰 <b>+" + str(base_amount) + "</b> U-Coin · баланс: <b>" + str(new_bal) + "</b>"

def game_status_text(uid):
    """Сколько наград за игры получено за 24ч."""
    c = db()
    day_ago = (now_utc() - timedelta(hours=24)).isoformat()
    cnt = c.execute("SELECT COUNT(*) FROM transactions WHERE user_id=? AND reason LIKE 'game_%' AND created >= ?",
                    (uid, day_ago)).fetchone()[0]
    c.close()
    return cnt

def has_united_love(uid):
    c = db()
    r = c.execute("SELECT sub_until FROM users WHERE id=?", (uid,)).fetchone()
    c.close()
    if not r or not r["sub_until"]: return False
    try:
        until = datetime.fromisoformat(r["sub_until"])
        if until.tzinfo is None: until = until.replace(tzinfo=timezone.utc)
        return until > now_utc()
    except Exception:
        return False

TYPE_ACTIONS = {
    "кружок": "record_video_note",
    "видео": "record_video",
    "голосовое": "record_voice",
    "фото": "upload_photo",
    "файл": "upload_document",
    "стикер": "choose_sticker",
    "геолокация": "find_location",
    "печатает": "typing",
    "typing": "typing",
}

async def _type_loop(owner_id, chat_id, conn_id, action):
    """Шлёт chat_action каждые 4 сек, пока не отменят."""
    key = (owner_id, chat_id)
    try:
        while True:
            # проверка: жива ли задача
            if TYPE_TASKS.get(key, {}).get("action") != action:
                return
            try:
                await bot.send_chat_action(chat_id=chat_id, action=action,
                                            business_connection_id=conn_id)
            except TelegramAPIError as e:
                log.warning(f"type action: {e}")
                TYPE_TASKS.pop(key, None)
                return
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        log.warning(f"_type_loop: {e}")



# ============ VOICEMOD ============
VOICE_PRESETS = {
    "1":  (1.35, 0.74),                # Мультяшный
    "2":  (0.75, 1.33),                # Робот
    "3":  (1.55, 0.65),                # Девочка
    "4":  (0.55, 1.82),                # Демон
    "5":  (1.55, 0.72),                # Бурундук
    "6":  (1.50, 0.66),                # Гелий
    "7":  (0.72, 1.38),                # Старик
    # Эффекты — усиленные
    "8":  "aecho=1.0:0.9:400|800:0.6|0.4",                        # Эхо (2 повтора, громкие)
    "9":  "highpass=f=500,lowpass=f=2800,volume=1.3",              # Телефон (узкий + громче)
    "10": "acrusher=bits=4:mode=log:aa=1:mix=0.9",                 # 8-бит (жёстче)
    "11": "tremolo=f=12:d=0.9",                                    # Вибрация (глубже)
    "12": "aecho=1.0:0.85:150|300|600:0.7|0.5|0.3",                # Пещера (3 повтора)
    "13": "highpass=f=500,lowpass=f=2500,equalizer=f=1200:t=q:w=3:g=15,acompressor=threshold=-35dB:ratio=20:attack=1:release=20,acrusher=bits=1:mode=log:mix=1.0,volume=5.0,alimiter=limit=0.9",
}
VOICE_PRESET_NAMES = {
    "1":  "🎈 Мультяшный",
    "2":  "🤖 Робот",
    "3":  "👧 Девочка",
    "4":  "👹 Демон",
    "5":  "🐿 Бурундук",
    "6":  "🎈 Гелий",
    "7":  "👴 Старик",
    "8":  "🏔 Эхо",
    "9":  "📞 Телефон",
    "10": "🎮 8-бит",
    "11": "📳 Вибрация",
    "12": "🕳 Пещера",
    "13": "🎖 Танкист",
}

def voicemod_set(owner_id, chat_id, preset):
    c = db()
    c.execute("""INSERT INTO voice_modes(owner_id,chat_id,preset,enabled)
                 VALUES(?,?,?,1)
                 ON CONFLICT(owner_id,chat_id) DO UPDATE SET
                 preset=excluded.preset, enabled=1""",
        (owner_id, chat_id, preset))
    c.commit(); c.close()

def voicemod_off(owner_id, chat_id):
    c = db()
    c.execute("DELETE FROM voice_modes WHERE owner_id=? AND chat_id=?", (owner_id, chat_id))
    c.commit(); c.close()

def voicemod_get(owner_id, chat_id):
    c = db()
    r = c.execute("SELECT preset FROM voice_modes WHERE owner_id=? AND chat_id=? AND enabled=1",
                  (owner_id, chat_id)).fetchone()
    c.close()
    return r["preset"] if r else None


async def _handle_voicemod_message(message, conn_id, chat_id, owner_id, preset):
    """Тихо удаляет оригинал, отправляет изменённый."""
    # 1. Удаляем СРАЗУ (мгновенно)
    try:
        await bot.delete_business_messages(
            business_connection_id=conn_id,
            message_ids=[message.message_id])
    except TelegramAPIError:
        pass

    # Помечаем — чтобы не пришло уведомление владельцу
    _BOT_DELETED.add((chat_id, message.message_id))

    # 2. Скачиваем + обрабатываем + отправляем
    in_ogg = f"/tmp/vm_in_{message.message_id}.ogg"
    out_ogg = f"/tmp/vm_out_{message.message_id}.ogg"
    try:
        tg_file = await bot.get_file(message.voice.file_id)
        await bot.download_file(tg_file.file_path, in_ogg)
        await _voice_transform(in_ogg, out_ogg, preset)
        from aiogram.types import FSInputFile
        await bot.send_voice(
            chat_id=chat_id, voice=FSInputFile(out_ogg),
            business_connection_id=conn_id)
        log.info(f"[vm sent] preset={preset} chat={chat_id} mid={message.message_id}")
    except Exception as e:
        log.warning(f"voicemod fail: {e}")
        try: await bot.send_message(owner_id, f"❌ VoiceMod: {e}")
        except: pass
    finally:
        for pp in (in_ogg, out_ogg):
            try: os.remove(pp)
            except: pass




async def _voice_transform(in_path, out_path, preset):
    """Меняет голос. preset — 1-12, внутри pitch/tempo или строка-фильтр.
    Обрабатываем через WAV, потом кодируем в opus — так эффекты слышны лучше."""
    import tempfile
    val = VOICE_PRESETS.get(preset, VOICE_PRESETS["1"])
    if isinstance(val, tuple):
        pitch, tempo = val
        af = f"asetrate=48000*{pitch},aresample=48000,atempo={tempo}"
    else:
        af = val

    wav_tmp = tempfile.mktemp(suffix=".wav")
    try:
        # Шаг 1: ogg → wav 48kHz mono + фильтр
        cmd1 = [
            "ffmpeg", "-y", "-i", in_path,
            "-af", af,
            "-ar", "48000", "-ac", "1",
            "-c:a", "pcm_s16le",
            wav_tmp,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd1, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"step1: {err.decode()[-250:]}")

        # Шаг 2: wav → opus 32кбит/с (качество выше, чем стандарт tg)
        cmd2 = [
            "ffmpeg", "-y", "-i", wav_tmp,
            "-c:a", "libopus", "-b:a", "32k", "-vbr", "on",
            "-application", "voip",
            out_path,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd2, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"step2: {err.decode()[-250:]}")

        sz_in = os.path.getsize(in_path) if os.path.exists(in_path) else 0
        sz_out = os.path.getsize(out_path) if os.path.exists(out_path) else 0
        log.info(f"[vm transform] preset={preset} filter={af[:50]} in={sz_in}b out={sz_out}b")
    finally:
        try: os.remove(wav_tmp)
        except: pass



# ============ QR-КОДЫ ============
def _make_wifi_qr(ssid, password, security="WPA"):
    """Генерирует PNG с Wi-Fi QR-кодом."""
    wifi_str = f"WIFI:T:{security};S:{ssid};P:{password};;"
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(wifi_str)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    path = f"/tmp/qr_wifi_{int(time.time())}.png"
    img.save(path)
    return path

def _make_vcard_qr(name, phone):
    """Генерирует PNG с vCard QR-кодом."""
    vcard_str = f"BEGIN:VCARD\nVERSION:3.0\nFN:{name}\nTEL:{phone}\nEND:VCARD"
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(vcard_str)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    path = f"/tmp/qr_vcard_{int(time.time())}.png"
    img.save(path)
    return path

def _read_qr_from_image(file_path):
    """Читает QR-код с изображения."""
    try:
        img = cv2.imread(file_path)
        if img is None:
            return None
        data = qr_decode(img)
        if data:
            return data[0].data.decode("utf-8")
    except Exception:
        pass
    return None


# ============ SUPPORT ============
def support_thread_get_or_create(uid):
    c = db()
    r = c.execute("SELECT * FROM support_threads WHERE user_id=? AND status='open' ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
    if not r:
        c.execute("INSERT INTO support_threads(user_id, created, updated, status) VALUES(?,?,?, 'open')",
                  (uid, now_utc().isoformat(), now_utc().isoformat()))
        c.commit()
        r = c.execute("SELECT * FROM support_threads WHERE user_id=? ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
    c.close(); return r

def support_save_msg(thread_id, from_admin, text, media_type=None, file_id=None):
    c = db()
    c.execute("""INSERT INTO support_messages(thread_id, from_admin, text, media_type, file_id, created)
                 VALUES(?,?,?,?,?,?)""",
              (thread_id, int(from_admin), text, media_type, file_id, now_utc().isoformat()))
    c.execute("UPDATE support_threads SET updated=?, last_msg=? WHERE id=?",
              (now_utc().isoformat(), (text or "")[:60], thread_id))
    c.commit(); c.close()

def support_close(thread_id):
    c = db()
    c.execute("UPDATE support_threads SET status='closed' WHERE id=?", (thread_id,))
    c.commit(); c.close()

def support_link(admin_msg_id, thread_id, user_id):
    c = db()
    c.execute("CREATE TABLE IF NOT EXISTS support_links(admin_msg_id INTEGER PRIMARY KEY, thread_id INTEGER, user_id INTEGER)")
    c.execute("INSERT OR REPLACE INTO support_links(admin_msg_id, thread_id, user_id) VALUES(?,?,?)",
              (admin_msg_id, thread_id, user_id))
    c.commit(); c.close()

def support_find_thread(admin_msg_id):
    c = db()
    c.execute("CREATE TABLE IF NOT EXISTS support_links(admin_msg_id INTEGER PRIMARY KEY, thread_id INTEGER, user_id INTEGER)")
    r = c.execute("SELECT thread_id, user_id FROM support_links WHERE admin_msg_id=?", (admin_msg_id,)).fetchone()
    c.close(); return r

def support_find_any_thread_by_user(uid):
    c = db()
    r = c.execute("SELECT * FROM support_threads WHERE user_id=? AND status='open' ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
    c.close(); return r

def _support_notify_admin(m, t, text):
    """Отправляет уведомление админу."""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [B("🔒 Закрыть тикет", f"sup_close_{t['id']}", style="danger")]])
    header = (
        "📩 <b>Поддержка</b> · тикет #<b>" + str(t["id"]) + "</b>" + NL + NL
        + q(f"👤 <b>{esc(m.from_user.full_name)}</b>" + NL
            + f"🆔 <code>{m.from_user.id}</code>" + NL
            + (f"🔗 @{esc(m.from_user.username)}" if m.from_user.username else ""))
        + NL + NL
        + (text or "<i>без текста</i>")
    )
    return header, kb


def mute_set(owner_id, chat_id, minutes):
    until = now_utc() + timedelta(minutes=minutes)
    c = db()
    c.execute("""INSERT INTO mutes(owner_id,chat_id,until_ts,muted_at)
                 VALUES(?,?,?,?)
                 ON CONFLICT(owner_id,chat_id) DO UPDATE SET
                 until_ts=excluded.until_ts, muted_at=excluded.muted_at""",
        (owner_id, chat_id, until.isoformat(), now_utc().isoformat()))
    c.commit(); c.close()
    return until

def mute_off(owner_id, chat_id):
    c = db()
    c.execute("DELETE FROM mutes WHERE owner_id=? AND chat_id=?", (owner_id, chat_id))
    c.commit(); c.close()

def mute_get(owner_id, chat_id):
    c = db()
    r = c.execute("SELECT * FROM mutes WHERE owner_id=? AND chat_id=?",
                  (owner_id, chat_id)).fetchone()
    c.close()
    return r

def mute_active(owner_id, chat_id):
    r = mute_get(owner_id, chat_id)
    if not r: return None
    try:
        until = datetime.fromisoformat(r["until_ts"])
        if until.tzinfo is None: until = until.replace(tzinfo=timezone.utc)
        if until <= now_utc():
            return None
        return until
    except Exception:
        return None


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

async def give_referral(new_uid, ref_id):
    if ref_id == new_uid: return
    c = db()
    if not c.execute("SELECT 1 FROM users WHERE id=?", (ref_id,)).fetchone():
        c.close(); return
    c.execute("UPDATE users SET referrer_id=? WHERE id=?", (ref_id, new_uid))
    c.execute("UPDATE users SET ucoin=ucoin+? WHERE id=?", (REF_BONUS, ref_id))
    c.execute("INSERT INTO transactions(user_id,amount,reason,created) VALUES(?,?,?,?)",
              (ref_id, REF_BONUS, "ref", now_utc().isoformat()))
    c.commit(); c.close()
    try:
        await bot.send_message(ref_id, "🎉 <b>Новый реферал!</b>" + NL + NL
                               + q("+" + str(REF_BONUS) + " U-Coin на баланс"))
    except TelegramAPIError: pass

# ============ UI ============
def B(text, cb=None, url=None, style=None, copy=None):
    kw = {"text": text}
    if cb: kw["callback_data"] = cb
    if url: kw["url"] = url
    if copy: kw["copy_text"] = CopyTextButton(text=copy)
    if style:
        try: return InlineKeyboardButton(**kw, style=style)
        except Exception: pass
    return InlineKeyboardButton(**kw)

def q(t): return f"<blockquote>{t}</blockquote>"
def esc(s): return html.escape(s or "")
def clip(s, n=1500):
    s = s or ""
    return s if len(s) <= n else s[:n] + "…"

def kb_main():
    return InlineKeyboardMarkup(inline_keyboard=[
        [B("🌐 Веб-Архив", "m_archive", style="success")],
        [B("👤 Профиль", "m_profile", style="success")],
        [B("🔄 Функции", "m_features", style="primary"),
         B("📊 Статистика", "m_stats", style="primary")],
        [B("✏️ Команды", "m_cmds", style="primary"),
         B("🎮 Игры", "m_games", style="success")],
        [B("👥 Рефералы", "m_refs", style="primary"),
         B("💎 Подписка", "m_sub", style="primary")],
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
        [B("📊 Статистика", "adm_stats", style="primary"),
         B("📈 Аналитика", "adm_an", style="success")],
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
    return InlineKeyboardMarkup(inline_keyboard=[[B("← Назад в панель", "adm_main", style="danger")]])


# ============ ГРАФИКИ ============
plt.rcParams.update({
    "figure.facecolor": "#17212b",
    "axes.facecolor": "#0e1621",
    "axes.edgecolor": "#2b3e50",
    "axes.labelcolor": "#8a9bae",
    "xtick.color": "#8a9bae",
    "ytick.color": "#8a9bae",
    "text.color": "#ffffff",
    "grid.color": "#1e2b38",
    "grid.linestyle": "--",
    "font.size": 10,
})

def build_activity_chart(days, msgs, users, path):
    """Строит PNG с двумя графиками: сообщения и новые юзеры."""
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(9, 6), dpi=120)
    fig.suptitle("United Dialog · Активность за 7 дней", color="#64b5f6", fontsize=14, fontweight="bold")

    labels = [d.strftime("%d.%m") for d in days]
    x = list(range(len(days)))

    # Верхний — сообщения
    ax1.plot(x, msgs, marker="o", color="#64b5f6", linewidth=2, markersize=8)
    ax1.fill_between(x, msgs, color="#64b5f6", alpha=0.2)
    ax1.set_title("Сообщений в день", color="#ffffff", fontsize=11, loc="left")
    ax1.set_xticks(x); ax1.set_xticklabels(labels)
    ax1.grid(True, alpha=0.4)
    for i, v in enumerate(msgs):
        ax1.annotate(str(v), (i, v), textcoords="offset points", xytext=(0, 8),
                     ha="center", color="#ffffff", fontsize=9)

    # Нижний — новые юзеры
    ax2.bar(x, users, color="#4caf50", alpha=0.85)
    ax2.set_title("Новых юзеров", color="#ffffff", fontsize=11, loc="left")
    ax2.set_xticks(x); ax2.set_xticklabels(labels)
    ax2.grid(True, alpha=0.4, axis="y")
    for i, v in enumerate(users):
        ax2.annotate(str(v), (i, v), textcoords="offset points", xytext=(0, 4),
                     ha="center", color="#ffffff", fontsize=9)

    plt.tight_layout()
    fig.savefig(path, facecolor="#17212b")
    plt.close(fig)


def build_hours_chart(hours, path):
    """Гистограмма по 24 часам."""
    fig, ax = plt.subplots(figsize=(9, 4), dpi=120)
    fig.suptitle("Топ активных часов (UTC)", color="#64b5f6", fontsize=14, fontweight="bold")

    x = list(range(24))
    colors = ["#64b5f6"] * 24
    top5 = sorted(range(24), key=lambda i: -hours[i])[:5]
    for i in top5:
        colors[i] = "#ff9800"

    bars = ax.bar(x, hours, color=colors, alpha=0.9)
    ax.set_xticks(x)
    ax.set_xticklabels([f"{h:02d}" for h in x], fontsize=8)
    ax.set_ylabel("Сообщений", color="#8a9bae")
    ax.grid(True, alpha=0.3, axis="y")
    for i, v in enumerate(hours):
        if v > 0:
            ax.annotate(str(v), (i, v), textcoords="offset points", xytext=(0, 4),
                        ha="center", color="#ffffff", fontsize=8)

    plt.tight_layout()
    fig.savefig(path, facecolor="#17212b")
    plt.close(fig)


def build_growth_chart(days, cumulative, path):
    """Накопительный график роста юзеров."""
    fig, ax = plt.subplots(figsize=(9, 4), dpi=120)
    fig.suptitle("Рост юзеров", color="#64b5f6", fontsize=14, fontweight="bold")

    labels = [d.strftime("%d.%m") for d in days]
    x = list(range(len(days)))

    ax.plot(x, cumulative, marker="o", color="#4caf50", linewidth=2, markersize=7)
    ax.fill_between(x, cumulative, color="#4caf50", alpha=0.25)
    ax.set_xticks(x); ax.set_xticklabels(labels)
    ax.set_ylabel("Всего юзеров", color="#8a9bae")
    ax.grid(True, alpha=0.4)

    for i, v in enumerate(cumulative):
        ax.annotate(str(v), (i, v), textcoords="offset points", xytext=(0, 8),
                    ha="center", color="#ffffff", fontsize=9)

    plt.tight_layout()
    fig.savefig(path, facecolor="#17212b")
    plt.close(fig)

# ============ АНАЛИТИКА ============
def kb_admin_analytics():
    return InlineKeyboardMarkup(inline_keyboard=[
        [B("📈 График за 7 дней", "adm_an_week", style="primary")],
        [B("🕒 Топ активных часов", "adm_an_hours", style="primary"),
         B("👥 Рост юзеров", "adm_an_growth", style="success")],
        [B("📁 CSV — юзеры", "adm_an_csv", style="success"),
         B("📦 JSON — вся БД", "adm_an_json", style="success")],
        [B("← Назад в панель", "adm_main", style="danger")]])

def _sparkline(values, width=20):
    """ASCII-график из чисел."""
    if not values: return ""
    mx = max(values) or 1
    chars = "▁▂▃▄▅▆▇█"
    out = []
    for v in values:
        idx = int((v / mx) * (len(chars) - 1))
        out.append(chars[idx])
    return "".join(out)


def kb_back(t="m_main"):
    return InlineKeyboardMarkup(inline_keyboard=[[B("← Назад", t, style="danger")]])

def kb_cmds():
    def btn(label, cmd, style="success"):
        return B(label, "cmd_" + cmd, style=style)
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
        [btn("💰 .balance", "balance"), btn("🎟 .redeem", "redeem")],
        [btn("✉️ Одноразовые сообщения", "oneshot", "success")],
        [B("← Назад", "m_main", style="danger")],
    ])

def kb_peer(username=None, archive=None):
    """Кнопка «Перейти» только с username: tg://user?id часто даёт BUTTON_USER_PRIVACY_RESTRICTED
    и роняет всё уведомление."""
    arch = archive or f"https://github.com/{GITHUB_USER}/{REPO}"
    row = []
    if username:
        row.append(B("↗ Перейти", url=f"https://t.me/{username}", style="primary"))
    row.append(B("📁 Веб-Архив", url=arch, style="success"))
    return InlineKeyboardMarkup(inline_keyboard=[row])

def start_banner_url():
    return "https://cfmz.github.io/united-dialog-archive/img/photo_2026-09-24_15-16-00.jpg"

def profile_banner_url():
    return "https://cfmz.github.io/united-dialog-archive/img/profile.jpg"

# ============ ТЕКСТЫ ============
def txt_start_connected():
    return ("🚀 <b>" + BRAND + " подключён!</b>" + NL + NL
        + q("🔥 <b>Теперь вам доступны</b>" + NL
            + "🗑 просмотр удалённых и изменённых сообщений" + NL
            + "📷 сохранение фото, видео и кружков" + NL
            + "🆕 все дополнительные функции и команды бота") + NL + NL
        + "😇 <i>Приятного использования!</i>")

def txt_start_new():
    return ("🚀 <b>Добро пожаловать!</b>" + NL + NL
        + q("🤖 Бот полностью бесплатный и готов к работе.") + NL + NL
        + "<b>🔥 Возможности бота</b>" + NL
        + q("🗑 Отслеживание удалённых сообщений" + NL
            + "✏️ Отслеживание изменённых сообщений" + NL
            + "🎥 Поддержка кружков, видео и фотографий" + NL
            + "🆕 Уникальные функции и команды") + NL + NL
        + "<b>❓ Как подключить</b>" + NL
        + q("1. Скопируй <code>@" + BOT_USERNAME + "</code>" + NL
            + "2. Открой Настройки → Telegram Business → Чат-боты" + NL
            + "3. Добавь <code>@" + BOT_USERNAME + "</code>" + NL
            + "4. Дай права: Отвечать + Читать сообщения + Удалять отправленные"))

def txt_profile(u, total):
    j = datetime.fromisoformat(u["joined"])
    if j.tzinfo is None: j = j.replace(tzinfo=timezone.utc)
    d = (now_utc() - j).days
    day_word = "дн."
    if d % 10 == 1 and d % 100 != 11: day_word = "день"
    elif d % 10 in (2, 3, 4) and d % 100 not in (12, 13, 14): day_word = "дня"
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
        + q("🔔 <b>" + BRAND + ":</b> " + con + NL
            + "📅 <b>С нами:</b> <b>" + str(d) + "</b> <i>" + day_word + "</i>" + NL
            + "✉️ <b>Сообщений:</b> <b>" + str(total) + "</b>") + NL
        + q("💰 <b>U-Coin:</b> <b>" + str(u["ucoin"]) + "</b> ⭐") + NL + NL
        + "<i>💝 Спасибо, что пользуетесь </i><b>" + BRAND + "</b>")

def txt_features():
    return ("🔄 <b>Функции</b>" + NL + NL
        + q("🤖 <b>AFK-режим</b>" + NL + "Автоответ: <code>.afk текст</code>" + NL + NL
            + "✨ <b>Стиль</b>" + NL + "<code>.anim .type .title .split .перевод</code>" + NL + NL
            + "🎭 <b>Развлечения</b>" + NL + "<code>.love .flip .rps .kawai .kub .roast</code>" + NL + NL
            + "🎟 <b>Промокод</b>" + NL + "<code>.redeem КОД</code>" + NL + NL
            + "🛡 <b>United Safe</b>" + NL + "Защита профиля (скоро)"))

def txt_stats(u, total):
    return ("📊 <b>Статистика</b>" + NL + NL
        + q("✉️ <b>Сообщений:</b> " + str(total) + NL
            + "⭐ <b>U-Coin:</b> " + str(u["ucoin"]) + NL
            + "🔔 <b>Активен:</b> " + ("Да" if user_has_active_connection(u["id"]) else "Нет")))

def txt_refs(link, u):
    return ("💼 <b>Реферальная программа</b>" + NL + NL
        + q("🔔 <b>Твоя ссылка</b>" + NL + link) + NL
        + q("🏆 <b>Вознаграждение</b>" + NL
            + "+" + str(REF_BONUS) + " U-Coin за каждого нового друга") + NL
        + q("💰 <b>Баланс:</b> " + str(u["ucoin"]) + " U-Coin"))

def txt_sub(u):
    if has_united_love(u["id"]):
        status = "✅ <b>Активна</b>"
        until = u["sub_until"][:10] if u["sub_until"] else "—"
    else:
        status = "❌ <b>Не активна</b>"
        until = "—"
    return (
        "💎 <b>United Love</b>" + NL + NL
        + q("📊 <b>Статус:</b> " + status + NL + "📅 <b>До:</b> " + until) + NL
        + q("🎁 <b>Что даёт подписка:</b>" + NL
            + "• 🚫 Убирает водяные знаки" + NL
            + "• 🎥 Лимит медиа: 100 → 400 МБ" + NL
            + "• 💬 Одноразовые: 10 → 100 в день" + NL
            + "• 🤖 AFK без пометки «автоответ»" + NL
            + "• ⚡ Уникальные команды" + NL
            + "• 🛡 Иммунитет к троллингу") + NL
        + q("💳 <b>Способы оплаты:</b>" + NL + "⭐ Telegram Stars" + NL
            + "💰 U-Coin (баланс: " + str(u["ucoin"]) + ")") + NL
        + q("Выбери тариф 👇"))

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
        + q("Нажми <b>Связаться</b> — опиши проблему." + NL + "Отвечаем в течение 24 часов."))

def txt_games():
    return ("🎮 <b>Игровой центр</b>" + NL + NL
        + q("Играй, соревнуйся и зарабатывай U-Coin." + NL
            + "Игры работают в личке с ботом." + NL + NL
            + "🎲 <b>КНБ с ботом</b> — 5 раундов" + NL
            + "❌⭕ <b>Крестики-нолики</b> — дуэль с другом" + NL
            + "💣 <b>Сапёр</b> — соло" + NL
            + "🎰 <b>Слоты</b> — крути барабаны" + NL
            + "👥 <b>Комната</b> — играй с друзьями"))

# ============ ЛИЧКА С БОТОМ ============
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

class AdminWait(BaseFilter):
    """Ловит текст только от админа, и только когда он ждёт ввод.
    Раньше хендлер глотал ВСЕ текстовые сообщения и блокировал games.router."""
    async def __call__(self, m):
        if not m.from_user or m.from_user.id != ADMIN_ID: return False
        if m.chat.type != "private" or not m.text: return False
        if m.text[0] in (".", "/"): return False
        return m.from_user.id in ADMIN_WAIT

@dp.message(F.text == "/adm")
@dp.message(F.text == ".adm")
async def cmd_adm(m: Message):
    if not m.from_user or m.from_user.id != ADMIN_ID:
        return
    ADMIN_WAIT.pop(m.from_user.id, None)
    await m.answer("🛡 <b>Админ-панель</b>" + NL + NL + q("Выбери действие 👇"), reply_markup=kb_admin())

@dp.message(CommandStart())
@dp.message(PM("start", "menu", "старт", "меню"))
async def pm_start(m: Message):
    uid = m.from_user.id
    c = db(); existed = c.execute("SELECT 1 FROM users WHERE id=?", (uid,)).fetchone() is not None; c.close()
    get_user(uid, m.from_user.username, m.from_user.full_name, m.from_user.is_premium)
    touch_seen(uid)
    if not existed:
        parts = (m.text or "").split(maxsplit=1)
        if len(parts) > 1 and parts[1].startswith("ref_") and parts[1][4:].isdigit():
            await give_referral(uid, int(parts[1][4:]))
    if user_has_active_connection(uid):
        text, kb = txt_start_connected(), kb_main()
    else:
        text = txt_start_new()
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            B("📋 Скопировать", copy=f"@{BOT_USERNAME}", style="success"),
            B("🔘 Подключить", url="tg://settings/edit", style="primary")]])
    try:
        await m.answer_photo(photo=start_banner_url(), caption=text, reply_markup=kb)
    except Exception as e:
        log.warning(f"start photo: {e}")
        await m.answer(text, reply_markup=kb)

@dp.message(PM("redeem"))
async def pm_redeem(m: Message):
    parts = (m.text or "").split(maxsplit=1)
    until, err = redeem_promo(m.from_user.id, parts[1] if len(parts) > 1 else "")
    if err:
        await m.answer(err)
    else:
        await m.answer("✅ <b>Промокод активирован!</b>" + NL + NL + q("United Love до " + until.strftime("%d.%m.%Y")))

async def _edit(c, text, kb=None):
    msg = c.message
    if msg is None: return
    if getattr(msg, "photo", None):      # редактировать текстом фото нельзя
        try: await msg.delete()
        except Exception: pass
        await bot.send_message(c.from_user.id, text, reply_markup=kb)
        return
    try:
        await msg.edit_text(text, reply_markup=kb)
    except TelegramAPIError as e:
        if "not modified" in str(e).lower(): return
        await msg.answer(text, reply_markup=kb)

@dp.callback_query(F.data.startswith("m_"))
async def on_menu_cb(c: CallbackQuery):
    if c.message is None or c.message.business_connection_id:
        await c.answer(); return
    touch_seen(c.from_user.id)
    d = c.data
    u = get_user(c.from_user.id, c.from_user.username, c.from_user.full_name, int(bool(c.from_user.is_premium)))

    if d == "m_main":
        await _edit(c, txt_start_connected() if user_has_active_connection(u["id"]) else txt_start_new(), kb_main())
    if d == "m_archive":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [B("🌐 Открыть архив", url=archive_url_for_user(u["id"]), style="success")],
            [B("🔒 Показать пароль", "m_archive_pw", style="primary")],
            [B("← Назад", "m_main", style="danger")]])
        await _edit(c, "🌐 <b>Веб-Архив</b>" + NL + NL
            + q("Все сохранённые сообщения — на сайте." + NL + NL
                + "🔒 Архив защищён паролем AES-256" + NL
                + "Пароль покажется кнопкой ниже."), kb)
    elif d == "m_archive_pw":
        _c = db()
        r = _c.execute("SELECT web_password FROM connections WHERE user_id=? AND web_password IS NOT NULL LIMIT 1", (u["id"],)).fetchone()
        _c.close()
        if r and r["web_password"]:
            kb_pw = InlineKeyboardMarkup(inline_keyboard=[
                [B("← Назад", "m_archive", style="danger")]])
            await c.message.answer(
                "🔒 <b>Пароль архива</b>" + NL + NL
                + q("Нажми на пароль — он скопируется." + NL + NL
                    + "<code>" + esc(r["web_password"]) + "</code>" + NL + NL
                    + "🔗 Ссылка на архив:" + NL
                    + "<code>" + archive_url_for_user(u["id"]) + "</code>"),
                reply_markup=kb_pw)
        else:
            await c.answer("Пароль ещё не создан. Перезапусти бота.", show_alert=True)
    elif d == "m_profile":
        prof_text, kb = txt_profile(u, count_msgs(u["id"])), kb_profile()
        try: await c.message.delete()
        except Exception: pass
        try:
            await bot.send_photo(chat_id=c.from_user.id, photo=profile_banner_url(), caption=prof_text, reply_markup=kb)
        except Exception as e:
            log.warning(f"profile photo: {e}")
            await bot.send_message(c.from_user.id, prof_text, reply_markup=kb)
    elif d == "m_features":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [B("✏️ Все команды", "m_cmds", style="primary")],
            [B("← Назад", "m_main", style="danger")]])
        await _edit(c, txt_features(), kb)
    elif d == "m_stats":
        await _edit(c, txt_stats(u, count_msgs(u["id"])), kb_back("m_main"))
    elif d == "m_games":   # раньше эта кнопка молча не работала: её перехватывал этот же хендлер
        await _edit(c, txt_games(), kb_games())
    elif d == "m_refs":
        me = await bot.get_me()
        await _edit(c, txt_refs(f"https://t.me/{me.username}?start=ref_{u['id']}", u), kb_back("m_main"))
    elif d == "m_cmds":
        await _edit(c, "✏️ <b>Команды</b>" + NL + NL
                    + q("Напиши команду в любом диалоге (Business) — она выполнится, а твоё сообщение исчезнет."), kb_cmds())
    elif d == "m_sub":
        await _edit(c, txt_sub(u), kb_sub())
    elif d == "m_support":
        has_open = support_find_any_thread_by_user(u["id"])
        if has_open:
            sub = "У тебя активный тикет #<b>" + str(has_open["id"]) + "</b>"
        else:
            sub = "Опиши проблему — ответим в течение 24 часов."
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [B("✍️ Написать в поддержку", "sup_new", style="success")],
            [B("📋 Мои тикеты", "sup_my", style="primary")],
            [B("← Назад", "m_main", style="danger")]])
        await _edit(c,
            "🎧 <b>Поддержка</b>" + NL + NL
            + q("Пиши прямо в бота — сообщение уйдёт в поддержку." + NL + NL + sub), kb)
    elif d == "m_notify":
        notify = user_notify_on(u["id"])
        status = "✅ <b>включены</b>" if notify else "🔕 <b>выключены</b>"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [B("🔔 Включить", "m_notify_on", style="success"),
             B("🔕 Отключить", "m_notify_off", style="danger")],
            [B("← Назад", "m_profile", style="danger")]])
        await _edit(c, "🔔 <b>Уведомления</b>" + NL + NL
            + q("Статус: " + status + NL + NL + "Приходят в личку, когда:" + NL
                + "🗑 удаляют сообщение" + NL + "✏️ изменяют сообщение"), kb)
    elif d in ("m_notify_on", "m_notify_off"):
        on = 1 if d == "m_notify_on" else 0
        _c = db(); _c.execute("UPDATE connections SET notify_on=? WHERE user_id=?", (on, u["id"])); _c.commit(); _c.close()
        await c.answer("🔔 Уведомления включены" if on else "🔕 Уведомления выключены", show_alert=True)
        return
    elif d == "m_ncoin":
        await _edit(c, "💰 <b>U-Coin</b>" + NL + NL
            + q("Баланс: <b>" + str(u["ucoin"]) + "</b> ⭐" + NL + NL + "Зарабатывай за:" + NL
                + "👥 Приглашённых друзей" + NL + "🎮 Игры и команды-развлечения"), kb_back("m_profile"))
    elif d == "m_mirrors":
        await _edit(c, "🌐 <b>Зеркала</b>" + NL + NL
            + q("Основной бот всегда доступен:" + NL + "<code>@" + BOT_USERNAME + "</code>"), kb_back("m_profile"))
    elif d == "m_unisafe":
        await _edit(c, "🛡 <b>United Safe</b>" + NL + NL
            + q("Защита твоего аккаунта." + NL + NL + "🔒 Блокировка по PIN" + NL
                + "🔑 Резервный ключ" + NL + "🚫 Защита от чужих входов") + NL + q("⚙️ В разработке"),
            kb_back("m_profile"))
    elif d == "m_delete_data":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [B("🗑 Да, удалить", "m_delete_confirm", style="danger")],
            [B("← Отмена", "m_profile", style="success")]])
        await _edit(c, "🗑 <b>Удалить мои данные</b>" + NL + NL
            + q("⚠️ Действие необратимо." + NL + NL + "Будут удалены все сохранённые сообщения."), kb)
    elif d == "m_delete_confirm":
        _c = db(); _c.execute("DELETE FROM saved_messages WHERE owner_id=?", (u["id"],)); _c.commit(); _c.close()
        await c.answer("✅ Данные удалены", show_alert=True)
        return
    await c.answer()

@dp.callback_query(F.data.startswith("sub_"))
async def on_sub_cb(c: CallbackQuery):
    touch_seen(c.from_user.id)
    d = c.data
    u = get_user(c.from_user.id, c.from_user.username, c.from_user.full_name)
    if d.startswith("sub_plan_"):
        plan = d[len("sub_plan_"):]
        if plan in PLANS:
            p = PLANS[plan]
            await _edit(c, "💎 <b>" + p["label"] + "</b>" + NL + NL
                + q("📅 Длительность: <b>" + str(p["days"]) + " дней</b>" + NL
                    + "⭐ Stars: <b>" + str(p["stars"]) + "</b>" + NL
                    + "💰 U-Coin: <b>" + str(p["ucoin"]) + "</b>") + NL
                + q("Выбери способ оплаты 👇"), kb_plan(plan))
    elif d.startswith("sub_pay_ucoin_"):
        plan = d[len("sub_pay_ucoin_"):]
        if plan in PLANS:
            p = PLANS[plan]
            _c = db()
            cur = _c.execute("UPDATE users SET ucoin=ucoin-? WHERE id=? AND ucoin>=?", (p["ucoin"], u["id"], p["ucoin"]))
            _c.commit(); ok = cur.rowcount > 0; _c.close()
            if not ok:
                await c.answer("❌ Недостаточно U-Coin", show_alert=True); return
            grant_sub(u["id"], p["days"])
            log_payment(u["id"], plan, "ucoin", p["ucoin"])
            await c.answer("✅ United Love на " + str(p["days"]) + " дней активирована!", show_alert=True)
            return
    elif d.startswith("sub_pay_stars_"):
        plan = d[len("sub_pay_stars_"):]
        if plan in PLANS:
            p = PLANS[plan]
            try:
                await bot.send_invoice(chat_id=c.from_user.id,
                    title="United Love — " + p["label"],
                    description="Подписка на " + str(p["days"]) + " дней",
                    payload="sub_" + plan, provider_token="", currency="XTR",
                    prices=[LabeledPrice(label="United Love", amount=p["stars"])])
            except Exception as e:
                log.warning(f"invoice fail: {e}")
                await c.answer("❌ Ошибка оплаты", show_alert=True); return
    elif d == "sub_promo":
        await _edit(c, "🎁 <b>Промокод</b>" + NL + NL
            + q("Отправь боту (или в любой чат) команду <code>.redeem КОД</code> — активирую подписку."),
            kb_back("m_sub"))
    await c.answer()

@dp.callback_query(F.data.startswith("cmd_"))
async def on_cmd_cb(c: CallbackQuery):
    cmd = c.data[4:]
    texts = {
        "help": ("🆘 <b>.help</b>", "Показывает список всех команд."),
        "afk": ("⏳ <b>.afk</b>", "Автоответ: <code>.afk текст</code> · выключить: <code>.unafk</code>."),
        "love": ("❤️ <b>.love</b>", "Случайная совместимость в процентах."),
        "flip": ("🪙 <b>.flip</b>", "Орёл или решка."),
        "rps": ("✂️ <b>.rps</b>", "Камень-ножницы-бумага: <code>.rps камень</code>."),
        "kawai": ("🌸 <b>.kawai</b>", "Случайный кавайный смайл."),
        "kub": ("🎲 <b>.kub</b>", "Бросает кубик 1-6."),
        "anim": ("✨ <b>.anim</b>", "Красивая обёртка: <code>.anim текст</code>."),
        "type": ("✏️ <b>.type</b>", "L33t-текст: <code>.type текст</code>."),
        "title": ("🎨 <b>.title</b>", "Рамка-заголовок: <code>.title текст</code>."),
        "split": ("🗑 <b>.split</b>", "Разбить текст: <code>.split слово1 слово2</code>."),
        "perevod": ("🌐 <b>.перевод</b>", "Транслит RU→EN: <code>.перевод текст</code>."),
        "id": ("🆔 <b>.id</b>", "Показывает твой ID и ID чата."),
        "info": ("ℹ️ <b>.info</b>", "Информация о собеседнике."),
        "balance": ("💰 <b>.balance</b>", "Баланс U-Coin (придёт тебе в личку с ботом)."),
        "redeem": ("🎟 <b>.redeem</b>", "Активировать промокод: <code>.redeem КОД</code> (ответ придёт в личку)."),
        "oneshot": ("✉️ <b>Одноразовые сообщения</b>", "Скоро."),
    }
    if cmd in texts:
        title, desc = texts[cmd]
        await _edit(c, title + NL + NL + q(desc), kb_back("m_cmds"))
        await c.answer()
    else:
        await c.answer("⚙️ В разработке", show_alert=True)

@dp.pre_checkout_query()
async def on_pre_checkout(pq: PreCheckoutQuery):
    await pq.answer(ok=True)

@dp.message(F.successful_payment)
async def on_success_pay(m: Message):
    payload = m.successful_payment.invoice_payload or ""
    if payload.startswith("sub_"):
        plan = payload[4:]
        if plan in PLANS:
            p = PLANS[plan]
            get_user(m.from_user.id, m.from_user.username, m.from_user.full_name)
            until = grant_sub(m.from_user.id, p["days"])
            log_payment(m.from_user.id, plan, "stars", p["stars"])
            await m.answer("✅ <b>United Love активирована!</b>" + NL + NL
                + q("Подписка на " + str(p["days"]) + " дней до " + until.strftime("%d.%m.%Y")))

# ============ АДМИНКА ============
@dp.callback_query(F.data.startswith("adm_"))
async def adm_cb(c: CallbackQuery):
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
            await c.message.edit_text("🛡 <b>Админ-панель</b>" + NL + NL + q("Выбери действие 👇"), reply_markup=kb_admin())
            await c.answer(); return

        if d == "adm_an":
            await c.message.edit_text(
                "📈 <b>Аналитика</b>" + NL + NL + q("Выбери раздел 👇"),
                reply_markup=kb_admin_analytics())
            await c.answer(); return

        if d == "adm_an_week":
            _c = db()
            from datetime import datetime as _dt, timedelta as _td
            today = now_utc().date()
            days = [(today - _td(days=i)) for i in range(6, -1, -1)]
            msgs = []; users = []
            for d_ in days:
                day_str = d_.isoformat()
                msgs.append(_c.execute("SELECT COUNT(*) FROM saved_messages WHERE created LIKE ?", (day_str + "%",)).fetchone()[0])
                users.append(_c.execute("SELECT COUNT(*) FROM users WHERE joined LIKE ?", (day_str + "%",)).fetchone()[0])
            _c.close()
            fname = f"/tmp/chart_week_{now_utc().strftime('%Y%m%d_%H%M%S')}.png"
            try:
                build_activity_chart(days, msgs, users, fname)
                await bot.send_photo(chat_id=uid, photo=FSInputFile(fname),
                    caption="📈 <b>Активность за 7 дней</b>" + NL + NL
                        + q(f"💬 Всего сообщений: <b>{sum(msgs)}</b>" + NL
                            + f"👥 Новых юзеров: <b>{sum(users)}</b>"))
                await c.answer("📈 Готово")
            except Exception as e:
                log.warning(f"chart week: {e}")
                await c.answer(f"❌ {e}"[:150], show_alert=True)
            finally:
                try: os.remove(fname)
                except: pass
            return

        if d == "adm_an_hours":
            _c = db()
            from datetime import timedelta as _td
            rows = _c.execute("SELECT created FROM saved_messages WHERE created >= ?",
                              ((now_utc() - _td(days=7)).isoformat(),)).fetchall()
            _c.close()
            hours = [0] * 24
            for r in rows:
                try: hours[int(r["created"][11:13])] += 1
                except Exception: pass
            top = sorted(range(24), key=lambda i: -hours[i])[:3]
            fname = f"/tmp/chart_hours_{now_utc().strftime('%Y%m%d_%H%M%S')}.png"
            try:
                build_hours_chart(hours, fname)
                top_lines = [f"<code>{h:02d}:00</code> — {hours[h]} сообщ." for h in top]
                await bot.send_photo(chat_id=uid, photo=FSInputFile(fname),
                    caption="🕒 <b>Топ активных часов</b>" + NL + NL
                        + q("<b>Топ-3 часа:</b>" + NL + NL.join(top_lines)))
                await c.answer("🕒 Готово")
            except Exception as e:
                log.warning(f"chart hours: {e}")
                await c.answer(f"❌ {e}"[:150], show_alert=True)
            finally:
                try: os.remove(fname)
                except: pass
            return

        if d == "adm_an_growth":
            _c = db()
            from datetime import timedelta as _td
            today = now_utc().date()
            days = [(today - _td(days=i)) for i in range(13, -1, -1)]
            # накопительный итог: сколько всего было юзеров на каждый день
            cumulative = []
            for d_ in days:
                day_end = d_.isoformat() + "T23:59:59"
                cnt = _c.execute("SELECT COUNT(*) FROM users WHERE joined <= ?", (day_end,)).fetchone()[0]
                cumulative.append(cnt)
            week = _c.execute("SELECT COUNT(*) FROM users WHERE joined >= ?",
                              ((today - _td(days=7)).isoformat(),)).fetchone()[0]
            month = _c.execute("SELECT COUNT(*) FROM users WHERE joined >= ?",
                               ((today - _td(days=30)).isoformat(),)).fetchone()[0]
            _c.close()
            fname = f"/tmp/chart_growth_{now_utc().strftime('%Y%m%d_%H%M%S')}.png"
            try:
                build_growth_chart(days, cumulative, fname)
                await bot.send_photo(chat_id=uid, photo=FSInputFile(fname),
                    caption="👥 <b>Рост юзеров</b>" + NL + NL
                        + q(f"Всего: <b>{cumulative[-1]}</b>" + NL
                            + f"За 7 дней: <b>+{week}</b>" + NL
                            + f"За 30 дней: <b>+{month}</b>"))
                await c.answer("👥 Готово")
            except Exception as e:
                log.warning(f"chart growth: {e}")
                await c.answer(f"❌ {e}"[:150], show_alert=True)
            finally:
                try: os.remove(fname)
                except: pass
            return

        if d == "adm_an_csv":
            import csv, io as _io
            _c = db()
            rows = _c.execute("""SELECT id, username, full_name, joined,
                                 is_premium, ucoin, messages, reputation, sub_until
                                 FROM users ORDER BY id""").fetchall()
            _c.close()
            buf = _io.StringIO()
            w = csv.writer(buf)
            w.writerow(["id", "username", "full_name", "joined",
                        "is_premium", "ucoin", "messages", "reputation", "sub_until"])
            for r in rows:
                w.writerow([r["id"], r["username"], r["full_name"] or "",
                            r["joined"], r["is_premium"], r["ucoin"],
                            r["messages"], r["reputation"], r["sub_until"] or ""])
            csv_bytes = buf.getvalue().encode("utf-8-sig")
            fname = f"/tmp/users_{now_utc().strftime('%Y%m%d_%H%M')}.csv"
            with open(fname, "wb") as f:
                f.write(csv_bytes)
            try:
                await bot.send_document(chat_id=uid,
                    document=FSInputFile(fname),
                    caption=f"📁 <b>CSV — {len(rows)} юзеров</b>")
                await c.answer("📤 Отправил")
            except Exception as e:
                await c.answer(f"❌ {e}"[:150], show_alert=True)
            finally:
                try: os.remove(fname)
                except: pass
            return

        if d == "adm_an_json":
            import json as _json
            _c = db()
            dump = {}
            for tbl in ("users", "connections", "payments", "promocodes", "transactions", "settings"):
                try:
                    rows = _c.execute(f"SELECT * FROM {tbl}").fetchall()
                    dump[tbl] = [dict(r) for r in rows]
                except Exception:
                    dump[tbl] = []
            _c.close()
            fname = f"/tmp/db_export_{now_utc().strftime('%Y%m%d_%H%M')}.json"
            with open(fname, "w", encoding="utf-8") as f:
                _json.dump(dump, f, ensure_ascii=False, indent=2, default=str)
            try:
                total = sum(len(v) for v in dump.values())
                await bot.send_document(chat_id=uid,
                    document=FSInputFile(fname),
                    caption=f"📦 <b>JSON экспорт БД</b>" + NL + f"Записей: {total}")
                await c.answer("📤 Отправил")
            except Exception as e:
                await c.answer(f"❌ {e}"[:150], show_alert=True)
            finally:
                try: os.remove(fname)
                except: pass
            return

        if d == "adm_stats":
            _c = db()
            users_total = _c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            msgs_total = _c.execute("SELECT COUNT(*) FROM saved_messages").fetchone()[0]
            subs_active = _c.execute("SELECT COUNT(*) FROM users WHERE sub_until IS NOT NULL AND sub_until > ?",
                                     (now_utc().isoformat(),)).fetchone()[0]
            pays_total = _c.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
            today = now_utc().date().isoformat()
            new_today = _c.execute("SELECT COUNT(*) FROM users WHERE joined LIKE ?", (today + "%",)).fetchone()[0]
            _c.close()
            await c.message.edit_text("📊 <b>Статистика</b>" + NL + NL
                + q("👥 Юзеров: <b>" + str(users_total) + "</b>" + NL
                    + "🆕 Сегодня: <b>" + str(new_today) + "</b>" + NL
                    + "💬 Сообщений: <b>" + str(msgs_total) + "</b>" + NL
                    + "💎 Активных подписок: <b>" + str(subs_active) + "</b>" + NL
                    + "💰 Платежей: <b>" + str(pays_total) + "</b>"), reply_markup=kb_admin_back())
            await c.answer(); return

        if d == "adm_find":
            ADMIN_WAIT[uid] = "adm_find"
            await c.message.edit_text("👤 <b>Поиск юзера</b>" + NL + NL + q("Отправь ID юзера:"), reply_markup=kb_admin_back())
            await c.answer(); return

        if d == "adm_give":
            ADMIN_WAIT[uid] = "adm_give"
            await c.message.edit_text("💎 <b>Выдать подписку</b>" + NL + NL + q("Формат: <code>ID ДНЕЙ</code>"), reply_markup=kb_admin_back())
            await c.answer(); return

        if d == "adm_promo":
            await c.message.edit_text("🎁 <b>Промокоды</b>" + NL + NL + q("Управление кодами 👇"),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [B("🎁 Создать", "adm_promo_create", style="success"),
                     B("📋 Список", "adm_promo_list", style="primary")],
                    [B("← Назад", "adm_main", style="danger")]]))
            await c.answer(); return

        if d == "adm_promo_create":
            ADMIN_WAIT[uid] = "adm_promo_create"
            await c.message.edit_text("🎁 <b>Создать промокод</b>" + NL + NL
                + q("Формат: <code>КОД ДНЕЙ ЛИМИТ</code>"), reply_markup=kb_admin_back())
            await c.answer(); return

        if d == "adm_promo_list":
            _c = db()
            rows = _c.execute("SELECT code, days, uses, max_uses FROM promocodes ORDER BY created DESC LIMIT 30").fetchall()
            _c.close()
            if not rows:
                txt = "📋 <b>Промокоды</b>" + NL + NL + q("Список пуст")
            else:
                lines = [f"<code>{esc(r['code'])}</code> — {r['days']}д · {r['uses']}/{r['max_uses']}" for r in rows]
                txt = "📋 <b>Промокоды</b>" + NL + NL + q(NL.join(lines))
            await c.message.edit_text(txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [B("🎁 Создать", "adm_promo_create", style="success")],
                [B("← Назад", "adm_main", style="danger")]]))
            await c.answer(); return

        if d == "adm_pays":
            _c = db()
            rows = _c.execute("SELECT user_id, plan, method, amount FROM payments ORDER BY id DESC LIMIT 15").fetchall()
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
            await c.message.edit_text("📢 <b>Рассылка</b>" + NL + NL + q("Отправь текст (HTML разрешён)."), reply_markup=kb_admin_back())
            await c.answer(); return

        if d == "adm_settings":
            ADMIN_WAIT.pop(uid, None)
            await c.message.edit_text("⚙️ <b>Настройки</b>" + NL + NL + q("Управление ботом 👇"), reply_markup=kb_admin_settings())
            await c.answer(); return

        if d == "adm_t_maintenance":
            new_val = "0" if maintenance_on() else "1"
            set_setting("maintenance", new_val)
            state = "включены 🟢" if new_val == "1" else "выключены 🔴"
            await c.message.edit_text("⚙️ <b>Настройки</b>" + NL + NL + q("Тех. работы: " + state), reply_markup=kb_admin_settings())
            await c.answer(); return

        if d == "adm_t_clean":
            await c.message.edit_text("🗑 <b>Очистка БД</b>" + NL + NL + q("Удалить сообщения:"),
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
                cur = _c.execute("DELETE FROM saved_messages")
            else:
                cutoff = (now_utc() - timedelta(days=int(mode))).isoformat()
                cur = _c.execute("DELETE FROM saved_messages WHERE created < ?", (cutoff,))
            deleted = cur.rowcount
            _c.commit(); _c.close()
            await c.answer(f"✅ Удалено: {deleted}", show_alert=True)
            await c.message.edit_text("⚙️ <b>Настройки</b>" + NL + NL + q(f"🗑 Удалено: <b>{deleted}</b>"),
                                      reply_markup=kb_admin_settings())
            return

        if d == "adm_t_export":
            try:
                _c = db(); _c.execute("PRAGMA wal_checkpoint(TRUNCATE)"); _c.close()
                await bot.send_document(chat_id=uid, document=FSInputFile(DB), caption="💾 <b>Экспорт БД</b>")
                await c.answer("📤 Отправил")
            except Exception as e:
                await c.answer(f"❌ {e}"[:190], show_alert=True)
            return

        if d == "adm_t_logs":
            try:
                with open(LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
                    lines = f.readlines()[-40:]
                text = "".join(lines)
                if len(text) > 3300: text = text[-3300:]
                await c.message.edit_text("📋 <b>Логи</b>" + NL + NL + q(f"<code>{esc(text)}</code>"),
                                          reply_markup=kb_admin_back())
            except Exception as e:
                await c.message.edit_text(f"❌ {esc(str(e))}", reply_markup=kb_admin_back())
            await c.answer(); return

        if d == "adm_t_ucoin":
            ADMIN_WAIT[uid] = "adm_ucoin"
            await c.message.edit_text("💰 <b>Выдать U-Coin</b>" + NL + NL + q("Формат: <code>ID СУММА</code>"), reply_markup=kb_admin_back())
            await c.answer(); return

        if d == "adm_t_bc_users":
            ADMIN_WAIT[uid] = "adm_bc_users"
            await c.message.edit_text("📢 <b>Рассылка юзерам</b>" + NL + NL + q("Отправь текст."), reply_markup=kb_admin_back())
            await c.answer(); return

        if d.startswith("adm_sub_"):
            parts = d.split("_")
            grant_sub(int(parts[2]), int(parts[3]))
            await c.answer(f"✅ +{parts[3]} дн.", show_alert=True); return

        if d.startswith("adm_unsub_"):
            target_id = int(d.split("_")[2])
            _c = db(); _c.execute("UPDATE users SET sub_until=NULL WHERE id=?", (target_id,)); _c.commit(); _c.close()
            await c.answer("🚫 Обнулено", show_alert=True); return

        await c.answer()
    except Exception as e:
        log.exception(f"adm_cb error: {e}")
        try: await c.answer(f"❌ {e}"[:190], show_alert=True)
        except Exception: pass

@dp.message(AdminWait())
async def adm_input(m: Message):
    state = ADMIN_WAIT.get(m.from_user.id)
    text = (m.text or "").strip()

    if state == "adm_find":
        if not text.isdigit():
            await m.answer("❌ Нужен ID числом"); return
        _c = db()
        r = _c.execute("SELECT * FROM users WHERE id=?", (int(text),)).fetchone()
        msgs = _c.execute("SELECT COUNT(*) FROM saved_messages WHERE owner_id=?", (int(text),)).fetchone()[0]
        _c.close()
        ADMIN_WAIT.pop(m.from_user.id, None)
        if not r:
            await m.answer("❌ Не найден"); return
        sub = r["sub_until"][:10] if r["sub_until"] else "—"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [B("💎 +30 дней", f"adm_sub_{r['id']}_30", style="success"),
             B("💎 +7 дней", f"adm_sub_{r['id']}_7", style="success")],
            [B("🎁 +1 день", f"adm_sub_{r['id']}_1", style="primary"),
             B("🚫 Обнулить", f"adm_unsub_{r['id']}", style="danger")],
            [B("← Назад", "adm_main", style="danger")]])
        await m.answer("👤 <b>Юзер</b>" + NL + NL
            + q(f"🆔 <code>{r['id']}</code>" + NL
                + f"📛 {esc(r['full_name'] or '—')}" + NL
                + f"🔗 @{esc(r['username'] or '—')}" + NL
                + f"💰 U-Coin: {r['ucoin']}" + NL
                + f"💎 До: {sub}" + NL
                + f"✉️ Сообщений: {msgs}"), reply_markup=kb)
        return

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
            await m.answer(f"✅ <code>{esc(parts[0].upper())}</code> создан")
        except Exception as e:
            await m.answer(f"❌ {esc(str(e))}")
        _c.close()
        ADMIN_WAIT.pop(m.from_user.id, None); return

    if state == "adm_ucoin":
        parts = text.split()
        if len(parts) != 2 or not parts[0].isdigit() or not parts[1].lstrip("-").isdigit():
            await m.answer("❌ Формат: ID СУММА"); return
        tid, amt = int(parts[0]), int(parts[1])
        get_user(tid)
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
            except Exception:
                failed += 1
            await asyncio.sleep(0.05)
        await m.answer(f"✅ Готово. Отправлено: {sent}. Ошибок: {failed}")
        ADMIN_WAIT.pop(m.from_user.id, None); return

# ============ ИГРЫ (меню) ============
@dp.callback_query(F.data.startswith("g_info_"))
async def game_info(c: CallbackQuery):
    game = c.data[len("g_info_"):]
    info = {
        "ttt": ("❌⭕ <b>Крестики-нолики</b>",
                "Дуэль двух игроков на поле 3×3." + NL + NL
                + "🎁 Победа: <b>+15</b> 💰" + NL + "🤝 Ничья: <b>+5</b> 💰" + NL + "😢 Проигрыш: <b>−5</b> 💰" + NL + NL
                + "Отправь <code>.ttt</code> в личке бота — бот создаст дуэль."),
        "saper": ("💣 <b>Сапёр</b>",
                  "Поле 5×5." + NL + NL + "🎁 Победа: <b>+50</b> 💰" + NL + "💥 Мина: <b>−20</b> 💰" + NL + NL
                  + "Отправь <code>.saper</code> боту в личке."),
        "slot": ("🎰 <b>Слоты</b>",
                 "Три барабана, 6 символов. Ставки от 20 до 200 💰." + NL + NL
                 + "🍒×3 → x3" + NL + "🍋×3 → x4" + NL + "🍇×3 → x6" + NL + "💎×3 → x10" + NL
                 + "⭐×3 → x15" + NL + "7️⃣×3 → x25" + NL + "Пара → x2" + NL + NL
                 + "Отправь <code>.slot</code> боту в личке."),
    }
    if game in info:
        title, desc = info[game]
        await _edit(c, title + NL + NL + q(desc), kb_back("m_games"))
    await c.answer()

@dp.callback_query(F.data == "g_private")
async def g_private(c: CallbackQuery):
    await _edit(c, "👥 <b>Приватная комната</b>" + NL + NL
        + q("Пока в разработке — используй крестики-нолики для дуэли:" + NL + "<code>.ttt</code> в личке бота"),
        kb_back("m_games"))
    await c.answer()

@dp.callback_query(F.data == "g_top")
async def g_top(c: CallbackQuery):
    _c = db()
    rows = _c.execute("SELECT full_name, username, ucoin FROM users ORDER BY ucoin DESC LIMIT 10").fetchall()
    _c.close()
    medals = ["🥇", "🥈", "🥉"] + ["▪️"] * 7
    lines = []
    for i, r in enumerate(rows):
        name = r["full_name"] or r["username"] or "User"
        lines.append(f"{medals[i]} <b>{esc(name[:20])}</b> — {r['ucoin']} 💰")
    await _edit(c, "🏆 <b>Топ игроков</b>" + NL + NL + q(NL.join(lines) if lines else "Пока пусто"), kb_back("m_games"))
    await c.answer()

# ============ BUSINESS ============
@dp.business_connection()
async def on_bc(conn):
    was = user_has_active_connection(conn.user.id)
    get_user(conn.user.id, conn.user.username, conn.user.full_name)
    save_connection(conn)
    ensure_web_token(conn.id)
    c = db()
    if conn.is_enabled:
        c.execute("UPDATE connections SET is_enabled=0 WHERE user_id=? AND id!=?", (conn.user.id, conn.id))
    else:
        c.execute("UPDATE connections SET is_enabled=0 WHERE user_id=?", (conn.user.id,))
    c.commit(); c.close()
    now = bool(conn.is_enabled)
    log.info(f"{'OK' if now else 'OFF'} conn {conn.id} was={was} now={now}")
    if was == now: return
    key = (conn.user.id, now)
    if time.time() - _NOTIFY_TS.get(key, 0) < 15: return
    _NOTIFY_TS[key] = time.time()
    try:
        if now:
            text = ("✅ <b>Спасибо, что выбрали нас</b>" + NL + NL
                + q("Теперь вам доступны все возможности:" + NL + NL
                    + "🗑 Просматривать удалённые" + NL + "✏️ Просматривать изменённые" + NL
                    + "🔔 Уведомления в реальном времени" + NL + "🚀 Уникальные команды") + NL + NL
                + q("Для перехода в меню нажми 🚀 Начать."))
            kb = InlineKeyboardMarkup(inline_keyboard=[[B("🚀 Начать", "m_main", style="success")]])
        else:
            text = ("🚫 <b>Вы отключили " + BRAND + "</b>" + NL + NL
                + q("🥺 Нам очень жаль." + NL + NL + "Теперь недоступны:" + NL
                    + "🗑 Удалённые сообщения" + NL + "✏️ Изменённые сообщения" + NL + "🎥 Отслеживание медиа") + NL + NL
                + q("Если передумаете — нажми «🔘 Подключить»."))
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [B("🔘 Подключить", url="tg://settings/edit", style="success")],
                [B("⭐ Оставить отзыв", url=SUPPORT, style="danger")]])
        await bot.send_message(conn.user_chat_id, text, reply_markup=kb)
    except Exception as e:
        log.warning(f"bc notify: {e}")

# только реально реализованные команды (раньше тут были ttt/saper/slot без обработчика —
# бот удалял такое сообщение и молчал)
KNOWN = {"ping", "id", "info", "help", "love", "flip", "rps", "kawai", "kub",
         "roast", "anim", "type", "title", "split", "перевод", "translit",
         "afk", "unafk", "redeem", "balance", "ttt", "knb", "saper", "slot", "mute", "unmute", "voicemod", "qr", "tr"}

async def biz_send(chat_id, conn_id, text, owner_id=None):
    try:
        await bot.send_message(chat_id=chat_id, text=text, business_connection_id=conn_id)
        return True
    except TelegramAPIError as e:
        log.warning(f"biz_send: {e}")
        if owner_id and time.time() - _RIGHTS_HINT.get(owner_id, 0) > 3600:
            _RIGHTS_HINT[owner_id] = time.time()
            try:
                await bot.send_message(owner_id, "⚠️ <b>Не удалось ответить в чате</b>" + NL + NL
                    + q("Проверь права бота: Настройки → Telegram Business → Чат-боты → "
                        "включи <b>Отвечать на сообщения</b> и <b>Удалять отправленные</b>."))
            except TelegramAPIError: pass
        return False

async def delete_cmd(conn_id, chat_id, message_id):
    key = (chat_id, message_id)
    if len(_BOT_DELETED) > 1000: _BOT_DELETED.clear()
    _BOT_DELETED.add(key)
    try:
        await bot.delete_business_messages(business_connection_id=conn_id, message_ids=[message_id])
    except TelegramAPIError as e:
        _BOT_DELETED.discard(key)
        log.warning(f"delete cmd: {e}")

@dp.business_message()
async def on_bm(message: Message):
    conn_id = message.business_connection_id
    if not conn_id:
        raise SkipHandler
    # сообщения, отправленные самим ботом от имени владельца, за активность владельца не считаем
    if getattr(message, "sender_business_bot", None):
        return
    conn = await ensure_connection(conn_id)
    if not conn: return
    chat_id = message.chat.id
    from_id = message.from_user.id if message.from_user else 0
    owner_id = conn["user_id"]
    text = (message.text or "").strip()

    if from_id == owner_id:
        touch_seen(owner_id)
        if text and text[0] in (".", "/"):
            parts = text[1:].split(maxsplit=1)
            if parts:
                cmd = parts[0].split("@", 1)[0].lower()
                arg = parts[1].strip() if len(parts) > 1 else ""
                if cmd == "adm":
                    if owner_id == ADMIN_ID:
                        ADMIN_WAIT.pop(owner_id, None)
                        await delete_cmd(conn_id, chat_id, message.message_id)
                        try:
                            await bot.send_message(owner_id, "🛡 <b>Админ-панель</b>" + NL + NL + q("Выбери действие 👇"),
                                                   reply_markup=kb_admin())
                        except TelegramAPIError as e:
                            log.warning(f"adm panel: {e}")
                    return
                if cmd in KNOWN:
                    log.info(f"[cmd .{cmd}]")
                    get_user(owner_id)
                    # Сначала выполняем, потом удаляем — если упадёт, сообщение останется
                    try:
                        await biz_dispatch(cmd, arg, message, conn_id, chat_id, owner_id)
                        await delete_cmd(conn_id, chat_id, message.message_id)
                    except Exception as e:
                        log.exception(f"biz_dispatch error: {e}")
                    return
        save_message(conn_id, chat_id, message, owner_id)

        # VoiceMod — если включён и это голосовое
        if message.voice:
            preset = voicemod_get(owner_id, chat_id)
            if preset:
                await _handle_voicemod_message(message, conn_id, chat_id, owner_id, preset)
                raise SkipHandler

        # Автоскачивание медиа владельца
        _mt, _fid = None, None
        if message.photo: _mt, _fid = "photo", message.photo[-1].file_id
        elif message.video: _mt, _fid = "video", message.video.file_id
        elif message.video_note: _mt, _fid = "video_note", message.video_note.file_id
        elif message.voice: _mt, _fid = "voice", message.voice.file_id
        elif message.audio: _mt, _fid = "audio", message.audio.file_id
        elif message.document: _mt, _fid = "document", message.document.file_id
        elif message.sticker: _mt, _fid = "sticker", message.sticker.file_id
        elif message.animation: _mt, _fid = "animation", message.animation.file_id
        if _mt and _fid:
            asyncio.create_task(download_one_media(owner_id, message.message_id, _mt, _fid))

        raise SkipHandler

    # входящее от собеседника
    # Проверяем мут — если активен, удаляем без сохранения
    until = mute_active(owner_id, chat_id)
    if until:
        try:
            await bot.delete_business_messages(business_connection_id=conn_id,
                                                message_ids=[message.message_id])
        except TelegramAPIError as e:
            log.warning(f"mute del: {e}")
        return
    # Не сохраняем, если собеседник написал что-то похожее на команду
    if text and text[0] in (".", "/") and len(text) > 1 and text[1:].split()[0].lower() in KNOWN:
        return
    save_message(conn_id, chat_id, message, owner_id)

    # Автоскачивание медиа в фоне
    if message.media_type if False else False:
        pass
    # Определяем медиа из message
    _mt, _fid = None, None
    if message.photo: _mt, _fid = "photo", message.photo[-1].file_id
    elif message.video: _mt, _fid = "video", message.video.file_id
    elif message.video_note: _mt, _fid = "video_note", message.video_note.file_id
    elif message.voice: _mt, _fid = "voice", message.voice.file_id
    elif message.audio: _mt, _fid = "audio", message.audio.file_id
    elif message.document: _mt, _fid = "document", message.document.file_id
    elif message.sticker: _mt, _fid = "sticker", message.sticker.file_id
    elif message.animation: _mt, _fid = "animation", message.animation.file_id
    if _mt and _fid:
        asyncio.create_task(download_one_media(owner_id, message.message_id, _mt, _fid))

    afk = afk_get(owner_id, chat_id)
    if afk and not is_online(owner_id, minutes=5):
        key = (owner_id, chat_id)
        if time.time() - _AFK_LAST.get(key, 0) >= 300:
            _AFK_LAST[key] = time.time()
            msg_text = esc(afk["message"] or "Сейчас не в сети")
            if has_united_love(owner_id):
                afk_reply = msg_text
            else:
                afk_reply = "🤖 <b>Автоответ</b>" + NL + NL + q(msg_text)
            try:
                await bot.send_message(chat_id=chat_id, text=afk_reply, business_connection_id=conn_id)
            except TelegramAPIError as e:
                log.warning(f"afk reply: {e}")

async def biz_dispatch(cmd, arg, message, conn_id, chat_id, owner_id):
    log.info(f"[dispatch ENTER] cmd={cmd!r} arg={arg!r} chat={chat_id}")
    async def out(text):
        try:
            await bot.send_message(chat_id=chat_id, text=text, business_connection_id=conn_id)
            log.info(f"[out OK] chat={chat_id}")
            return
        except TelegramAPIError as e:
            log.warning(f"[out conn fail] chat={chat_id}: {e}")
        try:
            await bot.send_message(chat_id=chat_id, text=text)
            log.info(f"[out plain OK] chat={chat_id}")
        except TelegramAPIError as e2:
            log.error(f"[out plain fail] chat={chat_id}: {e2}")

    async def private(text):
        try:
            await bot.send_message(owner_id, text)
            log.info(f"[private OK] to={owner_id}")
        except TelegramAPIError as e:
            log.error(f"[private fail] to={owner_id}: {e}")
    if cmd == "voicemod":
        a = (arg or "").strip().lower()
        # --- выключение ---
        if a in ("off", "выкл", "стоп", "stop"):
            voicemod_off(owner_id, chat_id)
            await private("🔇 <b>VoiceMod выключен</b>" + NL + NL
                + q("Твои голосовые снова отправляются как есть."))
            return
        # --- меню / выбор пресета ---
        if a not in VOICE_PRESETS:
            await out(
                "🎤 <b>VoiceMod</b>" + NL + NL
                + q("Меняет голос во всех твоих голосовых в этом чате." + NL + NL
                    + "<b>Пресеты:</b>" + NL + NL.join(
                        f"<code>.voicemod {k}</code> — {VOICE_PRESET_NAMES[k]}"
                        for k in sorted(VOICE_PRESETS.keys(), key=int)
                    ) + NL + NL
                    + "<code>.voicemod off</code> — выключить") + NL + NL
                + "<i>⚡ После включения просто пиши голосовые — они автоматически заменятся.</i>")
            return
        # --- включение ---
        voicemod_set(owner_id, chat_id, a)
        await private(
            "🎤 <b>VoiceMod включён</b>" + NL + NL
            + q(f"Пресет: <b>{VOICE_PRESET_NAMES[a]}</b>" + NL + NL
                + "Теперь все твои голосовые будут автоматически заменяться с изменённым голосом." + NL + NL
                + "<i>Выключить:</i> <code>.voicemod off</code>"))
        return



    if cmd == "mute":
        a = (arg or "").strip()
        minutes = None
        if a.isdigit():
            minutes = int(a)
            if minutes < 1 or minutes > 10080:
                await out("❌ <b>Время от 1 до 10080 минут</b>"); return
        if minutes is None:
            await out(
                "🔇 <b>Мут</b>" + NL + NL
                + q("Установи мут собеседнику на N минут." + NL + NL
                    + "<b>Как использовать:</b>" + NL
                    + "<code>.mute 15</code> — мут на 15 минут" + NL
                    + "<code>.mute 60</code> — мут на час" + NL + NL
                    + "⚡ <i>Пока мут активен — все сообщения собеседника автоматически удаляются.</i>" + NL
                    + "🔊 Снять досрочно: <code>.unmute</code>"))
            return
        mute_set(owner_id, chat_id, minutes)
        word = "минуту" if minutes == 1 else ("минуты" if minutes in (2,3,4) else "минут")
        await out(
            "🔇 <b>Мут активирован</b>" + NL + NL
            + q(f"👤 Собеседник не сможет писать <b>{minutes}</b> {word}." + NL + NL
                + "⚡ <i>Все его сообщения будут автоматически удаляться.</i>" + NL
                + "🔊 Снять: <code>.unmute</code>"))
        return

    if cmd == "unmute":
        if not mute_get(owner_id, chat_id):
            await out("❌ " + q("Сейчас нет активного мута.")); return
        mute_off(owner_id, chat_id)
        await out("🔊 <b>Мут снят</b>" + NL + NL + q("Собеседник снова может писать."))
        return
    if cmd == "ping":
        await out("🏓 <b>Понг!</b> Бот онлайн.")
        return

    if cmd == "qr":
        a = (arg or "").strip()
        sub = a.split(maxsplit=1)
        mode = sub[0].lower() if sub else ""
        rest = sub[1].strip() if len(sub) > 1 else ""

        if mode == "read":
            if not message.reply_to_message or not message.reply_to_message.photo:
                await out("❌ " + q("Ответь на фото с QR-кодом командой <code>.qr read</code>"))
                return
            tg_file = await bot.get_file(message.reply_to_message.photo[-1].file_id)
            tmp = f"/tmp/qr_read_{message.message_id}.png"
            await bot.download_file(tg_file.file_path, tmp)
            result = _read_qr_from_image(tmp)
            try: os.remove(tmp)
            except: pass
            if result:
                await out("🔍 <b>QR прочитан</b>" + NL + NL + q(f"<code>{esc(result)}</code>"))
            else:
                await out("❌ " + q("QR-код не распознан."))
            return

        if mode == "wifi":
            parts = rest.split(maxsplit=1)
            if len(parts) < 2:
                await out("❌ " + q("Формат: <code>.qr wifi ИмяСети Пароль</code>"))
                return
            ssid, password = parts[0], parts[1]
            try:
                path = _make_wifi_qr(ssid, password)
                await bot.send_photo(chat_id=chat_id, photo=FSInputFile(path),
                    caption="📶 <b>Wi-Fi QR</b>" + NL + NL
                        + q(f"Сеть: <b>{esc(ssid)}</b>" + NL + "Наведи камеру телефона — подключится автоматически."),
                    business_connection_id=conn_id)
                try: os.remove(path)
                except: pass
            except Exception as e:
                log.warning(f"qr wifi: {e}")
                await out("❌ " + q(f"Ошибка: {e}"))
            return

        if mode == "vcard":
            parts = rest.split(maxsplit=1)
            if len(parts) < 2:
                await out("❌ " + q("Формат: <code>.qr vcard Имя +79991234567</code>"))
                return
            name, phone = parts[0], parts[1]
            try:
                path = _make_vcard_qr(name, phone)
                await bot.send_photo(chat_id=chat_id, photo=FSInputFile(path),
                    caption="📇 <b>vCard QR</b>" + NL + NL
                        + q(f"Имя: <b>{esc(name)}</b>" + NL + f"Телефон: <b>{esc(phone)}</b>"),
                    business_connection_id=conn_id)
                try: os.remove(path)
                except: pass
            except Exception as e:
                log.warning(f"qr vcard: {e}")
                await out("❌ " + q(f"Ошибка: {e}"))
            return

        if a:
            url = f"https://api.qrserver.com/v1/create-qr-code/?size=300x300&data={a}"
            try:
                await bot.send_photo(chat_id=chat_id, photo=url,
                    caption="📱 QR-код", business_connection_id=conn_id)
            except TelegramAPIError:
                await out("❌ " + q("Не удалось сгенерировать"))
            return

        await out(
            "📱 <b>QR-коды</b>" + NL + NL
            + q("Генерирует QR-коды разных типов и читает их с фото." + NL + NL
                + "<b>Как использовать:</b>" + NL
                + "<code>.qr текст</code> — обычный QR" + NL
                + "<code>.qr wifi ИмяСеть Пароль</code> — для подключения к Wi-Fi" + NL
                + "<code>.qr vcard Имя Телефон</code> — визитка с контактом" + NL
                + "<code>.qr read</code> — прочитать QR (ответом на фото)") + NL
            + q("⚡ <i>Wi-Fi QR — телефон подключится к сети просто наведя камеру.</i>" + NL
                + "📇 <i>vCard QR — сохранит контакт в телефон одним касанием.</i>" + NL
                + "🔍 <i>Чтение — отправь фото с QR и ответь командой.</i>"))
        return

    if cmd == "tr":
        a = (arg or "").strip()
        target_lang = "ru"
        text = ""
        if message.reply_to_message:
            text = message.reply_to_message.text or message.reply_to_message.caption or ""
            if a:
                target_lang = a.lower()
        else:
            parts = a.split(maxsplit=1)
            if len(parts) == 2 and len(parts[0]) <= 5:
                target_lang, text = parts[0], parts[1]
            else:
                text = a

        if not text:
            await out(
                "🌐 <b>Перевод</b>" + NL + NL
                + q("Переводит текст между языками (автоопределение исходного)." + NL + NL
                    + "<b>Как использовать:</b>" + NL
                    + "<code>.tr привет</code> — перевести на русский" + NL
                    + "<code>.tr en привет</code> — на английский" + NL
                    + "Ответь на сообщение и напиши <code>.tr</code>" + NL
                    + "Ответь на сообщение и напиши <code>.tr en</code>") + NL
                + q("⚡ <i>Коды языков:</i> <code>ru, en, uk, de, fr, es, tr, ar, zh, ja, ko, pl, it</code>" + NL
                    + "📝 <i>Работает по ответу на любое сообщение.</i>"))
            return

        try:
            translated = GoogleTranslator(source="auto", target=target_lang).translate(text)
        except Exception as e:
            log.warning(f"translate: {e}")
            await out("❌ " + q(f"Ошибка перевода: {e}"))
            return

        await out(
            "🌐 <b>Перевод</b>" + NL + NL
            + q("<b>Оригинал:</b>" + NL + f"<i>{esc(text[:200])}</i>")
            + q(f"<b>Перевод ({target_lang}):</b>" + NL + f"<b>{esc(translated)}</b>"))
        return

    if cmd == "id":
        await out("🆔 " + q(f"<b>Чат:</b> <code>{chat_id}</code>" + NL + f"<b>Ты:</b> <code>{owner_id}</code>")); return
    if cmd == "info":
        c = db()
        p = c.execute("SELECT * FROM saved_messages WHERE owner_id=? AND chat_id=? AND from_id!=? ORDER BY id DESC LIMIT 1",
                      (owner_id, chat_id, owner_id)).fetchone()
        c.close()
        if p:
            uname = f" (@{esc(p['from_username'])})" if p["from_username"] else ""
            await out("👤 <b>Собеседник</b>" + NL
                + q(f"Имя: <b>{esc(p['from_name'])}</b>{uname}" + NL + f"ID: <code>{p['from_id']}</code>"))
        else:
            await out("ℹ️ " + q("Нет данных о собеседнике."))
        return
    if cmd == "help":
        await out("✏️ <b>Команды</b>" + NL + NL
            + "<b>📌 Инфо</b>" + NL + q("<code>.ping</code> · <code>.id</code> · <code>.info</code> · <code>.balance</code>") + NL
            + "<b>🎭 Fun</b>" + NL + q("<code>.love .flip .rps .kawai .kub .roast</code>") + NL
            + "<b>📝 Стиль</b>" + NL + q("<code>.anim .type .title .split .перевод</code>") + NL
            + "<b>⏳ AFK</b>" + NL + q("<code>.afk текст</code> · <code>.unafk</code>") + NL
            + "<b>🎟 Промокод</b>" + NL + q("<code>.redeem КОД</code>"))
        return
    if cmd == "balance":
        u = get_user(owner_id)
        left = max(0, GAME_DAILY_LIMIT - game_status_text(owner_id))
        await private("💰 <b>Баланс</b>" + NL + NL
            + q(f"U-Coin: <b>{u['ucoin']}</b>" + NL + f"Наград за игры осталось сегодня: <b>{left}</b>"))
        return
    if cmd == "redeem":
        until, err = redeem_promo(owner_id, arg)
        if err:
            await private(err)
        else:
            await private("✅ <b>Промокод активирован!</b>" + NL + NL + q("United Love до " + until.strftime("%d.%m.%Y")))
        return
    if cmd == "afk":
        text = (arg or "").strip()
        if not text:
            await out("⏳ <b>AFK-режим</b>" + NL + NL
                + q("Установи автоответ, чтобы собеседник знал, что ты не в сети." + NL + NL
                    + "<code>.afk текст</code> — включить" + NL + "<code>.unafk</code> — выключить" + NL + NL
                    + "⚡ <i>Сработает только когда ты офлайн</i> (5+ мин без активности)."))
            return
        afk_set(owner_id, chat_id, text)
        await private("⏳ <b>AFK активирован</b>" + NL + NL
            + q("Автоответ: <i>" + esc(text) + "</i>" + NL + NL
                + "⚡ Сработает, только когда ты офлайн." + NL + "<i>Выключить:</i> <code>.unafk</code>"))
        return
    if cmd == "unafk":
        afk_off(owner_id, chat_id)
        await private("✅ <b>AFK отключён</b>")
        return
    if cmd == "love":
        pct = random.randint(1, 100)
        bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
        _, suffix = try_game_reward(owner_id, "love", 3)
        await out("❤️ <b>Совместимость</b>" + NL + NL + q(f"{bar}" + NL + f"<b>{pct}%</b>") + suffix); return
    if cmd == "flip":
        r = random.choice(["Орёл", "Решка"])
        _, suffix = try_game_reward(owner_id, "flip", 2)
        await out("🪙 " + q(f"<b>{r}</b>") + suffix); return
    if cmd == "rps":
        a = arg.lower()
        if a not in ("камень", "ножницы", "бумага"):
            await out("❌ " + q("Использование: <code>.rps камень|ножницы|бумага</code>")); return
        b = random.choice(["камень", "ножницы", "бумага"])
        beats = {"камень": "ножницы", "ножницы": "бумага", "бумага": "камень"}
        if a == b: res, base = "Ничья 🤝", 3
        elif beats[a] == b: res, base = "Победа 🎉", 8
        else: res, base = "Проигрыш 🤖", 0
        _, suffix = try_game_reward(owner_id, "rps", base)
        await out("🎮 <b>КНБ</b>" + NL + NL
            + q(f"<b>Ты:</b> {a}" + NL + f"<b>Бот:</b> {b}" + NL + NL + f"<b>{res}</b>") + suffix)
        return
    if cmd == "kawai":
        r = random.choice(["(≧◡≦)", "(づ｡◕‿‿◕｡)づ", "(っ˘ω˘ς)", "(*/ω＼*)"])
        _, suffix = try_game_reward(owner_id, "kawai", 1)
        await out("✨ " + q(f"<b>{esc(r)}</b>") + suffix); return
    if cmd == "kub":
        n = random.randint(1, 6)
        _, suffix = try_game_reward(owner_id, "kub", n)
        await out("🎲 " + q(f"<b>{n}</b>") + suffix); return
    if cmd == "roast":
        r = random.choice([
            "Ты как WiFi без пароля — все хотят, но толку мало 😏",
            "Ты как капча — вроде нужен, но раздражаешь 😄",
            "Ты как баг в проде — все знают, но никто не трогает 🐛"])
        _, suffix = try_game_reward(owner_id, "roast", 2)
        await out("🎭 " + q(r) + suffix); return
    if cmd == "anim":
        l, r = random.choice([("★", "★"), ("『", "』"), ("⚡", "⚡"), ("◤", "◢"), ("✧", "✧")])
        await out("✨ " + q(f"<b>{l} {esc(arg or 'текст')} {r}</b>")); return
    if cmd == "type":
        a = (arg or "").strip().lower()
        # остановка без аргумента
        if not a:
            old_task = TYPE_TASKS.pop((owner_id, chat_id), None)
            if old_task and old_task.get("task"):
                old_task["task"].cancel()
            await private("⏹ " + q("<b>Имитация остановлена</b>"))
            return
        # маппинг
        if a not in TYPE_ACTIONS:
            await private(
                "📟 <b>.type</b>" + NL + NL
                + q("Имитация того, что ты пишешь / отправляешь." + NL
                    + "<i>Собеседник видит статус, но ничего не получает.</i>") + NL + NL
                + q("<b>Варианты:</b>" + NL
                    + "<code>.type печатает</code> — печатает" + NL
                    + "<code>.type кружок</code> — записывает кружок" + NL
                    + "<code>.type видео</code> — записывает видео" + NL
                    + "<code>.type голосовое</code> — записывает голосовое" + NL
                    + "<code>.type фото</code> — отправляет фото" + NL
                    + "<code>.type файл</code> — отправляет файл" + NL
                    + "<code>.type стикер</code> — выбирает стикер" + NL
                    + "<code>.type геолокация</code> — отправляет геолокацию" + NL + NL
                    + "<code>.type</code> — остановить"))
            return
        action = TYPE_ACTIONS[a]
        # отменяем предыдущую
        old_task = TYPE_TASKS.pop((owner_id, chat_id), None)
        if old_task and old_task.get("task"):
            old_task["task"].cancel()
        task = asyncio.create_task(_type_loop(owner_id, chat_id, conn_id, action))
        TYPE_TASKS[(owner_id, chat_id)] = {"task": task, "action": action}
        await private(
            "📟 <b>Имитация включена</b>" + NL + NL
            + q(f"Тип: <b>{esc(a)}</b>" + NL + NL
                + "Собеседник видит статус, но ничего не получает." + NL
                + "<i>Остановить:</i> <code>.type</code>"))
        return
        l33t = str.maketrans({"a": "4", "e": "3", "i": "1", "o": "0", "s": "5", "t": "7",
                              "A": "4", "E": "3", "I": "1", "O": "0", "S": "5", "T": "7"})
        await out("📝 " + q(f"<code>{esc(arg.translate(l33t))}</code>")); return
    if cmd == "title":
        t = esc(arg or BRAND); line = "═" * (len(t) + 4)
        await out(f"<pre>╔{line}╗" + NL + f"║  {t}  ║" + NL + f"╚{line}╝</pre>"); return
    if cmd == "split":
        if not arg:
            await out("❌ " + q("Использование: <code>.split сл1 сл2</code>")); return
        await out("📋 " + q(NL.join(f"▫️ {esc(p)}" for p in arg.split()))); return
    if cmd in ("перевод", "translit"):
        if not arg:
            await out("❌ " + q("Использование: <code>.перевод текст</code>")); return
        tr = {"а":"a","б":"b","в":"v","г":"g","д":"d","е":"e","ё":"e","ж":"zh","з":"z","и":"i","й":"y","к":"k","л":"l","м":"m","н":"n","о":"o","п":"p","р":"r","с":"s","т":"t","у":"u","ф":"f","х":"h","ц":"c","ч":"ch","ш":"sh","щ":"sch","ъ":"","ы":"y","ь":"","э":"e","ю":"yu","я":"ya"}
        res = []
        for ch in arg:
            mp = tr.get(ch.lower(), ch)
            if ch.isupper() and mp: mp = mp[0].upper() + mp[1:]
            res.append(mp)
        await out("🔤 " + q(f"<code>{esc(''.join(res))}</code>")); return

# ============ УВЕДОМЛЕНИЯ ОБ УДАЛЕНИИ / ПРАВКЕ ============
async def send_notice(chat_id, rich_html, plain_html, kb):
    if SendRichMessage and InputRichMessage:
        try:
            await bot(SendRichMessage(chat_id=chat_id,
                                      rich_message=InputRichMessage(html=rich_html), reply_markup=kb))
            return
        except Exception as e:
            log.warning(f"send_rich failed: {e}")
    try:
        await bot.send_message(chat_id, plain_html, reply_markup=kb)
    except TelegramAPIError as e:
        log.warning(f"notice with kb failed: {e}")
        try: await bot.send_message(chat_id, plain_html)     # без кнопок
        except TelegramAPIError as e2: log.warning(f"notice failed: {e2}")

async def resend_media(chat_id, s):
    mt, fid = s["media_type"], s["file_id"]
    if not mt or not fid: return
    try:
        await getattr(bot, "send_" + mt)(chat_id, **{mt: fid})
    except Exception as e:
        log.warning(f"resend media: {e}")

@dp.edited_business_message()
async def on_edited(message: Message):
    conn_id = message.business_connection_id
    if not conn_id: return
    conn = await ensure_connection(conn_id)
    if not conn: return
    owner_id = conn["user_id"]
    if not user_notify_on(owner_id): return
    if message.from_user and message.from_user.id == owner_id and not NOTIFY_OWN: return
    old = get_message(owner_id, message.chat.id, message.message_id)
    if not old: return
    old_text = old["text"] or ""
    new_text = message.text or message.caption or ""
    if old_text == new_text: return
    name = esc(old["from_name"] or "?")
    uname = old["from_username"]
    uname_txt = f" (@{esc(uname)})" if uname else ""
    o, n = esc(clip(old_text)), esc(clip(new_text))

    rich_html = ("<h2>✏️ Изменённое сообщение</h2>"
        + f"От <b>{name}</b>{uname_txt}" + NL + NL
        + "<details open><summary><b>Прошлое сообщение</b></summary>" + f"<p>{o}</p></details>" + NL + NL
        + "<details open><summary><b>Новое сообщение</b></summary>" + f"<p>{n}</p></details>")
    plain_html = ("✏️ <b>Изменённое сообщение</b>" + NL
        + f"От <b>{name}</b>{uname_txt}" + NL + NL
        + "<b>Прошлое:</b>" + q(o) + "<b>Новое:</b>" + q(n))
    await send_notice(conn["user_chat_id"], rich_html, plain_html, kb_peer(uname, archive_url(conn_id)))
    c = db(); c.execute("UPDATE saved_messages SET text=? WHERE id=?", (new_text, old["id"])); c.commit(); c.close()

@dp.deleted_business_messages()
async def on_deleted(event):
    conn_id = event.business_connection_id
    if not conn_id: return
    conn = await ensure_connection(conn_id)
    if not conn: return
    owner_id = conn["user_id"]
    log.info(f"[del event] chat={event.chat.id} mids={event.message_ids} owner={owner_id} notify={user_notify_on(owner_id)}")
    if not user_notify_on(owner_id): return
    # Если voicemod активен для этого чата — не уведомляем о удалениях голосовых
    vm_active = voicemod_get(owner_id, event.chat.id)

    for mid in event.message_ids:
        key = (event.chat.id, mid)
        if key in _BOT_DELETED:            # это бот сам удалил команду — не уведомляем
            _BOT_DELETED.discard(key); continue
        s = get_message(owner_id, event.chat.id, mid)
        log.info(f"[del lookup] mid={mid} found={bool(s)}")

        # voicemod активен — свои голосовые не показываем как удалённые
        if vm_active and s and s["from_id"] == owner_id and s["media_type"] == "voice":
            log.info(f"[del skip vm] mid={mid}")
            continue
        # Заодно пропускаем случай, когда сообщения нет в БД, но это голосовое владельца
        if vm_active:
            log.info(f"[del skip vm-blind] mid={mid}")
            continue

        if s:
            if s["from_id"] == owner_id and not NOTIFY_OWN:
                log.info(f"[del skip own] mid={mid}")
                continue
            name = esc(s["from_name"] or "?")
            uname = s["from_username"]
            uname_txt = f" (@{esc(uname)})" if uname else ""
            body = esc(clip(s["text"] or "—"))
        else:
            name, uname_txt, body, uname = "Неизвестно", "", "текст не сохранён", None

        rich_html = ("<h2>⚡ Удалённое сообщение</h2>"
            + f"От <b>{name}</b>{uname_txt}" + NL + NL
            + "<details open><summary><b>Текст сообщения</b></summary>" + f"<p>{body}</p></details>")
        plain_html = ("⚡ <b>Удалённое сообщение</b>" + NL
            + f"От <b>{name}</b>{uname_txt}" + NL + NL + q(body))
        await send_notice(conn["user_chat_id"], rich_html, plain_html, kb_peer(uname, archive_url(conn_id)))
        if s and s["media_type"]:
            await resend_media(conn["user_chat_id"], s)

# ============ SUPPORT: callback'и вне меню ============
@dp.callback_query(F.data == "sup_new")
async def sup_new_cb(c: CallbackQuery):
    if c.message.business_connection_id:
        await c.answer(); return
    SUPPORT_WAIT.add(c.from_user.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[[B("← Назад", "m_support", style="danger")]])
    try: await c.message.delete()
    except: pass
    await bot.send_message(c.from_user.id,
        "✍️ <b>Написать в поддержку</b>" + NL + NL
        + q("Отправь сообщение в этот чат — оно уйдёт в поддержку." + NL + NL
            + "📎 Можно приложить фото, голосовое, видео." + NL + NL
            + "<i>Ответ придёт сюда же.</i>"),
        reply_markup=kb)
    await c.answer()

@dp.callback_query(F.data == "sup_my")
async def sup_my_cb(c: CallbackQuery):
    if c.message.business_connection_id:
        await c.answer(); return
    u = get_user(c.from_user.id, c.from_user.username, c.from_user.full_name)
    _c = db()
    rows = _c.execute("""SELECT id, status, created, last_msg FROM support_threads
                         WHERE user_id=? ORDER BY id DESC LIMIT 10""", (c.from_user.id,)).fetchall()
    _c.close()
    if not rows:
        text = q("У тебя пока нет обращений.")
    else:
        lines = []
        for r in rows:
            icon = "🟢" if r["status"] == "open" else "⚪"
            lines.append(f"{icon} <b>#{r['id']}</b> · {r['status']} · {(r['last_msg'] or '')[:40]}")
        text = q(NL.join(lines))
    kb = InlineKeyboardMarkup(inline_keyboard=[[B("← Назад", "m_support", style="danger")]])
    try: await c.message.delete()
    except: pass
    await bot.send_message(c.from_user.id,
        "📋 <b>Мои тикеты</b>" + NL + NL + text,
        reply_markup=kb)
    await c.answer()

# ============ SUPPORT: юзер пишет ----------
@dp.message(F.chat.type == "private", F.text, ~F.text.startswith((".", "/")))
async def support_catch(m: Message):
    if not m.from_user: return
    if m.from_user.id == ADMIN_ID: return
    uid = m.from_user.id
    if uid in AFK_WAIT or uid in GREET_WAIT: return
    has_open = support_find_any_thread_by_user(uid)
    if uid not in SUPPORT_WAIT and not has_open: return

    get_user(uid, m.from_user.username, m.from_user.full_name)
    t = support_thread_get_or_create(uid)
    support_save_msg(t["id"], 0, m.text)
    SUPPORT_WAIT.discard(uid)

    header, kb = _support_notify_admin(m, t, m.text)
    try:
        sent = await bot.send_message(ADMIN_ID, header, reply_markup=kb)
        support_link(sent.message_id, t["id"], uid)
    except TelegramAPIError as e:
        log.warning(f"support send admin: {e}")

    await m.answer("✅ <b>Сообщение отправлено в поддержку</b>" + NL + NL
        + q(f"Тикет #<b>{t['id']}</b>" + NL + "Ответ придёт в этот чат."))

@dp.message(F.chat.type == "private", F.photo | F.voice | F.video | F.document)
async def support_catch_media(m: Message):
    if not m.from_user or m.from_user.id == ADMIN_ID: return
    uid = m.from_user.id
    if uid in AFK_WAIT or uid in GREET_WAIT: return
    has_open = support_find_any_thread_by_user(uid)
    if uid not in SUPPORT_WAIT and not has_open: return

    mt, fid = None, None
    if m.photo: mt, fid = "photo", m.photo[-1].file_id
    elif m.voice: mt, fid = "voice", m.voice.file_id
    elif m.video: mt, fid = "video", m.video.file_id
    elif m.document: mt, fid = "document", m.document.file_id
    if not mt: return
    cap = m.caption or ""

    get_user(uid, m.from_user.username, m.from_user.full_name)
    t = support_thread_get_or_create(uid)
    support_save_msg(t["id"], 0, cap, mt, fid)
    SUPPORT_WAIT.discard(uid)

    header, kb = _support_notify_admin(m, t, cap)
    try:
        sent = await bot.send_message(ADMIN_ID, header, reply_markup=kb)
        support_link(sent.message_id, t["id"], uid)
        fwd = await m.forward(ADMIN_ID)
        support_link(fwd.message_id, t["id"], uid)
    except TelegramAPIError as e:
        log.warning(f"support media: {e}")

    await m.answer("✅ Отправлено в поддержку.")

# ============ SUPPORT: ответ админа реплаем ============
@dp.message(F.chat.id == ADMIN_ID, F.reply_to_message, F.text | F.photo | F.voice | F.video | F.document)
async def adm_support_reply(m: Message):
    r = support_find_thread(m.reply_to_message.message_id)
    if not r: return
    thread_id, user_id = r["thread_id"], r["user_id"]

    text = m.text or m.caption or ""
    try:
        if m.photo:
            await bot.send_photo(user_id, m.photo[-1].file_id, caption=text or None)
        elif m.voice:
            await bot.send_voice(user_id, m.voice.file_id, caption=text or None)
        elif m.video:
            await bot.send_video(user_id, m.video.file_id, caption=text or None)
        elif m.document:
            await bot.send_document(user_id, m.document.file_id, caption=text or None)
        else:
            await bot.send_message(user_id, text)
    except TelegramAPIError as e:
        await m.reply("❌ " + q(f"Не доставлено: {esc(str(e))[:150]}"))
        return

    mt, fid = None, None
    if m.photo: mt, fid = "photo", m.photo[-1].file_id
    elif m.voice: mt, fid = "voice", m.voice.file_id
    elif m.video: mt, fid = "video", m.video.file_id
    elif m.document: mt, fid = "document", m.document.file_id
    support_save_msg(thread_id, 1, text, mt, fid)

    try: await m.reply("✅ " + q(f"Отправлено юзеру"))
    except TelegramAPIError: pass

@dp.callback_query(F.data.startswith("sup_close_"))
async def sup_close_btn(c: CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        await c.answer("Нет доступа", show_alert=True); return
    thread_id = int(c.data.split("_")[2])
    support_close(thread_id)
    await c.answer("🔒 Тикет закрыт", show_alert=True)
    try: await c.message.edit_reply_markup(reply_markup=None)
    except: pass

@dp.error()
async def on_error(event):
    log.error(f"Error: {event.exception}", exc_info=event.exception)
    return True

async def mutes_watcher():
    """Проверяет истёкшие муты и уведомляет."""
    while True:
        try:
            await asyncio.sleep(20)
            c = db()
            rows = c.execute("SELECT * FROM mutes").fetchall()
            c.close()
            for r in rows:
                try:
                    until = datetime.fromisoformat(r["until_ts"])
                    if until.tzinfo is None: until = until.replace(tzinfo=timezone.utc)
                except Exception:
                    continue
                if until > now_utc():
                    continue
                # мут истёк — уведомляем и удаляем
                owner_id = r["owner_id"]; chat_id = r["chat_id"]
                c2 = db()
                conn_row = c2.execute(
                    "SELECT id FROM connections WHERE user_id=? AND is_enabled=1 ORDER BY created DESC LIMIT 1",
                    (owner_id,)).fetchone()
                c2.close()
                if conn_row:
                    try:
                        await bot.send_message(
                            chat_id=chat_id,
                            text=("🔊 <b>Время мута вышло</b>" + NL + NL
                                + q("Собеседник снова может писать в чат.")),
                            business_connection_id=conn_row["id"])
                    except TelegramAPIError as e:
                        log.warning(f"mute expire notify: {e}")
                c3 = db()
                c3.execute("DELETE FROM mutes WHERE owner_id=? AND chat_id=?", (owner_id, chat_id))
                c3.commit(); c3.close()
        except Exception as e:
            log.warning(f"mutes_watcher: {e}")


async def download_one_media(owner_id, message_id, media_type, file_id):
    """Скачивает одно медиа в u/<token>/media/."""
    if not media_type or not file_id: return False
    c = db()
    r = c.execute("SELECT web_token FROM connections WHERE user_id=? AND web_token IS NOT NULL LIMIT 1", (owner_id,)).fetchone()
    c.close()
    if not r or not r["web_token"]: return False
    tok = r["web_token"]
    ext = MEDIA_EXT.get(media_type, "bin")
    media_dir = os.path.join(ARCHIVE_DIR, "u", tok, "media")
    os.makedirs(media_dir, exist_ok=True)
    target = os.path.join(media_dir, f"{message_id}.{ext}")
    # Уже есть?
    if os.path.exists(target): return True
    if any(f.startswith(f"{message_id}.") for f in os.listdir(media_dir)): return True
    try:
        tg_file = await bot.get_file(file_id)
        if tg_file.file_size and tg_file.file_size > MEDIA_MAX_SIZE:
            open(target + ".skip", "w").close()
            return False
        await bot.download_file(tg_file.file_path, destination=target)
        log.info(f"[media ↓] {message_id} → {ext}")
        return True
    except TelegramAPIError as e:
        log.warning(f"[media fail] {message_id}: {e}")
        try: open(target + ".skip", "w").close()
        except: pass
        return False



# ============ СКАЧИВАНИЕ МЕДИА ДЛЯ АРХИВА ============
MEDIA_EXT = {
    "photo": "jpg", "video": "mp4", "video_note": "mp4", "voice": "ogg",
    "audio": "mp3", "document": "bin", "sticker": "webp", "animation": "mp4",
}
MEDIA_MAX_SIZE = 50 * 1024 * 1024  # 50 МБ — чтобы не раздувать репо

async def download_missing_media():
    """Скачивает медиа, которых нет локально."""
    c = db()
    rows = c.execute("""
        SELECT owner_id, message_id, media_type, file_id, connection_id
        FROM saved_messages
        WHERE media_type IS NOT NULL AND file_id IS NOT NULL
        ORDER BY id DESC LIMIT 500
    """).fetchall()
    # Токены владельцев
    tokens = {}
    for t in c.execute("SELECT DISTINCT user_id, web_token FROM connections WHERE web_token IS NOT NULL").fetchall():
        tokens[t["user_id"]] = t["web_token"]
    c.close()

    downloaded = 0
    for r in rows:
        tok = tokens.get(r["owner_id"])
        if not tok: continue
        ext = MEDIA_EXT.get(r["media_type"], "bin")
        media_dir = os.path.join(ARCHIVE_DIR, "u", tok, "media")
        os.makedirs(media_dir, exist_ok=True)
        target = os.path.join(media_dir, f"{r['message_id']}.{ext}")
        # Уже скачано?
        if os.path.exists(target): continue
        # Может с другим расширением?
        found = False
        for f in os.listdir(media_dir):
            if f.startswith(f"{r['message_id']}."):
                found = True; break
        if found: continue

        # Скачиваем
        try:
            tg_file = await bot.get_file(r["file_id"])
            if tg_file.file_size and tg_file.file_size > MEDIA_MAX_SIZE:
                log.info(f"[media skip] {r['message_id']} — слишком большой ({tg_file.file_size} байт)")
                # Ставим заглушку-маркер, чтобы не пытаться повторно
                open(target + ".skip", "w").close()
                continue
            await bot.download_file(tg_file.file_path, destination=target)
            downloaded += 1
            if downloaded % 10 == 0:
                log.info(f"[media] скачано {downloaded}...")
        except TelegramAPIError as e:
            log.warning(f"[media fail] {r['message_id']}: {e}")
            # маркер, чтобы не пытаться снова в этом цикле
            try: open(target + ".skip", "w").close()
            except: pass
    if downloaded:
        log.info(f"[media] всего скачано за прогон: {downloaded}")
    return downloaded


# ============ АВТООБНОВЛЕНИЕ ВЕБ-АРХИВА ============
import subprocess

ARCHIVE_DIR = os.path.dirname(os.path.abspath(__file__))
ARCHIVE_INTERVAL = 180  # 3 минуты

async def _archive_generate():
    """Скачивает медиа, генерирует страницы, пушит."""
    # 0) Скачиваем медиа
    try:
        await download_missing_media()
    except Exception as e:
        log.warning(f"download media: {e}")

    # 1) генерация страниц
    proc = await asyncio.create_subprocess_exec(
        sys.executable, os.path.join(ARCHIVE_DIR, "generate_site.py"),
        cwd=ARCHIVE_DIR,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    if proc.returncode != 0:
        log.warning(f"generate_site failed: {err.decode()[-300:]}")
        return
    log.info(f"archive: {out.decode().strip().split(chr(10))[-1]}")

    # 2) git add -A
    async def _git(*args, cwd=None):
        p = await asyncio.create_subprocess_exec(
            "git", *args, cwd=cwd or ARCHIVE_DIR,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        o, e = await p.communicate()
        return p.returncode, o.decode().strip(), e.decode().strip()

    await _git("add", "-A")
    rc, out, err = await _git("status", "--porcelain")
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    if out:
        await _git("commit", "-m", f"auto: {ts}")
        rc, out, err = await _git("push", "origin", "main")
        if rc != 0:
            log.warning(f"git push origin failed: {err[-200:]}")

    # пушим ТОЛЬКО папку u/ и index.html в публичный репо
    import shutil
    web_dir = os.path.join(ARCHIVE_DIR, "_web_deploy")
    if os.path.isdir(web_dir):
        shutil.rmtree(web_dir)
    os.makedirs(web_dir, exist_ok=True)
    src_u = os.path.join(ARCHIVE_DIR, "u")
    if os.path.isdir(src_u):
        shutil.copytree(src_u, os.path.join(web_dir, "u"))
    src_idx = os.path.join(ARCHIVE_DIR, "index.html")
    if os.path.exists(src_idx):
        shutil.copy(src_idx, os.path.join(web_dir, "index.html"))
    open(os.path.join(web_dir, ".nojekyll"), "w").close()

    if not os.path.isdir(os.path.join(web_dir, ".git")):
        await _git("init", cwd=web_dir)
        await _git("checkout", "-b", "main", cwd=web_dir)
        await _git("remote", "add", "origin",
                   "https://github.com/cfmz/united-dialog-web.git", cwd=web_dir)
    await _git("add", "-A", cwd=web_dir)
    rc, out, err = await _git("status", "--porcelain", cwd=web_dir)
    if out:
        await _git("commit", "-m", f"auto: {ts}", cwd=web_dir)
        rc, out, err = await _git("push", "-u", "origin", "main", "--force", cwd=web_dir)
        if rc != 0:
            log.warning(f"web push failed: {err[-200:]}")
        else:
            log.info("archive: pushed to web")
    else:
        log.info("archive: web — нечего пушить")

async def archive_updater():
    await asyncio.sleep(5)  # первый прогон через 5 сек после старта
    while True:
        try:
            await _archive_generate()
        except Exception as e:
            log.warning(f"archive updater: {e}")
        await asyncio.sleep(ARCHIVE_INTERVAL)

async def main():
    init_db()
    asyncio.create_task(mutes_watcher())
    asyncio.create_task(archive_updater())
    await bot.delete_webhook(drop_pending_updates=False)
    log.info(f"{BRAND} запущен.")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    asyncio.run(main())
