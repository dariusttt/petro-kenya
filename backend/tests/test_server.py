"""Tests for backend/server.py — run from the repo root with:

    python3 -m unittest discover -s backend/tests
"""

import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from http.cookiejar import CookieJar
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import server  # noqa: E402

SAMPLE = {
    "report_date": "2026-10-14",
    "station": "Westlands Branch",
    "petrol_open": 1000, "petrol_close": 1100, "petrol_price": 189.5, "petrol_cost": 174,
    "diesel_open": 500, "diesel_close": 550, "diesel_price": 172, "diesel_cost": 157,
    "mpesa_amount": 20000, "cash_amount": 7550,
    "exp_wages": 1000,
    "misc_items": [{"name": "Tip", "amount": 200}],
    "gas_items": [{"name": "6kg LPG", "category": "Gas", "qty": 2, "price": 1000}],
}
PASSWORD = "correct-horse"


class Client:
    """Tiny HTTP client that keeps its own session cookie."""

    def __init__(self, base):
        self.base = base
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))

    def req(self, method, path, body=None, ctype="application/json"):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": ctype} if data is not None else {}
        r = Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with self.opener.open(r) as resp:
                raw = resp.read()
                is_json = "json" in resp.headers.get("Content-Type", "")
                return resp.status, (json.loads(raw) if is_json else raw.decode()), resp.headers
        except HTTPError as e:
            return e.code, json.loads(e.read()), e.headers

    def login(self, username, password=PASSWORD):
        status, body, _ = self.req("POST", "/api/login", {"username": username, "password": password})
        assert status == 200, body
        return self


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.httpd = server.make_server("127.0.0.1", 0, os.path.join(cls.tmp.name, "t.sqlite3"),
                                       bootstrap=False)
        cls.port = cls.httpd.server_address[1]
        cls.base = "http://127.0.0.1:%d" % cls.port
        auth = cls.httpd.RequestHandlerClass.auth
        auth.create("boss", PASSWORD, "admin")
        auth.create("kisumu", PASSWORD, "staff", "Kisumu Branch")
        auth.create("roamer", PASSWORD, "staff")
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.httpd.RequestHandlerClass.store.conn.close()
        cls.tmp.cleanup()

    def setUp(self):
        self.httpd.RequestHandlerClass.auth.fails.clear()
        self.admin = Client(self.base).login("boss")
        self.admin.req("DELETE", "/api/records")

    def raw_get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request("GET", path)
        resp = conn.getresponse()
        resp.read()
        conn.close()
        return resp.status

    # ── App files ──
    def test_serves_frontend_files(self):
        anon = Client(self.base)
        status, html, _ = anon.req("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn('href="manifest.webmanifest"', html)
        self.assertIn('src="js/app.js"', html)
        for path, ctype in [("/css/styles.css", "text/css"), ("/js/app.js", "javascript"),
                            ("/sw.js", "javascript"), ("/manifest.webmanifest", "manifest+json"),
                            ("/icons/icon-192.png", "image/png")]:
            r = anon.opener.open(self.base + path)
            self.assertEqual(r.status, 200, path)
            self.assertIn(ctype, r.headers["Content-Type"], path)
        self.assertEqual(self.raw_get("/nope.html"), 404)
        self.assertEqual(self.raw_get("/../backend/server.py"), 404)
        self.assertEqual(self.raw_get("/%2e%2e/backend/server.py"), 404)

    # ── Sign-in ──
    def test_api_requires_login(self):
        anon = Client(self.base)
        self.assertEqual(anon.req("GET", "/api/health")[0], 200)
        self.assertEqual(anon.req("GET", "/api/records")[0], 401)
        self.assertEqual(anon.req("POST", "/api/compute", SAMPLE)[0], 401)

    def test_login_logout_and_cookie_flags(self):
        c = Client(self.base)
        status, body, headers = c.req("POST", "/api/login", {"username": "BOSS", "password": PASSWORD})
        self.assertEqual(status, 200)
        self.assertEqual(body["user"]["role"], "admin")
        cookie = headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        self.assertNotIn("pw_hash", json.dumps(body))
        self.assertEqual(c.req("GET", "/api/me")[1]["user"]["username"], "boss")
        c.req("POST", "/api/logout", {})
        self.assertEqual(c.req("GET", "/api/me")[0], 401)

    def test_wrong_password_and_throttle(self):
        c = Client(self.base)
        for _ in range(server.LOGIN_MAX_FAILS):
            self.assertEqual(c.req("POST", "/api/login", {"username": "boss", "password": "nope"})[0], 401)
        self.assertEqual(c.req("POST", "/api/login", {"username": "boss", "password": PASSWORD})[0], 429)

    def test_change_own_password(self):
        auth = self.httpd.RequestHandlerClass.auth
        auth.create("temp", PASSWORD, "staff")
        c = Client(self.base).login("temp")
        status, body, _ = c.req("POST", "/api/me/password",
                                {"current_password": "wrong", "new_password": "new-password-1"})
        self.assertEqual(status, 400)
        status, _, _ = c.req("POST", "/api/me/password",
                             {"current_password": PASSWORD, "new_password": "new-password-1"})
        self.assertEqual(status, 200)
        self.assertEqual(c.req("GET", "/api/me")[0], 200)  # re-issued session still works
        Client(self.base).login("temp", "new-password-1")
        auth.delete(auth.find("temp")["id"])

    def test_requires_json_content_type(self):
        status, _, _ = self.admin.req("POST", "/api/records", SAMPLE, ctype="text/plain")
        self.assertEqual(status, 415)

    # ── Records ──
    def test_compute_matches_dashboard_formulas(self):
        status, c, _ = self.admin.req("POST", "/api/compute", SAMPLE)
        self.assertEqual(status, 200)
        self.assertEqual(c["petrol_litres"], 100)
        self.assertEqual(c["diesel_litres"], 50)
        self.assertEqual(c["gas_lube_revenue"], 2000)
        self.assertEqual(c["gross_revenue"], 18950 + 8600 + 2000)
        self.assertEqual(c["total_expenses"], 1200)            # wages + misc items
        self.assertEqual(c["net_profit"], 29550 - (17400 + 7850) - 1200)
        self.assertEqual(c["output_vat"], round(29550 - round(29550 / 1.16, 2), 2))
        self.assertEqual(c["total_excise"], round(100 * 21.95 + 50 * 7.21, 2))
        self.assertEqual(c["collection_variance"], 29550 - 27550)

    def test_create_upsert_get_delete(self):
        status, res, _ = self.admin.req("POST", "/api/records", SAMPLE)
        self.assertEqual(status, 201)
        rid = res["record"]["id"]
        self.assertEqual(res["record"]["net_profit"], 3100)
        self.assertEqual(res["record"]["saved_by"], "boss")

        status, res, _ = self.admin.req("POST", "/api/records", {**SAMPLE, "exp_wages": 2000})
        self.assertEqual(status, 200)
        self.assertFalse(res["created"])
        self.assertEqual(res["record"]["id"], rid)
        self.assertEqual(res["record"]["net_profit"], 2100)

        self.assertEqual(self.admin.req("GET", "/api/records")[1]["count"], 1)
        self.assertEqual(self.admin.req("GET", "/api/records/%d" % rid)[0], 200)
        self.assertEqual(self.admin.req("DELETE", "/api/records/%d" % rid)[0], 200)
        self.assertEqual(self.admin.req("GET", "/api/records/%d" % rid)[0], 404)

    def test_filters_analytics_and_export(self):
        self.admin.req("POST", "/api/records", SAMPLE)
        self.admin.req("POST", "/api/records", {**SAMPLE, "report_date": "2026-10-13"})
        self.admin.req("POST", "/api/records", {**SAMPLE, "station": "Kisumu Branch"})

        self.assertEqual(self.admin.req("GET", "/api/records?station=Kisumu%20Branch")[1]["count"], 1)
        self.assertEqual(self.admin.req("GET", "/api/records?date=2026-10-14")[1]["count"], 2)
        a = self.admin.req("GET", "/api/analytics?station=Westlands%20Branch")[1]
        self.assertEqual(a["totals"]["days"], 2)
        self.assertEqual([d["report_date"] for d in a["last_7"]], ["2026-10-13", "2026-10-14"])
        status, csv_text, _ = self.admin.req("GET", "/api/export.csv")
        self.assertEqual(status, 200)
        self.assertEqual(len(csv_text.strip().splitlines()), 4)
        self.assertEqual(self.admin.req("DELETE", "/api/records?station=Kisumu%20Branch")[1]["deleted"], 1)

    def test_csv_export_neutralises_formulas(self):
        self.admin.req("POST", "/api/records", {**SAMPLE, "kra_business": "=HYPERLINK(\"http://x\")"})
        csv_text = self.admin.req("GET", "/api/export.csv")[1]
        self.assertIn("'=HYPERLINK", csv_text)
        self.assertNotIn(",=HYPERLINK", csv_text)

    def test_put_conflict(self):
        a = self.admin.req("POST", "/api/records", SAMPLE)[1]
        self.admin.req("POST", "/api/records", {**SAMPLE, "report_date": "2026-10-13"})
        status, err, _ = self.admin.req("PUT", "/api/records/%d" % a["record"]["id"],
                                        {**SAMPLE, "report_date": "2026-10-13"})
        self.assertEqual(status, 409)

    def test_validation_and_item_cleaning(self):
        self.assertEqual(self.admin.req("POST", "/api/records", {"station": "X"})[0], 400)
        self.assertEqual(self.admin.req("POST", "/api/records", {**SAMPLE, "report_date": "2026-13-40"})[0], 400)
        self.assertEqual(self.admin.req("POST", "/api/records", {**SAMPLE, "gas_items": "nope"})[0], 400)
        body = {**SAMPLE, "invoices": [{"id": "1); alert(1", "customer": "<b>x</b>", "amount": "50"}]}
        inv = self.admin.req("POST", "/api/records", body)[1]["record"]["invoices"][0]
        self.assertEqual(inv["id"], 0)          # non-numeric id cannot reach an onclick handler
        self.assertEqual(inv["amount"], 50)
        self.assertEqual(inv["customer"], "<b>x</b>")  # stored as text; the page escapes it

    # ── Permissions ──
    def test_branch_staff_are_limited_to_their_branch(self):
        self.admin.req("POST", "/api/records", SAMPLE)
        other_id = self.admin.req("GET", "/api/records")[1]["records"][0]["id"]
        staff = Client(self.base).login("kisumu")

        self.assertEqual(staff.req("GET", "/api/records")[1]["count"], 0)
        self.assertEqual(staff.req("GET", "/api/records?station=Westlands%20Branch")[0], 403)
        self.assertEqual(staff.req("POST", "/api/records", SAMPLE)[0], 403)
        self.assertEqual(staff.req("GET", "/api/records/%d" % other_id)[0], 404)
        self.assertEqual(staff.req("DELETE", "/api/records/%d" % other_id)[0], 404)
        self.assertEqual(staff.req("PUT", "/api/records/%d" % other_id,
                                   {**SAMPLE, "station": "Kisumu Branch"})[0], 404)

        status, res, _ = staff.req("POST", "/api/records", {**SAMPLE, "station": "Kisumu Branch"})
        self.assertEqual(status, 201)
        self.assertEqual(staff.req("GET", "/api/records")[1]["count"], 1)
        self.assertEqual(staff.req("GET", "/api/stations")[1]["stations"], ["Kisumu Branch"])
        self.assertEqual(staff.req("DELETE", "/api/records/%d" % res["record"]["id"])[0], 200)
        self.assertEqual(staff.req("DELETE", "/api/records")[0], 403)
        self.assertEqual(staff.req("GET", "/api/users")[0], 403)

    def test_unassigned_staff_see_all_branches_but_cannot_bulk_delete(self):
        self.admin.req("POST", "/api/records", SAMPLE)
        roamer = Client(self.base).login("roamer")
        self.assertEqual(roamer.req("GET", "/api/records")[1]["count"], 1)
        self.assertEqual(roamer.req("POST", "/api/records", {**SAMPLE, "station": "Nakuru Branch"})[0], 201)
        self.assertEqual(roamer.req("DELETE", "/api/records")[0], 403)

    def test_admin_user_management(self):
        status, res, _ = self.admin.req("POST", "/api/users", {
            "username": "nakuru", "password": PASSWORD, "role": "staff", "station": "Nakuru Branch"})
        self.assertEqual(status, 201)
        uid = res["user"]["id"]
        self.assertEqual(self.admin.req("POST", "/api/users", {"username": "nakuru", "password": PASSWORD})[0], 409)
        self.assertEqual(self.admin.req("POST", "/api/users", {"username": "x", "password": PASSWORD})[0], 400)
        self.assertEqual(self.admin.req("POST", "/api/users", {"username": "shorty", "password": "short"})[0], 400)
        self.assertEqual(self.admin.req("POST", "/api/users",
                                        {"username": "badbranch", "password": PASSWORD, "station": "Moon"})[0], 400)
        names = [u["username"] for u in self.admin.req("GET", "/api/users")[1]["users"]]
        self.assertIn("nakuru", names)

        self.assertEqual(self.admin.req("POST", "/api/users/%d/password" % uid, {"password": "brand-new-pw"})[0], 200)
        Client(self.base).login("nakuru", "brand-new-pw")
        self.assertEqual(self.admin.req("DELETE", "/api/users/%d" % uid)[0], 200)
        me = self.admin.req("GET", "/api/me")[1]["user"]["id"]
        self.assertEqual(self.admin.req("DELETE", "/api/users/%d" % me)[0], 400)


class BootstrapTest(unittest.TestCase):
    def test_first_start_creates_one_admin(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = server.Store(os.path.join(tmp, "b.sqlite3"))
            auth = server.Auth(store)
            server.bootstrap_admin(auth)
            server.bootstrap_admin(auth)
            users = auth.list()
            self.assertEqual([(u["username"], u["role"]) for u in users], [("admin", "admin")])
            store.conn.close()


if __name__ == "__main__":
    unittest.main()
