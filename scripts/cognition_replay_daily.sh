#!/usr/bin/env bash
# 市场实践回放线·事实层每日定时更新（owner 2026-09-06 拍板授权，23:00）。
# 职责：snapshot（当日收盘事实）+ nominate（窗口/提名重算）——只写机器面
# （$STATE/fin-analyse/cognition-replay-evidence/），不写标注文档正文；
# 正文吸收仍走 owner 扫批/验证批次（设计稿 cognition-replay-facts 裁决层边界）。
# 数据源发布延迟对策（09-07 首晚实证：23:00 当日行未出→skip）：
#   交易日（周一至五）skip 后每 35 分钟重试，至多 4 次到 ~01:45；
#   周末/节假日首轮 skip 即止。全组取数失败=rc1 硬错误（快照内守卫）。
set -euo pipefail
umask 077

REPO=/home/ypk/fin-core
PY="$REPO/.venv/bin/python"
TODAY="$(date +%Y%m%d)"
LOG_DIR="/home/ypk/.local/state/fin-analyse/cognition-replay-evidence"
LOG="$LOG_DIR/daily.log"

mkdir -p "$LOG_DIR"
{
  echo "===== $(date '+%F %T') run batch=$TODAY"
  snap_out=""
  for attempt in 1 2 3 4; do
    snap_out="$("$PY" "$REPO/scripts/cognition_replay_snapshot.py" --as-of "$TODAY" --require-today --apply)"
    echo "$snap_out"
    grep -q '"mode": "skip"' <<<"$snap_out" || break
    [[ "$attempt" -lt 4 ]] && { sleep 2100; }
  done
  if grep -q '"mode": "skip"' <<<"$snap_out"; then
    echo "nominate skipped（非交易日或数据源当日行未发布；交易日已重试 4 次）"
  else
    "$PY" "$REPO/scripts/cognition_replay_nominate.py" --as-of "$TODAY" --apply
  fi
  echo "===== done rc=0"
} >> "$LOG" 2>&1
