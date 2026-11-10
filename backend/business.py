"""Business features on top of the records store: credit customer accounts,
alerts (in-app and email) and monthly reports.

Everything here shares the Store's SQLite connection and lock. Standard library only.
"""

import base64
import hashlib
import json
import os
import re
import secrets
import smtplib
import sys
import threading
from email.message import EmailMessage
from http import HTTPStatus

FUELS = ("petrol", "diesel", "kero")
FUEL_NAMES = {"petrol": "Petrol", "diesel": "Diesel", "kero": "Kerosene"}

# Alert thresholds an admin can change (Account → Alert Settings)
DEFAULT_SETTINGS = {
    "dip_shortage_litres": 50,       # tank this many litres below expected stock
    "cash_shortage_kes": 1000,       # money collected this much below sales
    "attendant_shortage_kes": 500,   # one attendant handed in this much less than their sales
    "short_delivery_litres": 50,     # a delivery this many litres short of the order
    "low_stock_petrol": 3000,        # closing dip below these litres
    "low_stock_diesel": 3000,
    "low_stock_kero": 1000,
    "alert_emails": "",              # comma-separated addresses that get alert emails
}


class Error(Exception):
    """Raised for bad input; server.py turns it into an HTTP error response."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return f if f == f and f not in (float("inf"), float("-inf")) else 0.0


def r2(v):
    return round(v + 0.0, 2)


def nice_date(d):
    from datetime import datetime
    try:
        return datetime.strptime(d, "%Y-%m-%d").strftime("%a %d %b %Y")
    except (TypeError, ValueError):
        return d or ""


# ── Settings ──────────────────────────────────────────────────────────────────
class Settings:
    def __init__(self, store):
        self.store = store
        store.conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        store.conn.commit()

    def all(self):
        out = dict(DEFAULT_SETTINGS)
        with self.store.lock:
            for row in self.store.conn.execute("SELECT key, value FROM settings").fetchall():
                if row["key"] in out:
                    out[row["key"]] = json.loads(row["value"])
        return out

    def update(self, changes):
        clean = {}
        for key, val in (changes or {}).items():
            if key not in DEFAULT_SETTINGS:
                continue
            if key == "alert_emails":
                emails = [e.strip() for e in str(val or "").replace(";", ",").split(",") if e.strip()]
                bad = [e for e in emails if "@" not in e or " " in e or len(e) > 120]
                if bad or len(emails) > 20:
                    raise Error(HTTPStatus.BAD_REQUEST, "Check the alert email addresses")
                clean[key] = ", ".join(emails)
            else:
                v = num(val)
                if v < 0 or v > 1_000_000:
                    raise Error(HTTPStatus.BAD_REQUEST, "Check the value for %s" % key)
                clean[key] = v
        with self.store.lock:
            for key, val in clean.items():
                self.store.conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                                        (key, json.dumps(val)))
            self.store.conn.commit()
        return self.all()


# ── Email ─────────────────────────────────────────────────────────────────────
class Mailer:
    """Sends alert emails through an SMTP account set in the environment:
    PK_SMTP_HOST, PK_SMTP_PORT (587, or 465 for SSL), PK_SMTP_USER, PK_SMTP_PASSWORD,
    PK_SMTP_FROM (defaults to the user)."""

    def __init__(self):
        self.host = os.environ.get("PK_SMTP_HOST", "")
        self.port = int(os.environ.get("PK_SMTP_PORT", "587") or 587)
        self.user = os.environ.get("PK_SMTP_USER", "")
        self.password = os.environ.get("PK_SMTP_PASSWORD", "")
        self.sender = os.environ.get("PK_SMTP_FROM", "") or self.user
        self.sent = []   # (recipients, subject) — kept for tests and the settings page

    @property
    def configured(self):
        return bool(self.host and self.sender)

    def send_now(self, recipients, subject, body):
        msg = EmailMessage()
        msg["From"] = self.sender
        msg["To"] = ", ".join(recipients)
        msg["Subject"] = subject
        msg.set_content(body)
        if self.port == 465:
            server = smtplib.SMTP_SSL(self.host, self.port, timeout=20)
        else:
            server = smtplib.SMTP(self.host, self.port, timeout=20)
            server.starttls()
        try:
            if self.user:
                server.login(self.user, self.password)
            server.send_message(msg)
        finally:
            server.quit()
        self.sent.append((list(recipients), subject))

    def send(self, recipients, subject, body):
        """Send in the background so saving a record never waits on the mail server."""
        if not self.configured or not recipients:
            return False

        def run():
            try:
                self.send_now(recipients, subject, body)
            except Exception as e:  # pragma: no cover - depends on the mail server
                sys.stderr.write("Alert email failed: %r\n" % e)
        threading.Thread(target=run, daemon=True).start()
        return True


# ── Alerts ────────────────────────────────────────────────────────────────────
def evaluate_alerts(rec, settings):
    """The alert conditions a saved record meets, as (kind, key, message) tuples."""
    where = "%s, %s" % (rec.get("station", ""), nice_date(rec.get("report_date")))
    out = []
    for fuel in FUELS:
        closing = num(rec.get(fuel + "_dip_close"))
        if closing <= 0:
            continue
        variance = num(rec.get(fuel + "_dip_variance"))
        if variance < -settings["dip_shortage_litres"]:
            out.append(("dip_shortage", fuel, "%s tank is %s L short of expected stock — %s"
                        % (FUEL_NAMES[fuel], "{:,.0f}".format(-variance), where)))
        level = settings["low_stock_" + fuel]
        if level and closing < level:
            out.append(("low_stock", fuel, "%s stock is low: %s L left (reorder level %s L) — %s"
                        % (FUEL_NAMES[fuel], "{:,.0f}".format(closing), "{:,.0f}".format(level), where)))
    gap = num(rec.get("collection_variance"))
    if gap > settings["cash_shortage_kes"]:
        out.append(("cash_shortage", "day", "Money collected is KES %s less than sales — %s"
                    % ("{:,.2f}".format(gap), where)))
    for a in rec.get("attendants") or []:
        short = num(a.get("expected")) - num(a.get("cash")) - num(a.get("mpesa"))
        if short > settings["attendant_shortage_kes"]:
            out.append(("attendant_shortage", str(a.get("name") or "?")[:60],
                        "%s handed in KES %s less than their sales — %s"
                        % (a.get("name") or "An attendant", "{:,.2f}".format(short), where)))
    mism = rec.get("opening_mismatches") or []
    if mism:
        out.append(("opening_mismatch", "day", "%d opening reading%s differ from the previous day's closing — %s"
                    % (len(mism), "" if len(mism) == 1 else "s", where)))
    for i, d in enumerate(rec.get("deliveries") or []):
        short = num(d.get("litres_ordered")) - num(d.get("litres_received"))
        if num(d.get("litres_ordered")) > 0 and short > settings["short_delivery_litres"]:
            out.append(("short_delivery", str(d.get("note_no") or i)[:60],
                        "%s delivery %s was %s L short of the order — %s"
                        % (FUEL_NAMES.get(d.get("fuel"), "Fuel"), d.get("note_no") or "", "{:,.0f}".format(short),
                           where)))
    return out


ALERT_TITLES = {
    "dip_shortage": "Tank stock short", "low_stock": "Low tank stock", "cash_shortage": "Cash short",
    "attendant_shortage": "Attendant short", "opening_mismatch": "Opening readings changed",
    "short_delivery": "Short delivery",
}


class Alerts:
    def __init__(self, store, settings, mailer):
        self.store, self.settings, self.mailer = store, settings, mailer
        store.conn.executescript("""
            CREATE TABLE IF NOT EXISTS alerts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                record_id   INTEGER NOT NULL,
                station     TEXT NOT NULL,
                report_date TEXT NOT NULL,
                kind        TEXT NOT NULL,
                key         TEXT NOT NULL,
                message     TEXT NOT NULL,
                created_at  TEXT NOT NULL,
                cleared_at  TEXT,
                emailed     INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_alerts_record ON alerts (record_id);
            CREATE TABLE IF NOT EXISTS alert_reads (
                alert_id INTEGER NOT NULL,
                user_id  INTEGER NOT NULL,
                PRIMARY KEY (alert_id, user_id)
            );
        """)
        store.conn.commit()

    def sync_record(self, rec, now):
        """Bring a record's alerts in line with its figures: raise new ones (and email
        them), update changed wording, and clear ones that no longer apply."""
        found = {(k, key): msg for k, key, msg in evaluate_alerts(rec, self.settings.all())}
        new = []
        with self.store.lock:
            rows = self.store.conn.execute(
                "SELECT * FROM alerts WHERE record_id = ? AND cleared_at IS NULL", (rec["id"],)).fetchall()
            open_keys = {}
            for row in rows:
                ident = (row["kind"], row["key"])
                if ident in found:
                    open_keys[ident] = row["id"]
                    if row["message"] != found[ident]:
                        self.store.conn.execute("UPDATE alerts SET message = ? WHERE id = ?",
                                                (found[ident], row["id"]))
                else:
                    self.store.conn.execute("UPDATE alerts SET cleared_at = ? WHERE id = ?", (now, row["id"]))
            for ident, msg in found.items():
                if ident not in open_keys:
                    cur = self.store.conn.execute(
                        "INSERT INTO alerts (record_id, station, report_date, kind, key, message, created_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (rec["id"], rec["station"], rec["report_date"], ident[0], ident[1], msg, now))
                    new.append({"id": cur.lastrowid, "kind": ident[0], "message": msg})
            self.store.conn.commit()
        if new:
            self.email(new, rec)
        return new

    def clear_record(self, rec_id, now):
        with self.store.lock:
            self.store.conn.execute("UPDATE alerts SET cleared_at = ? WHERE record_id = ? AND cleared_at IS NULL",
                                    (now, rec_id))
            self.store.conn.commit()

    def email(self, alerts, rec):
        to = [e.strip() for e in self.settings.all()["alert_emails"].split(",") if e.strip()]
        subject = "PetroKenya alert: %s — %s" % (rec["station"], ", ".join(
            sorted({ALERT_TITLES.get(a["kind"], a["kind"]) for a in alerts})))
        body = "\n".join(["• " + a["message"] for a in alerts]) + \
            "\n\nOpen the PetroKenya app to see the full record.\n"
        if self.mailer.send(to, subject, body):
            with self.store.lock:
                self.store.conn.executemany("UPDATE alerts SET emailed = 1 WHERE id = ?",
                                            [(a["id"],) for a in alerts])
                self.store.conn.commit()

    def list(self, user, station=None, include_cleared=False, limit=200):
        sql = ("SELECT a.*, EXISTS (SELECT 1 FROM alert_reads r WHERE r.alert_id = a.id AND r.user_id = ?)"
               " AS is_read FROM alerts a WHERE 1=1")
        args = [user["id"]]
        if station:
            sql += " AND a.station = ?"; args.append(station)
        if not include_cleared:
            sql += " AND a.cleared_at IS NULL"
        sql += " ORDER BY a.id DESC LIMIT ?"; args.append(limit)
        with self.store.lock:
            rows = self.store.conn.execute(sql, args).fetchall()
        return [{"id": r["id"], "record_id": r["record_id"], "station": r["station"],
                 "report_date": r["report_date"], "kind": r["kind"],
                 "title": ALERT_TITLES.get(r["kind"], r["kind"]), "message": r["message"],
                 "created_at": r["created_at"], "cleared_at": r["cleared_at"], "emailed": bool(r["emailed"]),
                 "read": bool(r["is_read"])} for r in rows]

    def mark_read(self, user, ids):
        with self.store.lock:
            self.store.conn.executemany("INSERT OR IGNORE INTO alert_reads (alert_id, user_id) VALUES (?, ?)",
                                        [(int(i), user["id"]) for i in ids])
            self.store.conn.commit()


# ── Credit customer accounts ──────────────────────────────────────────────────
class Credit:
    """Customers who fuel on credit: invoices come from the saved daily records,
    payments are recorded here; the balance is invoices minus payments."""

    def __init__(self, store):
        self.store = store
        store.conn.executescript("""
            CREATE TABLE IF NOT EXISTS customers (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                name         TEXT NOT NULL,
                company      TEXT NOT NULL DEFAULT '',
                phone        TEXT NOT NULL DEFAULT '',
                credit_limit REAL NOT NULL DEFAULT 0,
                created_by   TEXT NOT NULL,
                created_at   TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS credit_payments (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id INTEGER NOT NULL,
                paid_on     TEXT NOT NULL,
                amount      REAL NOT NULL,
                method      TEXT NOT NULL,
                reference   TEXT NOT NULL DEFAULT '',
                station     TEXT NOT NULL DEFAULT '',
                by_user     TEXT NOT NULL,
                at          TEXT NOT NULL,
                voided_at   TEXT,
                voided_by   TEXT
            );
        """)
        store.conn.commit()

    @staticmethod
    def _clean(body, partial=False):
        out = {}
        for key, limit in (("name", 80), ("company", 120), ("phone", 30)):
            if key in body or not partial:
                out[key] = " ".join(str(body.get(key) or "").split())[:limit]
        if not partial and not out["name"]:
            raise Error(HTTPStatus.BAD_REQUEST, "Enter the customer's name")
        if "credit_limit" in body:
            lim = num(body.get("credit_limit"))
            if lim < 0 or lim > 100_000_000:
                raise Error(HTTPStatus.BAD_REQUEST, "Check the credit limit")
            out["credit_limit"] = r2(lim)
        return out

    def create(self, body, by, now, can_set_limit):
        data = self._clean(body)
        if not can_set_limit:
            data.pop("credit_limit", None)
        with self.store.lock:
            cur = self.store.conn.execute(
                "INSERT INTO customers (name, company, phone, credit_limit, created_by, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (data["name"], data["company"], data["phone"], data.get("credit_limit", 0), by, now))
            self.store.conn.commit()
        return self.get(cur.lastrowid)

    def update(self, cid, body):
        data = self._clean(body, partial=True)
        if not data:
            return self.get(cid)
        with self.store.lock:
            cur = self.store.conn.execute(
                "UPDATE customers SET %s WHERE id = ?" % ", ".join("%s = ?" % k for k in data),
                list(data.values()) + [cid])
            self.store.conn.commit()
        if not cur.rowcount:
            raise Error(HTTPStatus.NOT_FOUND, "Customer not found")
        return self.get(cid)

    def _invoices(self):
        """Every credit invoice on a live (not binned) record that names a customer account."""
        with self.store.lock:
            rows = self.store.conn.execute(
                "SELECT id, report_date, station, data FROM records WHERE deleted_at IS NULL").fetchall()
        out = []
        for row in rows:
            for inv in json.loads(row["data"]).get("invoices") or []:
                cid = int(num(inv.get("customer_id")))
                if cid:
                    out.append({"customer_id": cid, "date": row["report_date"], "station": row["station"],
                                "record_id": row["id"], "invoice_no": inv.get("invoiceNo") or "",
                                "fuel": inv.get("fuelType") or "", "car_reg": inv.get("carReg") or "",
                                "amount": r2(num(inv.get("amount")))})
        return out

    def _payments(self, cid=None):
        sql, args = "SELECT * FROM credit_payments WHERE voided_at IS NULL", []
        if cid:
            sql += " AND customer_id = ?"; args.append(cid)
        with self.store.lock:
            return [dict(r) for r in self.store.conn.execute(sql, args).fetchall()]

    def list(self):
        with self.store.lock:
            rows = [dict(r) for r in self.store.conn.execute("SELECT * FROM customers ORDER BY name").fetchall()]
        invoiced, paid = {}, {}
        for inv in self._invoices():
            invoiced[inv["customer_id"]] = invoiced.get(inv["customer_id"], 0) + inv["amount"]
        for p in self._payments():
            paid[p["customer_id"]] = paid.get(p["customer_id"], 0) + p["amount"]
        for c in rows:
            c["invoiced"] = r2(invoiced.get(c["id"], 0))
            c["paid"] = r2(paid.get(c["id"], 0))
            c["balance"] = r2(c["invoiced"] - c["paid"])
            c["over_limit"] = bool(c["credit_limit"] and c["balance"] > c["credit_limit"])
        return rows

    def get(self, cid):
        for c in self.list():
            if c["id"] == cid:
                return c
        return None

    def statement(self, cid):
        cust = self.get(cid)
        if not cust:
            raise Error(HTTPStatus.NOT_FOUND, "Customer not found")
        entries = [{"type": "invoice", "date": i["date"], "station": i["station"],
                    "detail": " · ".join(x for x in (i["invoice_no"], i["fuel"], i["car_reg"]) if x),
                    "amount": i["amount"], "record_id": i["record_id"]}
                   for i in self._invoices() if i["customer_id"] == cid]
        entries += [{"type": "payment", "date": p["paid_on"], "station": p["station"],
                     "detail": " · ".join(x for x in (p["method"], p["reference"], "by " + p["by_user"]) if x),
                     "amount": -p["amount"], "payment_id": p["id"]}
                    for p in self._payments(cid)]
        entries.sort(key=lambda e: (e["date"], 0 if e["type"] == "invoice" else 1))
        bal = 0.0
        for e in entries:
            bal = r2(bal + e["amount"])
            e["balance"] = bal
        return {"customer": cust, "entries": entries}

    def add_payment(self, cid, body, by, station, now, today):
        if not self.get(cid):
            raise Error(HTTPStatus.NOT_FOUND, "Customer not found")
        amount = r2(num(body.get("amount")))
        if amount <= 0 or amount > 100_000_000:
            raise Error(HTTPStatus.BAD_REQUEST, "Enter the amount paid")
        paid_on = str(body.get("paid_on") or today)
        if len(paid_on) != 10 or paid_on > today:
            raise Error(HTTPStatus.BAD_REQUEST, "Check the payment date")
        method = str(body.get("method") or "M-Pesa")[:30]
        with self.store.lock:
            cur = self.store.conn.execute(
                "INSERT INTO credit_payments (customer_id, paid_on, amount, method, reference, station, by_user, at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (cid, paid_on, amount, method, str(body.get("reference") or "")[:60], station or "", by, now))
            self.store.conn.commit()
        return cur.lastrowid

    def void_payment(self, pid, by, now):
        with self.store.lock:
            cur = self.store.conn.execute(
                "UPDATE credit_payments SET voided_at = ?, voided_by = ? WHERE id = ? AND voided_at IS NULL",
                (now, by, pid))
            self.store.conn.commit()
        if not cur.rowcount:
            raise Error(HTTPStatus.NOT_FOUND, "Payment not found")


# ── Monthly report ────────────────────────────────────────────────────────────
REPORT_FIELDS = [
    ("petrol_litres", "Petrol litres"), ("diesel_litres", "Diesel litres"), ("kero_litres", "Kerosene litres"),
    ("total_litres", "Total litres"), ("gross_revenue", "Sales (KES)"), ("total_fuel_cost", "Fuel cost (KES)"),
    ("total_expenses", "Expenses (KES)"), ("net_profit", "Net profit (KES)"),
    ("output_vat", "VAT (KES)"), ("total_excise", "Excise (KES)"), ("total_tax", "Tax total (KES)"),
    ("mpesa_amount", "M-Pesa (KES)"), ("cash_amount", "Cash (KES)"), ("card_amount", "Card (KES)"),
    ("credit_amount", "Credit sales (KES)"), ("collection_variance", "Sales not collected (KES)"),
    ("attendant_shortage_total", "Attendant shortages (KES)"),
    ("petrol_delivered", "Petrol delivered (L)"), ("diesel_delivered", "Diesel delivered (L)"),
    ("kero_delivered", "Kerosene delivered (L)"), ("delivery_shortfall_litres", "Delivery shortfall (L)"),
    ("dip_variance_total", "Dip variance (L)"),
]


def monthly_report(records, month):
    """Totals per branch and for all branches for one month (records already filtered)."""
    branches = {}
    daily = []
    for r in sorted(records, key=lambda x: (x["report_date"], x["station"])):
        b = branches.setdefault(r["station"], {"station": r["station"], "days": 0, "approved_days": 0,
                                               "flagged_days": 0, **{k: 0.0 for k, _ in REPORT_FIELDS}})
        b["days"] += 1
        b["approved_days"] += 1 if r.get("approved_at") else 0
        b["flagged_days"] += 1 if r.get("opening_mismatches") else 0
        dip_var = sum(num(r.get(f + "_dip_variance")) for f in FUELS if num(r.get(f + "_dip_close")) > 0)
        for key, _ in REPORT_FIELDS:
            b[key] += dip_var if key == "dip_variance_total" else num(r.get(key))
        daily.append({"date": r["report_date"], "station": r["station"],
                      "gross_revenue": r2(num(r.get("gross_revenue"))), "net_profit": r2(num(r.get("net_profit"))),
                      "total_litres": r2(num(r.get("total_litres"))), "approved": bool(r.get("approved_at"))})
    rows = []
    for b in sorted(branches.values(), key=lambda x: x["station"]):
        rows.append({k: (r2(v) if isinstance(v, float) else v) for k, v in b.items()})
    totals = {"station": "All branches", "days": sum(b["days"] for b in rows),
              "approved_days": sum(b["approved_days"] for b in rows),
              "flagged_days": sum(b["flagged_days"] for b in rows)}
    for key, _ in REPORT_FIELDS:
        totals[key] = r2(sum(b[key] for b in rows))
    return {"month": month, "fields": [{"key": k, "label": l} for k, l in REPORT_FIELDS],
            "branches": rows, "totals": totals, "daily": daily}


# ── Receipt photos ────────────────────────────────────────────────────────────
UPLOAD_MAX_BYTES = 3 * 1024 * 1024
UPLOAD_ID_RE = re.compile(r"^[0-9a-f]{32}$")
IMAGE_TYPES = [(b"\xff\xd8\xff", "image/jpeg"), (b"\x89PNG\r\n\x1a\n", "image/png")]


def sniff_image(data):
    """The image type from the file's first bytes (never trust the name or the browser)."""
    for magic, mime in IMAGE_TYPES:
        if data.startswith(magic):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


class Uploads:
    """Photos of receipts and delivery notes, stored as files next to the database."""

    def __init__(self, store, folder):
        self.store, self.folder = store, folder
        os.makedirs(folder, exist_ok=True)
        store.conn.execute("""
            CREATE TABLE IF NOT EXISTS uploads (
                id       TEXT PRIMARY KEY,
                station  TEXT NOT NULL,
                mime     TEXT NOT NULL,
                size     INTEGER NOT NULL,
                sha256   TEXT NOT NULL,
                by_user  TEXT NOT NULL,
                at       TEXT NOT NULL
            )""")
        store.conn.commit()

    def save(self, data_b64, station, by, now):
        raw = str(data_b64 or "")
        if raw.startswith("data:"):
            raw = raw.split(",", 1)[-1]
        try:
            data = base64.b64decode(raw, validate=True)
        except (ValueError, TypeError):
            raise Error(HTTPStatus.BAD_REQUEST, "The photo could not be read")
        if not data or len(data) > UPLOAD_MAX_BYTES:
            raise Error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Photos must be smaller than 3 MB")
        mime = sniff_image(data)
        if not mime:
            raise Error(HTTPStatus.BAD_REQUEST, "Only JPEG, PNG or WebP photos can be uploaded")
        uid = secrets.token_hex(16)
        with open(os.path.join(self.folder, uid), "wb") as f:
            f.write(data)
        with self.store.lock:
            self.store.conn.execute(
                "INSERT INTO uploads (id, station, mime, size, sha256, by_user, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (uid, station, mime, len(data), hashlib.sha256(data).hexdigest(), by, now))
            self.store.conn.commit()
        return {"id": uid, "mime": mime, "size": len(data)}

    def get(self, uid):
        if not UPLOAD_ID_RE.match(uid or ""):
            return None
        with self.store.lock:
            row = self.store.conn.execute("SELECT * FROM uploads WHERE id = ?", (uid,)).fetchone()
        return dict(row) if row else None

    def read(self, uid):
        with open(os.path.join(self.folder, uid), "rb") as f:
            return f.read()
