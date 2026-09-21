#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# تست end-to-end: اجرای واقعی CLI بات روی یک دیتابیس SQLite با اسکیمای پنل.
# مسیر واقعیِ PanelDB + load_config + Ledger روی دیسک + main() را اجرا می‌کند.
# ---------------------------------------------------------------------------
set -uo pipefail
cd "$(dirname "$0")/.."

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

PANEL_DB="$WORK/panel.db"
LEDGER_DB="$WORK/ledger.db"
ENV_FILE="$WORK/panel.env"
CFG="$WORK/config.ini"
GB=1073741824

python3 - "$PANEL_DB" <<'PY'
import sqlite3, sys
from datetime import datetime, timezone
db = sqlite3.connect(sys.argv[1])
db.executescript(open('tests/schema_panel.sql').read())
now = datetime.now(timezone.utc).isoformat()
db.execute("INSERT INTO admins(id, username, hashed_password, created_at) VALUES(1,'ali','x',?)", (now,))
db.execute("INSERT INTO admins(id, username, hashed_password, created_at) VALUES(2,'reza','x',?)", (now,))
db.commit()
db.close()
PY

cat > "$ENV_FILE" <<EOF
# شبیه‌سازی .env پنل پاسارگارد (با فاصله و کوتیشن، مثل خروجی اسکریپت نصب)
SQLALCHEMY_DATABASE_URL = "sqlite+aiosqlite:///$PANEL_DB"
UVICORN_HOST = "0.0.0.0"
EOF

cat > "$CFG" <<EOF
[bot]
panel_env_file = $ENV_FILE
ledger_path = $LEDGER_DB
timezone = Asia/Tehran
report_time = 00:00
scan_interval = 60
dry_run_telegram = true
persian_digits = true
backfill_days = 30
EOF

PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); echo "  ✅ $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  ❌ $1"; }
must() { if grep -qF -- "$2" <<<"$1"; then ok "$3"; else bad "$3 (انتظار: $2)"; echo "$1" | head -20; fi; }

echo "=== ۱) health: اتصال واقعی به دیتابیس پنل ==="
OUT=$(python3 bot.py -c "$CFG" health 2>&1)
must "$OUT" "health   : ok" "اتصال به دیتابیس برقرار است"
must "$OUT" "admins   : 2" "دو ادمین خوانده شد"
must "$OUT" "users    : 0" "جدول users خالی خوانده شد"
must "$OUT" "sqlite:///$PANEL_DB" "درایور async به sync تبدیل شد"

echo
echo "=== ۲) ساخت ۲۰ اکانت توسط ادمین ali + ۲ اکانت توسط reza ==="
python3 - "$PANEL_DB" <<'PY'
import sqlite3, sys
from datetime import datetime, timezone
GB = 1024**3
db = sqlite3.connect(sys.argv[1])
now = datetime.now(timezone.utc)
def add(uid, name, aid, gb, used=0):
    db.execute("INSERT INTO users(id,username,status,used_traffic,data_limit,admin_id,created_at)"
               " VALUES(?,?, 'active',?,?,?,?)",
               (uid, name, used, gb*GB, aid, now.isoformat()))
for i in range(10): add(100+i, f"ali_100g_{i:02d}", 1, 100)
for i in range(10): add(110+i, f"ali_50g_{i:02d}",  1, 50)
add(201, "ali_temp_30g",   1, 30)
add(202, "ali_almost_full",1, 10)
add(203, "ali_reset_me",   1, 40)
add(301, "reza_big",       2, 200)
add(302, "reza_small",     2, 5)
db.commit(); db.close()
PY

OUT=$(python3 bot.py -c "$CFG" scan 2>&1)
must "$OUT" "provisioned=25" "۲۵ اکانت ثبت شد"
if grep -q "ERROR:" <<<"$OUT"; then bad "بدون خطا"; else ok "بدون خطا"; fi

echo
echo "=== ۳) گزارش: ادمین ali باید ۱۵۸۰ گیگ باشد ==="
TODAY=$(TZ=Asia/Tehran date +%F)
OUT=$(python3 bot.py -c "$CFG" report "$TODAY" 2>&1)
must "$OUT" "ali" "نام ادمین ali در گزارش"
must "$OUT" "1.54 TB" "جمع ali = ۱۵۸۰ گیگ (1.54 TB)"
must "$OUT" "ali_100g_00" "یوزرنیم‌ها در گزارش آمده"
must "$OUT" "205 GB" "جمع reza = ۲۰۵ گیگ"

echo
echo "=== ۴) حذف اکانت ۳۰ گیگی و اکانت ۹٫۹-از-۱۰ گیگی ==="
python3 - "$PANEL_DB" <<'PY'
import sqlite3, sys
GB = 1024**3
db = sqlite3.connect(sys.argv[1])
db.execute("UPDATE users SET used_traffic=? WHERE id=202", (int(9.9*GB),))
db.execute("DELETE FROM users WHERE id IN (201,202)")
db.commit(); db.close()
PY
OUT=$(python3 bot.py -c "$CFG" scan 2>&1)
must "$OUT" "deleted=2" "دو حذف شناسایی شد"

OUT=$(python3 bot.py -c "$CFG" report "$TODAY" 2>&1)
must "$OUT" "1.54 TB" "آمار ali بعد از حذف دست‌نخورده ماند"
must "$OUT" "🗑 حذف‌شده: ۲ اکانت" "تعداد حذف در گزارش"

