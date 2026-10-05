"""会话存储基准：量 SQL 后端与内存后端的延迟分布。

《为什么这个脚本值得留在仓库里》

`app/session/sqlite_store.py` 里写着一段"写操作为什么要加一把进程内锁"的说明，
里面有三组数字。**数字必须能被复现**，否则它和编造没有区别 ——
这个项目在压测脚本（`scripts/loadtest.py`）上已经立过这条规矩。

《它量的和 loadtest.py 不一样的东西》

`loadtest.py` 量的是 HTTP 层的吞吐（/healthz、RAG /context）。
这里**剥掉 HTTP**，直接量存储层：这样才能把"SQLite 本身慢"与
"我们的用法慢"分开 —— 实测中 SQLite 单次写只要 0.7ms，
而并发下尾延迟曾经到 282ms，问题完全在用法（每次操作自己开事务 + 抢文件锁）。

用法：
    python scripts/bench_session_store.py            # SQL（默认，用临时库文件）
    python scripts/bench_session_store.py --memory   # 内存后端（对照组）
    python scripts/bench_session_store.py -n 200 -c 32

输出是 P50 / P95 / max 与总耗时。**并发下的 P95 才是用户能感觉到的那一档** ——
只看平均值会漏掉"少数请求慢得离谱"这件事（那正是加锁前的情况）。
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "services" / "api"))

from app.session.sqlite_store import SqlSessionStore
from app.session.store import InMemorySessionStore, SessionStore


def report(label: str, durations: list[float]) -> None:
    ms = sorted(d * 1000 for d in durations)
    p50 = statistics.median(ms)
    p95 = ms[max(0, int(len(ms) * 0.95) - 1)]
    print(
        f"  {label:24s} P50 {p50:8.2f}ms  P95 {p95:8.2f}ms  max {ms[-1]:8.2f}ms  "
        f"合计 {sum(durations):6.2f}s"
    )


async def measure(store: SessionStore, count: int, concurrency: int) -> None:
    warm = await store.create(title="warm")
    await store.append_turn(warm.id, "u", "a")
    semaphore = asyncio.Semaphore(concurrency)

    async def timed(coro: object) -> float:
        started = time.perf_counter()
        await coro  # type: ignore[misc]
        return time.perf_counter() - started

    async def limited(coro_factory: object) -> float:
        async with semaphore:
            return await timed(coro_factory())  # type: ignore[misc]

    # ---- create ----
    sequential = [await timed(store.create(title=f"s{i}")) for i in range(count)]
    report("顺序 create", sequential)

    concurrent = await asyncio.gather(
        *(limited(lambda i=i: store.create(title=f"c{i}")) for i in range(count))
    )
    report(f"并发 create（{concurrency}）", list(concurrent))

    # ---- get ----
    sequential_get = [await timed(store.get(warm.id)) for _ in range(count)]
    report("顺序 get", sequential_get)

    # ---- append_turn ----
    sequential_append = [
        await timed(store.append_turn(warm.id, f"su{i}", f"sa{i}", tokens=1))
        for i in range(count)
    ]
    report("顺序 append_turn", sequential_append)

    concurrent_append = await asyncio.gather(
        *(
            limited(
                lambda i=i: store.append_turn(warm.id, f"cu{i}", f"ca{i}", tokens=1)
            )
            for i in range(count)
        )
    )
    report(f"并发 append_turn（{concurrency}）", list(concurrent_append))

    final = await store.get(warm.id)
    assert final is not None
    expected = 1 + count + count  # 预热 1 + 顺序 count + 并发 count
    status = (
        "全部保留"
        if final.turn_count == expected
        else f"只保留 {final.turn_count}/{expected}"
    )
    print(f"\n  并发追加后轮次数：{final.turn_count}（期望 {expected}）→ {status}")


async def main() -> int:
    parser = argparse.ArgumentParser(description="会话存储延迟基准")
    parser.add_argument("-n", "--count", type=int, default=60)
    parser.add_argument("-c", "--concurrency", type=int, default=16)
    parser.add_argument("--memory", action="store_true", help="量内存后端（对照组）")
    args = parser.parse_args()

    print(f"操作数 {args.count}，并发 {args.concurrency}\n")
    if args.memory:
        store: SessionStore = InMemorySessionStore()
        print("后端：memory（进程内字典，无持久化）")
    else:
        tmp = Path(tempfile.mkdtemp(prefix="session-bench-"))
        url = f"sqlite+aiosqlite:///{(tmp / 'bench.db').as_posix()}"
        store = SqlSessionStore.from_url(url)
        await store.ensure_ready()  # type: ignore[attr-defined]
        print(f"后端：{store.backend}（{url.split('///')[-1]}）")

    try:
        await measure(store, args.count, args.concurrency)
    finally:
        await store.aclose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
