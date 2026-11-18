import os
import sqlite3
import time
import threading

from . import crypto

from .paths import root as _root

ROOT = _root()
DATA_DIR = os.path.join(ROOT, "data")
DB_PATH = os.path.join(DATA_DIR, "chujian.db")

_lock = threading.Lock()


def _category(name):
    from .classify import classify

    return classify(name)

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS credential (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        platform TEXT NOT NULL,
        account TEXT NOT NULL,
        access_token TEXT,
        refresh_token TEXT,
        user_id TEXT,
        extra TEXT,
        status TEXT DEFAULT 'ok',
        expires_at INTEGER,
        updated_at INTEGER,
        UNIQUE(platform, account)
    )""",
    """CREATE TABLE IF NOT EXISTS price_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        platform TEXT NOT NULL,
        sku TEXT NOT NULL,
        name TEXT,
        price REAL,
        origin_price REAL,
        qty REAL,
        tags TEXT,
        image TEXT,
        ts INTEGER
    )""",
    """CREATE INDEX IF NOT EXISTS idx_price_sku ON price_history(platform, sku, ts)""",
    """CREATE TABLE IF NOT EXISTS alert (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        platform TEXT NOT NULL,
        sku TEXT NOT NULL,
        name TEXT,
        old_price REAL,
        new_price REAL,
        kind TEXT,
        status TEXT DEFAULT 'pending',
        created_at INTEGER
    )""",
    """CREATE TABLE IF NOT EXISTS capture (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        host TEXT,
        method TEXT,
        path TEXT,
        status INTEGER,
        note TEXT,
        ts INTEGER,
        UNIQUE(host, method, path)
    )""",
    """CREATE TABLE IF NOT EXISTS monitor_state (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        enabled INTEGER DEFAULT 1,
        started_at INTEGER
    )""",
    """CREATE TABLE IF NOT EXISTS active_address (
        platform TEXT PRIMARY KEY,
        data TEXT,
        ts INTEGER
    )""",
    """CREATE TABLE IF NOT EXISTS header_profile (
        platform TEXT PRIMARY KEY,
        headers TEXT,
        ts INTEGER
    )""",
    """CREATE TABLE IF NOT EXISTS watch (
        platform TEXT NOT NULL,
        sku TEXT NOT NULL,
        ts INTEGER,
        PRIMARY KEY (platform, sku)
    )""",
    """CREATE TABLE IF NOT EXISTS submit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id TEXT,
        item_count INTEGER,
        status TEXT DEFAULT 'open',
        ts INTEGER
    )""",
    """CREATE TABLE IF NOT EXISTS cart_now (
        platform TEXT NOT NULL,
        sku TEXT NOT NULL,
        ts INTEGER,
        PRIMARY KEY (platform, sku)
    )""",
    """CREATE TABLE IF NOT EXISTS host_stat (
        host TEXT PRIMARY KEY,
        hits INTEGER DEFAULT 0,
        last_path TEXT,
        last_ts INTEGER
    )""",
    """CREATE TABLE IF NOT EXISTS scan_item (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        platform TEXT NOT NULL,
        sku TEXT NOT NULL,
        name TEXT,
        price REAL,
        origin_price REAL,
        image TEXT,
        discount REAL,
        source TEXT,
        tags TEXT,
        product_id TEXT,
        store_product_id TEXT,
        activity_id TEXT,
        category TEXT,
        in_cart INTEGER DEFAULT 0,
        first_seen INTEGER,
        last_seen INTEGER,
        UNIQUE(platform, sku)
    )""",
    """CREATE TABLE IF NOT EXISTS replay (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        platform TEXT NOT NULL,
        key TEXT NOT NULL,
        url TEXT,
        method TEXT,
        headers TEXT,
        body TEXT,
        ts INTEGER,
        UNIQUE(platform, key)
    )""",
]


def _connect():
    os.makedirs(DATA_DIR, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init():
    with _lock:
        c = _connect()
        try:
            for sql in SCHEMA:
                c.execute(sql)
            scols = [r[1] for r in c.execute("PRAGMA table_info(scan_item)").fetchall()]
            if "in_cart" not in scols:
                c.execute("ALTER TABLE scan_item ADD COLUMN in_cart INTEGER DEFAULT 0")
            if "category" not in scols:
                c.execute("ALTER TABLE scan_item ADD COLUMN category TEXT")
            # 回填历史数据
            from .classify import classify as _cls

            todo = c.execute("SELECT id, name FROM scan_item WHERE category IS NULL").fetchall()
            for _id, _name in todo:
                c.execute("UPDATE scan_item SET category=? WHERE id=?", (_cls(_name), _id))
            for col in ("product_id", "store_product_id", "activity_id"):
                if col not in scols:
                    c.execute(f"ALTER TABLE scan_item ADD COLUMN {col} TEXT")
            cols = [r[1] for r in c.execute("PRAGMA table_info(price_history)").fetchall()]
            if "image" not in cols:
                c.execute("ALTER TABLE price_history ADD COLUMN image TEXT")
            if not c.execute("SELECT COUNT(*) FROM monitor_state").fetchone()[0]:
                c.execute("INSERT INTO monitor_state (enabled) VALUES (1)")
            c.commit()
        finally:
            c.close()


# ---------- 凭证 ----------

def upsert_credential(platform, account, access_token=None, refresh_token=None,
                      user_id=None, extra=None, expires_at=None):
    now = int(time.time())
    with _lock:
        c = _connect()
        try:
            row = c.execute(
                "SELECT * FROM credential WHERE platform=? AND account=?", (platform, account)
            ).fetchone()
            if row:
                c.execute(
                    """UPDATE credential SET
                        access_token=COALESCE(?, access_token),
                        refresh_token=COALESCE(?, refresh_token),
                        user_id=COALESCE(?, user_id),
                        extra=COALESCE(?, extra),
                        expires_at=COALESCE(?, expires_at),
                        status='ok', updated_at=?
                       WHERE id=?""",
                    (
                        crypto.enc(access_token) if access_token else None,
                        crypto.enc(refresh_token) if refresh_token else None,
                        user_id,
                        extra,
                        expires_at,
                        now,
                        row["id"],
                    ),
                )
                rid = row["id"]
            else:
                cur = c.execute(
                    """INSERT INTO credential
                       (platform, account, access_token, refresh_token, user_id, extra, expires_at, status, updated_at)
                       VALUES (?,?,?,?,?,?,?, 'ok', ?)""",
                    (
                        platform,
                        account,
                        crypto.enc(access_token),
                        crypto.enc(refresh_token),
                        user_id,
                        extra,
                        expires_at,
                        now,
                    ),
                )
                rid = cur.lastrowid
            c.commit()
            return rid
        finally:
            c.close()


def list_credentials(platform=None):
    with _lock:
        c = _connect()
        try:
            sql = "SELECT * FROM credential"
            args = ()
            if platform:
                sql += " WHERE platform=?"
                args = (platform,)
            rows = c.execute(sql, args).fetchall()
        finally:
            c.close()
    out = []
    for r in rows:
        d = dict(r)
        d["access_token"] = crypto.dec(d.get("access_token"))
        d["refresh_token"] = crypto.dec(d.get("refresh_token"))
        out.append(d)
    return out


def set_status(cred_id, status):
    with _lock:
        c = _connect()
        try:
            c.execute("UPDATE credential SET status=?, updated_at=? WHERE id=?",
                      (status, int(time.time()), cred_id))
            c.commit()
        finally:
            c.close()


def delete_credential(cred_id):
    with _lock:
        c = _connect()
        try:
            c.execute("DELETE FROM credential WHERE id=?", (cred_id,))
            c.commit()
        finally:
            c.close()


# ---------- 价格 ----------

def last_price(platform, sku):
    with _lock:
        c = _connect()
        try:
            r = c.execute(
                "SELECT price FROM price_history WHERE platform=? AND sku=? ORDER BY ts DESC LIMIT 1",
                (platform, sku),
            ).fetchone()
            return r["price"] if r else None
        finally:
            c.close()


def log_price(platform, sku, name, price, origin_price=None, qty=None, tags=None, image=None):
    with _lock:
        c = _connect()
        try:
            c.execute(
                """INSERT INTO price_history (platform, sku, name, price, origin_price, qty, tags, image, ts)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (platform, sku, name, price, origin_price, qty,
                 ",".join(tags or []) if isinstance(tags, list) else (tags or ""), image, int(time.time())),
            )
            c.commit()
        finally:
            c.close()


