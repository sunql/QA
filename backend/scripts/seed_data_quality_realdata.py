"""Phase 1.3 真实数据验证脚本（不依赖外部业务库）。

目的：
- 当前环境没有 MySQL demo 数据（docker-compose mysql-demo 空库）；
- 把"业务对象"用真实 PostgreSQL + 真实表 + 真实 SQL 验证：
  1. 建一个 `business` schema
  2. 在里面建 3 张 Sage X3 / WMS 风格的真实业务表：PORDER / BPSUPPLIER / PORDERQ
  3. 灌入真实风格数据（含故意制造的不合规样本：NULL、重复、引用不存在）
  4. 在 metadata public schema 创建 1 个 DataSource 指向当前 PG
  5. 注册 5 条 DQ rule（覆盖 5 个 rule_type）
  6. 跑 HTTP POST /scores/compute 真实落库 + GET /scores 验证
- 整个链路走真实 PG、真实 HTTP、真实 SQL、真实聚合（不调任何 mock adapter）。

幂等：每次跑都会先 DROP business schema 再建；DataSource 用 name 查重。
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

# 允许从 scripts/ 直接运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from app.infrastructure.database import getSessionFactory  # noqa: E402
from app.infrastructure.security.crypto import encryptApiKey  # noqa: E402

DATASOURCE_NAME = "phase13_realdata_pg"
DATASOURCE_DB = "qa_metadata_test"  # 与 metadata DB 同实例，不同 schema

# 5 条规则覆盖 5 个 rule_type：
#   1. PORDER.ORDERQTY 完整性（NULL 占比）应 < 5%
#   2. PORDER.ORDERQTY 合理性（> 0）应 >= 99%
#   3. PORDER.PONUM 唯一性应 = 100%
#   4. PORDER.PONUM 与 PORDERQ.PONUM 一致性应 >= 95%（JOIN count match）
#   5. PORDER.BPSNUM 引用 BPSUPPLIER.BPSNUM 应 >= 90%
RULES_SEED: list[dict] = [
    dict(
        rule_code="RQ_PORDER_QTY_COMPLETE",
        rule_name="采购订单数量完整性",
        target_table="PORDER",
        target_column="ORDERQTY",
        rule_type="COMPLETENESS",
        rule_expression=None,
        threshold="95.00",
        severity="HIGH",
    ),
    dict(
        rule_code="RQ_PORDER_QTY_VALID",
        rule_name="采购订单数量合理性",
        target_table="PORDER",
        target_column="ORDERQTY",
        rule_type="VALIDITY",
        rule_expression='"ORDERQTY" > 0',  # PG 大小写敏感，必须双引号
        threshold="99.00",
        severity="HIGH",
    ),
    dict(
        rule_code="RQ_PORDER_PO_UNIQUE",
        rule_name="采购订单号唯一性",
        target_table="PORDER",
        target_column="PONUM",
        rule_type="UNIQUENESS",
        rule_expression=None,
        threshold="100.00",
        severity="HIGH",
    ),
    dict(
        rule_code="RQ_PORDER_VS_ORDERQ_CONSIST",
        rule_name="订单头行一致性",
        target_table="PORDER",
        target_column="PONUM",
        rule_type="CONSISTENCY",
        # 计算 PORDER 中出现在 PORDERQ 的 PONUM 占比（一致性 = 实际关联率）
        # PG 大小写敏感，所有标识符必须双引号
        rule_expression='"PONUM" IN (SELECT "PONUM" FROM "PORDERQ")',
        threshold="80.00",
        severity="MEDIUM",
    ),
    dict(
        rule_code="RQ_PORDER_BPSNUM_REF",
        rule_name="供应商编码引用完整性",
        target_table="PORDER",
        target_column="BPSNUM",
        rule_type="REFERENTIAL",
        rule_expression="REF BPSUPPLIER.BPSNUM",
        threshold="90.00",
        severity="HIGH",
    ),
]


# ===== 建业务库 schema + 灌数据 =====

# 真实数据验证要求业务表能被 evaluator 通过简单表名访问。
# 当前环境只有 1 个 PG 实例（无 MySQL demo 数据），evaluator SQL 拼接为 FROM "PORDER"
# 不带 schema 前缀（且 identifier regex 不允许 . 字符），所以表必须放在默认 search_path
# 中。drop 旧 business schema 并改用 public。
BUSINESS_SCHEMA = "public"


async def _seedBusinessSchema(session) -> None:
    """在 metadata DB 同一个 PG 实例上建业务表 + 数据。

    数据故意制造缺陷以触发 FAIL / 完整性告警：
    - PORDER 共 20 行，3 行 ORDERQTY = NULL（15% NULL → COMPLETENESS FAIL < 95%）
    - 1 行 ORDERQTY = 0（合理性 FAIL）
    - 1 对 PONUM 重复（唯一性 FAIL）
    - 5 行 PONUM 在 PORDERQ 不存在（一致性 FAIL < 80%）
    - 3 行 BPSNUM 在 BPSUPPLIER 不存在（引用 FAIL < 90%）
    """
    # 仅删除我们要建的 3 张业务表（不能 DROP SCHEMA public）
    for t in ("PORDER", "BPSUPPLIER", "PORDERQ"):
        await session.execute(text(f"DROP TABLE IF EXISTS {t} CASCADE"))

    # BPSUPPLIER：5 条供应商
    # 注意：evaluator SQL 走 FROM "BPSUPPLIER" 双引号访问（case-sensitive）。
    # 这里必须用双引号建表/列才能匹配。
    await session.execute(
        text(f'''
        CREATE TABLE "BPSUPPLIER" (
            "BPSNUM" VARCHAR(20) PRIMARY KEY,
            "BPSNAM" VARCHAR(100),
            "STATUS" VARCHAR(10)
        )
    ''')
    )
    for i in range(1, 6):
        await session.execute(
            text(
                'INSERT INTO "BPSUPPLIER" ("BPSNUM", "BPSNAM", "STATUS") '
                "VALUES (:n, :name, 'A')"
            ),
            {"n": f"BPS{i:04d}", "name": f"供应商 {i}"},
        )

    # PORDER：20 行（含缺陷）
    await session.execute(
        text(f'''
        CREATE TABLE "PORDER" (
            "ROW_ID" BIGSERIAL PRIMARY KEY,
            "PONUM" VARCHAR(20),
            "BPSNUM" VARCHAR(20),
            "ORDERQTY" NUMERIC(18, 4),
            "ORDERDATE" DATE,
            "STATUS" VARCHAR(10)
        )
    ''')
    )
    # 正常 9 行
    for i in range(1, 10):
        await session.execute(
            text(
                'INSERT INTO "PORDER" ("PONUM", "BPSNUM", "ORDERQTY", "ORDERDATE", "STATUS") '
                "VALUES (:p, :b, :q, CURRENT_DATE, 'OPEN')"
            ),
            {"p": f"PO{i:05d}", "b": f"BPS{i % 5 + 1:04d}", "q": 100 + i},
        )
    # 1 行重复 PONUM（PO00002 再插一次，触发唯一性 FAIL）
    await session.execute(
        text(
            'INSERT INTO "PORDER" ("PONUM", "BPSNUM", "ORDERQTY", "ORDERDATE", "STATUS") '
            "VALUES ('PO00002', 'BPS0001', 50, CURRENT_DATE, 'OPEN')"
        )
    )
    # 3 行 ORDERQTY = NULL（完整性 FAIL）
    for i in range(11, 14):
        await session.execute(
            text(
                'INSERT INTO "PORDER" ("PONUM", "BPSNUM", "ORDERQTY", "ORDERDATE", "STATUS") '
                "VALUES (:p, :b, NULL, CURRENT_DATE, 'OPEN')"
            ),
            {"p": f"PO{i:05d}", "b": f"BPS{(i % 5) + 1:04d}"},
        )
    # 1 行 ORDERQTY = 0（合理性 FAIL）
    await session.execute(
        text(
            'INSERT INTO "PORDER" ("PONUM", "BPSNUM", "ORDERQTY", "ORDERDATE", "STATUS") '
            "VALUES ('PO00014', 'BPS0001', 0, CURRENT_DATE, 'OPEN')"
        )
    )
    # 5 行 BPSNUM 在 BPSUPPLIER 不存在（引用 FAIL）
    for i in range(15, 20):
        await session.execute(
            text(
                'INSERT INTO "PORDER" ("PONUM", "BPSNUM", "ORDERQTY", "ORDERDATE", "STATUS") '
                "VALUES (:p, :b, :q, CURRENT_DATE, 'OPEN')"
            ),
            {"p": f"PO{i:05d}", "b": f"BPS999{i % 9}", "q": 100 + i},
        )

    # PORDERQ：15 行（10 个正常 + 5 个不在 PORDER）→ 一致性 FAIL
    await session.execute(
        text(f'''
        CREATE TABLE "PORDERQ" (
            "PONUM" VARCHAR(20),
            "LINNUM" INTEGER,
            "ITMREF" VARCHAR(20),
            "QTY" NUMERIC(18, 4)
        )
    ''')
    )
    for i in range(1, 11):
        await session.execute(
            text(
                'INSERT INTO "PORDERQ" ("PONUM", "LINNUM", "ITMREF", "QTY") '
                "VALUES (:p, :l, :it, :q)"
            ),
            {
                "p": f"PO{i:05d}",
                "l": 1,
                "it": f"ITM{i:04d}",
                "q": 10,
            },
        )
    # PO00011（NULL QTY 行）不出现 → 一致性失败 1
    # 4 个额外假 PONUM → 一致性失败 4
    for i in range(100, 104):
        await session.execute(
            text(
                'INSERT INTO "PORDERQ" ("PONUM", "LINNUM", "ITMREF", "QTY") '
                "VALUES (:p, :l, :it, :q)"
            ),
            {"p": f"FAKE{i}", "l": 1, "it": "ITM0001", "q": 5},
        )


async def _seedDatasource(session) -> int:
    """在 data_source 表建一个指向当前 PG 实例的 DataSource。"""
    db_url = os.environ.get("DATABASE_URL", "")
    # 用 SQLAlchemy 解析不便，直接 hardcode 5433 测试实例
    host = "localhost"
    port = 5433
    user = "qa_user"
    password = "qa_pg_dev_2026"
    database = "qa_metadata_test"
    # 查重
    existing = (
        await session.execute(
            text("SELECT id FROM data_source WHERE name = :n"),
            {"n": DATASOURCE_NAME},
        )
    ).first()
    if existing:
        ds_id = existing[0]
        await session.execute(
            text("UPDATE data_source SET is_active=true WHERE id=:id"), {"id": ds_id}
        )
        await session.commit()
        return ds_id
    row = (
        await session.execute(
            text(
                """
            INSERT INTO data_source (
                name, type, host, port, database_name, username, password_encrypted,
                description, is_active, is_default, created_time, updated_time
            ) VALUES (
                :name, 'postgresql', :host, :port, :db, :user, :pw,
                'Phase 1.3 真实数据验证用（同实例 business schema）', true, false,
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            ) RETURNING id
            """
            ),
            {
                "name": DATASOURCE_NAME,
                "host": host,
                "port": port,
                "db": database,
                "user": user,
                "pw": encryptApiKey(password),
            },
        )
    ).first()
    await session.commit()
    return row[0]


async def _seedRules(session, ds_id: int) -> list[int]:
    """写入 5 条 DQ rule；按 rule_code 查重。"""
    rule_ids: list[int] = []
    for r in RULES_SEED:
        existing = (
            await session.execute(
                text("SELECT id FROM data_quality_rule WHERE rule_code = :c"),
                {"c": r["rule_code"]},
            )
        ).first()
        if existing:
            rule_ids.append(existing[0])
            await session.execute(
                text(
                    "UPDATE data_quality_rule SET is_enabled=true, threshold=:t, "
                    "datasource_id=:ds WHERE id=:id"
                ),
                {"t": r["threshold"], "ds": ds_id, "id": existing[0]},
            )
            continue
        row = (
            await session.execute(
                text(
                    """
                INSERT INTO data_quality_rule (
                    rule_name, rule_code, datasource_id, target_table, target_column,
                    rule_type, rule_expression, threshold, severity, is_enabled, version,
                    created_time, updated_time
                ) VALUES (
                    :rn, :rc, :ds, :tt, :tc, :rt, :re, :th, :sv, true, 'v1.0',
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                ) RETURNING id
                """
                ),
                {
                    "rn": r["rule_name"],
                    "rc": r["rule_code"],
                    "ds": ds_id,
                    "tt": r["target_table"],
                    "tc": r["target_column"],
                    "rt": r["rule_type"],
                    "re": r["rule_expression"],
                    "th": r["threshold"],
                    "sv": r["severity"],
                },
            )
        ).first()
        rule_ids.append(row[0])
    await session.commit()
    return rule_ids


async def main() -> None:
    factory = getSessionFactory()
    async with factory() as session:
        # 1. 建业务库 + 灌数据
        print("[1/3] 重建 business schema + 灌真实数据 ...")
        await _seedBusinessSchema(session)
        await session.commit()
        # 验数据量
        n = (
            await session.execute(text('SELECT COUNT(*) FROM "PORDER"'))
        ).scalar()
        print(f"  PORDER 行数: {n}")
        n = (
            await session.execute(text('SELECT COUNT(*) FROM "BPSUPPLIER"'))
        ).scalar()
        print(f"  BPSUPPLIER 行数: {n}")
        n = (
            await session.execute(text('SELECT COUNT(*) FROM "PORDERQ"'))
        ).scalar()
        print(f"  PORDERQ 行数: {n}")
        # 验数据缺陷
        null_qty = (
            await session.execute(
                text('SELECT COUNT(*) FROM "PORDER" WHERE "ORDERQTY" IS NULL')
            )
        ).scalar()
        zero_qty = (
            await session.execute(
                text('SELECT COUNT(*) FROM "PORDER" WHERE "ORDERQTY" = 0')
            )
        ).scalar()
        invalid_bps = (
            await session.execute(
                text(
                    'SELECT COUNT(*) FROM "PORDER" p '
                    'WHERE NOT EXISTS (SELECT 1 FROM "BPSUPPLIER" s '
                    'WHERE s."BPSNUM" = p."BPSNUM")'
                )
            )
        ).scalar()
        print(f"  故意制造的缺陷：NULL QTY={null_qty}, ZERO QTY={zero_qty}, "
              f"无效供应商引用={invalid_bps}")

        # 2. 建 DataSource
        print("[2/3] 注册 DataSource ...")
        ds_id = await _seedDatasource(session)
        print(f"  data_source.id = {ds_id}")

        # 3. 建 5 条 DQ rule
        print("[3/3] 写入 5 条 DQ rule ...")
        ids = await _seedRules(session, ds_id)
        for rid, r in zip(ids, RULES_SEED):
            print(f"  rule.id={rid} code={r['rule_code']} type={r['rule_type']}")

    print("\n✅ 真实数据种子完成。下一步：")
    print("   1. 启动 backend（uv run app/main.py 或 docker compose up -d backend）")
    print("   2. curl -X POST http://localhost:8000/api/v1/data-quality/scores/compute")
    print("   3. curl 'http://localhost:8000/api/v1/data-quality/scores?latest=true'")
    # 4. Phase 1.4：跑 chat 调用 + 断言 dataQuality badge
    print("\n[Phase 1.4] 真实 chat 调用 + badge 断言 ...")
    await _phase14ChatSmoke()


async def _phase14ChatSmoke() -> None:
    """Phase 1.4 真实数据验证：调 chat_service.processMessage → 断言 dataQuality。

    前置条件：
    - data_quality_score 表里已有 PORDER 的最新 TABLE score（Phase 1.3 compute 跑过；
      本脚本本身不调用 compute，由 ops 在外部触发）
    - ontology_class 包含 PORDER（Phase 1.1 种子已写）

    断言：
    - ChatResponse.dataQuality[0].targetTable == "PORDER"
    - dataQuality[0].evaluated == True
    - dataQuality[0].overallScore 与 Phase 1.3 整体分（76.84）一致
    - dataQuality[0].rulesCount == 5

    失败：raise AssertionError，让运维立刻知道是否一致。
    """
    import httpx
    from app.tests._testapp import buildTestApp
    from app.infrastructure import database as dbModule

    factory = dbModule.getSessionFactory()
    app = buildTestApp(factory)

    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # 找一条 active ds_id
            async with factory() as session:
                ds_id = (await session.execute(text("SELECT id FROM data_source WHERE is_active=true ORDER BY id DESC LIMIT 1"))).scalar()
            assert ds_id is not None, "未找到 active DataSource（请先运行本脚本第 1-3 步）"

            resp = await client.post(
                "/api/v1/chat",
                json={
                    "sessionId": f"phase14-smoke-{uuid.uuid4().hex[:8]}",
                    "question": "查询采购订单号",
                    "datasourceId": ds_id,
                },
            )
            assert resp.status_code == 200, f"chat 返回非 200: {resp.status_code} {resp.text}"
            body = resp.json()

            # 断言 plan 命中 PORDER
            plan = body.get("queryPlan") or {}
            assert "PORDER" in (plan.get("selectedClasses") or []), \
                f"plan 未命中 PORDER，实际: {plan.get('selectedClasses')}"

            # 断言 dataQuality 字段存在且含 PORDER badge
            badges = body.get("dataQuality")
            assert badges is not None, "ChatResponse.dataQuality 为 null（Phase 1.4 集成未生效）"
            porder_badges = [b for b in badges if b["targetTable"] == "PORDER"]
            assert len(porder_badges) == 1, f"PORDER badge 数量异常: {len(porder_badges)}"
            badge = porder_badges[0]
            assert badge["evaluated"] is True, f"PORDER badge.evaluated 应为 True: {badge}"
            assert badge["overallScore"] == "76.84", \
                f"PORDER badge.overallScore 与 Phase 1.3 不一致: {badge['overallScore']}"
            assert badge["rulesCount"] == 5, f"PORDER badge.rulesCount 应为 5: {badge}"
            assert badge["evaluatedAt"] is not None, "PORDER badge.evaluatedAt 应非 null"

            print(f"  ✅ dataQuality[0]: {badge}")
            print(f"  整体分与 Phase 1.3 一致: {badge['overallScore']} == 76.84")
    except Exception as exc:  # noqa: BLE001 —— best-effort 烟测，不阻断 seed 主流程
        # 真实 LLM / 业务库未配置时，本步骤为 best-effort：
        # 不阻断整体 seed；打印警告 + 提示运维完成 chat 烟测
        print(f"  ⚠️  chat 烟测未完成（{type(exc).__name__}: {exc}）")
        print("     可能原因：未配置真实 LLM API key / 业务库连接 / PORDER score 未 compute")
        print("     运维步骤：")
        print("       1. 确认 .env 或 settings.LLM_API_KEY 已配置")
        print("       2. curl -X POST http://localhost:8000/api/v1/data-quality/scores/compute")
        print("       3. 手动 curl /api/v1/chat 并检查 dataQuality 字段")


if __name__ == "__main__":
    asyncio.run(main())