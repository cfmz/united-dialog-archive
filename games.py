"""United Dialog Games — работают и в личке, и в бизнес-чате."""
import random, uuid, os, sqlite3, asyncio
from datetime import datetime, timezone
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, Message
from aiogram.exceptions import TelegramAPIError

router = Router()
NL = chr(10)
_BOT = {}
def set_bot(b): _BOT["bot"] = b

def _db():
    base = os.path.dirname(os.path.abspath(__file__))
    c = sqlite3.connect(os.path.join(base, "united_dialog.db"), timeout=10, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c

def _now(): return datetime.now(timezone.utc)

def _add_ucoin(uid, amount, reason):
    c = _db()
    try:
        if not c.execute("SELECT 1 FROM users WHERE id=?", (uid,)).fetchone():
            c.execute("INSERT INTO users(id,joined,ucoin) VALUES(?,?,0)", (uid, _now().isoformat()))
        c.execute("UPDATE users SET ucoin=MAX(0, ucoin+?) WHERE id=?", (amount, uid))
        c.execute("INSERT INTO transactions(user_id,amount,reason,created) VALUES(?,?,?,?)",
                  (uid, amount, reason, _now().isoformat()))
        c.commit()
        r = c.execute("SELECT ucoin FROM users WHERE id=?", (uid,)).fetchone()
        return r["ucoin"] if r else 0
    finally:
        c.close()

def B(text, cb, style=None):
    kw = {"text": text, "callback_data": cb}
    if style:
        try: return InlineKeyboardButton(**kw, style=style)
        except Exception: pass
    return InlineKeyboardButton(**kw)

def q(t): return f"<blockquote>{t}</blockquote>"

async def _send(chat_id, text, kb=None, conn_id=None):
    """Отправка с business_connection_id если это бизнес-чат."""
    bot = _BOT.get("bot")
    if not bot: return None
    try:
        kw = {"chat_id": chat_id, "text": text, "reply_markup": kb}
        if conn_id: kw["business_connection_id"] = conn_id
        return await bot.send_message(**kw)
    except TelegramAPIError as e:
        print(f"[games send] {e}")
        return None

async def _edit(c, text, kb=None):
    """Редактирование с поддержкой бизнес-сообщений."""
    bot = _BOT.get("bot")
    if not bot: return
    conn_id = getattr(c.message, "business_connection_id", None)
    try:
        kw = {"chat_id": c.message.chat.id, "message_id": c.message.message_id,
              "text": text, "reply_markup": kb}
        if conn_id: kw["business_connection_id"] = conn_id
        await bot.edit_message_text(**kw)
    except TelegramAPIError as e:
        if "not modified" in str(e).lower(): return
        print(f"[games edit] {e}")

# =================== КНБ ===================
KNB = {}
KNB_ICON = {"r": "🪨", "s": "✂️", "p": "📄"}
KNB_BEATS = {"r": "s", "s": "p", "p": "r"}

def _knb_render(st, name):
    h = "🎲 <b>Камень · Ножницы · Бумага</b>" + NL
    h += f"<i>Раунд {min(st['round']+1, 5)} / 5</i>" + NL + NL
    h += q(f"👤 <b>{name}:</b> {st['my']}" + NL + f"🤖 <b>Бот:</b> {st['bot']}")
    if st.get("last"): h += NL + NL + st["last"]
    return h

def _knb_kb(gid):
    return InlineKeyboardMarkup(inline_keyboard=[
        [B("🪨 Камень", f"knb:{gid}:r", "primary"),
         B("✂️ Ножницы", f"knb:{gid}:s", "primary"),
         B("📄 Бумага", f"knb:{gid}:p", "primary")],
        [B("🏳️ Сдаться", f"knb:{gid}:x", "danger")]])

async def knb_start_ctx(chat_id, uid, name, conn_id=None):
    gid = uuid.uuid4().hex[:8]
    KNB[gid] = {"round": 0, "my": 0, "bot": 0, "last": "", "done": False, "host": uid}
    await _send(chat_id, _knb_render(KNB[gid], name) + NL + NL + "<i>Выбирай ход 👇</i>",
                _knb_kb(gid), conn_id)

@router.message(Command("knb"))
async def knb_start(m: Message):
    if m.business_connection_id: return
    await knb_start_ctx(m.chat.id, m.from_user.id, m.from_user.full_name)

@router.callback_query(F.data.startswith("knb:"))
async def knb_cb(c: CallbackQuery):
    _, gid, act = c.data.split(":")
    st = KNB.get(gid)
    if not st: await c.answer("Игра устарела", show_alert=True); return
    if c.from_user.id != st["host"]: await c.answer("Не твоя игра", show_alert=True); return
    if st["done"]: await c.answer("Игра окончена", show_alert=True); return
    if act == "x":
        st["done"] = True
        await _edit(c, "🏳️ <b>Ты сдался.</b>" + NL + NL + q("Спасибо за игру!"))
        await c.answer(); return
    bm = random.choice(["r", "s", "p"])
    if act == bm: st["last"] = f"🤝 <b>Ничья</b> · {KNB_ICON[act]} vs {KNB_ICON[bm]}"
    elif KNB_BEATS[act] == bm:
        st["my"] += 1; st["last"] = f"🎉 <b>Ты взял раунд</b> · {KNB_ICON[act]} vs {KNB_ICON[bm]}"
    else:
        st["bot"] += 1; st["last"] = f"😢 <b>Бот взял раунд</b> · {KNB_ICON[act]} vs {KNB_ICON[bm]}"
    st["round"] += 1
    if st["round"] >= 5:
        st["done"] = True
        if st["my"] > st["bot"]: reward, verdict = 30, "🏆 <b>ПОБЕДА!</b>"
        elif st["my"] < st["bot"]: reward, verdict = -10, "💀 <b>Поражение</b>"
        else: reward, verdict = 5, "🤝 <b>Ничья</b>"
        _add_ucoin(st["host"], reward, "knb")
        sign = "+" if reward >= 0 else ""
        await _edit(c, _knb_render(st, c.from_user.full_name) + NL + NL
            + q(f"{verdict}" + NL + f"💰 <b>{sign}{reward}</b> U-Coin"),
            InlineKeyboardMarkup(inline_keyboard=[[B("🎲 Ещё раз", "knb_new", "success")]]))
        await c.answer(); return
    await _edit(c, _knb_render(st, c.from_user.full_name) + NL + NL + "<i>Выбирай ход 👇</i>", _knb_kb(gid))
    await c.answer()

@router.callback_query(F.data == "knb_new")
async def knb_new(c: CallbackQuery):
    gid = uuid.uuid4().hex[:8]
    KNB[gid] = {"round": 0, "my": 0, "bot": 0, "last": "", "done": False, "host": c.from_user.id}
    await _edit(c, _knb_render(KNB[gid], c.from_user.full_name) + NL + NL + "<i>Выбирай ход 👇</i>", _knb_kb(gid))
    await c.answer()

# =================== TTT ===================
TTT = {}
WIN = [(0,1,2),(3,4,5),(6,7,8),(0,3,6),(1,4,7),(2,5,8),(0,4,8),(2,4,6)]

def _ttt_win(b):
    for a, c2, d in WIN:
        if b[a] and b[a] == b[c2] == b[d]: return b[a]
    return "draw" if all(b) else None

def _ttt_kb(gid, st):
    rows = []
    for r in range(3):
        row = []
        for c in range(3):
            i = r*3 + c; v = st["board"][i]
            label = "❌" if v == "X" else ("⭕" if v == "O" else "⬜")
            row.append(B(label, f"ttt:{gid}:{i}", "primary" if not v else "success"))
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)

