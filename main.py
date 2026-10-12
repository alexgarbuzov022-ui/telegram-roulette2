import asyncio, hashlib, hmac, json, os, secrets, sqlite3, time, uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qsl
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, LabeledPrice, PreCheckoutQuery, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, InputMediaPhoto, ReplyKeyboardMarkup, KeyboardButton
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

BASE = Path(__file__).resolve().parent
os.chdir(BASE)
load_dotenv(BASE / ".env")
TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://telegram-roulette2.onrender.com").strip()
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
DB_PATH = BASE / os.getenv("DB_PATH", "roulette.sqlite3")
PRICE = int(os.getenv("SPIN_PRICE_STARS", "50"))
app = FastAPI(title="Telegram Prize Roulette")
bot = Bot(TOKEN) if TOKEN else None
dp = Dispatcher()

if not TOKEN:
    raise SystemExit("Set BOT_TOKEN in Render Environment.")

@contextmanager
def db():
    c = sqlite3.connect(DB_PATH, timeout=20)
    c.row_factory = sqlite3.Row
    try:
        c.execute("PRAGMA foreign_keys=ON")
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
          telegram_id INTEGER PRIMARY KEY, spins INTEGER NOT NULL DEFAULT 0 CHECK(spins>=0),
          points INTEGER NOT NULL DEFAULT 0 CHECK(points>=0), referred_by INTEGER,
          referral_paid INTEGER NOT NULL DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS prizes(
          id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT DEFAULT '', image TEXT DEFAULT '',
          rarity TEXT NOT NULL DEFAULT 'Обычный', weight REAL NOT NULL DEFAULT 1,
          points INTEGER NOT NULL DEFAULT 0, prize_type TEXT NOT NULL DEFAULT 'points',
          delivery_value TEXT DEFAULT '', media_files TEXT NOT NULL DEFAULT '[]', enabled INTEGER NOT NULL DEFAULT 1,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS payments(
          charge_id TEXT PRIMARY KEY, telegram_id INTEGER NOT NULL, total_amount INTEGER NOT NULL,
          currency TEXT NOT NULL, spins_added INTEGER NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS spins(
          id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_id INTEGER NOT NULL, prize_id TEXT NOT NULL,
          prize_label TEXT NOT NULL, rarity TEXT DEFAULT '', image TEXT DEFAULT '',
          points_awarded INTEGER NOT NULL DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS audit(
          id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id INTEGER, action TEXT NOT NULL,
          details TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS orders(
          id TEXT PRIMARY KEY, telegram_id INTEGER NOT NULL, method TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending', details TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS admins(telegram_id INTEGER PRIMARY KEY, added_by INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        """)
        # Migrate existing installations without deleting user data.
        spin_cols = {r["name"] for r in c.execute("PRAGMA table_info(spins)").fetchall()}
        if "prize_type" not in spin_cols:
            c.execute("ALTER TABLE spins ADD COLUMN prize_type TEXT NOT NULL DEFAULT 'points'")
        if "delivery_value" not in spin_cols:
            c.execute("ALTER TABLE spins ADD COLUMN delivery_value TEXT DEFAULT ''")
        if "claimed" not in spin_cols:
            c.execute("ALTER TABLE spins ADD COLUMN claimed INTEGER NOT NULL DEFAULT 0")
        prize_cols = {r["name"] for r in c.execute("PRAGMA table_info(prizes)").fetchall()}
        if "media_files" not in prize_cols:
            c.execute("ALTER TABLE prizes ADD COLUMN media_files TEXT NOT NULL DEFAULT '[]'")
        cur = c.execute("SELECT COUNT(*) n FROM prizes")
        if cur.fetchone()["n"] == 0:
            seeds = [
                ("points5","5 баллов","Небольшой бонус","", "Обычный",45,5,"points",""),
                ("points10","10 баллов","Бонус","", "Редкий",28,10,"points",""),
                ("points25","25 баллов","Крупный бонус","", "Эпический",15,25,"points",""),
                ("points100","100 баллов","Большой бонус","", "Легендарный",5,100,"points",""),
                ("tryagain","Ещё повезёт","Попробуй снова","", "Мифический",7,0,"points","")
            ]
            c.executemany("INSERT INTO prizes(id,name,description,image,rarity,weight,points,prize_type,delivery_value) VALUES(?,?,?,?,?,?,?,?,?)", seeds)

def audit(admin_id, action, details=""):
    with db() as c:
        c.execute("INSERT INTO audit(admin_id,action,details) VALUES(?,?,?)", (admin_id, action, details[:1000]))

def validate_init_data(raw):
    p = dict(parse_qsl(raw, keep_blank_values=True))
    received = p.pop("hash", None)
    if not received: raise ValueError("Missing hash")
    auth = int(p.get("auth_date", "0"))
    if auth <= 0 or time.time() - auth > 3600: raise ValueError("Expired initData")
    check = "\n".join(f"{k}={v}" for k,v in sorted(p.items()))
    key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    expected = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received): raise ValueError("Bad signature")
    return json.loads(p["user"])

def uid_from_header(auth):
    if not auth or not auth.startswith("tma "): raise HTTPException(401, "Открой Mini App из Telegram")
    try: return int(validate_init_data(auth[4:])["id"])
    except Exception: raise HTTPException(401, "Сессия Telegram недействительна. Открой Mini App заново.")

def is_admin(uid):
    if uid in ADMIN_IDS: return True
    with db() as c:
        return c.execute("SELECT 1 FROM admins WHERE telegram_id=?", (uid,)).fetchone() is not None

def require_admin(auth):
    uid = uid_from_header(auth)
    if not is_admin(uid): raise HTTPException(403, "Нет доступа администратора")
    return uid

def prize_dict(r):
    return {"id":r["id"],"name":r["name"],"description":r["description"],"image":r["image"],
            "rarity":r["rarity"],"weight":r["weight"],"points":r["points"],
            "type":r["prize_type"],"delivery_value":r["delivery_value"],
            "media_files":json.loads(r["media_files"] or "[]") if "media_files" in r.keys() else [],
            "enabled":bool(r["enabled"])}

@app.get("/")
async def home(): return FileResponse(BASE / "web" / "index.html")

@app.get("/uploads/{filename}")
async def uploaded(filename: str):
    safe = Path(filename).name
    p = BASE / "uploads" / safe
    if not p.exists(): raise HTTPException(404, "Файл не найден")
    return FileResponse(p)

@app.get("/api/me")
async def me(authorization: str|None=Header(default=None)):
    uid = uid_from_header(authorization)
    with db() as c:
        c.execute("INSERT OR IGNORE INTO users(telegram_id) VALUES(?)", (uid,))
        u = c.execute("SELECT * FROM users WHERE telegram_id=?", (uid,)).fetchone()
        hist = c.execute("SELECT prize_label,rarity,image,created_at FROM spins WHERE telegram_id=? ORDER BY id DESC LIMIT 30", (uid,)).fetchall()
        pays = c.execute("SELECT total_amount,currency,spins_added,created_at FROM payments WHERE telegram_id=? ORDER BY created_at DESC LIMIT 20", (uid,)).fetchall()
        referrals = c.execute("SELECT COUNT(*) n FROM users WHERE referred_by=? AND referral_paid=1", (uid,)).fetchone()["n"]
    return {"telegram_id":uid,"spins":u["spins"],"points":u["points"],"spin_price_stars":PRICE,
            "history":[{"prize":r["prize_label"],"rarity":r["rarity"],"image":r["image"],"at":r["created_at"]} for r in hist],
            "payments":[dict(r) for r in pays],"referrals":referrals,"is_admin":is_admin(uid)}

@app.get("/api/prizes")
async def prizes():
    with db() as c:
        rows = c.execute("SELECT * FROM prizes WHERE enabled=1 ORDER BY weight DESC").fetchall()
    total = sum(max(0,float(r["weight"])) for r in rows) or 1
    return {"prizes":[{**prize_dict(r),"chance":round(max(0,float(r["weight"]))/total*100,3)} for r in rows]}

@app.post("/api/create-invoice")
async def create_invoice(payload: dict, authorization: str|None=Header(default=None)):
    uid = uid_from_header(authorization)
    try:
        n = int(payload.get("spins", 0))
    except (TypeError, ValueError):
        raise HTTPException(400, "Неверный набор")
    if n not in PACKS:
        raise HTTPException(400, "Такого набора нет")
    link = await bot.create_invoice_link(
        title=f"{n} вращений рулетки",
        description=f"Пакет из {n} вращений виртуальной рулетки",
        payload=f"spinpack:{uid}:{n}",
        currency="XTR",
        prices=[LabeledPrice(label=f"{n} вращений", amount=PACKS[n])],
        provider_token=""
    )
    return {"url": link}

@app.post("/api/spin")
async def spin(authorization: str|None=Header(default=None)):
    uid = uid_from_header(authorization)
    admin_free = is_admin(uid)
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        c.execute("INSERT OR IGNORE INTO users(telegram_id) VALUES(?)", (uid,))
        u = c.execute("SELECT spins FROM users WHERE telegram_id=?", (uid,)).fetchone()
        if not admin_free and (not u or u["spins"] < 1):
            raise HTTPException(402, "Нет вращений. Купи набор в чате с ботом.")
        rows = c.execute("SELECT * FROM prizes WHERE enabled=1 AND weight>0").fetchall()
        if not rows: raise HTTPException(500, "Нет активных призов")
        total = sum(float(r["weight"]) for r in rows)
        n = secrets.randbelow(10**9)/10**9*total
        chosen = rows[-1]; acc = 0.0
        for r in rows:
            acc += float(r["weight"])
            if n < acc: chosen = r; break
        if admin_free:
            c.execute("UPDATE users SET points=points+? WHERE telegram_id=?", (chosen["points"],uid))
        else:
            c.execute("UPDATE users SET spins=spins-1,points=points+? WHERE telegram_id=?", (chosen["points"],uid))
        delivery_payload = chosen["delivery_value"] or ""
        media_files = json.loads(chosen["media_files"] or "[]") if "media_files" in chosen.keys() else []
        if media_files:
            delivery_payload = json.dumps({"media_files": media_files}, ensure_ascii=False)
        c.execute("INSERT INTO spins(telegram_id,prize_id,prize_label,rarity,image,points_awarded,prize_type,delivery_value,claimed) VALUES(?,?,?,?,?,?,?,?,?)",
                  (uid,chosen["id"],chosen["name"],chosen["rarity"],chosen["image"],chosen["points"],chosen["prize_type"],delivery_payload,0 if delivery_payload.strip() else 1))
    claimable = bool(str(delivery_payload or "").strip())
    return {"prize":chosen["name"],"prize_id":chosen["id"],"rarity":chosen["rarity"],"image":chosen["image"],
            "points_awarded":chosen["points"],"description":chosen["description"],"admin_free":admin_free,"claimable":claimable}

@app.get("/api/admin/overview")
async def admin_overview(authorization: str|None=Header(default=None)):
    uid = require_admin(authorization)
    with db() as c:
        ps = c.execute("SELECT * FROM prizes ORDER BY created_at DESC").fetchall()
        orders = c.execute("SELECT * FROM orders ORDER BY created_at DESC LIMIT 100").fetchall()
        users = c.execute("SELECT telegram_id,spins,points,created_at FROM users ORDER BY created_at DESC LIMIT 100").fetchall()
        logs = c.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 100").fetchall()
        admins = c.execute("SELECT telegram_id FROM admins").fetchall()
    return {"prizes":[prize_dict(r) for r in ps],"orders":[dict(r) for r in orders],
            "users":[dict(r) for r in users],"audit":[dict(r) for r in logs],
            "admins":[r["telegram_id"] for r in admins],"owner_ids":sorted(ADMIN_IDS)}

@app.post("/api/admin/prizes")
async def save_prize(payload: dict, authorization: str|None=Header(default=None)):
    uid = require_admin(authorization)
    name = str(payload.get("name","")).strip()
    if not name: raise HTTPException(400,"Укажи название приза")
    pid = str(payload.get("id") or uuid.uuid4().hex[:12])
    weight = max(0.0, min(1000000.0, float(payload.get("weight",1))))
    rarity = str(payload.get("rarity","Обычный"))[:40]
    points = max(0,min(100000000,int(payload.get("points",0))))
    with db() as c:
        media_files = payload.get("media_files", [])
        if not isinstance(media_files, list):
            raise HTTPException(400, "Список медиафайлов задан неверно")
        media_json = json.dumps([{"url":str(x.get("url",""))[:1000],"type":str(x.get("type",""))[:20]} for x in media_files if isinstance(x,dict) and x.get("url")], ensure_ascii=False)
        c.execute("""INSERT INTO prizes(id,name,description,image,rarity,weight,points,prize_type,delivery_value,media_files,enabled)
        VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,description=excluded.description,
        image=excluded.image,rarity=excluded.rarity,weight=excluded.weight,points=excluded.points,
        prize_type=excluded.prize_type,delivery_value=excluded.delivery_value,media_files=excluded.media_files,enabled=excluded.enabled""",
        (pid,name,str(payload.get("description",""))[:2000],str(payload.get("image",""))[:1000],rarity,weight,points,
         str(payload.get("type","points"))[:30],str(payload.get("delivery_value",""))[:2000],media_json,1 if payload.get("enabled",True) else 0))
    audit(uid,"save_prize",f"{pid}: {name}; weight={weight}; rarity={rarity}")
    return {"ok":True,"id":pid}

@app.delete("/api/admin/prizes/{pid}")
async def delete_prize(pid: str, authorization: str|None=Header(default=None)):
    uid = require_admin(authorization)
    with db() as c:
        row = c.execute("SELECT name FROM prizes WHERE id=?", (pid,)).fetchone()
        if not row:
            raise HTTPException(404, "Приз не найден")
        c.execute("DELETE FROM prizes WHERE id=?", (pid,))
    audit(uid,"delete_prize",f"{pid}: {row['name']}")
    return {"ok":True}

@app.post("/api/admin/upload")
async def upload_image(file: UploadFile=File(...), authorization: str|None=Header(default=None)):
    uid = require_admin(authorization)
    if not file.content_type or not file.content_type.startswith("image/"): raise HTTPException(400,"Загрузи файл изображения")
    data = await file.read()
    if len(data)>5*1024*1024: raise HTTPException(413,"Максимум 5 МБ")
    ext = {"image/jpeg":".jpg","image/png":".png","image/webp":".webp","image/gif":".gif"}.get(file.content_type)
    if not ext: raise HTTPException(400,"Поддерживаются JPG, PNG, WEBP, GIF")
    name = uuid.uuid4().hex + ext
    folder = BASE/"uploads"; folder.mkdir(exist_ok=True); (folder/name).write_bytes(data)
    audit(uid,"upload_image",name)
    return {"url":WEBAPP_URL.rstrip("/")+"/uploads/"+name}

@app.post("/api/admin/upload-media")
async def upload_media(files: list[UploadFile]=File(...), authorization: str|None=Header(default=None)):
    uid = require_admin(authorization)
    if not files or len(files) > 20:
        raise HTTPException(400, "Выбери от 1 до 20 фото или видео")
    allowed = {"image/jpeg":".jpg", "image/png":".png", "image/webp":".webp", "image/gif":".gif",
               "video/mp4":".mp4", "video/webm":".webm", "video/quicktime":".mov"}
    folder = BASE/"uploads"; folder.mkdir(exist_ok=True)
    results=[]
    for file in files:
        ext=allowed.get(file.content_type or "")
        if not ext:
            raise HTTPException(400, f"Не поддерживается файл {file.filename}. Используй JPG, PNG, WEBP, GIF, MP4, WEBM или MOV.")
        data=await file.read()
        if not data or len(data)>20*1024*1024:
            raise HTTPException(413, "Каждый файл должен быть не больше 20 МБ")
        name=uuid.uuid4().hex+ext
        (folder/name).write_bytes(data)
        results.append({"url":WEBAPP_URL.rstrip("/")+"/uploads/"+name,"type":"photo" if (file.content_type or "").startswith("image/") else "video"})
    audit(uid,"upload_media",f"files={len(results)}")
    return {"files":results}

@app.post("/api/admin/orders/{oid}/approve")
async def approve_order(oid: str, authorization: str|None=Header(default=None)):
    uid = require_admin(authorization)
    with db() as c:
        row = c.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        if not row: raise HTTPException(404,"Заявка не найдена")
        if row["status"]!="pending": return {"ok":True,"status":row["status"]}
        c.execute("UPDATE orders SET status='approved' WHERE id=?", (oid,))
    audit(uid,"approve_order",oid)
    return {"ok":True}

@app.post("/api/admin/admins")
async def add_admin(payload: dict, authorization: str|None=Header(default=None)):
    uid = require_admin(authorization)
    target = int(payload.get("telegram_id",0))
    if target<=0: raise HTTPException(400,"Нужен Telegram ID")
    with db() as c: c.execute("INSERT OR IGNORE INTO admins(telegram_id,added_by) VALUES(?,?)",(target,uid))
    audit(uid,"add_admin",str(target))
    return {"ok":True}

@app.post("/api/admin/broadcast")
async def broadcast(payload: dict, authorization: str|None=Header(default=None)):
    uid = require_admin(authorization)
    text = str(payload.get("text","")).strip()
    if not text: raise HTTPException(400,"Пустое сообщение")
    with db() as c: ids = [r["telegram_id"] for r in c.execute("SELECT telegram_id FROM users").fetchall()]
    sent=0
    for target in ids:
        try: await bot.send_message(target,text); sent+=1
        except Exception: pass
    audit(uid,"broadcast",f"sent={sent}")
    return {"sent":sent}

def keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎟 Купить вращения",callback_data="shop")],
        [InlineKeyboardButton(text="🎰 Открыть рулетку",web_app=WebAppInfo(url=WEBAPP_URL))],
        [InlineKeyboardButton(text="🎁 Мои призы",callback_data="myprizes")]
    ])

def reply_menu():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🎰 Рулетка"), KeyboardButton(text="👤 Профиль")],
        [KeyboardButton(text="🎁 Мои призы"), KeyboardButton(text="🎟 Купить вращения")]
    ], resize_keyboard=True, is_persistent=True)

