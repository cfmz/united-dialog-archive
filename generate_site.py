"""Генератор HTML-страниц веб-архива с медиа."""
import sqlite3, html, json, os, base64, hashlib, secrets
from datetime import datetime
from collections import defaultdict

BASE = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE, "united_dialog.db")
OUT_DIR = os.path.join(BASE, "u")
NL = chr(10)
MEDIA_EXT = {
    "photo": "jpg", "video": "mp4", "video_note": "mp4", "voice": "ogg",
    "audio": "mp3", "document": "bin", "sticker": "webp", "animation": "mp4",
}

HTML_TPL = '''<!DOCTYPE html>
<html lang="ru"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>United Dialog — Веб-Архив</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0e1621;color:#fff;height:100vh;overflow:hidden;
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;display:flex}

/* Лок-экран */
#lock{position:fixed;inset:0;background:#0e1621;display:flex;align-items:center;justify-content:center;z-index:100}
#lock .box{background:#17212b;padding:32px 28px;border-radius:16px;max-width:380px;width:90%;text-align:center;border:1px solid #1e2b38}
#lock h2{font-size:20px;margin-bottom:6px;color:#64b5f6}
#lock p{font-size:13px;color:#7e8f9e;margin-bottom:20px}
#lock input{width:100%;padding:12px 14px;background:#0e1621;border:1px solid #2b3e50;border-radius:10px;
color:#fff;font-size:15px;outline:none;margin-bottom:12px;font-family:inherit}
#lock input:focus{border-color:#64b5f6}
#lock button{width:100%;padding:12px;background:#2b5278;color:#fff;border:none;border-radius:10px;
font-size:15px;font-weight:600;cursor:pointer;transition:.15s;font-family:inherit}
#lock button:hover{background:#3a6894}
#lock .err{color:#ff6b6b;font-size:13px;margin-top:10px;min-height:18px}
#lock .hint{font-size:11px;color:#5a6b7a;margin-top:14px}

/* Основной интерфейс */
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
align-self:flex-start;word-wrap:break-word;font-size:14px;line-height:1.4}
.msg.own{background:#2b5278;align-self:flex-end}
.meta{font-size:11px;color:#64b5f6;margin-bottom:4px;font-weight:600}
.msg.own .meta{color:#a8c8ec}
.mtime{font-size:10px;color:#6b7c8e;float:right;margin-left:12px;margin-top:4px}
.mmedia{margin-top:8px;border-radius:8px;overflow:hidden;max-width:100%}
.mmedia img,.mmedia video{max-width:100%;max-height:320px;display:block;border-radius:8px}
.mmedia audio{width:100%;margin-top:6px}
.mmedia a{color:#64b5f6;font-size:12px}
.media-tag{display:inline-block;margin-top:6px;padding:4px 8px;background:#2b3e50;border-radius:6px;font-size:12px}
.empty{flex:1;display:flex;align-items:center;justify-content:center;color:#6b7c8e;font-size:14px}
.back{display:none;color:#64b5f6;cursor:pointer;margin-right:8px}
@media(max-width:700px){.sidebar{width:100%;position:absolute;z-index:10;height:100vh}
.sidebar.hidden{display:none}.back{display:inline-block}}
</style></head><body>

<div id="lock">
<div class="box">
<h2>🔒 United Dialog</h2>
<p>Введи пароль архива</p>
<input type="password" id="pw" placeholder="Пароль" autocomplete="current-password">
<button onclick="tryUnlock()">Открыть</button>
<div class="err" id="err"></div>
<div class="hint">Пароль можно найти в боте: Профиль → 🔒 Пароль архива</div>
</div></div>

<div class="sidebar" id="sidebar">
<div class="sbh"><h1>United Dialog — Архив</h1>
<input class="search" id="search" placeholder="Поиск по чатам..."></div>
<div class="chats" id="chats"></div></div>
<div class="main">
<div class="mh"><span class="back" id="back">←</span><span id="title">Выберите чат</span></div>
<div class="msgs" id="msgs"><div class="empty">Выберите чат слева</div></div>
</div>

<script>
let DATA=[], cur=null;
function esc(s){const d=document.createElement("div");d.textContent=s==null?"":s;return d.innerHTML;}

// --- AES-GCM расшифровка через Web Crypto ---
async function deriveKey(password, salt, iterations){
  const enc = new TextEncoder();
  const baseKey = await crypto.subtle.importKey(
    "raw", enc.encode(password), {name:"PBKDF2"}, false, ["deriveKey"]);
  return crypto.subtle.deriveKey(
    {name:"PBKDF2", salt:salt, iterations:iterations, hash:"SHA-256"},
    baseKey, {name:"AES-GCM", length:256}, false, ["decrypt"]);
}
function b64ToU8(s){const bin=atob(s);const a=new Uint8Array(bin.length);for(let i=0;i<bin.length;i++)a[i]=bin.charCodeAt(i);return a;}

async function decryptJson(blob, password){
  const salt = b64ToU8(blob.salt);
  const iv = b64ToU8(blob.iv);
  const ct = b64ToU8(blob.ct);
  const key = await deriveKey(password, salt, blob.iter);
  const plain = await crypto.subtle.decrypt({name:"AES-GCM", iv:iv}, key, ct);
  return JSON.parse(new TextDecoder().decode(plain));
}

async function tryUnlock(){
  const pw = document.getElementById("pw").value;
  if(!pw){document.getElementById("err").textContent="Введите пароль";return;}
  document.getElementById("err").textContent="Проверка...";
  try{
    const r = await fetch("data.json");
    const blob = await r.json();
    let data;
    if(blob.ct){
      // зашифровано
      data = await decryptJson(blob, pw);
    } else {
      // открытый формат (старые архивы)
      data = blob;
    }
    DATA = data.chats || data;
    localStorage.setItem("ud_pw", pw);
    document.getElementById("lock").style.display="none";
    render(DATA);
  } catch(e){
    document.getElementById("err").textContent="Неверный пароль";
  }
}

// Автозаход, если пароль сохранён
async function autoUnlock(){
  const saved = localStorage.getItem("ud_pw");
  if(saved){
    document.getElementById("pw").value = saved;
    await tryUnlock();
  }
}

function render(chats){const b=document.getElementById("chats");b.innerHTML="";
if(!chats.length){b.innerHTML='<div style="padding:20px;color:#6b7c8e;text-align:center">Пусто</div>';return;}
chats.forEach(c=>{const el=document.createElement("div");el.className="chat";
el.innerHTML=`<div class="chd"><span class="cname">${esc(c.name)}</span><span class="ctime">${esc(c.last_time)}</span></div>
<div class="cprev">${esc(c.last_text)}</div>`;el.onclick=()=>open(c);b.appendChild(el);});}

function mediaHtml(m){
  if(!m.media_url) return "";
  const u=m.media_url, t=m.media_type;
  if(t==="photo") return `<div class="mmedia"><img loading="lazy" src="${u}" alt=""></div>`;
  if(t==="video"||t==="animation"||t==="video_note") return `<div class="mmedia"><video controls preload="metadata" src="${u}"></video></div>`;
  if(t==="voice"||t==="audio") return `<div class="mmedia"><audio controls preload="none" src="${u}"></audio></div>`;
  if(t==="sticker") return `<div class="mmedia"><img loading="lazy" src="${u}" alt="" style="max-height:160px"></div>`;
  return `<div class="mmedia"><a href="${u}" target="_blank">📎 Скачать файл</a></div>`;
}

function open(c){cur=c;document.getElementById("title").textContent=c.name+(c.username?" (@"+c.username+")":"");
const bx=document.getElementById("msgs");bx.innerHTML="";
c.messages.forEach(m=>{const el=document.createElement("div");el.className="msg"+(m.is_owner?" own":"");
let md=mediaHtml(m);
if(m.media_type && !m.media_url){const L={photo:"📷 Фото",video:"🎬 Видео",voice:"🎙 Голосовое",
video_note:"⭕ Кружок",sticker:"🌟 Стикер",document:"📎 Файл",audio:"🎵 Аудио",animation:"🎞 GIF"};
md=`<div class="media-tag">${L[m.media_type]||"❔"}</div>`;}
el.innerHTML=`<div class="meta">${esc(m.from_name)}</div><div>${esc(m.text)}${md}</div>
<div class="mtime">${esc(m.time)}</div>`;bx.appendChild(el);});
bx.scrollTop=999999;document.getElementById("sidebar").classList.add("hidden");}

document.getElementById("search").oninput=e=>{const q=e.target.value.toLowerCase();
render(DATA.filter(c=>c.name.toLowerCase().includes(q)||c.messages.some(m=>(m.text||"").toLowerCase().includes(q))));};
document.getElementById("back").onclick=()=>document.getElementById("sidebar").classList.remove("hidden");

document.getElementById("pw").addEventListener("keydown", e=>{ if(e.key==="Enter") tryUnlock(); });
autoUnlock();
</script></body></html>'''


