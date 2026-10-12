import asyncio, hashlib, hmac, json, os, secrets, sqlite3, time, uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qsl
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, LabeledPrice, PreCheckoutQuery, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, InputMediaPhoto, ReplyKeyboardMarkup, KeyboardButton, FSInputFile
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
        CREATE TABLE IF NOT EXISTS app_settings(key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS deleted_prizes(id INTEGER PRIMARY KEY AUTOINCREMENT, prize_json TEXT NOT NULL, deleted_by INTEGER, deleted_at TEXT DEFAULT CURRENT_TIMESTAMP);
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
        # Add/update only the built-in starter prizes. Existing user-created prizes and
        # uploaded media are preserved; this migration is safe to run on every startup.
        starter_prizes = [
            ("photo1", "1 фото модели", "Одно фото из твоей коллекции. Загрузи фото в редакторе приза.", "", "Обычный", 50, 0, "media_group", "[]", "[]"),
            ("photo3", "Набор из 3 фото", "Три разных фото модели. Добавь 3 файла в медиа приза.", "", "Обычный", 20, 0, "media_group", "[]", "[]"),
            ("photo5", "Набор из 5 фото", "Пять фото модели.", "", "Редкий", 10, 0, "media_group", "[]", "[]"),
            ("photo10", "Набор из 10 фото", "Десять фото модели.", "", "Редкий", 5, 0, "media_group", "[]", "[]"),
            ("video1", "Видео модели", "Одно видео. Добавь видео в медиа приза или укажи Telegram file_id.", "", "Редкий", 8, 0, "video", "", "[]"),
            ("video_exclusive", "Эксклюзивное видео", "Особое видео модели.", "", "Эпический", 3, 0, "video", "", "[]"),
            ("private_week", "Приватный канал — 1 неделя", "Добавь ссылку-приглашение или инструкцию для выдачи.", "", "Эпический", 2, 0, "link", "", "[]"),
            ("private_month", "Приватный канал — 1 месяц", "Добавь ссылку-приглашение или инструкцию для выдачи.", "", "Легендарный", 1, 0, "link", "", "[]"),
            ("private_3months", "Приватный канал — 3 месяца", "Добавь ссылку-приглашение или инструкцию для выдачи.", "", "Легендарный", 0.5, 0, "link", "", "[]"),
            ("private_forever", "Приватный канал — навсегда", "Самый редкий приз. Добавь ссылку-приглашение или инструкцию для выдачи.", "", "Мифический", 0.1, 0, "link", "", "[]"),
            ("private_discount10", "Скидка 10% на приват", "Код скидки или инструкция выдачи настраивается администратором.", "", "Обычный", 8, 0, "text", "", "[]"),
            ("private_discount25", "Скидка 25% на приват", "Код скидки или инструкция выдачи настраивается администратором.", "", "Редкий", 3, 0, "text", "", "[]"),
            ("private_discount50", "Скидка 50% на приват", "Код скидки или инструкция выдачи настраивается администратором.", "", "Эпический", 1, 0, "text", "", "[]"),
            ("circle_dm", "Кружок в личные сообщения", "Добавь видео-кружок или file_id в медиа приза.", "", "Редкий", 3, 0, "video_note", "", "[]"),
            ("video_face", "Видео с лицом", "Добавь видео в медиа приза.", "", "Редкий", 4, 0, "video", "", "[]"),
            ("video_no_face", "Видео без лица", "Добавь видео в медиа приза.", "", "Обычный", 7, 0, "video", "", "[]"),
            ("video_pack3", "Пакет из 3 видео", "Добавь три видео в медиа приза; бот отправит их все.", "", "Эпический", 1.5, 0, "media_group", "", "[]"),
            ("rating_member", "Оценка члена", "Администратор укажет способ получения/инструкцию в поле выдачи.", "", "Мифический", 0.1, 0, "text", "", "[]"),
            ("bonus_spin1", "+1 дополнительное вращение", "Вращение начисляется автоматически.", "", "Обычный", 12, 1, "spins", "", "[]"),
            ("bonus_spin2", "+2 дополнительных вращения", "Вращения начисляются автоматически.", "", "Редкий", 5, 2, "spins", "", "[]"),
            ("bonus_spin3", "+3 дополнительных вращения", "Вращения начисляются автоматически.", "", "Эпический", 2, 3, "spins", "", "[]")        ]
        # Replace only the old bundled sample IDs, never arbitrary/custom prizes.
        legacy_ids = {"points5", "points10", "points25", "points100", "tryagain"}
        for pid in legacy_ids:
            c.execute("DELETE FROM prizes WHERE id=?", (pid,))
        for prize in starter_prizes:
            c.execute("""INSERT OR IGNORE INTO prizes(id,name,description,image,rarity,weight,points,prize_type,delivery_value,media_files)
                         VALUES(?,?,?,?,?,?,?,?,?,?)""", prize)


    with db() as c:
        defaults = {
            "card_payment_details": os.getenv("CARD_PAYMENT_DETAILS", "Реквизиты карты/СБП пока не настроены администратором."),
            "ton_wallet_address": os.getenv("TON_WALLET_ADDRESS", "TON-кошелёк пока не настроен администратором."),
            "rub_per_star": str(RUB_PER_STAR if "RUB_PER_STAR" in globals() else 2),
        }
        for key, value in defaults.items():
            c.execute("INSERT OR IGNORE INTO app_settings(key,value) VALUES(?,?)", (key, value))

def get_setting(key, fallback=""):
    try:
        with db() as c:
            row = c.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
            return row["value"] if row else fallback
    except Exception:
        return fallback

def pay_referral_reward(c, buyer_id):
    """Reward inviter once after the buyer's first confirmed purchase; caller owns transaction."""
    buyer = c.execute("SELECT referred_by, referral_paid FROM users WHERE telegram_id=?", (buyer_id,)).fetchone()
    if not buyer or not buyer["referred_by"] or buyer["referral_paid"]:
        return None
    inviter = int(buyer["referred_by"])
    if inviter == int(buyer_id):
        c.execute("UPDATE users SET referral_paid=1 WHERE telegram_id=?", (buyer_id,))
        return None
    claimed = c.execute("UPDATE users SET referral_paid=1 WHERE telegram_id=? AND referral_paid=0", (buyer_id,))
    if claimed.rowcount != 1:
        return None
    c.execute("INSERT OR IGNORE INTO users(telegram_id) VALUES(?)", (inviter,))
    c.execute("UPDATE users SET spins=spins+3 WHERE telegram_id=?", (inviter,))
    return inviter

async def notify_referral_reward(inviter_id, buyer_id):
    if not inviter_id:
        return
    try:
        await bot.send_message(inviter_id, f"🎉 Приглашённый тобой пользователь {buyer_id} впервые оплатил покупку! Тебе начислено 3 бонусных вращения.")
    except Exception:
        pass

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

def admin_ids_all():
    ids=set(ADMIN_IDS)
    try:
        with db() as c:
            ids.update(int(r["telegram_id"]) for r in c.execute("SELECT telegram_id FROM admins").fetchall())
    except Exception: pass
    return ids

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
    bot_info = await bot.get_me()
    referral_link = f"https://t.me/{bot_info.username}?start=ref_{uid}"
    return {"telegram_id":uid,"spins":u["spins"],"points":u["points"],"spin_price_stars":PRICE,
            "history":[{"prize":r["prize_label"],"rarity":r["rarity"],"image":r["image"],"at":r["created_at"]} for r in hist],
            "payments":[dict(r) for r in pays],"referrals":referrals,"referral_link":referral_link,"is_admin":is_admin(uid)}

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
        if chosen["prize_type"] == "spins":
            bonus_spins = max(0, int(chosen["points"] or 0))
            c.execute("UPDATE users SET spins=spins+? WHERE telegram_id=?", (bonus_spins,uid))
        elif admin_free:
            c.execute("UPDATE users SET points=points+? WHERE telegram_id=?", (chosen["points"],uid))
        else:
            c.execute("UPDATE users SET spins=spins-1,points=points+? WHERE telegram_id=?", (chosen["points"],uid))
        delivery_payload = chosen["delivery_value"] or ""
        media_files = json.loads(chosen["media_files"] or "[]") if "media_files" in chosen.keys() else []
        if media_files:
            delivery_payload = json.dumps({"media_files": media_files}, ensure_ascii=False)
        c.execute("INSERT INTO spins(telegram_id,prize_id,prize_label,rarity,image,points_awarded,prize_type,delivery_value,claimed) VALUES(?,?,?,?,?,?,?,?,?)",
                  (uid,chosen["id"],chosen["name"],chosen["rarity"],chosen["image"],chosen["points"],chosen["prize_type"],delivery_payload,0 if delivery_payload.strip() else 1))
    # Notify all configured admins about every win (including wins by admins).
    try:
        with db() as c:
            winner=c.execute("SELECT telegram_id FROM users WHERE telegram_id=?",(uid,)).fetchone()
        uname=""
        # username is supplied by Telegram WebApp initData, stored from the validated user payload when possible.
        for admin_id in admin_ids_all():
            try:
                await bot.send_message(admin_id, f"🎉 Выигрыш в рулетке!\nПользователь: {uid}\nПриз: {chosen['name']}\nРедкость: {chosen['rarity']}\nБаллы: {chosen['points']}\nID приза: {chosen['id']}")
            except Exception: pass
    except Exception: pass
    claimable = bool(str(delivery_payload or "").strip())
    return {"prize":chosen["name"],"prize_id":chosen["id"],"rarity":chosen["rarity"],"image":chosen["image"],
            "points_awarded":chosen["points"],"description":chosen["description"],"admin_free":admin_free,"claimable":claimable}

@app.get("/api/payment-options")
async def payment_options(authorization: str|None=Header(default=None)):
    uid = uid_from_header(authorization)
    rub_rate = max(1, min(100000, int(get_setting("rub_per_star", str(RUB_PER_STAR)) or RUB_PER_STAR)))
    return {"rub_per_star":rub_rate,"card_details":get_setting("card_payment_details", CARD_PAYMENT_DETAILS),"ton_wallet":get_setting("ton_wallet_address", TON_WALLET_ADDRESS),
            "packs":[{"spins":n,"stars":price,"rub":price*rub_rate} for n,price in PACKS.items()]}

@app.post("/api/payment-receipt")
async def upload_payment_receipt(file: UploadFile=File(...), authorization: str|None=Header(default=None)):
    uid=uid_from_header(authorization)
    if not file.content_type or not (file.content_type.startswith("image/") or file.content_type=="application/pdf"):
        raise HTTPException(400,"Чек должен быть изображением или PDF")
    data=await file.read()
    if not data or len(data)>10*1024*1024: raise HTTPException(413,"Чек должен быть не больше 10 МБ")
    ext={"image/jpeg":".jpg","image/png":".png","image/webp":".webp","image/gif":".gif","application/pdf":".pdf"}.get(file.content_type)
    if not ext: raise HTTPException(400,"Поддерживаются JPG, PNG, WEBP или PDF")
    folder=BASE/"uploads"; folder.mkdir(exist_ok=True)
    name="receipt_"+uuid.uuid4().hex+ext
    (folder/name).write_bytes(data)
    return {"url":WEBAPP_URL.rstrip("/")+"/uploads/"+name}

@app.post("/api/payment-orders")
async def create_payment_order(payload: dict, authorization: str|None=Header(default=None)):
    uid = uid_from_header(authorization)
    try: n=int(payload.get("spins",0))
    except (TypeError,ValueError): raise HTTPException(400,"Неверный пакет")
    method=str(payload.get("method",""))
    if n not in PACKS: raise HTTPException(400,"Такого набора нет")
    if method not in {"card","ton"}: raise HTTPException(400,"Выбери способ оплаты")
    proof=str(payload.get("proof","чек будет отправлен отдельно"))[:1500].strip()
    oid=uuid.uuid4().hex[:12]
    rub_rate=max(1, min(100000, int(get_setting("rub_per_star", str(RUB_PER_STAR)) or RUB_PER_STAR)))
    details=json.dumps({"spins":n,"stars":PACKS[n],"rub":PACKS[n]*rub_rate,"proof":proof},ensure_ascii=False)
    with db() as c:
        c.execute("INSERT INTO orders(id,telegram_id,method,status,details) VALUES(?,?,?,'pending',?)",(oid,uid,method,details))
    user_tag=f"@{payload.get('username')}" if payload.get("username") else "без username"
    keyboard=InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"payok:{oid}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"payno:{oid}")
    ]])
    receipt_path=None
    # If the user uploaded a receipt, keep its URL in order details and send the actual file to admins.
    for token in proof.split():
        if "/uploads/receipt_" in token:
            candidate=BASE/"uploads"/token.split("/uploads/",1)[1].split("?",1)[0]
            if candidate.is_file():
                receipt_path=candidate
                break
    for admin_id in admin_ids_all():
        try:
            summary=(f"🧾 Новая заявка на оплату\nПользователь: {uid} ({user_tag})\n"
                     f"Пакет: {n} вращений\nСумма: {PACKS[n]*RUB_PER_STAR} ₽ ({PACKS[n]} ⭐ эквивалент)\n"
                     f"Метод: {'банковская карта' if method=='card' else 'TON'}\n"
                     f"Комментарий/чек: {proof}\nЗаявка: {oid}\n\nПроверь поступление денег перед подтверждением.")
            if receipt_path:
                await bot.send_document(admin_id, FSInputFile(str(receipt_path)), caption=summary, reply_markup=keyboard)
            else:
                await bot.send_message(admin_id, summary+"\n⚠️ Файл чека не приложен.", reply_markup=keyboard)
        except Exception as e:
            print(f"Could not notify admin {admin_id} about order {oid}: {e}")
    return {"ok":True,"order_id":oid,"status":"pending","rub":PACKS[n]*rub_rate}

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
            "admins":[r["telegram_id"] for r in admins],"owner_ids":sorted(ADMIN_IDS),
            "payment_settings":{"card_details":get_setting("card_payment_details", CARD_PAYMENT_DETAILS),"ton_wallet":get_setting("ton_wallet_address", TON_WALLET_ADDRESS),"rub_per_star":int(get_setting("rub_per_star", str(RUB_PER_STAR)) or RUB_PER_STAR)}}