def _ttt_text(st):
    a = st.get("a_name") or "Игрок 1"
    b = st.get("b_name") or "Игрок 2"
    if st.get("done"):
        w = st.get("winner")
        if w == "draw": head = "🤝 <b>Ничья!</b>"
        elif w == "X": head = f"🏆 <b>Победил {a}</b> (❌)"
        else: head = f"🏆 <b>Победил {b}</b> (⭕)"
    else:
        cur = st.get("a_name") if st["turn"] == "X" else st.get("b_name")
        if not cur:
            head = "⏳ <b>Ожидание второго игрока...</b>"
        else:
            head = f"Ход: <b>{cur}</b> ({'❌' if st['turn']=='X' else '⭕'})" + NL + \
                   f"Поставьте {'❌' if st['turn']=='X' else '⭕'} на любое свободное поле."
    return ("🎲 <b>Крестики-нолики</b>" + NL + NL + q(head))

def _ttt_new(uid, name):
    gid = uuid.uuid4().hex[:8]
    TTT[gid] = {
        "board": [""]*9, "turn": "X", "done": False,
        "a_id": uid, "a_name": name,
        "b_id": None, "b_name": None,
        "chat_id": None, "conn_id": None,
    }
    return gid

async def ttt_start_ctx(chat_id, uid, name, conn_id=None, to_chat=None):
    gid = _ttt_new(uid, name)
    TTT[gid]["chat_id"] = to_chat or chat_id
    TTT[gid]["conn_id"] = conn_id
    await _send(chat_id, _ttt_text(TTT[gid]), _ttt_kb(gid, TTT[gid]), conn_id)

