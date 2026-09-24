"""Генератор HTML-страниц веб-архива. Читает united_dialog.db рядом с файлом."""
import sqlite3, html, json, os
from datetime import datetime
from collections import defaultdict

BASE = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE, "united_dialog.db")
OUT_DIR = os.path.join(BASE, "u")
NL = chr(10)

HTML_TPL = '''<!DOCTYPE html>
<html lang="ru"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>United Dialog — Веб-Архив</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0e1621;color:#fff;height:100vh;overflow:hidden;
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;display:flex}
.sidebar{width:320px;background:#17212b;display:flex;flex-direction:column;border-right:1px solid #101921}
.sbh{padding:12px;border-bottom:1px solid #101921}
.sbh h1{font-size:15px;font-weight:600;color:#64b5f6;margin-bottom:8px}
.search{width:100%;padding:8px 12px;background:#242f3d;border:none;border-radius:20px;
color:#fff;font-size:14px;outline:none}
.search::placeholder{color:#6b7c8e}
.chats{flex:1;overflow-y:auto}
.chat{padding:10px 14px;border-bottom:1px solid #101921;cursor:pointer;display:flex;flex-direction:column;gap:3px}
.chat:hover{background:#202b36}
.chat.active{background:#2b5278}
.cname{font-weight:600;font-size:14px}
.cprev{font-size:12px;color:#7e8f9e;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ctime{font-size:11px;color:#6b7c8e;float:right;font-weight:normal}
.chd{display:flex;justify-content:space-between;align-items:center}
.main{flex:1;display:flex;flex-direction:column;background:#0e1621}
.mh{padding:12px 20px;background:#17212b;border-bottom:1px solid #101921;font-weight:600;font-size:15px}
.msgs{flex:1;overflow-y:auto;padding:20px;display:flex;flex-direction:column;gap:8px}
.msg{max-width:70%;padding:8px 12px;border-radius:12px;background:#182533;
align-self:flex-start;word-wrap:break-word;font-size:14px;line-height:1.4;position:relative}
.msg.own{background:#2b5278;align-self:flex-end}
.meta{font-size:11px;color:#64b5f6;margin-bottom:4px;font-weight:600}
.msg.own .meta{color:#a8c8ec}
.mtime{font-size:10px;color:#6b7c8e;float:right;margin-left:12px;margin-top:4px}
.mmedia{display:inline-block;margin-top:6px;padding:4px 8px;background:#2b3e50;border-radius:6px;font-size:12px}
.empty{flex:1;display:flex;align-items:center;justify-content:center;color:#6b7c8e;font-size:14px}
.back{display:none;color:#64b5f6;cursor:pointer;margin-right:8px}
@media(max-width:700px){.sidebar{width:100%;position:absolute;z-index:10;height:100vh}
.sidebar.hidden{display:none}.back{display:inline-block}}
</style></head><body>
<div class="sidebar" id="sidebar">
<div class="sbh"><h1>United Dialog — Архив</h1>
<input class="search" id="search" placeholder="Поиск по чатам..."></div>
<div class="chats" id="chats"></div></div>
<div class="main">
<div class="mh"><span class="back" id="back">←</span><span id="title">Выберите чат</span></div>
<div class="msgs" id="msgs"><div class="empty">Выберите чат слева</div></div>
</div>
<script>
let DATA=[],cur=null;
async function load(){const r=await fetch("data.json");DATA=await r.json();render(DATA);}
function esc(s){const d=document.createElement("div");d.textContent=s==null?"":s;return d.innerHTML;}
function render(chats){const b=document.getElementById("chats");b.innerHTML="";
if(!chats.length){b.innerHTML='<div style="padding:20px;color:#6b7c8e;text-align:center">Пусто</div>';return;}
chats.forEach(c=>{const el=document.createElement("div");el.className="chat";
el.innerHTML=`<div class="chd"><span class="cname">${esc(c.name)}</span><span class="ctime">${esc(c.last_time)}</span></div>
<div class="cprev">${esc(c.last_text)}</div>`;el.onclick=()=>open(c);b.appendChild(el);});}
function open(c){cur=c;document.getElementById("title").textContent=c.name+(c.username?" (@"+c.username+")":"");
const bx=document.getElementById("msgs");bx.innerHTML="";
c.messages.forEach(m=>{const el=document.createElement("div");el.className="msg"+(m.is_owner?" own":"");
let md="";if(m.media_type){const L={photo:"📷 Фото",video:"🎬 Видео",voice:"🎙 Голосовое",
video_note:"⭕ Кружок",sticker:"🌟 Стикер",document:"📎 Файл",audio:"🎵 Аудио",
animation:"🎞 GIF"};md=`<div class="mmedia">${L[m.media_type]||"❔"}</div>`;}
el.innerHTML=`<div class="meta">${esc(m.from_name)}</div><div>${esc(m.text)}${md}</div>
<div class="mtime">${esc(m.time)}</div>`;bx.appendChild(el);});
bx.scrollTop=999999;document.getElementById("sidebar").classList.add("hidden");}
document.getElementById("search").oninput=e=>{const q=e.target.value.toLowerCase();
render(DATA.filter(c=>c.name.toLowerCase().includes(q)||c.messages.some(m=>(m.text||"").toLowerCase().includes(q))));};
document.getElementById("back").onclick=()=>document.getElementById("sidebar").classList.remove("hidden");
load();
</script></body></html>'''

def group(rows, owner_id):
    chats = defaultdict(list)
    for m in rows:
        chats[m["chat_id"]].append(m)
    out = []
    for cid, msgs in chats.items():
        peer_name, peer_username = "Чат", None
        for m in reversed(msgs):
            if m["from_id"] != owner_id and m["from_name"] and m["from_name"] != "?":
                peer_name = m["from_name"]; peer_username = m["from_username"]; break
        last = msgs[-1]
        try: lt = datetime.fromisoformat(last["created"]).strftime("%d.%m %H:%M")
        except: lt = ""
        out.append({
            "id": cid, "name": peer_name, "username": peer_username,
            "last_time": lt, "last_text": (last["text"] or "")[:60],
            "messages": [{"from_name": m["from_name"] or "?","from_username": m["from_username"],
                "text": m["text"] or "","media_type": m["media_type"],
                "is_owner": m["from_id"] == owner_id,
                "time": (datetime.fromisoformat(m["created"]).strftime("%d.%m.%Y %H:%M") if m["created"] else "")
            } for m in msgs]
        })
    out.sort(key=lambda c: c["last_time"], reverse=True)
    return out

def main():
    if not os.path.exists(DB_FILE):
        print("Нет БД."); return
    c = sqlite3.connect(DB_FILE); c.row_factory = sqlite3.Row
    users = c.execute("SELECT DISTINCT user_id, web_token FROM connections WHERE web_token IS NOT NULL").fetchall()
    if not users:
        print("Нет активных архивов."); c.close(); return
    total = 0
    for u in users:
        uid, tok = u["user_id"], u["web_token"]
        rows = c.execute("SELECT * FROM saved_messages WHERE owner_id=? ORDER BY created ASC", (uid,)).fetchall()
        chats = group(rows, uid)
        d = os.path.join(OUT_DIR, tok)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "index.html"), "w", encoding="utf-8") as f:
            f.write(HTML_TPL)
        with open(os.path.join(d, "data.json"), "w", encoding="utf-8") as f:
            json.dump(chats, f, ensure_ascii=False, indent=2)
        print(f"  u/{tok[:12]}… — {len(chats)} чатов, {len(rows)} сообщений")
        total += 1
    c.close()
    print(f"Готово: {total} архивов")

if __name__ == "__main__":
    main()
