# PetroKenya — Station Dashboard

Daily fuel station management for PetroKenya branches: pump meters, payments
(M-Pesa / cash / credit / card), gases & lubricants, tank dips, expenses, credit
invoices, KRA tax figures, analytics and records.

It is an **installable phone app (PWA)** backed by a small **Python server** that
keeps every branch's records in one shared SQLite database, with sign-in and
per-branch permissions.

## Project layout

```
frontend/                 the app people open on their phone or computer
  index.html              page structure
  css/styles.css          look & phone layout
  js/app.js               calculations, forms, sign-in, talking to the server
  manifest.webmanifest    makes it installable ("Add to Home Screen")
  sw.js                   service worker — opens offline, caches app files
  icons/                  app icons
backend/
  server.py               web server + JSON API + SQLite + accounts (Python stdlib only)
  tests/test_server.py    API tests
render.yaml, Procfile     hosting config (Render / Railway / Heroku-style hosts)
```

## Run it on your computer

Needs Python 3.9+. Nothing to install.

```bash
python3 backend/server.py
```

Open **http://localhost:8000**. On the very first start the server creates an
`admin` account and prints its password in the terminal — sign in with it, then
change the password under **👤 → Change Password**.

To try it on your phone over Wi-Fi, run `HOST=0.0.0.0 python3 backend/server.py`
and open `http://<your-computer's-IP>:8000` on the phone. (Installing as an app
and offline mode need HTTPS, so for those, host it online — see below.)

## Put it online (so every branch can use it)

**Render** (simplest): push this repo to GitHub → render.com → *New → Blueprint* →
choose the repo. Render reads `render.yaml`, asks for `PK_ADMIN_PASSWORD`, and gives
you an `https://….onrender.com` address. The 1 GB disk keeps the database between
deploys (this needs Render's paid *Starter* instance; on a free instance the data
would be wiped on every restart).

**Railway** also works: create a service from the repo (it uses the `Procfile`),
add a volume mounted at `/data`, and set `PK_DB=/data/petrokenya.sqlite3`,
`PK_ADMIN_USER`, `PK_ADMIN_PASSWORD` and `PK_SECURE_COOKIE=1`.

Back up the database by downloading the `.sqlite3` file, or use **📥 Export CSV**.

## Install on a phone

1. Open your app's `https://` address in the phone's browser and sign in.
2. **Android (Chrome):** tap **📲 Install** in the header, or ⋮ → *Install app*.
   **iPhone (Safari):** Share → *Add to Home Screen*.
3. It opens full-screen from its own icon. Without internet it still opens and
   shows the records from the last sync (read-only); saving needs a connection.

## Accounts & permissions

| Role | Can do |
|------|--------|
| **admin** | Everything: all branches, erase records, add/remove users, reset passwords |
| **staff** with a branch | Only that branch — see, save and delete its records |
| **staff** without a branch | All branches, but no bulk erase and no user management |

Admins manage users in the app under **👤**. From a terminal you can also run:

```bash
python3 backend/server.py create-user mary --role staff --station "Kisumu Branch"
python3 backend/server.py reset-password mary
```

## Settings

| Env var | Default | Purpose |
|---------|---------|---------|
| `PORT` | `8000` | Port to listen on |
| `HOST` | `127.0.0.1` | `0.0.0.0` to accept connections from other devices / when hosted |
| `PK_DB` | `backend/petrokenya.sqlite3` | Database file |
| `PK_ADMIN_USER`, `PK_ADMIN_PASSWORD` | — | Admin created on first start (otherwise `admin` + random password) |
| `PK_SECURE_COOKIE` | auto | `1` forces the Secure cookie flag (auto when behind an HTTPS proxy) |

## API

JSON in and out. Everything except `/api/health` and `/api/login` needs a
signed-in session (cookie). The server recomputes all figures — litres, revenue,
profit, VAT, excise, road levy, dip and pump variances — whenever a record is saved.

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/login`, `/api/logout` | Sign in / out |
| GET | `/api/me` · POST `/api/me/password` | Current user · change own password |
| GET | `/api/stations` | Branches this user may use |
| POST | `/api/compute` | Compute figures without saving |
| GET | `/api/records?station=&date=&from=&to=&limit=` | List records |
| POST | `/api/records` | Save (updates the existing date + branch record) |
| GET · PUT · DELETE | `/api/records/{id}` | One record |
| DELETE | `/api/records?station=` | Erase records (admin) |
| GET | `/api/analytics?station=` | Last 7 days + all-time totals |
| GET | `/api/export.csv?station=` | CSV download |
| GET · POST | `/api/users` | List / add users (admin) |
| DELETE | `/api/users/{id}` · POST `/api/users/{id}/password` | Remove user · reset password (admin) |

## Tests

```bash
python3 -m unittest discover -s backend/tests
```
