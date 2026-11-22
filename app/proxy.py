"""FastAPI 缓存代理。

缓存语义：
- 每个路径维护一份“已确认”的 Vary 规则；查找缓存时按当前 Vary 选取
  头部（Accept / Accept-Language）计算变体键。
- max-age 未过期直接命中；过期后带 If-None-Match 回源复验：
  * 304 -> 沿用发起复验时选中的旧正文，仅刷新元数据；
  * 200 -> 用新正文替换。
- 回源响应里的 Vary 与已确认规则不一致时，旧规则下的所有条目整体失效
  （generation 递增），本次正文按新规则缓存。
- 并发控制：每个协商身份（Accept + Accept-Language）配一个条件变量，
  同身份请求严格 single-flight（后到者等待在途回源后直接复用结果），
  不同身份互不阻塞、可并行回源。
- 在途切换识别：源站用 X-Rule-Epoch 标识规则世代，代理回源前探测基准
  世代；回源期间规则被别的请求切换时，在途答复只返回给其原始请求
  （INFLIGHT_RULE_SWITCHED / STALE_INFLIGHT_304），不写入新缓存。
  新规则已被别的请求确认后才到达的旧规则回包同样只给原请求，既不恢复
  旧规则，也不刷新当前条目。
- 面向 CDN 的回包：正常答复携带与正文对应的 Vary、原始 max-age 与 Age，
  CDN 只会按 Vary 匹配请求，并在代理剩余新鲜期内复用；只给原请求的答复
  一律 Cache-Control: no-store。
- 回源失败返回 502，不改动任何已确认的缓存状态。
"""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field

import httpx
from fastapi import FastAPI, Request, Response

ORIGIN_BASE = os.environ.get("ORIGIN_BASE", "http://origin:8080")
REPLAY_HEADERS = ("content-type", "content-language", "etag")

app = FastAPI(title="cache-proxy", version="1.0")


@dataclass
class Entry:
    body: bytes
    headers: dict[str, str]
    etag: str
    vary: tuple[str, ...]
    variant_key: tuple[str, ...]
    identity: tuple[str, ...]  # 产生该条目的请求协商身份（accept, accept-language）
    stored_at: float
    max_age: int
    fetch_generation: int


@dataclass
class IdentitySync:
    """单个协商身份的 single-flight 同步原语：同一时刻只允许一个回源，
    后到同身份请求等待在途回源完成后重新检查缓存。"""
    cond: asyncio.Condition = field(default_factory=asyncio.Condition)
    in_flight: bool = False


@dataclass
class PathState:
    vary: tuple[str, ...] | None = None
    # 已确认的源站规则世代号（None=尚未确认），随源站 X-Rule-Epoch 更新
    confirmed_epoch: int | None = None
    entries: dict[tuple[str, ...], Entry] = field(default_factory=dict)
    generation: int = 0
    path_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # 键为协商身份（与 generation 无关），保证同身份请求始终共享同一同步原语
    identity_sync: dict[tuple[str, ...], IdentitySync] = field(default_factory=dict)
    stats: dict = field(
        default_factory=lambda: {"hits": 0, "revalidated": 0, "fetches": 0, "vary_switches": 0, "origin_errors": 0}
    )


# path -> PathState
_cache: dict[str, PathState] = {}
_cache_guard = asyncio.Lock()


async def _get_path_state(path: str) -> PathState:
    async with _cache_guard:
        st = _cache.get(path)
        if st is None:
            st = PathState()
            _cache[path] = st
        return st


