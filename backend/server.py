#!/usr/bin/env python3
"""PetroKenya backend.

Serves the installable web app in ../frontend and a JSON REST API backed by
SQLite, with user accounts so several branches can share one online server.
Uses only the Python standard library.

    python3 backend/server.py                       # http://localhost:8000
    python3 backend/server.py create-user NAME --role admin
    python3 backend/server.py create-user NAME --role staff --station "Kisumu Branch"
    python3 backend/server.py reset-password NAME

Environment
    PORT, HOST            listen address (default 127.0.0.1:8000; use HOST=0.0.0.0 when hosted)
    PK_DB                 SQLite file (default backend/petrokenya.sqlite3)
    PK_ADMIN_USER/PK_ADMIN_PASSWORD   create this admin on first start if no users exist
    PK_SECURE_COOKIE=1    force the Secure cookie flag (auto-on behind HTTPS proxies)

API (all JSON; everything except /api/health and /api/login needs a signed-in session)
    POST   /api/login  /api/logout       GET /api/me     POST /api/me/password
    GET    /api/stations                 POST /api/compute
    GET    /api/records?station=&date=&from=&to=&limit=
    POST   /api/records                  (upserts on report_date + station)
    GET|PUT|DELETE /api/records/<id>     DELETE /api/records?station=   (admin)
    GET    /api/analytics?station=       GET /api/export.csv?station=
    GET|POST /api/users   DELETE /api/users/<id>   POST /api/users/<id>/password   (admin)

Roles: "admin" manages users and all branches. "staff" with a station can only
see and edit that branch; staff without a station can use every branch but
cannot bulk-delete or manage users.
"""

import csv
import getpass
import hashlib
import hmac
import io
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.realpath(os.path.join(BACKEND_DIR, "..", "frontend"))
DB_PATH = os.environ.get("PK_DB", os.path.join(BACKEND_DIR, "petrokenya.sqlite3"))
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))
MAX_BODY = 2 * 1024 * 1024
SESSION_DAYS = 30
COOKIE = "pk_session"
PBKDF2_ROUNDS = 240_000
LOGIN_MAX_FAILS, LOGIN_WINDOW = 5, 15 * 60

STATIONS = [
    "Westlands Branch", "Himaki Branch", "Ngeria Branch", "Nairobi West Branch",
    "Mombasa Road Branch", "Kisumu Branch", "Nakuru Branch",
]

mimetypes.add_type("application/manifest+json", ".webmanifest")
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("image/svg+xml", ".svg")

# ── Statutory rates (same constants as the dashboard) ─────────────────────────
VAT = 0.16
EX_P, EX_D, EX_K = 21.95, 7.21, 7.19   # excise per litre
RL_P, RL_D = 18.00, 13.19              # road levy per litre

# Raw numeric inputs accepted from clients
NUM_FIELDS = [
    "petrol_open", "petrol_close", "petrol_price", "petrol_cost",
    "petrol_sales_open", "petrol_sales_close",
    "diesel_open", "diesel_close", "diesel_price", "diesel_cost",
    "diesel_sales_open", "diesel_sales_close",
    "kero_open", "kero_close", "kero_price", "kero_cost",
    "kero_sales_open", "kero_sales_close",
    "petrol_dip_open", "petrol_delivered", "petrol_dip_close",
    "diesel_dip_open", "diesel_delivered", "diesel_dip_close",
    "kero_dip_open", "kero_delivered", "kero_dip_close",
    "mpesa_amount", "mpesa_txns", "cash_amount", "credit_amount", "card_amount",
    "float_amount", "banked_amount",
    "exp_wages", "exp_elec", "exp_water", "exp_maint", "exp_lubes", "exp_misc",
]
TEXT_FIELDS = [
    "operator", "shift", "mpesa_till", "mpesa_ref", "credit_customer",
    "kra_pin", "kra_business", "kra_period", "kra_branch", "kra_etr",
    "kra_return_type", "kra_remarks",
]
LIST_FIELDS = ["misc_items", "gas_items", "invoices"]

