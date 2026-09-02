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
ENV_FILE="$HERMES_HOME/.env"
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

# no_agent cron subprocesses sanitize Hermes credentials before launching this
# script. Preserve a systemd EnvironmentFile-provided key when present, but
# fall back to the same $HERMES_HOME/.env used by the standalone cron scripts.
if [[ -z "${NOTION_API_KEY:-}" && -f "$ENV_FILE" ]]; then
    notion_api_key_from_file="$(sed -nE \
        's/^[[:space:]]*(export[[:space:]]+)?NOTION_API_KEY[[:space:]]*=[[:space:]]*//p' \
        "$ENV_FILE" | tail -1 || true)"
    case "$notion_api_key_from_file" in
        \"*\") notion_api_key_from_file="${notion_api_key_from_file:1:${#notion_api_key_from_file}-2}" ;;
        \'*\') notion_api_key_from_file="${notion_api_key_from_file:1:${#notion_api_key_from_file}-2}" ;;
    esac
    if [[ -n "$notion_api_key_from_file" ]]; then
        export NOTION_API_KEY="$notion_api_key_from_file"
    fi
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

print_todo_section() {
    local todo_lines

    echo "## ✅ 重要待辦事項"
    echo ""

    # Keep every Notion/network/parser failure local to this optional section.
    # The helper has a 15-second per-request timeout and this subprocess is
    # bounded at 20 seconds, matching the calendar section's defensive shape.
    if ! todo_lines="$($VENV_PY - <<'PY'
import json
import os
from datetime import date, datetime
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

API_URL = (
    "https://api.notion.com/v1/data_sources/"
    "ce5f4bd9-9f86-4dda-937d-c751abc83983/query"
)
MAX_TASKS = 15
TODAY = datetime.now(ZoneInfo("Asia/Taipei")).date()


def compact(value):
    return " ".join(str(value or "").split())


def parse_due_date(value):
    if not isinstance(value, str) or not value:
        return None
    if "T" not in value:
        return date.fromisoformat(value[:10])
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.date()
    return parsed.astimezone(ZoneInfo("Asia/Taipei")).date()


def request_page(payload):
    api_key = compact(os.environ.get("NOTION_API_KEY"))
    if not api_key:
        raise RuntimeError("NOTION_API_KEY is not configured")
    request = Request(
        API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Notion-Version": "2025-09-03",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=15) as response:
            if response.status != 200:
                raise RuntimeError(f"Notion returned HTTP {response.status}")
            return json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        raise RuntimeError("Notion request failed") from exc


def title_from_property(property_value):
    if not isinstance(property_value, dict):
        return ""
    title_parts = property_value.get("title")
    if not isinstance(title_parts, list):
        return ""
    pieces = []
    for part in title_parts:
        if not isinstance(part, dict):
            continue
        text = part.get("plain_text")
        if not isinstance(text, str):
            text_data = part.get("text")
            text = text_data.get("content") if isinstance(text_data, dict) else ""
        if isinstance(text, str):
            pieces.append(text)
    return compact("".join(pieces))


def task_from_page(page):
    if not isinstance(page, dict):
        return None
    properties = page.get("properties")
    if not isinstance(properties, dict):
        return None

    name = title_from_property(properties.get("Name")) or "（無標題）"
    status_property = properties.get("Status")
    status_data = status_property.get("select") if isinstance(status_property, dict) else None
    status = status_data.get("name") if isinstance(status_data, dict) else None
    if status not in {"To Do", "Doing"}:
        return None

    due_property = properties.get("Due Date")
    due_data = due_property.get("date") if isinstance(due_property, dict) else None
    due_start = due_data.get("start") if isinstance(due_data, dict) else None
    try:
        due_date = parse_due_date(due_start) if due_start else None
    except (TypeError, ValueError, OverflowError):
        return None
    return {"name": name, "status": status, "due_date": due_date}


payload = {
    "filter": {
        "or": [
            {"property": "Status", "select": {"equals": "To Do"}},
            {"property": "Status", "select": {"equals": "Doing"}},
        ]
    },
    "sorts": [{"property": "Due Date", "direction": "ascending"}],
    "page_size": 100,
}
tasks = []
while True:
    response = request_page(payload)
    if not isinstance(response, dict) or not isinstance(response.get("results"), list):
        raise RuntimeError("Malformed Notion response")
    for page in response["results"]:
        task = task_from_page(page)
        if task is not None:
            tasks.append(task)
    if not response.get("has_more"):
        break
    cursor = response.get("next_cursor")
    if not isinstance(cursor, str) or not cursor:
        raise RuntimeError("Malformed Notion pagination response")
    payload["start_cursor"] = cursor

# Notion's server-side null ordering is not relied on: explicitly put tasks
# without a due date last before applying the rendering cap.
tasks.sort(key=lambda task: (task["due_date"] is None, task["due_date"] or date.max))
if not tasks:
    print("- 目前沒有待辦事項 🎉")
    raise SystemExit(0)

lines = []
for task in tasks[:MAX_TASKS]:
    due_date = task["due_date"]
    if due_date is None:
        prefix = "-"
    elif due_date < TODAY:
        prefix = f"- ⚠️ [逾期 {due_date:%m/%d}]"
    elif due_date == TODAY:
        prefix = "- 🔴 [今天到期]"
    else:
        prefix = f"- [到期: {due_date:%m/%d}]"
    suffix = "（進行中）" if task["status"] == "Doing" else ""
    lines.append(f"{prefix} {task['name']}{suffix}")

if len(tasks) > MAX_TASKS:
    lines.append(f"...還有 {len(tasks) - MAX_TASKS} 筆")
print("\n".join(lines))
PY
    )"; then
        echo "（待辦事項暫時無法取得）"
        return 0
    fi

    printf '%s\n' "$todo_lines"
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
print_todo_section
echo ""
echo "## 🌤️ 今日雙地天氣"
echo ""
echo "| 地區 | 天氣 | 溫度 | 降雨機率 | 穿搭建議 |"
echo "|------|------|------|----------|----------|"
echo "| 台北 | ${TAIPEI_WEATHER%%:*} | ${TAIPEI_TEMP:-N/A} | ${RAINY} | 薄長袖 + 備傘 |"
echo "| 新竹/竹東 (ITRI) | ${HSINCHU_WEATHER%%:*} | ${HSINCHU_TEMP:-N/A} | ${RAINY} | 輕薄外套 + 雨具備用 |"
echo ""
echo "=========================================="
