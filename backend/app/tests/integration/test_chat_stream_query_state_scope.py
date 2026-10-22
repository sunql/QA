"""F1/P0：流式（默认）路径的 SessionQueryState scope 绑定。

缺陷（基线 b81bf1e0 实测）：
- `chat_stream.py` 的 4 处读写漏传 `(userId, datasourceId)` —— L255
  `classifyAndRecall`、L802 / L1058 `_saveQueryState`；
- `chat_context.py` 的 `scalar_one_or_none()` 在同一 session 下「scoped 行 + NULL 行」
  并存时抛 `MultipleResultsFound` → 500。

**为什么必须钉 `done` 帧**：SSE 的 HTTP 状态码在第一个 yield 之前就定死了（200）。
生成器中途抛异常，客户端拿到的仍是 200 + 半截流 —— 只断言 `status_code == 200`
会把「补 scope 传错类型导致默认路径 500」判成绿（记忆
`qa-system-same-status-code-false-green`）。故每个用例都断言流**跑完了**。

**关于用例③直调 `_loadQueryState`**：项目约束要求走完整 API 链路，但那条约束针对的是
「用替身绕开真实 pipeline」（sqlite 内存库 / mock service）。用例③要构造的是
「同一 session 下 scoped 行与 NULL 行并存」这一**任何对话序列都造不出来**的历史遗留
形态（修复后所有写入点都带 scope），因此只能直接构造库内状态并直接调读取助手。
用例③同时保留一条 API 链路断言守住真实回归面，两者互补。
"""

from __future__ import annotations

from typing import NamedTuple

from sqlalchemy import select

from app.domain.models import DataSource, SessionQueryState
from app.infrastructure.security.crypto import encryptApiKey
from app.models.rbac import User
from app.tests.integration.test_chat_api import _seed
from app.tests.integration.test_chat_stream_api import (
    _installStreamFakes,
    _parseFrames,
)

_FOLLOW_UP_QUESTION = "那去年呢"
_FIRST_QUESTION = "各供应商的收货数量汇总"


async def _seedUser(dbSession, username: str) -> User:
    """写一个 enabled 用户，使 `getCurrentUser` 的 stub 头路径能解析出 dbUserId。

    没有它，`_user.dbUserId` 恒 None（integration 的 `client` 不带 X-User-Id，
    走 anonymous 兜底），scope 的 user_id 分支就永远测不到。
    """
    user = User(username=username, display_name=username, enabled=True)
    dbSession.add(user)
    await dbSession.commit()
    await dbSession.refresh(user)
    return user


async def _seedSecondDatasource(dbSession, name: str = "ERP") -> DataSource:
    other = DataSource(
        name=name,
        type="oracle",
        host="h2",
        port=1521,
        database_name="svc2",
        username="u2",
        password_encrypted=encryptApiKey("secret"),
        is_active=True,
    )
    dbSession.add(other)
    await dbSession.commit()
    await dbSession.refresh(other)
    return other


def _intentsOf(text: str) -> list[str]:
    return [d["intent"] for e, d in _parseFrames(text) if e == "meta" and "intent" in d]


def _hasDone(text: str) -> bool:
    return any(e == "done" for e, _ in _parseFrames(text))


class _StateRow(NamedTuple):
    """状态行的只读投影 —— 只取断言真正用到的列。"""

    user_id: int | None
    datasource_id: int | None
    turn_count: int
    last_question: str | None


async def _readStateRows(dbSession, sessionId: str = "s1") -> list[_StateRow]:
    """按列读回状态行。

    **刻意绕过 ORM 实体**：`dbSession` 与 HTTP 请求走两个不同的会话，ORM 身份映射会把
    先前 SELECT 到的实例原样返回，属性全是内存旧值 —— 断言 `turn_count` 会永远读到
    第一次 SELECT 时的快照（一个「测的不是事实」的假绿）。`expire_all()` 也踩不动：
    它会把属性访问推迟到懒加载的异步上下文里，触发 `MissingGreenlet`。
    只 SELECT 这几个列、拿裸标量，是这里唯一诚实的读法。
    """
    result = await dbSession.execute(
        select(
            SessionQueryState.user_id,
            SessionQueryState.datasource_id,
            SessionQueryState.turn_count,
            SessionQueryState.last_question,
        ).where(SessionQueryState.session_id == sessionId)
    )
    return [_StateRow(*row) for row in result.all()]


async def _onlyStateRow(dbSession, sessionId: str = "s1") -> _StateRow:
    rows = await _readStateRows(dbSession, sessionId)
    assert len(rows) == 1, (
        f"该 session 下应只有一行状态，实际 {[(r.user_id, r.datasource_id) for r in rows]}"
    )
    return rows[0]


