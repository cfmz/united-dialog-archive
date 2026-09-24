import random, json, uuid
from datetime import datetime, timezone, timedelta
from aiogram import Router, F, Bot
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, Message
from aiogram.exceptions import TelegramAPIError

router = Router()

def _private(m: Message) -> bool:
    """Игры только в личке с ботом (не в бизнес-чате)."""
    return m.chat.type == "private" and not m.business_connection_id

# Перебиваем Message-хендлеры кастомным фильтром

NL = chr(10)

# ---------- Помощник для БД (импорт из bot.py) ----------
def _db():
    import sqlite3
    c = sqlite3.connect("/home/maya/united/united_dialog.db")
    c.row_factory = sqlite3.Row
    return c

def _now():
    return datetime.now(timezone.utc)

def _init_games_table():
    c = _db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS games(
        id TEXT PRIMARY KEY,
        type TEXT,
        host_id INTEGER,
        guest_id INTEGER,
        state TEXT,
        created TEXT,
        finished INTEGER DEFAULT 0
    );
    """)
    c.commit(); c.close()

_init_games_table()

def _save_game(gid, gtype, host_id, guest_id, state):
    c = _db()
    c.execute("""INSERT INTO games(id,type,host_id,guest_id,state,created,finished)
                 VALUES(?,?,?,?,?,?,0)
                 ON CONFLICT(id) DO UPDATE SET state=excluded.state""",
              (gid, gtype, host_id, guest_id, json.dumps(state), _now().isoformat()))
    c.commit(); c.close()

def _get_game(gid):
    c = _db()
    r = c.execute("SELECT * FROM games WHERE id=?", (gid,)).fetchone()
    c.close()
    if not r: return None
    r = dict(r)
    r["state"] = json.loads(r["state"])
    return r

def _finish_game(gid):
    c = _db()
    c.execute("UPDATE games SET finished=1 WHERE id=?", (gid,))
    c.commit(); c.close()

def _add_ucoin(uid, amount, reason):
    c = _db()
    c.execute("UPDATE users SET ucoin = MAX(0, ucoin + ?) WHERE id=?", (amount, uid))
    c.execute("INSERT INTO transactions(user_id,amount,reason,created) VALUES(?,?,?,?)",
              (uid, amount, reason, _now().isoformat()))
    c.commit()
    r = c.execute("SELECT ucoin FROM users WHERE id=?", (uid,)).fetchone()
    c.close()
    return r["ucoin"] if r else 0

def B(text, cb, style=None):
    kw = {"text": text, "callback_data": cb}
    if style:
        try: return InlineKeyboardButton(**kw, style=style)
        except TypeError: pass
    return InlineKeyboardButton(**kw)

async def _safe_edit(c, text, kb):
    try:
        await c.message.edit_text(text, reply_markup=kb)
    except TelegramAPIError as e:
        if "message is not modified" not in str(e):
            try: await c.message.edit_reply_markup(reply_markup=kb)
            except: pass

# ================================================================
#                     КРЕСТИКИ-НОЛИКИ
# ================================================================

TTT_WIN = [
    [0,1,2],[3,4,5],[6,7,8],
    [0,3,6],[1,4,7],[2,5,8],
    [0,4,8],[2,4,6]
]

def _ttt_check(b):
    for a,c,d in TTT_WIN:
        if b[a] and b[a] == b[c] == b[d]:
            return b[a], [a,c,d]
    if all(b): return "draw", []
    return None, []

def _ttt_kb(gid, state, viewer=None, finished=False):
    b = state["board"]; turn = state["turn"]
    host = state["host"]; guest = state["guest"]
    win_sym = state.get("win_sym"); win_cells = state.get("win_cells", [])
    rows = []
    for r in range(3):
        row = []
        for c in range(3):
            i = r*3 + c
            v = b[i]
            if v == "X": label = "❌"
            elif v == "O": label = "⭕"
            else: label = "⬜"
            if i in win_cells: label = "🟩" if v=="X" else "🟦"
            cb = f"ttt_{gid}_{i}"
            row.append(B(label, cb, style="primary" if not v else "success"))
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)

def _ttt_render(state, host_name, guest_name):
    b = state["board"]; turn = state["turn"]
    if state.get("finished"):
        w = state.get("winner")
        if w == "draw": head = "🤝 <b>Ничья!</b>"
        elif w == "X": head = "❌ <b>Победил " + host_name + "!</b>"
        else: head = "⭕ <b>Победил " + guest_name + "!</b>"
    else:
        t_name = host_name if turn == "X" else guest_name
        head = "🎮 <b>Крестики-нолики</b>" + NL + NL + f"Ход: <b>{t_name}</b>"
    return head + NL + NL + _ttt_board_text(b)

def _ttt_board_text(b):
    lines = []
    for r in range(3):
        row = []
        for c in range(3):
            v = b[r*3+c]
            row.append("❌" if v=="X" else ("⭕" if v=="O" else "⬜"))
        lines.append(" ".join(row))
    return "<code>" + NL.join(lines).replace("❌","X").replace("⭕","O").replace("⬜","·") + "</code>"

@router.message(_private, F.text.regexp(r"^\.ttt(?:@\w+)?(?:\s+.*)?$"))
async def ttt_start(m: Message):
    gid = uuid.uuid4().hex[:10]
    state = {
        "board": [""]*9,
        "turn": "X",
        "host": m.from_user.id,
        "guest": None,
        "host_name": m.from_user.full_name,
    }
    _save_game(gid, "ttt", m.from_user.id, None, state)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [B("✋ Присоединиться", f"ttt_join_{gid}", style="success")],
    ])
    await m.answer(
        "🎮 <b>Крестики-нолики</b>" + NL + NL
        + "<blockquote>Игрок <b>" + m.from_user.full_name + "</b> создал дуэль." + NL
        + "Нажми кнопку, чтобы присоединиться!</blockquote>",
        reply_markup=kb)

@router.callback_query(F.data.startswith("ttt_join_"))
async def ttt_join(c: CallbackQuery):
    gid = c.data[len("ttt_join_"):]
    g = _get_game(gid)
    if not g: await c.answer("Игра не найдена", show_alert=True); return
    st = g["state"]
    if st.get("guest"):
        await c.answer("Уже занято", show_alert=True); return
    if c.from_user.id == st["host"]:
        await c.answer("Нельзя играть с собой", show_alert=True); return
    st["guest"] = c.from_user.id
    st["guest_name"] = c.from_user.full_name
    _save_game(gid, "ttt", st["host"], st["guest"], st)

    kb = _ttt_kb(gid, st)
    text = _ttt_render(st, st["host_name"], st["guest_name"])
    await _safe_edit(c, text, kb)
    await c.answer()

@router.callback_query(F.data.startswith("ttt_") & ~F.data.startswith("ttt_join_"))
async def ttt_move(c: CallbackQuery):
    parts = c.data.split("_")
    if len(parts) != 3: return
    gid = parts[1]; idx = int(parts[2])
    g = _get_game(gid)
    if not g: await c.answer("Игра не найдена", show_alert=True); return
    st = g["state"]
    if st.get("finished"):
        await c.answer("Игра окончена", show_alert=True); return
    if not st.get("guest"):
        await c.answer("Ждём соперника", show_alert=True); return
    uid = c.from_user.id
    host, guest = st["host"], st["guest"]
    turn = st["turn"]
    expected = host if turn == "X" else guest
    if uid != expected:
        await c.answer("Сейчас не твой ход", show_alert=True); return
    if st["board"][idx]:
        await c.answer("Клетка занята", show_alert=True); return

    st["board"][idx] = turn
    winner, cells = _ttt_check(st["board"])
    if winner:
        st["finished"] = True
        st["winner"] = winner
        st["win_cells"] = cells
        if winner == "X":
            _add_ucoin(host, 15, "ttt_win")
            _add_ucoin(guest, -5, "ttt_lose")
        elif winner == "O":
            _add_ucoin(guest, 15, "ttt_win")
            _add_ucoin(host, -5, "ttt_lose")
        else:
            _add_ucoin(host, 5, "ttt_draw")
            _add_ucoin(guest, 5, "ttt_draw")
        _finish_game(gid)
    else:
        st["turn"] = "O" if turn == "X" else "X"

    _save_game(gid, "ttt", host, guest, st)
    kb = _ttt_kb(gid, st)
    text = _ttt_render(st, st["host_name"], st["guest_name"])
    if st.get("finished"):
        text += NL + NL + "<i>Награды: победитель +15 💰, проигравший −5 💰, ничья +5 💰</i>"
    await _safe_edit(c, text, kb)
    await c.answer()

# ================================================================
#                           САПЁР
# ================================================================

def _saper_new():
    size = 5
    mines = set()
    while len(mines) < 5:
        mines.add(random.randint(0, size*size-1))
    state = {
        "size": size,
        "mines": list(mines),
        "opened": [],
        "flags": [],
        "dead": False,
        "won": False,
        "bet": 20,
    }
    return state

def _saper_neighbors(size, i):
    r, c = divmod(i, size)
    out = []
    for dr in (-1,0,1):
        for dc in (-1,0,1):
            if dr == 0 and dc == 0: continue
            nr, nc = r+dr, c+dc
            if 0 <= nr < size and 0 <= nc < size:
                out.append(nr*size + nc)
    return out

def _saper_mines_around(state, i):
    return sum(1 for n in _saper_neighbors(state["size"], i) if n in state["mines"])

def _saper_kb(gid, state):
    size = state["size"]
    rows = []
    for r in range(size):
        row = []
        for c in range(size):
            i = r*size + c
            if i in state["opened"]:
                n = _saper_mines_around(state, i)
                label = "💣" if i in state["mines"] else (str(n) if n > 0 else "⬜")
                cb = f"sap_noop_{gid}"
            elif i in state["flags"]:
                label = "🚩"; cb = f"sap_flag_{gid}_{i}"
            else:
                label = "🔷"; cb = f"sap_open_{gid}_{i}"
            row.append(B(label, cb, style="primary"))
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)

def _saper_render(state, name):
    opened = len([i for i in state["opened"] if i not in state["mines"]])
    total_safe = state["size"]*state["size"] - len(state["mines"])
    head = "💣 <b>Сапёр 5×5</b>"
    if state["dead"]:
        head += NL + "💥 <b>Ты подорвался!</b>"
    elif state["won"]:
        head += NL + "🏆 <b>Победа! Поле очищено</b>"
    else:
        head += NL + NL + f"Безопасных открыто: <b>{opened}/{total_safe}</b>"
    return head + NL + NL + f"<i>Игрок: {name}</i>"

@router.message(_private, F.text.regexp(r"^\.saper(?:@\w+)?$"))
async def saper_start(m: Message):
    gid = uuid.uuid4().hex[:10]
    state = _saper_new()
    _save_game(gid, "saper", m.from_user.id, None, state)
    await m.answer(
        _saper_render(state, m.from_user.full_name) + NL + NL
        + "<i>Нажми на 🔷 чтобы открыть клетку. Ставка: 20 💰. Победа: +50 💰, мина: −20 💰.</i>",
        reply_markup=_saper_kb(gid, state))

@router.callback_query(F.data.startswith("sap_"))
async def saper_cb(c: CallbackQuery):
    parts = c.data.split("_")
    action = parts[1]; gid = parts[2]
    g = _get_game(gid)
    if not g: await c.answer("Не найдено", show_alert=True); return
    if c.from_user.id != g["host_id"]:
        await c.answer("Это не твоя игра", show_alert=True); return
    st = g["state"]
    if st["dead"] or st["won"]:
        await c.answer("Игра окончена", show_alert=True); return

    if action == "noop":
        await c.answer(); return

    if action == "flag":
        i = int(parts[3])
        if i in st["flags"]:
            st["flags"].remove(i)
        elif i not in st["opened"]:
            st["flags"].append(i)
        _save_game(gid, "saper", g["host_id"], None, st)
        await _safe_edit(c, _saper_render(st, c.from_user.full_name) + NL + NL
            + "<i>🔷 открыть · 🚩 пометить · мин всего: 5</i>",
            _saper_kb(gid, st))
        await c.answer(); return

    if action == "open":
        i = int(parts[3])
        if i in st["opened"] or i in st["flags"]:
            await c.answer(); return
        if i in st["mines"]:
            st["dead"] = True
            st["opened"].append(i)
            _add_ucoin(g["host_id"], -st["bet"], "saper_mine")
            _finish_game(gid)
            _save_game(gid, "saper", g["host_id"], None, st)
            await _safe_edit(c, _saper_render(st, c.from_user.full_name) + NL + NL
                + f"<i>−{st['bet']} 💰</i>",
                _saper_kb(gid, st))
            await c.answer("💥 Мина!", show_alert=True); return

        # Открываем клетку + авто-открытие пустых соседних
        stack = [i]
        while stack:
            cur = stack.pop()
            if cur in st["opened"]: continue
            if cur in st["mines"]: continue
            st["opened"].append(cur)
            if _saper_mines_around(st, cur) == 0:
                for n in _saper_neighbors(st["size"], cur):
                    if n not in st["opened"] and n not in st["mines"]:
                        stack.append(n)

        safe_total = st["size"]*st["size"] - len(st["mines"])
        opened_safe = len([x for x in st["opened"] if x not in st["mines"]])
        if opened_safe >= safe_total:
            st["won"] = True
            reward = 50
            _add_ucoin(g["host_id"], reward, "saper_win")
            _finish_game(gid)
            _save_game(gid, "saper", g["host_id"], None, st)
            await _safe_edit(c, _saper_render(st, c.from_user.full_name) + NL + NL
                + f"<i>+{reward} 💰</i>",
                _saper_kb(gid, st))
            await c.answer(f"🏆 Победа! +{reward}", show_alert=True); return

        _save_game(gid, "saper", g["host_id"], None, st)
        await _safe_edit(c, _saper_render(st, c.from_user.full_name) + NL + NL
            + "<i>🔷 открыть · 🚩 пометить · мин всего: 5</i>",
            _saper_kb(gid, st))
        await c.answer(); return

# ================================================================
#                          СЛОТЫ
# ================================================================

SLOT_SYMBOLS = ["🍒", "🍋", "🍇", "💎", "⭐", "7️⃣"]
SLOT_PAYOUT = {
    "🍒": 3, "🍋": 4, "🍇": 6, "💎": 10, "⭐": 15, "7️⃣": 25,
}

def _slot_kb(gid, bet, spinning=False):
    if spinning:
        return InlineKeyboardMarkup(inline_keyboard=[[B("⏳ Крутится...", f"slot_noop_{gid}", style="primary")]])
    return InlineKeyboardMarkup(inline_keyboard=[
        [B(f"🎰 Крутить ({bet} 💰)", f"slot_spin_{gid}", style="success")],
        [B("×2", f"slot_bet_{gid}_40", style="primary"),
         B("×5", f"slot_bet_{gid}_100", style="primary"),
         B("×10", f"slot_bet_{gid}_200", style="primary")],
        [B("❌ Выйти", f"slot_exit_{gid}", style="danger")],
    ])

@router.message(_private, F.text.regexp(r"^\.slot(?:@\w+)?$"))
async def slot_start(m: Message):
    gid = uuid.uuid4().hex[:10]
    bet = 20
    state = {"bet": bet, "reels": ["❔","❔","❔"], "finished": False}
    _save_game(gid, "slot", m.from_user.id, None, state)
    await m.answer(
        "🎰 <b>Слоты</b>" + NL + NL
        + "<code>[ ❔ | ❔ | ❔ ]</code>" + NL + NL
        + "<i>Ставка: 20 💰 · 3 одинаковых = x3-x25</i>" + NL
        + "<i>Изменяй ставку кнопками ×2/×5/×10</i>",
        reply_markup=_slot_kb(gid, bet))

@router.callback_query(F.data.startswith("slot_"))
async def slot_cb(c: CallbackQuery):
    parts = c.data.split("_")
    action = parts[1]; gid = parts[2]
    g = _get_game(gid)
    if not g: await c.answer("Не найдено", show_alert=True); return
    if c.from_user.id != g["host_id"]:
        await c.answer("Это не твоя игра", show_alert=True); return
    st = g["state"]
    if st.get("finished"):
        await c.answer("Игра закрыта", show_alert=True); return

    if action == "noop":
        await c.answer(); return

    if action == "bet":
        new_bet = int(parts[3])
        st["bet"] = new_bet
        _save_game(gid, "slot", g["host_id"], None, st)
        await _safe_edit(c,
            "🎰 <b>Слоты</b>" + NL + NL
            + "<code>[ ❔ | ❔ | ❔ ]</code>" + NL + NL
            + f"<i>Ставка: {new_bet} 💰</i>",
            _slot_kb(gid, new_bet))
        await c.answer(f"Ставка: {new_bet}"); return

    if action == "exit":
        st["finished"] = True
        _finish_game(gid)
        _save_game(gid, "slot", g["host_id"], None, st)
        await _safe_edit(c, "🎰 Слоты закрыты. Заходи ещё!", None)
        await c.answer(); return

    if action == "spin":
        bet = st["bet"]
        # Проверка баланса
        c_db = _db()
        r = c_db.execute("SELECT ucoin FROM users WHERE id=?", (g["host_id"],)).fetchone()
        bal = r["ucoin"] if r else 0
        c_db.close()
        if bal < bet:
            await c.answer(f"❌ Нужно {bet} 💰, у тебя {bal}", show_alert=True); return

        _add_ucoin(g["host_id"], -bet, "slot_bet")

        # Анимация: показываем "крутится"
        await _safe_edit(c,
            "🎰 <b>Слоты</b>" + NL + NL
            + "<code>[ 🎲 | 🎲 | 🎲 ]</code>" + NL + NL
            + "<i>Крутится...</i>",
            _slot_kb(gid, bet, spinning=True))

        import asyncio as _asyncio
        await _asyncio.sleep(1.0)

        reels = [random.choice(SLOT_SYMBOLS) for _ in range(3)]
        st["reels"] = reels

        # Подсчёт выигрыша
        if reels[0] == reels[1] == reels[2]:
            mult = SLOT_PAYOUT[reels[0]]
            won = bet * mult
            result = f"🎉 <b>ДЖЕКПОТ! x{mult}</b>"
        elif reels[0] == reels[1] or reels[1] == reels[2] or reels[0] == reels[2]:
            won = bet * 2
            result = "✨ <b>Пара! x2</b>"
        else:
            won = 0
            result = "😢 <b>Мимо</b>"

        if won > 0:
            _add_ucoin(g["host_id"], won, "slot_win")
        profit = won - bet
        sign = "+" if profit >= 0 else ""

        text = (
            "🎰 <b>Слоты</b>" + NL + NL
            + f"<code>[ {reels[0]} | {reels[1]} | {reels[2]} ]</code>" + NL + NL
            + result + NL
            + f"Ставка: {bet} 💰 · Выигрыш: {won} 💰 · <b>{sign}{profit} 💰</b>"
        )
        _save_game(gid, "slot", g["host_id"], None, st)
        await _safe_edit(c, text, _slot_kb(gid, bet))
        await c.answer()

# ================================================================
#                     ЗАКРЫТИЕ (заглушка)
# ================================================================

@router.callback_query(F.data == "game_noop")
async def game_noop(c: CallbackQuery):
    await c.answer()

# ================================================================
#                     КНБ С БОТОМ (5 раундов)
# ================================================================

KNB_ICONS = {"r": "🪨 Камень", "s": "✂️ Ножницы", "p": "📄 Бумага"}
KNB_ORDER = ["r", "s", "p"]
KNB_BEATS = {"r": "s", "s": "p", "p": "r"}

def _knb_kb(gid, disabled=False):
    if disabled:
        return InlineKeyboardMarkup(inline_keyboard=[[B("⏳ Ход...", "knb_noop", style="primary")]])
    return InlineKeyboardMarkup(inline_keyboard=[
        [B("🪨 Камень", f"knb_r_{gid}", style="primary"),
         B("✂️ Ножницы", f"knb_s_{gid}", style="primary"),
         B("📄 Бумага", f"knb_p_{gid}", style="primary")],
        [B("🏳️ Сдаться", f"knb_exit_{gid}", style="danger")],
    ])

def _knb_render(state, name):
    st = state
    my = st.get("my", 0); bot = st.get("bot", 0); rnd = st.get("round", 0)
    last = st.get("last", "")
    head = "🎲 <b>КНБ с ботом</b> — раунд " + str(min(rnd+1, 5)) + "/5" + NL + NL
    head += f"👤 <b>{name}</b>: {my}" + NL
    head += f"🤖 <b>Бот</b>: {bot}"
    if last: head += NL + NL + last
    return head

@router.callback_query(F.data == "game_knb_new")
async def knb_new(c: CallbackQuery):
    gid = uuid.uuid4().hex[:10]
    state = {"round": 0, "my": 0, "bot": 0, "last": "", "finished": False}
    _save_game(gid, "knb", c.from_user.id, None, state)
    await _safe_edit(c,
        _knb_render(state, c.from_user.full_name) + NL + NL + "<i>Выбирай ход 👇</i>",
        _knb_kb(gid))
    await c.answer()

@router.callback_query(F.data.startswith("knb_"))
async def knb_cb(c: CallbackQuery):
    parts = c.data.split("_")
    action = parts[1]
    if action == "noop": await c.answer(); return
    gid = parts[2] if len(parts) > 2 else None
    if not gid: await c.answer(); return
    g = _get_game(gid)
    if not g: await c.answer("Игра не найдена", show_alert=True); return
    if c.from_user.id != g["host_id"]:
        await c.answer("Не твоя игра", show_alert=True); return
    st = g["state"]
    if st.get("finished"): await c.answer("Игра окончена", show_alert=True); return

    if action == "exit":
        st["finished"] = True
        _finish_game(gid)
        _save_game(gid, "knb", g["host_id"], None, st)
        await _safe_edit(c, "🎲 Игра завершена. Спасибо!", None)
        await c.answer(); return

    my = action
    bot_move = random.choice(KNB_ORDER)
    if my == bot_move:
        st["last"] = f"🤝 Ничья! ({KNB_ICONS[my]})"
    elif KNB_BEATS[my] == bot_move:
        st["my"] += 1
        st["last"] = f"🎉 Ты выиграл раунд! {KNB_ICONS[my]} vs {KNB_ICONS[bot_move]}"
    else:
        st["bot"] += 1
        st["last"] = f"😢 Бот выиграл. {KNB_ICONS[my]} vs {KNB_ICONS[bot_move]}"
    st["round"] += 1

    if st["round"] >= 5:
        st["finished"] = True
        if st["my"] > st["bot"]:
            reward = 30; verdict = "🏆 <b>ПОБЕДА!</b>"
        elif st["my"] < st["bot"]:
            reward = -10; verdict = "💀 <b>Поражение</b>"
        else:
            reward = 5; verdict = "🤝 <b>Ничья</b>"
        _add_ucoin(g["host_id"], reward, "knb_game")
        _finish_game(gid)
        _save_game(gid, "knb", g["host_id"], None, st)
        sign = "+" if reward >= 0 else ""
        await _safe_edit(c,
            _knb_render(st, c.from_user.full_name) + NL + NL + verdict + NL + f"<i>Баланс: {sign}{reward} 💰</i>",
            InlineKeyboardMarkup(inline_keyboard=[[B("🎲 Играть снова", "game_knb_new", style="success")]]))
        await c.answer(); return

    _save_game(gid, "knb", g["host_id"], None, st)
    await _safe_edit(c,
        _knb_render(st, c.from_user.full_name) + NL + NL + "<i>Выбирай ход 👇</i>",
        _knb_kb(gid))
    await c.answer()