CSV_COLS = [
    "report_date", "station",
    "petrol_litres", "diesel_litres", "kero_litres", "total_litres",
    "gross_revenue", "mpesa_amount", "cash_amount", "credit_amount", "card_amount",
    "total_expenses", "net_profit", "profit_margin",
    "output_vat", "total_excise", "total_tax",
    "kra_pin", "kra_business", "kra_period", "kra_return_type",
    "created_at",
]

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def r2(v):
    return round(v + 0.0, 2)


def num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return f if f == f and f not in (float("inf"), float("-inf")) else 0.0


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ── Computation engine — mirrors compute() in frontend/js/app.js ──────────────────────
def compute(raw):
    g = lambda k: num(raw.get(k))
    gas_items = raw.get("gas_items") or []
    misc_items = raw.get("misc_items") or []

    line = lambda i: num(i.get("qty")) * num(i.get("price"))
    gas_lube_rev = r2(sum(line(i) for i in gas_items))
    gas_rev_only = r2(sum(line(i) for i in gas_items if i.get("category") == "Gas"))
    lube_rev_only = r2(sum(line(i) for i in gas_items if i.get("category") == "Lubricant"))

    pL = max(0.0, g("petrol_close") - g("petrol_open"))
    dL = max(0.0, g("diesel_close") - g("diesel_open"))
    kL = max(0.0, g("kero_close") - g("kero_open"))
    pRev, dRev, kRev = r2(pL * g("petrol_price")), r2(dL * g("diesel_price")), r2(kL * g("kero_price"))
    pFC, dFC, kFC = r2(pL * g("petrol_cost")), r2(dL * g("diesel_cost")), r2(kL * g("kero_cost"))
    totalL = r2(pL + dL + kL)
    grossRev = r2(pRev + dRev + kRev + gas_lube_rev)
    totalFC = r2(pFC + dFC + kFC)

    pump = {}
    for prefix, rev in (("petrol", pRev), ("diesel", dRev), ("kero", kRev)):
        sales = max(0.0, r2(g(prefix + "_sales_close") - g(prefix + "_sales_open")))
        pump[prefix + "_pump_sales"] = sales
        pump[prefix + "_pump_variance"] = r2(rev - sales)

    dips = {}
    for prefix, sold in (("petrol", pL), ("diesel", dL), ("kero", kL)):
        expected = r2(g(prefix + "_dip_open") + g(prefix + "_delivered") - sold)
        dips[prefix + "_expected_stock"] = expected
        dips[prefix + "_dip_variance"] = r2(g(prefix + "_dip_close") - expected)

    mpesa, cash = g("mpesa_amount"), g("cash_amount")
    credit, card = g("credit_amount"), g("card_amount")
    collected = r2(mpesa + cash + credit + card)

    misc_total = r2(sum(num(i.get("amount")) for i in misc_items))
    exp_misc = misc_total if misc_items else g("exp_misc")
    totalExp = r2(g("exp_wages") + g("exp_elec") + g("exp_water") + g("exp_maint")
                  + g("exp_lubes") + exp_misc)

    totalCost = r2(totalFC + totalExp)
    netProfit = r2(grossRev - totalCost)

    exVat = r2(grossRev / (1 + VAT))
    outputVAT = r2(grossRev - exVat)
    excP, excD, excK = r2(pL * EX_P), r2(dL * EX_D), r2(kL * EX_K)
    totalExcise = r2(excP + excD + excK)

    return {
        "petrol_litres": r2(pL), "diesel_litres": r2(dL), "kero_litres": r2(kL),
        "total_litres": totalL,
        "petrol_revenue": pRev, "diesel_revenue": dRev, "kero_revenue": kRev,
        "petrol_margin": r2(pRev - pFC), "diesel_margin": r2(dRev - dFC), "kero_margin": r2(kRev - kFC),
        "gas_lube_revenue": gas_lube_rev, "gas_revenue": gas_rev_only, "lube_revenue": lube_rev_only,
        **pump,
        "total_pump_sales": r2(pump["petrol_pump_sales"] + pump["diesel_pump_sales"] + pump["kero_pump_sales"]),
        **dips,
        "gross_revenue": grossRev,
        "total_fuel_cost": totalFC,
        "exp_misc": exp_misc,
        "total_expenses": totalExp,
        "total_cost": totalCost,
        "net_profit": netProfit,
        "profit_margin": r2(netProfit / grossRev * 100) if grossRev > 0 else 0,
        "revenue_per_litre": r2(grossRev / totalL) if totalL > 0 else 0,
        "profit_per_litre": r2(netProfit / totalL) if totalL > 0 else 0,
        "collected": collected,
        "collection_variance": r2(grossRev - collected),
        "till_balance": r2(cash + g("float_amount") - g("banked_amount")),
        "taxable_sales": exVat,
        "output_vat": outputVAT,
        "excise_petrol": excP, "excise_diesel": excD, "excise_kero": excK,
        "total_excise": totalExcise,
        "road_levy_petrol": r2(pL * RL_P), "road_levy_diesel": r2(dL * RL_D),
        "total_tax": r2(outputVAT + totalExcise),
    }