PACKS = {5:225, 10:400, 25:875}
@dp.message(CommandStart())
async def start(m: Message):
    await m.answer("🎰 Добро пожаловать! Открой приложение — там можно крутить рулетку, смотреть профиль и покупать вращения.", reply_markup=reply_menu())
    await m.answer("Нажми, чтобы открыть рулетку:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎰 Открыть приложение", web_app=WebAppInfo(url=WEBAPP_URL))]
    ]))

@dp.message(F.text == "🎰 Рулетка")
async def menu_roulette(m: Message):
    await m.answer("Открывай рулетку кнопкой ниже:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎰 Открыть рулетку",web_app=WebAppInfo(url=WEBAPP_URL))]]))

@dp.message(F.text == "👤 Профиль")
async def menu_profile(m: Message):
    with db() as c:
        u=c.execute("SELECT spins,points FROM users WHERE telegram_id=?",(m.from_user.id,)).fetchone()
        wins=c.execute("SELECT COUNT(*) n FROM spins WHERE telegram_id=?",(m.from_user.id,)).fetchone()["n"]
        pending=c.execute("SELECT COUNT(*) n FROM spins WHERE telegram_id=? AND claimed=0",(m.from_user.id,)).fetchone()["n"]
    if not u:
        await m.answer("Профиль пока пуст. Нажми /start.",reply_markup=reply_menu()); return
    await m.answer(f"👤 Твой профиль\nID: {m.from_user.id}\nВращений: {u['spins']}\nБаллы: {u['points']}\nВыигрышей: {wins}\nПризов к получению: {pending}",reply_markup=reply_menu())

