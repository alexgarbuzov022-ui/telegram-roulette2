import asyncio, hashlib, hmac, json, os, secrets, sqlite3, time, uuid, re, html, csv, io
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qsl, urlparse
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, LabeledPrice, PreCheckoutQuery, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, InputMediaPhoto, BufferedInputFile, ReplyKeyboardMarkup, KeyboardButton, FSInputFile
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import re
import threading
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
        CREATE TABLE IF NOT EXISTS promo_draws(
          id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT NOT NULL UNIQUE,
          participants_json TEXT NOT NULL, winners_json TEXT NOT NULL DEFAULT '[]',
          status TEXT NOT NULL DEFAULT 'drawn', drawn_by INTEGER, confirmed_by INTEGER,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP, confirmed_at TEXT DEFAULT NULL);
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
        # User identity and first-activity timestamps for future analytics.
        add_column_if_missing(c, "users", "username", "TEXT DEFAULT NULL")
        add_column_if_missing(c, "users", "first_name", "TEXT DEFAULT NULL")
        add_column_if_missing(c, "users", "first_start_at", "TEXT DEFAULT NULL")
        add_column_if_missing(c, "users", "first_miniapp_at", "TEXT DEFAULT NULL")
        add_column_if_missing(c, "users", "first_spin_at", "TEXT DEFAULT NULL")
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
            ("photo5", "Набор из 5 фото", "Пять фото модели.", "", "Редкий", 10, 0, "media_group", "[]", "[]"),
            ("photo10", "Набор из 10 фото", "Десять фото модели.", "", "Редкий", 5, 0, "media_group", "[]", "[]"),
            ("video1", "Видео модели", "Одно видео. Добавь видео в медиа приза или укажи Telegram file_id.", "", "Редкий", 8, 0, "video", "", "[]"),
            ("video_exclusive", "Эксклюзивное видео", "Особое видео модели.", "", "Эпический", 3, 0, "video", "", "[]"),
            ("private_week", "Приватный канал — 1 неделя", "Добавь ссылку-приглашение или инструкцию для выдачи.", "", "Эпический", 2, 0, "link", "", "[]"),
            ("private_month", "Приватный канал — 1 месяц", "Добавь ссылку-приглашение или инструкцию для выдачи.", "", "Легендарный", 1, 0, "link", "", "[]"),
            ("private_forever", "Приватный канал — навсегда", "Самый редкий приз. Добавь ссылку-приглашение или инструкцию для выдачи.", "", "Мифический", 0.1, 0, "link", "", "[]"),
            ("private_discount10", "Скидка 10% на приват", "Код скидки или инструкция выдачи настраивается администратором.", "", "Обычный", 8, 0, "text", "", "[]"),
            ("private_discount50", "Скидка 50% на приват", "Код скидки или инструкция выдачи настраивается администратором.", "", "Эпический", 1, 0, "text", "", "[]"),
            ("circle_dm", "Кружок в личные сообщения", "Добавь видео-кружок или file_id в медиа приза.", "", "Редкий", 3, 0, "video_note", "", "[]"),
            ("video_face", "Видео с лицом", "Добавь видео в медиа приза.", "", "Редкий", 4, 0, "video", "", "[]"),
            ("video_pack3", "Пакет из 3 видео", "Добавь три видео в медиа приза; бот отправит их все.", "", "Эпический", 1.5, 0, "media_group", "", "[]"),
            ("rating_member", "Оценка члена", "Администратор укажет способ получения/инструкцию в поле выдачи.", "", "Мифический", 0.1, 0, "text", "", "[]"),
            ("bonus_spin1", "+1 дополнительное вращение", "Вращение начисляется автоматически.", "", "Обычный", 12, 1, "spins", "", "[]"),
            ("bonus_spin3", "+3 дополнительных вращения", "Вращения начисляются автоматически.", "", "Эпический", 2, 3, "spins", "", "[]")        ]
        # Replace only the old bundled sample IDs, never arbitrary/custom prizes.
        legacy_ids = {"points5", "points10", "points25", "points100", "tryagain"}
        for pid in legacy_ids:
            c.execute("DELETE FROM prizes WHERE id=?", (pid,))
        for prize in starter_prizes:
            c.execute("""INSERT OR IGNORE INTO prizes(id,name,description,image,rarity,weight,points,prize_type,delivery_value,media_files)
                         VALUES(?,?,?,?,?,?,?,?,?,?)""", prize)
        # These five prizes were explicitly retired. Disable existing rows in Neon
        # as well as omitting them from the starter list, without deleting user history.
        retired_prize_ids = ("private_discount25", "private_3months", "photo3", "bonus_spin2", "video_no_face")
        for retired_id in retired_prize_ids:
            c.execute("UPDATE prizes SET enabled=0 WHERE id=?", (retired_id,))


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

async def notify_admins_new_user(channel, telegram_id, username=None, first_name=None):
    username_text = f"@{html.escape(username)}" if username else "не указан"
    name_text = html.escape(first_name or "Не указано")
    message = (
        "🆕 <b>НОВЫЙ ПОЛЬЗОВАТЕЛЬ</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>{name_text}</b>\n"
        f"🔗 {username_text}\n"
        f"🆔 <code>{telegram_id}</code>\n\n"
        f"📍 <b>Первый вход:</b> {html.escape(channel)}"
    )
    for admin_id in admin_ids_all():
        try:
            await bot.send_message(admin_id, message, parse_mode="HTML")
        except Exception as exc:
            print(f"Не удалось отправить уведомление админу {admin_id}: {exc}")


async def notify_admins_star_payment(user, total_amount, spins_added):
    username = getattr(user, "username", None)
    username_text = f"@{html.escape(username)}" if username else "не указан"
    first_name = html.escape(getattr(user, "full_name", None) or "Не указано")
    paid_at = datetime.now(timezone.utc).strftime("%d.%m.%Y в %H:%M UTC")
    message = (
        "⭐ <b>НОВАЯ ОПЛАТА STARS</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>{first_name}</b>\n"
        f"🔗 {username_text}\n"
        f"🆔 <code>{user.id}</code>\n\n"
        f"💰 <b>Оплачено:</b> {total_amount} ⭐\n"
        f"🎟 <b>Начислено вращений:</b> {spins_added}\n"
        f"🕒 <b>Время:</b> {paid_at}"
    )
    for admin_id in admin_ids_all():
        try:
            await bot.send_message(admin_id, message, parse_mode="HTML")
        except Exception as exc:
            print(f"Не удалось отправить уведомление об оплате админу {admin_id}: {exc}")

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


# Reuse the same authenticated B2 client for the life of this process.
_B2_LOCK = threading.Lock()
_B2_CLIENT = None
_B2_CLIENT_CONFIG = None
_B2_MEDIA_CACHE_DIR = Path(os.getenv("B2_MEDIA_CACHE_DIR", "/tmp/roulette-b2-media-cache"))
_B2_MEDIA_CACHE_TTL = int(os.getenv("B2_MEDIA_CACHE_TTL_SECONDS", "3600"))


