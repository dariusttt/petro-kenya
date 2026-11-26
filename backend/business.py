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
import sqlite3
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

import pdf
from datetime import datetime, timedelta, timezone
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
    "backup_emails": "",             # comma-separated addresses that get the nightly backup
    "mpesa_mismatch_kes": 100,       # typed M-Pesa total differs this much from what Safaricom reported
    "rtt_alert_litres": 20,          # a pump had more than this returned to tank (RTT) in a day
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
            if key in ("alert_emails", "backup_emails"):
                emails = [e.strip() for e in str(val or "").replace(";", ",").split(",") if e.strip()]
                bad = [e for e in emails if "@" not in e or " " in e or len(e) > 120]
                if bad or len(emails) > 20:
                    raise Error(HTTPStatus.BAD_REQUEST, "Check the %s email addresses" % key.split("_")[0])
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
        # Pasted settings often carry stray spaces or line breaks; Gmail shows app passwords
        # in groups of four ("abcd efgh ijkl mnop"), which must be sent without the spaces
        self.host = os.environ.get("PK_SMTP_HOST", "").strip()
        self.port = int((os.environ.get("PK_SMTP_PORT", "") or "587").strip() or 587)
        self.user = os.environ.get("PK_SMTP_USER", "").strip()
        self.password = os.environ.get("PK_SMTP_PASSWORD", "").strip()
        if self.host.lower() in ("smtp.gmail.com", "smtp.googlemail.com"):
            self.password = "".join(self.password.split())
        self.sender = os.environ.get("PK_SMTP_FROM", "").strip() or self.user
        self.sent = []   # (recipients, subject) — kept for tests and the settings page

    @property
    def configured(self):
        return bool(self.host and self.sender)

    def send_now(self, recipients, subject, body, attachments=()):
        msg = EmailMessage()
        msg["From"] = self.sender
        msg["To"] = ", ".join(recipients)
        msg["Subject"] = subject
        msg.set_content(body)
        for filename, data in attachments:
            subtype = "pdf" if filename.lower().endswith(".pdf") else "zip"
            msg.add_attachment(data, maintype="application", subtype=subtype, filename=filename)
        if self.port == 465:
            server = smtplib.SMTP_SSL(self.host, self.port, timeout=20)
        else:
            server = smtplib.SMTP(self.host, self.port, timeout=20)
            server.starttls()
        try:
            if self.user:
                try:
                    server.login(self.user, self.password)
                except smtplib.SMTPServerDisconnected:
                    # Gmail refuses a wrong password and hangs up before Python can try again
                    raise smtplib.SMTPAuthenticationError(
                        535, b"Username and Password not accepted (the mail server hung up during sign-in)")
            server.send_message(msg)
        finally:
            # Gmail hangs up after refusing a password; closing must not hide that real reason
            try:
                server.quit()
            except Exception:
                server.close()
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
    # RTT can hide fuel that was really sold ("we poured it back"), so large amounts are flagged
    for key in sorted(k for k in rec if re.fullmatch(r"pump_[a-z]\d_rtt", k)):
        litres = num(rec.get(key))
        if litres > settings["rtt_alert_litres"]:
            pid = key.split("_")[1].upper()
            note = str(rec.get("rtt_note") or "").strip()
            out.append(("rtt", pid, "Pump %s: %s L returned to tank (RTT)%s — %s"
                        % (pid, "{:,.0f}".format(litres), " — reason: " + note[:80] if note else ", no reason given", where)))
    if rec.get("mpesa_received_count"):
        diff = num(rec.get("mpesa_amount")) - num(rec.get("mpesa_received"))
        if abs(diff) > settings["mpesa_mismatch_kes"]:
            out.append(("mpesa_mismatch", "day", "M-Pesa entered (KES %s) is KES %s %s than Safaricom reported (KES %s) — %s"
                        % ("{:,.2f}".format(num(rec.get("mpesa_amount"))), "{:,.2f}".format(abs(diff)),
                           "more" if diff > 0 else "less", "{:,.2f}".format(num(rec.get("mpesa_received"))), where)))
    return out