from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

def _derive_key(password: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=100_000)
    return kdf.derive(password.encode("utf-8"))

def encrypt_data(data: dict, password: str) -> dict:
    """Возвращает зашифрованный блоб: salt, iv, ciphertext, iterations."""
    salt = secrets.token_bytes(16)
    iv = secrets.token_bytes(12)
    key = _derive_key(password, salt)
    aes = AESGCM(key)
    plaintext = json.dumps(data, ensure_ascii=False).encode("utf-8")
    ct = aes.encrypt(iv, plaintext, None)
    return {
        "v": 1,
        "iter": 100_000,
        "salt": base64.b64encode(salt).decode(),
        "iv": base64.b64encode(iv).decode(),
        "ct": base64.b64encode(ct).decode(),
    }

def _media_url(base_url, m):
    """Возвращает относительный путь к медиа, если файл есть на диске."""
    if not m["media_type"] or not m["file_id"]:
        return None
    ext = MEDIA_EXT.get(m["media_type"], "bin")
    mid = m["message_id"]
    # Ищем файл mid.* — расширение может быть любым
    media_dir = os.path.join(base_url, "media")
    if not os.path.isdir(media_dir): return None
    for f in os.listdir(media_dir):
        if f.startswith(f"{mid}.") :
            return f"media/{f}"
    return None