@dp.message(F.text == "🎟 Купить вращения")
async def menu_buy(m: Message):
    await m.answer("Покупка вращений находится внутри приложения:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎰 Открыть приложение", web_app=WebAppInfo(url=WEBAPP_URL))]]))

@dp.message(F.text == "🎁 Мои призы")
async def menu_prizes(m: Message):
    await send_my_prizes(m.chat.id, m.from_user.id)

@dp.callback_query(F.data == "myprizes")
async def my_prizes_callback(cb):
    await send_my_prizes(cb.from_user.id, cb.from_user.id)
    await cb.answer()

async def send_my_prizes(chat_id: int, uid: int):
    with db() as c:
        rows=c.execute("SELECT id,prize_label,rarity,image,created_at FROM spins WHERE telegram_id=? AND claimed=0 ORDER BY id DESC LIMIT 30",(uid,)).fetchall()
    if not rows:
        await bot.send_message(chat_id,"🎁 Пока нет призов, которые можно получить. Когда выиграешь приз с выдачей, он появится здесь.",reply_markup=reply_menu())
        return
    await bot.send_message(chat_id,"🎁 Твои призы к получению. Нажми кнопку под нужным призом:",reply_markup=reply_menu())
    for r in rows:
        text=f"🎁 {r['prize_label']}\nРедкость: {r['rarity']}\nВыигран: {r['created_at']}"
        if r['image']:
            try: await bot.send_photo(chat_id,r['image'],caption=text)
            except Exception: await bot.send_message(chat_id,text)
        else:
            await bot.send_message(chat_id,text)
        await bot.send_message(chat_id,"Нажми, чтобы получить приз:",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"🎁 Получить: {r['prize_label'][:35]}",callback_data=f"claim:{r['id']}")]]))

