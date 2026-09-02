#!/bin/bash
# 台灣雙地天氣預報腳本
# 用於每日早報：台北 + 新竹竹東（ITRI）

set -euo pipefail
export TZ=Asia/Taipei

curl_weather() {
    curl -fsSL --connect-timeout 5 --max-time 15 "$1" 2>/dev/null || printf '%s' 'N/A'
}

TODAY=$(date +%Y-%m-%d)
WEEKDAY=$(date +%A)

# Resolve the Hermes release and Python binary the same way as other no-agent
# cron scripts, so this also works after the script is installed under
# $HERMES_HOME/scripts by the cron setup.
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
VENV_PY="${HERMES_PYTHON:-$HERMES_HOME/hermes-agent/venv/bin/python}"
RELEASE_PATH="@RELEASE_PATH@"
RELEASE_PYTHON="@PYTHON@"

if [[ ! -d "$RELEASE_PATH" ]]; then
    RELEASE_PATH="$HERMES_HOME/hermes-agent"
fi
export PYTHONPATH="$RELEASE_PATH${PYTHONPATH:+:$PYTHONPATH}"

if [[ "$RELEASE_PYTHON" != "@PYTHON@" && -x "$RELEASE_PYTHON" ]]; then
    VENV_PY="$RELEASE_PYTHON"
fi

if [[ ! -x "$VENV_PY" ]]; then
    VENV_PY="${PYTHON:-python3}"
fi

GOOGLE_API="$RELEASE_PATH/skills/productivity/google-workspace/scripts/google_api.py"

print_calendar_section() {
    local calendar_start calendar_end calendar_lines
    calendar_start="${TODAY}T00:00:00+08:00"
    calendar_end="${TODAY}T23:59:59+08:00"

    echo "## 📅 今日 ITRI 行事曆"
    echo ""

    if [[ ! -f "$GOOGLE_API" ]]; then
        echo "（行事曆暫時無法取得）"
        return 0
    fi

    # Run and parse the API through the resolved Hermes Python, containing all
    # failures, including a hung network request, to this optional section.
    if ! calendar_lines="$("$VENV_PY" - "$GOOGLE_API" "$calendar_start" "$calendar_end" <<'PY'
import json
import subprocess
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

api_path, start, end = sys.argv[1:]
try:
    result = subprocess.run(
        [sys.executable, api_path, "calendar", "list",
         "--calendar", "cwliao.itri@gmail.com", "--start", start, "--end", end],
        capture_output=True,
        check=True,
        text=True,
        timeout=20,
    )
except (OSError, subprocess.SubprocessError):
    raise SystemExit(1)

try:
    events = json.loads(result.stdout)
    if not isinstance(events, list):
        raise ValueError("calendar response is not a list")

    taipei = ZoneInfo("Asia/Taipei")

    def compact(value, default=""):
        return " ".join(str(value or default).split())

    def parse_timed_datetime(value):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timed event datetime is not timezone-aware")
        return parsed.astimezone(taipei)

    def sort_key(event):
        if not isinstance(event, dict):
            return (2, "")

        value = event.get("start") or ""
        if not isinstance(value, str):
            return (2, "")
        if "T" not in value:
            return (0, value)
        try:
            return (1, parse_timed_datetime(value))
        except (TypeError, ValueError, OverflowError):
            return (2, "")

    lines = []
    for event in sorted(events, key=sort_key):
        try:
            if not isinstance(event, dict):
                continue
            if event.get("status") == "cancelled":
                continue

            summary = compact(event.get("summary"), "（無標題）")
            event_start = compact(event.get("start"))
            event_end = compact(event.get("end"))
            location = compact(event.get("location"))
            if not event_start or not event_end:
                raise ValueError("calendar event has no start/end")

            if "T" not in event_start:
                line = f"- 全天：{summary}"
            else:
                start_dt = parse_timed_datetime(event_start)
                end_dt = parse_timed_datetime(event_end)
                line = f"- [{start_dt:%H:%M}-{end_dt:%H:%M}] {summary}"
            if location:
                line += f" @ {location}"
            lines.append(line)
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
            continue
    print("\n".join(lines))
except (json.JSONDecodeError, KeyError, TypeError, ValueError, OverflowError):
    raise SystemExit(1)
PY
)"; then
        echo "（行事曆暫時無法取得）"
        return 0
    fi

    if [[ -n "$calendar_lines" ]]; then
        printf '%s\n' "$calendar_lines"
    else
        echo "- 今日無排定行程"
    fi
}

# 獲取台北和竹東的天氣數據
TAIPEI_WEATHER=$(curl_weather "https://wttr.in/Taipei?format=%c:%t:%h:%w")
HSINCHU_WEATHER=$(curl_weather "https://wttr.in/Zhudong?format=%c:%t:%h:%w")

# 解析數據
TAIPEI_TEMP=$(printf '%s\n' "$TAIPEI_WEATHER" | cut -d: -f2)
HSINCHU_TEMP=$(printf '%s\n' "$HSINCHU_WEATHER" | cut -d: -f2)

# 判斷降雨情況（簡易邏輯）
RAINY="否"
if echo "$TAIPEI_WEATHER" | grep -q "🌧\|☁"; then
    RAINY="是，午後可能有雷雨"
fi

echo "=========================================="
echo "📅 ${WEEKDAY} (${TODAY}) 每日早報"
echo "=========================================="
echo ""

print_calendar_section
echo ""
echo "## 🌤️ 今日雙地天氣"
echo ""
echo "| 地區 | 天氣 | 溫度 | 降雨機率 | 穿搭建議 |"
echo "|------|------|------|----------|----------|"
echo "| 台北 | ${TAIPEI_WEATHER%%:*} | ${TAIPEI_TEMP:-N/A} | ${RAINY} | 薄長袖 + 備傘 |"
echo "| 新竹/竹東 (ITRI) | ${HSINCHU_WEATHER%%:*} | ${HSINCHU_TEMP:-N/A} | ${RAINY} | 輕薄外套 + 雨具備用 |"
echo ""
echo "=========================================="
