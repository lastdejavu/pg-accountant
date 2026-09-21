#!/usr/bin/env bash
# ===========================================================================
#  نصب ربات حسابداری حجم ادمین‌ها برای پنل PasarGuard
#
#  اجرا:
#      sudo bash install.sh
#      sudo bash install.sh --token "123:ABC" --chats "-100123,456"
#
#  این اسکریپت:
#    ۱. فایل‌ها را در /opt/pg-accountant می‌گذارد
#    ۲. محیط مجازی پایتون می‌سازد و وابستگی‌ها را نصب می‌کند
#    ۳. آدرس دیتابیس را از /opt/pasarguard/.env تشخیص می‌دهد و درایور
#       مناسب (psycopg2 / pymysql / sqlite) را نصب می‌کند
#    ۴. سرویس systemd می‌سازد و فعال می‌کند
# ===========================================================================
set -euo pipefail

APP="pg-accountant"
APP_DIR="/opt/$APP"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PANEL_ENV="/opt/pasarguard/.env"
PY="${PYTHON:-python3}"

BOT_TOKEN=""
CHAT_IDS=""
NO_START=0

green()  { printf '\033[32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
red()    { printf '\033[31m%s\033[0m\n' "$*"; }
step()   { printf '\n\033[36m▸ %s\033[0m\n' "$*"; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --token)      BOT_TOKEN="$2"; shift 2 ;;
        --chats)      CHAT_IDS="$2"; shift 2 ;;
        --panel-env)  PANEL_ENV="$2"; shift 2 ;;
        --no-start)   NO_START=1; shift ;;
        -h|--help)
            sed -n '2,20p' "$0"; exit 0 ;;
        *) red "آرگومان ناشناخته: $1"; exit 1 ;;
    esac
done

# --- ۰) بررسی روت -------------------------------------------------------- #
if [[ $EUID -ne 0 ]]; then
    red "این اسکریپت باید با sudo اجرا شود."
    exit 1
fi

if ! command -v "$PY" >/dev/null 2>&1; then
    red "پایتون ۳ پیدا نشد. اول نصبش کنید:  apt install -y python3 python3-venv"
    exit 1
fi

step "۱) کپی فایل‌ها به $APP_DIR"
mkdir -p "$APP_DIR/tests"
install -m 0755 "$SRC_DIR/bot.py"              "$APP_DIR/bot.py"
install -m 0644 "$SRC_DIR/requirements.txt"    "$APP_DIR/requirements.txt"
install -m 0644 "$SRC_DIR/config.example.ini"  "$APP_DIR/config.example.ini"
install -m 0644 "$SRC_DIR/tests/schema_panel.sql" "$APP_DIR/tests/schema_panel.sql" 2>/dev/null || true
install -m 0755 "$SRC_DIR/tests/test_accounting.py" "$APP_DIR/tests/test_accounting.py" 2>/dev/null || true
install -m 0755 "$SRC_DIR/tests/e2e.sh"        "$APP_DIR/tests/e2e.sh" 2>/dev/null || true
green "انجام شد."

# --- ۲) محیط مجازی ------------------------------------------------------- #
step "۲) ساخت محیط مجازی پایتون و نصب وابستگی‌ها"
if [[ ! -d "$APP_DIR/venv" ]]; then
    "$PY" -m venv "$APP_DIR/venv"
fi
VPY="$APP_DIR/venv/bin/python"
"$VPY" -m pip install --quiet --upgrade pip
"$VPY" -m pip install --quiet -r "$APP_DIR/requirements.txt"
green "وابستگی‌های پایه نصب شدند."

# --- ۳) تشخیص دیتابیس پنل ------------------------------------------------ #
step "۳) تشخیص دیتابیس پنل"
DB_URL=""
if [[ -f "$PANEL_ENV" ]]; then
    # فقط کوتیشن‌ها و CR/فاصله‌ی انتهایی را برمی‌داریم.
    # توجه: فاصله‌های «داخل» مقدار را دست نمی‌زنیم، چون پسورد دیتابیس
    # ممکن است فاصله داشته باشد و پاک کردنش اتصال را بی‌صدا می‌شکند.
    DB_URL="$(grep -E '^[[:space:]]*(export[[:space:]]+)?SQLALCHEMY_DATABASE_URL[[:space:]]*=' "$PANEL_ENV" \
              | tail -1 \
              | sed -E 's/^[^=]+=[[:space:]]*//' \
              | sed -e 's/\r$//' -e 's/[[:space:]]*$//' \
              | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'\$//")" || true
