"""本地源站：文档站 /releases 发布说明页。

- 按 Accept 在 JSON 与 Markdown 之间选格式；
- Vary 规则可通过管理 API 切换：
  * vary_accept   -> Vary: Accept（语言不参与协商）
  * vary_both     -> Vary: Accept, Accept-Language（语言参与协商）
- 支持管理 API 注入回包延迟与故障，方便演示代理的在途请求与失败处理；
- 支持条件请求：If-None-Match 命中时返回 304，304 同样携带当前 Vary，
  以便代理在复验响应里识别 Vary 规则切换。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from app.content import RELEASES

app = FastAPI(title="docs-origin", version="1.0")


@dataclass
class OriginState:
    # accept  -> 只按 Accept 协商；both -> 再按 Accept-Language 协商
    vary_mode: str = "both"
    rule_epoch: int = 0  # 每次切换 Vary 规则递增，随响应头返回，供代理识别在途切换
    delay_ms: int = 0
    fail_next: int = 0  # 接下来 N 个内容请求返回 500
    inflight: int = 0  # 当前正在处理（含延迟等待）的内容请求数
    counters: dict = field(default_factory=lambda: {"requests": 0, "status304": 0, "status200": 0, "status500": 0})


state = OriginState()
_lock = asyncio.Lock()


def _pick_accept(header: str | None) -> str:
    """简单解析 Accept 头部的 q 值，在 application/json 与 text/markdown 间选择。"""
    if not header:
        return "json"
    best, best_q = "json", 0.0
    for raw in header.split(","):
        part = raw.split(";")
        mime = part[0].strip().lower()
        q = 1.0
        for p in part[1:]:
            p = p.strip()
            if p.startswith("q="):
                try:
                    q = float(p[2:])
                except ValueError:
                    q = 0.0
        if mime in ("application/json", "text/markdown", "*/*") and q > best_q:
            best = "json" if mime in ("application/json", "*/*") else "md"
            best_q = q
    return best


def _pick_language(header: str | None) -> str:
    """简单解析 Accept-Language，支持 zh / en。"""
    if not header:
        return "en"
    best, best_q = "en", -1.0
    for raw in header.split(","):
        part = raw.split(";")
        tag = part[0].strip().lower()
        q = 1.0
        for p in part[1:]:
            p = p.strip()
            if p.startswith("q="):
                try:
                    q = float(p[2:])
                except ValueError:
                    q = 0.0
        lang = "zh" if tag.startswith("zh") else "en" if tag.startswith("en") else None
        if lang and q > best_q:
            best, best_q = lang, q
    return best


def _vary_header() -> str:
    return "Accept" if state.vary_mode == "accept" else "Accept, Accept-Language"


def _render(fmt: str, lang: str) -> bytes:
    if fmt == "json":
        payload = [
            {
                "version": r["version"],
                "date": r["date"],
                "title": r[lang]["title"],
                "summary": r[lang]["summary"],
                "items": r[lang]["items"],
            }
            for r in RELEASES
        ]
        return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")

    lines = [f"# {RELEASES[0][lang]['title']}", ""]
    for r in RELEASES:
        lines += [
            f"## {r['version']} — {r['date']}",
            "",
            r[lang]["summary"],
            "",
        ]
        lines.extend(f"- {item}" for item in r[lang]["items"])
        lines.append("")
    return "\n".join(lines).encode("utf-8")


def _etag(fmt: str, lang: str) -> str:
    # ETag 只与实际表示（格式+语言+内容）相关，不随 Vary 规则变化，
    # 这样规则切换后同一表示的条件复验仍可返回 304
    digest = hashlib.sha1(f"{fmt}:{lang}".encode()).hexdigest()[:16]
    return f'"{digest}"'


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "origin"}


@app.get("/rule_epoch")
async def rule_epoch() -> dict:
    """轻量端点：返回当前规则世代，供代理在回源前记录基准世代。"""
    return {"rule_epoch": state.rule_epoch, "vary_header": _vary_header()}


@app.get("/releases")
async def releases(request: Request) -> Response:
    async with _lock:
        state.counters["requests"] += 1
        state.inflight += 1
        should_fail = state.fail_next > 0
        if should_fail:
            state.fail_next -= 1
            state.counters["status500"] += 1

    try:
        if state.delay_ms:
            await asyncio.sleep(state.delay_ms / 1000.0)

        if should_fail:
            return PlainTextResponse("origin injected failure", status_code=500)

        fmt = _pick_accept(request.headers.get("accept"))
        # 只有 vary_mode=both 时语言才参与协商；accept 模式统一回英文
        lang = _pick_language(request.headers.get("accept-language")) if state.vary_mode == "both" else "en"
        etag = _etag(fmt, lang)
        vary = _vary_header()
        # 延迟结束后读取当前规则世代：在途期间切换规则会体现在这里
        epoch = str(state.rule_epoch)

        if request.headers.get("if-none-match") == etag:
            state.counters["status304"] += 1
            # 304 无正文，但仍带上当前 Vary / ETag / 规则世代，供代理判断规则是否变化
            return Response(
                status_code=304,
                headers={
                    "ETag": etag,
                    "Vary": vary,
                    "Cache-Control": "max-age=30",
                    "X-Rule-Epoch": epoch,
                },
            )

        state.counters["status200"] += 1
        body = _render(fmt, lang)
        media = "application/json" if fmt == "json" else "text/markdown"
        return Response(
            content=body,
            media_type=media,
            headers={
                "ETag": etag,
                "Vary": vary,
                "Cache-Control": "max-age=30",
                "Content-Language": lang,
                "X-Rule-Epoch": epoch,
            },
        )
    finally:
        async with _lock:
            state.inflight -= 1


@app.get("/admin/wait_inflight/{n}")
async def admin_wait_inflight(n: int) -> dict:
    """阻塞到在途内容请求数达到 n（测试同步用，最多等 5 秒）。"""
    for _ in range(100):
        if state.inflight >= n:
            return {"ok": True, "inflight": state.inflight}
        await asyncio.sleep(0.05)
    return {"ok": False, "inflight": state.inflight}


@app.get("/admin/state")
async def admin_get_state() -> dict:
    return {
        "vary_mode": state.vary_mode,
        "vary_header": _vary_header(),
        "rule_epoch": state.rule_epoch,
        "delay_ms": state.delay_ms,
        "fail_next": state.fail_next,
        "inflight": state.inflight,
        "counters": state.counters,
    }


@app.post("/admin/vary/{mode}")
async def admin_set_vary(mode: str) -> dict:
    if mode not in ("accept", "both"):
        return JSONResponse({"error": "mode must be 'accept' or 'both'"}, status_code=400)
    if mode != state.vary_mode:
        state.vary_mode = mode
        state.rule_epoch += 1
    return {
        "ok": True,
        "vary_mode": state.vary_mode,
        "vary_header": _vary_header(),
        "rule_epoch": state.rule_epoch,
    }


@app.post("/admin/delay/{ms}")
async def admin_set_delay(ms: int) -> dict:
    state.delay_ms = max(0, ms)
    return {"ok": True, "delay_ms": state.delay_ms}


@app.post("/admin/fail/{n}")
async def admin_fail(n: int) -> dict:
    state.fail_next = max(0, n)
    return {"ok": True, "fail_next": state.fail_next}


@app.post("/admin/reset")
async def admin_reset() -> dict:
    state.vary_mode = "both"
    state.rule_epoch = 0
    state.delay_ms = 0
    state.fail_next = 0
    state.inflight = 0
    state.counters = {"requests": 0, "status304": 0, "status200": 0, "status500": 0}
    return {"ok": True}