def clean_input(body):
    """Validate and normalise a record payload from a client."""
    if not isinstance(body, dict):
        raise ApiError(HTTPStatus.BAD_REQUEST, "Request body must be a JSON object")

    date = str(body.get("report_date") or "").strip()
    if not DATE_RE.match(date):
        raise ApiError(HTTPStatus.BAD_REQUEST, "report_date is required (YYYY-MM-DD)")
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise ApiError(HTTPStatus.BAD_REQUEST, "report_date is not a valid date")

    station = str(body.get("station") or "").strip()
    if not station:
        raise ApiError(HTTPStatus.BAD_REQUEST, "station is required")

    rec = {"report_date": date, "station": station[:120]}
    for k in NUM_FIELDS:
        rec[k] = num(body.get(k))
    for k in TEXT_FIELDS:
        v = body.get(k)
        rec[k] = "" if v is None else str(v)[:2000]
    for k in LIST_FIELDS:
        v = body.get(k)
        if v is None:
            v = []
        if not isinstance(v, list) or not all(isinstance(i, dict) for i in v):
            raise ApiError(HTTPStatus.BAD_REQUEST, f"{k} must be a list of objects")
        rec[k] = [clean_item(i) for i in v[:500]]
    return rec


# Item fields that must be numbers; everything else in an item is kept as short text
ITEM_NUM_FIELDS = {"id", "qty", "price", "amount"}


def clean_item(item):
    out = {}
    for key, val in list(item.items())[:30]:
        key = str(key)[:40]
        out[key] = num(val) if key in ITEM_NUM_FIELDS else ("" if val is None else str(val)[:300])
    return out