def _get_b2_bucket():
    """Return a cached, authenticated B2 bucket instead of authorizing every request."""
    global _B2_CLIENT, _B2_CLIENT_CONFIG
    key_id = os.getenv("B2_KEY_ID", "").strip()
    application_key = os.getenv("B2_APPLICATION_KEY", "").strip()
    bucket_name = os.getenv("B2_BUCKET_NAME", "").strip()
    if not (key_id and application_key and bucket_name):
        raise HTTPException(status_code=503, detail="Backblaze B2 is not configured")

    config = (key_id, application_key, bucket_name)
    with _B2_LOCK:
        if _B2_CLIENT is None or _B2_CLIENT_CONFIG != config:
            try:
                from b2sdk.v2 import InMemoryAccountInfo, B2Api
                info = InMemoryAccountInfo()
                api = B2Api(info)
                api.authorize_account("production", key_id, application_key)
                bucket = api.get_bucket_by_name(bucket_name)
                _B2_CLIENT = (api, bucket)
                _B2_CLIENT_CONFIG = config
            except Exception as exc:
                _B2_CLIENT = None
                _B2_CLIENT_CONFIG = None
                print(f"Backblaze B2 authorization failed: {exc}")
                raise HTTPException(status_code=502, detail="Не удалось подключиться к Backblaze B2")
        return _B2_CLIENT[1]


def upload_to_b2(data: bytes, filename: str, content_type: str):
    """Store prize media in B2 using the cached bucket connection."""
    try:
        bucket = _get_b2_bucket()
        bucket.upload_bytes(
            data,
            filename,
            content_type=content_type or "application/octet-stream",
        )
        # Invalidate local cache if an object is replaced with the same key.
        try:
            (_B2_MEDIA_CACHE_DIR / filename).unlink(missing_ok=True)
        except OSError:
            pass
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
    """Download B2 media with a local disk cache to avoid repeated B2 reads."""
    import tempfile

    _B2_MEDIA_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = _B2_MEDIA_CACHE_DIR / filename

    try:
        if cache_path.is_file() and (time.time() - cache_path.stat().st_mtime) < _B2_MEDIA_CACHE_TTL:
            return cache_path.read_bytes()
    except OSError:
        pass

    try:
        bucket = _get_b2_bucket()
        # Download to a temporary file, then atomically move it into the cache.
        with tempfile.NamedTemporaryFile(dir=str(_B2_MEDIA_CACHE_DIR), prefix="tmp-", delete=False) as tmp:
            temp_path = Path(tmp.name)
        try:
            bucket.download_file_by_name(filename).save_to(str(temp_path))
            data = temp_path.read_bytes()
            try:
                temp_path.replace(cache_path)
            except OSError:
                pass
            return data
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
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
    tg_user = validate_init_data(authorization[4:])
    first_miniapp_visit = False
    with db() as c:
        c.execute("INSERT OR IGNORE INTO users(telegram_id) VALUES(?)", (uid,))
        lock_suffix = " FOR UPDATE" if USE_POSTGRES else ""
        before = c.execute("SELECT first_miniapp_at FROM users WHERE telegram_id=?" + lock_suffix, (uid,)).fetchone()
        first_miniapp_visit = not before or not before["first_miniapp_at"]
        c.execute(
            "UPDATE users SET username=COALESCE(?, username), first_name=COALESCE(?, first_name), first_miniapp_at=COALESCE(first_miniapp_at, CURRENT_TIMESTAMP::text) WHERE telegram_id=?"
            if USE_POSTGRES else
            "UPDATE users SET username=COALESCE(?, username), first_name=COALESCE(?, first_name), first_miniapp_at=COALESCE(first_miniapp_at, CURRENT_TIMESTAMP) WHERE telegram_id=?",
            (tg_user.get("username") or None, " ".join(x for x in (tg_user.get("first_name"), tg_user.get("last_name")) if x).strip() or None, uid),
        )
        u = c.execute("SELECT * FROM users WHERE telegram_id=?", (uid,)).fetchone()
        hist = c.execute("SELECT prize_label,rarity,image,created_at,prize_type,claimed FROM spins WHERE telegram_id=? ORDER BY id DESC LIMIT 30", (uid,)).fetchall()
        pays = c.execute("SELECT total_amount,currency,spins_added,created_at FROM payments WHERE telegram_id=? ORDER BY created_at DESC LIMIT 20", (uid,)).fetchall()
        referrals = c.execute("SELECT COUNT(*) n FROM users WHERE referred_by=? AND referral_paid=1", (uid,)).fetchone()["n"]
    if first_miniapp_visit:
        await notify_admins_new_user(
            "первый вход через Mini App",
            uid,
            tg_user.get("username"),
            " ".join(x for x in (tg_user.get("first_name"), tg_user.get("last_name")) if x).strip(),
        )
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

RARITY_WEIGHTS = {
    "обычный": 58.0, "обычная": 58.0, "common": 58.0,
    "редкий": 30.0, "редкая": 30.0, "rare": 30.0,
    "эпический": 8.0, "эпическая": 8.0, "эпическое": 8.0, "epic": 8.0,
    "легендарный": 2.0, "легендарная": 2.0, "легендарное": 2.0, "legendary": 2.0,
    "мифический": 2.0, "мифическая": 2.0, "мифическое": 2.0, "mythic": 2.0, "mythical": 2.0,
}

def rarity_key(prize):
    return str(prize["rarity"] or "обычный").strip().lower().replace("ё", "е")

def prize_chances(rows):
    """Actual chance per prize: rarity tier first, then weight inside that tier."""
    groups = {}
    for row in rows:
        weight = max(0.0, float(row["weight"] or 0))
        if weight <= 0:
            continue
        key = rarity_key(row)
        groups.setdefault(key, {"tier": RARITY_WEIGHTS.get(key, 70.0), "items": [], "total": 0.0})
        groups[key]["items"].append((row, weight))
        groups[key]["total"] += weight
    total_tier = sum(group["tier"] for group in groups.values())
    chances = {}
    if total_tier <= 0:
        return chances
    for key, group in groups.items():
        for row, weight in group["items"]:
            chances[str(row["id"])] = (group["tier"] / total_tier) * (weight / group["total"]) * 100
    return chances