fi

DRIVER=""
if [[ -n "$DB_URL" ]]; then
    case "$DB_URL" in
        *postgresql*|*postgres*) DRIVER="psycopg2-binary"; yellow "دیتابیس: PostgreSQL/TimescaleDB" ;;
        *mysql*|*mariadb*)       DRIVER="pymysql[cryptography]"; yellow "دیتابیس: MySQL/MariaDB" ;;
        *sqlite*)                DRIVER=""; yellow "دیتابیس: SQLite" ;;
        *)                       yellow "نوع دیتابیس ناشناخته — درایور دستی نصب کنید" ;;
    esac
else
    red "نتوانستم SQLALCHEMY_DATABASE_URL را از $PANEL_ENV بخوانم."
    yellow "بعداً panel_db_url را دستی در $APP_DIR/config.ini تنظیم کنید."
fi

if [[ -n "$DRIVER" ]]; then
    if "$VPY" -m pip install --quiet "$DRIVER"; then
        green "درایور $DRIVER نصب شد."
    else
        red "نصب $DRIVER ناموفق بود."
    fi

    # بررسی کنیم درایور واقعاً import می‌شود — وگرنه بات با خطای گنگ بالا می‌آید
    IMPORT_MOD="psycopg2"
    [[ "$DRIVER" == pymysql* ]] && IMPORT_MOD="pymysql"
    if ! "$VPY" -c "import $IMPORT_MOD" 2>/dev/null; then
        yellow "درایور $IMPORT_MOD قابل import نیست. در حال تلاش با جایگزین…"
        if "$VPY" -m pip install --quiet "psycopg[binary]" && "$VPY" -c "import psycopg" 2>/dev/null; then
            green "درایور psycopg (نسخه‌ی ۳) نصب شد."
            yellow "در config.ini مقدار panel_db_url را دستی پر کن و به‌جای"
            yellow "  postgresql+psycopg2://  از  postgresql+psycopg://  استفاده کن."
        else
            red "نصب درایور ناموفق بود. احتمالاً ابزار ساخت لازم داری:"
            red "  Debian/Ubuntu:  apt install -y libpq-dev gcc python3-dev"
            red "  RHEL/Rocky:     dnf install -y libpq-devel gcc python3-devel"
        fi
    fi
fi

# SQLite پنل معمولاً در /var/lib/pasarguard/db.sqlite3 است.
# ما هیچ دسترسی‌ای روی فایل‌های پنل تغییر نمی‌دهیم — حتی chmod.
# فقط مالک فایل را گزارش می‌دهیم تا اگر لازم شد خودت تصمیم بگیری.
if [[ -n "$DB_URL" && "$DB_URL" == sqlite* ]]; then
    SQLITE_PATH="$(echo "$DB_URL" | sed -E 's#^sqlite(\+aiosqlite)?://##')"
    if [[ -f "$SQLITE_PATH" ]]; then
        OWNER="$(stat -c '%U' "$SQLITE_PATH" 2>/dev/null || echo '?')"
        green "دیتابیس SQLite پیدا شد: $SQLITE_PATH (مالک: $OWNER)"
        green "این نصب هیچ دسترسی‌ای روی فایل‌های پنل تغییر نمی‌دهد."
        yellow "اگر بعداً خطای «Permission denied» گرفتی، سرویس با روت اجرا می‌شود و"
        yellow "معمولاً مشکلی نیست؛ در غیر این صورت خودت دستی تصمیم بگیر."
    fi
fi

# --- ۴) گرفتن توکن و چت -------------------------------------------------- #
step "۴) تنظیمات تلگرام"
if [[ -z "$BOT_TOKEN" ]]; then
    read -r -p "توکن بات (از BotFather) را وارد کنید: " BOT_TOKEN
fi
if [[ -z "$BOT_TOKEN" ]]; then
    red "بدون توکن، بات کار نمی‌کند. بعداً در config.ini واردش کنید."
fi

if [[ -z "$CHAT_IDS" ]]; then
    echo "آیدی چت/گروهی که گزارش‌ها به آن برود (با کاما جدا کنید)."
    echo "خالی بگذارید = موقتاً همه می‌توانند استفاده کنند (بعداً محدودش کنید)."
    read -r -p "allowed_chat_ids: " CHAT_IDS
