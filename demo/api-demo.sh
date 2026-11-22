#!/usr/bin/env bash
# 通过 API 逐步演示缓存代理的关键行为（重点：CDN 场景下的 /releases）。
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
hdrs() { grep -iE 'HTTP|^vary:|^age:|cache-control|content-language|x-cache|x-cache-generation'; }

line "0. 复位：源站规则=Vary: Accept, Accept-Language，清空代理缓存"
code curl -s -X POST "$ORIGIN/admin/reset"
curl -s -X POST "$ORIGIN/admin/reset"; echo
code curl -s -X POST "$PROXY/_cache/clear"
curl -s -X POST "$PROXY/_cache/clear"; echo

line "1. 两种语言：回包带与正文对应的 Vary（CDN 不会把中文回包发给英文请求）"
code "curl -si $PROXY/releases -H '$H_JSON' -H 'Accept-Language: zh-CN'"
curl -si "$PROXY/releases" -H "$H_JSON" -H 'Accept-Language: zh-CN' | hdrs
code "curl -s $PROXY/releases -H '$H_JSON' -H 'Accept-Language: zh-CN' | head -3"
curl -s "$PROXY/releases" -H "$H_JSON" -H 'Accept-Language: zh-CN' | head -3
code "curl -si $PROXY/releases -H '$H_JSON' -H 'Accept-Language: en'"
curl -si "$PROXY/releases" -H "$H_JSON" -H 'Accept-Language: en' | hdrs
code "curl -s $PROXY/releases -H '$H_JSON' -H 'Accept-Language: en' | head -3"
curl -s "$PROXY/releases" -H "$H_JSON" -H 'Accept-Language: en' | head -3
code "curl -si $PROXY/releases -H '$H_MD' -H 'Accept-Language: zh-CN' （Markdown 变体）"
curl -si "$PROXY/releases" -H "$H_MD" -H 'Accept-Language: zh-CN' | hdrs

line "2. 临期命中：回包带 Age，CDN 只在代理剩余的新鲜期内复用"
code "curl -s -X POST $PROXY/_cache/age -d '{\"seconds\":22}'  # 模拟已过 22s"
curl -s -X POST "$PROXY/_cache/age" -H 'Content-Type: application/json' -d '{"seconds":22}'; echo
code "curl -si $PROXY/releases -H '$H_JSON' -H 'Accept-Language: zh-CN'"
curl -si "$PROXY/releases" -H "$H_JSON" -H 'Accept-Language: zh-CN' | hdrs
echo "-> X-Cache: HIT，Cache-Control: max-age=30 且 Age: 22，CDN 还能复用 8s 而不是重新计时 30s"

line "3. 查看代理缓存内部状态（按 Vary 存了几个变体）"
code curl -s "$PROXY/_cache/inspect"
curl -s "$PROXY/_cache/inspect" | python3 -m json.tool 2>/dev/null || curl -s "$PROXY/_cache/inspect"

line "4. max-age 到期后带 ETag 复验，304 沿用旧正文（按这次确认的规则与新鲜期回包）"
code "curl -s -X POST $PROXY/_cache/age -d '{\"seconds\":31}'  # 让条目立即过期"
curl -s -X POST "$PROXY/_cache/age" -H 'Content-Type: application/json' -d '{"seconds":31}'; echo
code "curl -si $PROXY/releases -H '$H_JSON' -H 'Accept-Language: zh-CN'"
curl -si "$PROXY/releases" -H "$H_JSON" -H 'Accept-Language: zh-CN' | hdrs
code "源站统计（可见 status304 计数）"
curl -s "$ORIGIN/admin/state"; echo

line "5. 切换源站 Vary 规则 -> 只按 Accept，复验发现后旧规则条目整体失效"
code curl -s -X POST "$ORIGIN/admin/vary/accept"
curl -s -X POST "$ORIGIN/admin/vary/accept"; echo
curl -s -X POST "$PROXY/_cache/age" -H 'Content-Type: application/json' -d '{"seconds":31}' >/dev/null
code "curl -si $PROXY/releases -H '$H_JSON' -H 'Accept-Language: zh-CN'"
curl -si "$PROXY/releases" -H "$H_JSON" -H 'Accept-Language: zh-CN' | hdrs
code "新规则下再用中文头请求：语言不参与键，直接 HIT 同一份英文正文"
curl -si "$PROXY/releases" -H "$H_JSON" -H 'Accept-Language: zh-CN' | hdrs
curl -s "$PROXY/_cache/inspect" | python3 -m json.tool 2>/dev/null | grep -E 'confirmed_vary|generation|entry_count'