@dp.callback_query(F.data.startswith("claim:"))
async def claim_prize(cb):
    try: spin_id=int(cb.data.split(":",1)[1])
    except Exception:
        await cb.answer("Неверный приз",show_alert=True); return
    with db() as c:
        row=c.execute("SELECT * FROM spins WHERE id=? AND telegram_id=?",(spin_id,cb.from_user.id)).fetchone()
        if not row:
            await cb.answer("Приз не найден",show_alert=True); return
        if row["claimed"]:
            await cb.answer("Этот приз уже получен",show_alert=True); return
        value=(row["delivery_value"] or "").strip()
        kind=(row["prize_type"] or "text").lower()
        if not value:
            await cb.answer("Для этого приза администратор ещё не настроил выдачу",show_alert=True); return
        c.execute("UPDATE spins SET claimed=1 WHERE id=? AND claimed=0",(spin_id,))
        if c.execute("SELECT changes() n").fetchone()["n"] != 1:
            await cb.answer("Этот приз уже получен",show_alert=True); return
    try:
        media_payload = None
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict) and isinstance(parsed.get("media_files"), list):
                media_payload = parsed["media_files"]
        except (ValueError, TypeError):
            pass
        if media_payload:
            photos = [x["url"] for x in media_payload if x.get("type") == "photo" and x.get("url")]
            videos = [x["url"] for x in media_payload if x.get("type") == "video" and x.get("url")]
            await bot.send_message(cb.from_user.id, f"🎉 Твой приз: {row['prize_label']}")
            # Telegram media groups accept up to 10 items; send larger collections in batches.
            for start in range(0, len(photos), 10):
                batch = photos[start:start+10]
                media = [InputMediaPhoto(media=url, caption=(f"📸 {row['prize_label']}" if i == 0 and start == 0 else None)) for i,url in enumerate(batch)]
                await bot.send_media_group(cb.from_user.id, media)
            for url in videos:
                await bot.send_video(cb.from_user.id, url, caption=f"🎬 {row['prize_label']}")
        elif kind in {"photo","image","картинка","фото"}:
            await bot.send_photo(cb.from_user.id,value,caption=f"🎉 Твой приз: {row['prize_label']}")
        elif kind in {"video","видео"}:
            await bot.send_video(cb.from_user.id,value,caption=f"🎉 Твой приз: {row['prize_label']}")
        elif kind in {"link","url","ссылка"}:
            await bot.send_message(cb.from_user.id,f"🎉 Твой приз: {row['prize_label']}\n{value}",disable_web_page_preview=False)
        else:
            await bot.send_message(cb.from_user.id,f"🎉 Твой приз: {row['prize_label']}\n\n{value}")
        await bot.send_message(cb.from_user.id,"Приз отправлен! 🎉",reply_markup=reply_menu())
        await cb.answer("Приз получен!")
    except Exception:
        with db() as c: c.execute("UPDATE spins SET claimed=0 WHERE id=?",(spin_id,))
        await cb.answer("Не удалось отправить приз. Проверь, что в настройках приза указан правильный URL или file_id.",show_alert=True)

