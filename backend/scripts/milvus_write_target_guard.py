"""Milvus 写入目标闸门（维护脚本写 Milvus 前必过）。

⚠️ 为什么要单独成文件：本仓**没有**可复用的 Milvus 侧「白名单」件。
``app/tests/milvus_isolation.py`` 的 ``guardIsolatedDatabase()`` 是**测试**用的
deny-by-default 闸门 —— 它直接禁掉 ``default`` 库；而维护脚本要的是
「默认禁、显式 opt-in 放行」。两者语义不同，**不能互相复用**。

为什么必须拦 ``default``
-------------------------
``default`` 库就是**生产本体集合**（2026-11-07 实测：``ontology_class_embeddings``
50 行 / ``ontology_property_embeddings`` 3743 行 / ``ontology_metric_embeddings`` 0 行），
而 Milvus **没有自动备份**（``Harness/rules/数据存储防护.md``：「仅备份 PostgreSQL…，
Neo4j / Milvus 当前没有自动备份脚本」）⇒ 误写**不可逆**。
本仓已有前科：2026-09-30 一次裸 pytest 把线上向量删空。

PG 侧闸门**不另写一份** —— 复用仓库既有的
``scripts/kpi_metric_db_guard.assertSafeTarget``（库名白名单 + prod 需显式 opt-in）
与 ``scripts/backfill_id_mapping.assertSafeWriteTarget``（再加 APP_ENV 一路）。

本模块**不 import app.***：``app.config`` 在导入期读环境变量，
而库名解析必须发生在设置 ``DATABASE_URL`` 之后才能 import app —— 同
``scripts/kpi_metric_db_guard.py`` 的理由。
"""
from __future__ import annotations

from urllib.parse import urlparse

# Milvus 的生产库名。pymilvus 不回读该值，配置为空时归一到它
# （见 app/infrastructure/milvus_client.py::getDbName）。
PROD_MILVUS_DB_NAME = "default"

# 默认端口：与 milvus_client._connect 的兜底一致。
DEFAULT_MILVUS_PORT = 19530


def assertSafeMilvusTarget(dbName: str, *, allowProdWrite: bool = False) -> str:
    """校验 Milvus 目标库；返回归一后的库名。

    - 库名是生产库 ``default`` 且未显式 ``allowProdWrite`` ⇒ ``SystemExit``；
    - 其它库名（测试库 / 隔离库）免 opt-in。

    与 ``guardIsolatedDatabase()`` 的区别：那个是**测试**闸门（直接禁），
    这个是**维护脚本**闸门（默认禁、显式放行）。
    """
    name = (dbName or "").strip() or PROD_MILVUS_DB_NAME
    if name == PROD_MILVUS_DB_NAME and not allowProdWrite:
        raise SystemExit(
            f"拒绝执行：Milvus 目标库 {name!r} 是生产库"
            "（生产本体集合所在，且 Milvus 无自动备份 ⇒ 误写不可逆）。"
            "确认后加 --allow-prod（并先完成当日 PG 备份）。"
        )
    return name


def describeMilvusTarget(uri: str, dbName: str) -> str:
    """目标的人类可读描述（``host:port`` + 库名）。

    ⚠️ **口令一律不出现**：URI 里的 userinfo 被整段丢弃（只取 hostname/port），
    与 ``milvus_client._connect()`` 的解析口径一致。
    """
    parsed = urlparse(uri if uri and "://" in uri else f"//{uri or ''}")
    host = parsed.hostname or "localhost"
    port = parsed.port or DEFAULT_MILVUS_PORT
    return f"milvus://{host}:{port}/{dbName or PROD_MILVUS_DB_NAME}"
