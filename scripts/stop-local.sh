#!/usr/bin/env bash
# 停止 start-local.sh 启动的 origin / proxy
set -u
cd "$(dirname "$0")/.."
for name in origin proxy; do
  pf=".run/${name}.pid"
  if [ -f "$pf" ]; then
    pid=$(cat "$pf")
    if kill "$pid" 2>/dev/null; then
      echo "stopped $name pid=$pid"
    else
      echo "$name pid=$pid not running"
    fi
    rm -f "$pf"
  fi
done