@router.message(Command("ttt"))
async def ttt_start(m: Message):
    if m.business_connection_id: return
    await ttt_start_ctx(m.chat.id, m.from_user.id, m.from_user.full_name)

@router.callback_query(F.data.startswith("ttt:"))
async def ttt_move(c: CallbackQuery):
    _, gid, idx_s = c.data.split(":")
    idx = int(idx_s); st = TTT.get(gid)
    if not st: await c.answer("Игра устарела", show_alert=True); return
    if st["done"]: await c.answer("Игра окончена"); return
    # Определяем игрока по порядку
    if c.from_user.id == st["a_id"]:
        role = "X"
    elif c.from_user.id == st["b_id"]:
        role = "O"
    elif st["b_id"] is None:
        st["b_id"] = c.from_user.id
        st["b_name"] = c.from_user.full_name
        role = "O"
    else:
        await c.answer("Это не твоя игра", show_alert=True); return
    if st["turn"] != role:
        await c.answer("Сейчас не твой ход", show_alert=True); return
    if st["board"][idx]:
        await c.answer("Клетка занята"); return
    st["board"][idx] = role
    w = _ttt_win(st["board"])
    if w:
        st["done"] = True; st["winner"] = w
        if w == "X": _add_ucoin(st["a_id"], 15, "ttt_win"); _add_ucoin(st["b_id"], -5, "ttt_lose")
        elif w == "O": _add_ucoin(st["b_id"], 15, "ttt_win"); _add_ucoin(st["a_id"], -5, "ttt_lose")
        else: _add_ucoin(st["a_id"], 5, "ttt_draw"); _add_ucoin(st["b_id"], 5, "ttt_draw")
    else:
        st["turn"] = "O" if st["turn"] == "X" else "X"
    await _edit(c, _ttt_text(st), _ttt_kb(gid, st))
    await c.answer()

# =================== САПЁР ===================
SAP = {}
SAP_SIZE = 5; SAP_MINES = 5; SAP_REWARD = 50

def _sap_nb(i):
    r, c = divmod(i, SAP_SIZE); out = []
    for dr in (-1,0,1):
        for dc in (-1,0,1):
            if dr == 0 and dc == 0: continue
            nr, nc = r+dr, c+dc
            if 0 <= nr < SAP_SIZE and 0 <= nc < SAP_SIZE: out.append(nr*SAP_SIZE+nc)
    return out

def _sap_around(st, i): return sum(1 for n in _sap_nb(i) if n in st["mines"])

def _sap_new():
    mines = set()
    while len(mines) < SAP_MINES: mines.add(random.randint(0, SAP_SIZE*SAP_SIZE-1))
    return {"mines": list(mines), "opened": [], "flags": [], "dead": False, "won": False}

