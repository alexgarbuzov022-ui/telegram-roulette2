import asyncio, hashlib, hmac, json, os, secrets, sqlite3, time, uuid, re
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
import re
from datetime import datetime, timezone, timedelta
import uvicorn

BASE = Path(__file__).resolve().parent
os.chdir(BASE)
load_dotenv(BASE / ".env")
TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://telegram-roulette2.onrender.com").strip()
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DB_PATH = BASE / os.getenv("DB_PATH", "roulette.sqlite3")
USE_POSTGRES = bool(DATABASE_URL)
if os.getenv("RENDER") and not DATABASE_URL:
    raise SystemExit("DATABASE_URL is required on Render to avoid losing data to ephemeral SQLite storage.")
if USE_POSTGRES:
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise SystemExit("DATABASE_URL is set but psycopg is missing. Add psycopg[binary] to requirements.txt.") from exc
PRICE = int(os.getenv("SPIN_PRICE_STARS", "50"))
app = FastAPI(title="Telegram Prize Roulette")
bot = Bot(TOKEN) if TOKEN else None
dp = Dispatcher()

if not TOKEN:
    raise SystemExit("Set BOT_TOKEN in Render Environment.")

class PgCursor:
    def __init__(self, cursor, lastrowid=None):
        self._cursor = cursor
        self.lastrowid = lastrowid
        self.rowcount = cursor.rowcount
    def fetchone(self): return self._cursor.fetchone()
    def fetchall(self): return self._cursor.fetchall()

class PgConnection:
    def __init__(self, conn): self._conn = conn
    def execute(self, sql, params=()):
        q = sql.strip()
        if q.upper() == "PRAGMA FOREIGN_KEYS=ON":
            return PgCursor(self._conn.execute("SELECT 1"))
        m = re.fullmatch(r"PRAGMA\s+table_info\((\w+)\)", q, re.I)
        if m:
            cur = self._conn.execute("SELECT column_name AS name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=%s ORDER BY ordinal_position", (m.group(1).lower(),))
            return PgCursor(cur)
        q = re.sub(r"\bBEGIN\s+IMMEDIATE\b", "BEGIN", q, flags=re.I)
        ignore = bool(re.match(r"INSERT\s+OR\s+IGNORE\s+INTO\b", q, re.I))
        if ignore:
            q = re.sub(r"INSERT\s+OR\s+IGNORE\s+INTO\b", "INSERT INTO", q, count=1, flags=re.I)
            if not re.search(r"\bON\s+CONFLICT\b", q, re.I):
                q = q.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
        q = re.sub(r"\bid\s+INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b", "id BIGSERIAL PRIMARY KEY", q, flags=re.I)
        q = q.replace("?", "%s")
        track_id = bool(re.match(r"\s*INSERT\s+INTO\s+(spins|media_library)\b", q, re.I))
        if track_id and " RETURNING " not in q.upper():
            cur = self._conn.execute(q.rstrip().rstrip(";") + " RETURNING id", params)
            row = cur.fetchone()
            return PgCursor(cur, row["id"] if row else None)
        return PgCursor(self._conn.execute(q, params))
    def executescript(self, script):
        result = None
        for statement in script.split(";"):
            if statement.strip(): result = self.execute(statement)
        return result
    def commit(self): self._conn.commit()
    def rollback(self): self._conn.rollback()
    def close(self): self._conn.close()

@contextmanager
def db():
    if USE_POSTGRES:
        conn = psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=15)
        c = PgConnection(conn)
    else:
        c = sqlite3.connect(DB_PATH, timeout=20)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()



