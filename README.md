# Free-Telegram-Store-Bot

I made this Bot Free 100%.

> Message me at [@InDMDev](https://t.me/InDMDev) for your advanced bot customizations.
> For more Bots like this, and to be the first to know when I publish more advanced bots, join my channel: [@InDMDevBots](https://t.me/InDMDevBots)
Telegram bot for selling digital products: · sell software license keys on Telegram · Telegram shop/store bot · crypto payment bot · CryptoBot integration · Telegram Payments card checkout · automated digital delivery · Python e-commerce bot · python-telegram-bot store · SQLAlchemy SQLite Telegram bot · self-hosted digital goods storefront.
> 
# Digital Products Store — Telegram Bot

![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white)
![python-telegram-bot](https://img.shields.io/badge/python--telegram--bot-21.6-26A5E4?logo=telegram&logoColor=white)
![SQLAlchemy](https://img.shields.io/badge/SQLAlchemy-2.0.35-D71F00?logo=sqlalchemy&logoColor=white)
![SQLite](https://img.shields.io/badge/database-SQLite%20%7C%20PostgreSQL-003B57?logo=sqlite&logoColor=white)
![Platform](https://img.shields.io/badge/OS-Windows%20%7C%20Linux%20%7C%20macOS-555)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A Telegram bot for selling digital products (software license keys and downloadable files).
Customers browse a catalog, top up an internal wallet (crypto, card, Binance Pay, Bybit, or bKash/Nagad/Rocket), and spend that balance on products.
License keys are automatically delivered from inventory; file products are delivered via download links.
A full in-Telegram admin panel handles products, categories, stock, orders, disputes, users, broadcasts, and store settings.

Built with **Python 3.11**, **python-telegram-bot 21.6** (async, with the `[job-queue]` extra), and **SQLAlchemy 2.0.35** (SQLite by default, PostgreSQL in production).

---

<img width="434" height="501" alt="image" src="https://github.com/user-attachments/assets/45c50008-6b86-4d0c-b0a9-d329b492862b" />

## Table of Contents

1. [Features](#features)
2. [Tech Stack](#tech-stack)
3. [Prerequisites](#prerequisites)
4. [Step 1 — Get your Telegram credentials](#step-1--get-your-telegram-credentials)
5. [Step 2 — Clone the repository](#step-2--clone-the-repository)
6. [Step 3 — Create a virtual environment](#step-3--create-a-virtual-environment)
7. [Step 4 — Install dependencies](#step-4--install-dependencies)
8. [Step 5 — Configure environment variables](#step-5--configure-environment-variables)
9. [Step 6 — Run the bot](#step-6--run-the-bot)
10. [Step 7 — Use the bot (`/start` and `/admin`)](#step-7--use-the-bot-start-and-admin)
11. [Optional — Real-time CryptoBot webhooks](#optional--real-time-cryptobot-webhooks)
12. [Optional — Keep the bot running 24/7](#optional--keep-the-bot-running-247)
13. [Deploying to Production (Railway + Supabase Postgres)](#deploying-to-production-railway--supabase-postgres)
14. [Database backups](#database-backups)
15. [Database notes](#database-notes)
16. [FAQ](#faq)
17. [Troubleshooting](#troubleshooting)
18. [License](#license)

---

## Features

- 🛒 Product catalog with categories and subcategories
- 🔑 Two product types: **license keys** (auto-delivered from inventory) and **downloadable files** (delivered as links)
- 💰 Internal wallet — users top up, then spend the balance on purchases
- 💳 Five top-up methods, each optional and independently enabled by config (a method's button only appears when its variables are set):
  - **CryptoBot** — pay with any cryptocurrency via [@CryptoBot](https://t.me/CryptoBot) (`CRYPTO_BOT_API_KEY`)
  - **Card** — native in-Telegram card payments via Telegram Payments (`TELEGRAM_PROVIDER_TOKEN`)
  - **Binance Pay** — buyer sends to your Binance Pay ID (or the optional on-chain fallback address); confirmed by reading your own Binance account (`BINANCE_API_KEY`, `BINANCE_API_SECRET`, `BINANCE_PAY_ID`)
  - **Bybit** — buyer sends an internal (off-chain) transfer to your Bybit UID/email, or the optional on-chain fallback address; confirmed by reading your own Bybit account (`BYBIT_API_KEY`, `BYBIT_API_SECRET`, `BYBIT_UID`)
  - **bKash / Nagad / Rocket via ZiniPay** — buyer sends to your personal number and submits the transaction ID, verified through ZiniPay's Transaction Verification API (`ZINIPAY_API_KEY` plus at least one of `ZINIPAY_BKASH_NUMBER`, `ZINIPAY_NAGAD_NUMBER`, `ZINIPAY_ROCKET_NUMBER`)

  Wallet credits/debits made by an admin are also recorded in the transactions table as an `admin_adjustment` (not a real payment rail).
- 📝 Store Reviews — buyers can rate completed orders from 1–5 stars; published reviews are shown with a masked username and date
- 🛠 Full in-Telegram **admin panel**: products, categories, stock/restock, orders, disputes, users (ban/unban), broadcasts, and store settings
- ⏱ Background jobs (python-telegram-bot **JobQueue**): pending-payment verification every 30s, expired-payment cleanup every 60s, availability broadcast every 12h, and a database backup every `BACKUP_INTERVAL_HOURS` (default 24h)

### How customers leave a review

1. Open **📝 Store Reviews** from the main menu.
2. Tap **⭐ Write a Review**.
3. Select a completed order.
4. Choose a rating from **1 to 5 stars**.

Each completed order can be reviewed only once.

## Tech Stack

| Component | Version / requirement |
|-----------|-----------------------|
| Python | **3.11** — pinned by `runtime.txt` (`python-3.11`) and by `nixpacks.toml` (`nixPkgs = ["python311", ...]`) |
| python-telegram-bot | **21.6**, installed as `python-telegram-bot[job-queue]` |
| JobQueue deps | `APScheduler==3.10.4` and `pytz==2024.2`, pinned explicitly as top-level requirements so the JobQueue works even when pip short-circuits the extra from a build cache |
| SQLAlchemy | **2.0.35** (no Alembic — schema creation and lightweight idempotent migrations live in `database/db.py`) |
| Database | **SQLite** by default (`sqlite:///bot_database.db`), or **PostgreSQL** via `DATABASE_URL`; the `psycopg2-binary==2.9.9` driver is included |
| HTTP / webhooks | `Flask==3.0.3` + `Flask-Session==0.8.0` for `webhook_server.py`, served by `gunicorn==23.0.0` in production |
| Other pinned deps | `requests==2.32.3`, `python-dotenv==1.0.1`, `urllib3==2.2.3`, `watchdog==6.0.0`, `tzdata==2025.2` |

Exact pins live in `requirements.txt` — install it as-is rather than upgrading packages ad hoc.

### JobQueue dependency (important)

The bot schedules all of its background work through `application.job_queue`, which only exists when python-telegram-bot is installed **with its `job-queue` extra**. Installing plain `python-telegram-bot` produces `No `JobQueue` set up` and then `AttributeError: 'NoneType' object has no attribute 'run_repeating'`. `bot.py` checks `application.job_queue is None` at startup and exits with an explicit message instead of crashing later.

Because of that, `requirements.txt` both requests the extra (`python-telegram-bot[job-queue]==21.6`) and pins `APScheduler` and `pytz` directly, and `nixpacks.toml` installs with `--no-cache-dir --force-reinstall` and then verifies the import:

```bash
python -c "import apscheduler, pytz; from telegram.ext import JobQueue; print('JobQueue dependencies OK')"
```

---

**How it fits together:** `bot.py` is the single wiring point — it validates config (`config/`), initializes the database (`database/`), then registers all the `handlers/`. Handlers talk to Telegram and call into `services/` (external APIs) and `utils/` (keyboards + helpers); all data access goes through `get_db_session()` in `database/db.py`.

---

## Prerequisites

Install these before you start:

- **Git** — [git-scm.com/downloads](https://git-scm.com/downloads)
- **Python 3.11** — [python.org/downloads](https://www.python.org/downloads/) (the version this project pins in `runtime.txt` and builds against on Railway)
  - On **Windows**, tick **“Add Python to PATH”** in the installer.
- A **Telegram account**

Verify your tools are installed:

**Windows (PowerShell):**
```powershell
git --version
python --version
```

**Linux / macOS:**
```bash
git --version
python3 --version
```

---

## Step 1 — Get your Telegram credentials

You need a **bot token** and your **admin Telegram ID**. The two payment keys are optional.

### 1a. Bot token (required)
1. Open [@BotFather](https://t.me/BotFather) in Telegram.
2. Send `/newbot` and follow the prompts (choose a name and a username ending in `bot`).
3. Copy the **API token** it gives you (looks like `1234567890:ABCdef...`).

### 1b. Your admin Telegram ID (required)
1. Open [@userinfobot](https://t.me/userinfobot) in Telegram.
2. Send any message; it replies with your numeric **Id** (e.g. `123456789`).
3. This ID is the only account that can access `/admin`.

### 1c. CryptoBot API key (optional — enables crypto top-ups)
1. Open [@CryptoBot](https://t.me/CryptoBot) → **Crypto Pay** → **My Apps** → create an app.
2. Copy the **API token**. Leave blank to disable the CryptoBot option.

### 1d. Telegram Payments provider token (optional — enables card top-ups)
1. Open [@BotFather](https://t.me/BotFather) → select your bot → **Payments**.
2. Connect a payment provider and copy the **provider token**. Leave blank to disable the Card option.
   > Card-provider availability is region-dependent — pick a provider supported in your country. Use the provider’s **TEST** token while developing.

---

## Step 2 — Clone the repository

**Windows (PowerShell) and Linux / macOS** (same commands):
```bash
git clone <YOUR_REPOSITORY_URL>
cd FreeTelegramStoreBot
```
> Replace `<YOUR_REPOSITORY_URL>` with your repo’s clone URL, and `FreeTelegramStoreBot` with the folder name if it differs.

---

## Step 3 — Create a virtual environment

A virtual environment keeps this project’s dependencies isolated.

**Windows (PowerShell):**
```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```
> If activation is blocked by execution policy, run once:
> `Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned.`
> (or use the CMD activator: `venv\Scripts\activate.bat`).

**Linux / macOS:**
```bash
python3 -m venv venv
source venv/bin/activate
```

When active, your shell prompt is prefixed with `(venv)`. To leave it later, run `deactivate`.

---

## Step 4 — Install dependencies

With the virtual environment active:

**Windows (PowerShell):**
```powershell
python -m pip install --upgrade pip
pip install -r requirements.txt
```

**Linux / macOS:**
```bash
python3 -m pip install --upgrade pip
pip install -r requirements.txt
```

---

## Step 5 — Configure environment variables

Copy the example file to a real `.env` and fill in your values.

**Windows (PowerShell):**
```powershell
Copy-Item .env.example .env
notepad .env
```

**Linux / macOS:**
```bash
cp .env.example .env
nano .env
```

Fill in the variables:

Every variable below is actually read by the code (`config/settings.py`, plus `webhook_server.py` for `LOG_LEVEL`/`PORT`). Nothing else is used.

**Core**

| Variable | Required | Default | Description |
|----------|:--------:|---------|-------------|
| `BOT_TOKEN` | ✅ | — | Bot token from [@BotFather](https://t.me/BotFather) (Step 1a). |
| `ADMIN_TELEGRAM_ID` | ✅ | `0` | Your numeric Telegram ID (Step 1b). The only admin account. |
| `ADMIN_TELEGRAM_USERNAME` | ➖ | empty | Your username without `@` (used in some messages). |
| `DATABASE_URL` | ➖ | `sqlite:///bot_database.db` | SQLAlchemy URL. A legacy `postgres://` prefix is auto-rewritten to `postgresql://`. |
| `TIMEZONE` | ➖ | `UTC` | IANA timezone used to bucket stored UTC timestamps into local days/months for the admin Stats dashboard. |
| `STORAGE_DIR` | ➖ | `.` (project dir) | Root for runtime-writable files — see [Runtime file storage](#e-persistent-storage-for-uploaded-images-storage_dir). Must point at persistent storage in production. |
| `LOG_LEVEL` | ➖ | `INFO` | Log level for `webhook_server.py`. |
| `PORT` | ➖ | `5000` | Port for `webhook_server.py`'s local dev server. On Railway the platform injects it — do not set it yourself. |
| `BACKUP_ENABLED` | ➖ | `true` | Set to `false` to disable the in-bot database backup job. |
| `BACKUP_INTERVAL_HOURS` | ➖ | `24` | Hours between automatic backups. |

**Payments** — all optional; each method's button appears only when its variables are set.

| Variable | Default | Description |
|----------|---------|-------------|
| `CRYPTO_BOT_API_KEY` | empty | CryptoBot Crypto Pay token (Step 1c). Blank disables crypto top-up. |
| `TELEGRAM_PROVIDER_TOKEN` | empty | Telegram Payments provider token (Step 1d). Blank disables card top-up. |
| `PAYMENT_CURRENCY` | `USD` | Currency for card invoices. Must be USD-denominated to match wallet amounts. |
| `BINANCE_API_KEY` / `BINANCE_API_SECRET` | empty | Read-only API key from a regular (non-merchant) Binance account, used to confirm incoming payments on your own account. |
| `BINANCE_PAY_ID` | empty | Your Binance ID / Pay ID, shown to buyers as the "send to" target. |
| `BINANCE_DEPOSIT_ADDRESS` | empty | Optional on-chain deposit address fallback. |
| `BINANCE_DEPOSIT_COIN` | `USDT` | Coin for the on-chain fallback. |
| `BYBIT_API_KEY` / `BYBIT_API_SECRET` | empty | Read-only (Wallet: read) API key from a regular Bybit account. |
| `BYBIT_UID` | empty | Your Bybit UID or email, shown as the "send to" target for a free internal transfer. |
| `BYBIT_DEPOSIT_ADDRESS` | empty | Optional on-chain deposit address fallback. |
| `BYBIT_DEPOSIT_COIN` / `BYBIT_DEPOSIT_CHAIN` | `USDT` / `TRX` | Coin and chain for the on-chain fallback. |
| `ZINIPAY_API_KEY` | empty | ZiniPay brand/API key. Requires the "Transaction Verification API" toggle enabled in the ZiniPay dashboard. |
| `ZINIPAY_BKASH_NUMBER`, `ZINIPAY_NAGAD_NUMBER`, `ZINIPAY_ROCKET_NUMBER` | empty | Your personal numbers buyers send money to. Set at least one; only the ones you set are offered. |
| `PUBLIC_BASE_URL` | empty | Public HTTPS base URL of `webhook_server.py`. Only needed for ZiniPay's hosted-checkout flow (`services/zinipay.py` `create_invoice`). |
| `ZINIPAY_REDIRECT_URL` / `ZINIPAY_CANCEL_URL` | `PUBLIC_BASE_URL` + `/zinipay/success` / `/zinipay/cancel` | Override only when `PUBLIC_BASE_URL`'s domain isn't the domain registered against your ZiniPay brand. |

> The bot **will not start** until at least `BOT_TOKEN` and `ADMIN_TELEGRAM_ID` are set — it validates these on startup and exits with a clear message if either is missing.

---

## Step 6 — Run the bot

The database is created and seeded automatically on first run — there is no separate setup command.

**Windows (PowerShell):**
```powershell
python bot.py
```

**Linux / macOS:**
```bash
python3 bot.py
```

You should see log lines ending with:
```
Bot started successfully!
```

**Start commands used by this project** (they are the source of truth — `Procfile` and `nixpacks.toml`):

| Process | Command | Purpose |
|---------|---------|---------|
| `bot` | `python bot.py` | The Telegram bot itself (long-polling). This is also the `nixpacks.toml` start command. |
| `web` (optional) | `gunicorn webhook_server:app --bind 0.0.0.0:$PORT` | Flask webhook server for instant CryptoBot / ZiniPay confirmations. Skip it and the bot's own 30-second polling job still confirms payments. |

The `Procfile`'s `bot` line reinstalls `requirements.txt` with `--no-cache-dir` before starting, so the JobQueue extra can never be missing at runtime.
Leave this terminal open — the bot runs as long as the process is running. Press **Ctrl+C** to stop it.

---

## Step 7 — Use the bot (`/start` and `/admin`)

With the bot running:

1. Open Telegram and search for your bot by the username you chose in Step 1a.
2. Send **`/start`** — you’ll get the welcome message and the main menu (Products, Top Up, Order History, Availability, Support).
3. Send **`/admin`** — if your Telegram ID matches `ADMIN_TELEGRAM_ID`, the **admin panel** opens (Product Management, User Management, Order Management, Store Settings, Broadcast).

> If `/admin` says access is denied or does nothing, your `ADMIN_TELEGRAM_ID` doesn’t match your account — recheck Step 1b, fix `.env`, and restart the bot.

**🎉 That’s it — your bot is live.** A typical first run as admin: open `/admin` → **Product Management** → create a category, then a product, then **Restock Keys** to add inventory. As a user, `/start` → **Top Up** to fund the wallet, then buy a product.

---

## Optional — Real-time CryptoBot webhooks

By default, CryptoBot payments are confirmed by polling every ~30 seconds (no extra setup). For **instant** confirmation, run the included webhook server alongside the bot.

1. Start the webhook server (separate terminal, same virtual environment):

   **Windows (PowerShell):**
   ```powershell
   python webhook_server.py
   ```
   **Linux / macOS:**
   ```bash
   python3 webhook_server.py
   ```
   It listens on port **5000**.

2. Expose it over HTTPS (e.g. with [ngrok](https://ngrok.com/)):
   ```bash
   ngrok http 5000
   ```

3. In [@CryptoBot](https://t.me/CryptoBot) → **Crypto Pay → My Apps → Webhooks**, set the URL to:
   ```
   https://<your-ngrok-or-domain>/webhook/cryptobot
   ```

> Run the bot and the webhook server as two separate processes (two terminals locally, or the `bot` and `web` process types from the `Procfile` in production). In production serve the webhook with gunicorn — `gunicorn webhook_server:app --bind 0.0.0.0:$PORT` — rather than Flask's dev server.
> Card payments need no webhook — Telegram delivers their confirmation through the bot’s normal update polling.

---

## Optional — Keep the bot running 24/7

### Linux (systemd)

Create `/etc/systemd/system/digitalstore-bot.service` (adjust paths and `User`):

```ini
[Unit]
Description: Digital Products Store Telegram Bot
After=network.target

[Service]
Type=simple
User=youruser
WorkingDirectory=/home/youruser/FreeTelegramStoreBot
ExecStart=/home/youruser/FreeTelegramStoreBot/venv/bin/python bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Then enable and start it:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now digitalstore-bot
sudo systemctl status digitalstore-bot      # check it's running
journalctl -u digitalstore-bot -f            # follow logs
```

### Windows
Keep the `python bot.py` window open, or run it as a background/scheduled task (e.g. Task Scheduler), or host it on a Linux server using the steps above.

---

## Deploying to Production (Railway + Supabase Postgres)

This is the recommended way to run the bot 24/7 without managing a server yourself.
Quick version — the detailed steps follow below:

1. **Create a Supabase Postgres project** at [supabase.com](https://supabase.com) (free tier is fine). Pick a strong database password and save it.
2. **Copy the Session Pooler connection string** from **Project Settings → Database → Connection string → Session pooler** (port `5432`, *not* the Transaction pooler on `6543`). Replace `[YOUR-PASSWORD]` in the string with the password from step 1, and prefer the `postgresql://` scheme:
   ```
   postgresql://postgres.<project-ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres
   ```
3. **Set `DATABASE_URL` on Railway** to that string: Railway → your service → **Variables → New Variable** → name `DATABASE_URL`, value the connection string. Set it on every service that talks to the DB (`bot`, and `web` if you deploy it).
4. **Set the rest of the config as Railway environment variables, not a committed `.env` file.** The `.env` file is for local development only and is git-ignored — Railway never reads it. Confirm each of these is present under **Variables** before the first deploy:
   - `BOT_TOKEN` (required)
   - `ADMIN_TELEGRAM_ID` (required)
   - `ADMIN_TELEGRAM_USERNAME`
   - `DATABASE_URL` (from step 3)
   - `STORAGE_DIR=/data` (with a Railway volume mounted at `/data`, see section E)
   - Optional backup tuning: `BACKUP_ENABLED` (default `true`), `BACKUP_INTERVAL_HOURS` (default `24`) — see [Database backups](#database-backups)
   - Optional, per payment method you enable: `CRYPTO_BOT_API_KEY`, `TELEGRAM_PROVIDER_TOKEN`, `PAYMENT_CURRENCY`, `BINANCE_API_KEY`, `BINANCE_API_SECRET`, `BINANCE_PAY_ID`, `BYBIT_API_KEY`, `BYBIT_API_SECRET`, `BYBIT_UID`, `ZINIPAY_API_KEY`
5. **Deploy.** On first boot `initialize_database()` creates every table and runs the lightweight, idempotent schema migrations in `database/db.py` (new `transactions.external_reference` / `admin_note` columns, `users.language_code`, and the `ALTER TYPE ... ADD VALUE IF NOT EXISTS` enum updates). This path is verified against a real Postgres server on both a fresh database and an older one, and it is safe to re-run on every restart.

> No manual migration command is needed, and no ORM migration tool (Alembic etc.) is used — the migrations are intentionally lightweight and idempotent.

### A. Create the Supabase database

1. Create a project at [supabase.com](https://supabase.com).
2. Go to **Project Settings → Database → Connection string**.
3. Copy the **Session pooler** connection string (or the direct connection string) — port `5432`. **Do not use the "Transaction pooler" (port `6543`)**: this bot is a long-running worker that holds a persistent connection pool, which the transaction pooler isn't designed for.
4. Make sure the string starts with `postgresql://` — Supabase sometimes shows `postgres://`, which this bot's `config/settings.py` now auto-corrects, but using `postgresql://` directly is cleaner.

### B. Push the code to GitHub

Railway deploys from a GitHub repo. Push this project to a new repo (the included `.gitignore` keeps your real `.env` out of it).

### C. Create the Railway project

1. On [railway.app](https://railway.app), **New Project → Deploy from GitHub repo** → select your repo.
2. Railway will detect the `Procfile` and offer two process types: `bot` and `web`. Create **two services** from the same repo, one per process type (in the service settings, set **Custom Start Command** if Railway doesn't pick it up automatically):
   - **`bot` service** — Start command: `python bot.py`. This is the actual Telegram bot (long-polling). It does not need a public domain.
   - **`web` service** *(optional, only if you want instant CryptoBot payment confirmations instead of the 30-second poll)* — Start command: `gunicorn webhook_server:app --bind 0.0.0.0:$PORT`. Under **Settings → Networking**, click **Generate Domain** so it gets a public HTTPS URL.

   If you don't need instant crypto confirmations, skip the `web` service entirely — the `bot` service's built-in polling job (`check_pending_payments`, every 30s) still verifies and credits crypto payments on its own.

### D. Set environment variables

On **both** services (Railway → Service → Variables), set:

```
BOT_TOKEN=your_bot_token
ADMIN_TELEGRAM_ID=your_telegram_id
ADMIN_TELEGRAM_USERNAME=your_username
DATABASE_URL=postgresql://...          # the Supabase connection string from step A
CRYPTO_BOT_API_KEY=your_cryptobot_key  # optional
TELEGRAM_PROVIDER_TOKEN=your_provider_token  # optional
PAYMENT_CURRENCY=USD
```

Do **not** set `PORT` yourself — Railway injects it automatically for the `web` service.

### E. Persistent storage for uploaded images (`STORAGE_DIR`)

Railway's filesystem resets on every redeploy. Product photos, the store logo, and broadcast images are saved to disk (`assets/`, `uploads/`), so **without a volume they'll vanish on your next deploy**.

1. On the `bot` service → **Settings → Volumes → New Volume**. Mount it at `/data`.
2. Set the environment variable `STORAGE_DIR=/data` on the `bot` service.
3. If you also run the `web` service and it ever needs to read those images, attach the same volume there too (otherwise it isn't needed on `web`).

**How `STORAGE_DIR` works**

- It is configured entirely through the environment (`STORAGE_DIR=/data`); nothing is hardcoded, and no credentials are involved. Files stay on local/volume disk — the bot does not upload to S3, Cloudinary or any external provider.
- The value is expanded and made absolute at startup, so it works regardless of the process's working directory.
- Everything derives from it: `STORAGE_DIR/assets/logos`, `STORAGE_DIR/assets/products`, `STORAGE_DIR/uploads`, `STORAGE_DIR/backups`.
- On startup the bot creates any missing directory (`exist_ok=True`, existing files are never touched). If they can't be created — read-only mount, wrong permissions — it logs a warning and keeps running instead of crashing; the upload handlers retry creating their own directory on demand.
- Left unset it defaults to the project directory and the bot logs an explicit warning that this storage is **ephemeral** on a container host: every redeploy or container restart wipes uploaded product images, logos, broadcast images and local backups. Configure a persistent volume as above for production.

**Runtime storage persistence requirements (summary)**

| What is written | Path | Persistence requirement |
|-----------------|------|-------------------------|
| Store logo | `STORAGE_DIR/assets/logos` | Persistent disk/volume in production |
| Product photos | `STORAGE_DIR/assets/products` | Persistent disk/volume in production |
| Broadcast images, uploaded key files | `STORAGE_DIR/uploads` | Persistent disk/volume in production |
| Local database dumps | `STORAGE_DIR/backups` | Transient — each dump is deleted after it is DM'd to the admin |
| SQLite database (`sqlite:///bot_database.db`) | project directory | Persistent disk/volume, or switch to PostgreSQL, otherwise all data is lost on redeploy |

The bot never uploads to S3/Cloudinary or any external storage provider, so these directories must live on a disk that survives restarts. `STORAGE_DIR` must be writable by the process; if a directory can't be created the bot logs a warning and keeps running, and upload handlers retry creating their own directory on demand.

### F. First deploy

Railway builds and starts the service automatically. On first boot, `bot.py` calls `initialize_database()`, which creates all tables in your Supabase database — no manual migration step needed. Watch the `bot` service's deploy logs for `[OK] Configuration validated successfully` and `Bot started successfully!`.

### G. Configure the CryptoBot webhook (only if you deployed the `web` service)

In Telegram, open **@CryptoBot → Crypto Pay → My Apps → your app → Webhooks**, and set the URL to:
```
https://<your-web-service>.up.railway.app/webhook/cryptobot
```

### Notes

- Both services read/write the same Supabase database, so keep `DATABASE_URL` identical on both.
- Restarting or redeploying the `bot` service is safe — `initialize_database()` only creates missing tables, it never drops data.
- If you ever see connection errors like `SSL connection has been closed unexpectedly` after the bot sits idle, that's Supabase's idle-connection timeout; `database/db.py` already sets `pool_pre_ping` and `pool_recycle` to handle this automatically — you shouldn't need to do anything.

---

## Database backups

The bot takes a **full database dump once every 24 hours and sends it to the admin's Telegram DMs** (`ADMIN_TELEGRAM_ID`), so Telegram itself acts as off-site storage. The job is registered in `bot.py` with `job_queue.run_repeating`, alongside the payment jobs, and first runs 5 minutes after startup.

- **Postgres:** runs `pg_dump --no-owner --no-privileges` against `DATABASE_URL` and gzips the output.
- **SQLite (local dev):** uses SQLite's online backup API, safe to run while the bot is writing.
- The local dump file is deleted right after it's delivered — there's no retention policy and no incremental backups by design.

**Railway needs the Postgres client tools.** The default Nixpacks Python image has no `pg_dump`, so add a `nixpacks.toml` at the repo root:

```toml
[phases.setup]
nixPkgs = ["...", "postgresql"]
```

Without it the job posts a clear "pg_dump is not installed" warning to the admin instead of a backup.

**Manual / cron use:** the same code runs standalone, which is what you'd point a Railway scheduled job at:

```bash
python scripts/backup_db.py                  # writes to STORAGE_DIR/backups
python scripts/backup_db.py --output-dir /tmp
```

**Settings:**

| Variable | Default | Meaning |
| --- | --- | --- |
| `BACKUP_ENABLED` | `true` | Set to `false` to disable the in-bot job (e.g. if you use Railway cron instead) |
| `BACKUP_INTERVAL_HOURS` | `24` | Hours between automatic backups |

**Restoring:** `gunzip -c backup-YYYYmmdd-HHMMSS.sql.gz | psql "$DATABASE_URL"` for Postgres, or `gunzip` the `.sqlite.gz` back into place for SQLite.

> Telegram bots can only upload files up to 50 MB. If the dump grows past that, the job warns the admin instead of failing silently — switch to a Railway scheduled job uploading elsewhere at that point.

---

## Database notes

- **Default:** SQLite, stored in `bot_database.db` in the project folder. Created automatically on first run.
- **Backup:** simply copy the `bot_database.db` file.
- **Reset (deletes all data):** stop the bot, delete `bot_database.db`, and start the bot again to recreate an empty database.

  **Windows (PowerShell):**
  ```powershell
  Remove-Item bot_database.db
  ```
  **Linux / macOS:**
  ```bash
  rm bot_database.db
  ```
- **PostgreSQL (optional):** set `DATABASE_URL` to a Postgres URL, e.g.
  `postgresql+psycopg2://user:password@localhost:5432/digitalstore`
  (The `psycopg2-binary` driver is already in `requirements.txt`).
- **PostgreSQL requirements:** Python 3.11 + `psycopg2-binary==2.9.9` (already pinned). Use a **direct or session-pooled connection on port 5432** — not a transaction pooler (Supabase port `6543`) — because the bot is a long-running worker holding a persistent pool. `database/db.py` sets `pool_pre_ping` and `pool_recycle` to survive idle-connection drops. For the backup job, the host also needs the `pg_dump` client binary (`nixpacks.toml` installs the `postgresql` package for this).
- **Migrations:** `initialize_database()` creates missing tables and applies the lightweight idempotent migrations in `database/db.py` on every start — no migration command is required and no ORM migration tool (Alembic) is used. The standalone scripts in `migrations/` (`schema_sync.py`, `categorynullable.py`, `add_faq_text.py`, `add_low_stock_threshold.py`, `fix_payment_method_enum.py`, or all of them via `python migrations/run_all.py`) exist for upgrading older databases and are not needed for fresh installs.

---

## FAQ

**What is this project?**
An open-source, self-hosted **Telegram bot for selling digital products** — software license/activation keys and downloadable files — with a customer-facing storefront and a full admin panel, all inside Telegram.

**What can I sell with it?**
Anything digital: software license keys, game keys, gift-card codes, e-books, PDFs, courses, templates, or any downloadable file delivered via a link.

**How do customers pay?**
Customers fund an in-bot **wallet**, then spend the balance on purchases. Top-ups are supported via **CryptoBot** (any cryptocurrency), **card payments** (Telegram Payments), **Binance Pay**, **Bybit** transfers, and **bKash / Nagad / Rocket** through ZiniPay. Every method is optional and enabled by config.

**Is delivery automatic?**
Yes. License keys are assigned automatically from your inventory the moment a purchase is confirmed; file products are delivered as a download link — no manual fulfillment.

**Do I need to know how to code to run it?**
No. Clone the repo, fill in a `.env` file, and run one command. The database is created automatically on first launch.

**Which database does it use?**
**SQLite** by default (zero setup). You can switch to **PostgreSQL** by changing a single environment variable.

**Does it work on Windows and Linux?**
Yes — the [setup guide](#table-of-contents) has step-by-step commands for **Windows, Linux, and macOS**, plus a `systemd` service for 24/7 hosting.

**Is it free and open source?**
Yes — released under the [MIT License](LICENSE).

---
## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `Configuration error: BOT_TOKEN is required` | `.env` is missing or `BOT_TOKEN`/`ADMIN_TELEGRAM_ID` is empty. Recheck Step 5 and that `.env` is in the project root. |
| `/admin` denied or no response | `ADMIN_TELEGRAM_ID` doesn’t match your account. Re-get your ID (Step 1b), update `.env`, restart. |
| `ModuleNotFoundError` / import errors | The virtual environment isn’t active or deps aren’t installed. Re-do Step 3 and Step 4. |
| `python` not found (Windows) | Reinstall Python with **“Add Python to PATH”** ticked, or use the `py` launcher (`py bot.py`). |
| Activation blocked (Windows) | `Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned`, then re-activate. |
| Card button shows “not configured” | `TELEGRAM_PROVIDER_TOKEN` is blank or invalid — see Step 1d. |
| Crypto top-up not auto-confirming | Verify `CRYPTO_BOT_API_KEY`, check the console for API errors, or set up webhooks for instant confirmation. |
| Bot stops when you close the terminal | That’s expected — use the [24/7 section](#optional--keep-the-bot-running-247). |


## License

Released under the [MIT License](LICENSE).

> ⚠️ **Note: Use this program only for legal purposes.**
> InDMDev is not and will not be responsible for any illegal activity/activities you indulge in using any of our programs.
