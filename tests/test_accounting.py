#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
تست سنجه‌ی حسابداری pg-accountant روی یک دیتابیس با اسکیمای واقعی پنل PasarGuard.

سناریوی کاربر:
  ۱. ادمین ۲۰ اکانت می‌سازد: ۱۰ تا ۱۰۰ گیگ + ۱۰ تا ۵۰ گیگ  → ۱۵۰۰ گیگ
  ۲. یک اکانت ۳۰ گیگ می‌سازد و چند ساعت بعد حذف می‌کند      →  ۳۰ گیگ (می‌ماند)
  ۳. یک اکانت ۱۰ گیگ، تا ۹٫۹ گیگ مصرف می‌شود بعد حذف        →  ۱۰ گیگ
  ۴. یک اکانت ۴۰ گیگ، ۳۹ گیگ مصرف و سپس ریست می‌شود        →  ۴۰ گیگ دوباره
  جمع مورد انتظار: ۱۵۸۰ گیگ ساخت + ۴۰ گیگ ریست = ۱۶۲۰ گیگ

اجرا:
    python3 tests/test_accounting.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import (  # noqa: E402
    Accountant,
    Config,
    GB,
    Ledger,
    PanelDB,
    build_day_report,
    human_bytes,
    normalize_panel_db_url,
    parse_hhmm,
    seconds_until_next_report,
    _parse_bool,
    _panel_env_value,
    _split_message,
)

PASS, FAIL = 0, 0


def _short(v: Any, limit: int = 90) -> str:
    s = repr(v)
    return s if len(s) <= limit else s[:limit] + f"… ({len(s)} کاراکتر)"


def check(label: str, got, want) -> None:
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  ✅ {label}: {_short(got)}")
    else:
        FAIL += 1
        print(f"  ❌ {label}\n       انتظار: {_short(want)}\n       دریافت: {_short(got)}")


# --------------------------------------------------------------------------- #
# ساخت دیتابیس پنل با اسکیمای واقعی
# --------------------------------------------------------------------------- #

PANEL_SCHEMA = """
CREATE TABLE admins (
    id INTEGER PRIMARY KEY,
    username VARCHAR(34) UNIQUE,
    hashed_password VARCHAR(128),
    used_traffic BIGINT DEFAULT 0,
    data_limit BIGINT,
    status VARCHAR(9) DEFAULT 'active',
    role_id BIGINT DEFAULT 0,
    created_at DATETIME
);

CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    username VARCHAR(128) UNIQUE,
    status VARCHAR(9) DEFAULT 'active',
    used_traffic BIGINT DEFAULT 0,
    data_limit BIGINT,
    data_limit_reset_strategy VARCHAR(7) DEFAULT 'no_reset',
    expire DATETIME,
    admin_id BIGINT REFERENCES admins(id),
    online_at DATETIME,
    edit_at DATETIME,
    created_at DATETIME
);

CREATE TABLE user_usage_logs (
    id INTEGER PRIMARY KEY,
    user_id BIGINT REFERENCES users(id),
    used_traffic_at_reset BIGINT NOT NULL,
    reset_at DATETIME
);

CREATE TABLE node_user_usages (
    id INTEGER PRIMARY KEY,
    created_at DATETIME,
    user_id BIGINT REFERENCES users(id),
    node_id BIGINT REFERENCES nodes(id),
    used_traffic BIGINT DEFAULT 0
);

CREATE TABLE nodes (
    id INTEGER PRIMARY KEY,
    name VARCHAR(256)
);
"""


