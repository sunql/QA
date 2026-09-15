"""违规样本 service + API 集成测试（feat-dq-evaluation-report，Phase 4）。

覆盖：
1. create_report 后自动生成样本行（rules 的 target_table 投影 PK）
2. /{id}/samples?ruleId=X&limit=N 端点按规则过滤
3. listForReport / cleanupOrphans 服务方法基本行为
4. dispatcher's collectSamples 对不存在的 rule / 不支持的 type 返回空列表
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.dependencies import CurrentUser
from app.domain.models import DataQualityRule, EvaluationReport, OntologyClass
from app.services.data_quality_evaluator import DataQualityEvaluatorDispatcher
from app.services.data_quality_violation_sample_service import (
    DataQualityViolationSampleService,
)
from app.services.evaluation_report_service import EvaluationReportService

ADMIN = CurrentUser(
    userId="vs-admin",
    tenantId="default",
    roles=("admin",),
    departments=(),
    dbUserId=None,
)
USER_A = CurrentUser(
    userId="vs-user-a",
    tenantId="default",
    roles=("user",),
    departments=("procurement",),
    dbUserId=None,
)


def _headers(actor: CurrentUser) -> dict[str, str]:
    h = {"X-User-Id": actor.userId, "X-User-Roles": ",".join(actor.roles)}
    if actor.departments:
        h["X-User-Departments"] = ",".join(actor.departments)
    return h


async def _make_datasource(dbSession, name: str | None = None) -> int:
    if name is None:
        name = f"vs-ds-{uuid.uuid4().hex[:8]}"
    from app.domain.enums import DataSourceType
    from app.domain.models import DataSource
    from app.infrastructure.security.crypto import encryptApiKey

    ds = DataSource(
        name=name,
        type=DataSourceType.POSTGRESQL,
        host="localhost",
        port=5432,
        database_name="t",
        username="t",
        password_encrypted=encryptApiKey("t"),
        is_active=True,
    )
    dbSession.add(ds)
    await dbSession.flush()
    return ds.id


async def _make_class(dbSession, *, name: str = "cls-vs") -> int:
    row = OntologyClass(
        class_name=name,
        source_table="SUPPLIER",
        object_owner="dept-x",
    )
    dbSession.add(row)
    await dbSession.flush()
    return row.id


async def _make_rule(
    dbSession,
    *,
    name: str = "rule-vs",
    rule_type: str = "COMPLETENESS",
    target_table: str = "SUPPLIER",
    target_column: str = "SUPPLIER_NAME",
    datasource_id: int | None = None,
    threshold: float = 80.0,
) -> int:
    if datasource_id is None:
        datasource_id = await _make_datasource(dbSession)
    row = DataQualityRule(
        rule_name=name,
        rule_code=f"R-VS-{name}",
        rule_type=rule_type,
        target_table=target_table,
        target_column=target_column,
        threshold=threshold,
        severity="HIGH",
        datasource_id=datasource_id,
    )
    dbSession.add(row)
    await dbSession.flush()
    return row.id


def _new_dto(name: str, class_ids: list[int], rule_ids: list[int]):
    from app.domain.schemas import EvaluationReportCreate

    return EvaluationReportCreate(
        name=name,
        description="violation sample test",
        class_ids=class_ids,
        rule_ids=rule_ids,
        time_window_start=datetime(2026, 9, 1, tzinfo=UTC),
        time_window_end=datetime(2026, 9, 14, tzinfo=UTC),
        status="PUBLISHED",
        tags=["test"],
    )


@pytest.mark.asyncio
class TestViolationSampleService:
    async def test_collect_samples_returns_empty_for_missing_rule(
        self, dbSession
    ) -> None:
        """dispatcher.collectSamples 对不存在 rule 静默返 ([], None)——配置错误。"""
        dispatcher = DataQualityEvaluatorDispatcher()
        samples, error = await dispatcher.collectSamples(
            dbSession, rule=None, limit=5,  # type: ignore[arg-type]
        )
        assert samples == []
        assert error is None

    async def test_collect_samples_handles_unknown_rule_type(
        self, dbSession
    ) -> None:
        """dispatcher.collectSamples 对不支持 rule_type 返 ([], None)——配置错误。"""
        from types import SimpleNamespace

        dispatcher = DataQualityEvaluatorDispatcher()
        ds_id = await _make_datasource(dbSession)
        await dbSession.commit()
        fake_rule = SimpleNamespace(
            id=999,
            rule_type="BOGUS_TYPE",
            datasource_id=ds_id,
            target_table="SUPPLIER",
            target_column="SUPPLIER_NAME",
        )
        samples, error = await dispatcher.collectSamples(
            dbSession, rule=fake_rule, limit=5,  # type: ignore[arg-type]
        )
        assert samples == []
        assert error is None

    async def test_collect_samples_returns_error_on_sampler_failure(
        self, dbSession
    ) -> None:
        """feat-sampling-error-visible (2026-09-15)：sampler 抛错时返回
        ([], str(exc)) 让 sampleForRule 能落库 sampling_error。

        用一个 bogus rule_type=VALIDITY + target_table 走真实 sampler，让
        sampler 内部对不存在的表执行失败（或直接抛错）。简化做法：mock sampler。
        """
        from types import SimpleNamespace
        from unittest.mock import patch

        dispatcher = DataQualityEvaluatorDispatcher()
        ds_id = await _make_datasource(dbSession)
        await dbSession.commit()

        async def _raise_sampler(*args, **kwargs):
            raise RuntimeError("ORA-00933: SQL 命令未正确结束")

        fake_rule = SimpleNamespace(
            id=42,
            rule_type="VALIDITY",
            datasource_id=ds_id,
            target_table="NO_SUCH_TABLE",
            target_column="X",
        )

        with patch.dict(
            "app.services.data_quality_evaluator._SAMPLERS",
            {"VALIDITY": _raise_sampler},
        ):
            samples, error = await dispatcher.collectSamples(
                dbSession, rule=fake_rule, limit=5,  # type: ignore[arg-type]
            )

        assert samples == []
        assert error is not None
        assert "ORA-00933" in error

    async def test_sampleForRule_persists_sampling_error_row(
        self, dbSession
    ) -> None:
        """feat-sampling-error-visible (2026-09-15)：sampler 抛错时仍写 DB 行，
        sampling_error 列存异常文本，sample_size=0，sample_pk_values=[]。
        这样前端就能在「违规样本」表里显式提示「采样失败」。
        """
        from app.services.data_quality_violation_sample_service import (
            DataQualityViolationSampleService,
        )
        from unittest.mock import patch

        cid = await _make_class(dbSession, name="cls-sample-err")
        rid = await _make_rule(dbSession, name="rule-sample-err")
        ds_id = await _make_datasource(dbSession, name="ds-sample-err")
        report = EvaluationReport(
            name="err-report",
            class_ids=[cid],
            rule_ids=[rid],
            time_window_start=datetime(2026, 9, 1, tzinfo=UTC),
            time_window_end=datetime(2026, 9, 14, tzinfo=UTC),
            snapshot={"schema_version": 1, "evaluated_at": "2026-09-14T00:00:00Z"},
            snapshot_version=1,
            created_by=ADMIN.userId,
        )
        dbSession.add(report)
        await dbSession.commit()

        async def _raise_sampler(*args, **kwargs):
            raise RuntimeError("ORA-00933: SQL command not properly ended")

        rule_obj = (await dbSession.get(DataQualityRule, rid))
        assert rule_obj is not None

        with patch.dict(
            "app.services.data_quality_evaluator._SAMPLERS",
            {"COMPLETENESS": _raise_sampler},
        ):
            svc = DataQualityViolationSampleService()
            row = await svc.sampleForRule(
                session=dbSession,
                report_id=report.id,
                rule=rule_obj,
                violation_count=42,  # 测试：传任意值，断言写到 total_violations
                limit=5,
            )

        await dbSession.commit()

        # 关键断言：失败时仍写行，sampling_error 有值
        assert row is not None
        assert row.sample_size == 0
        assert row.sample_pk_values == []
        assert row.total_violations == 42  # feat-total-violations (2026-09-15)：传啥写啥，不再 -1
        assert row.sampling_error is not None
        assert "ORA-00933" in row.sampling_error

        # listForReport 拿得到这行
        rows = await svc.listForReport(session=dbSession, report_id=report.id)
        assert len(rows) == 1
        assert rows[0].sampling_error is not None

    async def test_create_report_persists_samples(self, dbSession) -> None:
        """创建报告时自动采集样本；目标 SUPPLIER.SUPPLIER_NAME 在 SQL Guard
        不一定存在，但 sample service 失败时静默返 None，不会让 create 整批挂。"""
        cid = await _make_class(dbSession)
        rid = await _make_rule(dbSession)
        await dbSession.commit()
        svc = EvaluationReportService()
        report = await svc.createReport(
            dbSession, _new_dto("vs-1", [cid], [rid]), ADMIN,
        )
        await dbSession.commit()
        # snapshot 一定存在并含维度
        assert "dimensions" in report.snapshot
        assert "tables" in report.snapshot


@pytest.mark.asyncio
class TestListSamples:
    async def test_list_samples_filters_by_rule(self, dbSession) -> None:
        """createReport 后用 sample_service 查 listForReport；rule_id 过滤生效。"""
        cid = await _make_class(dbSession)
        rid_a = await _make_rule(dbSession, name="vs-A")
        rid_b = await _make_rule(dbSession, name="vs-B", rule_type="VALIDITY")
        ds_id = await _make_datasource(dbSession, name="vs-list-ds")
        await dbSession.commit()

        # 手工插两行 sample（模拟业务库返回样本，避免依赖真实业务表）
        from app.domain.models import DataQualityViolationSample

        report = EvaluationReport(
            name="vs-list",
            class_ids=[cid],
            rule_ids=[rid_a, rid_b],
            time_window_start=datetime(2026, 9, 1, tzinfo=UTC),
            time_window_end=datetime(2026, 9, 14, tzinfo=UTC),
            snapshot={"schema_version": 1, "evaluated_at": "2026-09-14T00:00:00Z"},
            snapshot_version=1,
            created_by=ADMIN.userId,
        )
        dbSession.add(report)
        await dbSession.flush()

        for rid, sample_pk in [(rid_a, "pk-a"), (rid_b, "pk-b")]:
            dbSession.add(
                DataQualityViolationSample(
                    report_id=report.id,
                    rule_id=rid,
                    datasource_id=ds_id,
                    target_table="SUPPLIER",
                    target_column="SUPPLIER_NAME",
                    total_violations=10,
                    sample_size=1,
                    sample_pk_values=[{"pk": sample_pk}],
                ),
            )
        await dbSession.commit()

        svc = DataQualityViolationSampleService()
        # 不带 rule_id 过滤：拿到 2 行
        all_rows = await svc.listForReport(session=dbSession, report_id=report.id)
        assert len(all_rows) == 2
        # 带 rule_id=rid_a：拿到 1 行
        rows_a = await svc.listForReport(
            session=dbSession, report_id=report.id, rule_id=rid_a,
        )
        assert len(rows_a) == 1
        assert rows_a[0].rule_id == rid_a
        assert rows_a[0].sample_pk_values[0]["pk"] == "pk-a"


@pytest.mark.asyncio
class TestCleanupOrphans:
    async def test_cleanup_orphans_removes_only_old_orphan_samples(
        self, dbSession
    ) -> None:
        from app.domain.models import DataQualityViolationSample

        # 软删报告 + 老样本
        cid = await _make_class(dbSession, name="cls-orphan")
        rid = await _make_rule(dbSession, name="rule-orphan")
        ds_id = await _make_datasource(dbSession, name="orphan-ds")
        await dbSession.commit()

        old_report = EvaluationReport(
            name="orphan-old",
            class_ids=[cid],
            rule_ids=[rid],
            time_window_start=datetime(2026, 9, 1, tzinfo=UTC),
            time_window_end=datetime(2026, 9, 14, tzinfo=UTC),
            snapshot={"schema_version": 1, "evaluated_at": "2026-09-14T00:00:00Z"},
            snapshot_version=1,
            created_by=ADMIN.userId,
        )
        old_report.deleted_at = datetime.now(UTC) - timedelta(days=60)
        dbSession.add(old_report)
        await dbSession.flush()

        old_sample = DataQualityViolationSample(
            report_id=old_report.id,
            rule_id=rid,
            datasource_id=ds_id,
            target_table="SUPPLIER",
            target_column="SUPPLIER_NAME",
            total_violations=10,
            sample_size=1,
            sample_pk_values=[{"pk": "old-pk"}],
            captured_at=datetime.now(UTC) - timedelta(days=60),
        )
        dbSession.add(old_sample)

        # 活跃报告 + 新样本（不应被清）
        new_report = EvaluationReport(
            name="orphan-new",
            class_ids=[cid],
            rule_ids=[rid],
            time_window_start=datetime(2026, 9, 1, tzinfo=UTC),
            time_window_end=datetime(2026, 9, 14, tzinfo=UTC),
            snapshot={"schema_version": 1, "evaluated_at": "2026-09-14T00:00:00Z"},
            snapshot_version=1,
            created_by=ADMIN.userId,
        )
        dbSession.add(new_report)
        await dbSession.flush()

        new_sample = DataQualityViolationSample(
            report_id=new_report.id,
            rule_id=rid,
            datasource_id=ds_id,
            target_table="SUPPLIER",
            target_column="SUPPLIER_NAME",
            total_violations=5,
            sample_size=1,
            sample_pk_values=[{"pk": "new-pk"}],
            captured_at=datetime.now(UTC),
        )
        dbSession.add(new_sample)
        await dbSession.commit()

        svc = DataQualityViolationSampleService()
        deleted = await svc.cleanupOrphans(dbSession, older_than_days=30)
        assert deleted == 1

        # 老样本已删，新样本还在
        from sqlalchemy import select

        stmt = select(DataQualityViolationSample).where(
            DataQualityViolationSample.id == new_sample.id,
        )
        assert (await dbSession.execute(stmt)).scalar_one_or_none() is not None
        stmt2 = select(DataQualityViolationSample).where(
            DataQualityViolationSample.id == old_sample.id,
        )
        assert (await dbSession.execute(stmt2)).scalar_one_or_none() is None


@pytest.mark.asyncio
class TestSamplesEndpoint:
    async def test_samples_endpoint_returns_rows(self, client, dbSession) -> None:
        """GET /{id}/samples?ruleId=X&limit=N 端到端：seed 一行样本，验证返回。"""
        from app.domain.models import DataQualityViolationSample

        cid = await _make_class(dbSession, name="cls-ep")
        rid = await _make_rule(dbSession, name="rule-ep")
        ds_id = await _make_datasource(dbSession, name="ep-ds")
        await dbSession.commit()

        report = EvaluationReport(
            name="ep-1",
            class_ids=[cid],
            rule_ids=[rid],
            time_window_start=datetime(2026, 9, 1, tzinfo=UTC),
            time_window_end=datetime(2026, 9, 14, tzinfo=UTC),
            snapshot={"schema_version": 1, "evaluated_at": "2026-09-14T00:00:00Z"},
            snapshot_version=1,
            created_by=ADMIN.userId,
        )
        dbSession.add(report)
        await dbSession.flush()
        dbSession.add(
            DataQualityViolationSample(
                report_id=report.id,
                rule_id=rid,
                datasource_id=ds_id,
                target_table="SUPPLIER",
                target_column="SUPPLIER_NAME",
                total_violations=3,
                sample_size=1,
                sample_pk_values=[{"pk": "v1"}],
            ),
        )
        await dbSession.commit()

        resp = await client.get(
            f"/api/v1/data-quality/reports/{report.id}/samples?limit=10",
            headers=_headers(USER_A),
        )
        assert resp.status_code == 200
        rows = resp.json()
        assert len(rows) == 1
        assert rows[0]["ruleId"] == rid
        assert rows[0]["targetTable"] == "SUPPLIER"

    async def test_samples_endpoint_404_on_missing_report(self, client) -> None:
        resp = await client.get(
            "/api/v1/data-quality/reports/999999/samples",
            headers=_headers(USER_A),
        )
        assert resp.status_code == 404