def price_series(platform, sku, limit=200):
    with _lock:
        c = _connect()
        try:
            rows = c.execute(
                """SELECT price, origin_price, ts FROM price_history
                   WHERE platform=? AND sku=? ORDER BY ts DESC LIMIT ?""",
                (platform, sku, limit),
            ).fetchall()
            return [dict(r) for r in rows][::-1]
        finally:
            c.close()


def last_refresh_ts():
    with _lock:
        c = _connect()
        try:
            r = c.execute("SELECT MAX(ts) FROM price_history").fetchone()
            return int(r[0] or 0)
        finally:
            c.close()


def log_submit(order_id, item_count):
    with _lock:
        c = _connect()
        try:
            c.execute(
                "INSERT INTO submit_log (order_id, item_count, status, ts) VALUES (?,?, 'open', ?)",
                (order_id, item_count, int(time.time())),
            )
            c.commit()
        finally:
            c.close()


def last_submit():
    with _lock:
        c = _connect()
        try:
            r = c.execute("SELECT * FROM submit_log ORDER BY id DESC LIMIT 1").fetchone()
            return dict(r) if r else None
        finally:
            c.close()


def mark_submit(order_id, status):
    with _lock:
        c = _connect()
        try:
            c.execute("UPDATE submit_log SET status=? WHERE order_id=?", (status, order_id))
            c.commit()
        finally:
            c.close()


