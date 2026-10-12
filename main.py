import asyncio, hashlib, hmac, json, os, secrets, sqlite3, time, uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qsl
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, LabeledPrice, PreCheckoutQuery, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
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
          delivery_value TEXT DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1,
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
        c.execute("SELECT COUNT(*) n FROM prizes")
        if c.fetchone()["n"] == 0:
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
            "type":r["prize_type"],"delivery_value":r["delivery_value"],"enabled":bool(r["enabled"])}

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

@app.post("/api/spin")
async def spin(authorization: str|None=Header(default=None)):
    uid = uid_from_header(authorization)
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        c.execute("INSERT OR IGNORE INTO users(telegram_id) VALUES(?)", (uid,))
        u = c.execute("SELECT spins FROM users WHERE telegram_id=?", (uid,)).fetchone()
        if not u or u["spins"] < 1: raise HTTPException(402, "Нет вращений. Купи набор в чате с ботом.")
        rows = c.execute("SELECT * FROM prizes WHERE enabled=1 AND weight>0").fetchall()
        if not rows: raise HTTPException(500, "Нет активных призов")
        total = sum(float(r["weight"]) for r in rows)
        n = secrets.randbelow(10**9)/10**9*total
        chosen = rows[-1]; acc = 0.0
        for r in rows:
            acc += float(r["weight"])
            if n < acc: chosen = r; break
        c.execute("UPDATE users SET spins=spins-1,points=points+? WHERE telegram_id=?", (chosen["points"],uid))
        c.execute("INSERT INTO spins(telegram_id,prize_id,prize_label,rarity,image,points_awarded) VALUES(?,?,?,?,?,?)",
                  (uid,chosen["id"],chosen["name"],chosen["rarity"],chosen["image"],chosen["points"]))
    return {"prize":chosen["name"],"prize_id":chosen["id"],"rarity":chosen["rarity"],"image":chosen["image"],
            "points_awarded":chosen["points"],"description":chosen["description"]}

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
        c.execute("""INSERT INTO prizes(id,name,description,image,rarity,weight,points,prize_type,delivery_value,enabled)
        VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,description=excluded.description,
        image=excluded.image,rarity=excluded.rarity,weight=excluded.weight,points=excluded.points,
        prize_type=excluded.prize_type,delivery_value=excluded.delivery_value,enabled=excluded.enabled""",
        (pid,name,str(payload.get("description",""))[:2000],str(payload.get("image",""))[:1000],rarity,weight,points,
         str(payload.get("type","points"))[:30],str(payload.get("delivery_value",""))[:2000],1 if payload.get("enabled",True) else 0))
    audit(uid,"save_prize",f"{pid}: {name}; weight={weight}; rarity={rarity}")
    return {"ok":True,"id":pid}

@app.delete("/api/admin/prizes/{pid}")
async def delete_prize(pid: str, authorization: str|None=Header(default=None)):
    uid = require_admin(authorization)
    with db() as c: c.execute("UPDATE prizes SET enabled=0 WHERE id=?", (pid,))
    audit(uid,"disable_prize",pid)
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
    return {"url":"/uploads/"+name}

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
        [InlineKeyboardButton(text="🎰 Открыть рулетку",web_app=WebAppInfo(url=WEBAPP_URL))]
    ])

PACKS = {5:225, 10:400, 25:875} # bundle prices in Stars; adjust from admin later
@dp.message(CommandStart())
async def start(m: Message):
    await m.answer("🎰 Добро пожаловать! Вращение — 50 ⭐. Выбирай набор вращений:",reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="5 вращений — 225 ⭐",callback_data="pack:5")],
        [InlineKeyboardButton(text="10 вращений — 400 ⭐",callback_data="pack:10")],
        [InlineKeyboardButton(text="25 вращений — 875 ⭐",callback_data="pack:25")],
        [InlineKeyboardButton(text="🎰 Открыть рулетку",web_app=WebAppInfo(url=WEBAPP_URL))]
    ]))
@dp.message(Command("paysupport"))
async def paysupport(m: Message):
    await m.answer("По вопросам оплаты напиши владельцу бота и укажи дату и сумму платежа.")

@dp.callback_query(F.data=="shop")
async def shop(cb):
    await cb.message.answer("Выбери набор:",reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="5 вращений — 225 ⭐",callback_data="pack:5")],
        [InlineKeyboardButton(text="10 вращений — 400 ⭐",callback_data="pack:10")],
        [InlineKeyboardButton(text="25 вращений — 875 ⭐",callback_data="pack:25")]
    ]))
    await cb.answer()

@dp.callback_query(F.data.startswith("pack:"))
async def buy_pack(cb):
    n=int(cb.data.split(":")[1])
    if n not in PACKS: await cb.answer("Неверный набор",show_alert=True); return
    await bot.send_invoice(chat_id=cb.from_user.id,title=f"{n} вращений рулетки",
      description=f"Пакет из {n} вращений виртуальной рулетки",
      payload=f"spinpack:{cb.from_user.id}:{n}",currency="XTR",
      prices=[LabeledPrice(label=f"{n} вращений",amount=PACKS[n])],provider_token="")
    await cb.answer()

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
    await m.answer(f"✅ Оплата подтверждена. Начислено {n} вращений!",reply_markup=keyboard())

async def main():
    init_db()
    port=int(os.environ.get("PORT","8000"))
    await asyncio.gather(
      uvicorn.Server(uvicorn.Config(app,host="0.0.0.0",port=port,log_level="info")).serve(),
      dp.start_polling(bot)
    )
if __name__=="__main__":
    asyncio.run(main())