class FakePanel:
    """جایگزین PanelDB که به یک SQLite در حافظه وصل است."""

    def __init__(self, db_path: str):
        self.path = db_path
        self.engine = None

    def _conn(self):
        import sqlite3

        return sqlite3.connect(self.path)

    def exec(self, sql: str, params: tuple = ()) -> None:
        with self._conn() as c:
            c.execute(sql, params)
            c.commit()

    # --- درج‌ها ---------------------------------------------------------- #
    def add_admin(self, aid: int, username: str) -> None:
        self.exec(
            "INSERT INTO admins(id, username, hashed_password, created_at)"
            " VALUES(?,?,?,?)",
            (aid, username, "x", datetime.now(timezone.utc).isoformat()),
        )

    def add_user(
        self,
        uid: int,
        username: str,
        admin_id: int | None,
        data_limit: int | None,
        used_traffic: int = 0,
        created_at: datetime | None = None,
    ) -> None:
        self.exec(
            "INSERT INTO users(id, username, status, used_traffic, data_limit,"
            " admin_id, created_at) VALUES(?,?,?,?,?,?,?)",
            (
                uid,
                username,
                "active",
                used_traffic,
                data_limit,
                admin_id,
                (created_at or datetime.now(timezone.utc)).isoformat(),
            ),
        )

    def set_used(self, uid: int, used_traffic: int) -> None:
        self.exec("UPDATE users SET used_traffic = ? WHERE id = ?", (used_traffic, uid))

    def set_limit(self, uid: int, data_limit: int | None) -> None:
        self.exec("UPDATE users SET data_limit = ? WHERE id = ?", (data_limit, uid))

    def delete_user(self, uid: int) -> None:
        self.exec("DELETE FROM users WHERE id = ?", (uid,))
        self.exec("DELETE FROM user_usage_logs WHERE user_id = ?", (uid,))

    def log_reset(self, uid: int, used_at_reset: int, when: datetime) -> None:
        self.exec(
            "INSERT INTO user_usage_logs(user_id, used_traffic_at_reset, reset_at)"
            " VALUES(?,?,?)",
            (uid, used_at_reset, when.isoformat()),
        )

    # --- رابط مورد نیاز Accountant -------------------------------------- #
    def admins(self):
        from bot import PanelAdmin

        with self._conn() as c:
            rows = c.execute("SELECT id, username FROM admins").fetchall()
        return {int(r[0]): PanelAdmin(int(r[0]), str(r[1])) for r in rows}

    def users(self):
        from bot import PanelUser, parse_ts

        with self._conn() as c:
            rows = c.execute(
                "SELECT id, username, admin_id, data_limit, used_traffic, created_at FROM users"
            ).fetchall()
        return [
            PanelUser(
                user_id=int(r[0]),
                username=str(r[1]),
                admin_id=(int(r[2]) if r[2] is not None else None),
                data_limit=(int(r[3]) if r[3] is not None else None),
                used_traffic=int(r[4] or 0),
                created_at=parse_ts(r[5]),
            )
            for r in rows
        ]

    def usage_reset_logs_since(self, since):
        from bot import parse_ts

        with self._conn() as c:
            rows = c.execute(
                "SELECT user_id, used_traffic_at_reset, reset_at FROM user_usage_logs"
            ).fetchall()
        return [
            {
                "user_id": int(r[0]),
                "used_traffic_at_reset": int(r[1] or 0),
                "reset_at": parse_ts(r[2]),
            }
            for r in rows
        ]


# --------------------------------------------------------------------------- #
# تست‌ها
# --------------------------------------------------------------------------- #


def make_env(cfg: Config) -> tuple[FakePanel, Ledger, Accountant]:
    panel_dir = tempfile.mkdtemp()
    panel_path = os.path.join(panel_dir, "panel.db")
    import sqlite3

    conn = sqlite3.connect(panel_path)
    conn.executescript(PANEL_SCHEMA)
    conn.commit()
    conn.close()

    panel = FakePanel(panel_path)
    ledger = Ledger(":memory:")
    acc = Accountant(cfg, panel, ledger)  # type: ignore[arg-type]
    return panel, ledger, acc