def group(rows, owner_id, token_dir_abs, token_dir_rel):
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
        msgs_out = []
        for m in msgs:
            mu = _media_url(token_dir_abs, m)
            msgs_out.append({
                "from_name": m["from_name"] or "?",
                "from_username": m["from_username"],
                "text": m["text"] or "",
                "media_type": m["media_type"],
                "media_url": mu,
                "is_owner": m["from_id"] == owner_id,
                "time": (datetime.fromisoformat(m["created"]).strftime("%d.%m.%Y %H:%M") if m["created"] else "")
            })
        out.append({
            "id": cid, "name": peer_name, "username": peer_username,
            "last_time": lt, "last_text": (last["text"] or "")[:60],
            "messages": msgs_out,
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
        abs_dir = os.path.join(OUT_DIR, tok)
        os.makedirs(abs_dir, exist_ok=True)
        chats = group(rows, uid, abs_dir, f"u/{tok}")
        with open(os.path.join(abs_dir, "index.html"), "w", encoding="utf-8") as f:
            f.write(HTML_TPL)
        # Шифруем data.json паролем
        pw_row = c.execute("SELECT web_password FROM connections WHERE user_id=? AND web_token=?", (uid, tok)).fetchone()
        password = pw_row["web_password"] if pw_row and pw_row["web_password"] else None
        if password:
            blob = encrypt_data({"chats": chats}, password)
            with open(os.path.join(abs_dir, "data.json"), "w", encoding="utf-8") as f:
                json.dump(blob, f, ensure_ascii=False)
        else:
            # без пароля — пишем как есть (не зашифровано)
            with open(os.path.join(abs_dir, "data.json"), "w", encoding="utf-8") as f:
                json.dump({"chats": chats}, f, ensure_ascii=False, indent=2)
        # Считаем медиа
        media_dir = os.path.join(abs_dir, "media")
        media_cnt = len(os.listdir(media_dir)) if os.path.isdir(media_dir) else 0
        print(f"  u/{tok[:12]}… — {len(chats)} чатов, {len(rows)} сообщ., {media_cnt} медиа")
        total += 1
    c.close()
    print(f"Готово: {total} архивов")

if __name__ == "__main__":
    main()