@app.get("/api/prizes")
async def prizes():
    with db() as c:
        rows = c.execute("SELECT * FROM prizes WHERE enabled=1 ORDER BY weight DESC").fetchall()
    chances = prize_chances(rows)
    return {"prizes":[{**prize_dict(r),"chance":round(chances.get(str(r["id"]), 0.0),3)} for r in rows]}

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
    tg_user = {}
    try:
        tg_user = validate_init_data(authorization[4:]) if authorization and authorization.startswith("tma ") else {}
        username = tg_user.get("username")
        full_name = " ".join(x for x in (tg_user.get("first_name"), tg_user.get("last_name")) if x).strip()
        user_label = f"@{username}" if username else (full_name or f"ID {uid}")
    except Exception:
        username = None
        full_name = ""
        user_label = f"ID {uid}"
    admin_free = is_admin(uid)
    with db() as c:
        if USE_POSTGRES:
            c.execute("INSERT OR IGNORE INTO users(telegram_id) VALUES(?)", (uid,))
            c.execute("SELECT telegram_id FROM users WHERE telegram_id=? FOR UPDATE", (uid,))
        else:
            c.execute("BEGIN IMMEDIATE")
        c.execute("INSERT OR IGNORE INTO users(telegram_id) VALUES(?)", (uid,))
        # Store profile fields from validated Telegram Mini App data and record first spin once.
        c.execute(
            "UPDATE users SET username=COALESCE(?, username), first_name=COALESCE(?, first_name) WHERE telegram_id=?",
            (username or None, full_name or None, uid),
        )
        u = c.execute("SELECT spins FROM users WHERE telegram_id=?", (uid,)).fetchone()
        if not admin_free and (not u or u["spins"] < 1):
            raise HTTPException(402, "Нет вращений. Купи набор в чате с ботом.")
        rows = c.execute("SELECT * FROM prizes WHERE enabled=1 AND weight>0").fetchall()
        if not rows: raise HTTPException(500, "Нет активных призов")
        # Draw rarity tier first, then choose a prize by its weight inside that tier.
        # /api/prizes uses the exact same formula to display the real odds.
        groups = {}
        for prize_row in rows:
            prize_weight = max(0.0, float(prize_row["weight"] or 0))
            if prize_weight <= 0:
                continue
            key = rarity_key(prize_row)
            groups.setdefault(key, {"tier": RARITY_WEIGHTS.get(key, 70.0), "items": []})
            groups[key]["items"].append((prize_row, prize_weight))
        if not groups:
            raise HTTPException(500, "Нет активных призов с корректным весом")
        tier_total = sum(group["tier"] for group in groups.values())
        draw = secrets.randbelow(10**9) / 10**9 * tier_total
        chosen_group = next(iter(groups.values()))
        acc = 0.0
        for group in groups.values():
            acc += group["tier"]
            if draw < acc:
                chosen_group = group
                break
        item_total = sum(weight for _, weight in chosen_group["items"])
        draw = secrets.randbelow(10**9) / 10**9 * item_total
        chosen = chosen_group["items"][-1][0]
        acc = 0.0
        for prize_row, prize_weight in chosen_group["items"]:
            acc += prize_weight
            if draw < acc:
                chosen = prize_row
                break
        if chosen["prize_type"] == "spins":
            bonus_spins = max(0, int(chosen["points"] or 0))
            # A normal spin is consumed even when the prize itself grants bonus spins.
            # Admin spins are free and therefore are not deducted.
            if admin_free:
                c.execute("UPDATE users SET spins=spins+? WHERE telegram_id=?", (bonus_spins,uid))
            else:
                c.execute("UPDATE users SET spins=spins-1+? WHERE telegram_id=?", (bonus_spins,uid))
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
        match_count = re.search(r"(?:(?:набор|пакет)\s+из\s+)?(10|5|3|1)\s*(?:фото|фотограф\w*|фотк\w*|видео|видос\w*|шт)", prize_name_norm)
        wanted_count = int(match_count.group(1)) if match_count else 1
        valid_media = []
        for item in media_files:
            if not isinstance(item, dict):
                continue
            file_value = str(item.get("url") or item.get("file_id") or "").strip()
            if not file_value:
                continue
            media_type = str(item.get("type", "")).lower().strip()
            if media_type in {"image", "картинка", "фото"} or media_type.startswith("image/"):
                media_type = "photo"
            if media_type in {"видео"} or media_type.startswith("video/"):
                media_type = "video"
            if media_type in {"photo", "video"}:
                valid_media.append({"url": file_value, "type": media_type})

        # Telegram file_id values can also be pasted into the legacy "delivery_value"
        # field, one per line or separated by semicolons. Treat them as a candidate pool.
        wants_video = (
            "видео" in prize_name_norm or "video" in prize_name_norm
            or str(chosen["prize_type"] or "").lower() in {"video", "видео"}
        )
        if not valid_media and wants_video and delivery_payload:
            raw_candidates = []
            try:
                parsed_delivery = json.loads(delivery_payload)
                if isinstance(parsed_delivery, dict) and isinstance(parsed_delivery.get("media_files"), list):
                    raw_candidates = parsed_delivery["media_files"]
            except (ValueError, TypeError):
                pass
            if raw_candidates:
                for item in raw_candidates:
                    if isinstance(item, dict):
                        file_value = str(item.get("url") or item.get("file_id") or "").strip()
                        media_type = str(item.get("type") or "video").lower().strip()
                        if file_value and media_type in {"video", "видео"}:
                            valid_media.append({"url": file_value, "type": "video"})
            else:
                candidates = [part.strip() for line in delivery_payload.splitlines() for part in line.split(";") if part.strip()]
                valid_media = [{"url": item, "type": "video"} for item in candidates]

        if valid_media:
            # Choose exactly the requested number of unique entries at random.
            matching = [m for m in valid_media if m["type"] == ("video" if wants_video else "photo")]
            if matching:
                valid_media = matching
            else:
                raise HTTPException(400, f"Для приза «{chosen['name']}» не прикреплены файлы нужного типа ({'видео' if wants_video else 'фото'}).")
            # Deduplicate repeated IDs so one video is not sent twice in the same pack.
            unique_media = list({(m["type"], m["url"]): m for m in valid_media}.values())
            if len(unique_media) < wanted_count:
                raise HTTPException(400, f"Для приза «{chosen['name']}» нужно {wanted_count} разных файлов, прикреплено {len(unique_media)} подходящих.")
            valid_media = secrets.SystemRandom().sample(unique_media, wanted_count)
            delivery_payload = json.dumps({"media_files": valid_media}, ensure_ascii=False)
        elif wants_video:
            # Never substitute the prize-card image for a missing video.
            raise HTTPException(400, f"Для видеоприза «{chosen['name']}» добавь Telegram file_id видео в список медиа или в поле «Содержимое приза».")
        elif str(chosen["image"] or "").strip() and delivery_payload in {"", "[]", "{}", "null"}:
            delivery_payload = json.dumps({"media_files": [{"url": str(chosen["image"]).strip(), "type": "photo"}]}, ensure_ascii=False)
        elif delivery_payload in {"[]", "{}", "null"}:
            delivery_payload = ""
        # Bonus-spin prizes are credited immediately above and have no media to open.
        # Mark them as already handled so they do not appear as broken pending prizes.
        if str(chosen["prize_type"] or "").lower() in {"spins", "spin", "bonus_spins"}:
            claimed_state = 2
        else:
            claimed_state = 0 if delivery_payload.strip() else 1
        spin_cursor = c.execute("INSERT INTO spins(telegram_id,prize_id,prize_label,rarity,image,points_awarded,prize_type,delivery_value,claimed) VALUES(?,?,?,?,?,?,?,?,?)",
                  (uid,chosen["id"],chosen["name"],chosen["rarity"],chosen["image"],chosen["points"],chosen["prize_type"],delivery_payload,claimed_state))
        spin_id = spin_cursor.lastrowid
        c.execute("UPDATE users SET first_spin_at=COALESCE(first_spin_at, CURRENT_TIMESTAMP::text) WHERE telegram_id=?", (uid,))
    # Notify all configured admins about every win (including wins by admins).
    try:
        for admin_id in admin_ids_all():
            try:
                player_name = html.escape(str(user_label))
                player_link = f'<a href="tg://user?id={uid}">{player_name}</a>'
                prize_name = html.escape(str(chosen["name"]))
                rarity_name = html.escape(str(chosen["rarity"]))
                prize_id = html.escape(str(chosen["id"]))
                notification = (
                    "🎉 <b>НОВЫЙ ВЫИГРЫШ В РУЛЕТКЕ</b>\n\n"
                    f"👤 <b>Игрок:</b> {player_link}\n"
                    f"🆔 <b>Telegram ID:</b> <code>{uid}</code>\n\n"
                    f"🎁 <b>Приз:</b> {prize_name}\n"
                    f"💎 <b>Редкость:</b> {rarity_name}\n"
                    f"🔖 <b>ID приза:</b> <code>{prize_id}</code>"
                )
                await bot.send_message(admin_id, notification, parse_mode="HTML")
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

