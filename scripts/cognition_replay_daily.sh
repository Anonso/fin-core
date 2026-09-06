#!/usr/bin/env bash
# 市场实践回放线·事实层每日定时更新（owner 2026-09-06 拍板授权，23:00）。
# 职责：snapshot（当日收盘事实）+ nominate（窗口/提名重算）——只写机器面
# （$STATE/fin-analyse/cognition-replay-evidence/），不写标注文档正文；
# 正文吸收仍走 owner 扫批/验证批次（设计稿 cognition-replay-facts 裁决层边界）。
# 节假日/数据未就绪：snapshot --require-today 返回 skip → nominate 同步跳过（退出 0）。
# 日志：追加 daily.log（stdout 数据契约 + stderr 诊断同落）；黑话禁令由 CLI 保证。

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
  snap_out="$("$PY" "$REPO/scripts/cognition_replay_snapshot.py" --as-of "$TODAY" --require-today --apply)"
  echo "$snap_out"
  if grep -q '"mode": "skip"' <<<"$snap_out"; then
    echo "nominate skipped (non-trading day / data not ready)"
  else
    "$PY" "$REPO/scripts/cognition_replay_nominate.py" --as-of "$TODAY" --apply
  fi
  echo "===== done rc=0"
} >> "$LOG" 2>&1