# ── Storage ───────────────────────────────────────────────────────────────────
class Store:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS records (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                report_date    TEXT NOT NULL,
                station        TEXT NOT NULL,
                gross_revenue  REAL NOT NULL DEFAULT 0,
                total_litres   REAL NOT NULL DEFAULT 0,
                net_profit     REAL NOT NULL DEFAULT 0,
                total_tax      REAL NOT NULL DEFAULT 0,
                data           TEXT NOT NULL,
                created_at     TEXT NOT NULL,
                updated_at     TEXT NOT NULL,
                UNIQUE (report_date, station)
            );
            CREATE INDEX IF NOT EXISTS idx_records_date ON records (report_date);
        """)
        self.conn.commit()

    @staticmethod
    def _row(row):
        rec = json.loads(row["data"])
        rec["id"] = row["id"]
        rec["uid"] = "rec_" + str(row["id"])
        rec["created_at"] = row["created_at"]
        rec["updated_at"] = row["updated_at"]
        return rec

    def list(self, station=None, date=None, date_from=None, date_to=None, limit=None):
        sql, args = "SELECT * FROM records WHERE 1=1", []
        if station:
            sql += " AND station = ?"; args.append(station)
        if date:
            sql += " AND report_date = ?"; args.append(date)
        if date_from:
            sql += " AND report_date >= ?"; args.append(date_from)
        if date_to:
            sql += " AND report_date <= ?"; args.append(date_to)
        sql += " ORDER BY report_date DESC, id DESC"
        if limit:
            sql += " LIMIT ?"; args.append(limit)
        with self.lock:
            return [self._row(r) for r in self.conn.execute(sql, args).fetchall()]

    def get(self, rec_id):
        with self.lock:
            row = self.conn.execute("SELECT * FROM records WHERE id = ?", (rec_id,)).fetchone()
        return self._row(row) if row else None

    def save(self, rec, rec_id=None):
        """Insert, or update the existing row for the same date+station (or id)."""
        rec = {**rec, **compute(rec)}
        ts = now_iso()
        data = json.dumps(rec)
        cols = (rec["report_date"], rec["station"], rec["gross_revenue"],
                rec["total_litres"], rec["net_profit"], rec["total_tax"], data)
        with self.lock:
            if rec_id is not None:
                if not self.conn.execute("SELECT 1 FROM records WHERE id = ?", (rec_id,)).fetchone():
                    raise ApiError(HTTPStatus.NOT_FOUND, "Record not found")
                clash = self.conn.execute(
                    "SELECT id FROM records WHERE report_date = ? AND station = ? AND id != ?",
                    (rec["report_date"], rec["station"], rec_id)).fetchone()
                if clash:
                    raise ApiError(HTTPStatus.CONFLICT,
                                   "Another record already exists for this date and station")
                existing_id, created = rec_id, False
            else:
                row = self.conn.execute(
                    "SELECT id FROM records WHERE report_date = ? AND station = ?",
                    (rec["report_date"], rec["station"])).fetchone()
                existing_id, created = (row["id"], False) if row else (None, True)

            if existing_id is None:
                cur = self.conn.execute(
                    """INSERT INTO records (report_date, station, gross_revenue, total_litres,
                       net_profit, total_tax, data, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", cols + (ts, ts))
                new_id = cur.lastrowid
            else:
                self.conn.execute(
                    """UPDATE records SET report_date = ?, station = ?, gross_revenue = ?,
                       total_litres = ?, net_profit = ?, total_tax = ?, data = ?, updated_at = ?
                       WHERE id = ?""", cols + (ts, existing_id))
                new_id = existing_id
            self.conn.commit()
        return self.get(new_id), created

    def delete(self, rec_id):
        with self.lock:
            cur = self.conn.execute("DELETE FROM records WHERE id = ?", (rec_id,))
            self.conn.commit()
        return cur.rowcount

    def delete_all(self, station=None):
        with self.lock:
            if station:
                cur = self.conn.execute("DELETE FROM records WHERE station = ?", (station,))
            else:
                cur = self.conn.execute("DELETE FROM records")
            self.conn.commit()
        return cur.rowcount


def analytics(records):
    week = sorted(records, key=lambda r: r.get("report_date") or "", reverse=True)[:7][::-1]
    totals = {"days": len(records), "revenue": 0.0, "litres": 0.0, "net_profit": 0.0,
              "tax": 0.0, "mpesa": 0.0, "cash": 0.0}
    for r in records:
        totals["revenue"] += num(r.get("gross_revenue"))
        totals["litres"] += num(r.get("total_litres"))
        totals["net_profit"] += num(r.get("net_profit"))
        totals["tax"] += num(r.get("total_tax"))
        totals["mpesa"] += num(r.get("mpesa_amount"))
        totals["cash"] += num(r.get("cash_amount"))
    totals = {k: (r2(v) if isinstance(v, float) else v) for k, v in totals.items()}
    totals["avg_daily_profit"] = r2(totals["net_profit"] / totals["days"]) if totals["days"] else 0
    return {
        "last_7": [{"report_date": r["report_date"], "station": r["station"],
                    "gross_revenue": r.get("gross_revenue", 0),
                    "net_profit": r.get("net_profit", 0),
                    "total_litres": r.get("total_litres", 0)} for r in week],
        "totals": totals,
    }


def to_csv(records):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(CSV_COLS)
    for r in records:
        w.writerow(["" if r.get(c) is None else r.get(c) for c in CSV_COLS])
    return buf.getvalue()



# ── Accounts & sessions ───────────────────────────────────────────────────────
USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}$")


def hash_password(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return "pbkdf2$%d$%s$%s" % (PBKDF2_ROUNDS, salt.hex(), dk.hex())


def verify_password(password, stored):
    try:
        _, rounds, salt, digest = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), int(rounds))
        return hmac.compare_digest(dk.hex(), digest)
    except (ValueError, AttributeError):
        return False


def check_new_password(password):
    if not isinstance(password, str) or len(password) < 8:
        raise ApiError(HTTPStatus.BAD_REQUEST, "Password must be at least 8 characters")
    return password


