"""База данных (один файл data.db). Каждый салон хранится один раз — по его ID из Google.

Статусы салона:
  filtered_out — не прошёл фильтр (сохраняем, чтобы не проверять повторно)
  new          — подходит, текст ещё не готов
  ready        — текст готов, ждёт /next
  shown        — показан вам, вы ещё не нажали кнопку
  written      — «написал»
  skipped      — «пропустить»
  rejected     — «не подходит»
"""
import sqlite3
import sys
import threading

# Чтобы русские и армянские буквы нормально печатались в консоли Windows
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass
from datetime import datetime

DB_FILE = "data.db"

_lock = threading.Lock()
_conn = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS places (
    id            TEXT PRIMARY KEY,
    niche         TEXT,
    name          TEXT,
    address       TEXT,
    phone         TEXT,
    maps_url      TEXT,
    social_url    TEXT,
    rating        REAL,
    reviews_count INTEGER,
    last_review   TEXT,
    reason        TEXT,
    category      TEXT,
    text_hy       TEXT,
    text_ru       TEXT,
    status        TEXT,
    found_at      TEXT,
    updated_at    TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS search_jobs (
    niche      TEXT,
    query      TEXT,
    page_token TEXT,
    done       INTEGER DEFAULT 0,
    PRIMARY KEY (niche, query)
);
"""


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def connect(path=DB_FILE):
    global _conn
    _conn = sqlite3.connect(path, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.executescript(SCHEMA)
    try:  # база, созданная до появления колонки category
        _conn.execute("ALTER TABLE places ADD COLUMN category TEXT")
    except sqlite3.OperationalError:
        pass
    try:  # база, созданная до появления регионов: тогда все салоны были из Еревана
        _conn.execute("ALTER TABLE places ADD COLUMN city TEXT")
        _conn.execute("UPDATE places SET city = 'yerevan' WHERE city IS NULL")
    except sqlite3.OperationalError:
        pass
    _conn.commit()


# ---------- настройки (например, выбранные регионы) ----------

def get_setting(key):
    row = one("SELECT value FROM settings WHERE key = ?", (key,))
    return row["value"] if row else None


def set_setting(key, value):
    run("INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def _in_cities(cities):
    """Кусок SQL «AND city IN (...)» и параметры для него. cities=None — без фильтра."""
    if cities is None:
        return "", ()
    if not cities:
        return " AND 0", ()
    return f" AND city IN ({','.join('?' * len(cities))})", tuple(cities)


def run(sql, params=()):
    """Выполнить запрос, который что-то меняет."""
    with _lock:
        cur = _conn.execute(sql, params)
        _conn.commit()
        return cur.rowcount


def one(sql, params=()):
    with _lock:
        return _conn.execute(sql, params).fetchone()


def many(sql, params=()):
    with _lock:
        return _conn.execute(sql, params).fetchall()


# ---------- салоны ----------

def place_exists(place_id):
    return one("SELECT 1 FROM places WHERE id = ?", (place_id,)) is not None


def phone_taken(niche, phone):
    """Тот же салон иногда есть на карте дважды — узнаём его по телефону."""
    return bool(phone) and one(
        "SELECT 1 FROM places WHERE niche = ? AND phone = ? AND status != 'filtered_out'",
        (niche, phone)) is not None


def add_place(niche, info, status):
    """Добавить салон. Если он уже есть — ничего не делаем (никаких повторов)."""
    return run(
        """INSERT OR IGNORE INTO places
           (id, niche, name, address, phone, maps_url, social_url, rating,
            reviews_count, last_review, reason, category, city, status, found_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (info["id"], niche, info.get("name"), info.get("address"), info.get("phone"),
         info.get("maps_url"), info.get("social_url"), info.get("rating"),
         info.get("reviews_count"), info.get("last_review"), info.get("reason"),
         info.get("category"), info.get("city"), status, now(), now()),
    )


def get_place(place_id):
    return one("SELECT * FROM places WHERE id = ?", (place_id,))


def set_status(place_id, status):
    run("UPDATE places SET status = ?, updated_at = ? WHERE id = ?", (status, now(), place_id))


def change_status(place_id, old, new):
    """Сменить статус, только если он сейчас равен old. Возвращает True, если сменили."""
    return run("UPDATE places SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
               (new, now(), place_id, old)) > 0


def recent_texts(niche, lang, n=3):
    """Последние написанные тексты — чтобы новый не повторял их обороты."""
    col = "text_hy" if lang == "hy" else "text_ru"
    rows = many(f"SELECT {col} AS t FROM places WHERE niche = ? AND {col} IS NOT NULL "
                f"ORDER BY updated_at DESC LIMIT ?", (niche, n))
    return [r["t"] for r in rows]


def set_text(place_id, lang, text):
    column = "text_hy" if lang == "hy" else "text_ru"
    run(f"UPDATE places SET {column} = ?, updated_at = ? WHERE id = ?", (text, now(), place_id))


def first_with_status(niche, status, cities=None):
    """Первый салон с этим статусом. cities — список ID городов (только из них) или None (из всех)."""
    extra, params = _in_cities(cities)
    return one(
        f"SELECT * FROM places WHERE niche = ? AND status = ?{extra} ORDER BY found_at LIMIT 1",
        (niche, status, *params),
    )


def count_status(niche, status, cities=None):
    extra, params = _in_cities(cities)
    return one(f"SELECT COUNT(*) FROM places WHERE niche = ? AND status = ?{extra}",
               (niche, status, *params))[0]


def stats(niche, cities=None):
    extra, params = _in_cities(cities)
    rows = many(f"SELECT status, COUNT(*) AS n FROM places WHERE niche = ?{extra} GROUP BY status",
                (niche, *params))
    return {r["status"]: r["n"] for r in rows}


# ---------- поисковые задания ----------

def add_job(niche, query):
    run("INSERT OR IGNORE INTO search_jobs (niche, query) VALUES (?, ?)", (niche, query))


def next_job(niche):
    return one("SELECT * FROM search_jobs WHERE niche = ? AND done = 0 ORDER BY rowid LIMIT 1", (niche,))


def update_job(niche, query, page_token):
    done = 0 if page_token else 1
    run("UPDATE search_jobs SET page_token = ?, done = ? WHERE niche = ? AND query = ?",
        (page_token, done, niche, query))


def jobs_left(niche):
    return one("SELECT COUNT(*) FROM search_jobs WHERE niche = ? AND done = 0", (niche,))[0]

