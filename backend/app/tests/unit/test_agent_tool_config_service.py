"""AgentToolConfigService CRUD 单元测试。

- 校验：name pattern、handler_kind/ref combo、data_layers 词表、version 乐观锁
- 不依赖真实 PG；用 MagicMock 模拟 ORM 操作 + outbox.enqueue

注意：session 整体用 MagicMock；只有真正的协程方法（execute / flush / delete）
才用 AsyncMock。return_value 整体必须是 MagicMock（同步 chain），否则
`.scalars().all()` 会拿到 AsyncMock 协程。
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.exc import ProgrammingError

from app.dependencies import CurrentUser
from app.domain.enums import AgentToolHandlerKind
from app.domain.exceptions import ConflictError, NotFoundError, ValidationError
from app.domain.schemas import AgentToolConfigCreate, AgentToolConfigUpdate
from app.services.agent_tool_config_service import AgentToolConfigService


def _admin() -> CurrentUser:
    return CurrentUser(userId="admin", roles=["admin"], departments=["IT"])


def _rowMock(**kw) -> MagicMock:
    """构造 AgentToolConfig 替身（含 ORM 字段）。"""
    defaults = dict(
        id=1, name="x", version=1, data_object="SUPPLIER",
        data_layers=[], input_schema={}, description="x",
        handler_kind="BUILTIN", handler_ref="supplier_360",
        arg_extractor_kind="supplier_key", enabled=True,
        created_time=None, updated_time=None,
    )
    defaults.update(kw)
    m = MagicMock()
    for k, v in defaults.items():
        setattr(m, k, v)
    return m


def _sessionWithExecute(execute_returns: list[MagicMock]) -> MagicMock:
    """构造 session：每次 execute 调用按顺序返回 execute_returns 中的元素。"""
    session = MagicMock()
    session.execute = AsyncMock(side_effect=execute_returns)
    session.flush = AsyncMock()
    session.delete = AsyncMock()
    session.add = MagicMock()
    session.rollback = AsyncMock()
    return session


@pytest.fixture
def service():
    outbox = MagicMock()
    outbox.enqueue = AsyncMock()
    return AgentToolConfigService(outbox=outbox)


class _SettingsStub:
    """最小 settings 替身：deleteTool 只用到 enableAgentOrchestration。"""

    def __init__(self, enabled: bool) -> None:
        self.enableAgentOrchestration = enabled


def _serviceWithFlag(enabled: bool) -> AgentToolConfigService:
    outbox = MagicMock()
    outbox.enqueue = AsyncMock()
    return AgentToolConfigService(outbox=outbox, settings=_SettingsStub(enabled))


def _missingOrchestrationTable() -> ProgrammingError:
    """asyncpg「relation does not exist」在 SQLAlchemy 侧的形态（DBAPI ProgrammingError）。"""
    return ProgrammingError(
        "SELECT agent_definition.agent_code ...",
        params=None,
        orig=Exception('relation "agent_orchestration_profile" does not exist'),
    )


def _executedSqlMentions(session: MagicMock, needle: str) -> bool:
    """session.execute 过的 SQL 里是否出现过某个标识符（表名片段）。"""
    for call in session.execute.await_args_list:
        statement = call.args[0]
        compiled = str(statement.compile(compile_kwargs={"literal_binds": True}))
        if needle in compiled:
            return True
    return False


class TestListTools:
    @pytest.mark.asyncio
    async def test_listTools_default_includes_disabled(self, service):
        r1, r2 = _rowMock(name="r1", enabled=True), _rowMock(name="r2", enabled=False)
        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = [r1, r2]
        session = _sessionWithExecute([result_mock])
        result = await service.listTools(session)
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_listTools_enabledOnly_filters(self, service):
        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = []
        session = _sessionWithExecute([result_mock])
        await service.listTools(session, enabledOnly=True)
        call_args = session.execute.call_args[0][0]
        compiled = str(call_args.compile(compile_kwargs={"literal_binds": True}))
        assert "enabled" in compiled.lower()


class TestGetTool:
    @pytest.mark.asyncio
    async def test_getTool_returns_row(self, service):
        row = _rowMock(name="supplier_360")
        result_mock = MagicMock(scalar_one_or_none=MagicMock(return_value=row))
        session = _sessionWithExecute([result_mock])
        result = await service.getTool(session, "supplier_360")
        assert result is row

    @pytest.mark.asyncio
    async def test_getTool_raises_404_when_missing(self, service):
        result_mock = MagicMock(scalar_one_or_none=MagicMock(return_value=None))
        session = _sessionWithExecute([result_mock])
        with pytest.raises(NotFoundError):
            await service.getTool(session, "nonexistent")


class TestCreateTool:
    @pytest.mark.asyncio
    async def test_createTool_normalizes_data_object(self, service):
        session = _sessionWithExecute([])
        dto = AgentToolConfigCreate(
            name="supplier_360",
            description="test",
            data_object="  supplier  ",
            data_layers=["DIM", "FEATURE"],
            input_schema={},
            handler_kind="BUILTIN",
            handler_ref="supplier_360",
        )
        await service.createTool(session, dto, _admin())
        session.add.assert_called_once()
        added = session.add.call_args[0][0]
        assert added.data_object == "SUPPLIER"
        assert added.handler_kind == "BUILTIN"
        assert added.version == 1

    @pytest.mark.asyncio
    async def test_createTool_emits_outbox(self, service):
        session = _sessionWithExecute([])
        dto = AgentToolConfigCreate(
            name="x", data_object="SUPPLIER",
            handler_kind="BUILTIN", handler_ref="supplier_360",
        )
        await service.createTool(session, dto, _admin())
        service._outbox.enqueue.assert_awaited_once()
        kwargs = service._outbox.enqueue.await_args.kwargs
        assert kwargs["event_type"] == "agent_tool_created"
        assert kwargs["entity_type"] == "agent_tool_config"
        assert kwargs["actor"] == "admin"

    @pytest.mark.asyncio
    async def test_createTool_409_on_duplicate_name(self, service):
        from sqlalchemy.exc import IntegrityError
        session = _sessionWithExecute([])
        session.flush.side_effect = IntegrityError(
            "dup", params=None, orig=Exception("uq_agent_tool_config_name")
        )
        dto = AgentToolConfigCreate(
            name="supplier_360",
            data_object="SUPPLIER",
            handler_kind="BUILTIN",
            handler_ref="supplier_360",
        )
        with pytest.raises(ConflictError):
            await service.createTool(session, dto, _admin())

    @pytest.mark.asyncio
    async def test_createTool_422_on_invalid_handler_combo(self, service):
        dto = AgentToolConfigCreate(
            name="bad",
            data_object="SUPPLIER",
            handler_kind="BUILTIN",
            handler_ref="nl2sql_default",
        )
        session = _sessionWithExecute([])
        with pytest.raises(ValidationError):
            await service.createTool(session, dto, _admin())


class TestUpdateTool:
    @pytest.mark.asyncio
    async def test_updateTool_with_unset_fields_skips(self, service):
        existing = _rowMock(name="supplier_360", version=1, description="x")
        result_mock = MagicMock(scalar_one_or_none=MagicMock(return_value=existing))
        session = _sessionWithExecute([result_mock])
        dto = AgentToolConfigUpdate(version=1)  # 全 UNSET
        await service.updateTool(session, "supplier_360", dto, _admin())
        assert existing.version == 2  # 仍 +1
        service._outbox.enqueue.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_updateTool_applies_set_fields(self, service):
        existing = _rowMock(name="x", version=1, description="old", data_object="SUPPLIER")
        result_mock = MagicMock(scalar_one_or_none=MagicMock(return_value=existing))
        session = _sessionWithExecute([result_mock])
        dto = AgentToolConfigUpdate(
            version=1, description="new", data_object="  VENDOR  "
        )
        await service.updateTool(session, "x", dto, _admin())
        assert existing.description == "new"
        assert existing.data_object == "VENDOR"

    @pytest.mark.asyncio
    async def test_updateTool_409_on_version_mismatch(self, service):
        existing = _rowMock(name="supplier_360", version=3)
        result_mock = MagicMock(scalar_one_or_none=MagicMock(return_value=existing))
        session = _sessionWithExecute([result_mock])
        dto = AgentToolConfigUpdate(version=2, description="new")
        with pytest.raises(ConflictError):
            await service.updateTool(session, "supplier_360", dto, _admin())

    @pytest.mark.asyncio
    async def test_updateTool_rejects_kind_ref_mismatch(self, service):
        existing = _rowMock(
            name="x", version=1, handler_kind="BUILTIN", handler_ref="supplier_360",
        )
        result_mock = MagicMock(scalar_one_or_none=MagicMock(return_value=existing))
        session = _sessionWithExecute([result_mock])
        dto = AgentToolConfigUpdate(version=1, handler_kind=AgentToolHandlerKind.NL2SQL)
        with pytest.raises(ValidationError):
            await service.updateTool(session, "x", dto, _admin())


class TestDeleteTool:
    @pytest.mark.asyncio
    async def test_deleteTool_409_when_referenced_by_agent(self, service):
        row = _rowMock(name="supplier_360")
        get_mock = MagicMock(scalar_one_or_none=MagicMock(return_value=row))
        refs_mock = MagicMock()
        refs_mock.scalars.return_value.all.return_value = ["SUPPLIER_360_AGENT"]
        no_orch_mock = MagicMock()
        no_orch_mock.scalars.return_value.all.return_value = []
        session = _sessionWithExecute([get_mock, refs_mock, no_orch_mock])
        with pytest.raises(ConflictError) as exc_info:
            await service.deleteTool(session, "supplier_360", _admin())
        assert exc_info.value.referencing_agents == ["SUPPLIER_360_AGENT"]

    @pytest.mark.asyncio
    async def test_deleteTool_success(self, service):
        row = _rowMock(name="supplier_360", id=42)
        get_mock = MagicMock(scalar_one_or_none=MagicMock(return_value=row))
        no_refs_mock = MagicMock()
        no_refs_mock.scalars.return_value.all.return_value = []
        no_orch_mock = MagicMock()
        no_orch_mock.scalars.return_value.all.return_value = []
        session = _sessionWithExecute([get_mock, no_refs_mock, no_orch_mock])
        await service.deleteTool(session, "supplier_360", _admin())
        session.delete.assert_awaited_once_with(row)
        service._outbox.enqueue.assert_awaited_once()
        kwargs = service._outbox.enqueue.await_args.kwargs
        assert kwargs["event_type"] == "agent_tool_deleted"
        assert kwargs["payload"]["after"] is None


class TestDeleteToolOrchestrationFlag:
    """编排特性开关对 deleteTool 的影响（I2）。

    - flag 关闭 ⇒ **完全不查**编排表（编排表在未迁移到 0135 的库上可能不存在）
    - flag 打开但编排表不存在 ⇒ fail-soft（降级为「没有编排引用」）而不是 500
    """

    @pytest.mark.asyncio
    async def test_deleteTool_flagOff_never_queries_orchestration_table(self):
        service = _serviceWithFlag(False)
        row = _rowMock(name="supplier_360", id=42)
        get_mock = MagicMock(scalar_one_or_none=MagicMock(return_value=row))
        no_refs_mock = MagicMock()
        no_refs_mock.scalars.return_value.all.return_value = []
        session = _sessionWithExecute([get_mock, no_refs_mock])

        await service.deleteTool(session, "supplier_360", _admin())

        # 只查了 2 次：工具行本身 + 历史 AgentDefinition.tool_name 引用
        assert session.execute.await_count == 2
        assert not _executedSqlMentions(session, "agent_orchestration")
        session.delete.assert_awaited_once_with(row)

    @pytest.mark.asyncio
    async def test_deleteTool_flagOff_survives_missing_orchestration_tables(self):
        """flag 关闭时即使编排表不存在也不会抛（表缺失 ⇒ 查询失败 ⇒ 不能到查询那步）。"""
        service = _serviceWithFlag(False)
        row = _rowMock(name="supplier_360", id=42)
        get_mock = MagicMock(scalar_one_or_none=MagicMock(return_value=row))
        no_refs_mock = MagicMock()
        no_refs_mock.scalars.return_value.all.return_value = []
        # 第 3 次执行会抛「表不存在」—— flag 关闭时它根本不该发生
        session = _sessionWithExecute(
            [get_mock, no_refs_mock, _missingOrchestrationTable()]
        )

        await service.deleteTool(session, "supplier_360", _admin())

        assert session.execute.await_count == 2
        session.delete.assert_awaited_once_with(row)

    @pytest.mark.asyncio
    async def test_deleteTool_flagOn_missing_orchestration_tables_fails_soft(self):
        """flag 打开但库还没迁移到 0135 ⇒ 降级为「无编排引用」，不抛 500。"""
        service = _serviceWithFlag(True)
        row = _rowMock(name="supplier_360", id=42)
        get_mock = MagicMock(scalar_one_or_none=MagicMock(return_value=row))
        no_refs_mock = MagicMock()
        no_refs_mock.scalars.return_value.all.return_value = []
        session = _sessionWithExecute(
            [get_mock, no_refs_mock, _missingOrchestrationTable()]
        )

        await service.deleteTool(session, "supplier_360", _admin())

        assert session.execute.await_count == 3
        assert _executedSqlMentions(session, "agent_orchestration")
        session.delete.assert_awaited_once_with(row)
        service._outbox.enqueue.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_deleteTool_reads_flag_at_call_time(self, monkeypatch):
        """settings 缺省时开关在调用点读取；环境变更后须清 getSettings 缓存。"""
        from app.config import getSettings

        outbox = MagicMock()
        outbox.enqueue = AsyncMock()
        service = AgentToolConfigService(outbox=outbox)

        def _buildSession() -> MagicMock:
            row = _rowMock(name="supplier_360", id=42)
            get_mock = MagicMock(scalar_one_or_none=MagicMock(return_value=row))
            no_refs_mock = MagicMock()
            no_refs_mock.scalars.return_value.all.return_value = []
            no_orch_mock = MagicMock()
            no_orch_mock.scalars.return_value.all.return_value = []
            return _sessionWithExecute([get_mock, no_refs_mock, no_orch_mock])

        monkeypatch.setenv("ENABLE_AGENT_ORCHESTRATION", "false")
        getSettings.cache_clear()
        offSession = _buildSession()
        await service.deleteTool(offSession, "supplier_360", _admin())
        assert offSession.execute.await_count == 2

        monkeypatch.setenv("ENABLE_AGENT_ORCHESTRATION", "true")
        getSettings.cache_clear()
        onSession = _buildSession()
        await service.deleteTool(onSession, "supplier_360", _admin())
        assert onSession.execute.await_count == 3


class TestToggleEnabled:
    @pytest.mark.asyncio
    async def test_toggleEnabled_updates_row(self, service):
        existing = _rowMock(name="supplier_360", version=1, enabled=True)
        result_mock = MagicMock(scalar_one_or_none=MagicMock(return_value=existing))
        session = _sessionWithExecute([result_mock])
        result = await service.toggleEnabled(session, "supplier_360", False, _admin())
        assert result.enabled is False
        assert result.version == 2


class TestUpsertSeed:
    @pytest.mark.asyncio
    async def test_upsertSeed_insert_when_missing(self, service):
        result_mock = MagicMock(scalar_one_or_none=MagicMock(return_value=None))
        session = _sessionWithExecute([result_mock])
        fields = {
            "name": "supplier_360",
            "description": "x",
            "data_object": "SUPPLIER",
            "data_layers": ["DIM"],
            "handler_kind": "BUILTIN",
            "handler_ref": "supplier_360",
        }
        await service.upsertSeed(session, "supplier_360", fields)
        session.add.assert_called_once()

    @pytest.mark.asyncio
    async def test_upsertSeed_update_when_exists(self, service):
        existing = _rowMock(
            name="supplier_360", version=5, description="old",
            data_layers=["DIM"], data_object="SUPPLIER",
        )
        result_mock = MagicMock(scalar_one_or_none=MagicMock(return_value=existing))
        session = _sessionWithExecute([result_mock])
        fields = {
            "name": "supplier_360",
            "description": "new",
            "data_object": "SUPPLIER",
            "data_layers": ["DIM", "FEATURE"],
            "handler_kind": "BUILTIN",
            "handler_ref": "supplier_360",
            "arg_extractor_kind": "supplier_key",
        }
        await service.upsertSeed(session, "supplier_360", fields)
        assert existing.description == "new"
        assert existing.data_layers == ["DIM", "FEATURE"]
        session.add.assert_not_called()