@app.get("/api/admin/statistics")
async def admin_statistics(authorization: str|None=Header(default=None)):
    """Admin-only user acquisition and roulette activity statistics."""
    require_admin(authorization)
    with db() as c:
        rows = c.execute("""
            SELECT u.telegram_id, u.username, u.first_name, u.created_at,
                   u.first_start_at, u.first_spin_at, u.spins AS balance,
                   COUNT(s.id) AS total_spins, MIN(s.created_at) AS first_spin_from_history
            FROM users u
            LEFT JOIN spins s ON s.telegram_id = u.telegram_id
            GROUP BY u.telegram_id, u.username, u.first_name, u.created_at,
                     u.first_start_at, u.first_spin_at, u.spins
            ORDER BY COALESCE(u.first_start_at, u.created_at) DESC
            LIMIT 500
        """).fetchall()
        total_users = c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        total_spins = c.execute("SELECT COUNT(*) AS n FROM spins").fetchone()["n"]
        spun_users = c.execute("SELECT COUNT(DISTINCT telegram_id) AS n FROM spins").fetchone()["n"]
    today = datetime.now(timezone.utc).date().isoformat()
    new_today = 0
    formatted = []
    for row in rows:
        item = dict(row)
        first_start = item.get("first_start_at") or item.get("created_at")
        first_spin = item.get("first_spin_at") or item.get("first_spin_from_history")
        if first_start and str(first_start)[:10] == today:
            new_today += 1
        username = (item.get("username") or "").strip()
        item["display_name"] = ("@" + username if username else (item.get("first_name") or "Без username"))
        item["first_start_at"] = first_start
        item["first_spin_at"] = first_spin
        item["total_spins"] = int(item.get("total_spins") or 0)
        formatted.append(item)
    return {"total_users": int(total_users or 0), "new_today": new_today,
            "spun_users": int(spun_users or 0), "total_spins": int(total_spins or 0),
            "users": formatted}


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
    normalized_media = []
    for item in media_files:
        if not isinstance(item, dict) or not (item.get("url") or item.get("file_id")):
            continue
        raw_type = str(item.get("type", "")).strip().lower()
        if raw_type in {"photo", "image", "картинка", "фото"} or raw_type.startswith("image/"):
            media_type = "photo"
        elif raw_type in {"video", "видео"} or raw_type.startswith("video/"):
            media_type = "video"
        else:
            media_type = raw_type[:20]
        media_value = str(item.get("url") or item.get("file_id") or "").strip()
        normalized_media.append({"url": media_value[:1000], "type": media_type})
    media_json = json.dumps(normalized_media, ensure_ascii=False)
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


async def resolve_admin_target(target: str) -> int:
    """Resolve a Telegram numeric ID or @username; usernames require Telegram to resolve the chat."""
    raw = str(target or "").strip()
    if not raw:
        raise HTTPException(400, "Укажи Telegram ID или @username")
    if raw.isdigit():
        return int(raw)
    username = raw.lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
        raise HTTPException(400, "Укажи корректный Telegram ID или @username")
    try:
        chat = await bot.get_chat("@" + username)
        return int(chat.id)
    except Exception as exc:
        raise HTTPException(400, "Не удалось найти пользователя по username. Укажи числовой Telegram ID; пользователь должен хотя бы раз запустить бота.") from exc


@app.post("/api/admin/grant-spins")
async def admin_grant_spins(payload: dict, authorization: str|None=Header(default=None)):
    admin_id = require_admin(authorization)
    target_id = await resolve_admin_target(payload.get("user", payload.get("telegram_id", "")))
    try:
        amount = int(payload.get("amount", 0))
    except (TypeError, ValueError):
        raise HTTPException(400, "Количество вращений должно быть целым числом")
    if amount < 1 or amount > 100000:
        raise HTTPException(400, "Количество вращений должно быть от 1 до 100000")
    with db() as c:
        c.execute("INSERT INTO users(telegram_id) VALUES(?) ON CONFLICT(telegram_id) DO NOTHING", (target_id,))
        c.execute("UPDATE users SET spins=spins+? WHERE telegram_id=?", (amount, target_id))
        balance = c.execute("SELECT spins FROM users WHERE telegram_id=?", (target_id,)).fetchone()["spins"]
    try:
        await bot.send_message(target_id, f"🎰 Администратор начислил тебе {amount} вращений. Текущий баланс: {balance}.")
    except Exception as exc:
        print(f"Could not notify user {target_id} about granted spins: {type(exc).__name__}: {exc}")
    audit(admin_id, "grant_spins", f"user={target_id}; amount={amount}; balance={balance}")
    return {"ok": True, "telegram_id": target_id, "added": amount, "balance": balance}


