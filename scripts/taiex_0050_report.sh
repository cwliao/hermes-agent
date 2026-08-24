#!/usr/bin/env bash
set -euo pipefail

mode=${1:-normal}
case "$mode" in
  open)   title='🔔 開盤觀察' ;;
  normal) title='📊 台股盤中快照' ;;
  close)  title='🏁 收盤整理' ;;
  *)
    echo "usage: $0 {open|normal|close}" >&2
    exit 2
    ;;
esac

tmpdir=$(mktemp -d "${TMPDIR:-/tmp}/taiex-0050.XXXXXX")
trap 'rm -rf "$tmpdir"' EXIT

# TWSE MIS is the same source used by the previous verified live script.
# t00.tw is the TAIEX index; 0050.tw is Yuanta Taiwan 50 ETF.
curl -fsSL --max-time 20 -A 'Mozilla/5.0' \
  'https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_t00.tw%7Ctse_0050.tw&json=1&delay=0' \
  > "$tmpdir/quotes.json"

export REPORT_MODE="$mode"
export REPORT_TITLE="$title"
/usr/bin/python3 - "$tmpdir/quotes.json" <<'PY'
import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo


def value(item, key):
    raw = item.get(key)
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or text in {"-", "--", "N/A", "null"}:
        return None
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def text_value(item, key):
    raw = item.get(key)
    if raw is None:
        return None
    text = str(raw).strip()
    return text if text and text not in {"-", "--", "N/A", "null"} else None


def number(value_, decimals):
    return "N/A" if value_ is None else f"{value_:,.{decimals}f}"


def signed(value_, decimals):
    return "N/A" if value_ is None else f"{value_:+,.{decimals}f}"


def marker(change):
    if change is None:
        return "▫️"
    if change > 0:
        return "🟢"
    if change < 0:
        return "🔴"
    return "⚪"


def quote(item, decimals, current_fallback=False):
    current = value(item, "z")
    source = "即時"
    if current is None and current_fallback:
        # pz is the explicitly documented previous-tick fallback. Never use y
        # here: y is the previous close and must not be shown as current price.
        current = value(item, "pz")
        source = "fallback" if current is not None else "缺價"
    elif current is None:
        source = "缺價"

    previous = value(item, "y")
    change = current - previous if current is not None and previous is not None else None
    percent = change / previous * 100 if change is not None and previous else None
    stamp = text_value(item, "%") or text_value(item, "t") or "N/A"
    volume = value(item, "v")
    return {
        "current": number(current, decimals),
        "change": signed(change, 2),
        "percent": signed(percent, 2),
        "previous": number(previous, decimals),
        "stamp": stamp,
        "source": source,
        "volume": "N/A" if volume is None else f"{volume:,.0f}",
        "marker": marker(change),
    }


with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)

items = {str(item.get("c")): item for item in payload.get("msgArray", [])}
index = items.get("t00")
etf = items.get("0050")
if index is None or etf is None:
    missing = ", ".join(name for name, item in (("t00", index), ("0050", etf)) if item is None)
    raise SystemExit(f"TWSE MIS response missing quote(s): {missing}")

now = datetime.now(ZoneInfo("Asia/Taipei"))
iq = quote(index, 2)
eq = quote(etf, 4, current_fallback=True)

print(os.environ["REPORT_TITLE"])
print(now.strftime("%Y-%m-%d %H:%M Asia/Taipei"))
print()
print(f"{iq['marker']} 台灣加權指數 TAIEX")
print(f"   現價 {iq['current']}｜漲跌 {iq['change']} ({iq['percent']}%)")
print(f"   前收 {iq['previous']}｜資料 {iq['stamp']}｜價源 {iq['source']}｜量 {iq['volume']}")
print(f"{eq['marker']} 元大台灣50 ETF 0050")
print(f"   現價 {eq['current']}｜漲跌 {eq['change']} ({eq['percent']}%)")
print(f"   前收 {eq['previous']}｜資料 {eq['stamp']}｜價源 {eq['source']}｜量 {eq['volume']}")
PY
