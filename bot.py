#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pg-accountant — ربات حسابداری حجم ادمین‌ها برای پنل PasarGuard

این بات فقط از دیتابیس پنل «می‌خواند» و هیچ چیزی در پنل تغییر نمی‌دهد.
یک دفترکل (ledger) مستقل در SQLite خودش نگه می‌دارد و هر رویدادی که
بار مالی دارد را ثبت می‌کند:

  provision : ادمین اکانتی ساخته (حتی با مصرف صفر، حتی اگر بعداً حذفش کند)
  reset     : مصرف اکانت ریست شده  → حجم پلن دوباره حساب می‌شود
  topup     : ادمین حجم اکانت موجود را زیاد کرده → مابه‌التفاوت حساب می‌شود
  renewal   : ریست هم‌زمان با تغییر حجم (مثلاً اعمال next_plan)
  deleted   : اکانت حذف شده (اطلاعاتی — قبلاً در provision حساب شده)
  limit_cut : ادمین حجم اکانت را کم کرده (اطلاعاتی)

طراحی ضد تقلب:
  * اسکن دوره‌ای (پیش‌فرض ۶۰ ثانیه) — اکانتی که ساخته و چند ساعت بعد حذف
    شود، در همان لحظه‌ی ساخت ثبت شده و حذف آن چیزی از آمار کم نمی‌کند.
  * ریستِ دسته‌جمعی پنل (bulk reset) هیچ لاگی در user_usage_logs نمی‌نویسد
    و لاگ‌های قبلی را پاک می‌کند؛ این بات ریست را از «افت مصرف بین دو
    اسکن» تشخیص می‌دهد، پس از دست نمی‌رود.
  * اگر بات مدتی خاموش بوده باشد، اکانت‌های ساخته‌شده‌ی هنوز موجود از روی
    users.created_at بازیابی (backfill) می‌شوند و فاصله‌ی خاموشی در گزارش
    اعلام می‌شود.
