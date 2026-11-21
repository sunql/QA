"""M7 判据 6：知识服务 P95 延迟压测（设计稿 §11 性能目标）。

目标（设计稿 §11，**需真实数据压测校准**）：
- 实体精确查询 P95 **< 1s**（`/procurement/knowledge/entities/search`）
- 常规图谱查询 P95 **< 2s**（`/procurement/knowledge/graph/neighbors`）
- RAG 检索   P95 **< 3s**（`/procurement/knowledge/query` channels=["RAG"]、
  `/procurement/knowledge/documents/search`）
- 四通道综合查询：**无设计目标**，仅作参考观测（`--info`）

测量口径（诚实的单客户端延迟，不是吞吐）：
1. **串行**逐次请求（不并发）⇒ 得到的是单请求延迟分布；并发吞吐另测。
2. **预热**默认 3 次不计入（JIT/连接池/缓存冷启动不该算进 P95）。
3. 统计 **p50/p95/p99/max/mean + 错误数**；错误响应（≥400）不进入延迟样本，
   单独计数（否则「快速失败」会伪造成「很快」）。
4. 绕开系统代理直连（macOS `urllib` 默认吃系统代理——本机常开着代理，
   会把 localhost 请求也绕出去）。
5. **降级≠快**：`/knowledge/query` 某通道降级（`DEGRADED`，如 embedding 服务
   不可用）时仍返回 **200 且极快** ⇒ 必须校验 `routeResults[].status`，
   有降级的样本判**无效**（否则「挂着的那条路」会被记成性能最好）。
6. **限流**：知识接口是 `30/minute`（`knowledge_facade.KNOWLEDGE_QUERY_LIMIT`）
   ⇒ 默认每次间隔 **3.0s**（`--delay`，即 20/分钟，留余量；预热也走间隔）。被限流（429）的请求**不计入延迟样本**、
   也不算「性能不达标」，而是判**样本无效**（退出码 2）——否则「快速失败」
   会伪造成「很快」。

用法::

    cd backend && uv run python scripts/bench_knowledge_p95.py \\
        --base-url http://localhost:8000 --username admin --password '***'

    # 输出 JSON 证据（供验收包归档）
    ... --json-out docs/update/1118/M7/evidence/knowledge-p95.json

退出码：0 = 全部达标；1 = 有超目标项（`--no-fail` 可只看数）；2 = 参数/登录失败。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

#: 设计稿 §11 的性能目标（秒）。
TARGET_ENTITY_P95 = 1.0
TARGET_GRAPH_P95 = 2.0
TARGET_RAG_P95 = 3.0


@dataclass(frozen=True)
class Case:
    """一个被测端点。"""

    name: str
    method: str
    path: str
    body: dict[str, Any] | None
    targetP95: float | None  # None = 仅观测（无设计目标）


CASES: tuple[Case, ...] = (
    Case("实体精确查询", "GET", "/api/v1/procurement/knowledge/entities/search?q=Q1630&limit=5", None, TARGET_ENTITY_P95),
    Case("图谱查询(1跳)", "GET", "/api/v1/procurement/knowledge/graph/neighbors?entityType=Supplier&entityKey=D135&maxHops=1", None, TARGET_GRAPH_P95),
    Case("图谱查询(2跳)", "GET", "/api/v1/procurement/knowledge/graph/neighbors?entityType=Supplier&entityKey=D135&maxHops=2", None, TARGET_GRAPH_P95),
    Case("RAG检索(向量)", "POST", "/api/v1/procurement/knowledge/query",
         {"question": "找出可以供应不锈钢板的供应商", "channels": ["RAG"], "topK": 5}, TARGET_RAG_P95),
    Case("文档检索", "POST", "/api/v1/procurement/knowledge/documents/search",
         {"question": "询价必须邀请几家供应商", "topK": 5}, TARGET_RAG_P95),
    Case("四通道综合(观测)", "POST", "/api/v1/procurement/knowledge/query",
         {"question": "找出可以供应不锈钢板的供应商", "channels": ["GRAPH", "SQL", "RAG", "METRIC"], "topK": 5}, None),
)


def _opener() -> urllib.request.OpenerDirector:
    """**无代理** opener：直连测试栈（本机系统代理会把 localhost 也绕出去）。"""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


#: 通道状态取值（与 `domain/knowledge_schemas.py` 同源）。
ROUTE_STATUS_DEGRADED = "DEGRADED"


def _request(
    opener: urllib.request.OpenerDirector,
    baseUrl: str,
    case: Case,
    token: str,
    timeout: float = 60.0,
) -> tuple[float, int, tuple[str, ...]]:
    """发一次请求，返回 (耗时秒, HTTP 状态码, 降级通道名)。

    耗时含网络+服务端处理。降级通道仅对 `/knowledge/query` 有意义：
    `routeResults[].status == "DEGRADED"` 表示该路**没干活就返回了**。
    """
    data = None
    headers = {"Authorization": f"Bearer {token}"}
    if case.body is not None:
        data = json.dumps(case.body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        f"{baseUrl}{case.path}", data=data, headers=headers, method=case.method
    )
    start = time.perf_counter()
    body = b""
    try:
        with opener.open(req, timeout=timeout) as resp:
            body = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:
        exc.read()
        status = exc.code
    elapsed = time.perf_counter() - start
    return elapsed, status, _degradedChannels(body)


def _degradedChannels(body: bytes) -> tuple[str, ...]:
    """从响应体里取降级通道名（解析失败 ⇒ 空，不影响延迟统计）。"""
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return ()
    routes = payload.get("routeResults") if isinstance(payload, dict) else None
    if not isinstance(routes, list):
        return ()
    return tuple(
        str(item.get("channel"))
        for item in routes
        if isinstance(item, dict) and item.get("status") == ROUTE_STATUS_DEGRADED
    )


def _percentile(samples: list[float], pct: float) -> float:
    """最近秩法分位数（样本少时比插值更保守）。"""
    if not samples:
        return 0.0
    ordered = sorted(samples)
    idx = min(len(ordered) - 1, max(0, int(round(pct / 100 * len(ordered) + 0.5)) - 1))
    return ordered[idx]


def _login(opener: urllib.request.OpenerDirector, baseUrl: str, username: str, password: str) -> str:
    req = urllib.request.Request(
        f"{baseUrl}/api/v1/auth/login",
        data=json.dumps({"username": username, "password": password}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with opener.open(req, timeout=15) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"登录失败（HTTP {exc.code}）：{exc.read()[:200]!r}") from None
    token = payload.get("accessToken") or payload.get("token")
    if not token:
        raise SystemExit(f"登录响应里没有 accessToken：{payload}")
    return str(token)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--iterations", type=int, default=30, help="每个端点的正式样本数")
    parser.add_argument("--warmup", type=int, default=3, help="预热次数（不计入统计）")
    parser.add_argument("--delay", type=float, default=3.0, help="每次请求间隔秒（默认 3.0 ⇒ 20/分钟，留出 30/分钟限流的余量）")
    parser.add_argument("--json-out", default=None)
    parser.add_argument("--no-fail", action="store_true", help="超目标也不退非零")
    args = parser.parse_args()

    opener = _opener()
    token = _login(opener, args.base_url, args.username, args.password)

    results: list[dict[str, Any]] = []
    failures: list[str] = []
    invalid: list[str] = []
    for case in CASES:
        for index in range(max(0, args.warmup)):
            # 预热同样走间隔：预热不计样本，但**算限流配额**（30/分钟滑动窗口）
            if index > 0 and args.delay > 0:
                time.sleep(args.delay)
            _request(opener, args.base_url, case, token)
        samples: list[float] = []
        errors = 0
        rateLimited = 0
        degraded = 0
        errorCodes: list[int] = []
        for index in range(args.iterations):
            if index > 0 and args.delay > 0:
                time.sleep(args.delay)
            elapsed, status, degradedChannels = _request(
                opener, args.base_url, case, token
            )
            if status == 429:
                rateLimited += 1
                errorCodes.append(status)
                continue
            if status >= 400:
                errors += 1
                errorCodes.append(status)
                continue
            if degradedChannels:
                # 降级路径没干活 ⇒ 它的「快」不能算进延迟（见文件头口径 5）
                degraded += 1
                continue
            samples.append(elapsed)
        row = {
            "name": case.name,
            "path": case.path,
            "targetP95": case.targetP95,
            "samples": len(samples),
            "errors": errors,
            "rateLimited": rateLimited,
            "degraded": degraded,
            "errorCodes": sorted(set(errorCodes)),
            "mean": round(statistics.fmean(samples), 4) if samples else None,
            "p50": round(_percentile(samples, 50), 4),
            "p95": round(_percentile(samples, 95), 4),
            "p99": round(_percentile(samples, 99), 4),
            "max": round(max(samples), 4) if samples else None,
        }
        row["valid"] = (
            rateLimited == 0
            and degraded == 0
            and len(samples) >= max(5, args.iterations // 2)
        )
        row["pass"] = (
            None
            if case.targetP95 is None or not row["valid"]
            else (row["p95"] < case.targetP95 and errors == 0)
        )
        results.append(row)
        if not row["valid"]:
            invalid.append(
                f"{case.name}: 样本无效（限流 {rateLimited} 次 / 降级 {degraded} 次"
                f" / 有效 {len(samples)} 条）"
                " ⇒ 调大 --delay，或先修通道降级"
            )
        elif row["pass"] is False:
            failures.append(
                f"{case.name}: P95={row['p95']}s ≥ 目标 {case.targetP95}s"
                + (f"（错误 {errors} 次）" if errors else "")
            )

    # —— 输出 ——
    header = f"{'用例':<18}{'样本':>4}{'错误':>4}{'P50':>8}{'P95':>8}{'P99':>8}{'目标':>7} 结果"
    print(header)
    print("-" * len(header) * 2)
    for row in results:
        if not row["valid"]:
            verdict = "⚠️样本无效"
        else:
            verdict = "—" if row["pass"] is None else ("✅" if row["pass"] else "❌")
        target = "-" if row["targetP95"] is None else f"{row['targetP95']}s"
        print(
            f"{row['name']:<18}{row['samples']:>4}{row['errors']:>4}"
            f"{row['p50']:>8.3f}{row['p95']:>8.3f}{row['p99']:>8.3f}{target:>7} {verdict}"
        )

    evidence = {
        "generatedAt": datetime.now(UTC).isoformat(),
        "baseUrl": args.base_url,
        "iterations": args.iterations,
        "warmup": args.warmup,
        "targets": {
            "entityP95": TARGET_ENTITY_P95,
            "graphP95": TARGET_GRAPH_P95,
            "ragP95": TARGET_RAG_P95,
        },
        "results": results,
        "failures": failures,
        "invalid": invalid,
    }
    if args.json_out:
        from pathlib import Path

        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n证据已写入 {out}")

    if invalid:
        print("\n样本无效（限流）：")
        for line in invalid:
            print(" -", line)
        sys.exit(2)
    if failures:
        print("\n未达标：")
        for line in failures:
            print(" -", line)
        if not args.no_fail:
            sys.exit(1)


if __name__ == "__main__":
    main()
