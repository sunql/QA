"""数据质量评估报告 service 集成测试（feat-dq-evaluation-report，Phase 3）。

真实 PostgreSQL + EvaluationReportService 直接调用：
- CRUD：create / list / get / update / soft_delete / regenerate
- ACL：admin 全权；非 admin 仅 owner 部门成员可写
- 审计：每次写都落 audit_log
- 软删除：list 不返回已删；get 404
- 重名：同 created_by + name 冲突
- 引用校验：class_ids / rule_ids 必须在对应表里存在
- JSONB 过滤：class_id / rule_id / created_by / date 范围

为简化依赖，每测试直接造 ontology_class / data_quality_rule 行；FK 暂未加（迁移 0072
仅跨报告自身的 FK），所以直接 INSERT 即可。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text

from app.dependencies import CurrentUser
from app.domain.enums import RuleType, Severity
from app.domain.models import DataQualityRule, EvaluationReport, OntologyClass
from app.domain.schemas import (
    EvaluationReportCreate,
    EvaluationReportUpdate,
)
from app.services.evaluation_report_service import EvaluationReportService

ADMIN = CurrentUser(
    userId="test-admin",
    tenantId="default",
    roles=("admin",),
    departments=(),
    dbUserId=None,
)
OWNER = CurrentUser(
    userId="test-procurement",
    tenantId="default",
    roles=("user",),
    departments=("procurement",),
    dbUserId=None,
)
STRANGER = CurrentUser(
    userId="test-finance",
    tenantId="default",
    roles=("user",),
    departments=("finance",),
    dbUserId=None,
)


async def _make_datasource(dbSession, name: str = "ds-1") -> int:
    """造一个最小可用的 DataSource（password_encrypted 用 Fernet 加密）。"""
    from app.domain.enums import DataSourceType
    from app.domain.models import DataSource
    from app.infrastructure.security.crypto import encryptApiKey

    ds = DataSource(
        name=name,
        type=DataSourceType.POSTGRESQL,
        host="localhost",
        port=5432,
        database_name="test",
        username="test",
        password_encrypted=encryptApiKey("test"),
        is_active=True,
    )
    dbSession.add(ds)
    await dbSession.flush()
    return ds.id


async def _make_class(
    dbSession, class_name: str = "PURCHASE_ORDER"
) -> int:
    """INSERT ontology_class 行，返回 id。"""
    obj = OntologyClass(
        class_name=class_name,
        description="test",
        object_type="Transaction",
        object_owner="procurement",
    )
    dbSession.add(obj)
    await dbSession.flush()
    return obj.id


async def _make_rule(
    dbSession,
    rule_code: str = "R1",
    *,
    datasource_id: int | None = None,
    target_table: str = "PORDER",
    target_column: str = "QTY",
) -> int:
    if datasource_id is None:
        datasource_id = await _make_datasource(dbSession)
    obj = DataQualityRule(
        rule_name="r1",
        rule_code=rule_code,
        datasource_id=datasource_id,
        target_table=target_table,
        target_column=target_column,
        rule_type=RuleType.COMPLETENESS,
        threshold="95.00",
        severity=Severity.MEDIUM,
        is_enabled=True,
        version="v1",
    )
    dbSession.add(obj)
    await dbSession.flush()
    return obj.id


def _new_dto(
    name: str = "report-1",
    class_ids: list[int] | None = None,
    rule_ids: list[int] | None = None,
    **kwargs: Any,
) -> EvaluationReportCreate:
    return EvaluationReportCreate(
        name=name,
        class_ids=class_ids or [1],
        rule_ids=rule_ids or [1],
        time_window_start=datetime(2026, 9, 1, tzinfo=UTC),
        time_window_end=datetime(2026, 9, 14, tzinfo=UTC),
        tags=["weekly"],
        **kwargs,
    )


class TestCreateReport:
    async def test_create_writes_row_with_snapshot(self, dbSession) -> None:
        cid = await _make_class(dbSession)
        rid = await _make_rule(dbSession)
        await dbSession.commit()
        svc = EvaluationReportService()
        report = await svc.createReport(
            dbSession, _new_dto(name="r-create", class_ids=[cid], rule_ids=[rid]), ADMIN
        )
        assert report.id is not None
        assert report.name == "r-create"
        assert report.snapshot["schema_version"] == 1
        # Phase 4: snapshot 现在是真实计算结果；dimensions/tables/overall 都存在
        assert "dimensions" in report.snapshot
        assert "tables" in report.snapshot
        assert "overall" in report.snapshot
        assert report.snapshot["evaluated_rule_count"] == 1
        assert report.status == "PUBLISHED" or report.status.value == "PUBLISHED"
        assert report.created_by == ADMIN.userId

    async def test_create_rejects_unknown_class_id(self, dbSession) -> None:
        rid = await _make_rule(dbSession)
        await dbSession.commit()
        svc = EvaluationReportService()
        with pytest.raises(Exception, match="本体类"):
            await svc.createReport(
                dbSession,
                _new_dto(class_ids=[99999], rule_ids=[rid]),
                ADMIN,
            )

    async def test_create_rejects_unknown_rule_id(self, dbSession) -> None:
        cid = await _make_class(dbSession)
        await dbSession.commit()
        svc = EvaluationReportService()
        with pytest.raises(Exception, match="数据质量规则"):
            await svc.createReport(
                dbSession,
                _new_dto(class_ids=[cid], rule_ids=[99999]),
                ADMIN,
            )

    async def test_create_rejects_duplicate_name(self, dbSession) -> None:
        cid = await _make_class(dbSession)
        rid = await _make_rule(dbSession)
        await dbSession.commit()
        svc = EvaluationReportService()
        await svc.createReport(
            dbSession, _new_dto(name="dup", class_ids=[cid], rule_ids=[rid]), ADMIN
        )
        await dbSession.commit()
        with pytest.raises(Exception, match="dup"):
            await svc.createReport(
                dbSession, _new_dto(name="dup", class_ids=[cid], rule_ids=[rid]), ADMIN
            )

    async def test_create_audit_log_written(self, dbSession) -> None:
        cid = await _make_class(dbSession)
        rid = await _make_rule(dbSession)
        await dbSession.commit()
        svc = EvaluationReportService()
        report = await svc.createReport(
            dbSession, _new_dto(name="r-audit", class_ids=[cid], rule_ids=[rid]), ADMIN
        )
        await dbSession.commit()
        result = await dbSession.execute(
            text(
                "SELECT action, actor, entity_type FROM audit_log "
                "WHERE entity_type='evaluation_report' AND entity_id=:id"
            ),
            {"id": report.id},
        )
        rows = result.fetchall()
        assert len(rows) == 1
        assert rows[0].action == "CREATE"
        assert rows[0].actor == ADMIN.userId


class TestListAndGetReport:
    async def test_list_excludes_soft_deleted(self, dbSession) -> None:
        cid = await _make_class(dbSession)
        rid = await _make_rule(dbSession)
        await dbSession.commit()
        svc = EvaluationReportService()
        a = await svc.createReport(
            dbSession, _new_dto(name="a", class_ids=[cid], rule_ids=[rid]), ADMIN
        )
        b = await svc.createReport(
            dbSession, _new_dto(name="b", class_ids=[cid], rule_ids=[rid]), ADMIN
        )
        await dbSession.commit()
        await svc.softDeleteReport(dbSession, a.id, ADMIN)
        result = await svc.listReports(dbSession, limit=10, offset=0)
        ids = {r.id for r in result.rows}
        assert b.id in ids
        assert a.id not in ids
        # 至少还有一个「待删前的 b」；总数应当只算未删
        assert result.total == 1

    async def test_get_returns_404_for_soft_deleted(self, dbSession) -> None:
        cid = await _make_class(dbSession)
        rid = await _make_rule(dbSession)
        await dbSession.commit()
        svc = EvaluationReportService()
        report = await svc.createReport(
            dbSession, _new_dto(name="x", class_ids=[cid], rule_ids=[rid]), ADMIN
        )
        await dbSession.commit()
        await svc.softDeleteReport(dbSession, report.id, ADMIN)
        with pytest.raises(Exception, match="不存在"):
            await svc.getReport(dbSession, report.id)

    async def test_list_filter_by_class_id_jsonb(self, dbSession) -> None:
        c1 = await _make_class(dbSession, class_name="C1")
        c2 = await _make_class(dbSession, class_name="C2")
        rid = await _make_rule(dbSession)
        await dbSession.commit()
        svc = EvaluationReportService()
        r1 = await svc.createReport(
            dbSession, _new_dto(name="r1", class_ids=[c1], rule_ids=[rid]), ADMIN
        )
        await svc.createReport(
            dbSession, _new_dto(name="r2", class_ids=[c2], rule_ids=[rid]), ADMIN
        )
        await dbSession.commit()
        result = await svc.listReports(dbSession, class_id=c1, limit=10, offset=0)
        ids = {r.id for r in result.rows}
        assert r1.id in ids
        assert all(r.id != r1.id or r.class_ids == [c1] for r in result.rows)

    async def test_list_filter_by_created_by(self, dbSession) -> None:
        cid = await _make_class(dbSession)
        rid = await _make_rule(dbSession)
        await dbSession.commit()
        svc = EvaluationReportService()
        await svc.createReport(
            dbSession, _new_dto(name="by-admin", class_ids=[cid], rule_ids=[rid]), ADMIN
        )
        await svc.createReport(
            dbSession, _new_dto(name="by-owner", class_ids=[cid], rule_ids=[rid]), OWNER
        )
        await dbSession.commit()
        result = await svc.listReports(
            dbSession, created_by=OWNER.userId, limit=10, offset=0
        )
        names = {r.name for r in result.rows}
        assert "by-owner" in names
        assert "by-admin" not in names


class TestAclEnforcement:
    async def test_update_admin_always_allowed(self, dbSession) -> None:
        cid = await _make_class(dbSession)
        rid = await _make_rule(dbSession)
        await dbSession.commit()
        svc = EvaluationReportService()
        report = await svc.createReport(
            dbSession, _new_dto(name="acl-1", class_ids=[cid], rule_ids=[rid]), ADMIN
        )
        await dbSession.commit()
        await svc.updateReport(
            dbSession,
            report.id,
            EvaluationReportUpdate(name="renamed"),
            ADMIN,
        )
        reloaded = await svc.getReport(dbSession, report.id)
        assert reloaded.name == "renamed"

    async def test_update_owner_in_departments_allowed(self, dbSession) -> None:
        cid = await _make_class(dbSession)
        rid = await _make_rule(dbSession)
        await dbSession.commit()
        svc = EvaluationReportService()
        report = await svc.createReport(
            dbSession, _new_dto(name="acl-2", class_ids=[cid], rule_ids=[rid]), ADMIN
        )
        # 把 owner 改成 procurement
        row = await dbSession.get(EvaluationReport, report.id)
        row.owner = "procurement"
        await dbSession.commit()
        await svc.updateReport(
            dbSession,
            report.id,
            EvaluationReportUpdate(description="by owner"),
            OWNER,
        )
        reloaded = await svc.getReport(dbSession, report.id)
        assert reloaded.description == "by owner"

    async def test_update_stranger_forbidden(self, dbSession) -> None:
        cid = await _make_class(dbSession)
        rid = await _make_rule(dbSession)
        await dbSession.commit()
        svc = EvaluationReportService()
        report = await svc.createReport(
            dbSession, _new_dto(name="acl-3", class_ids=[cid], rule_ids=[rid]), ADMIN
        )
        row = await dbSession.get(EvaluationReport, report.id)
        row.owner = "procurement"
        await dbSession.commit()
        with pytest.raises(Exception, match="无权"):
            await svc.updateReport(
                dbSession,
                report.id,
                EvaluationReportUpdate(description="nope"),
                STRANGER,
            )

    async def test_delete_stranger_forbidden(self, dbSession) -> None:
        cid = await _make_class(dbSession)
        rid = await _make_rule(dbSession)
        await dbSession.commit()
        svc = EvaluationReportService()
        report = await svc.createReport(
            dbSession, _new_dto(name="acl-4", class_ids=[cid], rule_ids=[rid]), ADMIN
        )
        row = await dbSession.get(EvaluationReport, report.id)
        row.owner = "procurement"
        await dbSession.commit()
        with pytest.raises(Exception, match="无权"):
            await svc.softDeleteReport(dbSession, report.id, STRANGER)


class TestUpdate:
    async def test_update_changes_name_and_tags(self, dbSession) -> None:
        cid = await _make_class(dbSession)
        rid = await _make_rule(dbSession)
        await dbSession.commit()
        svc = EvaluationReportService()
        report = await svc.createReport(
            dbSession, _new_dto(name="upd-1", class_ids=[cid], rule_ids=[rid]), ADMIN
        )
        await dbSession.commit()
        await svc.updateReport(
            dbSession,
            report.id,
            EvaluationReportUpdate(
                name="upd-1-renamed",
                tags=["weekly", "q3"],
                status="DRAFT",
            ),
            ADMIN,
        )
        reloaded = await svc.getReport(dbSession, report.id)
        assert reloaded.name == "upd-1-renamed"
        assert reloaded.tags == ["weekly", "q3"]
        assert reloaded.status == "DRAFT" or reloaded.status.value == "DRAFT"

    async def test_update_rejects_duplicate_name(self, dbSession) -> None:
        cid = await _make_class(dbSession)
        rid = await _make_rule(dbSession)
        await dbSession.commit()
        svc = EvaluationReportService()
        a = await svc.createReport(
            dbSession, _new_dto(name="name-a", class_ids=[cid], rule_ids=[rid]), ADMIN
        )
        await svc.createReport(
            dbSession, _new_dto(name="name-b", class_ids=[cid], rule_ids=[rid]), ADMIN
        )
        await dbSession.commit()
        with pytest.raises(Exception, match="name-b"):
            await svc.updateReport(
                dbSession,
                a.id,
                EvaluationReportUpdate(name="name-b"),
                ADMIN,
            )


class TestRegenerateSnapshot:
    async def test_regenerate_replaces_snapshot(self, dbSession) -> None:
        cid = await _make_class(dbSession)
        rid = await _make_rule(dbSession)
        await dbSession.commit()
        svc = EvaluationReportService()
        report = await svc.createReport(
            dbSession, _new_dto(name="reg-1", class_ids=[cid], rule_ids=[rid]), ADMIN
        )
        await dbSession.commit()
        # Phase 4: 两次都走真实计算，snapshot 含 dimensions/tables
        assert "dimensions" in report.snapshot
        assert "tables" in report.snapshot
        out = await svc.regenerateSnapshot(dbSession, report.id, ADMIN)
        assert "dimensions" in out.snapshot
        assert "tables" in out.snapshot
        assert out.snapshot["schema_version"] == 1


class TestRunSnapshotJobForPublishedReport:
    """回归：MDM-test 报告 status=PUBLISHED 卡 PENDING 不评估。

    UI 默认 EvaluationReportCreate.status='PUBLISHED'，但 runSnapshotJob 原本只接受
    PENDING，导致 BackgroundTasks 启动后立即 return，progress.stage 永远 PENDING，
    用户看到「没有数据」。
    修复契约：runSnapshotJob 必须接受 PENDING **和** PUBLISHED，DRAFT 才跳过。

    测试用 mock dispatcher 跳过真实业务数据源连接（test infra 限定 sqlite meta-db，
    业务 datasource 是 PG，容器内连不上），但保留 runSnapshotJob 内部的 PG 读写。
    """

    async def test_run_snapshot_job_progresses_published_to_completed(
        self, dbSession
    ) -> None:
        cid = await _make_class(dbSession)
        rid = await _make_rule(dbSession)
        await dbSession.commit()
        svc = EvaluationReportService()

        # mock dispatcher.evaluateBatch：返回空结果，避免真实连业务数据源
        class _FakeBatch:
            results: list = []
            duration_ms: int = 0

        class _FakeDispatcher:
            async def evaluateBatch(self, session, rule_ids, time_window=None):
                return _FakeBatch()

            async def collectSamples(self, *args, **kwargs):
                # feat-sampling-error-visible (2026-09-15)：签名改 tuple[list, str|None]，
                # mock 同步返 ([], None) 表示「采样成功但 0 命中」。
                return [], None

        # 跳过 sample_service.collectSamples 链路（避免真实连业务数据源）
        class _FakeSampleService:
            async def sampleForRule(self, *args, **kwargs):
                return []

        report = await svc.createReport(
            dbSession,
            _new_dto(name="mdm-published", class_ids=[cid], rule_ids=[rid], status="PUBLISHED"),
            ADMIN,
        )
        await dbSession.commit()
        # 创建立刻：status=PUBLISHED、progress.stage=PENDING
        assert report.status == "PUBLISHED"
        assert (report.progress or {}).get("stage") == "PENDING"

        # 跑后台任务：status 应推进到 COMPLETED，progress.stage 也到 COMPLETED
        await svc.runSnapshotJob(
            report.id,
            dispatcher=_FakeDispatcher(),
            sample_service=_FakeSampleService(),
        )
        # runSnapshotJob 用自己 session 写库；dbSession identity map 缓存的旧对象
        # 会让 getReport 返回 stale。用 raw SQL 直查绕过。
        await dbSession.commit()
        row = (await dbSession.execute(
            text("SELECT status, progress FROM evaluation_report WHERE id = :id"),
            {"id": report.id},
        )).first()
        assert row is not None
        assert row[0] == "COMPLETED", (
            f"PUBLISHED 报告必须被评估推进；当前 status={row[0]}, progress={row[1]}"
        )
        import json as _json
        prog = row[1] if isinstance(row[1], dict) else _json.loads(row[1])
        assert prog.get("stage") == "COMPLETED"

    async def test_run_snapshot_job_skips_draft_only(
        self, dbSession
    ) -> None:
        """DRAFT 报告不应被 runSnapshotJob 评估（草稿语义保留）。"""
        cid = await _make_class(dbSession)
        rid = await _make_rule(dbSession)
        await dbSession.commit()
        svc = EvaluationReportService()
        report = await svc.createReport(
            dbSession,
            _new_dto(name="draft-only", class_ids=[cid], rule_ids=[rid], status="DRAFT"),
            ADMIN,
        )
        await dbSession.commit()
        assert report.status == "DRAFT"

        await svc.runSnapshotJob(report.id)
        await dbSession.commit()
        refreshed = await svc.getReport(dbSession, report.id)
        assert refreshed.status == "DRAFT", "DRAFT 不应被后台自动评估"