def _sap_kb(gid, st):
    rows = []
    for r in range(SAP_SIZE):
        row = []
        for c in range(SAP_SIZE):
            i = r*SAP_SIZE + c
            if i in st["opened"]:
                if i in st["mines"]: label = "💥"
                else:
                    n = _sap_around(st, i); label = str(n) if n else "·"
                row.append(B(label, f"sap_noop:{gid}", "primary"))
            elif i in st["flags"]: row.append(B("🚩", f"sap_flag:{gid}:{i}", "danger"))
            else: row.append(B("🔷", f"sap_open:{gid}:{i}", "primary"))
        rows.append(row)
    if st["dead"] or st["won"]:
        rows.append([B("🔁 Новая игра", f"sap_new:{st['host']}", "success")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def _sap_text(st):
    total = SAP_SIZE*SAP_SIZE - SAP_MINES
    opened = len([i for i in st["opened"] if i not in st["mines"]])
    h = "💣 <b>Сапёр 5×5</b>"
    if st["dead"]: h += NL + "💥 <b>Взрыв!</b>"
    elif st["won"]: h += NL + f"🏆 <b>Победа! +{SAP_REWARD} 💰</b>"
    else: h += NL + f"<i>Открыто: {opened} / {total}</i>"
    return h + NL + NL + q("🔷 открыть · 🚩 пометить")

@router.message(Command("saper"))
async def sap_start(m: Message):
    if m.business_connection_id: return
    gid = uuid.uuid4().hex[:8]
    st = _sap_new(); st["host"] = m.from_user.id
    SAP[gid] = st
    await m.answer(_sap_text(st), reply_markup=_sap_kb(gid, st))

@router.callback_query(F.data.startswith("sap_"))
async def sap_cb(c: CallbackQuery):
    parts = c.data.split(":"); action = parts[0]; gid = parts[1]
    st = SAP.get(gid)
    if not st: await c.answer("Устарело", show_alert=True); return
    if c.from_user.id != st["host"]: await c.answer("Не твоя игра", show_alert=True); return
    if st["dead"] or st["won"]: await c.answer("Игра окончена"); return
    if action == "sap_noop": await c.answer(); return
    if action == "sap_flag":
        i = int(parts[2])
        if i in st["flags"]: st["flags"].remove(i)
        elif i not in st["opened"]: st["flags"].append(i)
        await _edit(c, _sap_text(st), _sap_kb(gid, st)); await c.answer(); return
    if action == "sap_open":
        i = int(parts[2])
        if i in st["opened"] or i in st["flags"]: await c.answer(); return
        if i in st["mines"]:
            st["dead"] = True; st["opened"].append(i)
            _add_ucoin(st["host"], -20, "saper_mine")
            await _edit(c, _sap_text(st), _sap_kb(gid, st))
            await c.answer("💥 Мина! −20", show_alert=True); return
        stack = [i]
        while stack:
            cur = stack.pop()
            if cur in st["opened"] or cur in st["mines"]: continue
            st["opened"].append(cur)
            if _sap_around(st, cur) == 0:
                for n in _sap_nb(cur):
                    if n not in st["opened"] and n not in st["mines"]: stack.append(n)
        total = SAP_SIZE*SAP_SIZE - SAP_MINES
        opened = len([x for x in st["opened"] if x not in st["mines"]])
        if opened >= total:
            st["won"] = True; _add_ucoin(st["host"], SAP_REWARD, "saper_win")
        await _edit(c, _sap_text(st), _sap_kb(gid, st)); await c.answer(); return

@router.callback_query(F.data.startswith("sap_new:"))
async def sap_new(c: CallbackQuery):
    if c.from_user.id != int(c.data.split(":")[1]): await c.answer("Только хост", show_alert=True); return
    gid = uuid.uuid4().hex[:8]
    st = _sap_new(); st["host"] = c.from_user.id
    SAP[gid] = st
    await _edit(c, _sap_text(st), _sap_kb(gid, st)); await c.answer()

# =================== СЛОТЫ ===================
SLOT = {}
SYM = ["🍒","🍋","🍇","💎","⭐","7️⃣"]
PAY = {"🍒":3,"🍋":4,"🍇":6,"💎":10,"⭐":15,"7️⃣":25}

def _slot_text(st, anim=False):
    reels = ["🎲","🎲","🎲"] if anim else st["reels"]
    foot = "<i>Крутится...</i>" if anim else (st.get("result") or f"<i>Ставка: {st['bet']} 💰</i>")
    return ("🎰 <b>Слоты</b>" + NL + NL
            + f"<code>[ {reels[0]} | {reels[1]} | {reels[2]} ]</code>" + NL + NL
            + foot + NL + NL + q(f"💰 Баланс: <b>{st['balance']}</b>"))

def _slot_kb(gid, st, anim=False):
    if anim: return InlineKeyboardMarkup(inline_keyboard=[[B("⏳ Крутится...", "slot_noop", "primary")]])
    return InlineKeyboardMarkup(inline_keyboard=[
        [B(f"🎰 Крутить · {st['bet']} 💰", f"slot_spin:{gid}", "success")],
        [B(("✅ " if st["bet"]==20 else "")+"20", f"slot_bet:{gid}:20", "primary"),
         B(("✅ " if st["bet"]==40 else "")+"40", f"slot_bet:{gid}:40", "primary"),
         B(("✅ " if st["bet"]==100 else "")+"100", f"slot_bet:{gid}:100", "primary")],
        [B("❌ Выйти", f"slot_exit:{gid}", "danger")]])

@router.message(Command("slot"))
async def slot_start(m: Message):
    if m.business_connection_id: return
    gid = uuid.uuid4().hex[:8]
    bal = _ucoin(m.from_user.id)
    SLOT[gid] = {"bet": 20, "reels": ["❔","❔","❔"], "host": m.from_user.id,
                 "balance": bal, "result": None, "done": False}
    await m.answer(_slot_text(SLOT[gid]) + NL + NL
        + q("Три одинаковых: 🍒×3 · 🍋×4 · 🍇×6 · 💎×10 · ⭐×15 · 7️⃣×25" + NL + "Пара — x2"),
        reply_markup=_slot_kb(gid, SLOT[gid]))

@router.callback_query(F.data.startswith("slot_"))
async def slot_cb(c: CallbackQuery):
    parts = c.data.split(":"); action = parts[0]
    if action == "slot_noop": await c.answer(); return
    gid = parts[1]; st = SLOT.get(gid)
    if not st: await c.answer("Устарело", show_alert=True); return
    if c.from_user.id != st["host"]: await c.answer("Не твоя игра", show_alert=True); return
    if st["done"]: await c.answer("Закрыто"); return
    if action == "slot_bet":
        st["bet"] = int(parts[2]); st["result"] = None
        await _edit(c, _slot_text(st), _slot_kb(gid, st))
        await c.answer(f"Ставка: {st['bet']}"); return
    if action == "slot_exit":
        st["done"] = True; SLOT.pop(gid, None)
        await _edit(c, "🎰 <b>Слоты закрыты.</b>" + NL + NL + q("Заходи ещё!")); await c.answer(); return
    if action == "slot_spin":
        bal = _ucoin(st["host"])
        if bal < st["bet"]: await c.answer(f"❌ Нужно {st['bet']} 💰, у тебя {bal}", show_alert=True); return
        _add_ucoin(st["host"], -st["bet"], "slot_bet")
        await _edit(c, _slot_text(st, anim=True), _slot_kb(gid, st, anim=True))
        await asyncio.sleep(0.9)
        reels = [random.choice(SYM) for _ in range(3)]
        st["reels"] = reels
        if reels[0] == reels[1] == reels[2]:
            mult = PAY[reels[0]]; won = st["bet"] * mult
            st["result"] = f"🎉 <b>ДЖЕКПОТ · x{mult}</b>"
        elif reels[0] == reels[1] or reels[1] == reels[2] or reels[0] == reels[2]:
            won = st["bet"] * 2; st["result"] = "✨ <b>Пара · x2</b>"
        else:
            won = 0; st["result"] = "😢 <b>Мимо</b>"
        if won: _add_ucoin(st["host"], won, "slot_win")
        st["balance"] = _ucoin(st["host"])
        profit = won - st["bet"]
        st["result"] += NL + f"💰 <b>{'+' if profit>=0 else ''}{profit}</b> U-Coin"
        await _edit(c, _slot_text(st), _slot_kb(gid, st))
        await c.answer(); return
