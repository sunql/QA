"""本体发现真实数据脚本的**安全护栏与断言口径**单测（WP-ONT-AI A1-7）。

本文件钉住脚本里三类「看起来是验证、实际可能假绿 / 越权」的地方：

1. **写库闸门**：目标库必须是隔离测试库族（`qa_metadata_test` / `qa_metadata_test_*`）；
   正式库 `qa_metadata` 需显式 `--allow-prod-write`，且放行时必须**打印将写入的
   行数**（操作者不该在不知情下写生产库）。
2. **只增不改断言必须看数据库真值**：ORM 身份映射里拿到的是**同一个实例** ⇒
   即便库里那行被改过，「二次采集前后逐字相同」也照样成立 ⇒ 空断言（假绿）。
   ⇒ 必须用绕开 identity map 的原生读。
3. **连接地址覆盖要作用到采集路径**：否则「内省用哪个地址」这件事在采集面与
   独立重算面**各行其是** ⇒ 两个哈希可能来自两个不同的库。

另：主机地址属内网拓扑信息 ⇒ 报告里必须掩码。
"""

from __future__ import annotations

import importlib
import json

import pytest

from app.domain.discovery_models import MetadataSnapshot


def _loadScript():
    return importlib.import_module("scripts.ontology_discovery_realdata")


# ---------------------------------------------------------------- 写库闸门
#
# ⚠️ 判定逻辑已收敛到**共享守卫** `scripts/kpi_metric_db_guard.assertSafeTarget`
# （安全审查 L-4：脚本里原本**自己抄了一份**白名单 ⇒ 两份正则会漂移）。
# 本文件断言的是「脚本确实接在共享守卫上」+ 共享守卫在本脚本语义下的行为。


def test_脚本复用共享守卫而不是自抄白名单():
    """脚本不得自己维护第二份库名白名单（两份正则会漂移）。"""

    script = _loadScript()

    assert script._guardDatabaseUrl is not None
    # 脚本里不得再出现自己实现的库名判定入口
    assert not hasattr(script, "_isTestDatabaseName")


def test_隔离测试库族被放行():
    """白名单是**族**（`qa_metadata_test` / `qa_metadata_test_*`），不是单个库名。

    代码审查 L-7：判定用精确匹配、文案却写「前缀族」⇒ 判定与文案不一致。
    """

    script = _loadScript()

    for dbName in ("qa_metadata_test", "qa_metadata_test_semantic", "qa_metadata_test_x1v"):
        assert script._guardDatabaseUrl(
            f"postgresql+asyncpg://qa_user:***@127.0.0.1:5434/{dbName}"
        ) == dbName


def test_正式库与相似库名被拒():
    """相似但不是族成员（前缀后无分隔符）⇒ 拒绝。"""

    script = _loadScript()

    for dbName in ("qa_metadata_testx", "qa_metadata_prod", "other_db"):
        with pytest.raises(SystemExit):
            script._guardDatabaseUrl(
                f"postgresql+asyncpg://qa_user:***@127.0.0.1:5434/{dbName}"
            )


def test_正式库默认拒绝执行(monkeypatch: pytest.MonkeyPatch):
    script = _loadScript()
    prodUrl = "postgresql+asyncpg://qa_user:***@127.0.0.1:5433/qa_metadata"

    with pytest.raises(SystemExit) as excinfo:
        script._guardDatabaseUrl(prodUrl, allowProdWrite=False)

    assert "allow-prod-write" in str(excinfo.value)


def test_正式库放行时打印将写入的行数(monkeypatch: pytest.MonkeyPatch, capsys):
    """L-6：放行正式库必须先说清「会写几行」⇒ 操作者知情。"""

    script = _loadScript()

    script._warnProdWrite("qa_metadata")

    out = capsys.readouterr().out
    assert "2" in out
    assert "INSERT" in out or "插入" in out


def test_未知库被拒(monkeypatch: pytest.MonkeyPatch):
    script = _loadScript()

    with pytest.raises(SystemExit):
        script._guardDatabaseUrl(
            "postgresql+asyncpg://qa_user:***@127.0.0.1:5433/other_db",
            allowProdWrite=True,
        )


# ---------------------------------------------------------------- 主机掩码


def test_主机地址被掩码():
    """L-5：主机属内网拓扑 ⇒ 报告里不出现完整地址。"""

    script = _loadScript()

    masked = script._maskHost("host.docker.internal")

    assert masked is not None
    assert "host.docker.internal" not in masked
    assert masked.startswith("h")


def test_空主机掩码后仍为空():
    script = _loadScript()

    assert script._maskHost(None) is None
    assert script._maskHost("") is None


# ---------------------------------------------------------------- 原生读（假绿防线）


async def _seedSource(dbSession) -> int:
    """种一个最小可用的发现源（快照表有 FK ⇒ 必须先有源）。"""

    from app.domain.discovery_models import DiscoverySource
    from app.domain.models import DataSource

    ds = DataSource(
        name="原生读演示源",
        type="mysql",
        host="127.0.0.1",
        port=3306,
        database_name="demo",
        username="demo",
        password_encrypted="x",
        is_active=True,
        is_default=False,
    )
    dbSession.add(ds)
    await dbSession.flush()
    source = DiscoverySource(
        name="原生读源",
        datasource_id=ds.id,
        owner="张业务",
        access_policy={"mode": "READ_ONLY"},
        status="ACTIVE",
    )
    dbSession.add(source)
    await dbSession.commit()
    return source.id


async def test_原生读绕开orm身份映射(dbSession):
    """H-3：只增不改断言必须看**数据库真值**。

    这里让 ORM 实例在内存里被改掉（不 flush）⇒ 若断言读的是 identity map，
    就会读到被改过的值（假绿）；原生读必须仍返回库里的原值。
    """

    script = _loadScript()
    sourceId = await _seedSource(dbSession)
    row = MetadataSnapshot(
        source_id=sourceId,
        schema_hash="a" * 64,
        snapshot_json={"schemaVersion": "v1", "tables": []},
        table_count=0,
        column_count=0,
    )
    dbSession.add(row)
    await dbSession.commit()

    # 只在内存里改（不 flush）⇒ 库里的值不变
    row.table_count = 999
    row.snapshot_json = {"schemaVersion": "v1", "tables": [{"tableName": "X"}]}

    raw = await script._readSnapshotRow(dbSession, row.id)

    assert raw["tableCount"] == 0
    assert raw["snapshotJson"] == {"schemaVersion": "v1", "tables": []}
    assert raw["schemaHash"] == "a" * 64


async def test_原生读返回的内容可独立重算哈希(dbSession):
    """原生读回来的 JSON 必须能重算出同一个哈希（口径可复现）。"""

    script = _loadScript()
    from app.services.discovery_snapshot_service import computeSnapshotHash

    sourceId = await _seedSource(dbSession)
    snapshot = {"schemaVersion": "v1", "tables": []}
    row = MetadataSnapshot(
        source_id=sourceId,
        schema_hash=computeSnapshotHash(snapshot),
        snapshot_json=snapshot,
        table_count=0,
        column_count=0,
    )
    dbSession.add(row)
    await dbSession.commit()

    raw = await script._readSnapshotRow(dbSession, row.id)

    assert computeSnapshotHash(raw["snapshotJson"]) == raw["schemaHash"]
    assert json.dumps(raw["snapshotJson"], sort_keys=True) == json.dumps(
        snapshot, sort_keys=True
    )


async def test_原生读不存在的行报错(dbSession):
    script = _loadScript()

    with pytest.raises(script.VerifyFailure):
        await script._readSnapshotRow(dbSession, 999999)
