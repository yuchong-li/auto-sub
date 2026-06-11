#!/bin/bash
# 启动即扫一遍，之后每天 SCAN_HOUR 点再扫。
# 脚本自带跳过逻辑：已有 .zh.srt 的视频不重做，所以每轮只补新的。
set -uo pipefail

SCAN_HOUR="${SCAN_HOUR:-4}"

run_once() {
    echo "[$(date '+%F %T')] 扫描 ${VIDEO_DIR:-/data/the-video} ，开始补字幕…"
    python /app/subtitle_batch.py
    echo "[$(date '+%F %T')] 本轮结束。"
}

while true; do
    run_once || echo "[$(date '+%F %T')] 本轮异常退出，等下一轮再试。"

    now=$(date +%s)
    next=$(date -d "today ${SCAN_HOUR}:00" +%s)
    if [ "$next" -le "$now" ]; then
        next=$(date -d "tomorrow ${SCAN_HOUR}:00" +%s)
    fi
    wait_s=$((next - now))
    echo "[$(date '+%F %T')] 睡到下次扫描：$(date -d "@$next" '+%F %T')（${wait_s}s 后）"
    sleep "$wait_s"
done
