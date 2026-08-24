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
echo "## 🌤️ 今日雙地天氣"
echo ""
echo "| 地區 | 天氣 | 溫度 | 降雨機率 | 穿搭建議 |"
echo "|------|------|------|----------|----------|"
echo "| 台北 | ${TAIPEI_WEATHER%%:*} | ${TAIPEI_TEMP:-N/A} | ${RAINY} | 薄長袖 + 備傘 |"
echo "| 新竹/竹東 (ITRI) | ${HSINCHU_WEATHER%%:*} | ${HSINCHU_TEMP:-N/A} | ${RAINY} | 輕薄外套 + 雨具備用 |"
echo ""
echo "=========================================="