line "6. 并行复验：不同语言各自并行回源，切换瞬间的在途/迟到答复只给原请求"
code "curl -s -X POST $ORIGIN/admin/vary/both  # 先切回双语规则，预热两个语言"
curl -s -X POST "$ORIGIN/admin/vary/both" >/dev/null
curl -s -X POST "$PROXY/_cache/clear" >/dev/null
curl -s "$PROXY/releases" -H "$H_JSON" -H 'Accept-Language: zh-CN' >/dev/null
curl -s "$PROXY/releases" -H "$H_JSON" -H 'Accept-Language: en' >/dev/null
curl -s -X POST "$PROXY/_cache/age" -H 'Content-Type: application/json' -d '{"seconds":31}' >/dev/null
curl -s -X POST "$ORIGIN/admin/delay/800" >/dev/null
code "并行发起 zh / en 复验，期间把源站规则切到只按 Accept"
( curl -s -D /tmp/demo-zh.h "$PROXY/releases" -H "$H_JSON" -H 'Accept-Language: zh-CN' -o /tmp/demo-zh.b & \
  curl -s -D /tmp/demo-en.h "$PROXY/releases" -H "$H_JSON" -H 'Accept-Language: en' -o /tmp/demo-en.b & \
  sleep 0.3; curl -s -X POST "$ORIGIN/admin/vary/accept" >/dev/null; wait )
printf 'zh: %s | %s | content-language: %s\n' \
  "$(grep -i '^x-cache:' /tmp/demo-zh.h | tr -d '\r')" \
  "$(grep -i '^cache-control:' /tmp/demo-zh.h | tr -d '\r')" \
  "$(grep -i '^content-language:' /tmp/demo-zh.h | tr -d '\r' | cut -d' ' -f2)"
printf 'en: %s | %s | content-language: %s\n' \
  "$(grep -i '^x-cache:' /tmp/demo-en.h | tr -d '\r')" \
  "$(grep -i '^cache-control:' /tmp/demo-en.h | tr -d '\r')" \
  "$(grep -i '^content-language:' /tmp/demo-en.h | tr -d '\r' | cut -d' ' -f2)"
echo "-> 两种语言并行复验（总耗时约 0.9s，而非串行 1.6s）；胜方按新规则缓存，"
echo "   只给原请求的答复带 Cache-Control: no-store，CDN 不会存储。"
code "新规则下再请求（正常回包带 Vary）"
curl -si "$PROXY/releases" -H "$H_JSON" -H 'Accept-Language: zh-CN' | hdrs
curl -s "$PROXY/_cache/inspect" | python3 -m json.tool 2>/dev/null | grep -E 'confirmed_vary|generation|entry_count'
curl -s -X POST "$ORIGIN/admin/delay/0" >/dev/null
rm -f /tmp/demo-zh.h /tmp/demo-zh.b /tmp/demo-en.h /tmp/demo-en.b

line "7. 回源失败：代理报 502，已确认缓存保持不变"
code curl -s -X POST "$ORIGIN/admin/fail/1"
curl -s -X POST "$ORIGIN/admin/fail/1"; echo
code "curl -si $PROXY/releases -H 'Accept: text/markdown' （冷变体，回源失败）"
curl -si "$PROXY/releases" -H "$H_MD" | hdrs
code "已缓存的英文 JSON 仍可命中"
curl -si "$PROXY/releases" -H "$H_JSON" -H 'Accept-Language: en' | hdrs

line "8. 完整自测"
echo "PROXY_BASE=$PROXY ORIGIN_BASE=$ORIGIN python -m app.tests.selftest（80 项断言）"
echo
line "演示结束"
