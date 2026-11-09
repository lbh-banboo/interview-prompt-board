"""M10-5 联网检索延迟评测：50 题批量跑 web_supplements。

默认 Mock 传输 + 假时钟（SIMULATED，不联网、不真等）。--real 需环境变量 DEEPSEEK_API_KEY，
且必须用 --transport module:factory 注入真实传输（本仓库不内置真实联网实现）。
达标口径：逐次，首条要点 elapsed <= 1500ms 的题数 / 总题数；P50/P95 仅作诊断。
"""
import argparse
import asyncio
import importlib
import json
import math
import os
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.search import SearchClient, web_supplements  # noqa: E402

TOPICS = ["Redis 持久化", "TCP 三次握手", "HTTP 缓存", "MySQL 索引", "Kafka 分区", "Docker 网络", "线程池",
          "垃圾回收", "B+ 树", "CAP 定理"]
ASKS = ["的原理是什么", "有哪些常见的优缺点", "和同类方案有什么区别", "在生产环境如何调优", "常见面试题怎么回答"]
FIRST_DEADLINE_MS = 1500


def synthetic_questions(n=50):
    return [f"{t}{a}" for a in ASKS for t in TOPICS][:n]


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def mock_transport(clock, seed=0):
    """脚本化延迟：多数 0.3~1.4s，少量 1.5~3s，约 4% 超过 8s。"""
    rng = random.Random(seed)

    async def transport(query, *, api_key, timeout_s):
        r = rng.random()
        clock.t += rng.uniform(8.5, 9.5) if r < 0.04 else rng.uniform(1.5, 3.0) if r < 0.2 else rng.uniform(0.3, 1.4)
        return [{"title": f"{query} 官方文档", "url": "https://docs.example.org/a",
                 "snippet": f"{query}的详细说明与要点，来自官方文档。"},
                {"title": f"{query} 百科", "url": "https://zh.wikipedia.org/wiki/x",
                 "snippet": f"{query}的百科解释与背景介绍。"}]
    return transport


def pct(values, p):
    if not values:
        return None
    s = sorted(values)
    return s[max(0, math.ceil(p / 100 * len(s)) - 1)]


async def run_eval(questions, make_client, clock, *, vocab=None, total_timeout_s=8):
    firsts, totals, timeouts = [], [], 0
    for q in questions:
        client = make_client()
        start, first, timed_out = clock(), None, False
        async for s in web_supplements(q, vocab, client, clock=clock, total_timeout_s=total_timeout_s):
            if s.kind == "point" and first is None:
                first = s.elapsed_ms
            timed_out |= s.kind == "timeout"
        totals.append(int((clock() - start) * 1000))
        timeouts += timed_out
        if first is not None:
            firsts.append(first)
    ok = sum(1 for f in firsts if f <= FIRST_DEADLINE_MS)
    return {"n": len(questions), "first_p50_ms": pct(firsts, 50), "first_p95_ms": pct(firsts, 95),
            "total_p50_ms": pct(totals, 50), "total_p95_ms": pct(totals, 95),
            "timeouts": timeouts, "no_first": len(questions) - len(firsts),
            "per_run_pass_rate": ok / len(questions) if questions else 0.0}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--real", action="store_true")
    ap.add_argument("--transport", help="module:factory，返回真实 transport")
    ap.add_argument("-n", type=int, default=50)
    a = ap.parse_args(argv)
    qs = synthetic_questions(a.n)
    if a.real:
        key = os.environ.get("DEEPSEEK_API_KEY")
        if not key or not a.transport:
            print("--real 需要环境变量 DEEPSEEK_API_KEY 与 --transport module:factory", file=sys.stderr)
            return 2
        mod, fn = a.transport.split(":")
        import time
        clock, transport, label = time.monotonic, getattr(importlib.import_module(mod), fn)(), "REAL"
    else:
        key, clock = None, FakeClock()
        transport, label = mock_transport(clock), "SIMULATED"
    report = asyncio.run(run_eval(qs, lambda: SearchClient(transport, api_key=key), clock))
    print(f"[{label}] M10 联网检索延迟评测 (n={report['n']})")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