def test_main_scenario() -> None:
    print("\n=== سناریوی اصلی کاربر (۲۰ اکانت + حذف + ریست) ===")
    cfg = Config(ledger_path=":memory:", scan_interval=60, dry_run_telegram=True)
    panel, ledger, acc = make_env(cfg)

    panel.add_admin(1, "ali_reseller")
    panel.add_admin(2, "reza_reseller")

    T0 = acc.now().replace(hour=9, minute=0, second=0, microsecond=0)

    # ۱) بیست اکانت: ۱۰ تا ۱۰۰ گیگ + ۱۰ تا ۵۰ گیگ
    uid = 100
    for i in range(10):
        panel.add_user(uid, f"ali_100g_{i:02d}", 1, 100 * GB, 0, T0 + timedelta(minutes=i))
        uid += 1
    for i in range(10):
        panel.add_user(uid, f"ali_50g_{i:02d}", 1, 50 * GB, 0, T0 + timedelta(minutes=i))
        uid += 1

    # ۲) اکانت ۳۰ گیگی که بعداً حذف می‌شود
    panel.add_user(201, "ali_temp_30g", 1, 30 * GB, 0, T0 + timedelta(minutes=30))
    # ۳) اکانت ۱۰ گیگی که تا ۹٫۹ گیگ مصرف می‌شود و بعد حذف
    panel.add_user(202, "ali_almost_full", 1, 10 * GB, 0, T0 + timedelta(minutes=31))
    # ۴) اکانت ۴۰ گیگی که ۳۹ گیگ مصرف و بعد ریست می‌شود
    panel.add_user(203, "ali_reset_me", 1, 40 * GB, 0, T0 + timedelta(minutes=32))

    # ادمین دوم هم دو اکانت می‌سازد
    panel.add_user(301, "reza_big", 2, 200 * GB, 0, T0 + timedelta(minutes=5))
    panel.add_user(302, "reza_small", 2, 5 * GB, 0, T0 + timedelta(minutes=6))

    # --- اسکن اول: همه دیده می‌شوند ------------------------------------ #
    # ۱۰ تا ۱۰۰گیگ + ۱۰ تا ۵۰گیگ + ۳۰گیگ + ۱۰گیگ + ۴۰گیگ = ۲۳ اکانت برای ali
    # به‌علاوه‌ی ۲ اکانت برای reza  => ۲۵ اکانت در کل
    res1 = acc.scan(now=T0 + timedelta(minutes=40))
    check("اسکن اول — تعداد provision", res1.provisioned, 25)
    check("اسکن اول — خطا", res1.errors, [])

    day = T0.strftime("%Y-%m-%d")
    expected_prov = (10 * 100 * GB) + (10 * 50 * GB) + 30 * GB + 10 * GB + 40 * GB
    check(
        "جمع ساخت ادمین ali (۱۶۲۰-۴۰ = ۱۵۸۰ گیگ)",
        int(
            sum(
                r["provision_bytes"]
                for r in ledger.summary_by_admin(day)
                if r["admin_name"] == "ali_reseller"
            )
            / GB
        ),
        int(expected_prov / GB),
    )

    # --- چند ساعت بعد: حذف‌ها و مصرف --------------------------------- #
    T1 = T0 + timedelta(hours=4)

    # اکانت ۳۰ گیگی حذف شد
    panel.delete_user(201)
    # اکانت ۱۰ گیگی تا ۹٫۹ گیگ مصرف شد و بعد حذف شد
    panel.set_used(202, int(9.9 * GB))
    panel.delete_user(202)
    # اکانت ۴۰ گیگی ۳۹ گیگ مصرف کرد
    panel.set_used(203, 39 * GB)
    res_mid = acc.scan(now=T1)
    check("اسکن میانی — حذف‌شده‌ها", res_mid.deleted, 2)

    # --- ریست اکانت ۴۰ گیگی (پنل used_traffic را صفر می‌کند) ---------- #
    T2 = T0 + timedelta(hours=6)
    panel.log_reset(203, 39 * GB, T2)
    panel.set_used(203, 0)
    res_reset = acc.scan(now=T2)
    check("اسکن ریست — تعداد reset", res_reset.resets, 1)
    check("اسکن ریست — بایت افزوده", res_reset.bytes_added, 40 * GB)

    # --- تأیید جمع نهایی ---------------------------------------------- #
    summary = {r["admin_name"]: r for r in ledger.summary_by_admin(day)}
    ali = summary["ali_reseller"]
    expected_ali = (10 * 100 * GB) + (10 * 50 * GB) + 30 * GB + 10 * GB + 40 * GB + 40 * GB
    check("جمع بیل‌شده‌ی ادمین ali (ساخت + ریست)", ali["total_bytes"], expected_ali)
    check("   = به گیگابایت", int(ali["total_bytes"] / GB), 1620)
    check("تعداد اکانت‌های ali", ali["provision_n"], 23)
    check("تعداد ریست‌های ali", ali["reset_n"], 1)
    check("تعداد حذف‌شده‌های ali", ali["deleted_n"], 2)

    reza = summary["reza_reseller"]
    check("جمع بیل‌شده‌ی ادمین reza", reza["total_bytes"], 205 * GB)

    totals = ledger.totals_between(day, day)
    check("جمع کل پنل", int(totals["total_bytes"] / GB), 1620 + 205)


