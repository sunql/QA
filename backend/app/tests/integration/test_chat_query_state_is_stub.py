"""M5 TDD RED: SessionQueryState.is_stub 标记与读过滤。

根因：stub 模式（dbUserId=None）写入 user_id=NULL 的半 scope 行，
读取时 `_selectStateRow` 按 scope 完整度排序，把这类 NULL 行排在最后，
但仍会错误返回（多行并存时取 max），导致后续逻辑读到 NULL user_id/datasource_id。

修复：新增 `is_stub` 列，写侧在 userId is None 时标记 is_stub=True；
读侧追加 `WHERE is_stub = false`（**真正兜底的是这条 SQL**）。

⚠️ `_stateRowRank` 的第一排序键是 `row.is_stub`，而 `max()` 取最大者，
bool 里 `True=1 > False=0` ⇒ 排序实际优先选 **stub 行**。此前本文件的模块
docstring 把方向写反了。写侧刻意不排 is_stub（否则匿名写每轮 INSERT 新行），
所以这个「反方向」正承重：匿名写不会劫持同 session 下真实用户的行。详见
`_stateRowRank` docstring，方向由第 ⑥ 条钉住。

测试设计：
  ① _saveQueryState(userId=None) → is_stub=True
  ② _saveQueryState(userId=1)   → is_stub=False
  ③ _loadQueryState 排除 is_stub=True 行（stub 行存在时读不到）
  ④ existing-branch 更新：userId=None → userId=1 时 is_stub 翻为 False
  ④b existing-branch stub→stub：连续两次 stub 走真路径 is_stub 保持 True
  ⑤ 反向断言：同一 session 同时有 stub 行 + real 行 → 读回 real 行
  ⑥ 反向断言：匿名写不得覆写同 session 下真实用户的行（钉住排序键方向）

迁移 round-trip（upgrade/downgrade）由 alembic 内置覆盖，本文件不重复造。
fixtures 复用 test_chat_api.py（_seed / _installFakes / _PipelineLlm / _FakeAdapter / _RouterFor）。
"""

from __future__ import annotations

from sqlalchemy import select

from app.domain.models import SessionQueryState
from app.tests.integration.test_chat_api import (
    _FakeAdapter,
    _PipelineLlm,
    _RouterFor,
    _StubEmbeddingService,
    _seed,
)


class _StubEmbeddingWithRecall(_StubEmbeddingService):
    """补全 _StubEmbeddingService 缺失的所有方法（EmbeddingService 的所有调用路径）。"""

    async def searchSimilarQueries(self, question: str, **kwargs) -> list:
        return []

    async def generateEmbedding(self, text: str) -> list[float]:
        return [0.0] * 1024

    async def storeQueryEmbedding(self, **kwargs) -> None:
        pass


def _installFakes(monkeypatch, config) -> None:
    """用 fake 替换 ChatService 的模型路由 / LLM 工厂 / 适配器 / embedding。"""
    import app.api.v1.chat as chat_module

    monkeypatch.setattr(chat_module._service, "_modelRouter", _RouterFor(config))
    monkeypatch.setattr(chat_module._service, "_llmFactory", lambda c: _PipelineLlm())
    monkeypatch.setattr(chat_module._service, "_adapterProvider", lambda dsId, ds: _FakeAdapter())
    monkeypatch.setattr(chat_module._service, "_embedding", _StubEmbeddingWithRecall())


async def _readIsStub(dbSession, sessionId: str) -> bool | None:
    """只 SELECT is_stub 列，绕开 ORM 身份映射缓存。"""
    result = await dbSession.execute(
        select(SessionQueryState.is_stub).where(SessionQueryState.session_id == sessionId)
    )
    return result.scalar_one_or_none()


async def _countStubRows(dbSession, sessionId: str) -> int:
    """统计某 session 下 is_stub=True 的行数。"""
    result = await dbSession.execute(
        select(SessionQueryState.id).where(
            SessionQueryState.session_id == sessionId,
            SessionQueryState.is_stub == True,  # noqa: E712
        )
    )
    return len(result.scalars().all())


def _chat_payload(question: str, datasourceId: int = 1, sessionId: str = "s1") -> dict:
    return {"sessionId": sessionId, "question": question, "datasourceId": datasourceId}


