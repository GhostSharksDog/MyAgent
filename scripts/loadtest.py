r"""并发压测：给出真实的 P50 / P95 / P99 与 QPS。

【为什么这件事不能省】

P5 阶段要写简历条目，而简历上的数字**必须能追溯到某次实测**。
"性能提升了 3 倍"这种话在面试里经不起追问：怎么测的？多大数据量？
并发多少？—— 答不上来的数字比不写更糟，它会让面试官怀疑其它数字也是编的。

【为什么自己写而不用 k6 / wrk】
本机没有装它们，而且这里要测的是**我们自己服务的延迟分布**，
不是标准的 HTTP 压测。自写脚本能做一件 k6 做起来麻烦的事：
**同时发起干扰负载**（比如一边压检索一边跑重建索引），
把"CPU 争抢"这个拆分动机变成可测量的现象。

【必须诚实标注的两件事】
1. 这是**单机本地**数据，没有网络延迟、没有跨可用区、没有真实流量分布。
   写进文档时必须一起写清楚，否则就是在暗示一个不存在的生产能力。
2. 压测机与被测服务在同一台机器上，**压测进程自己会争抢 CPU**。
   并发越高，这个偏差越大。所以并发度不能无限往上加 ——
   本脚本会把这个限制写进输出。
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent


def percentile(sorted_values: list[float], q: float) -> float:
    """线性插值分位数。

    【为什么不能用"取第 N 个"的简单写法】
    用 `values[int(len * q)]` 在小样本上会严重失真：
    20 个样本算 P99 会直接取到最大值，而 P95 和 P99 可能落在同一个点上。
    P95/P99 恰恰是最需要精确的那两个数 —— 它们决定"用户会不会觉得卡"。
    """
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = q * (len(sorted_values) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = pos - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


def summarize(samples: list[float], wall_seconds: float, errors: int) -> dict[str, float]:
    s = sorted(samples)
    return {
        "count": len(s),
        "errors": errors,
        "qps": len(s) / wall_seconds if wall_seconds > 0 else 0.0,
        "p50": percentile(s, 0.50),
        "p90": percentile(s, 0.90),
        "p95": percentile(s, 0.95),
        "p99": percentile(s, 0.99),
        "max": s[-1] if s else 0.0,
        "mean": statistics.fmean(s) if s else 0.0,
    }


async def _worker(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    payload: dict | None,
    deadline: float,
    latencies: list[float],
    counter: list[int],
) -> int:
    """单个并发 worker：在 deadline 之前不停地发请求。

    【为什么用"跑到时间到"而不是"每人固定发 N 次"】
    固定次数的问题是：慢的请求发得少、快的请求发得多，
    于是样本被快请求占满，**P99 被系统性地低估**。
    按时间跑能让所有 worker 同时停止，样本比例更接近真实流量。
    """
    errors = 0
    while time.perf_counter() < deadline:
        started = time.perf_counter()
        try:
            if method == "POST":
                resp = await client.post(url, json=payload)
            else:
                resp = await client.get(url)
            # 只把 5xx 算错误：4xx 是调用方问题（比如限流 429），
            # 把它算进错误率会掩盖真实的服务端故障率。
            if resp.status_code >= 500:
                errors += 1
        except httpx.HTTPError:
            errors += 1
        finally:
            latencies.append((time.perf_counter() - started) * 1000)
            counter[0] += 1
    return errors


async def run_load(
    method: str,
    url: str,
    *,
    concurrency: int,
    duration: float,
    payload: dict | None = None,
    label: str = "",
) -> dict[str, float]:
    latencies: list[float] = []
    counter = [0]
    deadline = time.perf_counter() + duration

    limits = httpx.Limits(max_connections=concurrency + 4, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(timeout=30.0, limits=limits) as client:
        started = time.perf_counter()
        results = await asyncio.gather(
            *[
                _worker(client, method, url, payload, deadline, latencies, counter)
                for _ in range(concurrency)
            ]
        )
        wall = time.perf_counter() - started

    stats = summarize(latencies, wall, sum(results))
    if label:
        print(f"\n--- {label} ---")
    print(
        f"  并发 {concurrency}，持续 {wall:.1f}s，样本 {int(stats['count'])}，"
        f"错误 {int(stats['errors'])}"
    )
    print(
        f"  QPS {stats['qps']:.1f}  |  "
        f"P50 {stats['p50']:.1f}ms  P90 {stats['p90']:.1f}ms  "
        f"P95 {stats['p95']:.1f}ms  P99 {stats['p99']:.1f}ms  "
        f"max {stats['max']:.1f}ms"
    )
    return stats


async def _reindex_spammer(base_url: str, deadline: float, done: list[int]) -> None:
    """在压测期间持续触发索引重建，制造 CPU 争抢。

    【这是拆分动机的实验】
    RAG 的检索与索引重建都是纯 CPU 计算。让它们同时发生，
    就能看到"CPU 争抢"到底是多大一个问题 —— 用数字，不是用形容词。
    """
    async with httpx.AsyncClient(timeout=120.0) as client:
        while time.perf_counter() < deadline:
            try:
                await client.post(f"{base_url}/reindex")
                done[0] += 1
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.05)


async def main() -> int:
    ap = argparse.ArgumentParser(description="Legacy 并发压测")
    ap.add_argument("--api-url", default="http://127.0.0.1:8000")
    ap.add_argument("--rag-url", default="http://127.0.0.1:8001")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--duration", type=float, default=8.0)
    ap.add_argument(
        "--contention",
        action="store_true",
        help="额外跑一轮：一边压检索一边持续重建索引，量化 CPU 争抢",
    )
    ap.add_argument("--json-out", default="", help="把结果写入 JSON 文件")
    args = ap.parse_args()

    print("=" * 68)
    print("Legacy 压测（单机本地，非生产能力数据）")
    print("=" * 68)
    print(
        "\n⚠ 压测进程与被测服务在同一台机器上，并发越高、测出来的延迟越偏悲观。\n"
        "  这是本机环境的固有限制，写入文档时必须一并说明。"
    )

    results: dict[str, object] = {}

    # ---------- 连通性 ----------
    async with httpx.AsyncClient(timeout=10.0) as probe:
        try:
            h = (await probe.get(f"{args.api_url}/healthz")).json()
            print(f"\nAPI:  {h.get('model')} | rag_backend={h.get('rag_backend')}")
        except httpx.HTTPError as exc:
            print(f"无法连接 API {args.api_url}：{exc}")
            print("请先启动：.\\scripts\\dev.ps1 serve")
            return 1
        try:
            rh = (await probe.get(f"{args.rag_url}/healthz")).json()
            print(f"RAG:  chunks={rh.get('chunks')} mode={rh.get('mode')} reranker={rh.get('reranker')}")
        except httpx.HTTPError as exc:
            print(f"无法连接 RAG {args.rag_url}：{exc}")
            print("请先启动：.\\scripts\\dev.ps1 rag")
            return 1

    # ---------- 1. 框架基线 ----------
    results["healthz"] = await run_load(
        "GET",
        f"{args.api_url}/healthz",
        concurrency=args.concurrency,
        duration=args.duration,
        label="1. /healthz —— 纯框架开销基线（不碰检索、不碰模型）",
    )

    # ---------- 2. 检索延迟 ----------
    results["retrieve"] = await run_load(
        "POST",
        f"{args.rag_url}/context",
        concurrency=args.concurrency,
        duration=args.duration,
        payload={"query": "分布式 消息队列 缓存 经验", "k": 4, "max_chars": 3000},
        label="2. RAG /context —— 混合召回 + 词法重排（CPU 密集）",
    )

    # ---------- 3. CPU 争抢实验 ----------
    if args.contention:
        print("\n" + "=" * 68)
        print("3. CPU 争抢实验：检索与索引重建同时进行")
        print("=" * 68)
        print(
            "  这是「为什么要把 RAG 拆成独立服务」的直接证据：\n"
            "  重建索引是纯 CPU 计算，与在线检索争抢同一份 CPU 时间片。"
        )
        deadline = time.perf_counter() + args.duration + 3
        rebuilt = [0]
        spammer = asyncio.create_task(_reindex_spammer(args.rag_url, deadline, rebuilt))

        results["retrieve_under_reindex"] = await run_load(
            "POST",
            f"{args.rag_url}/context",
            concurrency=args.concurrency,
            duration=args.duration,
            payload={"query": "分布式 消息队列 缓存 经验", "k": 4, "max_chars": 3000},
            label="   检索延迟（重建索引进行中）",
        )
        spammer.cancel()
        try:
            await spammer
        except asyncio.CancelledError:
            pass
        print(f"\n  实测重建索引触发了 {rebuilt[0]} 次，说明干扰负载确实生效")

        base = results["retrieve"]
        cont = results["retrieve_under_reindex"]
        assert isinstance(base, dict) and isinstance(cont, dict)
        p95_ratio = cont["p95"] / base["p95"] if base["p95"] else 0
        print(
            f"\n  P95 放大倍数：{p95_ratio:.1f}×  "
            f"（{base['p95']:.1f}ms → {cont['p95']:.1f}ms）"
        )

    print("\n" + "=" * 68)
    print("说明：以上是单机本地数据。并发与延迟的关系受压测进程自身 CPU 占用影响，")
    print("      不能直接当作生产容量结论 —— 生产数据需要独立压测机与真实网络。")
    print("=" * 68)

    if args.json_out:
        import json

        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {
                    "environment": "single-machine-local",
                    "concurrency": args.concurrency,
                    "duration_seconds": args.duration,
                    "results": results,
                    "caveat": "压测进程与服务同机，并发越高延迟越偏悲观；非生产能力数据",
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\n结果已写入 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