def test_deleted_before_seen() -> None:
    """اکانتی که ساخته و در همان فاصله‌ی اسکن حذف می‌شود — نباید حذف آن آمار را کم کند."""
    print("\n=== حذف بعد از ثبت، آمار را کم نمی‌کند ===")
    cfg = Config(ledger_path=":memory:", scan_interval=60)
    panel, ledger, acc = make_env(cfg)
    panel.add_admin(1, "ali")
    T0 = acc.now().replace(hour=10, minute=0, second=0, microsecond=0)

    panel.add_user(1, "ghost", 1, 50 * GB, 0, T0)
    acc.scan(now=T0)
    day = T0.strftime("%Y-%m-%d")
    check("ثبت اولیه", ledger.summary_by_admin(day)[0]["total_bytes"], 50 * GB)

    # یک دقیقه بعد حذف می‌شود
    panel.delete_user(1)
    res = acc.scan(now=T0 + timedelta(minutes=1))
    check("حذف شناسایی شد", res.deleted, 1)
    check("آمار بعد از حذف دست‌نخورده ماند", ledger.summary_by_admin(day)[0]["total_bytes"], 50 * GB)


def test_bulk_reset_without_log() -> None:
    """
    ریست دسته‌جمعی پنل (bulk reset) هیچ لاگی در user_usage_logs نمی‌نویسد
    و لاگ‌های قبلی را پاک می‌کند. بات باید از افت مصرف آن را بگیرد.
    """
    print("\n=== ریست دسته‌جمعی بدون لاگ ===")
    cfg = Config(ledger_path=":memory:")
    panel, ledger, acc = make_env(cfg)
    panel.add_admin(1, "ali")
    T0 = acc.now().replace(hour=8, minute=0, second=0, microsecond=0)

    for i in range(3):
        panel.add_user(i + 1, f"u{i}", 1, 100 * GB, 0, T0)
    acc.scan(now=T0)

    # مصرف بالا می‌رود
    for i in range(3):
        panel.set_used(i + 1, 90 * GB)
    acc.scan(now=T0 + timedelta(hours=2))

    # ریست دسته‌جمعی: used_traffic = 0 و user_usage_logs پاک می‌شود
    for i in range(3):
        panel.set_used(i + 1, 0)
    res = acc.scan(now=T0 + timedelta(hours=3))
    check("ریست‌های شناسایی‌شده", res.resets, 3)
    day = T0.strftime("%Y-%m-%d")
    s = ledger.summary_by_admin(day)[0]
    check("تعداد ریست در دفترکل", s["reset_n"], 3)
    check("جمع کل = ۳۰۰ ساخت + ۳۰۰ ریست", int(s["total_bytes"] / GB), 600)