@app.post("/api/admin/grant-prize")
async def admin_grant_prize(payload: dict, authorization: str|None=Header(default=None)):
    admin_id = require_admin(authorization)
    target_id = await resolve_admin_target(payload.get("user", payload.get("telegram_id", "")))
    prize_id = str(payload.get("prize_id", "")).strip()
    if not prize_id:
        raise HTTPException(400, "Выбери приз")
    with db() as c:
        prize = c.execute("SELECT * FROM prizes WHERE id=?", (prize_id,)).fetchone()
        if not prize:
            raise HTTPException(404, "Выбранный приз не найден")
        c.execute("INSERT INTO users(telegram_id) VALUES(?) ON CONFLICT(telegram_id) DO NOTHING", (target_id,))
        cur = c.execute(
            """INSERT INTO spins(telegram_id,prize_id,prize_label,rarity,image,points_awarded,prize_type,delivery_value,claimed)
               VALUES(?,?,?,?,?,?,?, ?,0)""",
            (target_id, prize["id"], prize["name"], prize["rarity"], prize["image"], prize["points"],
             prize["prize_type"], str(prize["delivery_value"] or "")))
        spin_id = cur.lastrowid
    try:
        ok, message = await claim_prize_for_user(target_id, int(spin_id))
        if not ok:
            raise RuntimeError(message)
    except Exception as exc:
        print(f"Could not notify user {target_id} about granted prize {prize_id}: {type(exc).__name__}: {exc}")
        raise HTTPException(502, "Приз создан, но уведомление не отправилось. Проверь, что пользователь запускал бота, затем проверь «Мои призы».")
    audit(admin_id, "grant_prize", f"user={target_id}; prize={prize_id}; spin={spin_id}")
    return {"ok": True, "telegram_id": target_id, "prize_id": prize_id, "spin_id": spin_id}


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
        lock_suffix = " FOR UPDATE" if USE_POSTGRES else ""
        before = c.execute("SELECT first_start_at FROM users WHERE telegram_id=?" + lock_suffix, (m.from_user.id,)).fetchone()
        first_start_visit = not before or not before["first_start_at"]
        # Save the first observed /start timestamp once, and refresh Telegram profile fields.
        c.execute(
            "UPDATE users SET username=COALESCE(?, username), first_name=COALESCE(?, first_name), first_start_at=COALESCE(first_start_at, CURRENT_TIMESTAMP::text) WHERE telegram_id=?",
            (m.from_user.username or None, m.from_user.full_name or None, m.from_user.id),
        )
        if inviter_id is not None:
            c.execute("INSERT OR IGNORE INTO users(telegram_id) VALUES(?)", (inviter_id,))
            c.execute("UPDATE users SET referred_by=? WHERE telegram_id=? AND referred_by IS NULL AND referral_paid=0", (inviter_id, m.from_user.id))
    if first_start_visit:
        await notify_admins_new_user(
            "первый запуск через /start",
            m.from_user.id,
            m.from_user.username,
            m.from_user.full_name,
        )
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

PROMO_EVENT_KEY = "roulette_promo_2026-10-16_2026-10-21_1300_msk"
PROMO_START_UTC = "2026-10-15 21:00:00"  # 16 Oct 00:00 Moscow
PROMO_END_UTC = "2026-10-21 10:00:00"    # 21 Oct 13:00 Moscow, exclusive
PROMO_PRIZES = ["Приват на полгода", "Приват на месяц", "3 кружка"]
PROMO_END_MSK = datetime(2026, 10, 21, 13, 0, tzinfo=timezone(timedelta(hours=3)))


def promo_is_closed():
    return datetime.now(timezone.utc) >= PROMO_END_MSK.astimezone(timezone.utc)


def promo_qualifying_participants(c):
    # Both the entrant's own successful purchase and at least one referred friend's
    # successful purchase must fall within the announced contest interval.
    rows = c.execute("""
        SELECT u.telegram_id, u.username, u.first_name,
          (SELECT COUNT(*) FROM payments p WHERE p.telegram_id=u.telegram_id
             AND p.created_at >= ? AND p.created_at < ?)
          +
          (SELECT COUNT(*) FROM orders o WHERE o.telegram_id=u.telegram_id AND o.status='approved'
             AND o.created_at >= ? AND o.created_at < ?) AS successful_purchases,
          (SELECT COUNT(*) FROM users r
             WHERE r.referred_by=u.telegram_id AND r.referral_paid=1
               AND EXISTS (SELECT 1 FROM payments rp WHERE rp.telegram_id=r.telegram_id
                           AND rp.created_at >= ? AND rp.created_at < ?)
               OR r.referred_by=u.telegram_id AND r.referral_paid=1
               AND EXISTS (SELECT 1 FROM orders ro WHERE ro.telegram_id=r.telegram_id AND ro.status='approved'
                           AND ro.created_at >= ? AND ro.created_at < ?)) AS successful_referrals
        FROM users u
        WHERE (
          EXISTS (SELECT 1 FROM payments p WHERE p.telegram_id=u.telegram_id AND p.created_at >= ? AND p.created_at < ?)
          OR EXISTS (SELECT 1 FROM orders o WHERE o.telegram_id=u.telegram_id AND o.status='approved' AND o.created_at >= ? AND o.created_at < ?)
        )
        AND EXISTS (
          SELECT 1 FROM users r WHERE r.referred_by=u.telegram_id AND r.referral_paid=1 AND (
            EXISTS (SELECT 1 FROM payments rp WHERE rp.telegram_id=r.telegram_id AND rp.created_at >= ? AND rp.created_at < ?)
            OR EXISTS (SELECT 1 FROM orders ro WHERE ro.telegram_id=r.telegram_id AND ro.status='approved' AND ro.created_at >= ? AND ro.created_at < ?)
          )
        )
        ORDER BY u.telegram_id
    """, (
        PROMO_START_UTC,PROMO_END_UTC,PROMO_START_UTC,PROMO_END_UTC,
        PROMO_START_UTC,PROMO_END_UTC,PROMO_START_UTC,PROMO_END_UTC,
        PROMO_START_UTC,PROMO_END_UTC,PROMO_START_UTC,PROMO_END_UTC,
        PROMO_START_UTC,PROMO_END_UTC,PROMO_START_UTC,PROMO_END_UTC
    )).fetchall()
    qualifying=[]
    for row in rows:
        referrals = c.execute("""
            SELECT r.telegram_id, r.username, r.first_name FROM users r
            WHERE r.referred_by=? AND r.referral_paid=1 AND (
              EXISTS (SELECT 1 FROM payments p WHERE p.telegram_id=r.telegram_id AND p.created_at >= ? AND p.created_at < ?)
              OR EXISTS (SELECT 1 FROM orders o WHERE o.telegram_id=r.telegram_id AND o.status='approved' AND o.created_at >= ? AND o.created_at < ?)
            ) ORDER BY r.telegram_id
        """, (row["telegram_id"],PROMO_START_UTC,PROMO_END_UTC,PROMO_START_UTC,PROMO_END_UTC)).fetchall()
        qualifying.append({
            "telegram_id": int(row["telegram_id"]), "username": row["username"] or "",
            "name": row["first_name"] or "", "successful_purchases": int(row["successful_purchases"] or 0),
            "successful_referrals": len(referrals), "referral_telegram_ids": [int(r["telegram_id"]) for r in referrals]
        })
    return qualifying


@dp.message(Command("promo_check"))
async def promo_check(m: Message):
    """Admin-only preflight: validate contest SQL against the live database before the end date."""
    if not m.from_user or not is_admin(m.from_user.id):
        await m.answer("⛔ Эта команда доступна только администратору.")
        return
    try:
        with db() as c:
            qualifying = promo_qualifying_participants(c)
    except Exception as exc:
        # Return a short actionable database error; full details remain in Render logs.
        print(f"promo_check failed: {type(exc).__name__}: {exc}")
        await m.answer(
            "❌ Проверка конкурса не прошла: ошибка при чтении участников из базы Neon.\n"
            f"Тип ошибки: {type(exc).__name__}.\n"
            "Открой Render → Logs и пришли строку promo_check failed."
        )
        return
    await m.answer(
        "✅ Проверка конкурса и запроса к Neon прошла успешно.\n"
        f"Подходящих участников на текущий момент: {len(qualifying)}\n"
        "Период конкурса: 16.10.2026 00:00 — 21.10.2026 13:00 МСК.\n"
        "До окончания конкурса эта команда только проверяет базу и ничего не разыгрывает."
    )


