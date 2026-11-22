#!/usr/bin/env bash
# 通过 API 逐步演示缓存代理的全部关键行为。
# 用法：
#   Docker:  docker compose up -d --build && bash demo/api-demo.sh
#   本地:    bash scripts/start-local.sh && bash demo/api-demo.sh
set -u
PROXY="${PROXY_BASE:-http://127.0.0.1:18000}"
ORIGIN="${ORIGIN_BASE:-http://127.0.0.1:18080}"
H_JSON='Accept: application/json'
H_MD='Accept: text/markdown'

line() { printf '\n========== %s ==========\n' "$1"; }
code() { printf '$ %s\n' "$*"; }

line "0. 复位：源站规则=Vary: Accept, Accept-Language，清空代理缓存"
code curl -s -X POST "$ORIGIN/admin/reset"
curl -s -X POST "$ORIGIN/admin/reset"; echo
code curl -s -X POST "$PROXY/_cache/clear"
curl -s -X POST "$PROXY/_cache/clear"; echo

line "1. 按 Accept / Accept-Language 协商，同一变体第二次命中缓存"
code "curl -s $PROXY/releases -H '$H_JSON' -H 'Accept-Language: zh-CN' | head -4"
curl -s "$PROXY/releases" -H "$H_JSON" -H 'Accept-Language: zh-CN' | head -4
code "curl -si ... （查看 X-Cache: HIT）"
curl -si "$PROXY/releases" -H "$H_JSON" -H 'Accept-Language: zh-CN' | grep -iE 'HTTP|x-cache|vary|etag'
code "curl -s $PROXY/releases -H '$H_MD' -H 'Accept-Language: zh-CN' | head -3"
curl -s "$PROXY/releases" -H "$H_MD" -H 'Accept-Language: zh-CN' | head -3

line "2. 查看代理缓存内部状态（按 Vary 存了几个变体）"
code curl -s "$PROXY/_cache/inspect"
curl -s "$PROXY/_cache/inspect" | python3 -m json.tool 2>/dev/null || curl -s "$PROXY/_cache/inspect"

line "3. max-age 到期后带 ETag 复验，304 沿用旧正文"
code "curl -s -X POST $PROXY/_cache/age -d '{\"seconds\":31}'  # 演示用：让条目立即过期"
curl -s -X POST "$PROXY/_cache/age" -H 'Content-Type: application/json' -d '{"seconds":31}'; echo
code curl -si "$PROXY/releases" -H "'$H_JSON'" -H 'Accept-Language: zh-CN'
curl -si "$PROXY/releases" -H "$H_JSON" -H 'Accept-Language: zh-CN' | grep -iE 'HTTP|x-cache'
code "源站统计（可见 status304 计数）"
curl -s "$ORIGIN/admin/state"; echo

line "4. 切换源站 Vary 规则 -> 只按 Accept，复验发现后旧规则条目整体失效"
code curl -s -X POST "$ORIGIN/admin/vary/accept"
curl -s -X POST "$ORIGIN/admin/vary/accept"; echo
curl -s -X POST "$PROXY/_cache/age" -H 'Content-Type: application/json' -d '{"seconds":31}' >/dev/null
code curl -si "$PROXY/releases" -H "'$H_JSON'" -H 'Accept-Language: zh-CN'
curl -si "$PROXY/releases" -H "$H_JSON" -H 'Accept-Language: zh-CN' | grep -iE 'HTTP|x-cache|vary'
code "新规则下再用中文头请求：语言不参与键，直接 HIT 同一份英文正文"
curl -si "$PROXY/releases" -H "$H_JSON" -H 'Accept-Language: zh-CN' | grep -iE 'x-cache|content-language'
curl -s "$PROXY/_cache/inspect" | python3 -m json.tool 2>/dev/null | grep -E 'confirmed_vary|generation|entry_count'

line "5. 回源失败：代理报 502，已确认缓存保持不变"
code curl -s -X POST "$ORIGIN/admin/fail/1"
curl -s -X POST "$ORIGIN/admin/fail/1"; echo
code "curl -si $PROXY/releases -H 'Accept: text/markdown' （冷变体，回源失败）"
curl -si "$PROXY/releases" -H "$H_MD" | grep -iE 'HTTP|x-cache'
code "已缓存的英文 JSON 仍可命中"
curl -si "$PROXY/releases" -H "$H_JSON" -H 'Accept-Language: en' | grep -iE 'x-cache'

line "6. 并发演示（可选）：不同语言并行回源、切换瞬间在途请求隔离"
code "PROXY_BASE=$PROXY ORIGIN_BASE=$ORIGIN python -m app.tests.selftest"
echo "完整并发自测请运行：python -m app.tests.selftest（51 项断言）"
echo
line "演示结束"