"""

from __future__ import annotations

import argparse
import configparser
import logging
import os
import re
import signal
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import requests
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

try:  # pragma: no cover - اختیاری، فقط برای لاگ رنگی
    pass
except Exception:  # pragma: no cover
    pass

APP_NAME = "pg-accountant"
VERSION = "1.0.0"

log = logging.getLogger(APP_NAME)

# --------------------------------------------------------------------------- #
# واحد حجم
# --------------------------------------------------------------------------- #

GB = 1024 ** 3
MB = 1024 ** 2
TB = 1024 ** 4

FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def fa(value: Any) -> str:
    """تبدیل ارقام به فارسی."""
    return str(value).translate(FA_DIGITS)


def human_bytes(n: float | int | None) -> str:
    """تبدیل بایت به متن خوانا (مبنای ۱۰۲۴، مثل خود پنل)."""
    if n is None:
        return "∞"
    n = float(n)
    if n < 0:
        return f"-{human_bytes(-n)}"
    for unit, factor in (("TB", TB), ("GB", GB), ("MB", MB), ("KB", 1024)):
        if n >= factor:
            v = n / factor
            text_v = f"{v:.2f}".rstrip("0").rstrip(".")
            return f"{text_v} {unit}"
    return f"{int(n)} B"


# --------------------------------------------------------------------------- #
# پیکربندی
# --------------------------------------------------------------------------- #


@dataclass
class Config:
    # تلگرام
    bot_token: str = ""
    allowed_chat_ids: list[int] = field(default_factory=list)
    # دیتابیس پنل
    panel_db_url: str = ""
    panel_env_file: str = "/opt/pasarguard/.env"
    # دفترکل خودِ بات
    ledger_path: str = "/opt/pg-accountant/ledger.db"
    # زمان‌بندی
    timezone: str = "Asia/Tehran"
    report_time: str = "00:00"          # ساعت ارسال گزارش روزانه (وقت محلی)
    report_previous_day: bool = True    # گزارشِ روزِ گذشته را بدهد (نه روزِ در حال جریان)
    scan_interval: int = 60             # فاصله‌ی اسکن‌ها به ثانیه
    # قوانین حسابداری
    reset_drop_tolerance: int = 1 * MB  # افت مصرف کمتر از این = ریست حساب نمی‌شود
    count_topup: bool = True            # افزایش حجم اکانت موجود حساب شود
    backfill_on_first_run: bool = True  # اکانت‌های موجود در اولین اجرا ثبت شوند
    backfill_days: int = 30             # فقط اکانت‌های ساخته‌شده در این چند روز اخیر
    # رفتار
    report_show_user_list: bool = True  # لیست یوزرنیم‌ها در گزارش بیاید
    max_users_in_report: int = 60       # بیش از این، فقط خلاصه
    persian_digits: bool = True
    heartbeat_gap_warn_seconds: int = 5 * 60
    # تست
    dry_run_telegram: bool = False      # ارسال واقعی به تلگرام انجام نشود (تست)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def num(self, v: Any) -> str:
        return fa(v) if self.persian_digits else str(v)


_ENV_KEY_ALIASES = {
    "bot_token": ("TELEGRAM_BOT_TOKEN", "BOT_TOKEN"),
    "allowed_chat_ids": ("TELEGRAM_ALLOWED_CHAT_IDS", "ALLOWED_CHAT_IDS"),
    "panel_db_url": ("PANEL_DB_URL", "PG_PANEL_DB_URL"),
    "panel_env_file": ("PANEL_ENV_FILE",),
    "ledger_path": ("LEDGER_PATH",),
    "timezone": ("TZ_NAME", "TIMEZONE"),
    "report_time": ("REPORT_TIME",),
    "scan_interval": ("SCAN_INTERVAL",),
}

_BOOL_KEYS = {
    "report_previous_day",
    "count_topup",
    "backfill_on_first_run",
    "report_show_user_list",
    "persian_digits",
    "dry_run_telegram",
}
_INT_KEYS = {
    "scan_interval",
    "reset_drop_tolerance",
    "backfill_days",
    "max_users_in_report",
    "heartbeat_gap_warn_seconds",
}


def _parse_bool(raw: str) -> bool:
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "y", "بله", "درست"}


def _panel_env_value(env_file: str, key: str = "SQLALCHEMY_DATABASE_URL") -> str | None:
    """
    خواندن یک کلید از فایل .env پنل.

    اسکریپت نصب پاسارگارد این خط را با فاصله و کوتیشن می‌نویسد:
        SQLALCHEMY_DATABASE_URL = "postgresql+asyncpg://u:p@127.0.0.1:5432/pasarguard"
    پس پارسر باید هر دو حالت را تحمل کند.
    """
    path = Path(env_file)
    if not path.exists():
        return None
    pattern = re.compile(rf'^\s*(?:export\s+)?{re.escape(key)}\s*=\s*(.*)$')
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = pattern.match(stripped)
        if not m:
            continue
        val = m.group(1).strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in {"'", '"'}:
            val = val[1:-1]
        return val.strip()
    return None


def normalize_panel_db_url(url: str) -> str:
    """
    پنل از درایورهای async استفاده می‌کند؛ بات همزمان (sync) است، پس درایور
    معادل sync را جایگزین می‌کنیم.

        postgresql+asyncpg://... -> postgresql+psycopg2://...
        mysql+asyncmy://...      -> mysql+pymysql://...
        sqlite+aiosqlite:///x    -> sqlite:///x
    """
    url = url.strip().strip('"').strip("'")
    replacements = {
        "postgresql+asyncpg://": "postgresql+psycopg2://",
        "postgres+asyncpg://": "postgresql+psycopg2://",
        "postgresql+psycopg_async://": "postgresql+psycopg2://",
        "mysql+asyncmy://": "mysql+pymysql://",
        "mysql+aiomysql://": "mysql+pymysql://",
        "mysql+asyncmy": "mysql+pymysql",
        "mariadb+asyncmy://": "mysql+pymysql://",
        "sqlite+aiosqlite://": "sqlite://",
    }
    for src, dst in replacements.items():
        if url.startswith(src):
            return dst + url[len(src):]
    return url


def load_config(path: str | None) -> Config:
    cfg = Config()
    parser = configparser.ConfigParser(interpolation=None)
    files: list[str] = []
    if path:
        files.append(path)
    else:
        files.extend(
            [
                os.environ.get("PG_ACCOUNTANT_CONFIG", ""),
                "/opt/pg-accountant/config.ini",
                str(Path(__file__).resolve().parent / "config.ini"),
                "./config.ini",
            ]
        )

    read = [f for f in files if f and Path(f).exists()]
    if read:
        parser.read(read, encoding="utf-8")
        section = parser["bot"] if parser.has_section("bot") else parser[parser.default_section]
        for key, raw in section.items():
            key = key.strip().lower()
            if not hasattr(cfg, key):
                log.warning("کلید ناشناخته در config: %s", key)
                continue
            raw = raw.strip()
            if key in _BOOL_KEYS:
                setattr(cfg, key, _parse_bool(raw))
            elif key in _INT_KEYS:
                setattr(cfg, key, int(raw))
            elif key == "allowed_chat_ids":
                setattr(cfg, key, _parse_chat_ids(raw))
            else:
                setattr(cfg, key, raw)

    # متغیرهای محیطی اولویت بالاتری دارند
    for key, aliases in _ENV_KEY_ALIASES.items():
        for alias in aliases:
            val = os.environ.get(alias)
            if val:
                if key == "allowed_chat_ids":
                    setattr(cfg, key, _parse_chat_ids(val))
                elif key in _INT_KEYS:
                    setattr(cfg, key, int(val))
                else:
                    setattr(cfg, key, val.strip())
                break

    # اگر آدرس دیتابیس داده نشده، از .env پنل بردار
    if not cfg.panel_db_url and cfg.panel_env_file:
        found = _panel_env_value(cfg.panel_env_file)
        if found:
            log.info("آدرس دیتابیس از %s خوانده شد", cfg.panel_env_file)
            cfg.panel_db_url = found

    cfg.panel_db_url = normalize_panel_db_url(cfg.panel_db_url) if cfg.panel_db_url else ""
    return cfg


def _parse_chat_ids(raw: str) -> list[int]:
    out: list[int] = []
    for part in re.split(r"[,\s]+", raw.strip()):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError:
            log.warning("chat_id نامعتبر نادیده گرفته شد: %r", part)
    return out


# --------------------------------------------------------------------------- #
# دفترکل (ledger)
# --------------------------------------------------------------------------- #

LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    user_id      INTEGER PRIMARY KEY,
    username     TEXT,
    admin_id     INTEGER,
    admin_name   TEXT,
    data_limit   INTEGER,
    used_traffic INTEGER,
    created_at   TEXT,
    first_seen   TEXT,
    last_seen    TEXT,
    reset_count  INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             TEXT NOT NULL,
    day            TEXT NOT NULL,
    admin_id       INTEGER,
    admin_name     TEXT,
    user_id        INTEGER,
    username       TEXT,
    kind           TEXT NOT NULL,
    bytes          INTEGER NOT NULL DEFAULT 0,
    used_traffic   INTEGER,
    data_limit     INTEGER,
    dedupe_key     TEXT,
    note           TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_day        ON events(day);
CREATE INDEX IF NOT EXISTS ix_events_admin_day  ON events(admin_id, day);
CREATE UNIQUE INDEX IF NOT EXISTS ux_events_dedupe ON events(dedupe_key);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

EVENT_KINDS = ("provision", "reset", "renewal", "topup", "limit_cut", "deleted")
BILLED_KINDS = ("provision", "reset", "renewal", "topup")

KIND_LABEL_FA = {
    "provision": "ساخت اکانت",
    "reset": "ریست مصرف",
    "renewal": "تمدید/تغییر پلن",
    "topup": "افزودن حجم",
    "limit_cut": "کاهش حجم",
    "deleted": "حذف اکانت",
}


@dataclass
class PanelUser:
    user_id: int
    username: str
    admin_id: int | None
    data_limit: int | None
    used_traffic: int
    created_at: datetime | None


@dataclass
class PanelAdmin:
    admin_id: int
    username: str


def parse_ts(value: Any) -> datetime | None:
    """تبدیل مقدار زمانِ دیتابیس به datetime آگاه از UTC."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    s = str(value).strip()
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    # SQLite ممکن است فاصله به‌جای T بگذارد
    if " " in s and "T" not in s:
        s = s.replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
            try:
                dt = datetime.strptime(s[:26], fmt)
                break
            except ValueError:
                continue
        else:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class Ledger:
    """دفترکل محلی بات (SQLite)."""

    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript(LEDGER_SCHEMA)
            self.conn.commit()

    # -- ابزار عمومی ------------------------------------------------------ #

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    def get_meta(self, key: str) -> str | None:
        with self.lock:
            row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self.conn.commit()

    # -- رویدادها --------------------------------------------------------- #

    def add_event(
        self,
        *,
        kind: str,
        day: str,
        ts: datetime,
        admin_id: int | None,
        admin_name: str,
        user_id: int | None,
        username: str,
        bytes_: int,
        used_traffic: int | None = None,
        data_limit: int | None = None,
        dedupe_key: str | None = None,
        note: str = "",
    ) -> bool:
        """ثبت رویداد؛ اگر dedupe_key تکراری باشد ثبت نمی‌شود و False برمی‌گرداند."""
        assert kind in EVENT_KINDS, kind
        with self.lock:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO events"
                "(ts, day, admin_id, admin_name, user_id, username, kind, bytes,"
                " used_traffic, data_limit, dedupe_key, note)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ts.astimezone(timezone.utc).isoformat(),
                    day,
                    admin_id,
                    admin_name,
                    user_id,
                    username,
                    kind,
                    int(bytes_),
                    used_traffic,
                    data_limit,
                    dedupe_key,
                    note,
                ),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def get_snapshot(self, user_id: int) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(
                "SELECT * FROM snapshots WHERE user_id = ?", (user_id,)
            ).fetchone()

    def upsert_snapshot(
        self,
        *,
        user_id: int,
        username: str,
        admin_id: int | None,
        admin_name: str,
        data_limit: int | None,
        used_traffic: int,
        created_at: datetime | None,
        now: datetime,
        bump_reset: bool = False,
    ) -> None:
        now_iso = now.astimezone(timezone.utc).isoformat()
        created_iso = created_at.astimezone(timezone.utc).isoformat() if created_at else None
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO snapshots
                    (user_id, username, admin_id, admin_name, data_limit, used_traffic,
                     created_at, first_seen, last_seen, reset_count)
                VALUES (?,?,?,?,?,?,?,?,?,0)
                ON CONFLICT(user_id) DO UPDATE SET
                    username     = excluded.username,
                    admin_id     = excluded.admin_id,
                    admin_name   = excluded.admin_name,
                    data_limit   = excluded.data_limit,
                    used_traffic = excluded.used_traffic,
                    last_seen    = excluded.last_seen,
                    reset_count  = reset_count + CASE WHEN ? THEN 1 ELSE 0 END
                """,
                (
                    user_id,
                    username,
                    admin_id,
                    admin_name,
                    data_limit,
                    used_traffic,
                    created_iso,
                    now_iso,
                    now_iso,
                    bump_reset,
                ),
            )
            self.conn.commit()

    def remove_snapshot(self, user_id: int) -> sqlite3.Row | None:
        """اسنپ‌شات را برمی‌دارد و برمی‌گرداند (برای ثبت رویداد حذف)."""
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM snapshots WHERE user_id = ?", (user_id,)
            ).fetchone()
            self.conn.execute("DELETE FROM snapshots WHERE user_id = ?", (user_id,))
            self.conn.commit()
            return row

    def all_snapshots(self) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute("SELECT * FROM snapshots").fetchall()

    # -- گزارش ------------------------------------------------------------ #

    def day_rows(self, day: str) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(
                "SELECT * FROM events WHERE day = ? ORDER BY ts, id", (day,)
            ).fetchall()

    def summary_by_admin(self, day: str) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute(
                """
                SELECT admin_id, admin_name,
                       SUM(CASE WHEN kind = 'provision' THEN bytes ELSE 0 END) AS provision_bytes,
                       SUM(CASE WHEN kind = 'provision' THEN 1 ELSE 0 END)     AS provision_n,
                       SUM(CASE WHEN kind IN ('reset','renewal') THEN bytes ELSE 0 END) AS reset_bytes,
                       SUM(CASE WHEN kind IN ('reset','renewal') THEN 1 ELSE 0 END)     AS reset_n,
                       SUM(CASE WHEN kind = 'topup' THEN bytes ELSE 0 END)     AS topup_bytes,
                       SUM(CASE WHEN kind = 'topup' THEN 1 ELSE 0 END)         AS topup_n,
                       SUM(CASE WHEN kind = 'limit_cut' THEN 1 ELSE 0 END)     AS cut_n,
                       SUM(CASE WHEN kind = 'deleted' THEN 1 ELSE 0 END)       AS deleted_n,
                       SUM(CASE WHEN kind IN ('provision','reset','renewal','topup') THEN bytes ELSE 0 END) AS total_bytes,
                       COUNT(DISTINCT CASE WHEN kind = 'provision' THEN user_id END) AS distinct_users
                FROM events WHERE day = ?
                GROUP BY admin_id, admin_name
                ORDER BY total_bytes DESC
                """,
                (day,),
            ).fetchall()
        return [dict(r) for r in rows]

    def events_between(self, start_day: str, end_day: str) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(
                "SELECT * FROM events WHERE day >= ? AND day <= ? ORDER BY ts, id",
                (start_day, end_day),
            ).fetchall()

    def user_events(self, username: str, limit: int = 40) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(
                "SELECT * FROM events WHERE username = ? ORDER BY ts DESC LIMIT ?",
                (username, limit),
            ).fetchall()

    def totals_between(self, start_day: str, end_day: str) -> dict[str, Any]:
        with self.lock:
            row = self.conn.execute(
                """
                SELECT COALESCE(SUM(CASE WHEN kind IN ('provision','reset','renewal','topup')
                                         THEN bytes ELSE 0 END), 0) AS total_bytes,
                       COALESCE(SUM(CASE WHEN kind = 'provision' THEN 1 ELSE 0 END), 0) AS accounts
                FROM events WHERE day >= ? AND day <= ?
                """,
                (start_day, end_day),
            ).fetchone()
        return dict(row)


# --------------------------------------------------------------------------- #
# خواندن از پنل
# --------------------------------------------------------------------------- #


class PanelDB:
    """دسترسی فقط‌خواندنی به دیتابیس پنل PasarGuard."""

    def __init__(self, url: str, *, timeout: int = 15):
        self.url = url
        kwargs: dict[str, Any] = {"future": True, "pool_pre_ping": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"timeout": timeout}
        else:
            kwargs["pool_size"] = 1
            kwargs["max_overflow"] = 0
            kwargs["pool_recycle"] = 300
        try:
            self.engine: Engine = create_engine(url, **kwargs)
        except ModuleNotFoundError as exc:
            raise SystemExit(
                f"درایور دیتابیس نصب نیست ({exc.name}).\n"
                f"  آدرس دیتابیس: {url}\n"
                f"  راه‌حل:\n"
                + (
                    "    pip install psycopg2-binary\n"
                    "    (اگر نصب نشد:  pip install \"psycopg[binary]\"  و در آدرس دیتابیس\n"
                    "     postgresql+psycopg2 را به postgresql+psycopg تغییر بده)"
                    if "postgresql" in url
                    else "    pip install \"pymysql[cryptography]\""
                    if "mysql" in url
                    else f"    pip install {exc.name}"
                )
            ) from exc

    def dispose(self) -> None:
        try:
            self.engine.dispose()
        except Exception:  # pragma: no cover
            pass

    def admins(self) -> dict[int, PanelAdmin]:
        sql = text("SELECT id, username FROM admins")
        with self.engine.connect() as conn:
            rows = conn.execute(sql).fetchall()
        return {int(r[0]): PanelAdmin(int(r[0]), str(r[1])) for r in rows}

    def users(self) -> list[PanelUser]:
        sql = text(
            "SELECT id, username, admin_id, data_limit, used_traffic, created_at FROM users"
        )
        with self.engine.connect() as conn:
            rows = conn.execute(sql).fetchall()
        out: list[PanelUser] = []
        for r in rows:
            out.append(
                PanelUser(
                    user_id=int(r[0]),
                    username=str(r[1]),
                    admin_id=(int(r[2]) if r[2] is not None else None),
                    data_limit=(int(r[3]) if r[3] is not None else None),
                    used_traffic=int(r[4] or 0),
                    created_at=parse_ts(r[5]),
                )
            )
        return out

    def usage_reset_logs_since(self, since: datetime, *, strict: bool = False) -> list[dict[str, Any]]:
        """
        ریست‌های ثبت‌شده در user_usage_logs.

        نکته: ریست دسته‌جمعی پنل این جدول را پاک می‌کند و چیزی نمی‌نویسد،
        پس این فقط یک منبع کمکی است؛ منبع اصلی تشخیص ریست، مقایسه‌ی
        used_traffic بین دو اسکن است.

        در حالت عادی (strict=False) خطا را قورت می‌دهد تا اسکن نشکند؛
        برای checkdb با strict=True صدا می‌شود تا مشکل پنهان نماند.
        """
        sql = text(
            "SELECT user_id, used_traffic_at_reset, reset_at FROM user_usage_logs "
            "WHERE reset_at >= :since"
        )
        with self.engine.connect() as conn:
            try:
                rows = conn.execute(sql, {"since": since}).fetchall()
            except Exception:
                if strict:
                    raise
                return []
        out = []
        for r in rows:
            out.append(
                {
                    "user_id": int(r[0]),
                    "used_traffic_at_reset": int(r[1] or 0),
                    "reset_at": parse_ts(r[2]),
                }
            )
        return out

    def healthcheck(self) -> str:
        with self.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return "ok"

    def db_kind(self) -> str:
        return self.engine.dialect.name

    def inspect_schema(self) -> dict[str, list[str]]:
        """ستون‌های واقعیِ جدول‌های پنل را برمی‌گرداند (برای اعتبارسنجی)."""
        from sqlalchemy import inspect as sa_inspect

        insp = sa_inspect(self.engine)
        existing = set(insp.get_table_names())
        out: dict[str, list[str]] = {}
        for table in ("admins", "users", "user_usage_logs"):
            out[table] = (
                [c["name"] for c in insp.get_columns(table)] if table in existing else []
            )
        return out


# --------------------------------------------------------------------------- #
# موتور حسابداری
# --------------------------------------------------------------------------- #


@dataclass
class ScanResult:
    provisioned: int = 0
    resets: int = 0
    renewals: int = 0
    topups: int = 0
    cuts: int = 0
    deleted: int = 0
    backfilled: int = 0
    bytes_added: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def total_events(self) -> int:
        return self.provisioned + self.resets + self.renewals + self.topups + self.cuts + self.deleted


class Accountant:
    """
    هسته‌ی حسابداری: دیتابیس پنل را اسکن می‌کند و رویدادهای بیل‌شدنی را در
    دفترکل ثبت می‌کند. کاملاً مستقل از تلگرام است تا قابل تست باشد.
    """

    def __init__(self, cfg: Config, panel: PanelDB, ledger: Ledger):
        self.cfg = cfg
        self.panel = panel
        self.ledger = ledger

    # -- زمان ------------------------------------------------------------- #

    def now(self) -> datetime:
        return datetime.now(self.cfg.tz)

    def local_day(self, ts: datetime) -> str:
        return ts.astimezone(self.cfg.tz).strftime("%Y-%m-%d")

    # -- اسکن ------------------------------------------------------------- #

    def scan(self, *, now: datetime | None = None) -> ScanResult:
        res = ScanResult()
        now = now or self.now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        try:
            panel_users = self.panel.users()
        except Exception as exc:
            res.errors.append(f"خواندن users ناموفق: {exc}")
            log.exception("خواندن جدول users ناموفق بود")
            return res

        try:
            admins = self.panel.admins()
        except Exception as exc:
            res.errors.append(f"خواندن admins ناموفق: {exc}")
            admins = {}

        def admin_name_of(admin_id: int | None, fallback: str = "") -> str:
            if admin_id is None:
                return fallback or "بدون ادمین"
            found = admins.get(admin_id)
            return found.username if found else (fallback or f"admin#{admin_id}")

        # ۱) ریست‌های ثبت‌شده در user_usage_logs (به‌عنوان منبع کمکی)
        last_scan_iso = self.ledger.get_meta("last_scan_ts")
        since = parse_ts(last_scan_iso) if last_scan_iso else now - timedelta(days=self.cfg.backfill_days)
        logged_resets: dict[int, list[dict[str, Any]]] = {}
        if since:
            for entry in self.panel.usage_reset_logs_since(since - timedelta(minutes=5)):
                logged_resets.setdefault(entry["user_id"], []).append(entry)

        seen_ids: set[int] = set()
        first_run = self.ledger.get_meta("initialized") != "1"

        # ۲) کاربران موجود
        for u in panel_users:
            seen_ids.add(u.user_id)
            snap = self.ledger.get_snapshot(u.user_id)
            a_name = admin_name_of(u.admin_id)
            day = self.local_day(now)

            if snap is None:
                # --- کاربر جدید (یا اولین باری که بات او را می‌بیند) -------- #
                created = u.created_at or now
                age_days = (now - created).total_seconds() / 86400

                if first_run and not self.cfg.backfill_on_first_run:
                    # فقط اسنپ‌شات می‌گیریم، بدون بیل کردن
                    self.ledger.upsert_snapshot(
                        user_id=u.user_id, username=u.username, admin_id=u.admin_id,
                        admin_name=a_name, data_limit=u.data_limit,
                        used_traffic=u.used_traffic, created_at=created, now=now,
                    )
                    continue

                if first_run and age_days > self.cfg.backfill_days:
                    # قدیمی‌تر از پنجره‌ی backfill — فقط اسنپ‌شات
                    self.ledger.upsert_snapshot(
                        user_id=u.user_id, username=u.username, admin_id=u.admin_id,
                        admin_name=a_name, data_limit=u.data_limit,
                        used_traffic=u.used_traffic, created_at=created, now=now,
                    )
                    continue

                event_day = self.local_day(created) if first_run else day
                event_ts = created if first_run else now

                added = self.ledger.add_event(
                    kind="provision",
                    day=event_day,
                    ts=event_ts,
                    admin_id=u.admin_id,
                    admin_name=a_name,
                    user_id=u.user_id,
                    username=u.username,
                    bytes_=(u.data_limit or 0),
                    used_traffic=u.used_traffic,
                    data_limit=u.data_limit,
                    dedupe_key=f"prov:{u.user_id}",
                    note="backfill" if first_run else "",
                )
                if added:
                    res.provisioned += 1
                    if first_run:
                        res.backfilled += 1
                    res.bytes_added += (u.data_limit or 0)

                self.ledger.upsert_snapshot(
                    user_id=u.user_id, username=u.username, admin_id=u.admin_id,
                    admin_name=a_name, data_limit=u.data_limit,
                    used_traffic=u.used_traffic, created_at=created, now=now,
                )
                continue

            # --- کاربر شناخته‌شده: مقایسه با اسنپ‌شات قبلی ------------------ #
            old_limit = snap["data_limit"]
            old_used = int(snap["used_traffic"] or 0)
            old_admin = snap["admin_id"]
            new_limit = u.data_limit
            new_used = int(u.used_traffic or 0)

            limit_changed = (old_limit or 0) != (new_limit or 0)
            drop = old_used - new_used
            reset_detected = drop > self.cfg.reset_drop_tolerance

            billed = 0

            if reset_detected:
                # حجم پلنِ فعلی دوباره حساب می‌شود (تمدید/فروش مجدد).
                # اگر هم‌زمان حجم هم عوض شده، kind می‌شود renewal و فقط یک بار حساب می‌شود.
                kind = "renewal" if limit_changed else "reset"
                count_bytes = (new_limit or 0)
                reset_count = int(snap["reset_count"] or 0) + 1
                added = self.ledger.add_event(
                    kind=kind,
                    day=day,
                    ts=now,
                    admin_id=u.admin_id,
                    admin_name=a_name,
                    user_id=u.user_id,
                    username=u.username,
                    bytes_=count_bytes,
                    used_traffic=new_used,
                    data_limit=new_limit,
                    dedupe_key=f"{kind}:{u.user_id}:{reset_count}:{int(now.timestamp() // 5)}",
                    note=f"مصرف از {human_bytes(old_used)} به {human_bytes(new_used)} افت کرد",
                )
                if added:
                    if kind == "renewal":
                        res.renewals += 1
                    else:
                        res.resets += 1
                    billed += count_bytes

            elif limit_changed and self.cfg.count_topup:
                delta = (new_limit or 0) - (old_limit or 0)
                if delta > 0:
                    added = self.ledger.add_event(
                        kind="topup",
                        day=day,
                        ts=now,
                        admin_id=u.admin_id,
                        admin_name=a_name,
                        user_id=u.user_id,
                        username=u.username,
                        bytes_=delta,
                        used_traffic=new_used,
                        data_limit=new_limit,
                        dedupe_key=f"topup:{u.user_id}:{old_limit or 0}->{new_limit or 0}:{int(now.timestamp() // 5)}",
                        note=f"{human_bytes(old_limit)} → {human_bytes(new_limit)}",
                    )
                    if added:
                        res.topups += 1
                        billed += delta
                else:
                    added = self.ledger.add_event(
                        kind="limit_cut",
                        day=day,
                        ts=now,
                        admin_id=u.admin_id,
                        admin_name=a_name,
                        user_id=u.user_id,
                        username=u.username,
                        bytes_=0,
                        used_traffic=new_used,
                        data_limit=new_limit,
                        dedupe_key=f"cut:{u.user_id}:{old_limit or 0}->{new_limit or 0}:{int(now.timestamp() // 5)}",
                        note=f"{human_bytes(old_limit)} → {human_bytes(new_limit)}",
                    )
                    if added:
                        res.cuts += 1

            # تغییر مالکیت ادمین — برای شفافیت ثبت می‌شود
            if old_admin != u.admin_id:
                self.ledger.add_event(
                    kind="limit_cut",  # بدون بار مالی، فقط ثبت
                    day=day,
                    ts=now,
                    admin_id=u.admin_id,
                    admin_name=a_name,
                    user_id=u.user_id,
                    username=u.username,
                    bytes_=0,
                    used_traffic=new_used,
                    data_limit=new_limit,
                    dedupe_key=f"owner:{u.user_id}:{old_admin}->{u.admin_id}:{int(now.timestamp() // 5)}",
                    note=f"مالکیت از {admin_name_of(old_admin)} به {a_name} منتقل شد",
                )

            res.bytes_added += billed

            self.ledger.upsert_snapshot(
                user_id=u.user_id, username=u.username, admin_id=u.admin_id,
                admin_name=a_name, data_limit=new_limit,
                used_traffic=new_used, created_at=u.created_at, now=now,
                bump_reset=reset_detected,
            )

        # ۳) کاربرانی که از پنل ناپدید شده‌اند (حذف شده‌اند)
        for snap in self.ledger.all_snapshots():
            uid = snap["user_id"]
            if uid in seen_ids:
                continue
            removed = self.ledger.remove_snapshot(uid)
            if removed is None:
                continue
            used = int(removed["used_traffic"] or 0)
            limit = removed["data_limit"]
            self.ledger.add_event(
                kind="deleted",
                day=self.local_day(now),
                ts=now,
                admin_id=removed["admin_id"],
                admin_name=removed["admin_name"] or admin_name_of(removed["admin_id"]),
                user_id=uid,
                username=removed["username"] or f"#{uid}",
                bytes_=0,  # قبلاً در provision حساب شده
                used_traffic=used,
                data_limit=limit,
                dedupe_key=f"del:{uid}:{int(now.timestamp() // 5)}",
                note=f"آخرین مصرف {human_bytes(used)} از {human_bytes(limit)}",
            )
            res.deleted += 1

        self.ledger.set_meta("last_scan_ts", now.astimezone(timezone.utc).isoformat())
        if first_run:
            self.ledger.set_meta("initialized", "1")
            self.ledger.set_meta("initialized_at", now.astimezone(timezone.utc).isoformat())
        return res


# --------------------------------------------------------------------------- #
# متن گزارش
# --------------------------------------------------------------------------- #


def _n(cfg: Config, v: Any) -> str:
    return fa(v) if cfg.persian_digits else str(v)


def _fmt_bytes(cfg: Config, n: int | None) -> str:
    return human_bytes(n)


def build_day_report(
    cfg: Config,
    ledger: Ledger,
    day: str,
    *,
    title_prefix: str = "📊 گزارش حجم ادمین‌ها",
    extra_footer: str = "",
) -> list[str]:
    """
    گزارش یک روز را می‌سازد و به صورت لیستی از پیام‌ها برمی‌گرداند
    (به‌خاطر محدودیت ۴۰۹۶ کاراکتری تلگرام).
    """
    summaries = ledger.summary_by_admin(day)
    rows = ledger.day_rows(day)
    totals = ledger.totals_between(day, day)

    if not rows:
        return [
            f"{title_prefix} — {day}\n\n"
            f"هیچ رویدادی در این روز ثبت نشده است.\n"
            f"(بات از {ledger.get_meta('initialized_at') or '—'} فعال است)"
        ]

    # دسته‌بندی رویدادها بر اساس ادمین
    by_admin: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        by_admin.setdefault(f"{r['admin_id']}|{r['admin_name']}", []).append(r)

    ordered_keys = sorted(
        by_admin.keys(),
        key=lambda k: next(
            (s["total_bytes"] for s in summaries if f"{s['admin_id']}|{s['admin_name']}" == k),
            0,
        ),
        reverse=True,
    )

    header = (
        f"{title_prefix}\n📅 {day}\n"
        f"──────────────\n"
        f"👥 ادمین‌های فعال: {_n(cfg, len(by_admin))}   |   "
        f"📦 مجموع: {_fmt_bytes(cfg, totals['total_bytes'])}\n"
        f"🆕 اکانت جدید: {_n(cfg, totals['accounts'])}\n"
    )

    chunks: list[str] = [header]
    current = ""

    def flush() -> None:
        nonlocal current
        if current:
            chunks.append(current)
            current = ""

    for key in ordered_keys:
        admin_events = by_admin[key]
        summ = next(
            (s for s in summaries if f"{s['admin_id']}|{s['admin_name']}" == key),
            None,
        )
        admin_name = admin_events[0]["admin_name"] or "بدون ادمین"
        total_bytes = summ["total_bytes"] if summ else 0

        block = [
            f"👤 {admin_name}",
            f"   📦 {_fmt_bytes(cfg, total_bytes)}"
            f"   |   🆕 {_n(cfg, summ['provision_n'] if summ else 0)} اکانت"
            f"   |   🔁 {_n(cfg, (summ['reset_n'] or 0) if summ else 0)} ریست",
        ]

        if summ and (summ["topup_n"] or 0):
            block.append(
                f"   ➕ افزودن حجم: {_n(cfg, summ['topup_n'])} مورد"
                f" ({_fmt_bytes(cfg, summ['topup_bytes'])})"
            )
        if summ and (summ["deleted_n"] or 0):
            block.append(f"   🗑 حذف‌شده: {_n(cfg, summ['deleted_n'])} اکانت")
        if summ and (summ["cut_n"] or 0):
            block.append(f"   ⚠️ کاهش حجم/تغییر مالک: {_n(cfg, summ['cut_n'])} مورد")

        if cfg.report_show_user_list:
            lines: list[str] = []
            billed_events = [e for e in admin_events if e["kind"] in BILLED_KINDS]
            for ev in billed_events[: cfg.max_users_in_report]:
                tag = ""
                if ev["kind"] in ("reset", "renewal"):
                    tag = " 🔁"
                elif ev["kind"] == "topup":
                    tag = " ➕"
                limit_txt = _fmt_bytes(cfg, ev["data_limit"])
                used_txt = _fmt_bytes(cfg, ev["used_traffic"])
                line = f"   ├ {ev['username']} — {limit_txt}"
                if ev["kind"] in ("reset", "renewal"):
                    line += f" (مصرف قبل: {used_txt})"
                elif ev["kind"] == "topup":
                    line += f" ({ev['note']})"
                line += tag
                lines.append(line)
            if len(billed_events) > cfg.max_users_in_report:
                rest = billed_events[cfg.max_users_in_report:]
                rest_bytes = sum(int(r["bytes"]) for r in rest)
                lines.append(
                    f"   └ … و {_n(cfg, len(rest))} مورد دیگر"
                    f" ({_fmt_bytes(cfg, rest_bytes)})"
                )
            block.extend(lines)

        block.append("")  # فاصله
        text_block = "\n".join(block)

        if len(current) + len(text_block) + 2 > 3800:
            flush()
        current += ("\n" if current else "") + text_block

    flush()

    # پاورقی خلاصه
    reset_bytes = sum(int(r["bytes"]) for r in rows if r["kind"] in ("reset", "renewal"))
    provision_bytes = sum(int(r["bytes"]) for r in rows if r["kind"] == "provision")
    topup_bytes = sum(int(r["bytes"]) for r in rows if r["kind"] == "topup")
    deleted_n = sum(1 for r in rows if r["kind"] == "deleted")

    footer = (
        "──────────────\n"
        f"🆕 ساخت اکانت: {_fmt_bytes(cfg, provision_bytes)}\n"
        f"🔁 ریست/تمدید: {_fmt_bytes(cfg, reset_bytes)}\n"
        f"➕ افزودن حجم: {_fmt_bytes(cfg, topup_bytes)}\n"
        f"💰 جمع بیل‌شده: {_fmt_bytes(cfg, totals['total_bytes'])}\n"
        f"🗑 اکانت‌های حذف‌شده: {_n(cfg, deleted_n)}"
    )
    if extra_footer:
        footer += "\n" + extra_footer
    if len(chunks) == 1:
        chunks[0] += "\n" + footer
    else:
        chunks.append(footer)
    return chunks


def build_status_report(cfg: Config, ledger: Ledger) -> str:
    day = datetime.now(cfg.tz).strftime("%Y-%m-%d")
    totals = ledger.totals_between(day, day)
    last_scan = ledger.get_meta("last_scan_ts") or "—"
    return (
        "🟢 وضعیت بات حسابداری\n"
        f"نسخه: {VERSION}\n"
        f"آخرین اسکن: {last_scan}\n"
        f"فاصله‌ی اسکن: {_n(cfg, cfg.scan_interval)} ثانیه\n"
        f"امروز ({day}):\n"
        f"   📦 {_fmt_bytes(cfg, totals['total_bytes'])}\n"
        f"   🆕 {_n(cfg, totals['accounts'])} اکانت"
    )


# --------------------------------------------------------------------------- #
# تلگرام
# --------------------------------------------------------------------------- #


class TelegramBot:
    API = "https://api.telegram.org"

    def __init__(self, cfg: Config, accountant: Accountant, ledger: Ledger):
        self.cfg = cfg
        self.acc = accountant
        self.ledger = ledger
        self.session = requests.Session()
        self.offset = 0
        self._stop = threading.Event()

    # -- ارسال ------------------------------------------------------------ #

    def send(self, chat_id: int, text: str) -> None:
        for chunk in _split_message(text, 4000):
            self._send_chunk(chat_id, chunk)

    def _send_chunk(self, chat_id: int, chunk: str) -> None:
        if self.cfg.dry_run_telegram:
            log.info("[dry-run] پیام به %s:\n%s", chat_id, chunk)
            return
        for attempt in range(4):
            try:
                resp = self.session.post(
                    f"{self.API}/bot{self.cfg.bot_token}/sendMessage",
                    json={"chat_id": chat_id, "text": chunk},
                    timeout=30,
                )
                if resp.status_code == 200:
                    return
                if resp.status_code == 429:
                    retry = resp.json().get("parameters", {}).get("retry_after", 3)
                    log.warning("محدودیت نرخ تلگرام، %s ثانیه صبر", retry)
                    time.sleep(float(retry) + 0.5)
                    continue
                log.error("تلگرام %s: %s", resp.status_code, resp.text[:300])
                return
            except Exception as exc:  # noqa: BLE001
                log.warning("خطای ارسال (تلاش %s): %s", attempt + 1, exc)
                time.sleep(2 ** attempt)

    def broadcast(self, text: str) -> None:
        for chat_id in self.cfg.allowed_chat_ids:
            self.send(chat_id, text)

    # -- دریافت ----------------------------------------------------------- #

    def _get_updates(self) -> list[dict[str, Any]]:
        try:
            resp = self.session.get(
                f"{self.API}/bot{self.cfg.bot_token}/getUpdates",
                params={"offset": self.offset, "timeout": 30, "allowed_updates": '["message"]'},
                timeout=40,
            )
            if resp.status_code != 200:
                log.error("getUpdates %s: %s", resp.status_code, resp.text[:200])
                time.sleep(5)
                return []
            return resp.json().get("result", []) or []
        except Exception as exc:  # noqa: BLE001
            log.warning("خطای getUpdates: %s", exc)
            time.sleep(5)
            return []

    def run(self) -> None:
        log.info("حلقه‌ی تلگرام شروع شد")
        while not self._stop.is_set():
            for upd in self._get_updates():
                self.offset = max(self.offset, int(upd["update_id"]) + 1)
                try:
                    self.handle_update(upd)
                except Exception:  # noqa: BLE001
                    log.exception("خطا در پردازش آپدیت")

    def stop(self) -> None:
        self._stop.set()

    # -- پردازش پیام ------------------------------------------------------ #

    def handle_update(self, upd: dict[str, Any]) -> None:
        msg = upd.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        if chat_id is None:
            return
        if self.cfg.allowed_chat_ids and int(chat_id) not in self.cfg.allowed_chat_ids:
            log.warning("پیام از چت غیرمجاز %s نادیده گرفته شد", chat_id)
            return
        raw = (msg.get("text") or "").strip()
        if not raw:
            return
        reply = self.dispatch(raw, now=self.acc.now())
        if reply:
            for line in reply:
                self.send(int(chat_id), line)

    # -- دستورات ---------------------------------------------------------- #

    def dispatch(self, raw: str, *, now: datetime | None = None) -> list[str]:
        """تفسیر دستور و تولید پاسخ. مستقل از شبکه است تا قابل تست باشد."""
        now = now or self.acc.now()
        parts = raw.split()
        cmd = parts[0].lower().split("@")[0]
        args = parts[1:]

        if cmd in ("/start", "/help", "راهنما"):
            return [self.help_text()]

        if cmd in ("/ping", "پینگ"):
            return ["pong ✅"]

        if cmd in ("/status", "/scan", "/now", "/today", "امروز", "الان"):
            if cmd == "/scan":
                res = self.acc.scan(now=now)
                return [
                    "🔄 اسکن انجام شد\n"
                    f"🆕 {_n(self.cfg, res.provisioned)} ساخت | "
                    f"🔁 {_n(self.cfg, res.resets + res.renewals)} ریست | "
                    f"➕ {_n(self.cfg, res.topups)} افزودن | "
                    f"🗑 {_n(self.cfg, res.deleted)} حذف\n"
                    f"📦 {_fmt_bytes(self.cfg, res.bytes_added)}",
                    build_status_report(self.cfg, self.ledger),
                ]
            return [build_status_report(self.cfg, self.ledger)]

        if cmd in ("/report", "گزارش", "/day"):
            day = args[0] if args else self._default_report_day(now)
            if args and args[0].lower() in ("today", "امروز"):
                day = now.strftime("%Y-%m-%d")
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                return ["فرمت تاریخ نامعتبر است. مثال: /report 2026-09-20"]
            return build_day_report(self.cfg, self.ledger, day)

        if cmd in ("/week", "/range", "هفته"):
            days = 7
            if args and args[0].isdigit():
                days = min(max(int(args[0]), 1), 90)
            end = now.strftime("%Y-%m-%d")
            start = (now - timedelta(days=days - 1)).strftime("%Y-%m-%d")
            return self._range_report(start, end)

        if cmd in ("/admin", "ادمین"):
            if not args:
                return ["نام ادمین را بنویسید. مثال: /admin ali"]
            return self._admin_report(" ".join(args))

        if cmd in ("/admins", "ادمین‌ها", "ادمینها"):
            try:
                admins = self.acc.panel.admins()
            except Exception as exc:  # noqa: BLE001
                return [f"خطا در خواندن پنل: {exc}"]
            if not admins:
                return ["ادمینی در پنل یافت نشد."]
            lines = ["👥 ادمین‌های پنل:"]
            for a in sorted(admins.values(), key=lambda x: x.username.lower()):
                lines.append(f"   • {a.username}  (id={_n(self.cfg, a.admin_id)})")
            return ["\n".join(lines)]

        if cmd in ("/user", "کاربر"):
            if not args:
                return ["یوزرنیم را بنویسید. مثال: /user test1"]
            return self._user_report(args[0])

        return ["دستور ناشناخته. برای دیدن دستورات /help را بفرستید."]

    def help_text(self) -> str:
        n = _n
        return (
            "🤖 ربات حسابداری حجم ادمین‌ها (PasarGuard)\n"
            "──────────────\n"
            "/today یا /status — وضعیت امروز و آخرین اسکن\n"
            "/report — گزارش روزانه (پیش‌فرض: روزِ گزارش‌شده)\n"
            "/report 2026-09-20 — گزارش یک روز خاص\n"
            "/week [تعداد روز] — جمع چند روز اخیر\n"
            "/admin نام‌ادمین — جزئیات یک ادمین امروز\n"
            "/admins — فهرست ادمین‌های پنل\n"
            "/user یوزرنیم — تاریخچه‌ی یک اکانت\n"
            "/scan — اسکن فوری دیتابیس\n"
            "──────────────\n"
            f"📅 گزارش خودکار هر روز ساعت {self.cfg.report_time} ({self.cfg.timezone})\n"
            f"⏱ فاصله‌ی اسکن: {n(self.cfg, self.cfg.scan_interval)} ثانیه"
        )

    # -- گزارش‌های کمکی ---------------------------------------------------- #

    def _default_report_day(self, now: datetime) -> str:
        if self.cfg.report_previous_day:
            return (now - timedelta(days=1)).strftime("%Y-%m-%d")
        return now.strftime("%Y-%m-%d")

    def _range_report(self, start: str, end: str) -> list[str]:
        summaries = self.ledger.events_between(start, end)
        totals = self.ledger.totals_between(start, end)
        if not summaries:
            return [f"در بازه‌ی {start} تا {end} رویدادی ثبت نشده است."]
        per_admin: dict[tuple[int | None, str], dict[str, int]] = {}
        for r in summaries:
            key = (r["admin_id"], r["admin_name"] or "بدون ادمین")
            slot = per_admin.setdefault(
                key, {"bytes": 0, "prov": 0, "reset": 0, "topup": 0, "del": 0}
            )
            if r["kind"] in BILLED_KINDS:
                slot["bytes"] += int(r["bytes"])
            if r["kind"] == "provision":
                slot["prov"] += 1
            if r["kind"] in ("reset", "renewal"):
                slot["reset"] += 1
            if r["kind"] == "topup":
                slot["topup"] += 1
            if r["kind"] == "deleted":
                slot["del"] += 1

        lines = [
            f"📊 جمع بازه‌ی {start} تا {end}",
            f"💰 مجموع بیل‌شده: {_fmt_bytes(self.cfg, totals['total_bytes'])}",
            f"🆕 اکانت جدید: {_n(self.cfg, totals['accounts'])}",
            "──────────────",
        ]
        for (admin_id, name), slot in sorted(
            per_admin.items(), key=lambda kv: kv[1]["bytes"], reverse=True
        ):
            lines.append(
                f"👤 {name}\n"
                f"   📦 {_fmt_bytes(self.cfg, slot['bytes'])} | "
                f"🆕 {_n(self.cfg, slot['prov'])} | "
                f"🔁 {_n(self.cfg, slot['reset'])} | "
                f"➕ {_n(self.cfg, slot['topup'])} | "
                f"🗑 {_n(self.cfg, slot['del'])}"
            )
        return ["\n".join(lines)]

    def _admin_report(self, name: str) -> list[str]:
        day = self.acc.now().strftime("%Y-%m-%d")
        rows = self.ledger.day_rows(day)
        matches = [r for r in rows if (r["admin_name"] or "").lower() == name.lower()]
        if not matches:
            matches = [r for r in rows if name.lower() in (r["admin_name"] or "").lower()]
        if not matches:
            return [f"رویدادی برای «{name}» در {day} ثبت نشده است."]
        total = sum(int(r["bytes"]) for r in matches if r["kind"] in BILLED_KINDS)
        lines = [f"👤 {name} — {day}", f"💰 مجموع: {_fmt_bytes(self.cfg, total)}", "──────────────"]
        for ev in matches:
            if ev["kind"] in BILLED_KINDS:
                lines.append(
                    f"• {ev['username']} — {_fmt_bytes(self.cfg, ev['data_limit'])}"
                    f"  [{KIND_LABEL_FA.get(ev['kind'], ev['kind'])}]"
                )
        return ["\n".join(lines)]

    def _user_report(self, username: str) -> list[str]:
        events = self.ledger.user_events(username)
        if not events:
            return [f"رویدادی برای «{username}» ثبت نشده است."]
        lines = [f"🧾 تاریخچه‌ی {username}"]
        for ev in events:
            ts = parse_ts(ev["ts"])
            ts_txt = ts.astimezone(self.cfg.tz).strftime("%Y-%m-%d %H:%M") if ts else "—"
            lines.append(
                f"{ts_txt} — {KIND_LABEL_FA.get(ev['kind'], ev['kind'])}"
                f" | {_fmt_bytes(self.cfg, ev['bytes'])}"
                + (f" | {ev['note']}" if ev["note"] else "")
            )
        return ["\n".join(lines)]


def _split_message(text: str, limit: int = 4000) -> list[str]:
    """
    متن را به تکه‌هایی کوچک‌تر از `limit` تقسیم می‌کند.

    اول سعی می‌کند در مرز خط‌ها بشکند؛ اگر یک خط به‌تنهایی از حد بلندتر
    باشد، آن خط را هم به‌صورت سخت تقسیم می‌کند (وگرنه تلگرام پیام را رد
    می‌کند چون سقفش ۴۰۹۶ کاراکتر است).
    """
    if len(text) <= limit:
        return [text]

    pieces = text.split("\n")
    out: list[str] = []
    buf = ""

    for idx, raw_line in enumerate(pieces):
        # آخرین قطعه نباید \n انتهایی بگیرد
        line = raw_line + ("\n" if idx < len(pieces) - 1 else "")

        # خط‌های خیلی بلند را سخت تقسیم کن
        while len(line) > limit:
            head, line = line[:limit], line[limit:]
            if buf.strip():
                out.append(buf.rstrip("\n"))
            buf = ""
            out.append(head)

        if len(buf) + len(line) > limit:
            out.append(buf.rstrip("\n"))
            buf = ""
        buf += line

    if buf.strip():
        out.append(buf.rstrip("\n"))
    return [c for c in out if c]



# --------------------------------------------------------------------------- #
# زمان‌بند گزارش روزانه
# --------------------------------------------------------------------------- #


def parse_hhmm(value: str) -> tuple[int, int]:
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", value or "")
    if not m:
        raise ValueError(f"ساعت نامعتبر: {value!r}")
    hh, mm = int(m.group(1)), int(m.group(2))
    if hh > 23 or mm > 59:
        raise ValueError(f"ساعت نامعتبر: {value!r}")
    return hh, mm


def seconds_until_next_report(cfg: Config, now: datetime) -> tuple[float, datetime]:
    """فاصله تا زمان بعدی گزارش و زمان آن را برمی‌گرداند."""
    hh, mm = parse_hhmm(cfg.report_time)
    target = now.astimezone(cfg.tz).replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= now.astimezone(cfg.tz):
        target = target + timedelta(days=1)
    return (target - now.astimezone(cfg.tz)).total_seconds(), target


# --------------------------------------------------------------------------- #
# اجرای اصلی
# --------------------------------------------------------------------------- #


def build_accountant(cfg: Config) -> tuple[Accountant, PanelDB, Ledger]:
    if not cfg.panel_db_url:
        raise SystemExit(
            "آدرس دیتابیس پنل پیدا نشد. آن را در config.ini (panel_db_url) یا "
            "متغیر PANEL_DB_URL تنظیم کنید، یا مطمئن شوید مسیر panel_env_file درست است."
        )
    if not cfg.bot_token and not cfg.dry_run_telegram:
        raise SystemExit("توکن تلگرام تنظیم نشده است (bot_token).")
    panel = PanelDB(cfg.panel_db_url)
    ledger = Ledger(cfg.ledger_path)
    return Accountant(cfg, panel, ledger), panel, ledger


REQUIRED_COLUMNS = {
    "admins": ["id", "username"],
    "users": ["id", "username", "admin_id", "data_limit", "used_traffic", "created_at"],
    "user_usage_logs": ["user_id", "used_traffic_at_reset", "reset_at"],
}


def checkdb(panel: PanelDB, cfg: Config) -> int:
    """
    اعتبارسنجی کامل دیتابیس پنل.

    روی سرور واقعی اجرا می‌شود تا مطمئن شویم اسکیمای پنل با کوئری‌های بات
    سازگار است (مخصوصاً برای PostgreSQL/TimescaleDB که این‌جا تست نشده).
    """
    problems: list[str] = []
    print(f"آدرس دیتابیس : {cfg.panel_db_url.split('@')[-1]}")
    try:
        panel.healthcheck()
        print("اتصال        : ✅ برقرار")
    except Exception as exc:  # noqa: BLE001
        print(f"اتصال        : ❌ ناموفق — {exc}")
        return 1

    print(f"نوع دیتابیس  : {panel.db_kind()}")

    schema = panel.inspect_schema()
    print("\n-- ستون‌های مورد نیاز --")
    for table, cols in REQUIRED_COLUMNS.items():
        found = schema.get(table, [])
        if not found:
            print(f"  {table:18} ❌ جدول وجود ندارد")
            problems.append(f"جدول {table} پیدا نشد")
            continue
        missing = [c for c in cols if c not in found]
        if missing:
            print(f"  {table:18} ⚠️  ستون‌های غایب: {', '.join(missing)}")
            problems.append(f"{table}: ستون‌های {', '.join(missing)} غایب‌اند")
        else:
            print(f"  {table:18} ✅ {len(found)} ستون، همه‌ی موارد لازم موجود")

    print("\n-- اجرای کوئری‌های واقعی --")
    try:
        admins = panel.admins()
        print(f"  admins()                ✅ {len(admins)} ادمین")
    except Exception as exc:  # noqa: BLE001
        print(f"  admins()                ❌ {exc}")
        problems.append("کوئری admins ناموفق")
        admins = {}

    try:
        users = panel.users()
        print(f"  users()                 ✅ {len(users)} کاربر")
        limited = sum(1 for u in users if u.data_limit)
        unlimited = sum(1 for u in users if not u.data_limit)
        print(f"      محدود: {limited}   نامحدود: {unlimited}")
        if users:
            sample = users[0]
            print(f"      نمونه: {sample.username!r} limit={sample.data_limit} "
                  f"used={sample.used_traffic} admin_id={sample.admin_id}")
            if sample.created_at is None:
                print("      ⚠️  created_at این کاربر خالی بود")
    except Exception as exc:  # noqa: BLE001
        print(f"  users()                 ❌ {exc}")
        problems.append("کوئری users ناموفق")
        users = []

    try:
        logs = panel.usage_reset_logs_since(
            datetime.now(timezone.utc) - timedelta(days=30), strict=True
        )
        print(f"  usage_reset_logs_since  ✅ {len(logs)} ریست در ۳۰ روز اخیر")
    except Exception as exc:  # noqa: BLE001
        print(f"  usage_reset_logs_since  ❌ {exc}")
        problems.append("کوئری user_usage_logs ناموفق")

    if not admins and not users:
        problems.append("هیچ ادمین یا کاربری خوانده نشد — دیتابیس خالی یا اشتباه است")

    print()
    if problems:
        print("❌ مشکلات یافت‌شده:")
        for p in problems:
            print(f"   • {p}")
        return 1
    print("✅ دیتابیس پنل با بات سازگار است.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ربات حسابداری حجم ادمین‌های PasarGuard")
    parser.add_argument("-c", "--config", help="مسیر فایل config.ini")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {VERSION}")
    sub = parser.add_subparsers(dest="cmd")

    p_scan = sub.add_parser("scan", help="یک اسکن انجام بده و خلاصه را چاپ کن")
    p_scan.add_argument("--telegram", action="store_true", help="نتیجه را در تلگرام هم بفرست")

    p_report = sub.add_parser("report", help="گزارش یک روز را چاپ/ارسال کن")
    p_report.add_argument("day", nargs="?", help="YYYY-MM-DD")
    p_report.add_argument("--send", action="store_true", help="ارسال به تلگرام")

    sub.add_parser("daily", help="گزارش روزانه را همین حالا تولید و ارسال کن")
    sub.add_parser("status", help="وضعیت را چاپ کن")
    sub.add_parser("health", help="اتصال به دیتابیس پنل را بررسی کن")
    sub.add_parser("checkdb", help="اسکیمای دیتابیس پنل را کامل اعتبارسنجی کن")

    p_run = sub.add_parser("run", help="اجرای دائمی (اسکن + تلگرام + گزارش خودکار)")
    p_run.add_argument("--no-telegram", action="store_true", help="فقط اسکن و گزارش، بدون تلگرام")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    cfg = load_config(args.config)
    accountant, panel, ledger = build_accountant(cfg)

    try:
        if args.cmd == "checkdb":
            return checkdb(panel, cfg)

        if args.cmd == "health":
            print("panel db :", cfg.panel_db_url.split("@")[-1])
            print("health   :", panel.healthcheck())
            print("admins   :", len(panel.admins()))
            print("users    :", len(panel.users()))
            print("ledger   :", cfg.ledger_path)
            return 0

        if args.cmd == "scan":
            res = accountant.scan()
            print(
                f"provisioned={res.provisioned} backfilled={res.backfilled} "
                f"resets={res.resets} renewals={res.renewals} topups={res.topups} "
                f"cuts={res.cuts} deleted={res.deleted} bytes={res.bytes_added}"
            )
            for e in res.errors:
                print("ERROR:", e)
            if args.telegram:
                bot = TelegramBot(cfg, accountant, ledger)
                day = accountant.now().strftime("%Y-%m-%d")
                bot.broadcast("\n".join(build_day_report(cfg, ledger, day, title_prefix="🔄 گزارش پس از اسکن")))
            return 1 if res.errors else 0

        if args.cmd == "report":
            day = args.day or (accountant.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            chunks = build_day_report(cfg, ledger, day)
            for c in chunks:
                print(c)
            if args.send:
                TelegramBot(cfg, accountant, ledger).broadcast("\n\n".join(chunks))
            return 0

        if args.cmd == "daily":
            day = (accountant.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            accountant.scan()
            chunks = build_day_report(cfg, ledger, day)
            for c in chunks:
                print(c)
            TelegramBot(cfg, accountant, ledger).broadcast("\n\n".join(chunks))
            return 0

        if args.cmd == "status":
            print(build_status_report(cfg, ledger))
            return 0

        # --- اجرای دائمی ------------------------------------------------- #
        run_forever(cfg, accountant, panel, ledger, with_telegram=not args.no_telegram)
        return 0
    finally:
        panel.dispose()
        ledger.close()


def run_forever(
    cfg: Config,
    accountant: Accountant,
    panel: PanelDB,
    ledger: Ledger,
    *,
    with_telegram: bool = True,
) -> None:
    stop = threading.Event()

    def _handle_sig(signum, frame):  # noqa: ANN001
        log.info("سیگنال %s دریافت شد، در حال خروج…", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _handle_sig)
    signal.signal(signal.SIGINT, _handle_sig)

    # بررسی فاصله‌ی خاموشی
    last = parse_ts(ledger.get_meta("last_scan_ts"))
    if last:
        gap = (datetime.now(timezone.utc) - last).total_seconds()
        if gap > cfg.heartbeat_gap_warn_seconds:
            warn = (
                f"⚠️ بات {int(gap // 60)} دقیقه خاموش بود.\n"
                "اکانت‌هایی که در این فاصله ساخته و حذف شده باشند ممکن است ثبت نشده باشند."
            )
            log.warning(warn.replace("\n", " "))
            ledger.set_meta("last_gap_warning", warn)

    bot = TelegramBot(cfg, accountant, ledger) if with_telegram else None
    tg_thread: threading.Thread | None = None
    if bot:
        tg_thread = threading.Thread(target=bot.run, name="telegram", daemon=True)
        tg_thread.start()

    log.info("شروع اسکن با فاصله‌ی %s ثانیه", cfg.scan_interval)

    # نصب کاملاً تازه؟ در این حالت گزارشِ عقب‌افتاده را «نفرست» — وگرنه بلافاصله
    # پس از نصب یک گزارش (احتمالاً خالی) ارسال می‌شود که فقط نویز است.
    # از ساعت گزارشِ بعدی شروع می‌کنیم.
    fresh_install = ledger.get_meta("last_report_day") is None
    last_report_sent_for: str | None = ledger.get_meta("last_report_day")

    while not stop.is_set():
        loop_start = time.monotonic()
        try:
            res = accountant.scan()
            if res.errors:
                for e in res.errors:
                    log.error(e)
            elif res.total_events:
                log.info(
                    "اسکن: %s ساخت / %s ریست / %s افزودن / %s حذف / %s",
                    res.provisioned, res.resets + res.renewals, res.topups,
                    res.deleted, human_bytes(res.bytes_added),
                )
        except Exception:  # noqa: BLE001
            log.exception("خطای غیرمنتظره در اسکن")

        # ارسال گزارش روزانه سر ساعت
        now = accountant.now()
        day_now = now.strftime("%Y-%m-%d")
        hh, mm = parse_hhmm(cfg.report_time)
        due = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        report_day = (now - timedelta(days=1)).strftime("%Y-%m-%d") if cfg.report_previous_day else day_now

        marker = report_day if cfg.report_previous_day else day_now
        if fresh_install and now >= due:
            # نصب تازه: نشانگر را ست می‌کنیم ولی چیزی نمی‌فرستیم
            last_report_sent_for = marker
            ledger.set_meta("last_report_day", marker)
            fresh_install = False
            log.info("نصب تازه — گزارش خودکار از %s به بعد ارسال می‌شود", marker)
        elif now >= due and last_report_sent_for != marker:
            try:
                accountant.scan(now=now)
                chunks = build_day_report(cfg, ledger, report_day if cfg.report_previous_day else day_now)
                if bot:
                    for chat_id in cfg.allowed_chat_ids:
                        for c in chunks:
                            bot.send(chat_id, c)
                else:
                    for c in chunks:
                        log.info("گزارش:\n%s", c)
                last_report_sent_for = marker
                ledger.set_meta("last_report_day", marker)
                log.info("گزارش %s ارسال شد", marker)
            except Exception:  # noqa: BLE001
                log.exception("ارسال گزارش روزانه ناموفق بود")

        elapsed = time.monotonic() - loop_start
        stop.wait(max(1.0, cfg.scan_interval - elapsed))

    if bot:
        bot.stop()
    log.info("بات متوقف شد")


if __name__ == "__main__":
    raise SystemExit(main())