@dp.message(Command("promo_participants"))
async def promo_participants(m: Message):
    """Export contest-eligible participants for the fixed Moscow-time window."""
    if not m.from_user or not is_admin(m.from_user.id):
        await m.answer("⛔ Эта команда доступна только администратору.")
        return
    if not promo_is_closed():
        await m.answer("⏳ Конкурс ещё не завершён. Список можно зафиксировать после 21 октября 2026 года, 13:00 по Москве.")
        return
    with db() as c:
        qualifying = promo_qualifying_participants(c)
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["telegram_id", "username", "name", "successful_purchases_in_period", "successful_referrals_in_period", "referral_telegram_ids"])
    for row in qualifying:
        writer.writerow([row["telegram_id"], row["username"], row["name"], row["successful_purchases"], row["successful_referrals"], ",".join(map(str,row["referral_telegram_ids"]))])
    document = BufferedInputFile(output.getvalue().encode("utf-8-sig"), filename="promo_participants_2026-10-16_21.csv")
    await m.answer_document(document, caption=(
        f"📋 Подходящих участников: {len(qualifying)}\n"
        "Период: 16.10.2026 00:00 — 21.10.2026 13:00 МСК.\n"
        "Условия: участник сам совершил успешную покупку в периоде и минимум один приглашённый друг совершил успешную покупку в периоде.\n"
        "Это предварительная выгрузка. Для жеребьёвки используй /promo_draw после проверки списка."
    ))


@dp.message(Command("promo_draw"))
async def promo_draw(m: Message):
    """Select and save three distinct winners; only show them to the admin."""
    if not m.from_user or not is_admin(m.from_user.id):
        await m.answer("⛔ Эта команда доступна только администратору.")
        return
    if not promo_is_closed():
        await m.answer("⏳ Жеребьёвка доступна после 21 октября 2026 года, 13:00 по Москве.")
        return
    with db() as c:
        existing = c.execute("SELECT * FROM promo_draws WHERE event_key=?", (PROMO_EVENT_KEY,)).fetchone()
        if existing:
            await m.answer("Жеребьёвка уже сохранена. Новые имена не выбираю. Используй /promo_result для просмотра результата.")
            return
        participants = promo_qualifying_participants(c)
        if len(participants) < 3:
            await m.answer(f"❌ Подходящих участников: {len(participants)}. Нужно минимум 3 для выбора трёх разных победителей. Новая жеребьёвка не проводилась.")
            return
        winners = secrets.SystemRandom().sample(participants, 3)
        winners_payload=[]
        for i, winner in enumerate(winners):
            winners_payload.append({**winner, "place": i+1, "prize": PROMO_PRIZES[i]})
        c.execute("INSERT INTO promo_draws(event_key,participants_json,winners_json,status,drawn_by) VALUES(?,?,?,?,?)",
                  (PROMO_EVENT_KEY,json.dumps(participants,ensure_ascii=False),json.dumps(winners_payload,ensure_ascii=False),"awaiting_confirmation",m.from_user.id))
    audit(m.from_user.id, "promo_draw", f"participants={len(participants)}; winners=3; event={PROMO_EVENT_KEY}")
    lines=[f"🎲 <b>Результаты жеребьёвки — проверка администратором</b>",f"Участников: {len(participants)}","Период: 16.10.2026 00:00 — 21.10.2026 13:00 МСК.",""]
    for w in winners_payload:
        username = ("@"+w["username"]) if w["username"] else "username не указан"
        lines.append(f"<b>{w['place']}-е место — {html.escape(w['prize'])}</b>")
        lines.append(f"Имя: {html.escape(w['name'] or 'Не указано')} | {html.escape(username)} | ID: <code>{w['telegram_id']}</code>")
    lines += ["", "Победители пока НЕ уведомлены.", "Проверь список и отправь /promo_confirm только когда готов разослать сообщения."]
    await m.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("promo_result"))
async def promo_result(m: Message):
    if not m.from_user or not is_admin(m.from_user.id):
        await m.answer("⛔ Эта команда доступна только администратору.")
        return
    with db() as c:
        draw = c.execute("SELECT * FROM promo_draws WHERE event_key=?", (PROMO_EVENT_KEY,)).fetchone()
    if not draw:
        await m.answer("Жеребьёвка ещё не проводилась. После окончания конкурса используй /promo_participants, проверь CSV, затем /promo_draw.")
        return
    winners=json.loads(draw["winners_json"] or "[]")
    lines=["🎲 <b>Сохранённые результаты жеребьёвки</b>",f"Статус: {html.escape(draw['status'])}",""]
    for w in winners:
        username=("@"+w["username"]) if w.get("username") else "username не указан"
        lines.append(f"<b>{w['place']}-е место — {html.escape(w['prize'])}</b>")
        lines.append(f"Имя: {html.escape(w.get('name') or 'Не указано')} | {html.escape(username)} | ID: <code>{w['telegram_id']}</code>")
    await m.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("promo_confirm"))
