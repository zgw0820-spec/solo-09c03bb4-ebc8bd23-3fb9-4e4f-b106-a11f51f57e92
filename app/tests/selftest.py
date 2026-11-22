"""端到端自测：覆盖 Vary 缓存、304 复验、规则切换失效、并发切换与回源失败。

运行方式：
  Docker:   docker compose run --rm self-test
  本地裸跑: PROXY_BASE=http://127.0.0.1:18000 ORIGIN_BASE=http://127.0.0.1:18080 \
            python -m app.tests.selftest
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

import httpx

PROXY = os.environ.get("PROXY_BASE", "http://127.0.0.1:18000").rstrip("/")
ORIGIN = os.environ.get("ORIGIN_BASE", "http://127.0.0.1:18080").rstrip("/")

JSON = "application/json"
MD = "text/markdown"
ZH = "zh-CN"
EN = "en"

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


async def reset(c: httpx.AsyncClient) -> None:
    await c.post(f"{ORIGIN}/admin/reset")
    await c.post(f"{PROXY}/_cache/clear")
    # 确认两侧均已归零，避免跨用例污染统计
    origin = await origin_state(c)
    assert origin["counters"]["requests"] == 0 and origin["inflight"] == 0, origin
    cache = (await c.get(f"{PROXY}/_cache/inspect")).json()
    assert cache["paths"] == {}, cache


async def origin_state(c: httpx.AsyncClient) -> dict:
    return (await c.get(f"{ORIGIN}/admin/state")).json()


async def test_content_negotiation_and_hit(c: httpx.AsyncClient) -> None:
    print("[1] 按 Accept / Accept-Language 协商，第二次命中缓存")
    await reset(c)
    r1 = await c.get(f"{PROXY}/releases", headers={"Accept": JSON, "Accept-Language": ZH})
    r2 = await c.get(f"{PROXY}/releases", headers={"Accept": JSON, "Accept-Language": EN})
    r3 = await c.get(f"{PROXY}/releases", headers={"Accept": MD, "Accept-Language": ZH})
    check("zh/json 返回中文", "发布说明" in r1.text and r1.headers["content-type"].startswith(JSON))
    check("en/json 返回英文", "Release Notes" in r2.text)
    check("zh/md 返回 markdown", r3.headers["content-type"].startswith("text/markdown") and "# 发布说明" in r3.text)

    r1b = await c.get(f"{PROXY}/releases", headers={"Accept": JSON, "Accept-Language": ZH})
    check("相同变体二次请求 HIT", r1b.headers.get("x-cache") == "HIT", r1b.headers.get("x-cache"))
    check("HIT 正文与首次一致", r1b.content == r1.content)

    st = await origin_state(c)
    check("命中不回源（源站计数=3）", st["counters"]["requests"] == 3, str(st["counters"]))

    inspect = (await c.get(f"{PROXY}/_cache/inspect")).json()
    entries = inspect["paths"]["/releases"]["entries"]
    check("按 Vary 缓存 3 个变体", len(entries) == 3, str(entries))
    check("已确认 Vary 为两个头", inspect["paths"]["/releases"]["confirmed_vary"] == ["accept", "accept-language"])


async def test_revalidation_304(c: httpx.AsyncClient) -> None:
    print("[2] max-age 到期带 ETag 复验，304 沿用旧正文")
    await reset(c)
    headers = {"Accept": JSON, "Accept-Language": ZH}
    first = await c.get(f"{PROXY}/releases", headers=headers)
    etag = first.headers["etag"]
    check("首请求带 ETag", bool(etag))

    # 等待超过源站 max-age=30s 不现实，这里直接把缓存条目时间戳改老：
    # 通过代理内部没有管理接口，改为等待源站 max-age 过期的缩短方案：
    # 源站 max-age 固定 30s，因此测试中用内部状态端点确认 fresh 标记，
    # 再调用一个“时光快进”辅助端点（见 proxy 的 /_cache/age）。
    aged = await c.post(f"{PROXY}/_cache/age", json={"seconds": 31})
    check("缓存快进成功", aged.status_code == 200, aged.text)

    second = await c.get(f"{PROXY}/releases", headers=headers)
    check("复验走 304", second.headers.get("x-cache") == "REVALIDATED_304", second.headers.get("x-cache"))
    check("304 沿用原正文", second.content == first.content)
    check("复验后恢复新鲜", (await c.get(f"{PROXY}/releases", headers=headers)).headers.get("x-cache") == "HIT")
    st = await origin_state(c)
    check("源站收到条件请求并回 304", st["counters"]["status304"] >= 1, str(st["counters"]))


async def test_vary_switch_invalidation(c: httpx.AsyncClient) -> None:
    print("[3] Vary 规则切换：旧规则条目整体失效，按新规则缓存")
    await reset(c)
    h_zh = {"Accept": JSON, "Accept-Language": ZH}
    h_en = {"Accept": JSON, "Accept-Language": EN}
    await c.get(f"{PROXY}/releases", headers=h_zh)
    await c.get(f"{PROXY}/releases", headers=h_en)
    before = (await c.get(f"{PROXY}/_cache/inspect")).json()["paths"]["/releases"]
    check("切换前 2 个语言变体", before["entry_count"] == 2 and before["generation"] == 0)

    # 规则只能在回源时确认：先让缓存过期，复验才会带着新 Vary 回来
    await c.post(f"{PROXY}/_cache/age", json={"seconds": 31})
    await c.post(f"{ORIGIN}/admin/vary/accept")
    r = await c.get(f"{PROXY}/releases", headers=h_zh)
    check("复验发现 Vary 变化，按新规则回源", r.headers.get("x-cache") == "MISS_NEW_RULE", r.headers.get("x-cache"))
    check("accept 模式语言被忽略，回英文", "Release Notes" in r.text)

    after = (await c.get(f"{PROXY}/_cache/inspect")).json()["paths"]["/releases"]
    check("旧条目已清空，只剩 1 条", after["entry_count"] == 1, str(after["entries"]))
    check("generation 递增", after["generation"] == 1, str(after["generation"]))
    check("新 Vary 只剩 accept", after["confirmed_vary"] == ["accept"], str(after["confirmed_vary"]))

    # 新规则下，不同 Accept-Language 映射到同一个变体键 -> HIT 同一份英文正文
    r2 = await c.get(f"{PROXY}/releases", headers={"Accept": JSON, "Accept-Language": ZH})
    check("新规则下语言不参与键，直接 HIT", r2.headers.get("x-cache") == "HIT", r2.headers.get("x-cache"))
    check("HIT 正文为英文", r2.content == r.content)


async def test_parallel_languages(c: httpx.AsyncClient) -> None:
    print("[4] 不同语言并行回源（single-flight 只合并同变体）")
    await reset(c)
    await c.post(f"{ORIGIN}/admin/delay/500")

    async def get(lang: str) -> httpx.Response:
        return await c.get(f"{PROXY}/releases", headers={"Accept": JSON, "Accept-Language": lang})

    t0 = time.monotonic()
    zh1, zh2, en1 = await asyncio.gather(get(ZH), get(ZH), get(EN))
    elapsed = time.monotonic() - t0
    check("两个语言都成功", zh1.status_code == 200 and en1.status_code == 200)
    check("同语言并发只回源一次（正文一致）", zh1.content == zh2.content)
    check("两种语言正文不同", zh1.content != en1.content)
    check("并行而非串行（耗时 < 1.3s）", elapsed < 1.3, f"{elapsed:.2f}s")
    # 等待在途回源全部结束再核对计数，避免统计窗口竞态
    for _ in range(100):
        st = await origin_state(c)
        if st["inflight"] == 0:
            break
        await asyncio.sleep(0.05)
    check("源站只收到 2 个请求（同变体合并）", st["counters"]["requests"] == 2, str(st["counters"]))
    await c.post(f"{ORIGIN}/admin/delay/0")


async def test_concurrent_switch_inflight(c: httpx.AsyncClient) -> None:
    print("[5] 并发切换（冷回源）：切换瞬间两个在途回源，负方答复只给原请求不进缓存")
    await reset(c)
    await c.post(f"{ORIGIN}/admin/delay/800")
    h_zh = {"Accept": JSON, "Accept-Language": ZH}
    h_en = {"Accept": JSON, "Accept-Language": EN}

    async def get(headers: dict) -> httpx.Response:
        return await c.get(f"{PROXY}/releases", headers=headers)

    # 两个不同语言的冷请求并行在途（不同身份键，代理并行放行）
    t_zh = asyncio.create_task(get(h_zh))
    t_en = asyncio.create_task(get(h_en))
    # 精确等待两个回源都进入源站延迟窗口，再切换规则
    w = await c.get(f"{ORIGIN}/admin/wait_inflight/2")
    check("两个回源确认在途", w.json().get("ok") is True, w.text)
    await c.post(f"{ORIGIN}/admin/vary/accept")  # 在途期间切换规则
    r_zh, r_en = await asyncio.gather(t_zh, t_en)

    statuses = {r_zh.headers.get("x-cache"), r_en.headers.get("x-cache")}
    check("两个在途请求都成功", r_zh.status_code == 200 and r_en.status_code == 200)
    check("恰好一个成为新规则条目，另一个只答复原请求",
          statuses == {"MISS_NEW_RULE", "INFLIGHT_RULE_SWITCHED"}, str(statuses))
    # 新规则 accept-only 下源站统一回英文；关键是两个请求都拿到了完整答复，
    # 且负方（INFLIGHT_RULE_SWITCHED）的结果没有写入共享缓存
    loser = r_zh if r_zh.headers.get("x-cache") == "INFLIGHT_RULE_SWITCHED" else r_en
    winner = r_en if loser is r_zh else r_zh
    check("负方答复只给原请求（完整英文正文）", "Release Notes" in loser.text and loser.status_code == 200)
    check("胜方按新规则缓存（英文正文）", "Release Notes" in winner.text)

    inspect = (await c.get(f"{PROXY}/_cache/inspect")).json()["paths"]["/releases"]
    check("新缓存只有 1 条（负方在途结果未写入）", inspect["entry_count"] == 1, str(inspect["entry_count"]))
    check("缓存条目属于新 generation", inspect["entries"][0]["generation"] == 1,
          str(inspect["entries"][0]["generation"]))
    await c.post(f"{ORIGIN}/admin/delay/0")


async def test_concurrent_switch_during_revalidation(c: httpx.AsyncClient) -> None:
    print("[6] 并发切换（304 复验）：同一英文表示在规则切换后复验命中 304，旧正文只给复验方")
    await reset(c)
    await c.post(f"{ORIGIN}/admin/delay/800")
    h_zh = {"Accept": JSON, "Accept-Language": ZH}
    h_en = {"Accept": JSON, "Accept-Language": EN}
    # 两种语言都预热（旧规则 both）
    await c.get(f"{PROXY}/releases", headers=h_zh)
    warm_en = await c.get(f"{PROXY}/releases", headers=h_en)
    await c.post(f"{PROXY}/_cache/age", json={"seconds": 31})

    async def get(headers: dict) -> httpx.Response:
        return await c.get(f"{PROXY}/releases", headers=headers)

    # 两个过期变体并行复验；en 在 accept 模式下表示不变 -> 304；zh 表示消失 -> 200
    t_zh = asyncio.create_task(get(h_zh))
    t_en = asyncio.create_task(get(h_en))
    w = await c.get(f"{ORIGIN}/admin/wait_inflight/2")
    check("两个复验回源确认在途", w.json().get("ok") is True, w.text)
    await c.post(f"{ORIGIN}/admin/vary/accept")
    r_zh, r_en = await asyncio.gather(t_zh, t_en)

    check("两个复验请求都拿到 200 答复", r_zh.status_code == 200 and r_en.status_code == 200)

    winner = r_zh if r_zh.headers.get("x-cache") == "MISS_NEW_RULE" else r_en
    loser = r_en if winner is r_zh else r_zh
    check("恰好一个请求提交新规则（MISS_NEW_RULE）",
          winner.headers.get("x-cache") == "MISS_NEW_RULE", winner.headers.get("x-cache"))
    check("胜方按新规则缓存英文正文", "Release Notes" in winner.text)
    check("负方标记为在途切换（304 或 200 透传，均不进缓存）",
          loser.headers.get("x-cache") in ("STALE_INFLIGHT_304", "INFLIGHT_RULE_SWITCHED"),
          loser.headers.get("x-cache"))
    if loser.headers.get("x-cache") == "STALE_INFLIGHT_304":
        # en 复验命中 304：沿用发起复验时选中的英文旧正文
        check("304 负方沿用自己的旧正文", loser.content == warm_en.content)
    else:
        # zh 的表示在新规则下不存在，源站回 200：负方仍拿到完整答复
        check("200 负方拿到完整英文正文", "Release Notes" in loser.text)

    inspect = (await c.get(f"{PROXY}/_cache/inspect")).json()["paths"]["/releases"]
    check("缓存中只有新规则的 1 条", inspect["entry_count"] == 1, str(inspect["entry_count"]))
    check("缓存已切换为新 generation", inspect["generation"] == 1, str(inspect["generation"]))
    check("胜方条目属于新 generation", inspect["entries"][0]["generation"] == 1)
    # 新规则下再用中文头请求：语言不参与键，直接命中同一份英文缓存
    follow = await c.get(f"{PROXY}/releases", headers=h_zh)
    check("切换后中文头请求命中英文缓存", follow.headers.get("x-cache") == "HIT" and "Release Notes" in follow.text,
          follow.headers.get("x-cache"))
    await c.post(f"{ORIGIN}/admin/delay/0")


async def test_origin_failure_keeps_cache(c: httpx.AsyncClient) -> None:
    print("[7] 回源失败报错，已确认缓存状态保持不变")
    await reset(c)
    headers = {"Accept": JSON, "Accept-Language": ZH}
    good = await c.get(f"{PROXY}/releases", headers=headers)
    before = (await c.get(f"{PROXY}/_cache/inspect")).json()["paths"]["/releases"]

    await c.post(f"{ORIGIN}/admin/fail/1")
    # 冷变体（en）回源 -> 502
    bad = await c.get(f"{PROXY}/releases", headers={"Accept": JSON, "Accept-Language": EN})
    check("回源失败返回 502", bad.status_code == 502, str(bad.status_code))
    check("带错误标记头", bad.headers.get("x-cache") == "ORIGIN_ERROR")

    after = (await c.get(f"{PROXY}/_cache/inspect")).json()["paths"]["/releases"]
    check("缓存条目数不变", after["entry_count"] == before["entry_count"] == 1)
    check("generation 不变", after["generation"] == before["generation"] == 0)

    # 已缓存且新鲜的变体仍可正常命中
    hit = await c.get(f"{PROXY}/releases", headers=headers)
    check("已确认缓存仍可命中", hit.headers.get("x-cache") == "HIT" and hit.content == good.content)

    # 过期复验时回源失败：保留旧正文缓存不动
    await c.post(f"{PROXY}/_cache/age", json={"seconds": 31})
    await c.post(f"{ORIGIN}/admin/fail/1")
    bad2 = await c.get(f"{PROXY}/releases", headers=headers)
    check("复验失败也返回 502", bad2.status_code == 502)
    after2 = (await c.get(f"{PROXY}/_cache/inspect")).json()["paths"]["/releases"]
    check("失败后旧条目仍在", after2["entry_count"] == 1 and after2["entries"][0]["etag"] == good.headers["etag"])


async def main() -> int:
    async with httpx.AsyncClient(timeout=30) as c:
        for base in (PROXY, ORIGIN):
            try:
                r = await c.get(f"{base}/health")
                r.raise_for_status()
            except Exception as exc:  # noqa: BLE001
                print(f"服务不可达 {base}: {exc}")
                return 2
        await test_content_negotiation_and_hit(c)
        await test_revalidation_304(c)
        await test_vary_switch_invalidation(c)
        await test_parallel_languages(c)
        await test_concurrent_switch_inflight(c)
        await test_concurrent_switch_during_revalidation(c)
        await test_origin_failure_keeps_cache(c)

    print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