ALERT_TITLES = {
    "dip_shortage": "Tank stock short", "low_stock": "Low tank stock", "cash_shortage": "Cash short",
    "attendant_shortage": "Attendant short", "opening_mismatch": "Opening readings changed",
    "short_delivery": "Short delivery", "mpesa_mismatch": "M-Pesa doesn't match Safaricom",
    "rtt": "Large return to tank (RTT)",
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
        if "name" in out and not out["name"]:
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
        try:
            datetime.strptime(paid_on, "%Y-%m-%d")
        except ValueError:
            raise Error(HTTPStatus.BAD_REQUEST, "Check the payment date")
        if paid_on > today:
            raise Error(HTTPStatus.BAD_REQUEST, "A payment can't be dated in the future")
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
    ("dip_variance_total", "Dip variance (L)"), ("total_rtt", "Returned to tank, RTT (L)"),
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


def explain_mail_error(e):
    """The mail server's refusal in plain words, with what to do about it."""
    text = str(e)
    code = getattr(e, "smtp_code", None)
    if code == 534 or "Application-specific password" in text or "InvalidSecondFactor" in text:
        return ("Gmail needs an app password, not the normal Gmail password. Create one at "
                "myaccount.google.com/apppasswords and put it in PK_SMTP_PASSWORD on the server.")
    if code == 535 or "Username and Password not accepted" in text or "BadCredentials" in text:
        return ("Gmail didn't accept the sign-in. Check that PK_SMTP_USER is the full Gmail address and "
                "PK_SMTP_PASSWORD is a 16-letter app password for that same account, typed without spaces.")
    if isinstance(e, (TimeoutError, ConnectionRefusedError, OSError)) and not isinstance(e, smtplib.SMTPException):
        return ("The server couldn't reach the mail server (%s). Check PK_SMTP_HOST (smtp.gmail.com) and "
                "PK_SMTP_PORT (587). On Render, email only works on a paid instance — the Free plan blocks it." % text)
    return "The mail server refused: %s" % text


# ── SMS ───────────────────────────────────────────────────────────────────────
class Sms:
    """Sends text messages through Africa's Talking (https://africastalking.com), set in the
    environment: PK_SMS_USERNAME, PK_SMS_API_KEY and optionally PK_SMS_SENDER (an approved
    sender name). The username "sandbox" uses their free test system."""

    def __init__(self):
        self.username = os.environ.get("PK_SMS_USERNAME", "")
        self.api_key = os.environ.get("PK_SMS_API_KEY", "")
        self.sender = os.environ.get("PK_SMS_SENDER", "")
        self.url = os.environ.get("PK_SMS_URL") or (
            "https://api.sandbox.africastalking.com/version1/messaging" if self.username == "sandbox"
            else "https://api.africastalking.com/version1/messaging")
        self.sent = []   # phone numbers, kept for tests

    @property
    def configured(self):
        return bool(self.username and self.api_key)

    def send_now(self, phone, message):
        form = {"username": self.username, "to": phone, "message": message}
        if self.sender:
            form["from"] = self.sender
        req = urllib.request.Request(self.url, data=urllib.parse.urlencode(form).encode(), method="POST", headers={
            "apiKey": self.api_key, "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read() or b"{}")
        recipients = (data.get("SMSMessageData") or {}).get("Recipients") or []
        if not recipients or recipients[0].get("status") != "Success":
            raise RuntimeError((data.get("SMSMessageData") or {}).get("Message") or "SMS not accepted")
        self.sent.append(phone)

    def send(self, phone, message):
        """Send in the background; failures are logged, never shown to the person asking."""
        if not self.configured or not phone:
            return False

        def run():
            try:
                self.send_now(phone, message)
            except Exception as e:  # pragma: no cover - depends on the SMS service
                sys.stderr.write("SMS failed: %r\n" % e)
        threading.Thread(target=run, daemon=True).start()
        return True


# ── M-Pesa (Safaricom Daraja) ─────────────────────────────────────────────────
DARAJA_URLS = {"sandbox": "https://sandbox.safaricom.co.ke", "production": "https://api.safaricom.co.ke"}
MPESA_CODE_RE = re.compile(r"^\d{5,7}$")


class Mpesa:
    """Payments to the branches' tills or paybills, reported by Safaricom the moment they're made
    (Daraja C2B). Settings: PK_MPESA_CONSUMER_KEY, PK_MPESA_CONSUMER_SECRET and PK_MPESA_ENV
    ("sandbox" while testing, "production" once Safaricom has approved Go Live)."""

    def __init__(self, store):
        self.store = store
        self.key = os.environ.get("PK_MPESA_CONSUMER_KEY", "").strip()
        self.secret = os.environ.get("PK_MPESA_CONSUMER_SECRET", "").strip()
        self.env = "production" if os.environ.get("PK_MPESA_ENV", "").strip().lower() == "production" else "sandbox"
        self.base = (os.environ.get("PK_MPESA_BASE_URL") or DARAJA_URLS[self.env]).rstrip("/")
        self.register_path = os.environ.get("PK_MPESA_REGISTER_PATH") or (
            "/mpesa/c2b/v2/registerurl" if self.env == "production" else "/mpesa/c2b/v1/registerurl")
        self._token = (None, 0.0)
        c = store.conn
        c.execute("""CREATE TABLE IF NOT EXISTS mpesa_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT, trans_id TEXT NOT NULL UNIQUE, shortcode TEXT NOT NULL,
            station TEXT, amount REAL NOT NULL, paid_at TEXT NOT NULL, report_date TEXT NOT NULL,
            phone TEXT, name TEXT, bill_ref TEXT, kind TEXT, raw TEXT NOT NULL, received_at TEXT NOT NULL)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_mpesa_day ON mpesa_payments (station, report_date)")
        c.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        cols = [r["name"] for r in c.execute("PRAGMA table_info(stations)").fetchall()]
        for col in ("mpesa_shortcode", "mpesa_type", "mpesa_registered_at"):
            if col not in cols:
                c.execute("ALTER TABLE stations ADD COLUMN %s TEXT" % col)
        c.commit()

    @property
    def configured(self):
        return bool(self.key and self.secret)

    def callback_secret(self):
        """The secret part of the address Safaricom reports payments to. Safaricom doesn't sign its
        messages, so only someone who knows this address can post one."""
        env = os.environ.get("PK_MPESA_CALLBACK_SECRET", "").strip()
        if env:
            return env
        with self.store.lock:
            row = self.store.conn.execute("SELECT value FROM kv WHERE key = 'mpesa_callback_secret'").fetchone()
            if row:
                return row["value"]
            value = secrets.token_hex(16)
            self.store.conn.execute("INSERT INTO kv (key, value) VALUES ('mpesa_callback_secret', ?)", (value,))
            self.store.conn.commit()
            return value

    # ── Branch tills ──
    def branches(self):
        with self.store.lock:
            rows = self.store.conn.execute(
                "SELECT name, mpesa_shortcode, mpesa_type, mpesa_registered_at FROM stations ORDER BY id").fetchall()
        return [{"station": r["name"], "shortcode": r["mpesa_shortcode"] or "", "type": r["mpesa_type"] or "till",
                 "registered_at": r["mpesa_registered_at"]} for r in rows]

    def branch(self, station):
        found = next((b for b in self.branches() if b["station"] == station), None)
        if not found:
            raise Error(HTTPStatus.NOT_FOUND, "Branch not found")
        return found

    def set_till(self, station, shortcode, kind):
        self.branch(station)
        shortcode = re.sub(r"\s", "", str(shortcode or ""))
        kind = kind if kind in ("till", "paybill") else "till"
        if shortcode and not MPESA_CODE_RE.match(shortcode):
            raise Error(HTTPStatus.BAD_REQUEST, "A till or paybill number is 5 to 7 digits")
        other = next((b["station"] for b in self.branches() if shortcode and b["shortcode"] == shortcode
                      and b["station"] != station), None)
        if other:
            raise Error(HTTPStatus.CONFLICT, "%s already uses %s" % (other, shortcode))
        with self.store.lock:
            self.store.conn.execute("UPDATE stations SET mpesa_shortcode = ?, mpesa_type = ?, mpesa_registered_at = NULL "
                                    "WHERE name = ?", (shortcode or None, kind, station))
            # Payments that came in for this number before it was assigned now belong to the branch
            if shortcode:
                self.store.conn.execute("UPDATE mpesa_payments SET station = ? WHERE shortcode = ? AND station IS NULL",
                                        (station, shortcode))
            self.store.conn.commit()
        return self.branch(station)

    def station_for(self, shortcode):
        return next((b["station"] for b in self.branches() if b["shortcode"] and b["shortcode"] == str(shortcode)), None)

    # ── Payments reported by Safaricom ──
    def record(self, payload, now):
        """Store one confirmation from Safaricom. Repeats of the same M-Pesa code are ignored."""
        if not isinstance(payload, dict):
            raise Error(HTTPStatus.BAD_REQUEST, "Expected a JSON object")
        trans_id = str(payload.get("TransID") or "").strip()[:30]
        amount = num(payload.get("TransAmount"))
        shortcode = str(payload.get("BusinessShortCode") or "").strip()[:20]
        if not trans_id or amount <= 0 or not shortcode:
            raise Error(HTTPStatus.BAD_REQUEST, "Missing TransID, TransAmount or BusinessShortCode")
        raw_time = str(payload.get("TransTime") or "")
        try:
            paid = datetime.strptime(raw_time, "%Y%m%d%H%M%S")      # Safaricom sends Kenya time
        except ValueError:
            paid = datetime.now(KENYA).replace(tzinfo=None)
        name = " ".join(str(payload.get(k) or "") for k in ("FirstName", "MiddleName", "LastName")).split()
        name = " ".join(name)
        station = self.station_for(shortcode)          # (looked up before taking the database lock)
        try:
            with self.store.lock:
                self.store.conn.execute(
                    "INSERT INTO mpesa_payments (trans_id, shortcode, station, amount, paid_at, report_date, phone, name,"
                    " bill_ref, kind, raw, received_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (trans_id, shortcode, station, round(amount, 2),
                     paid.strftime("%Y-%m-%dT%H:%M:%S"), paid.strftime("%Y-%m-%d"),
                     str(payload.get("MSISDN") or "")[:80], name[:120], str(payload.get("BillRefNumber") or "")[:60],
                     str(payload.get("TransactionType") or "")[:40], json.dumps(payload)[:4000], now))
                self.store.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def payments(self, station, date):
        with self.store.lock:
            rows = self.store.conn.execute(
                "SELECT trans_id, amount, paid_at, phone, name, bill_ref, shortcode FROM mpesa_payments "
                "WHERE station = ? AND report_date = ? ORDER BY paid_at", (station, date)).fetchall()
        items = [dict(r) for r in rows]
        return {"payments": items, "count": len(items), "total": round(sum(p["amount"] for p in items), 2)}

    def unassigned(self):
        with self.store.lock:
            return self.store.conn.execute(
                "SELECT shortcode, COUNT(*) AS n, SUM(amount) AS total FROM mpesa_payments WHERE station IS NULL "
                "GROUP BY shortcode").fetchall()

    # ── Talking to Safaricom ──
    def _call(self, method, path, body=None, auth=None):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": auth or "Bearer " + self._access_token(),
                                              "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read() or b"{}")
            except ValueError:
                detail = {}
            msg = detail.get("errorMessage") or detail.get("ResponseDescription") or detail.get("error_description") or str(e)
            raise Error(HTTPStatus.BAD_GATEWAY, "Safaricom refused: %s" % msg)
        except (OSError, ValueError) as e:
            raise Error(HTTPStatus.BAD_GATEWAY, "Couldn't reach Safaricom: %s" % e)

    def _access_token(self):
        token, until = self._token
        if token and time.time() < until - 60:
            return token
        if not self.configured:
            raise Error(HTTPStatus.BAD_REQUEST, "M-Pesa isn't set up on the server (PK_MPESA_CONSUMER_KEY and "
                                                "PK_MPESA_CONSUMER_SECRET)")
        basic = base64.b64encode(("%s:%s" % (self.key, self.secret)).encode()).decode()
        data = self._call("GET", "/oauth/v1/generate?grant_type=client_credentials", auth="Basic " + basic)
        token = data.get("access_token")
        if not token:
            raise Error(HTTPStatus.BAD_GATEWAY, "Safaricom didn't accept the consumer key and secret")
        self._token = (token, time.time() + float(data.get("expires_in") or 3599))
        return token

    def register(self, station, base_url):
        """Tell Safaricom where to report this branch's payments."""
        b = self.branch(station)
        if not b["shortcode"]:
            raise Error(HTTPStatus.BAD_REQUEST, "Add the branch's till or paybill number first")
        if not base_url.startswith("https://"):
            raise Error(HTTPStatus.BAD_REQUEST, "Safaricom only reports payments to an https:// address — "
                                                "connect from the hosted site, not a local copy")
        # Safaricom rejects addresses containing words such as "mpesa", hence /api/c2b/…
        hook = "%s/api/c2b/%s" % (base_url.rstrip("/"), self.callback_secret())
        res = self._call("POST", self.register_path, {
            "ShortCode": b["shortcode"], "ResponseType": "Completed",
            "ConfirmationURL": hook + "/confirm", "ValidationURL": hook + "/validate"})
        if str(res.get("ResponseCode", "0")) not in ("0", "00000000"):
            raise Error(HTTPStatus.BAD_GATEWAY, "Safaricom refused: %s" % (res.get("ResponseDescription") or res))
        with self.store.lock:
            self.store.conn.execute("UPDATE stations SET mpesa_registered_at = ? WHERE name = ?",
                                    (datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"), station))
            self.store.conn.commit()
        return self.branch(station)

    def simulate(self, station, amount):
        """Sandbox only: ask Safaricom to send a pretend payment to the branch's number."""
        if self.env != "sandbox":
            raise Error(HTTPStatus.BAD_REQUEST, "Test payments only work while PK_MPESA_ENV is sandbox")
        b = self.branch(station)
        if not b["shortcode"]:
            raise Error(HTTPStatus.BAD_REQUEST, "Add the branch's till or paybill number first")
        amount = int(num(amount))
        if amount < 1 or amount > 70000:
            raise Error(HTTPStatus.BAD_REQUEST, "Choose an amount between 1 and 70,000")
        # Safaricom only accepts a short account reference of letters and numbers ("The element
        # AccountReference is invalid" otherwise); a till has no account reference at all
        return self._call("POST", "/mpesa/c2b/v1/simulate", {
            "ShortCode": b["shortcode"], "Amount": amount, "Msisdn": "254708374149",
            "CommandID": "CustomerPayBillOnline" if b["type"] == "paybill" else "CustomerBuyGoodsOnline",
            "BillRefNumber": "PKTEST" if b["type"] == "paybill" else None})


# ── The readable side of a backup: a PDF of every saved day ────────────────────
def backup_report_pdf(db_file, made_at, photos=0):
    """A PDF anyone can open, from a backup's database: every saved day per branch with
    branch totals, and the users. (Restoring uses the backup's .zip, not this.)"""
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    try:
        records = [json.loads(r["data"]) for r in conn.execute(
            "SELECT data FROM records WHERE deleted_at IS NULL ORDER BY station, report_date")]
        users = conn.execute("SELECT username, full_name, role, station FROM users ORDER BY role, username").fetchall()
    finally:
        conn.close()
    money = lambda v: "{:,.0f}".format(num(v))
    litres = lambda v: "{:,.0f}".format(num(v))
    doc = pdf.Document("PetroKenya backup report - %s" % made_at)
    doc.heading("PetroKenya - Backup Report", 18)
    doc.text("Made %s.  %d saved day%s across %d branch%s, %d user%s, %d receipt photo%s." % (
        made_at, len(records), "" if len(records) == 1 else "s",
        len({r["station"] for r in records}), "" if len({r["station"] for r in records}) == 1 else "es",
        len(users), "" if len(users) == 1 else "s", photos, "" if photos == 1 else "s"))
    doc.text("This PDF is for reading and printing. To put the data back into the app, use the backup's "
             ".zip file (python3 backend/server.py restore FILE.zip).", 8.5, grey=True)
    cols = [("Date", 10, "l"), ("Petrol L", 9, "r"), ("Diesel L", 9, "r"), ("Kero L", 8, "r"),
            ("Sales KES", 12, "r"), ("M-Pesa", 11, "r"), ("Cash", 11, "r"), ("Credit", 10, "r"),
            ("Expenses", 10, "r"), ("Profit", 11, "r"), ("RTT L", 6, "r"), ("Status", 9, "l"), ("Saved by", 12, "l")]
    keys = ["petrol_litres", "diesel_litres", "kero_litres", "gross_revenue", "mpesa_amount", "cash_amount",
            "credit_amount", "total_expenses", "net_profit", "total_rtt"]
    by_station = {}
    for r in records:
        by_station.setdefault(r["station"], []).append(r)
    if not records:
        doc.space(10)
        doc.text("No days have been saved yet.", 10)
    for station, days in by_station.items():
        doc.space(8)
        doc.heading(station, 12)
        rows = [[d.get("report_date", "")] +
                [(litres if i < 3 or k == "total_rtt" else money)(d.get(k)) for i, k in enumerate(keys)] +
                ["Approved" if d.get("approved_at") else "Open", d.get("operator") or ""] for d in days]
        totals = ["Total"] + [(litres if i < 3 or k == "total_rtt" else money)(sum(num(d.get(k)) for d in days))
                              for i, k in enumerate(keys)] + ["%d day%s" % (len(days), "" if len(days) == 1 else "s"), ""]
        doc.table(cols, rows, totals)
    doc.space(10)
    doc.heading("Users", 12)
    doc.table([("Username", 20, "l"), ("Name", 30, "l"), ("Role", 10, "l"), ("Branch", 30, "l")],
              [[u["username"], u["full_name"] or "", u["role"], u["station"] or "All branches"] for u in users])
    return doc.render()


# ── Backups ───────────────────────────────────────────────────────────────────
BACKUP_KEEP = 14                       # copies kept on the server
BACKUP_EVERY = timedelta(hours=24)
BACKUP_EMAIL_LIMIT = 20 * 1024 * 1024  # most mail services refuse attachments much bigger than this
BACKUP_NAME_RE = re.compile(r"^petrokenya-backup-(\d{4}-\d{2}-\d{2}_\d{4})(?:-(\d+))?\.zip$")
UPLOAD_NAME_RE = re.compile(r"^[0-9a-f]{32}$")
DB_IN_ZIP = "petrokenya.sqlite3"
KENYA = timezone(timedelta(hours=3))


def snapshot_db(conn, lock, dest):
    """A consistent copy of a live SQLite database, taken with SQLite's own backup API."""
    out = sqlite3.connect(dest)
    try:
        with lock:
            conn.backup(out)
    finally:
        out.close()


class Backups:
    """Nightly zip of the database and receipt photos: kept on the server (last 14),
    emailed to the backup addresses (database only, to stay small) and downloadable."""

    def __init__(self, store, settings, mailer, uploads_dir, folder):
        self.store, self.settings, self.mailer = store, settings, mailer
        self.uploads_dir, self.folder = uploads_dir, folder
        self.lock = threading.Lock()
        self.last_email = None          # {"at", "to", "ok", "error"} of the latest emailed backup
        os.makedirs(folder, exist_ok=True)

    def list(self):
        def newest_first(name):
            m = BACKUP_NAME_RE.match(name)
            return (m.group(1), int(m.group(2) or 1))
        out = []
        for name in sorted((n for n in os.listdir(self.folder) if BACKUP_NAME_RE.match(n)), key=newest_first, reverse=True):
            full = os.path.join(self.folder, name)
            out.append({"name": name, "size": os.path.getsize(full),
                        "at": datetime.fromtimestamp(os.path.getmtime(full), timezone.utc)
                        .isoformat(timespec="seconds").replace("+00:00", "Z")})
        return out

    def path(self, name):
        """A backup's .zip (for restoring) or its .pdf (for reading). Backups made before PDFs
        existed get theirs made from the zip the first time it's asked for."""
        name = name or ""
        if name.endswith(".pdf"):
            zip_name = name[:-4] + ".zip"
            zip_path = self.path(zip_name)
            pdf_path = os.path.join(self.folder, name)
            if not os.path.isfile(pdf_path):
                with self.lock, tempfile.TemporaryDirectory() as tmp, zipfile.ZipFile(zip_path) as z:
                    z.extract(DB_IN_ZIP, tmp)
                    info = json.loads(z.read("backup-info.json")) if "backup-info.json" in z.namelist() else {}
                    self._write_pdf(os.path.join(tmp, DB_IN_ZIP), pdf_path,
                                    info.get("made_at", "")[:16].replace("T", " "), info.get("photos", 0))
            return pdf_path
        if not BACKUP_NAME_RE.match(name) or not os.path.isfile(os.path.join(self.folder, name)):
            raise Error(HTTPStatus.NOT_FOUND, "Backup not found")
        return os.path.join(self.folder, name)

    @staticmethod
    def _write_pdf(db, pdf_path, made_at, photos):
        data = backup_report_pdf(db, made_at, photos)
        with open(pdf_path + ".part", "wb") as f:
            f.write(data)
        os.replace(pdf_path + ".part", pdf_path)

    def _db_snapshot(self, tmpdir):
        db = os.path.join(tmpdir, DB_IN_ZIP)
        snapshot_db(self.store.conn, self.store.lock, db)
        return db

    def create(self, by="system"):
        """Write a new backup zip; returns its listing entry."""
        with self.lock, tempfile.TemporaryDirectory() as tmp:
            db = self._db_snapshot(tmp)
            stamp = datetime.now(KENYA).strftime("%Y-%m-%d_%H%M")
            # Same minute as an earlier backup: number it after the highest one, even if older ones were pruned
            taken = [int(m.group(2) or 1) for m in map(BACKUP_NAME_RE.match, os.listdir(self.folder))
                     if m and m.group(1) == stamp]
            n = max(taken) + 1 if taken else 1
            name = "petrokenya-backup-%s%s.zip" % (stamp, "-%d" % n if n > 1 else "")
            part = os.path.join(self.folder, name + ".part")
            with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED) as z:
                z.write(db, DB_IN_ZIP)
                photos = [f for f in os.listdir(self.uploads_dir) if UPLOAD_NAME_RE.match(f)] \
                    if os.path.isdir(self.uploads_dir) else []
                for f in photos:
                    z.write(os.path.join(self.uploads_dir, f), "uploads/" + f)
                z.writestr("backup-info.json", json.dumps(
                    {"made_at": datetime.now(KENYA).isoformat(timespec="seconds"), "by": by,
                     "photos": len(photos)}, indent=1))
            os.replace(part, os.path.join(self.folder, name))
            # The readable copy of the same moment
            self._write_pdf(db, os.path.join(self.folder, name[:-4] + ".pdf"),
                            datetime.now(KENYA).strftime("%Y-%m-%d %H:%M"), len(photos))
        for old in self.list()[BACKUP_KEEP:]:
            os.remove(os.path.join(self.folder, old["name"]))
            if os.path.exists(os.path.join(self.folder, old["name"][:-4] + ".pdf")):
                os.remove(os.path.join(self.folder, old["name"][:-4] + ".pdf"))
        return next(b for b in self.list() if b["name"] == name)

    def email(self):
        """Email the backup to the backup addresses: a PDF to read, and the database (zip) to
        restore from. Returns where it went."""
        to = [e.strip() for e in self.settings.all()["backup_emails"].split(",") if e.strip()]
        if not self.mailer.configured:
            raise Error(HTTPStatus.BAD_REQUEST, "Email isn't set up on the server (PK_SMTP_HOST and friends)")
        if not to:
            raise Error(HTTPStatus.BAD_REQUEST, "Add at least one backup email address first")
        day = datetime.now(KENYA).strftime("%Y-%m-%d")
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db_snapshot(tmp)
            buf = os.path.join(tmp, "db.zip")
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                z.write(db, DB_IN_ZIP)
            with open(buf, "rb") as f:
                data = f.read()
            photos = len([f for f in os.listdir(self.uploads_dir) if UPLOAD_NAME_RE.match(f)]) \
                if os.path.isdir(self.uploads_dir) else 0
            report = backup_report_pdf(db, datetime.now(KENYA).strftime("%Y-%m-%d %H:%M"), photos)
        self.last_email = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
                           "to": to, "ok": False, "error": None}
        if len(data) + len(report) > BACKUP_EMAIL_LIMIT:
            self.last_email["error"] = "The database is too big to email — download backups instead"
            raise Error(HTTPStatus.BAD_REQUEST, self.last_email["error"])
        try:
            self.mailer.send_now(
                to, "PetroKenya backup %s" % day,
                "Attached is the PetroKenya backup as of %s:\n\n"
                "  - petrokenya-report-%s.pdf: every saved day per branch, to open and read on any phone\n"
                "    or computer.\n"
                "  - petrokenya-db-%s.zip: the database itself, to put the app back as it was. It isn't\n"
                "    meant to be opened; to restore, stop the app and run\n"
                "        python3 backend/server.py restore petrokenya-db-%s.zip\n\n"
                "Receipt photos are not included (download a full backup from the app for those).\n"
                % (day, day, day, day),
                [("petrokenya-report-%s.pdf" % day, report), ("petrokenya-db-%s.zip" % day, data)])
        except Exception as e:
            self.last_email["error"] = explain_mail_error(e)[:300]
            raise Error(HTTPStatus.BAD_GATEWAY, self.last_email["error"])
        self.last_email["ok"] = True
        return to

    def due(self):
        newest = self.list()[:1]
        if not newest:
            return True
        made = datetime.fromisoformat(newest[0]["at"].replace("Z", "+00:00"))
        return datetime.now(timezone.utc) - made >= BACKUP_EVERY

    def run_due(self):
        """Back up (and email, if addresses are set) when the last backup is a day old."""
        if not self.due():
            return False
        self.create()
        if self.mailer.configured and self.settings.all()["backup_emails"]:
            try:
                self.email()
            except Error as e:
                sys.stderr.write("Backup email failed: %s\n" % e.message)
        return True

    def start(self, check_every=600):
        def loop():
            while True:
                try:
                    self.run_due()
                except Exception as e:  # pragma: no cover - keep the schedule alive
                    sys.stderr.write("Backup failed: %r\n" % e)
                time.sleep(check_every)
        threading.Thread(target=loop, daemon=True, name="backups").start()


def restore(zip_path, db_path, uploads_dir, backups_dir):
    """Put a backup back in place (the app must be stopped). The current database is
    saved into the backups folder first, so a restore can itself be undone."""
    try:
        z = zipfile.ZipFile(zip_path)
    except (OSError, zipfile.BadZipFile):
        raise Error(HTTPStatus.BAD_REQUEST, "That isn't a PetroKenya backup zip")
    with z, tempfile.TemporaryDirectory() as tmp:
        if DB_IN_ZIP not in z.namelist():
            raise Error(HTTPStatus.BAD_REQUEST, "No %s inside that zip" % DB_IN_ZIP)
        new_db = os.path.join(tmp, DB_IN_ZIP)
        with z.open(DB_IN_ZIP) as src, open(new_db, "wb") as dst:
            dst.write(src.read())
        try:
            check = sqlite3.connect(new_db)
            tables = {r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            ok = check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            days = check.execute("SELECT COUNT(*) FROM records").fetchone()[0] if "records" in tables else 0
            check.close()
        except sqlite3.DatabaseError:
            ok, tables = False, set()
        if not ok or not {"records", "users"} <= tables:
            raise Error(HTTPStatus.BAD_REQUEST, "The database in that zip is damaged or not PetroKenya's")
        saved = None
        if os.path.exists(db_path):
            os.makedirs(backups_dir, exist_ok=True)
            saved = os.path.join(backups_dir, "before-restore-%s.sqlite3" % datetime.now(KENYA).strftime("%Y-%m-%d_%H%M%S"))
            try:
                cur = sqlite3.connect(db_path)
                try:
                    snapshot_db(cur, threading.Lock(), saved)
                finally:
                    cur.close()
            except sqlite3.DatabaseError:        # damaged: keep its raw bytes instead
                _copy(db_path, saved)
        for extra in ("-wal", "-shm"):
            if os.path.exists(db_path + extra):
                os.remove(db_path + extra)
        _copy(new_db, db_path)
        photos = 0
        os.makedirs(uploads_dir, exist_ok=True)
        for name in z.namelist():
            base = name[len("uploads/"):] if name.startswith("uploads/") else None
            if base and UPLOAD_NAME_RE.match(base):       # only real photo names: nothing can escape the folder
                with z.open(name) as src, open(os.path.join(uploads_dir, base), "wb") as dst:
                    dst.write(src.read())
                photos += 1
    return {"days": days, "photos": photos, "saved_current_as": saved}


def _copy(src, dst):
    tmp = dst + ".restoring"
    with open(src, "rb") as a, open(tmp, "wb") as b:
        b.write(a.read())
    os.replace(tmp, dst)