async def promo_confirm(m: Message):
    """Admin-only delivery with a claim lock and per-winner retry tracking."""
    if not m.from_user or not is_admin(m.from_user.id):
        await m.answer("⛔ Эта команда доступна только администратору.")
        return

    # Claim the draw atomically so two admins/clicks cannot start two broadcasts.
    with db() as c:
        draw = c.execute("SELECT * FROM promo_draws WHERE event_key=?", (PROMO_EVENT_KEY,)).fetchone()
        if not draw:
            await m.answer("Сначала проведи жеребьёвку командой /promo_draw.")
            return
        if draw["status"] == "confirmed":
            await m.answer("Результаты уже подтверждены. Повторно сообщения не отправляю.")
            return
        if draw["status"] == "sending":
            await m.answer("Рассылка уже выполняется. Подожди завершения и проверь /promo_result.")
            return
        winners = json.loads(draw["winners_json"] or "[]")
        claim = c.execute(
            "UPDATE promo_draws SET status='sending',confirmed_by=?,confirmed_at=CURRENT_TIMESTAMP "
            "WHERE event_key=? AND status='awaiting_confirmation'",
            (m.from_user.id, PROMO_EVENT_KEY),
        )
        if claim.rowcount != 1:
            await m.answer("Не удалось начать рассылку: статус жеребьёвки уже изменился. Проверь /promo_result.")
            return

    sent = []
    failed = []
    for w in winners:
        # Successful deliveries are saved immediately, so a normal retry only
        # attempts recipients who have not yet received their notification.
        if w.get("notified"):
            continue
        msg = (f"🎉 Поздравляем! Ты выиграл(а) в конкурсе!\n\n"
               f"🎁 Твой приз: {w['prize']}\n\n"
               "Чтобы получить приз, напиши в личные сообщения @XDarryanaX.")
        try:
            await bot.send_message(int(w["telegram_id"]), msg)
            w["notified"] = True
            sent.append(w)
            with db() as c:
                c.execute("UPDATE promo_draws SET winners_json=? WHERE event_key=? AND status='sending'",
                          (json.dumps(winners, ensure_ascii=False), PROMO_EVENT_KEY))
        except Exception as exc:
            failed.append((w, str(exc)[:160]))

    final_status = "awaiting_confirmation" if failed else "confirmed"
    with db() as c:
        c.execute(
            "UPDATE promo_draws SET status=?,confirmed_by=?,confirmed_at=? WHERE event_key=? AND status='sending'",
            (final_status, m.from_user.id if not failed else None,
             datetime.now(timezone.utc).isoformat() if not failed else None, PROMO_EVENT_KEY),
        )
    audit(m.from_user.id, "promo_confirm", f"sent={len(sent)}; failed={len(failed)}; event={PROMO_EVENT_KEY}")
    lines = ["📨 <b>Рассылка результатов конкурса завершена</b>",
             f"Успешно отправлено сейчас: {len(sent)}",
             f"Ранее отправлено: {sum(1 for w in winners if w.get('notified')) - len(sent)}",
             f"Не удалось отправить: {len(failed)}"]
    if failed:
        lines.append("\nНе удалось написать этим победителям (возможно, они не запускали бота). Можно повторить /promo_confirm после проверки:")
        for w, err in failed:
            lines.append(f"ID <code>{w['telegram_id']}</code> — {html.escape(err)}")
    elif not sent:
        lines.append("Все уведомления уже были отмечены как отправленные; повторной рассылки не было.")
    await m.answer("\n".join(lines), parse_mode="HTML")

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

        # Bonus-spin rewards do not have media to open. Credit once and mark
        # claimed=2 so they disappear from the pending "Мои призы" list.
        prize = c.execute("SELECT * FROM prizes WHERE id=?", (row["prize_id"],)).fetchone()
        prize_type = str((row["prize_type"] or (prize["prize_type"] if prize else "")) or "").lower()
        label = str(row["prize_label"] or (prize["name"] if prize else "") or "")
        label_norm = label.lower().replace("ё", "е")
        # Legacy/admin-created bonus rewards may incorrectly be stored as
        # prize_type='points'. Detect them by their explicit label too.
        is_spin_bonus = (
            prize_type in {"spins", "spin", "bonus_spins"}
            or ("вращени" in label_norm and re.search(r"\+?\s*\d+", label_norm) is not None)
        )
        if is_spin_bonus:
            # The explicit +N in the label is authoritative for legacy rows.
            match = re.search(r"\+?\s*(\d+)", label_norm)
            amount = int(match.group(1)) if match else 0
            if amount < 1 and prize:
                try:
                    amount = int(prize["points"] or 0)
                except (TypeError, ValueError):
                    amount = 0
            if amount < 1:
                return False, "Не удалось определить количество бонусных вращений. Напиши администратору."
            update = c.execute(
                "UPDATE spins SET claimed=2 WHERE id=? AND telegram_id=? AND claimed=0",
                (spin_id, uid),
            )
            if update.rowcount != 1:
                return False, "Этот приз уже обработан"
            c.execute("UPDATE users SET spins=spins+? WHERE telegram_id=?", (amount, uid))
            balance_row = c.execute("SELECT spins FROM users WHERE telegram_id=?", (uid,)).fetchone()
            balance = int(balance_row["spins"] if balance_row else 0)
            try:
                await bot.send_message(
                    uid,
                    f"🎉 <b>Бонус начислен!</b>\\n\\n"
                    f"🎰 Добавлено вращений: <b>{amount}</b>\\n"
                    f"💰 Твой баланс: <b>{balance}</b> вращений",
                    parse_mode="HTML",
                )
            except Exception as exc:
                # The reward is already credited and closed; do not roll it back
                # or a retry could credit the same bonus twice.
                print(f"Could not notify user {uid} about bonus spins {spin_id}: {type(exc).__name__}: {exc}")
            return True, f"Начислено {amount} вращений. Баланс: {balance}."
        value = str(row["delivery_value"] or "").strip()
        # For photo/video packs, the attached prize media is authoritative. Older
        # spin rows may contain only the legacy single-file delivery_value.
        prize = c.execute("SELECT * FROM prizes WHERE id=?", (row["prize_id"],)).fetchone()
        if prize:
            name_norm = str(prize["name"] or row["prize_label"] or "").lower().replace("ё", "е")
            # Accept both digits and Russian number words (e.g. "пакет из трех видео").
            match_count = re.search(
                r"(?:(?:набор|пакет)\s+из\s+)?(10|5|3|1|десяти|пяти|трех|одного|одна|один)\s*(?:фото|фотограф\w*|фотк\w*|видео|видос\w*|шт)",
                name_norm,
            )
            count_token = match_count.group(1) if match_count else "1"
            wanted = {"десяти": 10, "пяти": 5, "трех": 3, "одного": 1, "одна": 1, "один": 1}.get(count_token, int(count_token) if count_token.isdigit() else 1)
            existing_media = []
            try:
                parsed_value = json.loads(value) if value else {}
                if isinstance(parsed_value, dict) and isinstance(parsed_value.get("media_files"), list):
                    existing_media = parsed_value["media_files"]
            except (ValueError, TypeError):
                pass
            kind_wanted = "video" if "видео" in name_norm or "video" in name_norm else "photo"
            existing_matching = [x for x in existing_media if isinstance(x, dict) and x.get("url") and ("photo" if x.get("type") == "image" else x.get("type")) == kind_wanted]
            prize_media = []
            try:
                prize_media = json.loads(prize["media_files"] or "[]")
            except (ValueError, TypeError):
                prize_media = []
            valid = [x for x in prize_media if isinstance(x, dict) and (x.get("url") or x.get("file_id")) and x.get("type") in {"photo", "image", "video"}]
            matching = [x for x in valid if ("photo" if x.get("type") == "image" else x.get("type")) == kind_wanted]
            if matching:
                valid = matching

            # For video prizes, media_files is the preferred pool. If none was
            # uploaded, allow multiple Telegram file_ids / URLs in delivery_value,
            # one per line or separated by semicolons. Select randomly per win.
            raw_delivery = str(prize["delivery_value"] or "").strip()
            if kind_wanted == "video" and not valid and raw_delivery:
                has_media_payload = False
                try:
                    parsed_delivery = json.loads(raw_delivery)
                    has_media_payload = isinstance(parsed_delivery, dict) and isinstance(parsed_delivery.get("media_files"), list)
                    if has_media_payload:
                        valid = [
                            {"url": str(x.get("url") or x.get("file_id") or "").strip(), "type": "video"}
                            for x in parsed_delivery["media_files"]
                            if isinstance(x, dict) and str(x.get("url") or x.get("file_id") or "").strip()
                            and str(x.get("type") or "video").lower() in {"video", "видео"}
                        ]
                except (ValueError, TypeError):
                    pass
                if not valid and not has_media_payload:
                    candidates = [part.strip() for line in raw_delivery.splitlines() for part in line.split(";") if part.strip()]
                    valid = [{"url": item, "type": "video"} for item in candidates]
            if valid and (wanted > 1 or kind_wanted == "video"):
                pick_count = min(wanted, len(valid)) if wanted > 1 else 1
                chosen_media = secrets.SystemRandom().sample(valid, pick_count)
                value = json.dumps({"media_files": chosen_media}, ensure_ascii=False)
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
            # claimed=3 means unavailable/misconfigured; it is excluded from the
            # user's pending prize list but remains in history for the admin.
            c.execute(
                "UPDATE spins SET claimed=3 WHERE id=? AND telegram_id=? AND claimed=0",
                (spin_id, uid),
            )
            return False, "Этот приз недоступен: содержимое не настроено. Он убран из ожидающих; напиши администратору."
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