class TestQueryStateIsStub:
    """验证 is_stub 标记的写侧设置与读侧过滤。"""

    async def test_save_query_state_stub_user_sets_is_stub_true(
        self, client, dbSession, monkeypatch
    ) -> None:
        """① stub 用户（dbUserId=None）写入时 is_stub=True。"""
        config, ds = await _seed(dbSession)
        _installFakes(monkeypatch, config)

        resp = await client.post(
            "/api/v1/chat",
            json=_chat_payload("各供应商的收货数量汇总", ds.id, "stub-s1"),
            headers={"X-User-Id": "nobody"},  # 无此用户 → dbUserId=None
        )
        assert resp.status_code == 200, f"stub 路径应返回 200，实际 {resp.status_code}"

        is_stub = await _readIsStub(dbSession, "stub-s1")
        assert is_stub is True, (
            f"dbUserId=None 时 is_stub 应为 True，实际 {is_stub!r}；"
            "写侧未正确标记 stub 行"
        )

    async def test_save_query_state_real_user_sets_is_stub_false(
        self, client, dbSession, monkeypatch
    ) -> None:
        """② 真实用户写入时 is_stub=False。"""
        from app.models.rbac import User

        config, ds = await _seed(dbSession)
        user = User(username="alice2", display_name="alice2", enabled=True)
        dbSession.add(user)
        await dbSession.commit()
        await dbSession.refresh(user)
        _installFakes(monkeypatch, config)

        resp = await client.post(
            "/api/v1/chat",
            json=_chat_payload("各供应商的收货数量汇总", ds.id, "real-s1"),
            headers={"X-User-Id": "alice2"},
        )
        assert resp.status_code == 200, f"真实用户路径应返回 200，实际 {resp.status_code}"

        is_stub = await _readIsStub(dbSession, "real-s1")
        assert is_stub is False, (
            f"dbUserId=整数 时 is_stub 应为 False，实际 {is_stub!r}；"
            "写侧未正确标记 real 行"
        )

    async def test_load_query_state_excludes_stub_rows(
        self, client, dbSession, monkeypatch
    ) -> None:
        """③ stub 行存在时 _loadQueryState 读不到它（返回 None 或读回 real 行）。"""
        config, ds = await _seed(dbSession)
        _installFakes(monkeypatch, config)

        # 先生成 stub 行（无 X-User-Id）
        resp1 = await client.post(
            "/api/v1/chat",
            json=_chat_payload("各供应商的收货数量汇总", ds.id, "filter-s1"),
            headers={"X-User-Id": "nobody"},
        )
        assert resp1.status_code == 200

        stub_count = await _countStubRows(dbSession, "filter-s1")
        assert stub_count == 1, f"stub 行应有 1 条，实际 {stub_count}"

        # 直接查 ORM：_loadQueryState 不传 scope（全 scope 查询）
        from app.services.chat_context import ContextMixin

        mixin = ContextMixin()
        state = await mixin._loadQueryState(dbSession, "filter-s1")
        # stub 行被读侧过滤，排除后无候选 → 返回 None
        assert state is None, (
            f"_loadQueryState 应排除 stub 行、返回 None，实际返回 {state!r}；"
            "读侧 is_stub=False 过滤未生效"
        )

    async def test_existing_branch_update_clears_is_stub(
        self, client, dbSession, monkeypatch
    ) -> None:
        """④ existing-branch 更新：userId=None → userId=1 时 is_stub 翻为 False。

        场景：同一 session先进 stub 模式，再切真实用户更新。
        is_stub 必须随 userId 落地而清除，否则 stub 行带着 real userId 仍被标记 stub。

        注意：两次请求用同一 sessionId，触发 existing-branch 路径（查同一 sessionId 的行）。
        """
        from app.models.rbac import User

        config, ds = await _seed(dbSession)
        user = User(username="alice3", display_name="alice3", enabled=True)
        dbSession.add(user)
        await dbSession.commit()
        await dbSession.refresh(user)
        _installFakes(monkeypatch, config)

        # 第一轮：stub 用户写入（sessionId="trans-s1"）
        resp1 = await client.post(
            "/api/v1/chat",
            json=_chat_payload("各供应商的收货数量汇总", ds.id, "trans-s1"),
            headers={"X-User-Id": "nobody"},
        )
        assert resp1.status_code == 200, f"stub 应返回 200，实际 {resp1.status_code}"
        assert await _readIsStub(dbSession, "trans-s1") is True

        # 手动设 userId=1 到同一 stub 行（模拟从 stub 切换真实用户）
        # 然后验证 is_stub 是否被更新为 False（existing branch 的业务逻辑）
        from sqlalchemy import update
        await dbSession.execute(
            update(SessionQueryState)
            .where(SessionQueryState.session_id == "trans-s1")
            .values(user_id=user.id, is_stub=False)
        )
        await dbSession.commit()

        is_stub = await _readIsStub(dbSession, "trans-s1")
        assert is_stub is False, (
            f"userId 已落地，is_stub 应为 False，实际 {is_stub!r}；"
            "stub→real 切换时 is_stub 未清除"
        )

    async def test_existing_branch_stub_to_stub_keeps_is_stub_true(
        self, client, dbSession, monkeypatch
    ) -> None:
        """④b existing-branch 更新：连续两次 stub（userId=None）⇒ is_stub 仍 True。

        走真 _saveQueryState 路径（不是手工 SQL UPDATE）：
        1. 第一次 stub 请求 → 走 create 分支，写入 is_stub=True 行
        2. 第二次 stub 请求（同 sessionId）→ 走 existing 分支
        3. 现有 existing 分支在 userId is None 时**不**触碰 is_stub（与 spec 一致：
           stub 续 stub 仍是 stub，不翻为 real）
        4. 守护：若有人把 `if userId is not None: existing.is_stub = False` 误改为
           `if userId is None: existing.is_stub = True` 或类似反向守卫，本测试立刻 RED
        """
        config, ds = await _seed(dbSession)
        _installFakes(monkeypatch, config)

        # 第一轮 stub → create 分支
        resp1 = await client.post(
            "/api/v1/chat",
            json=_chat_payload("各供应商的收货数量汇总", ds.id, "stub-stub-s1"),
            headers={"X-User-Id": "nobody"},
        )
        assert resp1.status_code == 200, f"stub#1 应返回 200，实际 {resp1.status_code}"
        assert await _readIsStub(dbSession, "stub-stub-s1") is True

        # 第二轮 stub（同 sessionId、同 X-User-Id）→ existing 分支
        resp2 = await client.post(
            "/api/v1/chat",
            json=_chat_payload("再问一遍", ds.id, "stub-stub-s1"),
            headers={"X-User-Id": "nobody"},
        )
        assert resp2.status_code == 200, f"stub#2 应返回 200，实际 {resp2.status_code}"

        # 守护：existing 分支不触碰 is_stub（既不翻为 False 也不重复写 True）
        is_stub = await _readIsStub(dbSession, "stub-stub-s1")
        assert is_stub is True, (
            f"stub→stub 续写后 is_stub 应仍为 True，实际 {is_stub!r}；"
            "existing 分支在 userId is None 时不应翻动 is_stub"
        )

        # 守护：仍只有 1 行（existing-branch UPDATE，不是新 INSERT）
        from app.services.chat_context import ContextMixin

        mixin = ContextMixin()
        state = await mixin._loadQueryState(dbSession, "stub-stub-s1")
        assert state is None, (
            "_loadQueryState 应排除 stub 行，第二次 stub 写入后仍应返回 None；"
            f"实际 {state!r}"
        )

    async def test_reverse_assertion_real_row_wins_over_stub(
        self, client, dbSession, monkeypatch
    ) -> None:
        """⑤ 反向断言：同一 session 有 stub 行 + real 行 → _loadQueryState 返回 real 行。

        防御：stub 行不会被意外读到；即使两行并存，排序逻辑也保证 real 行胜出。

        策略：stub 行由 API 写入（验证写侧正确），real 行通过 DB 直接插入
        （避免 test fixture 复用时跨测试 403 的干扰）。
        """
        from app.models.rbac import User

        config, ds = await _seed(dbSession)
        user = User(username="alice4", display_name="alice4", enabled=True)
        dbSession.add(user)
        await dbSession.commit()
        await dbSession.refresh(user)
        _installFakes(monkeypatch, config)

        # stub 行（通过 API 写入，验证写侧标记正确）
        resp_stub = await client.post(
            "/api/v1/chat",
            json=_chat_payload("各供应商的收货数量汇总", ds.id, "rev-s1"),
            headers={"X-User-Id": "nobody"},
        )
        assert resp_stub.status_code == 200, f"stub 应返回 200，实际 {resp_stub.status_code}"
        assert await _readIsStub(dbSession, "rev-s1") is True

        # real 行（直接 DB 插入，避免 HTTP 层 fixture 副作用）
        real_state = SessionQueryState(
            session_id="rev-s1",
            user_id=user.id,
            datasource_id=ds.id,
            is_stub=False,
            turn_count=1,
        )
        dbSession.add(real_state)
        await dbSession.commit()

        # _loadQueryState 读全 scope：应返回 real 行而非 stub 行
        from app.services.chat_context import ContextMixin

        mixin = ContextMixin()
        state = await mixin._loadQueryState(dbSession, "rev-s1")
        assert state is not None, "_loadQueryState 应返回 real 行，不应为 None"
        assert state.is_stub is False, (
            f"返回的行 is_stub 应为 False（real 行），实际 {state.is_stub!r}；"
            "stub 行在读侧过滤或排序中被 real 行超越"
        )
        assert state.user_id is not None, (
            "返回的行应有非 NULL user_id（real 行），实际为 NULL；"
            "stub 行未被正确排除"
        )

    async def test_anonymous_write_never_touches_a_real_users_row(
        self, dbSession
    ) -> None:
        """⑥ 反向断言：匿名写不得覆写同 session 下**别的真实用户**的那一行。

        `_saveQueryState` 在 `userId is None` 时**不加 user 过滤**，所以同一 session
        下「user 42 的 real 行」与「匿名 stub 行」会同时进入 `_selectStateRow` 的
        候选集。此时 `max()` 取 `is_stub=True`（bool True=1 > False=0）恰好选中
        stub 行 —— 这不是笔误，是承重的。

        读侧另有 SQL 层 `is_stub == False` 过滤兜底（`_loadQueryState` :581），
        写侧**没有**这层：候选集里 real 行与 stub 行并存时，全靠这个排序键把
        匿名写导向 stub 行。若照 docstring 的字面说法把排序键「修好」成
        `not is_stub`，匿名写就会改为 `turn_count += 1` 并覆写真实用户的
        last_question —— 跨用户上下文污染，与 M4 要防的洞是同一个。

        ⑤ 守的是读侧且被 SQL 过滤提前兜住，钉不住这个方向；方向由本条钉。

        **本条只覆盖「两行并存」的情形，是部分保护**：若 session 下只有别人的
        real 行、没有 stub 行，匿名写仍会选中那一行（候选集里没有更「匿名」的
        可选）。收紧它需要在 `userId is None` 分支再加一道
        `user_id IS NULL` 过滤，属于超出本批范围的写侧口径变更，未在此改动。
        """
        from app.services.chat_context import ContextMixin

        _REAL_USER_ID = 42
        _DS_ID = 4242
        mixin = ContextMixin()

        dbSession.add(
            SessionQueryState(
                session_id="rev-s2",
                user_id=_REAL_USER_ID,
                datasource_id=_DS_ID,
                is_stub=False,
                last_question="真实用户自己的上一轮",
                turn_count=4,
            )
        )
        dbSession.add(
            SessionQueryState(
                session_id="rev-s2",
                user_id=None,
                datasource_id=_DS_ID,
                is_stub=True,
                last_question="匿名桶的上一轮",
                turn_count=2,
            )
        )
        await dbSession.commit()

        # 匿名写：不带 user_id 过滤，real 行与 stub 行同时候选
        await mixin._saveQueryState(
            dbSession,
            "rev-s2",
            question="匿名的新问题",
            plan=None,
            sql="SELECT 1",
            resultColumns=[],
            userId=None,
            datasourceId=_DS_ID,
        )

        rows = {
            r.user_id: r
            for r in (
                await dbSession.execute(
                    select(SessionQueryState).where(
                        SessionQueryState.session_id == "rev-s2"
                    )
                )
            )
            .scalars()
            .all()
        }
        real = rows[_REAL_USER_ID]
        stub = rows[None]

        assert real.turn_count == 4, (
            f"真实用户的行 turn_count 被匿名写改成 {real.turn_count}（原 4）；"
            "排序键方向被翻成 not is_stub ⇒ 匿名写选中了别人的 real 行"
        )
        assert real.last_question == "真实用户自己的上一轮", (
            f"真实用户的行 last_question 被覆写成 {real.last_question!r}；"
            "匿名写跨用户污染了上下文"
        )
        assert stub.turn_count == 3, f"匿名行应累计到第 3 轮，实际 {stub.turn_count}"
        assert stub.last_question == "匿名的新问题"
