# 发布说明页 FastAPI 缓存代理

为文档站的发布说明页（`/releases`）实现的带内容协商感知的缓存代理。

## 架构

```
客户端 ──HTTP──> cache-proxy (FastAPI, :8000) ──回源──> docs-origin (FastAPI, :8080)
                  内存缓存 + 并发控制                  可切换 Vary 规则 / 可延迟 / 可注入故障
```

- `app/origin.py`：本地源站。按 `Accept` 选 JSON / Markdown，可切换是否再按
  `Accept-Language` 选语言；管理 API 可注入回包延迟、故障、挂起、切换 Vary 规则。
- `app/proxy.py`：缓存代理，实现全部缓存与并发语义。
- `app/content.py`：中英文发布说明示例内容。
- `app/tests/selftest.py`：端到端自测（80 项断言，含 CDN 回包头、304 确认新规则、并发切换与迟到旧回包）。

## 缓存语义（与需求逐条对应）

1. **按 Vary 查缓存**：每个路径确认一份 Vary 规则，按规则中的请求头
   （`Accept`、`Accept-Language`）计算变体键，不同变体分别缓存。
2. **max-age + ETag 复验**：新鲜期内直接命中；过期后带 `If-None-Match` 回源。
   - 源站回 **304**：沿用发起复验时选中的旧正文，只刷新元数据（`X-Cache: REVALIDATED_304`）。
   - 源站回 **200**：用新正文替换（`X-Cache: REVALIDATED_200` / `MISS`）。
3. **Vary 规则改变整体失效**：回源响应的 Vary 与已确认规则不同时，清空该路径
   旧规则全部条目、generation 递增，本次正文按新规则缓存（`X-Cache: MISS_NEW_RULE`）。
4. **并行回源 + single-flight**：不同协商身份（不同 Accept / 语言）用不同的锁，
   可并行回源；同一身份的并发请求合并为一次回源。
5. **切换时在途请求隔离**：源站通过 `X-Rule-Epoch` 标识规则世代；代理回源前探测
   基准世代，响应世代不一致且规则已被其他请求提交时，该在途答复只返回给它的
   原始请求（`X-Cache: INFLIGHT_RULE_SWITCHED` / `STALE_INFLIGHT_304`），不写入新缓存。
   新规则已被别的请求确认之后才到达的旧规则回包同样只给原请求：不恢复旧规则、
   不刷新当前条目（`X-Cache: INFLIGHT_RULE_SWITCHED` / `STALE_INFLIGHT_304`）。
6. **面向 CDN 的回包**：正常回包携带与正文对应的 `Vary`（缓存按该规则匹配请求，
   中文回包不会发给英文请求），并回放原始 `max-age` 与 `Age`（代理已消耗的新鲜期），
   CDN 只在代理剩余新鲜期内复用，不会把命中当成新内容从头计时。
   只给原请求的回包一律 `Cache-Control: no-store`，禁止 CDN 存储。
7. **回源失败保状态**：连接错误或源站 5xx 返回 502（`X-Cache: ORIGIN_ERROR`），
   不修改任何已确认的缓存条目与规则；已缓存的新鲜内容仍可正常命中，
   过期条目按此前确认的期限处理。

## 用 Docker Compose 启动

```bash
docker compose up -d --build
# 代理: http://localhost:18000   源站: http://localhost:18080

# API 演示（CDN 头、两种语言、临期命中、并行复验、规则切换、故障保缓存）
bash demo/api-demo.sh

# 全部自测（80 项断言）
docker compose run --rm self-test
```

## 无 Docker 时本地裸跑

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
bash scripts/start-local.sh
PROXY_BASE=http://127.0.0.1:18000 ORIGIN_BASE=http://127.0.0.1:18080 \
  .venv/bin/python -m app.tests.selftest
bash demo/api-demo.sh
bash scripts/stop-local.sh
```

## 主要 API

| 服务 | 方法/路径 | 说明 |
| --- | --- | --- |
| 代理 | `GET /releases` | 代理入口，响应头 `X-Cache` 标注缓存状态 |
| 代理 | `GET /_cache/inspect` | 查看各路径的 Vary、generation、条目、统计 |
| 代理 | `POST /_cache/clear` | 清空缓存 |
| 代理 | `POST /_cache/age` | 测试辅助：把条目录龄（模拟 max-age 到期） |
| 源站 | `POST /admin/vary/{accept,both}` | 切换 Vary 规则 |
| 源站 | `POST /admin/delay/{ms}` | 设置回包延迟 |
| 源站 | `POST /admin/hold/{ms}/{n}` | 接下来 n 个回包渲染后额外挂起 ms（复现旧规则回包晚到） |
| 源站 | `POST /admin/fail/{n}` | 接下来 n 个请求返回 500 |
| 源站 | `GET /admin/wait_inflight/{n}` | 测试同步：等待 n 个请求在途 |
| 源站 | `GET /admin/state` | 源站状态与请求计数 |

## 已知边界

- 缓存为进程内内存存储，重启即失效、不支持多实例共享。
- 仅代理 GET；`/_cache/*` 管理接口无鉴权，仅适用于本地演示。
- 为识别“回源在途期间的规则切换”，代理每次回源前会额外请求一次源站
  `/rule_epoch` 探测基准世代；该端点很轻量，但若源站不支持等价机制，
  在途切换的精确识别只能退化为按 Vary 头比对。
- 源站不提供 `X-Rule-Epoch` 时，代理无法区分“迟到的旧规则回包”与“源站
  世代回退”，只能按当前 Vary 规则处理；建议生产源站始终返回该头部。
