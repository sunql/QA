"""Neo4j 写目标闸门（P3b 发布镜像写路径专用）。

**为什么需要**：Milvus 侧有 `scripts/milvus_write_target_guard.py`，**Neo4j 侧
原本没有**（D22 recon 事实 C-7）——而 Neo4j **也没有自动备份** ⇒ 误写不可逆。

**为什么默认拒绝一切非测试目标**：只挡生产端口（7687）是**不够**的——生产实例
换个端口、换个主机名、走个隧道就绕过去了。默认改成**白名单**：只有测试实例端口
才放行，其它目标必须显式 `allowProdWrite=True`。

**为什么放在基础设施层而不是脚本里**：闸门只写在 CLI 脚本里 ⇒ 库函数
（`neo4j_mirror_client` 的写函数）依然是「裸写」，将来 D23 的物化 Job 直接调库
就会继承这个洞。**闸门必须对所有调用方生效** ⇒ 放在 infrastructure，脚本复用它。

端口口径：按 **驱动实际使用的端口**判定（`urlparse().port`；缺省 bolt 端口
按 7687）—— 不能用字符串切分（`rsplit(":", 1)` 会把 `bolt://localhost:7687/`
的端口读成 `7687/` ⇒ 闸门失效）。
"""

from __future__ import annotations

from urllib.parse import urlparse

# 测试实例端口（本地 compose：`qa-neo4j-test` → 7688；生产 `qa-neo4j` → 7687）
TEST_NEO4J_PORTS: frozenset[int] = frozenset({7688})

# 缺省端口（与 neo4j 驱动一致：bolt/neo4j 协议默认 7687）
DEFAULT_NEO4J_PORT = 7687


def effectiveNeo4jPort(uri: str) -> int | None:
    """取驱动**实际**连的端口；解析不出来返回 None（⇒ 按「非测试目标」拒绝）。

    `bolt://neo4j`（没写端口）⇒ 驱动连 7687 ⇒ 这里返回 7687。
    """
    text = uri or ""
    if "://" not in text:
        text = f"//{text}"
    try:
        parsed = urlparse(text)
    except ValueError:
        return None
    if parsed.port is None:
        return DEFAULT_NEO4J_PORT
    return parsed.port


def describeNeo4jTarget(uri: str) -> str:
    """目标的人类可读描述（`neo4j://host:port`）。

    ⚠️ **口令一律不出现**：URI 里的 userinfo 整段丢弃（只取 hostname/port）——
    报告会被贴进工单/日志，不能带凭据。与 `databaseNameOf()` 同口径。
    """
    text = uri or ""
    if "://" not in text:
        text = f"//{text}"
    try:
        parsed = urlparse(text)
    except ValueError:
        parsed = None
    host = (parsed.hostname if parsed else None) or "localhost"
    port = effectiveNeo4jPort(uri)
    return f"neo4j://{host}:{port}"


def assertSafeNeo4jTarget(uri: str, *, allowProdWrite: bool = False) -> None:
    """写图前的目标闸：**默认只准写测试实例**。

    抛 `ValueError`（库层语义）⇒ 脚本侧可自行转成 `SystemExit` 退出码。
    """
    if allowProdWrite:
        return
    port = effectiveNeo4jPort(uri)
    if port in TEST_NEO4J_PORTS:
        return
    raise ValueError(
        f"拒绝向 {describeNeo4jTarget(uri)} 写镜像：镜像写路径没有写目标闸门、"
        "图库也没有自动备份（误写不可逆）。默认只允许写测试实例"
        f"（端口 {sorted(TEST_NEO4J_PORTS)}，例如 NEO4J_URI=bolt://localhost:7688）；"
        "确需写其它目标请显式加 --allow-prod-write。"
    )


__all__ = [
    "DEFAULT_NEO4J_PORT",
    "TEST_NEO4J_PORTS",
    "assertSafeNeo4jTarget",
    "describeNeo4jTarget",
    "effectiveNeo4jPort",
]