def add_column_if_missing(c, table, column, definition):
    if USE_POSTGRES:
        exists = c.execute("SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=? AND column_name=?", (table, column)).fetchone()
    else:
        exists = column in {r["name"] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
    if not exists:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

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
        add_column_if_missing(c, "users", "daily_claimed_at", "TEXT DEFAULT NULL")
        add_column_if_missing(c, "users", "daily_streak", "INTEGER NOT NULL DEFAULT 0")
        add_column_if_missing(c, "users", "spin_discount_pct", "INTEGER NOT NULL DEFAULT 0")
        add_column_if_missing(c, "users", "spin_discount_expires_at", "TEXT DEFAULT NULL")
        add_column_if_missing(c, "users", "private_discount_pct", "INTEGER NOT NULL DEFAULT 0")
        add_column_if_missing(c, "spins", "prize_type", "TEXT NOT NULL DEFAULT 'points'")
        add_column_if_missing(c, "spins", "delivery_value", "TEXT DEFAULT ''")
        add_column_if_missing(c, "spins", "claimed", "INTEGER NOT NULL DEFAULT 0")
        add_column_if_missing(c, "prizes", "media_files", "TEXT NOT NULL DEFAULT '[]'")
        add_column_if_missing(c, "prizes", "media_pool", "TEXT NOT NULL DEFAULT ''")
        c.execute("""CREATE TABLE IF NOT EXISTS media_library(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            url TEXT NOT NULL,
            media_type TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
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
            "media_pool":r["media_pool"] if "media_pool" in r.keys() else "",
            "enabled":bool(r["enabled"])}


def upload_to_b2(data: bytes, filename: str, content_type: str):
    """Store prize media in the private Backblaze B2 bucket."""
    key_id = os.getenv("B2_KEY_ID", "").strip()
    application_key = os.getenv("B2_APPLICATION_KEY", "").strip()
    bucket_name = os.getenv("B2_BUCKET_NAME", "").strip()

    if not (key_id and application_key and bucket_name):
        raise HTTPException(
            status_code=503,
            detail="Backblaze B2 is not configured. Check B2_KEY_ID, B2_APPLICATION_KEY and B2_BUCKET_NAME."
        )

    try:
        from b2sdk.v2 import InMemoryAccountInfo, B2Api

        info = InMemoryAccountInfo()
        b2_api = B2Api(info)
        b2_api.authorize_account("production", key_id, application_key)
        bucket = b2_api.get_bucket_by_name(bucket_name)
        bucket.upload_bytes(
            data,
            filename,
            content_type=content_type or "application/octet-stream",
        )
        return f"{WEBAPP_URL.rstrip('/')}/media/{filename}"
    except HTTPException:
        raise
    except Exception as exc:
        print(f"Backblaze B2 upload failed: {exc}")
        raise HTTPException(
            status_code=502,
            detail="Не удалось сохранить файл в Backblaze B2. Проверь настройки B2 в Render."
        )


def download_from_b2(filename: str):
    """Download one object from the private B2 bucket into memory."""
    key_id = os.getenv("B2_KEY_ID", "").strip()
    application_key = os.getenv("B2_APPLICATION_KEY", "").strip()
    bucket_name = os.getenv("B2_BUCKET_NAME", "").strip()
    if not (key_id and application_key and bucket_name):
        raise HTTPException(status_code=503, detail="Backblaze B2 is not configured")

    try:
        import tempfile
        from b2sdk.v2 import InMemoryAccountInfo, B2Api

        info = InMemoryAccountInfo()
        b2_api = B2Api(info)
        b2_api.authorize_account("production", key_id, application_key)
        bucket = b2_api.get_bucket_by_name(bucket_name)

        # b2sdk save_to() expects a filesystem path, not a BytesIO object.
        with tempfile.TemporaryDirectory() as temp_dir:
            local_path = Path(temp_dir) / filename
            bucket.download_file_by_name(filename).save_to(str(local_path))
            return local_path.read_bytes()
    except HTTPException:
        raise
    except Exception as exc:
        print(f"Backblaze B2 download failed for {filename}: {exc}")
        raise HTTPException(status_code=404, detail="Файл не найден в Backblaze B2")

@app.get("/")
async def home(): return FileResponse(BASE / "web" / "index.html")

@app.get("/media/{filename}")
async def uploaded_media(filename: str):
    # Only UUID-style generated names are expected; never accept folder paths.
    safe = Path(filename).name
    if safe != filename or not re.fullmatch(r"[0-9a-f]{32}\.(jpg|png|webp|gif|mp4|webm|mov)", safe, re.I):
        raise HTTPException(status_code=404, detail="Файл не найден")
    data = download_from_b2(safe)
    import mimetypes
    media_type = mimetypes.guess_type(safe)[0] or "application/octet-stream"
    from fastapi.responses import Response
    return Response(content=data, media_type=media_type, headers={"Cache-Control": "private, max-age=300"})


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
        hist = c.execute("SELECT prize_label,rarity,image,created_at,prize_type,claimed FROM spins WHERE telegram_id=? ORDER BY id DESC LIMIT 30", (uid,)).fetchall()
        pays = c.execute("SELECT total_amount,currency,spins_added,created_at FROM payments WHERE telegram_id=? ORDER BY created_at DESC LIMIT 20", (uid,)).fetchall()
        referrals = c.execute("SELECT COUNT(*) n FROM users WHERE referred_by=? AND referral_paid=1", (uid,)).fetchone()["n"]
    bot_info = await bot.get_me()
    referral_link = f"https://t.me/{bot_info.username}?start=ref_{uid}"
    spin_discount_pct = int(u["spin_discount_pct"] or 0)
    spin_discount_expires_at = u["spin_discount_expires_at"]
    if spin_discount_pct:
        try:
            if not spin_discount_expires_at: raise ValueError("missing discount expiry")
            expires = datetime.fromisoformat(spin_discount_expires_at)
            if expires.tzinfo is None: expires = expires.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) >= expires.astimezone(timezone.utc): spin_discount_pct = 0
        except (ValueError, TypeError): spin_discount_pct = 0
    return {"telegram_id":uid,"spins":u["spins"],"points":u["points"],"spin_price_stars":PRICE,
            "history":[{"prize":r["prize_label"],"rarity":r["rarity"],"image":r["image"],"at":r["created_at"],"prize_type":r["prize_type"],"claimed":r["claimed"]} for r in hist],
            "daily_claimed_at":u["daily_claimed_at"],"daily_streak":u["daily_streak"],"daily_next_day":(int(u["daily_streak"] or 0)%7)+1,"spin_discount_pct":spin_discount_pct,"spin_discount_expires_at":spin_discount_expires_at,"private_discount_pct":u["private_discount_pct"],
            "payments":[dict(r) for r in pays],"referrals":referrals,"referral_link":referral_link,"is_admin":is_admin(uid)}

@app.post("/api/daily-bonus")
async def daily_bonus(authorization: str|None=Header(default=None)):
    uid = uid_from_header(authorization)
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    reward_message = ""
    delivery = None
    with db() as c:
        if USE_POSTGRES:
            c.execute("INSERT OR IGNORE INTO users(telegram_id) VALUES(?)", (uid,))
            c.execute("SELECT telegram_id FROM users WHERE telegram_id=? FOR UPDATE", (uid,))
        else:
            c.execute("BEGIN IMMEDIATE")
        c.execute("INSERT OR IGNORE INTO users(telegram_id) VALUES(?)", (uid,))
        user = c.execute("SELECT * FROM users WHERE telegram_id=?", (uid,)).fetchone()
        last = user["daily_claimed_at"]
        if last:
            try:
                last_dt = datetime.fromisoformat(last)
                if last_dt.tzinfo is None: last_dt = last_dt.replace(tzinfo=timezone.utc)
                elapsed = (now - last_dt.astimezone(timezone.utc)).total_seconds()
                remaining = 86400 - int(elapsed)
                if remaining > 0:
                    hours, rem = divmod(remaining, 3600)
                    raise HTTPException(409, f"Бонус уже получен. Следующий через {hours} ч. {rem//60} мин.")
                streak = (int(user["daily_streak"] or 0) % 7 + 1) if elapsed <= 172800 else 1
            except HTTPException: raise
            except (ValueError, TypeError, OverflowError): streak = 1
        else: streak = 1
        c.execute("UPDATE users SET daily_streak=?,daily_claimed_at=? WHERE telegram_id=?", (streak, now_iso, uid))
        if streak == 1:
            expires_iso = (now + timedelta(hours=24)).isoformat()
            c.execute("UPDATE users SET spin_discount_pct=10,spin_discount_expires_at=? WHERE telegram_id=?", (expires_iso, uid))
            reward_message = "Скидка 10% на одну покупку вращений в любом из трёх наборов активирована на 24 часа. После успешной оплаты скидка сгорит."
        elif streak == 2:
            c.execute("UPDATE users SET private_discount_pct=10 WHERE telegram_id=?", (uid,))
            reward_message = "Скидка 10% на приват получена. Покажи это сообщение при оформлении привата."
        elif streak == 3:
            # The circle is delivered as a real Telegram video-note message,
            # which the user can forward to @XDarryanaX.
            circle = c.execute(
                "SELECT * FROM prizes WHERE enabled=1 AND lower(prize_type) IN ('video_note','кружок') "
                "AND (COALESCE(delivery_value,'')<>'' OR COALESCE(media_files,'[]')<>'[]') "
                "ORDER BY RANDOM() LIMIT 1"
            ).fetchone()
            if circle:
                delivery = dict(circle)
                reward_message = "🎥 Ежедневный приз — кружок. Перешли сообщение с кружком из чата бота в личку модели @XDarryanaX."
            else:
                c.execute("UPDATE users SET daily_claimed_at=NULL,daily_streak=CASE WHEN daily_streak > 0 THEN daily_streak-1 ELSE 0 END WHERE telegram_id=?", (uid,))
                raise HTTPException(503, "Ежедневный кружок пока не настроен. Попробуй позже.")
        elif streak == 4:
            c.execute("UPDATE users SET spins=spins+1 WHERE telegram_id=?", (uid,))
            reward_message = "🎰 Одно бесплатное вращение начислено!"
        elif streak == 5:
            c.execute("UPDATE users SET spins=spins+2 WHERE telegram_id=?", (uid,))
            reward_message = "🎰 +2 бесплатных вращения начислены!"
        elif streak == 6:
            c.execute("UPDATE users SET private_discount_pct=25 WHERE telegram_id=?", (uid,))
            reward_message = "🏷️ Скидка 25% на приват получена. Покажи это сообщение при оформлении привата."
        else:
            # Day 7: weighted random surprise from enabled prizes. Fallback is +3 spins.
            candidates = c.execute("SELECT * FROM prizes WHERE enabled=1 AND weight>0").fetchall()
            total_weight = sum(max(0.0, float(p["weight"] or 0)) for p in candidates)
            prize = None
            if candidates and total_weight > 0:
                target = secrets.SystemRandom().random() * total_weight
                acc = 0.0
                for candidate in candidates:
                    acc += max(0.0, float(candidate["weight"] or 0))
                    if target < acc:
                        prize = candidate
                        break
                prize = prize or candidates[-1]
            if prize:
                media_files = json.loads(prize["media_files"] or "[]") if "media_files" in prize.keys() else []
                delivery_value = (prize["delivery_value"] or "").strip()
                if media_files:
                    delivery_value = json.dumps({"media_files": media_files}, ensure_ascii=False)
                c.execute("INSERT INTO spins(telegram_id,prize_id,prize_label,rarity,image,points_awarded,prize_type,delivery_value,claimed) VALUES(?,?,?,?,?,0,?,?,2)", (uid,prize['id'],prize['name'],prize['rarity'],prize['image'],prize['prize_type'],delivery_value))
                delivery = dict(prize)
                delivery["delivery_value"] = delivery_value
                reward_message = f"🎁 Сюрприз дня: {prize['name']}! Приз отправлен в этот чат."
            else:
                c.execute("UPDATE users SET spins=spins+3 WHERE telegram_id=?", (uid,))
                reward_message = "🎰 Сюрприз дня: +3 бесплатных вращения!"
        balance = c.execute("SELECT spins FROM users WHERE telegram_id=?", (uid,)).fetchone()["spins"]
    # Send daily rewards to the user's bot chat. For day 3, send the actual
    # video-note message so the user can forward that message to the model.
    try:
        if streak == 3 and delivery:
            raw_value = (delivery.get("delivery_value") or "").strip()
            media_items = []
            try:
                parsed = json.loads(raw_value)
                if isinstance(parsed, dict) and isinstance(parsed.get("media_files"), list):
                    media_items = parsed["media_files"]
            except (ValueError, TypeError):
                pass
            if not media_items:
                try:
                    stored = json.loads(delivery.get("media_files") or "[]")
                    if isinstance(stored, list):
                        media_items = stored
                except (ValueError, TypeError):
                    pass
            circle_value = None
            for item in media_items:
                if isinstance(item, dict) and str(item.get("type", "")).lower() in ("video_note", "кружок"):
                    circle_value = item.get("url") or item.get("file_id")
                    if circle_value:
                        break
            if not circle_value:
                circle_value = raw_value
            if not circle_value:
                raise ValueError("Кружок не настроен: отсутствует файл или file_id")
            await bot.send_video_note(uid, circle_value)
            await bot.send_message(uid, "🎁 Ежедневный бонус · День 3/7\nПерешли именно сообщение с кружком выше в личные сообщения модели @XDarryanaX.")
        elif streak == 7 and delivery:
            raw_value = (delivery.get("delivery_value") or "").strip()
            kind = (delivery.get("prize_type") or "text").lower()
            media_items = []
            try:
                parsed = json.loads(raw_value)
                if isinstance(parsed, dict) and isinstance(parsed.get("media_files"), list):
                    media_items = parsed["media_files"]
            except (ValueError, TypeError):
                pass
            if not media_items:
                try:
                    stored = json.loads(delivery.get("media_files") or "[]")
                    if isinstance(stored, list):
                        media_items = stored
                except (ValueError, TypeError):
                    pass
            if media_items:
                sent = 0
                for item in media_items:
                    if not isinstance(item, dict):
                        continue
                    file_value = item.get("url") or item.get("file_id")
                    if not file_value:
                        continue
                    media_kind = str(item.get("type", "")).lower()
                    caption = f"🎁 Ежедневный приз: {delivery['name']}" if sent == 0 else None
                    if media_kind in ("photo", "image", "картинка", "фото"):
                        await bot.send_photo(uid, file_value, caption=caption)
                        sent += 1
                    elif media_kind in ("video", "видео"):
                        await bot.send_video(uid, file_value, caption=caption)
                        sent += 1
                    elif media_kind in ("video_note", "кружок"):
                        await bot.send_video_note(uid, file_value)
                        sent += 1
                if not sent:
                    raise ValueError("В настройках случайного приза не найдены отправляемые медиафайлы")
            elif kind in ("photo", "image", "картинка", "фото") and raw_value:
                await bot.send_photo(uid, raw_value, caption=f"🎁 Ежедневный приз: {delivery['name']}")
            elif kind in ("video", "видео") and raw_value:
                video_values = [part.strip() for line in raw_value.splitlines() for part in line.split(";") if part.strip()]
                for index, video_value in enumerate(video_values):
                    await bot.send_video(uid, video_value, caption=f"🎁 Ежедневный приз: {delivery['name']}" if index == 0 else None)
            elif kind in ("video_note", "кружок") and raw_value:
                await bot.send_video_note(uid, raw_value)
                await bot.send_message(uid, f"🎁 Ежедневный приз: {delivery['name']}\nПерешли кружок в личку модели @XDarryanaX.")
            elif kind in ("link", "url", "ссылка"):
                await bot.send_message(uid, f"🎁 Ежедневный приз: {delivery['name']}\n{raw_value}")
            else:
                await bot.send_message(uid, f"🎁 Ежедневный приз: {delivery['name']}\n{raw_value or delivery.get('description') or 'Содержимое приза не настроено.'}")
        else:
            await bot.send_message(uid, f"🎁 Ежедневный бонус · День {streak}/7\n\n{reward_message}")
    except Exception as exc:
        print(f"Daily bonus delivery failed for user {uid}, day {streak}: {exc}")
        # Do not leave a failed circle bonus consumed. For day 7, put the surprise
        # into the normal prize collection so the user can retry from the bot.
        try:
            with db() as c:
                if streak == 3:
                    c.execute("UPDATE users SET daily_claimed_at=NULL,daily_streak=CASE WHEN daily_streak > 0 THEN daily_streak-1 ELSE 0 END WHERE telegram_id=? AND daily_claimed_at=?", (uid, now_iso))
                elif streak == 7 and delivery:
                    c.execute("UPDATE spins SET claimed=0 WHERE id=(SELECT id FROM spins WHERE telegram_id=? AND prize_id=? ORDER BY id DESC LIMIT 1)", (uid, delivery.get("id")))
        except Exception as rollback_exc:
            print(f"Could not recover failed daily reward for user {uid}: {rollback_exc}")
        reward_message += "\n⚠️ Не получилось отправить бонус в чат бота. Для кружка попробуй забрать бонус ещё раз; если проблема повторится, напиши администратору."
    return {"ok":True,"spins":balance,"daily_claimed_at":now_iso,"daily_streak":streak,"message":f"День {streak}/7: {reward_message}"}

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
    with db() as c:
        user = c.execute("SELECT spin_discount_pct,spin_discount_expires_at FROM users WHERE telegram_id=?", (uid,)).fetchone()
        discount_pct = int(user["spin_discount_pct"] or 0) if user else 0
        if discount_pct:
            try:
                if not user["spin_discount_expires_at"]: raise ValueError("missing discount expiry")
                expires = datetime.fromisoformat(user["spin_discount_expires_at"])
                if expires.tzinfo is None: expires = expires.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) >= expires.astimezone(timezone.utc): discount_pct = 0
            except (ValueError, TypeError): discount_pct = 0
    amount = max(1, int(round(PACKS[n] * (100 - discount_pct) / 100)))
    link = await bot.create_invoice_link(
        title=f"{n} вращений рулетки",
        description=f"Пакет из {n} вращений виртуальной рулетки" + (f" (скидка {discount_pct}%)" if discount_pct else ""),
        payload=f"spinpack:{uid}:{n}:{discount_pct}",
        currency="XTR",
        prices=[LabeledPrice(label=f"{n} вращений" + (f" −{discount_pct}%" if discount_pct else ""), amount=amount)],
        provider_token=""
    )
    return {"url": link}

@app.post("/api/spin")
async def spin(authorization: str|None=Header(default=None)):
    uid = uid_from_header(authorization)
    # Get username from Telegram's validated WebApp initData.
    try:
        tg_user = validate_init_data(authorization[4:]) if authorization and authorization.startswith("tma ") else {}
        username = tg_user.get("username")
        full_name = " ".join(x for x in (tg_user.get("first_name"), tg_user.get("last_name")) if x).strip()
        user_label = f"@{username}" if username else (full_name or f"ID {uid}")
    except Exception:
        user_label = f"ID {uid}"
    admin_free = is_admin(uid)
    with db() as c:
        if USE_POSTGRES:
            c.execute("INSERT OR IGNORE INTO users(telegram_id) VALUES(?)", (uid,))
            c.execute("SELECT telegram_id FROM users WHERE telegram_id=? FOR UPDATE", (uid,))
        else:
            c.execute("BEGIN IMMEDIATE")
        c.execute("INSERT OR IGNORE INTO users(telegram_id) VALUES(?)", (uid,))
        u = c.execute("SELECT spins FROM users WHERE telegram_id=?", (uid,)).fetchone()
        if not admin_free and (not u or u["spins"] < 1):
            raise HTTPException(402, "Нет вращений. Купи набор в чате с ботом.")
        rows = c.execute("SELECT * FROM prizes WHERE enabled=1 AND weight>0").fetchall()
        if not rows: raise HTTPException(500, "Нет активных призов")
        # Select across ALL enabled prizes using the same weights exposed by
        # /api/prizes. Previously the code first selected a rarity tier and only
        # then a prize, so the displayed per-prize chance (weight / all weights)
        # did not match the real chance. This single weighted draw makes them match.
        weighted_prizes = [(r, max(0.0, float(r["weight"] or 0))) for r in rows]
        total_weight = sum(w for _, w in weighted_prizes)
        if total_weight <= 0:
            raise HTTPException(500, "Нет активных призов с корректным весом")
        n = secrets.randbelow(10**9) / 10**9 * total_weight
        chosen = weighted_prizes[-1][0]
        acc = 0.0
        for prize_row, prize_weight in weighted_prizes:
            acc += prize_weight
            if n < acc:
                chosen = prize_row
                break
        if chosen["prize_type"] == "spins":
            bonus_spins = max(0, int(chosen["points"] or 0))
            c.execute("UPDATE users SET spins=spins+? WHERE telegram_id=?", (bonus_spins,uid))
        elif admin_free:
            c.execute("UPDATE users SET points=points+? WHERE telegram_id=?", (chosen["points"],uid))
        else:
            c.execute("UPDATE users SET spins=spins-1,points=points+? WHERE telegram_id=?", (chosen["points"],uid))
        delivery_payload = (chosen["delivery_value"] or "").strip()
        # A category is inferred on the server too, so prize delivery does not depend
        # on the admin page having sent the hidden category field correctly.
        media_files = json.loads(chosen["media_files"] or "[]") if "media_files" in chosen.keys() else []
        # Each prize owns its own media. Infer pack size from the prize name.
        prize_name_norm = str(chosen["name"] or "").lower().replace("ё", "е")
        match_count = re.search(r"(?:набор\s+из\s+)?(10|5|3|1)\s*(?:фото|фотограф|видео|шт)", prize_name_norm)
        wanted_count = int(match_count.group(1)) if match_count else 1
        valid_media = []
        for item in media_files:
            if not isinstance(item, dict):
                continue
            file_value = str(item.get("url") or item.get("file_id") or "").strip()
            if not file_value:
                continue
            media_type = str(item.get("type", "")).lower().strip()
            if media_type in {"image", "картинка", "фото"}:
                media_type = "photo"
            if media_type in {"видео"}:
                media_type = "video"
            if media_type in {"photo", "video"}:
                valid_media.append({"url": file_value, "type": media_type})
        if valid_media:
            # Select exactly the pack size, without duplicates. Never silently deliver a smaller pack.
            wants_video = "видео" in prize_name_norm or "video" in prize_name_norm
            matching = [m for m in valid_media if m["type"] == ("video" if wants_video else "photo")]
            if matching:
                valid_media = matching
            else:
                raise HTTPException(400, f"Для приза «{chosen['name']}» не прикреплены файлы нужного типа ({'видео' if wants_video else 'фото'}).")
            if len(valid_media) < wanted_count:
                raise HTTPException(400, f"Для приза «{chosen['name']}» нужно {wanted_count} файлов, прикреплено {len(valid_media)} подходящих.")
            valid_media = secrets.SystemRandom().sample(valid_media, wanted_count)
            delivery_payload = json.dumps({"media_files": valid_media}, ensure_ascii=False)
        elif str(chosen["image"] or "").strip() and delivery_payload in {"", "[]", "{}", "null"}:
            delivery_payload = json.dumps({"media_files": [{"url": str(chosen["image"]).strip(), "type": "photo"}]}, ensure_ascii=False)
        elif delivery_payload in {"[]", "{}", "null"}:
            delivery_payload = ""
        spin_cursor = c.execute("INSERT INTO spins(telegram_id,prize_id,prize_label,rarity,image,points_awarded,prize_type,delivery_value,claimed) VALUES(?,?,?,?,?,?,?,?,?)",
                  (uid,chosen["id"],chosen["name"],chosen["rarity"],chosen["image"],chosen["points"],chosen["prize_type"],delivery_payload,0 if delivery_payload.strip() else 1))
        spin_id = spin_cursor.lastrowid
    # Notify all configured admins about every win (including wins by admins).
    try:
        for admin_id in admin_ids_all():
            try:
                await bot.send_message(admin_id, f"🎉 Выигрыш в рулетке!\\nПользователь: {user_label} (ID: {uid})\\nПриз: {chosen['name']}\\nРедкость: {chosen['rarity']}\\nID приза: {chosen['id']}")
            except Exception: pass
    except Exception: pass
    claimable = bool(str(delivery_payload or "").strip())
    return {"prize":chosen["name"],"prize_id":chosen["id"],"spin_id":spin_id,"rarity":chosen["rarity"],"image":chosen["image"],
            "points_awarded":chosen["points"],"description":chosen["description"],"admin_free":admin_free,"claimable":claimable}

@app.post("/api/claim-prize")
async def api_claim_prize(payload: dict, authorization: str|None=Header(default=None)):
    uid = uid_from_header(authorization)
    try:
        spin_id = int(payload.get("spin_id", 0))
    except (TypeError, ValueError):
        raise HTTPException(400, "Некорректный ID приза")
    if spin_id <= 0:
        raise HTTPException(400, "Некорректный ID приза")
    try:
        ok, message = await claim_prize_for_user(uid, spin_id)
    except Exception:
        raise HTTPException(502, "Не удалось отправить уведомление в Telegram. Попробуй ещё раз.")
    if not ok:
        raise HTTPException(400, message)
    return {"ok": True, "message": message}

@app.get("/api/payment-options")
async def payment_options(authorization: str|None=Header(default=None)):
    uid = uid_from_header(authorization)
    rub_rate = max(1, min(100000, int(get_setting("rub_per_star", str(RUB_PER_STAR)) or RUB_PER_STAR)))
    with db() as c:
        user = c.execute("SELECT spin_discount_pct,spin_discount_expires_at FROM users WHERE telegram_id=?", (uid,)).fetchone()
        discount_pct = max(0, min(90, int(user["spin_discount_pct"] or 0))) if user else 0
        if discount_pct:
            try:
                if not user or not user["spin_discount_expires_at"]: raise ValueError("missing discount expiry")
                expires = datetime.fromisoformat(user["spin_discount_expires_at"])
                if expires.tzinfo is None: expires = expires.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) >= expires.astimezone(timezone.utc): discount_pct = 0
            except (ValueError, TypeError): discount_pct = 0
    packs = []
    for n, price in PACKS.items():
        discounted_stars = max(1, int(round(price * (100-discount_pct) / 100)))
        base_rub = price * rub_rate
        discounted_rub = max(1, int(round(base_rub * (100-discount_pct) / 100)))
        packs.append({"spins": n, "stars": price, "discounted_stars": discounted_stars,
                      "rub": base_rub, "discounted_rub": discounted_rub})
    return {"rub_per_star":rub_rate,"spin_discount_pct":discount_pct,
            "card_details":get_setting("card_payment_details", CARD_PAYMENT_DETAILS),
            "ton_wallet":get_setting("ton_wallet_address", TON_WALLET_ADDRESS), "packs":packs}

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
    with db() as c:
        pending_discount = c.execute(
            "SELECT 1 FROM orders WHERE telegram_id=? AND status='pending' AND details LIKE ? LIMIT 1",
            (uid, '%"discount_pct": 10%')
        ).fetchone()
        if pending_discount:
            raise HTTPException(409, "У тебя уже есть ожидающая подтверждения заявка со скидкой. Дождись решения администратора.")
        user = c.execute("SELECT spin_discount_pct,spin_discount_expires_at FROM users WHERE telegram_id=?", (uid,)).fetchone()
        discount_pct = int(user["spin_discount_pct"] or 0) if user else 0
        if discount_pct:
            try:
                if not user["spin_discount_expires_at"]: raise ValueError("missing discount expiry")
                expires = datetime.fromisoformat(user["spin_discount_expires_at"])
                if expires.tzinfo is None: expires = expires.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) >= expires.astimezone(timezone.utc): discount_pct = 0
            except (ValueError, TypeError): discount_pct = 0
    final_rub = round(PACKS[n] * rub_rate * (100-discount_pct) / 100)
    details=json.dumps({"spins":n,"stars":PACKS[n],"discount_pct":discount_pct,"rub":final_rub,"proof":proof},ensure_ascii=False)
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
                     f"Пакет: {n} вращений\nСумма: {final_rub} ₽ ({discount_pct}% скидка; {PACKS[n]} ⭐ базовая цена)\n"
                     f"Метод: {'банковская карта' if method=='card' else 'TON'}\n"
                     f"Комментарий/чек: {proof}\nЗаявка: {oid}\n\nПроверь поступление денег перед подтверждением.")
            if receipt_path:
                await bot.send_document(admin_id, FSInputFile(str(receipt_path)), caption=summary, reply_markup=keyboard)
            else:
                await bot.send_message(admin_id, summary+"\n⚠️ Файл чека не приложен.", reply_markup=keyboard)
        except Exception as e:
            print(f"Could not notify admin {admin_id} about order {oid}: {e}")
    return {"ok":True,"order_id":oid,"status":"pending","rub":final_rub,"discount_pct":discount_pct}

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
    name = str(payload.get("name", "")).strip()
    if not name:
        raise HTTPException(400, "Укажи название приза")
    pid = str(payload.get("id") or uuid.uuid4().hex[:12])
    try:
        weight = max(0.0, min(1000000.0, float(payload.get("weight", 1))))
        points = max(0, min(100000000, int(payload.get("points", 0))))
    except (TypeError, ValueError):
        raise HTTPException(400, "Вес и количество баллов должны быть числами")
    rarity = str(payload.get("rarity", "Обычный"))[:40]
    media_files = payload.get("media_files", [])
    if not isinstance(media_files, list):
        raise HTTPException(400, "Список медиафайлов задан неверно")
    media_json = json.dumps(
        [{"url": str(x.get("url", ""))[:1000], "type": str(x.get("type", ""))[:20]}
         for x in media_files if isinstance(x, dict) and x.get("url")],
        ensure_ascii=False,
    )
    media_pool = str(payload.get("media_pool", ""))[:30]
    if media_pool not in {"", "photo", "video_noface", "video_face"}:
        raise HTTPException(400, "Неизвестная категория медиа")
    with db() as c:
        c.execute("""INSERT INTO prizes(id,name,description,image,rarity,weight,points,prize_type,delivery_value,media_files,enabled,media_pool)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,description=excluded.description,
            image=excluded.image,rarity=excluded.rarity,weight=excluded.weight,points=excluded.points,
            prize_type=excluded.prize_type,delivery_value=excluded.delivery_value,media_files=excluded.media_files,
            enabled=excluded.enabled,media_pool=excluded.media_pool""",
            (pid, name, str(payload.get("description", ""))[:2000], str(payload.get("image", ""))[:1000],
             rarity, weight, points, str(payload.get("type", "points"))[:30],
             str(payload.get("delivery_value", ""))[:2000], media_json,
             1 if payload.get("enabled", True) else 0, media_pool))
    audit(uid, "save_prize", f"{pid}: {name}; weight={weight}; rarity={rarity}; media={len(media_files)}")
    return {"ok": True, "id": pid}

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
    url = upload_to_b2(data, name, file.content_type)
    audit(uid,"upload_image",name)
    return {"url":url}

@app.post("/api/admin/upload-media")
async def upload_media(files: list[UploadFile]=File(...), authorization: str|None=Header(default=None)):
    uid = require_admin(authorization)
    if not files or len(files) > 20:
        raise HTTPException(400, "Выбери от 1 до 20 фото или видео")
    allowed = {"image/jpeg":".jpg", "image/png":".png", "image/webp":".webp", "image/gif":".gif",
               "video/mp4":".mp4", "video/webm":".webm", "video/quicktime":".mov"}
    results=[]
    for file in files:
        ext=allowed.get(file.content_type or "")
        if not ext:
            raise HTTPException(400, f"Не поддерживается файл {file.filename}. Используй JPG, PNG, WEBP, GIF, MP4, WEBM или MOV.")
        data=await file.read()
        if not data or len(data)>20*1024*1024:
            raise HTTPException(413, "Каждый файл должен быть не больше 20 МБ")
        name=uuid.uuid4().hex+ext
        url=upload_to_b2(data, name, file.content_type or "")
        results.append({"url":url,"type":"photo" if (file.content_type or "").startswith("image/") else "video"})
    audit(uid,"upload_media",f"files={len(results)}")
    return {"files":results}


@app.get("/api/admin/media-pool")
async def list_media_pool(authorization: str|None=Header(default=None)):
    require_admin(authorization)
    with db() as c:
        rows=c.execute("SELECT id,category,url,media_type,created_at FROM media_library ORDER BY id DESC").fetchall()
    return {"files":[dict(r) for r in rows]}

@app.post("/api/admin/media-pool/upload")
async def upload_media_pool(category: str, files: list[UploadFile]=File(...), authorization: str|None=Header(default=None)):
    uid=require_admin(authorization)
    if category not in {"photo","video_noface","video_face"}:
        raise HTTPException(400,"Выбери категорию загрузки")
    if not files or len(files)>50:
        raise HTTPException(400,"Выбери от 1 до 50 файлов")
    allowed={"image/jpeg":".jpg","image/png":".png","image/webp":".webp","image/gif":".gif",
             "video/mp4":".mp4","video/webm":".webm","video/quicktime":".mov"}
    saved=[]
    for file in files:
        ct=file.content_type or ""; ext=allowed.get(ct)
        if not ext: raise HTTPException(400,f"Формат файла {file.filename} не поддерживается")
        if category=="photo" and not ct.startswith("image/"):
            raise HTTPException(400,"В раздел «Фото» загружай только изображения")
        if category!="photo" and not ct.startswith("video/"):
            raise HTTPException(400,"В разделы видео загружай только видео")
        data=await file.read()
        if not data or len(data)>20*1024*1024: raise HTTPException(413,"Каждый файл должен быть не больше 20 МБ")
        name=uuid.uuid4().hex+ext
        url=upload_to_b2(data, name, ct)
        mt="photo" if ct.startswith("image/") else "video"
        with db() as c:
            cur=c.execute("INSERT INTO media_library(category,url,media_type) VALUES(?,?,?)",(category,url,mt))
            saved.append({"id":cur.lastrowid,"category":category,"url":url,"media_type":mt})
    audit(uid,"upload_media_pool",f"{category}: {len(saved)} files")
    return {"files":saved,"count":len(saved)}

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
        if int(info.get("discount_pct", 0) or 0) > 0:
            c.execute("UPDATE users SET spin_discount_pct=0,spin_discount_expires_at=NULL WHERE telegram_id=? AND spin_discount_pct=?", (row["telegram_id"], int(info.get("discount_pct", 0))))
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
        [KeyboardButton(text="💳 Оплата"), KeyboardButton(text="🎰 Рулетка"), KeyboardButton(text="👤 Профиль")],
        [KeyboardButton(text="🎁 Мои призы")]
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
    await m.answer("Выбери действие:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎰 Открыть рулетку", web_app=WebAppInfo(url=WEBAPP_URL))],
        [InlineKeyboardButton(text="🎁 Мои призы / Получить призы", callback_data="myprizes")]
    ]))

@dp.message(F.text == "💳 Оплата")
async def menu_payment(m: Message):
    await m.answer("Открой приложение и выбери раздел «Оплата», чтобы купить вращения картой/СБП, TON или Telegram Stars.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💳 Открыть оплату", web_app=WebAppInfo(url=WEBAPP_URL))]]))

@dp.message(F.text == "🎰 Рулетка")
async def menu_roulette(m: Message):
    await m.answer("Открывай рулетку кнопкой ниже:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎰 Открыть рулетку",web_app=WebAppInfo(url=WEBAPP_URL))]]))

@dp.message(F.text == "👤 Профиль")
async def menu_profile(m: Message):
    with db() as c:
        u=c.execute("SELECT spins FROM users WHERE telegram_id=?",(m.from_user.id,)).fetchone()
        wins=c.execute("SELECT COUNT(*) n FROM spins WHERE telegram_id=?",(m.from_user.id,)).fetchone()["n"]
        pending=c.execute("SELECT COUNT(*) n FROM spins WHERE telegram_id=? AND claimed=0",(m.from_user.id,)).fetchone()["n"]
    if not u:
        await m.answer("Профиль пока пуст. Нажми /start.",reply_markup=reply_menu()); return
    await m.answer(f"👤 Твой профиль\nID: {m.from_user.id}\nВращений: {u['spins']}\nВыигрышей: {wins}\nПризов к получению: {pending}",reply_markup=reply_menu())

@dp.message(F.text == "🎟 Купить вращения")
async def menu_buy(m: Message):
    await m.answer("Покупка вращений находится внутри приложения:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎰 Открыть приложение", web_app=WebAppInfo(url=WEBAPP_URL))]]))

@dp.message(F.text == "🎁 Мои призы")
async def menu_prizes(m: Message):
    await send_my_prizes(m.chat.id, m.from_user.id)

@dp.message(Command("myprizes"))
async def menu_prizes_command(m: Message):
    await send_my_prizes(m.chat.id, m.from_user.id)

@dp.callback_query(F.data == "myprizes")
async def my_prizes_callback(cb):
    await send_my_prizes(cb.from_user.id, cb.from_user.id)
    await cb.answer()

async def send_my_prizes(chat_id: int, uid: int):
    with db() as c:
        rows=c.execute("SELECT id,prize_label,rarity,image,created_at,claimed FROM spins WHERE telegram_id=? AND claimed IN (0,1) ORDER BY id DESC LIMIT 30",(uid,)).fetchall()
    if not rows:
        await bot.send_message(chat_id,"🎁 Пока нет призов, ожидающих открытия. Когда выиграешь приз в рулетке, он появится здесь.",reply_markup=reply_menu())
        return
    await bot.send_message(chat_id,"🎁 Твои призы. Нажми кнопку под нужным призом:",reply_markup=reply_menu())
    for r in rows:
        if int(r["claimed"] or 0) == 1:
            text = f"🎉 Новый приз!\n{r['prize_label']}\n\nНажми «Открыть приз», чтобы получить содержимое."
            button = InlineKeyboardButton(text="🎁 Открыть приз", callback_data=f"openprize:{r['id']}")
        else:
            text = "🎁 У тебя есть выигранный приз!\nНажми «Получить приз», чтобы подтвердить его и открыть доступ."
            button = InlineKeyboardButton(text="🎁 Получить приз", callback_data=f"claim:{r['id']}")
        await bot.send_message(chat_id, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[button]]))

async def claim_prize_for_user(uid: int, spin_id: int):
    """Move a won prize to the unopened state and notify the user once."""
    with db() as c:
        row = c.execute("SELECT * FROM spins WHERE id=? AND telegram_id=?", (spin_id, uid)).fetchone()
        if not row:
            return False, "Приз не найден"
        if int(row["claimed"] or 0) == 1:
            return False, "Этот приз уже ожидает открытия"
        if int(row["claimed"] or 0) >= 2:
            return False, "Этот приз уже открыт"
        value = str(row["delivery_value"] or "").strip()
        # For photo/video packs, the attached prize media is authoritative. Older
        # spin rows may contain only the legacy single-file delivery_value.
        prize = c.execute("SELECT * FROM prizes WHERE id=?", (row["prize_id"],)).fetchone()
        if prize:
            name_norm = str(prize["name"] or row["prize_label"] or "").lower().replace("ё", "е")
            match_count = re.search(r"(?:набор\s+из\s+)?(10|5|3|1)\s*(?:фото|фотограф|видео|шт)", name_norm)
            wanted = int(match_count.group(1)) if match_count else 1
            existing_media = []
            try:
                parsed_value = json.loads(value) if value else {}
                if isinstance(parsed_value, dict) and isinstance(parsed_value.get("media_files"), list):
                    existing_media = parsed_value["media_files"]
            except (ValueError, TypeError):
                pass
            kind_wanted = "video" if "видео" in name_norm or "video" in name_norm else "photo"
            existing_matching = [x for x in existing_media if isinstance(x, dict) and x.get("url") and ("photo" if x.get("type") == "image" else x.get("type")) == kind_wanted]
            if wanted > 1 and len(existing_matching) < wanted:
                prize_media = []
                try:
                    prize_media = json.loads(prize["media_files"] or "[]")
                except (ValueError, TypeError):
                    prize_media = []
                valid = [x for x in prize_media if isinstance(x, dict) and x.get("url") and x.get("type") in {"photo", "image", "video"}]
                matching = [x for x in valid if ("photo" if x.get("type") == "image" else x.get("type")) == kind_wanted]
                if matching:
                    valid = matching
                if len(valid) >= wanted:
                    value = json.dumps({"media_files": secrets.SystemRandom().sample(valid, wanted)}, ensure_ascii=False)
            if not value:
                value = str(prize["delivery_value"] or "").strip()
            if not value and str(prize["image"] or "").strip():
                value = json.dumps({"media_files":[{"url":str(prize["image"]).strip(),"type":"photo"}]}, ensure_ascii=False)
            if not value:
                pool = str(prize["media_pool"] or "").strip()
                if not pool:
                    if any(k in name_norm for k in ("видео с лицом", "video face", "видео без лица", "video noface")):
                        pool = "video_face" if "лицом" in name_norm or "face" in name_norm else "video_noface"
                    elif any(k in name_norm for k in ("фото", "фотограф", "photo", "picture")):
                        pool = "photo"
                if pool:
                    media_row = c.execute("SELECT url,media_type FROM media_library WHERE category=? ORDER BY RANDOM() LIMIT 1", (pool,)).fetchone()
                    if media_row:
                        value = json.dumps({"media_files":[{"url":media_row["url"],"type":media_row["media_type"]}]}, ensure_ascii=False)
        if not value or value in {"[]", "{}", "null"}:
            return False, "Для этого приза не настроено содержимое"
        c.execute("UPDATE spins SET delivery_value=? WHERE id=? AND telegram_id=?", (value, spin_id, uid))
        claim_update = c.execute("UPDATE spins SET claimed=1 WHERE id=? AND telegram_id=? AND claimed=0", (spin_id, uid))
        if claim_update.rowcount != 1:
            return False, "Этот приз уже обработан"
        label = row["prize_label"]
    try:
        await bot.send_message(uid, f"🎉 Новый приз!\n{label}\n\nОткрыть:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🎁 Открыть приз", callback_data=f"openprize:{spin_id}")
        ]]))
    except Exception:
        with db() as c:
            c.execute("UPDATE spins SET claimed=0 WHERE id=? AND telegram_id=? AND claimed=1", (spin_id, uid))
        raise
    return True, "Отправил уведомление о новом призе!"

@dp.callback_query(F.data.startswith("claim:"))
async def claim_prize(cb):
    try:
        spin_id = int(cb.data.split(":", 1)[1])
        ok, message = await claim_prize_for_user(cb.from_user.id, spin_id)
        if ok:
            try:
                await cb.message.edit_text("✅ Приз получен! Я отправил отдельное уведомление — нажми там «🎁 Открыть приз».")
            except Exception:
                pass
        await cb.answer(message, show_alert=not ok)
    except Exception:
        await cb.answer("Не удалось получить приз. Попробуй ещё раз.", show_alert=True)

@dp.message(F.web_app_data)
async def webapp_prize_claim(m: Message):
    """Handle the Mini App's explicit 'Получить приз' confirmation."""
    try:
        payload = json.loads(m.web_app_data.data or "{}")
        if payload.get("action") != "claim_prize":
            return
        spin_id = int(payload.get("spin_id", 0))
        ok, message = await claim_prize_for_user(m.from_user.id, spin_id)
        if not ok and message != "Этот приз уже ожидает открытия":
            await m.answer(message)
    except Exception:
        await m.answer("Не удалось подтвердить приз. Открой «🎁 Мои призы» и попробуй ещё раз.")

@dp.callback_query(F.data.startswith("openprize:"))
async def open_prize(cb):
    try:
        spin_id = int(cb.data.split(":", 1)[1])
    except Exception:
        await cb.answer("Неверный приз", show_alert=True)
        return
    # Reserve the prize before sending so repeated taps cannot deliver it twice.
    with db() as c:
        row = c.execute("SELECT * FROM spins WHERE id=? AND telegram_id=?", (spin_id, cb.from_user.id)).fetchone()
        if not row:
            await cb.answer("Приз не найден", show_alert=True)
            return
        if int(row["claimed"] or 0) == 2:
            await cb.answer("Этот приз уже открыт", show_alert=True)
            return
        if int(row["claimed"] or 0) != 1:
            await cb.answer("Сначала нажми «Получить приз»", show_alert=True)
            return
        value = (row["delivery_value"] or "").strip()
        kind = (row["prize_type"] or "text").lower()
        if not value:
            await cb.answer("У этого приза не настроено содержимое", show_alert=True)
            return
        open_update = c.execute("UPDATE spins SET claimed=2 WHERE id=? AND telegram_id=? AND claimed=1", (spin_id, cb.from_user.id))
        if open_update.rowcount != 1:
            await cb.answer("Этот приз уже открыт", show_alert=True)
            return
    try:
        media_payload = None
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict) and isinstance(parsed.get("media_files"), list):
                media_payload = parsed["media_files"]
        except (ValueError, TypeError):
            pass
        if media_payload:
            photos = [(x.get("url") or x.get("file_id")) for x in media_payload if isinstance(x, dict) and str(x.get("type", "")).lower() in {"photo", "image", "картинка", "фото"} and (x.get("url") or x.get("file_id"))]
            videos = [(x.get("url") or x.get("file_id")) for x in media_payload if isinstance(x, dict) and str(x.get("type", "")).lower() in {"video", "видео"} and (x.get("url") or x.get("file_id"))]
            if not photos and not videos:
                raise ValueError("В призе нет файлов для отправки")
            for start in range(0, len(photos), 10):
                batch = photos[start:start+10]
                if len(batch) == 1:
                    await bot.send_photo(cb.from_user.id, batch[0], caption=f"📸 {row['prize_label']}")
                else:
                    media = [InputMediaPhoto(media=url, caption=(f"📸 {row['prize_label']}" if i == 0 and start == 0 else None)) for i, url in enumerate(batch)]
                    await bot.send_media_group(cb.from_user.id, media)
            for url in videos:
                await bot.send_video(cb.from_user.id, url, caption=f"🎬 {row['prize_label']}")
            if not photos and videos:
                # The first video already carries the prize caption.
                pass
            elif photos and videos:
                await bot.send_message(cb.from_user.id, f"🎉 Приз открыт: {row['prize_label']}")
        elif kind in {"photo", "image", "картинка", "фото"}:
            await bot.send_photo(cb.from_user.id, value, caption=f"🎉 Твой приз: {row['prize_label']}")
        elif kind in {"video", "видео"}:
            # Поддерживаем несколько URL или Telegram file_id: по одному на строку
            # (также можно разделять точкой с запятой). Не делим по пробелам,
            # чтобы не повреждать URL с параметрами.
            video_values = [part.strip() for line in value.splitlines() for part in line.split(";") if part.strip()]
            if not video_values:
                raise ValueError("Не указаны ссылки на видео")
            for index, video_value in enumerate(video_values):
                caption = f"🎉 Твой приз: {row['prize_label']}" if index == 0 else None
                await bot.send_video(cb.from_user.id, video_value, caption=caption)
        elif kind in {"video_note", "кружок"}:
            await bot.send_video_note(cb.from_user.id, value)
            await bot.send_message(cb.from_user.id, f"🎉 Твой приз: {row['prize_label']}\n\nЕсли это бонусный кружок, перешли сообщение с кружком в личку модели @XDarryanaX.")
        elif kind in {"link", "url", "ссылка"}:
            await bot.send_message(cb.from_user.id, f"🎉 Твой приз: {row['prize_label']}\n{value}", disable_web_page_preview=False)
        else:
            await bot.send_message(cb.from_user.id, f"🎉 Твой приз: {row['prize_label']}\n\n{value}")
        try:
            await cb.message.edit_text("✅ Приз открыт! Содержимое отправлено выше.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🎰 Крутить", web_app=WebAppInfo(url=WEBAPP_URL)),
                InlineKeyboardButton(text="🎁 Мои призы", callback_data="myprizes")
            ]]))
        except Exception:
            pass
        await cb.answer("Приз открыт!")
    except Exception:
        with db() as c:
            c.execute("UPDATE spins SET claimed=1 WHERE id=? AND telegram_id=? AND claimed=2", (spin_id, cb.from_user.id))
        await cb.answer("Не удалось отправить приз. Проверь медиа и попробуй снова.", show_alert=True)

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
        if int(info.get("discount_pct", 0) or 0) > 0:
            c.execute("UPDATE users SET spin_discount_pct=0,spin_discount_expires_at=NULL WHERE telegram_id=? AND spin_discount_pct=?", (row["telegram_id"], int(info.get("discount_pct", 0))))
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
    # A discounted invoice can be paid only while that exact discount is still active.
    parts = (q.invoice_payload or "").split(":")
    if len(parts) >= 4 and parts[0] == "spinpack":
        try:
            uid = int(parts[1]); discount_pct = int(parts[3])
        except (TypeError, ValueError):
            await bot.answer_pre_checkout_query(q.id, ok=False, error_message="Счёт некорректен. Создай новый счёт в приложении.")
            return
        if uid != q.from_user.id:
            await bot.answer_pre_checkout_query(q.id, ok=False, error_message="Этот счёт создан для другого пользователя.")
            return
        if discount_pct > 0:
            with db() as c:
                user = c.execute("SELECT spin_discount_pct,spin_discount_expires_at FROM users WHERE telegram_id=?", (uid,)).fetchone()
            valid = bool(user and int(user["spin_discount_pct"] or 0) == discount_pct and user["spin_discount_expires_at"])
            if valid:
                try:
                    expires = datetime.fromisoformat(user["spin_discount_expires_at"])
                    if expires.tzinfo is None: expires = expires.replace(tzinfo=timezone.utc)
                    valid = datetime.now(timezone.utc) < expires.astimezone(timezone.utc)
                except (ValueError, TypeError): valid = False
            if not valid:
                await bot.answer_pre_checkout_query(q.id, ok=False, error_message="Скидка уже использована или истекла. Создай новый счёт без скидки.")
                return
    await bot.answer_pre_checkout_query(q.id,ok=True)

