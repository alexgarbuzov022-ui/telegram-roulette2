import asyncio, hashlib, hmac, json, os, secrets, sqlite3, time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qsl
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, LabeledPrice, PreCheckoutQuery, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
import uvicorn

BASE=Path(__file__).resolve().parent
os.chdir(BASE)
load_dotenv(BASE/".env")
TOKEN=os.getenv("BOT_TOKEN","").strip()
WEBAPP_URL=os.getenv("WEBAPP_URL","https://example.invalid").strip()
PRICE=int(os.getenv("SPIN_PRICE_STARS","10"))
PACK=int(os.getenv("SPINS_PER_PACK","1"))
DB_PATH=BASE/os.getenv("DB_PATH","roulette.sqlite3")
if not TOKEN or TOKEN=="PASTE_BOTFATHER_TOKEN_HERE":
    raise SystemExit("Open .env and paste your token from @BotFather into BOT_TOKEN.")
bot=Bot(TOKEN); dp=Dispatcher(); app=FastAPI(title="Local Roulette")
PRIZES=[
 {"id":"points_5","label":"5 баллов","weight":45,"amount":5},
 {"id":"points_10","label":"10 баллов","weight":28,"amount":10},
 {"id":"points_25","label":"25 баллов","weight":15,"amount":25},
 {"id":"points_100","label":"100 баллов","weight":5,"amount":100},
 {"id":"try_again","label":"Ещё повезёт","weight":7,"amount":0},
]
@contextmanager
def db():
    c=sqlite3.connect(DB_PATH,timeout=20); c.row_factory=sqlite3.Row
    try:
        c.execute("PRAGMA foreign_keys=ON"); yield c; c.commit()
    except Exception: c.rollback(); raise
    finally: c.close()
def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(telegram_id INTEGER PRIMARY KEY,spins INTEGER NOT NULL DEFAULT 0 CHECK(spins>=0),points INTEGER NOT NULL DEFAULT 0 CHECK(points>=0),created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS payments(telegram_payment_charge_id TEXT PRIMARY KEY,telegram_id INTEGER NOT NULL,total_amount INTEGER NOT NULL,currency TEXT NOT NULL,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS spins(id INTEGER PRIMARY KEY AUTOINCREMENT,telegram_id INTEGER NOT NULL,prize_id TEXT NOT NULL,prize_label TEXT NOT NULL,points_awarded INTEGER NOT NULL DEFAULT 0,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        """)
def validate_init_data(raw):
    p=dict(parse_qsl(raw,keep_blank_values=True)); received=p.pop("hash",None)
    if not received: raise ValueError("Missing hash")
    auth=int(p.get("auth_date","0"))
    if auth<=0 or time.time()-auth>3600: raise ValueError("Expired initData")
    check="\n".join(f"{k}={v}" for k,v in sorted(p.items()))
    key=hmac.new(b"WebAppData",TOKEN.encode(),hashlib.sha256).digest()
    expected=hmac.new(key,check.encode(),hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected,received): raise ValueError("Bad signature")
    return json.loads(p["user"])
def uid_from_header(auth):
    if not auth or not auth.startswith("tma "): raise HTTPException(401,"Open this Mini App from Telegram")
    try: return int(validate_init_data(auth[4:])["id"])
    except Exception: raise HTTPException(401,"Telegram session invalid or expired")
@app.get("/")
async def home(): return FileResponse(BASE/"web"/"index.html")
@app.get("/api/me")
async def me(authorization: str|None=Header(default=None)):
    uid=uid_from_header(authorization)
    with db() as c:
        c.execute("INSERT OR IGNORE INTO users(telegram_id) VALUES(?)",(uid,))
        u=c.execute("SELECT spins,points FROM users WHERE telegram_id=?",(uid,)).fetchone()
        hist=c.execute("SELECT prize_label,created_at FROM spins WHERE telegram_id=? ORDER BY id DESC LIMIT 10",(uid,)).fetchall()
    return {"spins":u["spins"],"points":u["points"],"spin_price_stars":PRICE,"history":[{"prize":r["prize_label"],"at":r["created_at"]} for r in hist]}
@app.post("/api/spin")
async def spin(authorization: str|None=Header(default=None)):
    uid=uid_from_header(authorization)
    n=secrets.randbelow(sum(p["weight"] for p in PRIZES)); prize=PRIZES[-1]; acc=0
    for p in PRIZES:
        acc+=p["weight"]
        if n<acc: prize=p; break
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        u=c.execute("SELECT spins FROM users WHERE telegram_id=?",(uid,)).fetchone()
        if not u or u["spins"]<1: raise HTTPException(402,"Нет вращений. Купи вращение в чате с ботом.")
        c.execute("UPDATE users SET spins=spins-1,points=points+? WHERE telegram_id=?",(prize["amount"],uid))
        c.execute("INSERT INTO spins(telegram_id,prize_id,prize_label,points_awarded) VALUES(?,?,?,?)",(uid,prize["id"],prize["label"],prize["amount"]))
    return {"prize":prize["label"],"prize_id":prize["id"],"points_awarded":prize["amount"]}
def keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎟 Купить вращение",callback_data="buy_spin")],[InlineKeyboardButton(text="🎰 Открыть рулетку",web_app=WebAppInfo(url=WEBAPP_URL))]])
@dp.message(CommandStart())
async def start(m:Message):
    await m.answer(f"🎰 Привет! Одно вращение стоит {PRICE} Telegram Stars. Призы — виртуальные баллы.",reply_markup=keyboard())
@dp.message(Command("paysupport"))
async def paysupport(m:Message): await m.answer("По вопросам оплаты напиши владельцу бота и укажи дату и сумму платежа.")
@dp.callback_query(F.data=="buy_spin")
async def buy(cb):
    await bot.send_invoice(chat_id=cb.from_user.id,title="Вращение рулетки",description="Одно вращение виртуальной рулетки",payload=f"spinpack:{cb.from_user.id}:{PACK}",currency="XTR",prices=[LabeledPrice(label="Одно вращение",amount=PRICE)],provider_token="")
    await cb.answer()
@dp.pre_checkout_query()
async def checkout(q:PreCheckoutQuery): await bot.answer_pre_checkout_query(q.id,ok=True)
@dp.message(F.successful_payment)
async def paid(m:Message):
    p=m.successful_payment; uid=m.from_user.id
    with db() as c:
        cur=c.execute("INSERT OR IGNORE INTO payments(telegram_payment_charge_id,telegram_id,total_amount,currency) VALUES(?,?,?,?)",(p.telegram_payment_charge_id,uid,p.total_amount,p.currency))
        if cur.rowcount:
            c.execute("INSERT OR IGNORE INTO users(telegram_id) VALUES(?)",(uid,))
            c.execute("UPDATE users SET spins=spins+? WHERE telegram_id=?",(PACK,uid))
    await m.answer("✅ Оплата получена, вращение добавлено!",reply_markup=keyboard())
async def main():
    init_db()
    port = int(os.environ.get("PORT", "8000"))
    await asyncio.gather(
        uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")).serve(),
        dp.start_polling(bot),
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