fi

if [[ -f "$APP_DIR/config.ini" ]]; then
    cp "$APP_DIR/config.ini" "$APP_DIR/config.ini.bak.$(date +%s)"
    yellow "config.ini قبلی پشتیبان‌گیری شد."
fi

cat > "$APP_DIR/config.ini" <<EOF
[bot]
bot_token = $BOT_TOKEN
allowed_chat_ids = $CHAT_IDS
panel_db_url =
panel_env_file = $PANEL_ENV
ledger_path = $APP_DIR/ledger.db
timezone = Asia/Tehran
report_time = 00:00
report_previous_day = true
scan_interval = 60
read_only = true
reset_drop_tolerance = 1048576
count_topup = true
backfill_on_first_run = true
backfill_days = 30
report_show_user_list = true
max_users_in_report = 60
persian_digits = true
heartbeat_gap_warn_seconds = 300
EOF
chmod 0600 "$APP_DIR/config.ini"
green "config.ini ساخته شد (دسترسی ۶۰۰ — فقط روت می‌خواندش)."

# --- ۵) تست اتصال -------------------------------------------------------- #
step "۵) بررسی اتصال به دیتابیس پنل"
if "$APP_DIR/venv/bin/python" "$APP_DIR/bot.py" -c "$APP_DIR/config.ini" health; then
    green "اتصال به دیتابیس برقرار است."
else
    red "اتصال به دیتابیس ناموفق بود. تنظیمات را بررسی کنید."
    yellow "ادامه می‌دهم تا سرویس ساخته شود، ولی باید درستش کنید."
fi

# --- ۶) سرویس systemd ---------------------------------------------------- #
step "۶) ساخت سرویس systemd"
cat > "/etc/systemd/system/$APP.service" <<EOF
[Unit]
Description=PasarGuard Admin Volume Accountant Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/venv/bin/python $APP_DIR/bot.py -c $APP_DIR/config.ini run
Restart=always
RestartSec=10
StandardOutput=append:/var/log/$APP.log
StandardError=append:/var/log/$APP.log
# سخت‌گیری امنیتی
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ReadWritePaths=$APP_DIR /var/log

[Install]
WantedBy=multi-user.target
EOF

touch "/var/log/$APP.log"
systemctl daemon-reload
systemctl enable "$APP.service" >/dev/null 2>&1
green "سرویس ساخته و فعال شد."

if [[ $NO_START -eq 0 ]]; then
    systemctl restart "$APP.service"
    sleep 3
    if systemctl is-active --quiet "$APP.service"; then
        green "سرویس در حال اجراست ✅"
    else
        red "سرویس بالا نیامد. لاگ را ببینید:"
        tail -n 25 "/var/log/$APP.log" || true
        exit 1
    fi
fi

# --- پایان --------------------------------------------------------------- #
cat <<DONE

$(green "نصب کامل شد ✅")

دستورهای مفید:
  sudo systemctl status  $APP          وضعیت سرویس
  sudo systemctl restart $APP          راه‌اندازی مجدد
  sudo journalctl -u $APP -f           دنبال کردن لاگ systemd
  sudo tail -f /var/log/$APP.log       لاگ برنامه

استفاده دستی:
  sudo $APP_DIR/venv/bin/python $APP_DIR/bot.py -c $APP_DIR/config.ini health
  sudo $APP_DIR/venv/bin/python $APP_DIR/bot.py -c $APP_DIR/config.ini scan
  sudo $APP_DIR/venv/bin/python $APP_DIR/bot.py -c $APP_DIR/config.ini report
  sudo $APP_DIR/venv/bin/python $APP_DIR/bot.py -c $APP_DIR/config.ini daily   # گزارش روز قبل + ارسال

تست‌ها:
  $APP_DIR/venv/bin/python $APP_DIR/tests/test_accounting.py
  bash $APP_DIR/tests/e2e.sh

نکته‌ی مهم:
  در اولین اجرا، اکانت‌های موجود در پنل هم ثبت (backfill) می‌شوند —
  فقط آن‌هایی که در $APP_DIR/config.ini (backfill_days) تعیین شده ساخته شده باشند.
  اگر نمی‌خواهید گذشته ثبت شود، قبل از اولین اجرا
  backfill_on_first_run = false  بگذارید.
DONE
