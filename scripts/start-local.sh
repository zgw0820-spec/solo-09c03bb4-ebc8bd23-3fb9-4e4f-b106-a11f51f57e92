#!/usr/bin/env bash
# 无 Docker 环境下的本地裸跑启动（与 docker-compose 等价的两个进程）
set -eu
cd "$(dirname "$0")/.."
export ORIGIN_BASE=http://127.0.0.1:18080
mkdir -p .run
.venv/bin/python -m uvicorn app.origin:app --host 127.0.0.1 --port 18080 \
  > .run/origin.log 2>&1 &
echo $! > .run/origin.pid
.venv/bin/python -m uvicorn app.proxy:app --host 127.0.0.1 --port 18000 \
  > .run/proxy.log 2>&1 &
echo $! > .run/proxy.pid
echo "origin  -> http://127.0.0.1:18080 (pid $(cat .run/origin.pid))"
echo "proxy   -> http://127.0.0.1:18000 (pid $(cat .run/proxy.pid))"