def _parse_vary(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(part.strip().lower() for part in value.split(",") if part.strip())


def _parse_max_age(value: str | None, default: int = 0) -> int:
    if not value:
        return default
    for part in value.split(","):
        part = part.strip().lower()
        if part.startswith("max-age="):
            try:
                return max(0, int(part.split("=", 1)[1]))
            except ValueError:
                return default
    return default


def _variant_key(vary: tuple[str, ...], headers) -> tuple[str, ...]:
    return tuple(headers.get(h.lower(), "") for h in vary)


_HEADER_CASE = {
    "accept": "Accept",
    "accept-language": "Accept-Language",
    "accept-encoding": "Accept-Encoding",
    "cookie": "Cookie",
    "user-agent": "User-Agent",
}


def _vary_value(vary: tuple[str, ...]) -> str:
    """把解析后的 Vary 字段名还原成规范的响应头值。"""
    return ", ".join(_HEADER_CASE.get(name, name) for name in vary)


def _age_seconds(entry: Entry) -> int:
    return max(0, int(time.time() - entry.stored_at))


def _build_response(entry: Entry, cache_status: str, *, private: bool = False) -> Response:
    # 缓存指令由代理统一决定，不回放源站旧值，避免重复 Cache-Control
    headers = {k: v for k, v in entry.headers.items() if k in REPLAY_HEADERS}
    # Vary 取正文被选中时对应的规则（而不是可能已变化的当前规则），
    # CDN 才会按语言 / 格式分别匹配请求，不会把中文回包发给英文请求
    if entry.vary:
        headers["Vary"] = _vary_value(entry.vary)
    if private:
        # 只给原请求的答复：CDN 不得存储、不得用于其他请求
        headers["Cache-Control"] = "no-store"
    else:
        # 已确认的 max-age + 已消耗的 Age：CDN 只会在代理剩余的新鲜期内复用它，
        # 而不是把这次命中当成一份全新的 max-age 重新计时
        headers["Cache-Control"] = f"max-age={entry.max_age}"
        headers["Age"] = str(_age_seconds(entry))
    headers["X-Cache"] = cache_status
    headers["X-Cache-Generation"] = str(entry.fetch_generation)
    return Response(content=entry.body, status_code=200, headers=headers)


async def _fetch_from_origin(request: Request, path: str, etag: str | None) -> httpx.Response:
    # 只转发协商与条件请求头部，不转发 Accept-Encoding，避免本地缓存压缩字节
    fwd = {}
    for name in ("accept", "accept-language"):
        value = request.headers.get(name)
        if value is not None:
            fwd[name] = value
    if etag:
        fwd["if-none-match"] = etag
    url = f"{ORIGIN_BASE}{path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"
    async with httpx.AsyncClient(timeout=10.0) as client:
        return await client.get(url, headers=fwd)


async def _probe_origin_epoch() -> int | None:
    """回源前探测源站当前规则世代，作为本次回源的基准世代。失败不阻断主流程。"""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{ORIGIN_BASE}/rule_epoch")
            return int(r.json()["rule_epoch"])
    except (httpx.HTTPError, KeyError, ValueError):
        return None


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "proxy"}


@app.get("/_cache/inspect")
async def cache_inspect() -> dict:
    out = {}
    for path, st in _cache.items():
        out[path] = {
            "confirmed_vary": list(st.vary) if st.vary else [],
            "generation": st.generation,
            "entry_count": len(st.entries),
            "stats": dict(st.stats),
            "entries": [
                {
                    "variant_key": list(e.variant_key),
                    "etag": e.etag,
                    "vary": list(e.vary),
                    "age_seconds": round(time.time() - e.stored_at, 2),
                    "stored_at": e.stored_at,
                    "max_age": e.max_age,
                    "fresh": time.time() - e.stored_at < e.max_age,
                    "generation": e.fetch_generation,
                }
                for e in st.entries.values()
            ],
        }
    return {"paths": out}


@app.post("/_cache/clear")
async def cache_clear() -> dict:
    _cache.clear()
    return {"ok": True}


@app.post("/_cache/age")
async def cache_age(request: Request) -> dict:
    """测试辅助：把所有条目的 stored_at 向前拨，模拟 max-age 到期。"""
    payload = await request.json()
    seconds = float(payload.get("seconds", 0))
    touched = 0
    for st in _cache.values():
        for entry in st.entries.values():
            entry.stored_at -= seconds
            touched += 1
    return {"ok": True, "entries_aged": touched}


def _select_entry(st: PathState, request: Request, identity_key: tuple[str, ...]) -> "Entry | None":
    """按当前 Vary 规则为请求选取可用缓存条目。"""
    key = _variant_key(st.vary or (), request.headers)

    def _usable(candidate: "Entry | None") -> "Entry | None":
        if candidate is None or candidate.fetch_generation != st.generation:
            return None
        # 规则尚未确认的冷启动期，暂存在空键下的条目只能被同协商身份复用；
        # 一旦确认规则（即使规则为空），同键即同表示，允许跨身份共享
        # （例如 Vary 切到只按 Accept 后，不同语言共享一份正文）
        if st.vary is None and candidate.variant_key == () and candidate.identity != identity_key:
            return None
        return candidate

    entry = _usable(st.entries.get(key))
    if entry is None and st.vary is None:
        entry = _usable(st.entries.get(()))
    return entry