def test_topup() -> None:
    print("\n=== افزایش/کاهش حجم اکانت موجود ===")
    cfg = Config(ledger_path=":memory:")
    panel, ledger, acc = make_env(cfg)
    panel.add_admin(1, "ali")
    T0 = acc.now().replace(hour=11, minute=0, second=0, microsecond=0)
    panel.add_user(1, "vip", 1, 100 * GB, 0, T0)
    acc.scan(now=T0)

    panel.set_limit(1, 160 * GB)
    res = acc.scan(now=T0 + timedelta(minutes=10))
    check("topup شناسایی شد", res.topups, 1)
    check("مابه‌التفاوت بیل شد", res.bytes_added, 60 * GB)

    panel.set_limit(1, 120 * GB)
    res2 = acc.scan(now=T0 + timedelta(minutes=20))
    check("کاهش حجم بیل نمی‌شود", res2.bytes_added, 0)
    check("کاهش حجم ثبت می‌شود", res2.cuts, 1)


def test_no_double_count_on_restart() -> None:
    """راه‌اندازی مجدد بات نباید اکانت‌ها را دوباره بیل کند."""
    print("\n=== جلوگیری از بیل دوباره ===")
    cfg = Config(ledger_path=":memory:")
    panel, ledger, acc = make_env(cfg)
    panel.add_admin(1, "ali")
    T0 = acc.now().replace(hour=12, minute=0, second=0, microsecond=0)
    panel.add_user(1, "once", 1, 100 * GB, 0, T0)

    acc.scan(now=T0)
    acc.scan(now=T0 + timedelta(minutes=1))
    acc.scan(now=T0 + timedelta(minutes=2))
    day = T0.strftime("%Y-%m-%d")
    check("فقط یک بار بیل شد", ledger.summary_by_admin(day)[0]["provision_n"], 1)
    check("حجم درست است", ledger.summary_by_admin(day)[0]["total_bytes"], 100 * GB)


def test_backfill_window() -> None:
    """اکانت‌های قدیمی‌تر از پنجره‌ی backfill نباید بیل شوند."""
    print("\n=== پنجره‌ی backfill ===")
    cfg = Config(ledger_path=":memory:", backfill_days=7)
    panel, ledger, acc = make_env(cfg)
    panel.add_admin(1, "ali")
    now = acc.now()
    panel.add_user(1, "old_user", 1, 500 * GB, 0, now - timedelta(days=90))
    panel.add_user(2, "new_user", 1, 50 * GB, 0, now - timedelta(days=2))

    res = acc.scan(now=now)
    check("فقط اکانت جدید بیل شد", res.provisioned, 1)
    check("حجم بیل‌شده", res.bytes_added, 50 * GB)


def test_orphan_users() -> None:
    """اکانت بدون ادمین نباید گزارش را بشکند."""
    print("\n=== اکانت بدون ادمین ===")
    cfg = Config(ledger_path=":memory:")
    panel, ledger, acc = make_env(cfg)
    T0 = acc.now().replace(hour=13, minute=0, second=0, microsecond=0)
    panel.add_user(1, "orphan", None, 20 * GB, 0, T0)
    res = acc.scan(now=T0)
    check("ثبت شد", res.provisioned, 1)
    s = ledger.summary_by_admin(T0.strftime("%Y-%m-%d"))[0]
    check("نام ادمین جایگزین", s["admin_name"], "بدون ادمین")


def test_report_rendering() -> None:
    print("\n=== ساخت متن گزارش ===")
    cfg = Config(ledger_path=":memory:")
    panel, ledger, acc = make_env(cfg)
    panel.add_admin(1, "ali")
    T0 = acc.now().replace(hour=14, minute=0, second=0, microsecond=0)
    for i in range(5):
        panel.add_user(i + 1, f"user_{i}", 1, 100 * GB, 0, T0)
    acc.scan(now=T0)

    day = T0.strftime("%Y-%m-%d")
    chunks = build_day_report(cfg, ledger, day)
    joined = "\n".join(chunks)
    check("نام ادمین در گزارش", "ali" in joined, True)
    check("یوزرنیم در گزارش", "user_0" in joined, True)
    check("جمع در گزارش", "500 GB" in joined, True)
    check("تکه‌ها زیر حد تلگرام", all(len(c) <= 4096 for c in chunks), True)

    empty = build_day_report(cfg, ledger, "2000-01-01")
    check("گزارش روز خالی", "هیچ رویدادی" in empty[0], True)