echo
echo "=== ۵) ریست اکانت ۴۰ گیگی → باید ۴۰ گیگ دوباره بیل شود ==="
python3 - "$PANEL_DB" <<'PY'
import sqlite3, sys
from datetime import datetime, timezone
GB = 1024**3
db = sqlite3.connect(sys.argv[1])
db.execute("UPDATE users SET used_traffic=? WHERE id=203", (39*GB,))
db.commit(); db.close()
PY
python3 bot.py -c "$CFG" scan >/dev/null 2>&1
python3 - "$PANEL_DB" <<'PY'
import sqlite3, sys
from datetime import datetime, timezone
GB = 1024**3
db = sqlite3.connect(sys.argv[1])
db.execute("INSERT INTO user_usage_logs(user_id, used_traffic_at_reset, reset_at) VALUES(?,?,?)",
           (203, 39*GB, datetime.now(timezone.utc).isoformat()))
db.execute("UPDATE users SET used_traffic=0 WHERE id=203")
db.commit(); db.close()
PY
OUT=$(python3 bot.py -c "$CFG" scan 2>&1)
must "$OUT" "resets=1" "یک ریست شناسایی شد"

OUT=$(python3 bot.py -c "$CFG" report "$TODAY" 2>&1)
must "$OUT" "1.58 TB" "جمع نهایی ali = ۱۶۲۰ گیگ (1.58 TB)"
must "$OUT" "🔁 ریست/تمدید: 40 GB" "بخش ریست در پاورقی"

echo
echo "=== ۶) اجرای مجدد scan نباید چیزی را دوباره بیل کند ==="
OUT=$(python3 bot.py -c "$CFG" scan 2>&1)
must "$OUT" "provisioned=0 backfilled=0 resets=0 renewals=0 topups=0 cuts=0 deleted=0 bytes=0" "اسکن تکراری خالی است"

echo
echo "=== ۷) دفترکل روی دیسک باقی مانده و وضعیت درست است ==="
OUT=$(python3 bot.py -c "$CFG" status 2>&1)
must "$OUT" "🟢 وضعیت بات حسابداری" "دستور status کار می‌کند"
test -f "$LEDGER_DB" && ok "فایل ledger.db ساخته شد" || bad "فایل ledger.db ساخته نشد"

echo
echo "=== ۸) پایداری دفترکل بین اجراهای جداگانه ==="
# sqlite3 CLI ممکن است نصب نباشد؛ با خودِ ماژول sqlite3 پایتون می‌پرسیم.
q() { python3 - "$LEDGER_DB" "$1" <<'PY'
import sqlite3, sys
db = sqlite3.connect(sys.argv[1])
print(db.execute(sys.argv[2]).fetchone()[0])
PY
}
ROWS=$(q "SELECT COUNT(*) FROM events WHERE kind='provision'")
[ "$ROWS" = "25" ] && ok "۲۵ رویداد provision در دفترکل" || bad "تعداد provision: $ROWS (انتظار ۲۵)"
TOTAL=$(q "SELECT SUM(bytes) FROM events WHERE kind IN ('provision','reset','renewal','topup')")
WANT=$(( (1580 + 205 + 40) * GB ))
[ "$TOTAL" = "$WANT" ] && ok "جمع کل بیل‌شده درست است" || bad "جمع کل: $TOTAL (انتظار $WANT)"
DEL=$(q "SELECT COUNT(*) FROM events WHERE kind='deleted'")
[ "$DEL" = "2" ] && ok "۲ رویداد حذف در دفترکل" || bad "تعداد deleted: $DEL (انتظار ۲)"
SNAPS=$(q "SELECT COUNT(*) FROM snapshots")
[ "$SNAPS" = "23" ] && ok "۲۳ اسنپ‌شات زنده (۲۵ منهای ۲ حذف‌شده)" || bad "اسنپ‌شات‌ها: $SNAPS (انتظار ۲۳)"

echo
echo "=== ۹) حلقه‌ی اجرایی (run) روی نصب تازه ==="
# دفترکل را پاک کن تا شبیه نصب تازه شود، بعد دمون را چند ثانیه اجرا کن.
rm -f "$LEDGER_DB"
RUN_CFG="$WORK/run.ini"
sed -e "s#^scan_interval = .*#scan_interval = 2#" \
    -e "s#^ledger_path = .*#ledger_path = $WORK/run_ledger.db#" \
    "$CFG" > "$RUN_CFG"

RUNLOG="$WORK/run.log"
python3 bot.py -c "$RUN_CFG" run --no-telegram > "$RUNLOG" 2>&1 &
RUNPID=$!
sleep 8
kill -TERM "$RUNPID" 2>/dev/null
wait "$RUNPID" 2>/dev/null

must "$(cat "$RUNLOG")" "شروع اسکن با فاصله‌ی 2 ثانیه" "دمون بالا آمد"
# حلقه‌ی run با فرمت فارسی لاگ می‌زند (نه فرمت کلید=مقدارِ زیردستور scan)
must "$(cat "$RUNLOG")" "اسکن: 23 ساخت" "اسکن انجام شد و ۲۳ اکانت backfill شد"
# نصب تازه نباید گزارش خالی بفرستد
if grep -q "گزارش .* ارسال شد" "$RUNLOG"; then
    bad "نصب تازه گزارش خالی نفرستاد"
else
    ok "نصب تازه گزارش خالی نفرستاد"
fi
must "$(cat "$RUNLOG")" "نصب تازه" "نشانگر گزارش بدون ارسال ست شد"
must "$(cat "$RUNLOG")" "بات متوقف شد" "SIGTERM تمیز هندل شد"

echo
echo "=================================================================="
echo " نتیجه‌ی E2E: $PASS موفق / $FAIL ناموفق"
echo "=================================================================="
exit $([ "$FAIL" -eq 0 ] && echo 0 || echo 1)