def replace_cart(platform, skus):
    """记录「此刻购物车里到底有哪些」，用于把已删除/已下单的商品从界面摘掉。"""
    now = int(time.time())
    with _lock:
        c = _connect()
        try:
            c.execute("DELETE FROM cart_now WHERE platform=?", (platform,))
            c.executemany(
                "INSERT OR REPLACE INTO cart_now (platform, sku, ts) VALUES (?,?,?)",
                [(platform, str(k), now) for k in skus],
            )
            c.commit()
        finally:
            c.close()


def latest_snapshot(limit=100):
    with _lock:
        c = _connect()
        try:
            rows = c.execute(
                """SELECT p.* FROM price_history p
                   JOIN (SELECT platform, sku, MAX(ts) AS mts FROM price_history GROUP BY platform, sku) t
                     ON p.platform=t.platform AND p.sku=t.sku AND p.ts=t.mts
                   JOIN cart_now n ON n.platform=p.platform AND n.sku=p.sku
                   ORDER BY p.ts DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            c.close()


# ---------- 告警 ----------

def add_alert(platform, sku, name, old_price, new_price, kind):
    with _lock:
        c = _connect()
        try:
            cur = c.execute(
                """INSERT INTO alert (platform, sku, name, old_price, new_price, kind, status, created_at)
                   VALUES (?,?,?,?,?,?, 'pending', ?)""",
                (platform, sku, name, old_price, new_price, kind, int(time.time())),
            )
            c.commit()
            return cur.lastrowid
        finally:
            c.close()


def last_alert(platform, sku):
    with _lock:
        c = _connect()
        try:
            r = c.execute(
                "SELECT * FROM alert WHERE platform=? AND sku=? ORDER BY id DESC LIMIT 1",
                (platform, sku),
            ).fetchone()
            return dict(r) if r else None
        finally:
            c.close()


def ack_alert(alert_id, status="acked"):
    with _lock:
        c = _connect()
        try:
            c.execute("UPDATE alert SET status=? WHERE id=?", (status, alert_id))
            c.commit()
        finally:
            c.close()


def recent_alerts(limit=30):
    with _lock:
        c = _connect()
        try:
            rows = c.execute("SELECT * FROM alert ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            c.close()


# ---------- 抓包记录 ----------

def record_capture(host, method, path, status=None, note=None):
    with _lock:
        c = _connect()
        try:
            c.execute(
                """INSERT INTO capture (host, method, path, status, note, ts) VALUES (?,?,?,?,?,?)
                   ON CONFLICT(host, method, path) DO UPDATE SET
                     status=excluded.status, note=excluded.note, ts=excluded.ts""",
                (host, method, path, status, note, int(time.time())),
            )
            c.commit()
        finally:
            c.close()


def list_captures(limit=200):
    with _lock:
        c = _connect()
        try:
            rows = c.execute(
                "SELECT * FROM capture ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            c.close()


# ---------- 流量诊断（只记域名，不记内容）----------

def bump_host(host, path=""):
    with _lock:
        c = _connect()
        try:
            c.execute(
                """INSERT INTO host_stat (host, hits, last_path, last_ts) VALUES (?,1,?,?)
                   ON CONFLICT(host) DO UPDATE SET
                     hits = hits + 1,
                     last_path = excluded.last_path,
                     last_ts = excluded.last_ts""",
                (host, (path or "")[:120], int(time.time())),
            )
            c.commit()
        finally:
            c.close()


def top_hosts(limit=40):
    with _lock:
        c = _connect()
        try:
            rows = c.execute(
                "SELECT * FROM host_stat ORDER BY hits DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            c.close()


def total_hits():
    with _lock:
        c = _connect()
        try:
            r = c.execute("SELECT COALESCE(SUM(hits),0) FROM host_stat").fetchone()
            return int(r[0] or 0)
        finally:
            c.close()


# ---------- 出清池 ----------

def upsert_scan(platform, items, source=""):
    """把商品收进出清池。

    items 可以是 Item，也可以是 dict（字段：sku/name/price/origin_price/image/tags/
    product_id/store_product_id/activity_id）。返回写入条数。
    """
    now = int(time.time())
    n = 0
    with _lock:
        c = _connect()
        try:
            for it in items:
                if isinstance(it, dict):
                    g = lambda k, d=None: it.get(k, d)
                    sku = g("sku") or g("store_product_id") or g("product_id")
                    name = g("name")
                    price = g("price")
                    origin = g("origin_price")
                    image = g("image")
                    tags = g("tags") or []
                    pid, spid, aid = g("product_id"), g("store_product_id"), g("activity_id")
                else:
                    sku, name, price = it.sku, it.name, it.price
                    origin, image = it.origin_price, it.image
                    tags = it.tags or []
                    raw = it.raw or {}
                    pid = raw.get("product_id")
                    spid = raw.get("store_product_id")
                    aid = raw.get("activity_id")
                if not sku or not name or not price:
                    continue
                disc = round(float(price) / float(origin), 4) if origin and float(origin) > 0 else 1.0
                c.execute(
                    """INSERT INTO scan_item
                       (platform, sku, name, price, origin_price, image, discount, source, tags,
                        product_id, store_product_id, activity_id, category, first_seen, last_seen)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(platform, sku) DO UPDATE SET
                         name=excluded.name,
                         price=excluded.price,
                         origin_price=COALESCE(excluded.origin_price, scan_item.origin_price),
                         image=COALESCE(excluded.image, scan_item.image),
                         discount=excluded.discount,
                         source=excluded.source,
                         tags=excluded.tags,
                         product_id=COALESCE(excluded.product_id, scan_item.product_id),
                         store_product_id=COALESCE(excluded.store_product_id, scan_item.store_product_id),
                         activity_id=COALESCE(excluded.activity_id, scan_item.activity_id),
                         category=excluded.category,
                         last_seen=excluded.last_seen""",
                    (
                        platform,
                        str(sku),
                        name,
                        float(price),
                        float(origin) if origin else None,
                        image,
                        disc,
                        (source or "")[:80],
                        ",".join(tags) if isinstance(tags, list) else (tags or ""),
                        pid,
                        spid,
                        aid,
                        _category(name),
                        now,
                        now,
                    ),
                )
                n += 1
            c.commit()
        finally:
            c.close()
    return n


def mark_in_cart(platform, product_ids):
    """把「已在购物车里」的标记刷一遍：先全清，再按 product_id 打上。"""
    with _lock:
        c = _connect()
        try:
            c.execute("UPDATE scan_item SET in_cart=0 WHERE platform=?", (platform,))
            ids = [p for p in (product_ids or []) if p]
            if ids:
                c.executemany(
                    "UPDATE scan_item SET in_cart=1 WHERE platform=? AND product_id=?",
                    [(platform, p) for p in ids],
                )
            c.commit()
        finally:
            c.close()


def save_headers(platform, headers):
    """存一份最新的请求头（登录态变了、换设备了都用这套）。"""
    import json as _json
    keep = {
        k: v for k, v in (headers or {}).items()
        if k.lower() not in ("content-length", "host", "connection", "accept-encoding")
    }
    with _lock:
        c = _connect()
        try:
            c.execute(
                """INSERT INTO header_profile (platform, headers, ts) VALUES (?,?,?)
                   ON CONFLICT(platform) DO UPDATE SET headers=excluded.headers, ts=excluded.ts""",
                (platform, crypto.enc(_json.dumps(keep, ensure_ascii=False)), int(time.time())),
            )
            c.commit()
        finally:
            c.close()


def get_headers(platform):
    import json as _json
    with _lock:
        c = _connect()
        try:
            r = c.execute("SELECT * FROM header_profile WHERE platform=?", (platform,)).fetchone()
        finally:
            c.close()
    if not r:
        return {}
    try:
        return _json.loads(crypto.dec(r["headers"]) or "{}")
    except Exception:
        return {}


def set_active_address(platform, data):
    import json as _json
    with _lock:
        c = _connect()
        try:
            c.execute(
                """INSERT INTO active_address (platform, data, ts) VALUES (?,?,?)
                   ON CONFLICT(platform) DO UPDATE SET data=excluded.data, ts=excluded.ts""",
                (platform, crypto.enc(_json.dumps(data, ensure_ascii=False)), int(time.time())),
            )
            c.commit()
        finally:
            c.close()


def get_active_address(platform):
    import json as _json
    with _lock:
        c = _connect()
        try:
            r = c.execute("SELECT * FROM active_address WHERE platform=?", (platform,)).fetchone()
        finally:
            c.close()
    if not r:
        return None
    try:
        return _json.loads(crypto.dec(r["data"]) or "{}")
    except Exception:
        return None


def add_watch(platform, sku):
    with _lock:
        c = _connect()
        try:
            c.execute(
                "INSERT OR REPLACE INTO watch (platform, sku, ts) VALUES (?,?,?)",
                (platform, str(sku), int(time.time())),
            )
            c.commit()
        finally:
            c.close()


def del_watch(platform, sku):
    with _lock:
        c = _connect()
        try:
            c.execute("DELETE FROM watch WHERE platform=? AND sku=?", (platform, str(sku)))
            c.commit()
        finally:
            c.close()


def list_watch(platform=None):
    with _lock:
        c = _connect()
        try:
            if platform:
                rows = c.execute("SELECT * FROM watch WHERE platform=?", (platform,)).fetchall()
            else:
                rows = c.execute("SELECT * FROM watch").fetchall()
            return [dict(r) for r in rows]
        finally:
            c.close()


def replace_scan(platform, items, source="疯狂折扣"):
    """全量替换出清池：写入本次抓到的，删掉本次没出现的。

    只在「完整抓取成功」之后调用——中途失败时不要清，否则会误删一半。
    分两步各自加锁，避免 _lock 嵌套死锁。
    """
    t0 = int(time.time())
    written = upsert_scan(platform, items, source)
    with _lock:
        c = _connect()
        try:
            r = c.execute(
                "DELETE FROM scan_item WHERE platform=? AND (last_seen IS NULL OR last_seen < ?)",
                (platform, t0),
            )
            removed = r.rowcount
            c.commit()
        finally:
            c.close()
    return written, max(0, int(removed or 0))


def list_scan(limit=120, max_discount=1.0):
    """按折扣从低到高排（越靠前越便宜）。"""
    with _lock:
        c = _connect()
        try:
            rows = c.execute(
                """SELECT * FROM scan_item
                   WHERE discount <= ?
                   ORDER BY discount ASC, last_seen DESC
                   LIMIT ?""",
                (max_discount, limit),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            c.close()


def get_scan(platform, sku):
    with _lock:
        c = _connect()
        try:
            r = c.execute(
                "SELECT * FROM scan_item WHERE platform=? AND sku=?", (platform, sku)
            ).fetchone()
            return dict(r) if r else None
        finally:
            c.close()


def scan_stats():
    with _lock:
        c = _connect()
        try:
            total = c.execute("SELECT COUNT(*) FROM scan_item").fetchone()[0]
            cheap = c.execute("SELECT COUNT(*) FROM scan_item WHERE discount <= 0.7").fetchone()[0]
            return {"total": int(total), "cheap": int(cheap)}
        finally:
            c.close()


def clear_scan(platform=None):
    with _lock:
        c = _connect()
        try:
            if platform:
                c.execute("DELETE FROM scan_item WHERE platform=?", (platform,))
            else:
                c.execute("DELETE FROM scan_item")
            c.commit()
        finally:
            c.close()


# ---------- 请求录制（原样重放用）----------

def save_replay(platform, key, url, method, headers, body):
    """把一次真实请求整份存下来。headers 加密，因为里面带 token。"""
    import json as _json

    with _lock:
        c = _connect()
        try:
            c.execute(
                """INSERT INTO replay (platform, key, url, method, headers, body, ts)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(platform, key) DO UPDATE SET
                     url=excluded.url, method=excluded.method, headers=excluded.headers,
                     body=excluded.body, ts=excluded.ts""",
                (
                    platform,
                    key,
                    url,
                    method,
                    crypto.enc(_json.dumps(headers, ensure_ascii=False)),
                    crypto.enc(body or ""),
                    int(time.time()),
                ),
            )
            c.commit()
        finally:
            c.close()


def find_replay(platform, keywords):
    """按路径关键词找最近录到的那条。"""
    with _lock:
        c = _connect()
        try:
            rows = c.execute(
                "SELECT key FROM replay WHERE platform=? ORDER BY ts DESC", (platform,)
            ).fetchall()
        finally:
            c.close()
    for r in rows:
        k = (r["key"] or "").lower()
        if any(w in k for w in keywords):
            return get_replay(platform, r["key"])
    return None


def list_replay_keys(platform="pupu"):
    with _lock:
        c = _connect()
        try:
            rows = c.execute(
                "SELECT key, ts FROM replay WHERE platform=? ORDER BY ts DESC", (platform,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            c.close()


def get_replay(platform, key):
    import json as _json

    with _lock:
        c = _connect()
        try:
            r = c.execute(
                "SELECT * FROM replay WHERE platform=? AND key=?", (platform, key)
            ).fetchone()
        finally:
            c.close()
    if not r:
        return None
    d = dict(r)
    try:
        d["headers"] = _json.loads(crypto.dec(d["headers"]) or "{}")
    except Exception:
        d["headers"] = {}
    d["body"] = crypto.dec(d["body"]) or ""
    return d


# ---------- 监控开关 ----------

def monitor_enabled():
    with _lock:
        c = _connect()
        try:
            r = c.execute("SELECT enabled FROM monitor_state ORDER BY id LIMIT 1").fetchone()
            return bool(r and r["enabled"])
        finally:
            c.close()


def set_monitor(enabled):
    with _lock:
        c = _connect()
        try:
            c.execute("UPDATE monitor_state SET enabled=?, started_at=?",
                      (1 if enabled else 0, int(time.time())))
            c.commit()
        finally:
            c.close()