@dp.message(F.successful_payment)
async def paid(m: Message):
    p=m.successful_payment; uid=m.from_user.id
    parts = p.invoice_payload.split(":")
    try:
        if len(parts) < 3 or parts[0] != "spinpack" or int(parts[1]) != uid: raise ValueError("bad payload")
        n = int(parts[2])
        invoice_discount_pct = int(parts[3]) if len(parts) > 3 else 0
        if n not in PACKS: raise ValueError("bad pack")
    except Exception:
        await m.answer("⚠️ Не удалось проверить счёт. Напиши администратору и пришли чек оплаты.")
        return
    with db() as c:
        cur=c.execute("INSERT OR IGNORE INTO payments(charge_id,telegram_id,total_amount,currency,spins_added) VALUES(?,?,?,?,?)",
                      (p.telegram_payment_charge_id,uid,p.total_amount,p.currency,n))
        if cur.rowcount:
            c.execute("INSERT OR IGNORE INTO users(telegram_id) VALUES(?)",(uid,))
            c.execute("UPDATE users SET spins=spins+? WHERE telegram_id=?",(n,uid))
            if invoice_discount_pct > 0:
                c.execute("UPDATE users SET spin_discount_pct=0,spin_discount_expires_at=NULL WHERE telegram_id=? AND spin_discount_pct=?", (uid, invoice_discount_pct))
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