@app.post("/api/admin/payment-settings")
async def save_payment_settings(payload: dict, authorization: str|None=Header(default=None)):
    uid = require_admin(authorization)
    card = str(payload.get("card_details", "")).strip()[:1000]
    wallet = str(payload.get("ton_wallet", "")).strip()[:300]
    try: rate = int(payload.get("rub_per_star", 2))
    except (TypeError, ValueError): raise HTTPException(400, "Курс должен быть числом")
    if rate < 1 or rate > 100000: raise HTTPException(400, "Курс должен быть от 1 до 100000")
    with db() as c:
        for key, value in (("card_payment_details", card), ("ton_wallet_address", wallet), ("rub_per_star", str(rate))):
            c.execute("INSERT INTO app_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    audit(uid, "save_payment_settings", f"card_set={bool(card)}; ton_set={bool(wallet)}; rub_per_star={rate}")
    return {"ok": True}

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
        row = c.execute("SELECT * FROM prizes WHERE id=?", (pid,)).fetchone()
        if not row:
            raise HTTPException(404, "Приз не найден")
        snapshot=json.dumps(dict(row),ensure_ascii=False)
        c.execute("INSERT INTO deleted_prizes(prize_json,deleted_by) VALUES(?,?)",(snapshot,uid))
        c.execute("DELETE FROM prizes WHERE id=?", (pid,))
    audit(uid,"delete_prize",f"{pid}: {row['name']}")
    return {"ok":True}

@app.post("/api/admin/undo-delete")
async def undo_delete_prize(authorization: str|None=Header(default=None)):
    uid=require_admin(authorization)
    with db() as c:
        snap=c.execute("SELECT * FROM deleted_prizes ORDER BY id DESC LIMIT 1").fetchone()
        if not snap: raise HTTPException(404,"Нет удалённого приза для восстановления")
        prize=json.loads(snap["prize_json"])
        columns=["id","name","description","image","rarity","weight","points","prize_type","delivery_value","media_files","enabled","created_at"]
        vals=[prize.get(k) for k in columns]
        c.execute("INSERT INTO prizes("+",".join(columns)+") VALUES("+",".join(["?"]*len(columns))+")",vals)
        c.execute("DELETE FROM deleted_prizes WHERE id=?",(snap["id"],))
    audit(uid,"undo_delete",str(prize.get("id","")))
    return {"ok":True,"name":prize.get("name","")}

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
        try: info=json.loads(row["details"] or "{}")
        except Exception: info={}
        spins=int(info.get("spins",0))
        if spins not in PACKS: raise HTTPException(400,"В заявке указан неизвестный пакет")
        cur = c.execute("UPDATE orders SET status='approved' WHERE id=? AND status='pending'", (oid,))
        if cur.rowcount != 1:
            return {"ok":True,"status":"already_processed"}
        c.execute("INSERT OR IGNORE INTO users(telegram_id) VALUES(?)",(row["telegram_id"],))
        c.execute("UPDATE users SET spins=spins+? WHERE telegram_id=?",(spins,row["telegram_id"]))
        inviter_id = pay_referral_reward(c, row["telegram_id"])
    audit(uid,"approve_order",oid)
    try: await bot.send_message(row["telegram_id"],f"✅ Оплата подтверждена! Начислено вращений: {spins}. Сумма: {info.get('rub',0)} ₽.")
    except Exception: pass
    await notify_referral_reward(inviter_id, row["telegram_id"])
    return {"ok":True,"spins_added":spins,"referral_rewarded":bool(inviter_id)}

@app.post("/api/admin/orders/{oid}/reject")
async def reject_order(oid: str, authorization: str|None=Header(default=None)):
    uid = require_admin(authorization)
    with db() as c:
        row = c.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        if not row: raise HTTPException(404,"Заявка не найдена")
        if row["status"] != "pending": return {"ok":True,"status":row["status"]}
        c.execute("UPDATE orders SET status='rejected' WHERE id=? AND status='pending'", (oid,))
    audit(uid,"reject_order",oid)
    try: await bot.send_message(row["telegram_id"], "❌ Оплата не подтверждена. Проверь чек/перевод и при необходимости отправь заявку повторно или свяжись с администратором.")
    except Exception: pass
    return {"ok":True,"status":"rejected"}

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

PACKS = {1:50, 3:120, 10:400}
RUB_PER_STAR = 2
CARD_PAYMENT_DETAILS = os.getenv("CARD_PAYMENT_DETAILS", "Укажи реквизиты карты в Render → Environment: CARD_PAYMENT_DETAILS")
TON_WALLET_ADDRESS = os.getenv("TON_WALLET_ADDRESS", "Укажи адрес TON-кошелька в Render → Environment: TON_WALLET_ADDRESS")
@dp.message(CommandStart())
async def start(m: Message):
    inviter_id = None
    if m.text:
        parts = m.text.split(maxsplit=1)
        if len(parts) == 2 and parts[1].startswith("ref_"):
            try:
                candidate = int(parts[1][4:])
                if candidate > 0 and candidate != m.from_user.id:
                    inviter_id = candidate
            except ValueError:
                pass
    with db() as c:
        c.execute("INSERT OR IGNORE INTO users(telegram_id) VALUES(?)", (m.from_user.id,))
        if inviter_id is not None:
            c.execute("INSERT OR IGNORE INTO users(telegram_id) VALUES(?)", (inviter_id,))
            c.execute("UPDATE users SET referred_by=? WHERE telegram_id=? AND referred_by IS NULL AND referral_paid=0", (inviter_id, m.from_user.id))
    me = await bot.get_me()
    referral_link = f"https://t.me/{me.username}?start=ref_{m.from_user.id}"
    await m.answer("🎰 Добро пожаловать! Открой приложение — там можно крутить рулетку, смотреть профиль и покупать вращения.\n\n"
                   "👥 Приглашай друзей: за первую подтверждённую покупку друга получишь 3 вращения.\n"
                   f"Твоя ссылка: {referral_link}", reply_markup=reply_menu())
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
        elif kind in {"video_note","кружок"}:
            await bot.send_video_note(cb.from_user.id,value)
            await bot.send_message(cb.from_user.id, f"🎉 Твой приз: {row['prize_label']}")
        elif kind in {"link","url","ссылка"}:
            await bot.send_message(cb.from_user.id,f"🎉 Твой приз: {row['prize_label']}\n{value}",disable_web_page_preview=False)
        else:
            await bot.send_message(cb.from_user.id,f"🎉 Твой приз: {row['prize_label']}\n\n{value}")
        await bot.send_message(cb.from_user.id,"Приз отправлен! 🎉",reply_markup=reply_menu())
        await cb.answer("Приз получен!")
    except Exception:
        with db() as c: c.execute("UPDATE spins SET claimed=0 WHERE id=?",(spin_id,))
        await cb.answer("Не удалось отправить приз. Проверь, что в настройках приза указан правильный URL или file_id.",show_alert=True)

@dp.message(F.video | F.document)
async def admin_video_file_id(m: Message):
    if not is_admin(m.from_user.id): return
    if m.video:
        file_id=m.video.file_id
        kind="видео"
        size=m.video.file_size or 0
    elif m.document and (m.document.mime_type or "").startswith("video/"):
        file_id=m.document.file_id
        kind="видеофайл"
        size=m.document.file_size or 0
    else:
        return
    await m.answer(f"✅ {kind.capitalize()} принят. Сохрани этот Telegram file_id и вставь его в поле «Содержимое приза» с типом выдачи «Видео».\n\n<code>{file_id}</code>\n\nРазмер: {size/1024/1024:.1f} МБ\nБот будет отправлять сохранённый файл напрямую через Telegram, без загрузки на сервер.",parse_mode="HTML")

@dp.callback_query(F.data.startswith("payok:"))
async def bot_approve_payment(callback):
    uid=callback.from_user.id
    if uid not in admin_ids_all():
        await callback.answer("Нет доступа", show_alert=True)
        return
    oid=callback.data.split(":",1)[1]
    with db() as c:
        row=c.execute("SELECT * FROM orders WHERE id=?",(oid,)).fetchone()
        if not row:
            await callback.answer("Заявка не найдена", show_alert=True); return
        if row["status"]!="pending":
            await callback.answer(f"Уже обработана: {row['status']}", show_alert=True); return
        try: info=json.loads(row["details"] or "{}")
        except Exception: info={}
        spins=int(info.get("spins",0))
        if spins not in PACKS:
            await callback.answer("Некорректный пакет", show_alert=True); return
        cur=c.execute("UPDATE orders SET status='approved' WHERE id=? AND status='pending'",(oid,))
        if cur.rowcount != 1:
            await callback.answer("Заявка уже обработана", show_alert=True); return
        c.execute("INSERT OR IGNORE INTO users(telegram_id) VALUES(?)",(row["telegram_id"],))
        c.execute("UPDATE users SET spins=spins+? WHERE telegram_id=?",(spins,row["telegram_id"]))
        inviter_id = pay_referral_reward(c, row["telegram_id"])
    audit(uid,"approve_order_bot",oid)
    try: await bot.send_message(row["telegram_id"],f"✅ Оплата подтверждена! Начислено вращений: {spins}. Сумма: {info.get('rub',0)} ₽.")
    except Exception: pass
    await notify_referral_reward(inviter_id, row["telegram_id"])
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.reply(f"✅ Заявка {oid} подтверждена администратором {uid}. Начислено {spins} вращений пользователю {row['telegram_id']}.")
    except Exception: pass
    await callback.answer("Оплата подтверждена, вращения начислены")

@dp.callback_query(F.data.startswith("payno:"))
async def bot_reject_payment(callback):
    uid=callback.from_user.id
    if uid not in admin_ids_all():
        await callback.answer("Нет доступа", show_alert=True)
        return
    oid=callback.data.split(":",1)[1]
    with db() as c:
        row=c.execute("SELECT * FROM orders WHERE id=?",(oid,)).fetchone()
        if not row:
            await callback.answer("Заявка не найдена", show_alert=True); return
        if row["status"]!="pending":
            await callback.answer(f"Уже обработана: {row['status']}", show_alert=True); return
        c.execute("UPDATE orders SET status='rejected' WHERE id=? AND status='pending'",(oid,))
    audit(uid,"reject_order_bot",oid)
    try: await bot.send_message(row["telegram_id"],"❌ Оплата отклонена администратором. Проверь чек и реквизиты; если считаешь это ошибкой, свяжись с администратором.")
    except Exception: pass
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.reply(f"❌ Заявка {oid} отклонена администратором {uid}. Вращения не начислены.")
    except Exception: pass
    await callback.answer("Заявка отклонена")

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
            inviter_id = pay_referral_reward(c, uid)
        else:
            inviter_id = None
    await notify_referral_reward(inviter_id, uid)
    # Баланс обновится в Mini App; пригласивший получит уведомление о бонусе.

async def main():
    init_db()
    port=int(os.environ.get("PORT","8000"))
    await asyncio.gather(
      uvicorn.Server(uvicorn.Config(app,host="0.0.0.0",port=port,log_level="info")).serve(),
      dp.start_polling(bot)
    )
if __name__=="__main__":
    asyncio.run(main())