def test_helpers() -> None:
    print("\n=== ابزارهای کمکی ===")
    check("human_bytes گیگ", human_bytes(100 * GB), "100 GB")
    check("human_bytes ترابایت", human_bytes(1536 * GB), "1.5 TB")
    check("human_bytes صفر", human_bytes(0), "0 B")
    check("human_bytes None (نامحدود)", human_bytes(None), "∞")
    check("human_bytes مگ", human_bytes(int(1.5 * GB)), "1.5 GB")

    check(
        "تبدیل asyncpg",
        normalize_panel_db_url("postgresql+asyncpg://u:p@h:5432/db"),
        "postgresql+psycopg2://u:p@h:5432/db",
    )
    check(
        "تبدیل asyncmy",
        normalize_panel_db_url("mysql+asyncmy://u:p@h/db"),
        "mysql+pymysql://u:p@h/db",
    )
    check(
        "تبدیل aiosqlite",
        normalize_panel_db_url("sqlite+aiosqlite:////var/lib/pasarguard/db.sqlite3"),
        "sqlite:////var/lib/pasarguard/db.sqlite3",
    )
    check("پارس ساعت", parse_hhmm(" 00:00 "), (0, 0))
    check("پارس ساعت ۲۳:۳۰", parse_hhmm("23:30"), (23, 30))
    check("بولی فارسی", _parse_bool("بله"), True)
    check("بولی false", _parse_bool("false"), False)

    cfg = Config(report_time="00:00", timezone="Asia/Tehran")
    now = datetime(2026, 9, 21, 10, 0, 0, tzinfo=cfg.tz)
    secs, target = seconds_until_next_report(cfg, now)
    check("گزارش بعدی نیمه‌شب است", target.strftime("%H:%M"), "00:00")
    check("فاصله ۱۴ ساعت", round(secs / 3600), 14)

    parts = _split_message("x" * 9000, 4000)
    check("تقسیم پیام", len(parts), 3)
    check("هیچ تکه‌ای بیش از حد نیست", all(len(p) <= 4000 for p in parts), True)
    check("محتوا گم نشد", "".join(parts), "x" * 9000)

    multiline = "\n".join(f"خط شماره {i} با کمی متن اضافه برای بلندتر شدن" for i in range(200))
    mparts = _split_message(multiline, 4000)
    check("چندخطی: همه زیر حد", all(len(p) <= 4000 for p in mparts), True)
    check("چندخطی: محتوا گم نشد", "\n".join(mparts).strip(), multiline.strip())
    check("تک‌خطی کوتاه دست‌نخورده", _split_message("سلام", 4000), ["سلام"])


def test_env_parsing() -> None:
    print("\n=== پارس .env پنل ===")
    with tempfile.TemporaryDirectory() as d:
        env = Path(d) / ".env"
        env.write_text(
            '# comment\n'
            'SQLALCHEMY_DATABASE_URL = "postgresql+asyncpg://pg:secret@127.0.0.1:5432/pasarguard"\n'
            'UVICORN_HOST = "0.0.0.0"\n',
            encoding="utf-8",
        )
        val = _panel_env_value(str(env))
        check(
            "خواندن از .env",
            val,
            "postgresql+asyncpg://pg:secret@127.0.0.1:5432/pasarguard",
        )
        check("نرمال‌سازی", normalize_panel_db_url(val).startswith("postgresql+psycopg2://"), True)

        env2 = Path(d) / ".env2"
        env2.write_text("SQLALCHEMY_DATABASE_URL=sqlite+aiosqlite:////var/lib/pasarguard/db.sqlite3\n")
        check(
            "حالت بدون فاصله و کوتیشن",
            _panel_env_value(str(env2)),
            "sqlite+aiosqlite:////var/lib/pasarguard/db.sqlite3",
        )
        check("فایل ناموجود", _panel_env_value(str(Path(d) / "nope")), None)


