"""本体发现底座真实数据验证脚本（WP-ONT-AI A1-7）。

在**真实数据**上验证 A1 的两件底座，而不是在 fixture 上自说自话：

1. **快照采集面（写）**：对本地 **MySQL 演示源**（`asset_demo` 库的真实表）做结构
   内省 ⇒ 规范化 ⇒ 内容寻址 ⇒ 落 `metadata_snapshot`，并逐项断言：
   - 落库行的 `schema_hash` 与**独立重算**的哈希一致（口径可复现）；
   - **二次采集复用既有行**（`reused=True`、行数不增）⇒ 内容寻址幂等；
   - **只增不改**：二次采集后先前行内容与时间戳**逐字未变**（用**原生 SQL 读**
     比对，不看 ORM 身份映射 —— 同一个实例比较等于没比）；
   - 快照 JSON 的形状（`schemaVersion` / 表序 / 列序）符合规范化约定。
2. **差异面（只读）**：对**已发布 ACTIVE** 的语义发布与当前治理表做差异分析，
   断言两次计算结果**完全相同**（纯函数确定性），并打印破坏性分类与风险分级。

**两个面必须分库跑**（脚本用两个模式）：

- 采集面要写库 ⇒ 目标库必须是**隔离测试库族** `qa_metadata_test` / `qa_metadata_test_*`
  （默认闸门）；写正式库 `qa_metadata` 需显式 `--allow-prod-write`（本脚本只 INSERT
  自己那两张新表，**无 UPDATE / DELETE** ⇒ 不破坏既有数据；放行时会打印**将写入的行数**）。
- 差异面**零写**（`computeOntologyDiff` 只读）⇒ 可直接对**有真实 ACTIVE 发布**的
  治理库跑（`--diff-only`），该库不必是测试库。

**连接地址覆盖**（`--connect-host/--connect-port`）：宿主机跑时容器内网名
（`host.docker.internal`）可能不可解析 ⇒ 换成宿主机可达地址。该覆盖**同时作用
于独立重算与采集路径**（否则两个哈希可能来自两个不同的库），且**不改库里的数据
源配置**（只改本次内省的连接地址）。

**报告里不出现完整主机地址**（主机属内网拓扑信息）⇒ 只留掩码。

跑法（宿主机，backend/ 下）：

    # ① 采集面（写隔离测试库；MySQL 演示源在容器内叫 host.docker.internal，
    #    宿主机跑要用 --connect-host 换成宿主机可达地址）
    DATABASE_URL='postgresql+asyncpg://qa_user:***@127.0.0.1:5434/qa_metadata_test' \\
        .venv/bin/python -m scripts.ontology_discovery_realdata \\
        --datasource-id 9 --connect-host 127.0.0.1

    # ② 差异面（只读治理库）
    DATABASE_URL='postgresql+asyncpg://qa_user:***@127.0.0.1:5433/qa_metadata' \\
        .venv/bin/python -m scripts.ontology_discovery_realdata --diff-only

末行恒为 ``REALDATA RESULT: PASS`` 或 ``FAIL``。

⚠️ **前置条件**：目标库必须已有该数据源行（`discovery_source.datasource_id` 对
`data_source.id` 是 FK）—— 集成测试的清库夹具会把 `data_source` 一起清掉 ⇒ 跑
本脚本前需把演示源行补回目标库。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# 允许从 backend/ 直接运行（与同级脚本一致）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.domain.discovery_models import DiscoverySource  # noqa: E402
from app.domain.models import DataSource  # noqa: E402
from app.services.audit_service import AuditService  # noqa: E402
from app.services.discovery_diff_service import computeOntologyDiff  # noqa: E402
from app.services.discovery_snapshot_service import (  # noqa: E402
    ConnectOverride,
    DiscoverySnapshotService,
    computeSnapshotHash,
    normalizePolicy,
    normalizeSnapshot,
    withConnectOverride,
)
from app.services.schema_introspection_service import (  # noqa: E402
    SchemaIntrospectionService,
)

# 目标库守卫**复用** KPI 工具链的同一份实现（安全审查 L-4）：本脚本不再
# 自带一份库名白名单 / 正则 —— 两处各写一套 ⇒ 迟早漂移。
from scripts.kpi_metric_db_guard import (  # noqa: E402
    PROD_DB_NAME,
    assertSafeTarget,
    databaseNameOf,
    maskDsn,
)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 本脚本写入元数据库时登记的 actor（审计口径，不冒充真人）
_SCRIPT_ACTOR = "script:ontology_discovery_realdata"

# 默认发现源名（幂等登记：同名已存在则复用，不报冲突）
_DEFAULT_SOURCE_NAME = "asset-demo-mysql 发现源"

# 默认演示数据源名（--datasource-id 未给时按名字找；找不到再按类型挑）
_DEFAULT_DATASOURCE_NAME = "asset-demo-mysql"

# 快照 JSON 的结构版本（与发现服务常量一致）
_EXPECTED_SCHEMA_VERSION = "v1"

# 报告里最多列出的变更条数（全部计数仍完整）
_MAX_REPORTED_CHANGES = 20

# 目标库白名单与写库闸**复用** `scripts/kpi_metric_db_guard`（见上方 import）：
# 白名单 = `qa_metadata`（prod 同源，需显式 opt-in）+ `qa_metadata_test*`（隔离测试库）。
# 本脚本**不再**自带第二份库名正则（安全审查 L-4）。

# 放行正式库时告知操作者的**写入规模**：discovery_source 1 行 + metadata_snapshot 1 行。
_MAX_INSERT_ROWS = 2

# 差异面在**无 ACTIVE 发布**时不算失败（未发布是正常状态），只如实报告
_RESULT_MARK = "REALDATA RESULT"


@dataclass(frozen=True)
class Check:
    """一条断言结果（不可变）。"""

    name: str
    ok: bool
    detail: str = ""


class VerifyFailure(Exception):
    """一项断言不成立。"""


def _check(results: list[Check], name: str, ok: bool, detail: str = "") -> bool:
    results.append(Check(name=name, ok=ok, detail=detail))
    mark = "PASS" if ok else "FAIL"
    suffix = f" — {detail}" if detail else ""
    print(f"  [{mark}] {name}{suffix}")
    return ok


def _silenceSqlEcho() -> None:
    """关掉 SQL 回显（dev 下 engine ``echo=True``），免得把 JSON 报告淹没。

    SQLAlchemy 的 echo 由**实例 logger** 决定：``InstanceLogger`` 直接按 echo
    映射成 INFO 并调 ``logger._log()``，**绕过** logger 层级 ⇒ 改 logger level
    不管用，只能摘掉它自动挂上的默认 handler。
    """

    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    engineLogger = logging.getLogger("sqlalchemy.engine.Engine")
    for handler in list(engineLogger.handlers):
        engineLogger.removeHandler(handler)


def _maskHost(host: str | None) -> str | None:
    """主机地址掩码（主机属内网拓扑信息，不进报告）。"""

    if not host:
        return None
    return f"{host[0]}***"


def _hostOf(dsn: str) -> str | None:
    """从 DSN 取 `host[:port]`（失败路径要掩码的主机之一）。"""

    withoutQuery = dsn.split("?", 1)[0].split("#", 1)[0].strip()
    if "://" not in withoutQuery:
        return None
    hostAndPath = withoutQuery.split("://", 1)[1].rsplit("@", 1)[-1]
    host = hostAndPath.partition("/")[0]
    return host or None


def _redact(text: str, secrets: list[str | None]) -> str:
    """把已知的敏感字面量（主机 / DSN）从文本里抹掉（安全审查 L-5）。

    失败路径的异常消息常带连接地址（`Connection refused ('127.0.0.1', 3306)`）
    ⇒ 报告里不该出现完整主机地址。DSN 走 `maskDsn`（口令打码），主机走
    `_maskHost`。
    """

    redacted = text
    for secret in secrets:
        if not secret:
            continue
        if "://" in secret:
            redacted = redacted.replace(secret, maskDsn(secret))
        else:
            redacted = redacted.replace(secret, str(_maskHost(secret)))
    return redacted


def _requireDatabaseUrlEnv() -> str:
    """要求操作者**显式**指定 DATABASE_URL（不猜、不用默认值）。

    守卫本身判定的是**引擎实际连的** DSN（`settings.databaseUrl`），不是操作者
    口口相传的字符串 ⇒ 不会出现「嘴上说测试库、实际连生产库」。
    """

    envValue = os.environ.get("DATABASE_URL", "").strip()
    if not envValue:
        raise SystemExit(
            "未设置 DATABASE_URL 环境变量。本脚本要连元数据库，请显式指定：\n"
            "  DATABASE_URL='postgresql+asyncpg://qa_user:***@127.0.0.1:5434/qa_metadata_test' \\\n"
            "      .venv/bin/python -m scripts.ontology_discovery_realdata"
        )
    return envValue


def _typeNameOf(ds: DataSource) -> str:
    """数据源类型的可读名（枚举 / 纯字符串都兼容）。"""

    return str(getattr(ds.type, "value", ds.type))


def _guardDatabaseUrl(databaseUrl: str, *, allowProdWrite: bool = False) -> str:
    """写库闸（脚本侧的具名入口）。

    判定逻辑**不在本文件**——复用 KPI 工具链的同一份守卫
    （`scripts.kpi_metric_db_guard.assertSafeTarget`）⇒ 白名单只有一份，
    两边行为不会漂移。本函数只是给它起个脚本内的名字，便于单测直接断言。
    """

    return assertSafeTarget(databaseUrl, allowProdWrite=allowProdWrite)


def _warnProdWrite(dbName: str) -> None:
    """放行正式库时**先说清会写几行**（安全审查 L-6：操作者要知情）。"""

    print(
        f"  [WARN] 已放行正式库 {dbName!r} 的写入："
        f"本次最多 INSERT {_MAX_INSERT_ROWS} 行"
        "（discovery_source 1 行 + metadata_snapshot 1 行），无 UPDATE / DELETE。"
    )


async def _resolveDatasource(
    session: AsyncSession,
    datasourceId: int | None,
    datasourceName: str,
) -> DataSource:
    """定位演示数据源：显式 id 优先，其次按名字，最后按「活动的 mysql 源」。"""

    if datasourceId is not None:
        row = await session.get(DataSource, datasourceId)
        if row is None:
            raise VerifyFailure(
                f"数据源 {datasourceId} 在目标库不存在。"
                "（发现源外键指向 data_source.id ⇒ 目标库必须已有该行；"
                "集成测试的清库夹具会清掉 data_source，跑脚本前需补回。）"
            )
        return row

    named = await session.scalar(
        select(DataSource).where(DataSource.name == datasourceName)
    )
    if named is not None:
        return named

    fallback = await session.scalar(
        select(DataSource)
        .where(DataSource.type == "mysql", DataSource.is_active.is_(True))
        .order_by(DataSource.id)
        .limit(1)
    )
    if fallback is None:
        raise VerifyFailure(
            f"未找到演示数据源（名字 {datasourceName!r} 不存在，也没有活动的 mysql 源）"
        )
    return fallback


def _withConnectOverride(
    ds: DataSource, connectHost: str | None, connectPort: int | None
) -> DataSource:
    """返回**新的** DataSource（不改原对象），按需替换连接地址。

    实现已收敛到服务层 `withConnectOverride`（单一事实源）⇒ 这里只做参数转译。
    """

    return withConnectOverride(
        ds, ConnectOverride(host=connectHost, port=connectPort)
    )


async def _readSnapshotRow(session: AsyncSession, snapshotId: int) -> dict[str, Any]:
    """**绕开 ORM identity map** 的原生读（只看数据库真值）。

    代码审查 H-3：ORM 在同一 session 里返回的是**同一个实例** ⇒ 即便库里那行
    被改过，「二次采集前后逐字相同」也照样成立 ⇒ 空断言（假绿）。这里用原生
    SQL 读，拿到的永远是库里此刻的值。
    """

    result = await session.execute(
        text(
            "SELECT schema_hash, snapshot_json::text AS snapshot_json_text, "
            "table_count, column_count, captured_at "
            "FROM metadata_snapshot WHERE id = :snapshotId"
        ),
        {"snapshotId": snapshotId},
    )
    row = result.mappings().first()
    if row is None:
        raise VerifyFailure(f"快照 {snapshotId} 不存在（原生读）")
    return {
        "schemaHash": row["schema_hash"],
        "snapshotJson": json.loads(row["snapshot_json_text"]),
        "tableCount": row["table_count"],
        "columnCount": row["column_count"],
        "capturedAt": row["captured_at"],
    }


async def _ensureSource(
    session: AsyncSession,
    service: DiscoverySnapshotService,
    *,
    name: str,
    datasourceId: int,
) -> DiscoverySource:
    """幂等登记发现源：同名已存在 ⇒ 复用（脚本可重复跑）。"""

    existing = await session.scalar(
        select(DiscoverySource).where(DiscoverySource.name == name)
    )
    if existing is not None:
        print(f"  [INFO] 发现源已存在，复用 id={existing.id}")
        return existing
    source = await service.registerSource(
        session,
        name=name,
        datasourceId=datasourceId,
        owner="WP-ONT-AI A1-7",
        accessPolicy={"mode": "READ_ONLY"},
        note="本体发现真实数据验证脚本登记",
        createdBy=_SCRIPT_ACTOR,
    )
    # 治理写动作必须留痕（安全审查 L-6）：脚本绕过了 API 层 ⇒ API 那条
    # 「审计随写动作」的纪律在脚本路径上没人执行。
    await AuditService().record(
        session,
        entity_type="discovery_source",
        entity_id=source.id,
        action="CREATE",
        actor=_SCRIPT_ACTOR,
        after={
            "name": source.name,
            "datasourceId": source.datasource_id,
            "owner": source.owner,
            "status": source.status,
        },
    )
    await session.commit()
    print(f"  [INFO] 发现源已登记 id={source.id}")
    return source


async def _recomputeHash(
    effective: DataSource, source: DiscoverySource
) -> tuple[dict[str, Any], str]:
    """独立重算：脚本自己内省一次 ⇒ 规范化 ⇒ 哈希（用来核对落库行的哈希）。

    ⚠️ 必须与服务层**同一把尺子**：`schema_owner`（Oracle 的命名空间）取自发现源
    的 `access_policy`。漏传 ⇒ 独立重算落在另一个命名空间 ⇒ 两个哈希必然不同，
    「口径可复现」这条断言就成了假断言（代码审查 H-2）。
    """

    policyOwner = normalizePolicy(source.access_policy).get("schema_owner")
    tables = await SchemaIntrospectionService().introspect(effective, owner=policyOwner)
    recomputed = normalizeSnapshot(tables)
    return recomputed, computeSnapshotHash(recomputed)


async def _auditSnapshot(session: AsyncSession, result: Any) -> None:
    """把脚本的快照写入落 `audit_log`（安全审查 L-6）。

    动作口径与 API 层一致：内容未变 ⇒ 复用既有行（**没有新建**）⇒ 记 `REUSE`
    而不是 `CREATE`（记 CREATE 会让「新增了多少行」的台账虚高）。
    """

    await AuditService().record(
        session,
        entity_type="metadata_snapshot",
        entity_id=result.snapshotId,
        action="REUSE" if result.reused else "CREATE",
        actor=_SCRIPT_ACTOR,
        after={
            "sourceId": result.sourceId,
            "schemaHash": result.schemaHash,
            "tableCount": result.tableCount,
            "columnCount": result.columnCount,
            "reused": result.reused,
        },
    )


def _snapshotShapeIssues(snapshot: dict[str, Any]) -> list[str]:
    """检查快照 JSON 的形状是否符合规范化约定（返回问题清单，空 = 合规）。"""

    issues: list[str] = []
    if snapshot.get("schemaVersion") != _EXPECTED_SCHEMA_VERSION:
        issues.append(
            f"schemaVersion={snapshot.get('schemaVersion')!r} ≠ {_EXPECTED_SCHEMA_VERSION!r}"
        )
    tables = snapshot.get("tables")
    if not isinstance(tables, list):
        return issues + ["tables 不是数组"]
    keys = [(table.get("owner", ""), table.get("tableName", "")) for table in tables]
    if keys != sorted(keys):
        issues.append("表未按 (owner, tableName) 排序")
    for table in tables:
        columnNames = [column.get("columnName", "") for column in table.get("columns", [])]
        if columnNames != sorted(columnNames):
            issues.append(f"表 {table.get('tableName')} 的列未按列名排序")
        fkKeys = [
            (
                fk.get("columnName", ""),
                fk.get("refTable", ""),
                fk.get("refColumn", ""),
            )
            for fk in table.get("foreignKeys", [])
        ]
        if fkKeys != sorted(fkKeys):
            issues.append(f"表 {table.get('tableName')} 的外键未按 (列, 参照表, 参照列) 排序")
    return issues


@dataclass(frozen=True)
class CaptureEvidence:
    """采集面的证据（不可变）：首采 + 复采 + 两次**原生读**。"""

    first: Any
    second: Any
    beforeCount: int
    afterFirst: int
    afterSecond: int
    rowRaw: dict[str, Any]
    rowAgain: dict[str, Any]
    recomputedHash: str


async def _captureOnce(
    session: AsyncSession,
    service: DiscoverySnapshotService,
    source: DiscoverySource,
    override: ConnectOverride,
) -> tuple[Any, int, int]:
    """采集一次（落库 + 审计 + 提交），返回 (结果, 采集前行数, 采集后行数)。"""

    beforeCount = await service.countSnapshots(session, sourceId=source.id)
    result = await service.captureSnapshot(
        session,
        sourceId=source.id,
        createdBy=_SCRIPT_ACTOR,
        connectOverride=override,
    )
    await _auditSnapshot(session, result)
    await session.commit()
    afterCount = await service.countSnapshots(session, sourceId=source.id)
    return result, beforeCount, afterCount


def _runCaptureChecks(results: list[Check], evidence: CaptureEvidence) -> None:
    """采集面的 8 条断言（内容寻址 / 幂等 / 只增不改 / 口径可复现）。"""

    first = evidence.first
    second = evidence.second
    rowRaw = evidence.rowRaw
    rowAgain = evidence.rowAgain
    rowSnapshot = rowRaw["snapshotJson"]

    _check(
        results,
        "首次采集落库（内容寻址）",
        first.schemaHash == evidence.recomputedHash,
        f"落库 hash={first.schemaHash[:12]}… vs 独立重算={evidence.recomputedHash[:12]}…",
    )
    _check(
        results,
        "首次采集的行数变化符合内容寻址语义",
        evidence.afterFirst - evidence.beforeCount == (0 if first.reused else 1),
        f"采集前 {evidence.beforeCount} 行 → 采集后 {evidence.afterFirst} 行"
        f"（reused={first.reused}）",
    )
    _check(
        results,
        "落库行的表/列计数与快照内容一致",
        first.tableCount == len(rowSnapshot.get("tables", []))
        and first.columnCount
        == sum(len(t.get("columns", [])) for t in rowSnapshot.get("tables", [])),
        f"tableCount={first.tableCount} columnCount={first.columnCount}",
    )
    _check(
        results,
        "二次采集复用既有快照（内容寻址幂等）",
        second.reused is True and second.snapshotId == first.snapshotId,
        f"reused={second.reused} snapshotId={second.snapshotId}",
    )
    _check(
        results,
        "二次采集不新增快照行（只增不改）",
        evidence.afterSecond == evidence.afterFirst,
        f"采集后 {evidence.afterFirst} 行 → 二次采集后 {evidence.afterSecond} 行",
    )
    _check(
        results,
        "二次采集未改写既有快照行（原生读比对）",
        rowAgain["snapshotJson"] == rowSnapshot
        and rowAgain["schemaHash"] == rowRaw["schemaHash"]
        and rowAgain["capturedAt"] == rowRaw["capturedAt"],
        f"captured_at={rowRaw['capturedAt'].isoformat() if rowRaw['capturedAt'] else None}",
    )
    _check(
        results,
        "快照 JSON 形状合规（版本/排序）",
        not _snapshotShapeIssues(rowSnapshot),
        "; ".join(_snapshotShapeIssues(rowSnapshot)) or "无问题",
    )
    _check(
        results,
        "哈希可由落库 JSON 独立重算（口径可复现）",
        computeSnapshotHash(rowSnapshot) == rowRaw["schemaHash"],
        f"重算={computeSnapshotHash(rowSnapshot)[:12]}… 落库={rowRaw['schemaHash'][:12]}…",
    )


def _buildCaptureReport(
    *,
    datasource: DataSource,
    effective: DataSource,
    source: DiscoverySource,
    evidence: CaptureEvidence,
    results: list[Check],
) -> dict[str, Any]:
    """采集面的报告（主机地址已掩码）。"""

    first = evidence.first
    rowSnapshot = evidence.rowRaw["snapshotJson"]
    return {
        "mode": "capture",
        "datasource": {
            "id": datasource.id,
            "name": datasource.name,
            "type": _typeNameOf(datasource),
            "database": datasource.database_name,
            "connectHost": _maskHost(effective.host),
            "connectPort": effective.port,
        },
        "source": {"id": source.id, "name": source.name},
        "snapshot": {
            "id": first.snapshotId,
            "schemaHash": first.schemaHash,
            "tableCount": first.tableCount,
            "columnCount": first.columnCount,
            "capturedAt": first.capturedAt.isoformat() if first.capturedAt else None,
            "reused": first.reused,
            "tables": [table["tableName"] for table in rowSnapshot.get("tables", [])],
        },
        "secondCapture": {
            "reused": evidence.second.reused,
            "snapshotId": evidence.second.snapshotId,
            "rowsBefore": evidence.afterFirst,
            "rowsAfter": evidence.afterSecond,
        },
        "checks": [
            {"name": item.name, "ok": item.ok, "detail": item.detail}
            for item in results
        ],
    }


async def _capturePhase(
    session: AsyncSession,
    service: DiscoverySnapshotService,
    *,
    datasourceId: int,
    sourceName: str,
    connectHost: str | None,
    connectPort: int | None,
) -> dict[str, Any]:
    """采集面入口：定位数据源 ⇒ 跑采集面 ⇒ 失败路径掩码主机。"""

    datasource = await _resolveDatasource(session, datasourceId, sourceName)
    print(
        f"  [INFO] 演示数据源 id={datasource.id} name={datasource.name} "
        f"type={datasource.type} db={datasource.database_name}"
    )
    try:
        return await _runCapture(
            session,
            service,
            datasource=datasource,
            sourceName=sourceName,
            connectHost=connectHost,
            connectPort=connectPort,
        )
    except Exception as exc:  # noqa: BLE001 - 统一转成脚本化失败
        # 业务库主机属内网拓扑信息 ⇒ 失败路径也要掩码（安全审查 L-5）：
        # 驱动异常（连接被拒 / 认证失败）常把地址原样带出来。
        raise VerifyFailure(
            _redact(f"{type(exc).__name__}: {exc}", [datasource.host, connectHost])
        ) from exc


async def _runCapture(
    session: AsyncSession,
    service: DiscoverySnapshotService,
    *,
    datasource: DataSource,
    sourceName: str,
    connectHost: str | None,
    connectPort: int | None,
) -> dict[str, Any]:
    """采集面主体：真实内省 ⇒ 内容寻址落库 ⇒ 幂等/只增不改断言。"""

    results: list[Check] = []
    override = ConnectOverride(host=connectHost, port=connectPort)
    effective = _withConnectOverride(datasource, connectHost, connectPort)
    if connectHost or connectPort:
        print(
            f"  [INFO] 连接地址覆盖生效：内省用 {_maskHost(effective.host)}:"
            f"{effective.port}（库里的数据源配置不变）"
        )

    source = await _ensureSource(
        session, service, name=sourceName, datasourceId=datasource.id
    )

    # 独立重算：脚本自己内省一次 ⇒ 规范化 ⇒ 哈希（用来核对落库行的哈希）
    recomputed, recomputedHash = await _recomputeHash(effective, source)
    print(
        f"  [INFO] 独立内省：{len(recomputed['tables'])} 张表 / "
        f"{sum(len(t['columns']) for t in recomputed['tables'])} 列 / hash={recomputedHash[:12]}…"
    )

    first, beforeCount, afterFirst = await _captureOnce(session, service, source, override)
    # 原生读（数据库真值），不用 ORM 实例
    rowRaw = await _readSnapshotRow(session, first.snapshotId)

    # 二次采集：同内容 ⇒ 复用既有行（不新增）
    second, _, afterSecond = await _captureOnce(session, service, source, override)
    # 再次原生读：断言看的是**库里此刻**的值（H-3：ORM 实例比较 = 空断言）
    rowAgain = await _readSnapshotRow(session, first.snapshotId)

    evidence = CaptureEvidence(
        first=first,
        second=second,
        beforeCount=beforeCount,
        afterFirst=afterFirst,
        afterSecond=afterSecond,
        rowRaw=rowRaw,
        rowAgain=rowAgain,
        recomputedHash=recomputedHash,
    )
    _runCaptureChecks(results, evidence)
    return _buildCaptureReport(
        datasource=datasource,
        effective=effective,
        source=source,
        evidence=evidence,
        results=results,
    )


async def _diffPhase(session: AsyncSession) -> dict[str, Any]:
    """差异面：基线（ACTIVE 发布）vs 当前治理表（**只读**）。"""

    results: list[Check] = []
    result = await computeOntologyDiff(session)
    again = await computeOntologyDiff(session)
    diff = result.diff

    _check(
        results,
        "差异计算确定性（两次结果完全相同）",
        diff == again.diff,
        f"变更数 {diff.changeCount} vs {again.diff.changeCount}",
    )
    _check(
        results,
        "风险分级与严重度一致（BREAKING→HIGH / ADDITIVE→LOW / COSMETIC→INFO）",
        all(
            change.riskLevel
            == {"BREAKING": "HIGH", "ADDITIVE": "LOW", "COSMETIC": "INFO"}.get(
                change.severity
            )
            for change in diff.changes
        ),
        f"{diff.changeCount} 条变更",
    )
    _check(
        results,
        "整体风险等级 = 各变更最高级",
        diff.overallRiskLevel
        == (
            "HIGH"
            if diff.hasBreakingChanges
            else ("LOW" if diff.additiveCount > 0 else "INFO")
        ),
        f"overall={diff.overallRiskLevel}",
    )

    changes = [
        {
            "kind": change.kind,
            "severity": change.severity,
            "riskLevel": change.riskLevel,
            "section": change.section,
            "target": change.target,
            "field": change.field,
            "before": change.before,
            "after": change.after,
        }
        for change in diff.changes[:_MAX_REPORTED_CHANGES]
    ]

    report = {
        "mode": "diff",
        "baseline": {
            "releaseId": result.baselineReleaseId,
            "version": result.baselineVersion,
            "manifestHash": result.baselineHash,
            "present": result.baselineReleaseId is not None,
        },
        "diff": {
            "changeCount": diff.changeCount,
            "breakingCount": diff.breakingCount,
            "additiveCount": diff.additiveCount,
            "cosmeticCount": diff.cosmeticCount,
            "hasBreakingChanges": diff.hasBreakingChanges,
            "overallRiskLevel": diff.overallRiskLevel,
            "changes": changes,
            "truncated": diff.changeCount > len(changes),
        },
        "checks": [
            {"name": item.name, "ok": item.ok, "detail": item.detail}
            for item in results
        ],
    }
    if result.baselineReleaseId is None:
        print("  [INFO] 该库没有 ACTIVE 发布 ⇒ 基线为空（未发布是正常状态，不是失败）")
    return report


async def _disposeAdapters() -> None:
    """释放业务库适配器（连接池），避免进程退出时的回收噪声。"""

    from app.infrastructure.business_db_pool import reset_pool

    try:
        await reset_pool()
    except Exception as exc:  # noqa: BLE001 - 释放失败不影响判定结果
        print(f"  [WARN] 释放业务库适配器失败：{exc}", file=sys.stderr)


async def main() -> int:
    parser = argparse.ArgumentParser(description="本体发现底座真实数据验证（A1-7）")
    parser.add_argument(
        "--datasource-id",
        type=int,
        default=None,
        help="演示业务数据源 id；不给则按 --datasource-name 找，再退化为「活动的 mysql 源」",
    )
    parser.add_argument(
        "--datasource-name",
        default=_DEFAULT_DATASOURCE_NAME,
        help=f"演示业务数据源名（默认 {_DEFAULT_DATASOURCE_NAME!r}）",
    )
    parser.add_argument(
        "--source-name",
        default=_DEFAULT_SOURCE_NAME,
        help=f"发现源名（默认 {_DEFAULT_SOURCE_NAME!r}；同名已存在则复用）",
    )
    parser.add_argument(
        "--connect-host",
        default=None,
        help="内省时的连接地址覆盖（宿主机跑时把容器内网名换成宿主机可达地址）",
    )
    parser.add_argument(
        "--connect-port",
        type=int,
        default=None,
        help="内省时的连接端口覆盖",
    )
    parser.add_argument(
        "--diff-only",
        action="store_true",
        help="只跑差异面（**零写**）⇒ 可对有真实 ACTIVE 发布的治理库直接跑",
    )
    parser.add_argument(
        "--allow-prod-write",
        action="store_true",
        help="允许写正式库 qa_metadata（prod 同源；默认拒绝）",
    )
    args = parser.parse_args()

    from app.config import getSettings

    _requireDatabaseUrlEnv()
    settings = getSettings()
    databaseUrl = settings.databaseUrl
    dbName = databaseNameOf(databaseUrl)
    dsnHost = _hostOf(databaseUrl)
    print(f"[ontology_discovery_realdata] 元数据库：{dbName}（不打印凭据）")
    print(f"[ontology_discovery_realdata] 模式：{'差异（只读）' if args.diff_only else '采集（写）'}")

    if not args.diff_only:
        # 写库闸：复用 KPI 工具链的同一份守卫（白名单 + prod 需显式 opt-in）。
        _guardDatabaseUrl(databaseUrl, allowProdWrite=args.allow_prod_write)
        if dbName == PROD_DB_NAME:
            _warnProdWrite(dbName)

    from app.infrastructure.database import getSessionFactory

    sessionMaker = getSessionFactory()
    # 引擎此刻才创建（echo 的默认 handler 也在这时挂上）⇒ 摘 handler 必须在其后。
    _silenceSqlEcho()
    service = DiscoverySnapshotService()
    try:
        async with sessionMaker() as session:
            if args.diff_only:
                report = await _diffPhase(session)
            else:
                report = await _capturePhase(
                    session,
                    service,
                    datasourceId=args.datasource_id,
                    sourceName=args.source_name,
                    connectHost=args.connect_host,
                    connectPort=args.connect_port,
                )
    except VerifyFailure as exc:
        print(
            f"[ontology_discovery_realdata] FAIL: {_redact(str(exc), [dsnHost])}",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:  # noqa: BLE001 - 连接/驱动异常也要脚本化退出
        # 失败路径也要掩码主机（安全审查 L-5）：驱动异常常带连接地址。
        print(
            f"[ontology_discovery_realdata] FAIL: "
            f"{_redact(f'{type(exc).__name__}: {exc}', [dsnHost])}",
            file=sys.stderr,
        )
        return 1
    finally:
        # 释放业务库适配器：否则进程退出时 aiomysql 连接回收会打
        # 「Event loop is closed」噪声（结果已判定，无碍，但会污染证据输出）。
        await _disposeAdapters()

    print(json.dumps(report, ensure_ascii=False, indent=2))
    failed = [item for item in report["checks"] if not item["ok"]]
    if failed:
        print(
            f"[ontology_discovery_realdata] FAIL: {len(failed)} 项断言不成立："
            + "; ".join(item["name"] for item in failed),
            file=sys.stderr,
        )
        return 1
    print(f"{_RESULT_MARK}: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
