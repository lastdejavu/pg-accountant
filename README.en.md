# 🧾 pg-accountant

### Telegram bot that audits admin-provisioned traffic for the [PasarGuard](https://github.com/PasarGuard/panel) panel

Every night it tells you **exactly how much traffic each admin provisioned** —
with the username of every single account. You no longer have to trust admins to
report their own numbers.

[![tests](https://github.com/lastdejavu/pg-accountant/actions/workflows/test.yml/badge.svg)](https://github.com/lastdejavu/pg-accountant/actions/workflows/test.yml)
[![Python](https://img.shields.io/badge/python-3.11_%7C_3.12_%7C_3.13-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Read-only on panel DB](https://img.shields.io/badge/panel%20database-read--only-success)](#how-it-works)

[فارسی 🇮🇷](README.md) · **English**

---

## Why this exists

PasarGuard reports *consumed* traffic per admin. That is what customers
**used**, not what the admin **sold**. The gap between the two is exactly where
you lose money:

| What the admin does | Panel reports | This bot bills |
|---|---:|---:|
| Creates a 100 GB account, customer never uses it | `0` | **100 GB** |
| Creates a 30 GB account, deletes it hours later | `0` (vanishes) | **30 GB** |
| 10 GB account, uses 9.9 GB, then deletes it | `9.9 GB` | **10 GB** |
| 40 GB account, 39 GB used, then **reset** | `0` (after reset) | **80 GB** (40 + 40) |
| Raises an existing account from 100 → 160 GB | no change | **+60 GB** |
| Bulk-resets all their users | no log is left | **detected** ✅ |
| Creates 20 accounts, tells you it was 17 | — | **all 20 usernames** |

> **Goal:** no sale ever disappears — not even if the admin deletes the account
> before the day ends.

---

## What the midnight report looks like

The bot writes its messages in Persian. This is verbatim output from a real run:

```
📊 گزارش حجم ادمین‌ها
📅 2026-09-22
──────────────
👥 ادمین‌های فعال: ۲   |   📦 مجموع: 1.78 TB
🆕 اکانت جدید: ۲۵

👤 ali
   📦 1.58 TB   |   🆕 ۲۳ اکانت   |   🔁 ۱ ریست
   🗑 حذف‌شده: ۲ اکانت
   ├ ali_100g_00 — 100 GB
   ├ ali_100g_01 — 100 GB
   ├ ali_100g_02 — 100 GB
   ├ ali_100g_03 — 100 GB
   ├ ali_100g_04 — 100 GB
   ├ ali_100g_05 — 100 GB
   └ … و ۱۸ مورد دیگر (1020 GB)

👤 reza
   📦 205 GB   |   🆕 ۲ اکانت   |   🔁 ۰ ریست
   ├ reza_big — 200 GB
   ├ reza_small — 5 GB

──────────────
🆕 ساخت اکانت: 1.74 TB
🔁 ریست/تمدید: 40 GB
➕ افزودن حجم: 0 B
💰 جمع بیل‌شده: 1.78 TB
🗑 اکانت‌های حذف‌شده: ۲
```

**Reading it:** `گزارش حجم ادمین‌ها` = admin traffic report · `ادمین‌های فعال` =
active admins · `مجموع` = total · `اکانت جدید` = new accounts · `ریست` = reset ·
`حذف‌شده` = deleted · `ساخت اکانت` = provisioned · `جمع بیل‌شده` = billed total.

The list is truncated at `max_users_in_report` (60 by default; 6 in the sample
above) and the remainder is summarised. Set `persian_digits = false` for Latin
digits.

---

## Install (5 minutes)

### 1) Get a bot token

Message [@BotFather](https://t.me/BotFather) → `/newbot` → pick a name and
username (must end in `bot`). Copy the token.

### 2) Get your chat ID

- **Yourself:** message [@userinfobot](https://t.me/userinfobot).
- **A group:** make the bot an admin; group IDs start with `-100`.

### 3) Install

```bash
cd /opt
git clone https://github.com/lastdejavu/pg-accountant.git
cd pg-accountant
sudo bash install.sh --token "TOKEN_HERE" --chats "123456789,-1001234567890"
```

Run it without arguments for an interactive prompt. The script:

- detects the database type from `/opt/pasarguard/.env`
- installs the right driver (`psycopg2-binary` / `pymysql` / sqlite) and
  **verifies it actually imports**
- tests the database connection
- creates and enables a `systemd` service

### 4) Validate the schema before you rely on it

```bash
sudo /opt/pg-accountant/venv/bin/python /opt/pg-accountant/bot.py \
  -c /opt/pg-accountant/config.ini checkdb
```

Healthy output (the tool itself writes Persian):

```
آدرس دیتابیس : 127.0.0.1:5432/pasarguard
اتصال        : ✅ برقرار
نوع دیتابیس  : postgresql

-- ستون‌های مورد نیاز --
  admins             ✅ 18 ستون، همه‌ی موارد لازم موجود
  users              ✅ 22 ستون، همه‌ی موارد لازم موجود
  user_usage_logs    ✅ 4 ستون، همه‌ی موارد لازم موجود

-- اجرای کوئری‌های واقعی --
  admins()                ✅ 3 ادمین
  users()                 ✅ 1240 کاربر
      محدود: 1180   نامحدود: 60
  usage_reset_logs_since  ✅ 42 ریست در ۳۰ روز اخیر

✅ دیتابیس پنل با بات سازگار است.
```

`✅` is a pass and `❌` a failure. If a panel upgrade renamed a column, the
`ستون‌های غایب` ("missing columns") line names it exactly, and the command
exits `1`.


### 5) Check it

```bash
sudo systemctl status pg-accountant
sudo tail -f /var/log/pg-accountant.log
```

Then send `/today` to your bot.

---

## Bot commands

| Command | What it does |
|---|---|
| `/today` · `/status` | Today so far, plus last scan time |
| `/report` | Daily report (defaults to yesterday) |
| `/report 2026-09-20` | Report for a specific day |
| `/week` · `/week 14` | Rolling 7- or 14-day totals |
| `/admin ali` | One admin's detail for today |
| `/admins` | List the panel's admins |
| `/user test1` | Full history of one account |
| `/scan` | Force an immediate scan |
| `/help` | Help |

---

## Configuration

`/opt/pg-accountant/config.ini` (written with mode `600`):

| Key | Default | Description |
|---|---|---|
| `bot_token` | — | BotFather token |
| `allowed_chat_ids` | empty | Comma-separated chat IDs allowed to use the bot |
| `panel_db_url` | empty | Database URL; if empty it is read from the panel's `.env` |
| `panel_env_file` | `/opt/pasarguard/.env` | Path to the panel's `.env` |
| `ledger_path` | `/opt/pg-accountant/ledger.db` | The bot's own independent ledger |
| `timezone` | `Asia/Tehran` | Timezone for reports |
| `report_time` | `00:00` | When the daily report is sent |
| `report_previous_day` | `true` | Report yesterday (`true`) or today (`false`) |
| `scan_interval` | `60` | Seconds between scans |
| `reset_drop_tolerance` | `1048576` | Usage drops below this are not treated as a reset |
| `count_topup` | `true` | Bill increases to an existing account's limit |
| `backfill_on_first_run` | `true` | Record accounts that already exist on first run |
| `backfill_days` | `30` | Only backfill accounts created within this many days |
| `report_show_user_list` | `true` | Include usernames in the report |
| `max_users_in_report` | `60` | Beyond this, show a summary instead |
| `report_per_admin` | `true` | Send **one separate message per admin** |
| `max_admin_messages` | `50` | Most admins that get a detail message (0 = all) |
| `message_delay` | `1.2` | Seconds between messages (avoids `429`) |
| `persian_digits` | `true` | Use Persian digits in messages |

### With many admins

The report is sent as **one summary message plus one separate message per
admin**, so no message gets crowded however many admins you have — and you can
forward each admin's own report to them individually:

```
📊 گزارش حجم ادمین‌ها          ← message 1: summary, one line per admin
📅 2026-09-22
──────────────
👥 ادمین‌های فعال: ۳۰   |   📦 مجموع: 36.62 TB
🆕 اکانت جدید: ۷۵۰

۱. admin_00 — 1.22 TB  (۲۵ اکانت)
۲. admin_01 — 1.22 TB  (۲۵ اکانت)
…
──────────────
💰 جمع بیل‌شده: 36.62 TB
```

```
📊 گزارش حجم ادمین‌ها          ← message 2: the first admin only
📅 2026-09-22
──────────────
۱ از ۳۰
👤 admin_00
   📦 1.22 TB   |   🆕 ۲۵ اکانت   |   🔁 ۰ ریست
   ├ a00_user_00 — 50 GB
   ├ a00_user_01 — 50 GB
   └ … و ۲۳ مورد دیگر (1.13 TB)
```

If you have more admins than `max_admin_messages`, only the highest-volume ones
get a detail message; the rest stay in the summary, which reminds you that
`/admin NAME` fetches any one of them.

Set `report_per_admin = false` to go back to packing admins into fewer, larger
messages.

### ⚠️ About the first run

On first run the bot **also records accounts that already exist in the panel**,
so your totals are complete from day one. If your panel is full of old accounts
and you would rather start clean, set this **before** the first run:

```ini
backfill_on_first_run = false
```

---

## Command line

```bash
BIN=/opt/pg-accountant/venv/bin/python
BOT=/opt/pg-accountant/bot.py
CFG=/opt/pg-accountant/config.ini

sudo $BIN $BOT -c $CFG health      # connection test
sudo $BIN $BOT -c $CFG checkdb     # full schema validation
sudo $BIN $BOT -c $CFG scan        # one immediate scan
sudo $BIN $BOT -c $CFG status      # current status
sudo $BIN $BOT -c $CFG report      # yesterday's report, printed
sudo $BIN $BOT -c $CFG report 2026-09-20 --send   # same, sent to Telegram
sudo $BIN $BOT -c $CFG daily       # yesterday's report + send (cron-friendly)
sudo $BIN $BOT -c $CFG run         # the long-running daemon
```

Prefer `cron` for the daily push?

```cron
5 0 * * * /opt/pg-accountant/venv/bin/python /opt/pg-accountant/bot.py -c /opt/pg-accountant/config.ini daily >> /var/log/pg-accountant.log 2>&1
```

---

## How it works

Every `scan_interval` seconds the bot reads the panel's `users` table and
diffs it against its own ledger (`ledger.db`):

| Observed change | Event | What gets billed |
|---|---|---|
| New user appeared | `provision` | current `data_limit` |
| `used_traffic` dropped | `reset` | current `data_limit` (a re-sale) |
| `data_limit` increased | `topup` | the difference |
| limit **and** usage changed together | `renewal` | new `data_limit` only — **once** |
| `data_limit` decreased | `limit_cut` | nothing (warning only) |
| User disappeared | `deleted` | nothing (already billed at `provision`) |

`data_limit` in the panel is **bytes**. The bot bills bytes and renders
GiB/TiB with a 1024 base — exactly like the panel does.

### Why bulk resets are caught

PasarGuard's `bulk.py` performs a bulk reset with a plain `UPDATE`, writes
**no row to `user_usage_logs`**, and deletes the existing rows:

```python
await db.execute(update(User).where(User.id.in_(user_ids)).values(used_traffic=0, ...))
await db.execute(delete(UserUsageResetLogs).where(UserUsageResetLogs.user_id.in_(user_ids)))
```

So that table cannot be relied on. The bot detects resets from the **drop in
`used_traffic` between two scans**, which works regardless of how the reset was
issued.

### Why `renewal` exists

When the panel applies a `next_plan`, it changes `data_limit` *and* zeroes
`used_traffic`. Counted separately, renewing a 40 GB plan into a 100 GB plan
would bill:

```
limit change:  100 − 40 = +60
reset:                   +100
                         ─────
total:                   160   ❌ (correct: 100)
```

The bot records this as a single `renewal` event and bills `100`.

---

## Compared with the trigger approach

The other way to solve this is a `TRIGGER` on the `users` table, as in
[`pasarguard-admin-report`](https://github.com/lastdejavu/pasarguard-admin-report).
Both are legitimate; they trade off differently:

| | Trigger on the panel | This project (polling) |
|---|---|---|
| Supported databases | MySQL/MariaDB only | PostgreSQL · TimescaleDB · MySQL · MariaDB · SQLite |
| Touches the panel DB | adds a table + triggers | **nothing** — `SELECT` only |
| Miss window | zero | `scan_interval` (60 s default) |
| Survives panel upgrades | a migration can drop it | independent of the panel's internal schema |
| Interactive reports | no | `/report` `/week` `/admin` `/user` |
| Automated tests | no | 122 checks + CI |

**If your panel runs MySQL/MariaDB**, triggers have no miss window and are the
better choice. **If you run PostgreSQL or TimescaleDB** — which the PasarGuard
docs themselves recommend — `DELIMITER` and `@var :=` are not valid PostgreSQL,
and this project is the option that works.

---

## Limitations (honest ones)

- **The `scan_interval` window:** an admin who creates and deletes an account
  within 60 seconds will not be recorded. Set `scan_interval = 15` to shrink
  the window; the load is negligible (two plain `SELECT`s).
- **Bot downtime:** accounts created during downtime that **still exist** are
  recovered from `users.created_at`. An account created *and deleted* during
  downtime cannot be recovered, because the panel keeps no trace of it. If the
  bot was down for more than 5 minutes, a warning is recorded.
- **Unlimited accounts:** `data_limit = NULL` cannot be turned into gigabytes;
  the bot counts them but bills zero, and renders `∞`.
- **Not tested against a live PostgreSQL server:** the automated tests run on
  SQLite using the real panel schema, and the queries are compiled against the
  PostgreSQL dialect, but no live PostgreSQL server was available in the
  development environment. Run `checkdb` before you start — it closes exactly
  this gap.

---

## Security

- The bot issues **only** `SELECT` against the panel database. No
  `INSERT`/`UPDATE`/`DELETE` ever touches it.
- The ledger lives in a separate file (`ledger.db`); deleting it cannot harm
  the panel.
- `config.ini` is written with mode `600` (root only).
- The database password is not stored in `config.ini`; it is read from the
  panel's `.env` on each start.
- If `allowed_chat_ids` is empty, **anyone** who finds your bot can read your
  admins' statistics. Fill it in.

---

## Development

```bash
git clone https://github.com/lastdejavu/pg-accountant.git
cd pg-accountant
python3 -m pip install -r requirements.txt pytest

python3 tests/test_accounting.py            # 94 checks (script mode)
python3 -m pytest tests/test_accounting.py  # the same, under pytest
bash tests/e2e.sh                           # 28 end-to-end checks via the real CLI
bash -n install.sh                          # installer syntax
```

The tests build a database using the real panel schema
(`tests/schema_panel.sql`) and exercise the accounting engine against real
scenarios: 20 accounts, deletion hours later, deletion before the quota is
used, a reset after 39 GB consumed, a bulk reset that leaves no log, and
limit increases and decreases.

Every push to `main` is tested on Python 3.11, 3.12 and 3.13 →
[CI results](https://github.com/lastdejavu/pg-accountant/actions/workflows/test.yml)

---

## Layout

```
pg-accountant/
├── bot.py                 the whole bot (single file, minimal dependencies)
├── config.example.ini     template with every key documented
├── install.sh             installer + systemd service
├── requirements.txt       just requests and SQLAlchemy
├── .github/workflows/
│   └── test.yml           CI across three Python versions
└── tests/
    ├── schema_panel.sql   panel schema for tests
    ├── test_accounting.py 94 unit checks
    └── e2e.sh             28 end-to-end checks
```

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `ModuleNotFoundError: psycopg2` | `sudo /opt/pg-accountant/venv/bin/pip install psycopg2-binary`<br>If that fails: `apt install -y libpq-dev gcc python3-dev` |
| `connection refused` | Is the DB in Docker? Make sure the port is bound to `127.0.0.1` |
| No report arrives | Check `allowed_chat_ids`; is the bot an admin in the group? |
| `Permission denied` on the DB | For SQLite: `chmod a+r /var/lib/pasarguard/db.sqlite3` |
| Totals don't match the panel | Expected — the panel shows **usage**, the bot shows **sales**. See the [table above](#why-this-exists) |
| Bot sends an empty report on restart | Fixed in the current version; make sure you have `git pull`ed |

Logs:

```bash
sudo journalctl -u pg-accountant -f
sudo tail -f /var/log/pg-accountant.log
```

---

## License

[MIT](LICENSE)