class TestStreamQueryStateScope:
    async def test_stream_persists_user_and_datasource_scope(
        self, client, dbSession, monkeypatch
    ) -> None:
        """① 流式主查询成功后，库里该 session 行的 user_id/datasource_id 非 NULL。"""
        config, ds = await _seed(dbSession)
        user = await _seedUser(dbSession, "alice")
        _installStreamFakes(monkeypatch, config)

        resp = await client.post(
            "/api/v1/chat/stream",
            json={"sessionId": "s1", "question": _FIRST_QUESTION, "datasourceId": ds.id},
            headers={"X-User-Id": "alice"},
        )
        assert resp.status_code == 200, resp.text
        assert _hasDone(resp.text), f"流未跑完（缺 done 帧），实际事件：{[e for e, _ in _parseFrames(resp.text)]}"

        rows = list(
            (
                await dbSession.execute(
                    select(SessionQueryState).where(SessionQueryState.session_id == "s1")
                )
            ).scalars().all()
        )
        assert rows, "流式成功后一个 SessionQueryState 行都没有 —— scope 断言会假绿"
        for row in rows:
            assert row.user_id is not None, "user_id 为 NULL：流式写入漏传 scope"
            assert row.datasource_id is not None, "datasource_id 为 NULL：流式写入漏传 scope"
            assert row.user_id == user.id
            assert row.datasource_id == ds.id

    async def test_cross_datasource_second_turn_does_not_inherit_state(
        self, client, dbSession, monkeypatch
    ) -> None:
        """② 跨数据源同 sessionId 的第二轮拿不到上一轮的 state。

        判据用**重分类产生的第二个 meta 帧**：`classifyAndRecall` 只有在读到 state 时
        才用 `hasPriorState=True` 重发一次 meta。基线上跨数据源第二轮照样读到上一轮
        state（串味），表现为 meta 序列是 `['clarify', 'follow_up']`。

        同一个用例里再跑一次**同数据源**第二轮作对照组：它必须仍是两帧 —— 否则
        「只有一帧」可能只是因为 meta 帧机制变了，而不是因为 scope 生效了。
        """
        config, ds = await _seed(dbSession)
        other = await _seedSecondDatasource(dbSession)
        user = await _seedUser(dbSession, "alice")
        _installStreamFakes(monkeypatch, config)
        headers = {"X-User-Id": "alice"}

        first = await client.post(
            "/api/v1/chat/stream",
            json={"sessionId": "s1", "question": _FIRST_QUESTION, "datasourceId": ds.id},
            headers=headers,
        )
        assert first.status_code == 200, first.text
        assert _hasDone(first.text)

        crossDatasource = await client.post(
            "/api/v1/chat/stream",
            json={
                "sessionId": "s1",
                "question": _FOLLOW_UP_QUESTION,
                "datasourceId": other.id,
            },
            headers=headers,
        )
        assert crossDatasource.status_code == 200, crossDatasource.text
        assert _hasDone(crossDatasource.text)
        assert _intentsOf(crossDatasource.text) == ["clarify"], (
            "跨数据源的第二轮读到了别的数据源留下的 state："
            f"实际 meta 序列 {_intentsOf(crossDatasource.text)}"
        )

        # 写侧断言：既然没读到锚点，这一轮就走澄清、**不会**产生查询状态，
        # 上一轮那行必须原封不动。修复前它会被当成同数据源的续轮覆盖（turn_count 变 2）。
        afterCross = await _onlyStateRow(dbSession)
        assert afterCross.datasource_id == ds.id
        assert afterCross.turn_count == 1, (
            f"跨数据源的一轮把上一轮的状态覆盖了（turn_count={afterCross.turn_count}，应为 1）"
        )
        assert afterCross.last_question == _FIRST_QUESTION

        sameDatasource = await client.post(
            "/api/v1/chat/stream",
            json={"sessionId": "s1", "question": _FOLLOW_UP_QUESTION, "datasourceId": ds.id},
            headers=headers,
        )
        assert sameDatasource.status_code == 200, sameDatasource.text
        assert _hasDone(sameDatasource.text)
        # 对照组：同数据源第二轮**必须**读到 state（否则上一条断言没有鉴别力）
        assert _intentsOf(sameDatasource.text) == ["clarify", "follow_up"], (
            "对照组失效：同数据源的第二轮也没读到 state，"
            f"实际 meta 序列 {_intentsOf(sameDatasource.text)}"
        )
        afterSame = await _onlyStateRow(dbSession)
        assert afterSame.datasource_id == ds.id
        assert afterSame.turn_count == 2, "同数据源第二轮应续在同一行上"
        assert afterSame.user_id == user.id

    async def test_legacy_null_scope_row_coexists_without_crashing(
        self, client, dbSession, monkeypatch
    ) -> None:
        """③ scoped 行与 NULL 行并存时，读取确定性地选一行而不抛异常。

        Postgres 的 UNIQUE 约束把 NULL 视作互不相等，所以 `(NULL,NULL,s1)` 与
        `(alice,ds,s1)` 可以合法并存 —— 这是 P0-3 上线前的历史遗留形态。裸
        `scalar_one_or_none()` 命中两行即 `MultipleResultsFound` → 500。
        """
        config, ds = await _seed(dbSession)
        user = await _seedUser(dbSession, "alice")

        dbSession.add_all(
            [
                SessionQueryState(
                    session_id="legacy-sid",
                    last_question="历史无 scope 的那一轮",
                    turn_count=7,
                    user_id=None,
                    datasource_id=None,
                ),
                SessionQueryState(
                    session_id="legacy-sid",
                    last_question="带 scope 的那一轮",
                    turn_count=1,
                    user_id=user.id,
                    datasource_id=ds.id,
                ),
                # 同一用户、半 scope 的历史行：让「只给 userId」的读取真的有多候选，
                # 否则 `_selectStateRow` 的去重职责在本用例里没有鉴别力（见下方 partial）。
                SessionQueryState(
                    session_id="legacy-sid",
                    last_question="同用户半 scope 的那一轮",
                    turn_count=3,
                    user_id=user.id,
                    datasource_id=None,
                ),
            ]
        )
        await dbSession.commit()

        from app.services.chat_context import ContextMixin

        class _Svc(ContextMixin):
            pass

        service = _Svc()

        # 无 scope（匿名）读取：一行都取不回 —— 不抛 MultipleResultsFound，且不得
        # 把**别人的 real 行**当成自己的上一轮。
        #
        # ⚠️ 2026-10-22 读侧收紧后匿名读是 `user_id IS NULL`，与写侧对称。原断言
        # `unscoped.user_id == user.id` 钉的正是本 PR 要堵的洞：收紧前读侧完全不过滤
        # user_id，匿名读会把 alice 的 last_question / last_plan / inheritance_snapshot
        # 当成自己的追问锚点。
        #
        # 为什么这里连**历史 NULL 桶**也读不到（不是 bug，是两道过滤叠加的结果）：
        # `_loadQueryState` 的 SQL 恒带 M4 的 `user_id != -1`，而 SQL 三值逻辑下
        # `NULL != -1` 求值为 NULL 而非 TRUE，NULL 行一律被 WHERE 排除。所以
        # `user_id IS NULL` ∧ `user_id != -1` 的候选集恒为空 —— 收紧没有让任何一行
        # 从「读得到」变成「读不到」。
        unscoped = await service._loadQueryState(dbSession, "legacy-sid")
        assert unscoped is None, (
            f"匿名读取回了状态行（user_id={unscoped.user_id!r}，"
            f"last_question={unscoped.last_question!r}）—— 两行并存时既可能抛 "
            "MultipleResultsFound，更可能把别人的 real 行泄露给匿名请求"
        )

        # 反向守卫：同一批数据以真实身份读**必须**读得到 alice 的行 ——
        # 证明上面那个 None 来自匿名守卫本身，不是「数据压根没写进去」。
        scoped = await service._loadQueryState(
            dbSession, "legacy-sid", userId=user.id, datasourceId=ds.id
        )
        assert scoped is not None, "带全 scope 读取取不到自己那行"
        assert scoped.last_question == "带 scope 的那一轮"

        # 部分 scope（只给 userId）：候选集真的有 3 行里的 2 行，必须确定性地选出一行
        # 而不是抛 MultipleResultsFound —— 这才是本用例 docstring 的职责。
        partial = await service._loadQueryState(
            dbSession, "legacy-sid", userId=user.id
        )
        assert partial is not None, "只给 userId 的读取取不到任何行"
        assert partial.last_question == "带 scope 的那一轮", (
            "候选集含 alice 的全 scope 行与半 scope 行，应按 scope 完整度取全 scope 那行；"
            f"实际取回 {partial.last_question!r}"
        )

        # 同 session 下的真实请求也不能被这行遗留数据打挂
        _installStreamFakes(monkeypatch, config)
        resp = await client.post(
            "/api/v1/chat/stream",
            json={"sessionId": "legacy-sid", "question": _FIRST_QUESTION, "datasourceId": ds.id},
            headers={"X-User-Id": "alice"},
        )
        assert resp.status_code == 200, resp.text
        assert _hasDone(resp.text), (
            f"遗留 NULL 行导致请求失败，事件：{[e for e, _ in _parseFrames(resp.text)]}"
        )