async def prepare_telegram_media(value: str, index: int, fallback_ext: str):
    """Download HTTP media ourselves, then upload bytes to Telegram instead of asking Telegram to fetch a URL."""
    value = str(value or "").strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        # Telegram file_id or another supported Telegram media identifier.
        return value

    import urllib.request
    def _download():
        request = urllib.request.Request(value, headers={"User-Agent": "TelegramRouletteBot/1.0"})
        with urllib.request.urlopen(request, timeout=45) as response:
            data = response.read(25 * 1024 * 1024 + 1)
            content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].lower()
        if not data:
            raise ValueError("Ссылка вернула пустой файл")
        if len(data) > 25 * 1024 * 1024:
            raise ValueError("Файл больше 25 МБ")
        return data, content_type

    data, content_type = await asyncio.to_thread(_download)
    path = parsed.path.lower()
    ext = Path(path).suffix
    if ext not in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".webm", ".mov"}:
        ext = ".mp4" if content_type.startswith("video/") else fallback_ext
    return BufferedInputFile(data, filename=f"prize_{index}{ext}")


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
        if not value or value in {"[]", "{}", "null"}:
            c.execute(
                "UPDATE spins SET claimed=3 WHERE id=? AND telegram_id=? AND claimed=1",
                (spin_id, cb.from_user.id),
            )
            await cb.answer("Приз недоступен: содержимое не настроено. Он убран из ожидающих; напиши администратору.", show_alert=True)
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
            photos = []
            videos = []
            for item in media_payload:
                if not isinstance(item, dict):
                    continue
                media_value = (item.get("url") or item.get("file_id") or "").strip()
                media_type = str(item.get("type", "")).lower().strip()
                if not media_value:
                    continue
                if media_type in {"photo", "image", "картинка", "фото"} or media_type.startswith("image/"):
                    photos.append(media_value)
                elif media_type in {"video", "видео"} or media_type.startswith("video/"):
                    videos.append(media_value)
            if not photos and not videos:
                raise ValueError("В призе нет файлов для отправки")
            # Fetch web URLs in the bot process and upload bytes to Telegram. This avoids
            # Telegram being unable to fetch private/slow B2-backed media URLs itself.
            prepared_photos = []
            for photo_index, photo_url in enumerate(photos, start=1):
                prepared_photos.append(await prepare_telegram_media(photo_url, photo_index, ".jpg"))
            for start in range(0, len(prepared_photos), 10):
                batch = prepared_photos[start:start+10]
                if len(batch) == 1:
                    await bot.send_photo(cb.from_user.id, batch[0], caption=f"📸 {row['prize_label']}")
                else:
                    media = [InputMediaPhoto(media=file_obj, caption=(f"📸 {row['prize_label']}" if i == 0 and start == 0 else None)) for i, file_obj in enumerate(batch)]
                    try:
                        await bot.send_media_group(cb.from_user.id, media)
                    except Exception as album_exc:
                        print(f"send_media_group failed for spin {spin_id}: {type(album_exc).__name__}: {album_exc}")
                        for photo_index, photo_file in enumerate(batch):
                            caption = f"📸 {row['prize_label']}" if start == 0 and photo_index == 0 else None
                            await bot.send_photo(cb.from_user.id, photo_file, caption=caption)
            for video_index, url in enumerate(videos, start=1):
                video_file = await prepare_telegram_media(url, video_index, ".mp4")
                await bot.send_video(cb.from_user.id, video_file, caption=f"🎬 {row['prize_label']}" if video_index == 1 else None)
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
        elif kind in {"voice", "audio_message", "голосовое", "гс"}:
            await bot.send_voice(cb.from_user.id, value, caption=f"🎉 Твой приз: {row['prize_label']}")
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
    except Exception as exc:
        print(f"Prize delivery failed for spin {spin_id}, user {cb.from_user.id}: {type(exc).__name__}: {exc}")
        # The user asked not to leave broken gifts hanging in the pending list.
        # Keep the history row for admins, but mark the gift unavailable/consumed.
        with db() as c:
            c.execute("UPDATE spins SET claimed=3 WHERE id=? AND telegram_id=? AND claimed=2", (spin_id, cb.from_user.id))
        try:
            await cb.message.edit_text("⚠️ Приз закрыт: не удалось отправить содержимое. Он убран из ожидающих; напиши администратору.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🎰 Крутить", web_app=WebAppInfo(url=WEBAPP_URL)),
                InlineKeyboardButton(text="🎁 Мои призы", callback_data="myprizes")
            ]]))
        except Exception:
            pass
        await cb.answer("Приз закрыт и убран из ожидающих: отправка не удалась.", show_alert=True)

@dp.message(F.voice | F.video_note | F.video | F.document)
async def admin_video_file_id(m: Message):
    if not m.from_user or not is_admin(m.from_user.id):
        return

    if m.voice:
        file_id = m.voice.file_id
        kind = "голосовое сообщение"
        prize_type = "voice"
        size = m.voice.file_size or 0
        hint = "В админке выбери тип выдачи «Голосовое» (voice) и вставь file_id в «Содержимое приза»."
    elif m.video_note:
        file_id = m.video_note.file_id
        kind = "кружок"
        prize_type = "video_note"
        size = m.video_note.file_size or 0
        hint = "В админке выбери тип выдачи «Кружок» (video_note) и вставь file_id в «Содержимое приза»."
    elif m.video:
        file_id = m.video.file_id
        kind = "видео"
        prize_type = "video"
        size = m.video.file_size or 0
        hint = "В админке выбери тип выдачи «Видео» и вставь file_id в «Содержимое приза»."
    elif m.document and (m.document.mime_type or "").startswith("video/"):
        file_id = m.document.file_id
        kind = "видеофайл"
        prize_type = "video"
        size = m.document.file_size or 0
        hint = "В админке выбери тип выдачи «Видео» и вставь file_id в «Содержимое приза»."
    else:
        return

    safe_file_id = html.escape(file_id)
    await m.answer(
        f"✅ {kind.capitalize()} принят!\n\n"
        f"<code>{safe_file_id}</code>\n\n"
        f"Тип приза: <b>{prize_type}</b>\n"
        f"Размер: {size / 1024 / 1024:.1f} МБ\n\n"
        f"{hint}\nБот отправит его через Telegram без загрузки на сервер.",
        parse_mode="HTML",
    )

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
    if cur.rowcount and p.currency == "XTR":
        await notify_admins_star_payment(m.from_user, p.total_amount, n)
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