def test_telegram_dispatch() -> None:
    """دستورات تلگرام از مسیر واقعی TelegramBot.dispatch بررسی می‌شوند."""
    print("\n=== دستورات تلگرام (dispatch واقعی) ===")
    from bot import TelegramBot

    cfg = Config(ledger_path=":memory:", dry_run_telegram=True, allowed_chat_ids=[123])
    panel, ledger, acc = make_env(cfg)
    panel.add_admin(1, "ali")
    panel.add_admin(2, "reza")
    T0 = acc.now().replace(hour=15, minute=0, second=0, microsecond=0)
    for i in range(3):
        panel.add_user(i + 1, f"cust_{i}", 1, 100 * GB, 0, T0)
    acc.scan(now=T0)

    bot = TelegramBot(cfg, acc, ledger)
    day = T0.strftime("%Y-%m-%d")

    joined = " ".join(bot.dispatch("/help"))
    check("/help دستورات را دارد", "/report" in joined and "/admins" in joined, True)

    joined = " ".join(bot.dispatch("/today"))
    check("/today حجم امروز را دارد", "300 GB" in joined, True)

    joined = " ".join(bot.dispatch(f"/report {day}"))
    check("/report نام ادمین را دارد", "ali" in joined, True)
    check("/report یوزرنیم را دارد", "cust_0" in joined, True)

    bad = bot.dispatch("/report not-a-date")
    check("/report تاریخ بد → راهنما", "فرمت تاریخ نامعتبر" in bad[0], True)

    joined = " ".join(bot.dispatch("/week 7"))
    check("/week جمع هفته را دارد", "ali" in joined, True)

    joined = " ".join(bot.dispatch("/admin ali"))
    check("/admin جزئیات ادمین", "ali" in joined and "300 GB" in joined, True)

    empty = bot.dispatch("/admin nobody")
    check("/admin ادمین ناشناخته", "ثبت نشده" in empty[0], True)

    joined = " ".join(bot.dispatch("/user cust_1"))
    check("/user تاریخچه", "cust_1" in joined and "ساخت اکانت" in joined, True)

    missing = bot.dispatch("/user ghost_user")
    check("/user کاربر ناشناخته", "ثبت نشده" in missing[0], True)

    joined = " ".join(bot.dispatch("/admins"))
    check("/admins فهرست ادمین‌ها", "ali" in joined and "reza" in joined, True)

    scan_out = " ".join(bot.dispatch("/scan"))
    check("/scan اسکن فوری", "اسکن انجام شد" in scan_out, True)

    unknown = bot.dispatch("/blahblah")
    check("دستور ناشناخته", "ناشناخته" in unknown[0], True)

    check("/ping", bot.dispatch("/ping")[0], "pong ✅")

    # دسترسی غیرمجاز نباید پردازش شود
    upd = {"update_id": 1, "message": {"chat": {"id": 999}, "text": "/today"}}
    sent: list[str] = []
    bot.send = lambda chat_id, text: sent.append(text)  # type: ignore[assignment]
    bot.handle_update(upd)
    check("چت غیرمجاز پاسخ نمی‌گیرد", sent, [])

    upd_ok = {"update_id": 2, "message": {"chat": {"id": 123}, "text": "/today"}}
    bot.handle_update(upd_ok)
    check("چت مجاز پاسخ می‌گیرد", len(sent) > 0, True)


def main() -> int:
    print("=" * 68)
    print(" تست سنجه‌ی حسابداری pg-accountant")
    print("=" * 68)
    for fn in (
        test_main_scenario,
        test_deleted_before_seen,
        test_bulk_reset_without_log,
        test_topup,
        test_no_double_count_on_restart,
        test_backfill_window,
        test_orphan_users,
        test_report_rendering,
        test_telegram_dispatch,
        test_helpers,
        test_env_parsing,
    ):
        fn()
    print("\n" + "=" * 68)
    print(f" نتیجه: {PASS} موفق / {FAIL} ناموفق")
    print("=" * 68)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