def _commit_upstream(st: PathState, request: Request, identity_key: tuple[str, ...],
                     upstream: httpx.Response, entry: "Entry | None",
                     epoch_before: int | None, snapshot_generation: int,
                     snapshot_epoch: int | None):
    """回源成功后，在路径锁内原子完成规则确认/切换/写入，返回 (Response, 是否错误)。"""
    # 注意：upstream 缺少 Vary 头时按空规则处理，不能回退到旧规则
    if "vary" in upstream.headers:
        origin_vary = _parse_vary(upstream.headers.get("vary"))
    else:
        origin_vary = ()
    epoch_header = upstream.headers.get("x-rule-epoch")
    epoch_known = epoch_header is not None
    origin_epoch = int(epoch_header) if epoch_known else 0
    current_vary = st.vary
    first_confirmation = st.confirmed_epoch is None
    vary_changed = current_vary is not None and set(origin_vary) != set(current_vary)

    # 本次回源期间是否有别的请求确认过新规则。只有此时低世代回包才是
    # “迟到的旧规则回包”；若期间无人确认，低世代只能说明源站世代回退
    # （例如重启 / 管理端复位），应按新答复重新确认，不能永久卡在旧规则。
    committed_during_fetch = (
        st.generation != snapshot_generation or st.confirmed_epoch != snapshot_epoch
    )
    # 发起复验时选中的条目若已被并发的规则切换清出当前世代，就不能再
    # 把这次 304 的元数据写回它（否则等于刷新/复活一个已失效条目）。
    revalidated_entry_stale = entry is not None and entry.fetch_generation != st.generation

    # 旧规则回包：本次回源期间别的请求已确认过新规则，而这份回包仍带着
    # 旧规则（世代更低，或同世代却给出不同的 Vary）。它只能答复原请求，
    # 不能恢复旧规则、也不能刷新当前条目。若期间无人确认，低世代只说明
    # 源站世代回退（如重启 / 管理端复位），应按新答复重新确认，避免卡死。
    stale_rule_reply = (
        (epoch_known and not first_confirmation and committed_during_fetch and (
            origin_epoch < st.confirmed_epoch
            or (origin_epoch == st.confirmed_epoch and vary_changed)
        ))
        or (revalidated_entry_stale and upstream.status_code == 304)
    )

    # 在途切换：回源发起前的源站世代与响应世代不同，且新世代已被别的请求提交，
    # 或代理 generation 在回源期间已被递增。顺序切换的发现者不在此列。
    epoch_mismatch = epoch_before is not None and epoch_before != origin_epoch
    inflight_switch = stale_rule_reply or (
        epoch_mismatch
        and (st.generation != snapshot_generation or st.confirmed_epoch == origin_epoch)
    )

    if inflight_switch:
        # 结果只给原请求：不改动已确认规则与任何缓存条目
        if upstream.status_code == 304:
            if entry is None:
                st.stats["origin_errors"] += 1
                return Response(b"origin 304 without cached representation\n", status_code=502,
                                headers={"X-Cache": "ORIGIN_ERROR", "Cache-Control": "no-store"}), True
            return _build_response(entry, "STALE_INFLIGHT_304", private=True), False
        passthrough = Entry(
            body=upstream.content,
            headers={k.lower(): v for k, v in upstream.headers.items()},
            etag=upstream.headers.get("etag", ""),
            vary=origin_vary,
            variant_key=(),
            identity=identity_key,
            stored_at=time.time(),
            max_age=_parse_max_age(upstream.headers.get("cache-control")),
            fetch_generation=snapshot_generation,
        )
        return _build_response(passthrough, "INFLIGHT_RULE_SWITCHED", private=True), False

    if vary_changed:
        # 旧规则条目整体失效，确认新规则
        st.entries.clear()
        st.generation += 1
        st.vary = origin_vary
        st.confirmed_epoch = origin_epoch
        st.stats["vary_switches"] += 1
        new_key = _variant_key(origin_vary, request.headers)
    elif first_confirmation:
        # 首次确认 Vary：之前的冷条目用空键暂存，按真实规则重新入键
        st.vary = origin_vary
        st.confirmed_epoch = origin_epoch
        new_key = _variant_key(origin_vary, request.headers)
        if epoch_before is not None and epoch_before != origin_epoch:
            # 冷启动在途期间源站已切换过规则：同样记一次规则世代切换
            st.generation += 1
            st.stats["vary_switches"] += 1
        rekeyed = {}
        for old_entry in st.entries.values():
            # 用产生该条目的请求协商身份重算键（响应头里没有 Accept 等请求头）
            req_headers = {"accept": old_entry.identity[0], "accept-language": old_entry.identity[1]}
            real_key = _variant_key(origin_vary, req_headers)
            old_entry.variant_key = real_key
            old_entry.vary = origin_vary
            rekeyed[real_key] = old_entry
        st.entries = rekeyed
    else:
        # 同规则下，世代号可能因“切走又切回”而推进，保持同步
        st.confirmed_epoch = origin_epoch
        new_key = _variant_key(origin_vary, request.headers)

    if upstream.status_code == 304:
        if entry is None:
            st.stats["origin_errors"] += 1
            return Response(b"origin 304 without cached representation\n", status_code=502,
                            headers={"X-Cache": "ORIGIN_ERROR", "Cache-Control": "no-store"}), True
        # 沿用发起复验时选中的正文，仅刷新元数据；规则切换由本次 304 发现时，
        # 正文按这次确认的规则重新入键，并按这次答复的 max-age 重新计时
        entry.stored_at = time.time()
        entry.max_age = _parse_max_age(upstream.headers.get("cache-control"), entry.max_age)
        if etag_new := upstream.headers.get("etag"):
            entry.etag = etag_new
            entry.headers["etag"] = etag_new
        entry.vary = origin_vary
        entry.fetch_generation = st.generation
        if vary_changed:
            entry.variant_key = new_key
            st.entries[new_key] = entry
            st.stats["fetches"] += 1
            return _build_response(entry, "MISS_NEW_RULE"), False
        st.stats["revalidated"] += 1
        return _build_response(entry, "REVALIDATED_304"), False

    # 200：写入（或替换）缓存
    revalidation = current_vary is not None and entry is not None
    new_entry = Entry(
        body=upstream.content,
        headers={k.lower(): v for k, v in upstream.headers.items()},
        etag=upstream.headers.get("etag", ""),
        vary=origin_vary,
        variant_key=new_key,
        identity=identity_key,
        stored_at=time.time(),
        max_age=_parse_max_age(upstream.headers.get("cache-control")),
        fetch_generation=st.generation,
    )
    st.entries[new_key] = new_entry
    if revalidation and not vary_changed:
        st.stats["revalidated"] += 1
        status = "REVALIDATED_200"
    else:
        st.stats["fetches"] += 1
        status = "MISS_NEW_RULE" if vary_changed or first_confirmation else "MISS"
    return _build_response(new_entry, status), False