class Auth:
    """Users and login sessions, stored in the same SQLite database as records."""

    def __init__(self, store):
        self.store = store
        self.fails = {}  # ip -> [count, first_failure_time]
        store.conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                username   TEXT NOT NULL UNIQUE COLLATE NOCASE,
                pw_hash    TEXT NOT NULL,
                role       TEXT NOT NULL CHECK (role IN ('admin', 'staff')),
                station    TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at REAL NOT NULL
            );
        """)
        store.conn.commit()

    @staticmethod
    def public(row):
        return {"id": row["id"], "username": row["username"], "role": row["role"],
                "station": row["station"] or None, "created_at": row["created_at"]}

    def count(self):
        with self.store.lock:
            return self.store.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def list(self):
        with self.store.lock:
            rows = self.store.conn.execute("SELECT * FROM users ORDER BY username").fetchall()
        return [self.public(r) for r in rows]

    def create(self, username, password, role="staff", station=None):
        username = (username or "").strip()
        if not USERNAME_RE.match(username):
            raise ApiError(HTTPStatus.BAD_REQUEST,
                           "Username must be 3-32 letters, numbers, dots, dashes or underscores")
        if role not in ("admin", "staff"):
            raise ApiError(HTTPStatus.BAD_REQUEST, "role must be 'admin' or 'staff'")
        station = (station or "").strip() or None
        if station and station not in STATIONS:
            raise ApiError(HTTPStatus.BAD_REQUEST, "Unknown station")
        check_new_password(password)
        try:
            with self.store.lock:
                cur = self.store.conn.execute(
                    "INSERT INTO users (username, pw_hash, role, station, created_at) VALUES (?, ?, ?, ?, ?)",
                    (username, hash_password(password), role, station, now_iso()))
                self.store.conn.commit()
        except sqlite3.IntegrityError:
            raise ApiError(HTTPStatus.CONFLICT, "That username is already taken")
        return self.get(cur.lastrowid)

    def get(self, user_id):
        with self.store.lock:
            row = self.store.conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return self.public(row) if row else None

    def find(self, username):
        with self.store.lock:
            return self.store.conn.execute(
                "SELECT * FROM users WHERE username = ?", ((username or "").strip(),)).fetchone()

    def set_password(self, user_id, password):
        check_new_password(password)
        with self.store.lock:
            cur = self.store.conn.execute("UPDATE users SET pw_hash = ? WHERE id = ?",
                                          (hash_password(password), user_id))
            # Signing out everywhere else is the point of changing a password
            self.store.conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            self.store.conn.commit()
        if not cur.rowcount:
            raise ApiError(HTTPStatus.NOT_FOUND, "User not found")

    def delete(self, user_id):
        with self.store.lock:
            row = self.store.conn.execute("SELECT role FROM users WHERE id = ?", (user_id,)).fetchone()
            if not row:
                raise ApiError(HTTPStatus.NOT_FOUND, "User not found")
            if row["role"] == "admin" and self.store.conn.execute(
                    "SELECT COUNT(*) FROM users WHERE role = 'admin'").fetchone()[0] <= 1:
                raise ApiError(HTTPStatus.BAD_REQUEST, "Cannot delete the last admin")
            self.store.conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            self.store.conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
            self.store.conn.commit()

    # Login throttling, per client IP
    def throttled(self, ip):
        entry = self.fails.get(ip)
        if entry and time.time() - entry[1] > LOGIN_WINDOW:
            self.fails.pop(ip, None)
            return False
        return bool(entry and entry[0] >= LOGIN_MAX_FAILS)

    def login(self, username, password, ip):
        if self.throttled(ip):
            raise ApiError(HTTPStatus.TOO_MANY_REQUESTS, "Too many failed attempts — try again in 15 minutes")
        row = self.find(username)
        if not row or not verify_password(password or "", row["pw_hash"]):
            entry = self.fails.setdefault(ip, [0, time.time()])
            entry[0] += 1
            raise ApiError(HTTPStatus.UNAUTHORIZED, "Wrong username or password")
        self.fails.pop(ip, None)
        token = secrets.token_urlsafe(32)
        with self.store.lock:
            self.store.conn.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))
            self.store.conn.execute("INSERT INTO sessions VALUES (?, ?, ?)",
                                    (hashlib.sha256(token.encode()).hexdigest(), row["id"],
                                     time.time() + SESSION_DAYS * 86400))
            self.store.conn.commit()
        return token, self.public(row)

    def user_for(self, token):
        if not token:
            return None
        with self.store.lock:
            row = self.store.conn.execute(
                """SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id
                   WHERE s.token_hash = ? AND s.expires_at > ?""",
                (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
        return self.public(row) if row else None

    def logout(self, token):
        if token:
            with self.store.lock:
                self.store.conn.execute("DELETE FROM sessions WHERE token_hash = ?",
                                        (hashlib.sha256(token.encode()).hexdigest(),))
                self.store.conn.commit()


# ── HTTP layer ────────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    server_version = "PetroKenya/2.0"
    store = None  # set in make_server()
    auth = None

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    # ── Responses ──
    def _send(self, status, body=b"", ctype="application/json; charset=utf-8", extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        for k, v in (extra or {}).items():
            if isinstance(v, list):
                for item in v:
                    self.send_header(k, item)
            else:
                self.send_header(k, v)
        if "Cache-Control" not in (extra or {}):
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status, payload, extra=None):
        self._send(status, json.dumps(payload).encode("utf-8"), extra=extra)

    def _body(self):
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            # Also blocks cross-site form posts, which cannot send this content type
            raise ApiError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Content-Type must be application/json")
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Request body too large")
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            raise ApiError(HTTPStatus.BAD_REQUEST, "Invalid JSON body")
        if not isinstance(body, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "Request body must be a JSON object")
        return body

    # ── Request context ──
    def _https(self):
        return (os.environ.get("PK_SECURE_COOKIE") == "1"
                or (self.headers.get("X-Forwarded-Proto") or "").lower() == "https")

    def _client_ip(self):
        fwd = self.headers.get("X-Forwarded-For")
        # Rightmost entry is the one added by the proxy directly in front of us
        return fwd.split(",")[-1].strip() if fwd else self.client_address[0]

    def _token(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE:
                return v
        return None

    def _cookie(self, token, max_age):
        attrs = ["%s=%s" % (COOKIE, token), "Path=/", "HttpOnly", "SameSite=Lax", "Max-Age=%d" % max_age]
        if self._https():
            attrs.append("Secure")
        return "; ".join(attrs)

    # ── Static files ──
    def _static(self, path):
        rel = unquote(path).lstrip("/") or "index.html"
        full = os.path.realpath(os.path.join(FRONTEND_DIR, rel))
        if not full.startswith(FRONTEND_DIR + os.sep) or not os.path.isfile(full):
            raise ApiError(HTTPStatus.NOT_FOUND, "Not found")
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith(("json", "javascript")):
            ctype += "; charset=utf-8"
        # HTML and the service worker must revalidate so app updates reach phones
        fresh = rel in ("index.html", "sw.js") or rel.endswith(".webmanifest")
        with open(full, "rb") as f:
            self._send(HTTPStatus.OK, f.read(), ctype,
                       {"Cache-Control": "no-cache" if fresh else "public, max-age=3600"})

    # ── Permissions ──
    @staticmethod
    def _station_scope(user, requested):
        """Station a request may touch: staff tied to a branch only get that branch."""
        if user["role"] == "staff" and user["station"]:
            if requested and requested != user["station"]:
                raise ApiError(HTTPStatus.FORBIDDEN, "You can only access %s" % user["station"])
            return user["station"]
        return requested or None

    @staticmethod
    def _require_admin(user):
        if user["role"] != "admin":
            raise ApiError(HTTPStatus.FORBIDDEN, "Admins only")

    def _own_record(self, user, rec_id):
        rec = self.store.get(rec_id)
        if not rec:
            raise ApiError(HTTPStatus.NOT_FOUND, "Record not found")
        if user["role"] == "staff" and user["station"] and rec["station"] != user["station"]:
            raise ApiError(HTTPStatus.NOT_FOUND, "Record not found")
        return rec

    # ── Routing ──
    def _dispatch(self):
        url = urlparse(self.path)
        path = url.path.rstrip("/") or "/"
        q = {k: v[-1] for k, v in parse_qs(url.query).items()}
        m = self.command
        try:
            if not path.startswith("/api"):
                if m not in ("GET", "HEAD"):
                    raise ApiError(HTTPStatus.METHOD_NOT_ALLOWED, "Method not allowed")
                return self._static(path)

            if path == "/api/health" and m == "GET":
                return self._json(HTTPStatus.OK, {"status": "ok", "time": now_iso()})

            if path == "/api/login" and m == "POST":
                body = self._body()
                token, user = self.auth.login(body.get("username"), body.get("password"), self._client_ip())
                return self._json(HTTPStatus.OK, {"user": user},
                                  {"Set-Cookie": self._cookie(token, SESSION_DAYS * 86400)})

            if path == "/api/logout" and m == "POST":
                self.auth.logout(self._token())
                return self._json(HTTPStatus.OK, {"ok": True}, {"Set-Cookie": self._cookie("", 0)})

            user = self.auth.user_for(self._token())
            if not user:
                raise ApiError(HTTPStatus.UNAUTHORIZED, "Please sign in")
            return self._route(m, path, q, user)
        except ApiError as e:
            self._json(e.status, {"error": e.message})
        except Exception as e:  # pragma: no cover - last-resort guard
            self.log_message("Unhandled error: %r", e)
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Internal server error"})

    def _route(self, m, path, q, user):
        if path == "/api/me" and m == "GET":
            return self._json(HTTPStatus.OK, {"user": user})
        if path == "/api/me/password" and m == "POST":
            body = self._body()
            row = self.auth.find(user["username"])
            if not verify_password(body.get("current_password") or "", row["pw_hash"]):
                raise ApiError(HTTPStatus.BAD_REQUEST, "Current password is wrong")
            self.auth.set_password(user["id"], body.get("new_password"))
            token, _ = self.auth.login(user["username"], body.get("new_password"), self._client_ip())
            return self._json(HTTPStatus.OK, {"ok": True},
                              {"Set-Cookie": self._cookie(token, SESSION_DAYS * 86400)})
        if path == "/api/stations" and m == "GET":
            scope = self._station_scope(user, None)
            return self._json(HTTPStatus.OK, {"stations": [scope] if scope else STATIONS})
        if path == "/api/compute" and m == "POST":
            return self._json(HTTPStatus.OK, compute(self._body()))
        if path == "/api/analytics" and m == "GET":
            station = self._station_scope(user, q.get("station"))
            return self._json(HTTPStatus.OK, analytics(self.store.list(station=station)))
        if path == "/api/export.csv" and m == "GET":
            station = self._station_scope(user, q.get("station"))
            name = "PetroKenya_%s_%s.csv" % (
                (station or "All-Branches").replace(" ", "-"), datetime.now().strftime("%Y-%m-%d"))
            return self._send(HTTPStatus.OK, to_csv(self.store.list(station=station)).encode("utf-8"),
                              "text/csv; charset=utf-8",
                              {"Content-Disposition": 'attachment; filename="%s"' % name})

        if path == "/api/records":
            if m == "GET":
                limit = q.get("limit")
                limit = int(limit) if limit and limit.isdigit() else None
                station = self._station_scope(user, q.get("station"))
                recs = self.store.list(station, q.get("date"), q.get("from"), q.get("to"), limit)
                return self._json(HTTPStatus.OK, {"count": len(recs), "records": recs})
            if m == "POST":
                rec = clean_input(self._body())
                self._station_scope(user, rec["station"])
                rec["saved_by"] = user["username"]
                saved, created = self.store.save(rec)
                return self._json(HTTPStatus.CREATED if created else HTTPStatus.OK,
                                  {"created": created, "record": saved})
            if m == "DELETE":
                self._require_admin(user)
                return self._json(HTTPStatus.OK, {"deleted": self.store.delete_all(q.get("station"))})

        mm = re.fullmatch(r"/api/records/(\d+)", path)
        if mm:
            rec_id = int(mm.group(1))
            if m == "GET":
                return self._json(HTTPStatus.OK, self._own_record(user, rec_id))
            if m == "PUT":
                self._own_record(user, rec_id)
                rec = clean_input(self._body())
                self._station_scope(user, rec["station"])
                rec["saved_by"] = user["username"]
                saved, _ = self.store.save(rec, rec_id=rec_id)
                return self._json(HTTPStatus.OK, {"created": False, "record": saved})
            if m == "DELETE":
                self._own_record(user, rec_id)
                self.store.delete(rec_id)
                return self._json(HTTPStatus.OK, {"deleted": 1})

        if path == "/api/users":
            self._require_admin(user)
            if m == "GET":
                return self._json(HTTPStatus.OK, {"users": self.auth.list()})
            if m == "POST":
                body = self._body()
                created = self.auth.create(body.get("username"), body.get("password"),
                                           body.get("role") or "staff", body.get("station"))
                return self._json(HTTPStatus.CREATED, {"user": created})

        mm = re.fullmatch(r"/api/users/(\d+)(/password)?", path)
        if mm:
            self._require_admin(user)
            uid = int(mm.group(1))
            if mm.group(2) and m == "POST":
                self.auth.set_password(uid, self._body().get("password"))
                return self._json(HTTPStatus.OK, {"ok": True})
            if not mm.group(2) and m == "DELETE":
                if uid == user["id"]:
                    raise ApiError(HTTPStatus.BAD_REQUEST, "You cannot delete your own account")
                self.auth.delete(uid)
                return self._json(HTTPStatus.OK, {"deleted": 1})

        raise ApiError(HTTPStatus.NOT_FOUND if m == "GET" else HTTPStatus.METHOD_NOT_ALLOWED,
                       "No route for %s %s" % (m, path))

    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = _dispatch


def bootstrap_admin(auth):
    """Make sure a fresh database has an admin account to sign in with."""
    if auth.count():
        return
    user = os.environ.get("PK_ADMIN_USER")
    password = os.environ.get("PK_ADMIN_PASSWORD")
    if user and password:
        auth.create(user, password, "admin")
        print(f"   Created admin '{user}' from PK_ADMIN_USER / PK_ADMIN_PASSWORD.")
        return
    password = secrets.token_urlsafe(9)
    auth.create("admin", password, "admin")
    print("   ┌──────────────────────────────────────────────────────────┐")
    print("   │ First start: created admin account                       │")
    print(f"   │   username: admin   password: {password:<26}│")
    print("   │ Sign in and change it under 👤 Account.                   │")
    print("   └──────────────────────────────────────────────────────────┘")


def make_server(host=HOST, port=PORT, db_path=DB_PATH, bootstrap=True):
    store = Store(db_path)
    auth = Auth(store)
    if bootstrap:
        bootstrap_admin(auth)
    handler = type("BoundHandler", (Handler,), {"store": store, "auth": auth})
    return ThreadingHTTPServer((host, port), handler)


def cli(argv):
    """create-user NAME [--role admin|staff] [--station NAME]  |  reset-password NAME"""
    store = Store(DB_PATH)
    auth = Auth(store)
    cmd, name, opts = argv[0], argv[1] if len(argv) > 1 else None, argv[2:]
    if not name:
        sys.exit(cli.__doc__)
    flags = dict(zip(opts[::2], opts[1::2]))
    password = getpass.getpass("Password for %s: " % name)
    try:
        if cmd == "create-user":
            u = auth.create(name, password, flags.get("--role", "staff"), flags.get("--station"))
            print("Created %s (%s%s)" % (u["username"], u["role"], ", " + u["station"] if u["station"] else ""))
        elif cmd == "reset-password":
            row = auth.find(name)
            if not row:
                sys.exit("No such user: %s" % name)
            auth.set_password(row["id"], password)
            print("Password updated for %s" % name)
        else:
            sys.exit(cli.__doc__)
    except ApiError as e:
        sys.exit("Error: " + e.message)


def main():
    if len(sys.argv) > 1:
        return cli(sys.argv[1:])
    httpd = make_server()
    host, port = httpd.server_address[:2]
    shown = "localhost" if host in ("127.0.0.1", "0.0.0.0") else host
    print(f"⛽ PetroKenya running at http://{shown}:{port}")
    print(f"   Database: {httpd.RequestHandlerClass.store.path}")
    print("   Press Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