@dp.message(Command("paysupport"))
async def paysupport(m: Message):
    await m.answer("По вопросам оплаты напиши владельцу бота и укажи дату и сумму платежа.")

@dp.pre_checkout_query()
async def checkout(q: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(q.id,ok=True)

@dp.message(F.successful_payment)
async def paid(m: Message):
    p=m.successful_payment; uid=m.from_user.id
    try: n=int(p.invoice_payload.split(":")[-1])
    except Exception: n=1
    with db() as c:
        cur=c.execute("INSERT OR IGNORE INTO payments(charge_id,telegram_id,total_amount,currency,spins_added) VALUES(?,?,?,?,?)",
                      (p.telegram_payment_charge_id,uid,p.total_amount,p.currency,n))
        if cur.rowcount:
            c.execute("INSERT OR IGNORE INTO users(telegram_id) VALUES(?)",(uid,))
            c.execute("UPDATE users SET spins=spins+? WHERE telegram_id=?",(n,uid))
    # Оплата обработана; баланс обновится в Mini App без отдельного сообщения в чате.

async def main():
    init_db()
    port=int(os.environ.get("PORT","8000"))
    await asyncio.gather(
      uvicorn.Server(uvicorn.Config(app,host="0.0.0.0",port=port,log_level="info")).serve(),
      dp.start_polling(bot)
    )
if __name__=="__main__":
    asyncio.run(main())