@app.api_route("/{full_path:path}", methods=["GET"])
async def proxy_get(full_path: str, request: Request) -> Response:
    path = request.url.path
    st = await _get_path_state(path)

    # 协商身份与 Vary 规则、generation 均无关：
    # - 不同身份（不同 Accept / 语言）使用不同条件变量，可并行回源；
    # - 同身份共享同一条件变量，严格 single-flight。
    identity_key = _variant_key(("accept", "accept-language"), request.headers)
    async with st.path_lock:
        sync = st.identity_sync.setdefault(identity_key, IdentitySync())

    # 基准规则世代在 single-flight 判定之外探测
    epoch_before = await _probe_origin_epoch()

    async with sync.cond:
        # 已有同身份回源在途：等待其完成，然后重新检查缓存（可能已被填充）
        if sync.in_flight:
            await sync.cond.wait_for(lambda: not sync.in_flight)
            entry = _select_entry(st, request, identity_key)
            if entry is not None and time.time() - entry.stored_at < entry.max_age:
                st.stats["hits"] += 1
                return _build_response(entry, "HIT")
            # 前序回源失败或未产生可用条目：落到下方自行回源
        sync.in_flight = True

    snapshot_generation = st.generation
    snapshot_epoch = st.confirmed_epoch
    try:
        entry = _select_entry(st, request, identity_key)
        if entry is not None and time.time() - entry.stored_at < entry.max_age:
            async with sync.cond:
                sync.in_flight = False
                sync.cond.notify_all()
            st.stats["hits"] += 1
            return _build_response(entry, "HIT")

        # 需要回源（冷请求或过期复验）
        etag = entry.etag if entry is not None else None
        try:
            upstream = await _fetch_from_origin(request, path, etag)
        except httpx.HTTPError as exc:
            async with sync.cond:
                sync.in_flight = False
                sync.cond.notify_all()
            st.stats["origin_errors"] += 1
            kind = "revalidation" if etag else "fetch"
            return Response(
                f"origin {kind} failed: {exc}\n".encode(),
                status_code=502,
                headers={"X-Cache": "ORIGIN_ERROR", "Cache-Control": "no-store"},
            )

        if upstream.status_code not in (200, 304):
            async with sync.cond:
                sync.in_flight = False
                sync.cond.notify_all()
            st.stats["origin_errors"] += 1
            return Response(
                f"origin returned status {upstream.status_code}\n".encode(),
                status_code=502,
                headers={"X-Cache": "ORIGIN_ERROR", "Cache-Control": "no-store"},
            )

        async with st.path_lock:
            response, _ = _commit_upstream(
                st, request, identity_key, upstream, entry, epoch_before,
                snapshot_generation, snapshot_epoch,
            )
        return response
    finally:
        async with sync.cond:
            sync.in_flight = False
            sync.cond.notify_all()
