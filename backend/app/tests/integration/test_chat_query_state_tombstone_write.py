"""P0-3 写侧口径：`_saveQueryState` 不得覆写 M4 墓碑行。

根因：`_saveQueryState` 的 docstring 自己写着契约——
「查 existing 的那一步与 `_loadQueryState` 共用 `_selectStateRow`，
『读』和『找要更新的那一行』必须是同一个选择口径」（chat_context.py:612）。
但两条路径的**过滤口径并不相同**：

- `_loadQueryState`（:579-583）先在 SQL 里排掉 `is_stub=True` 与 `user_id=-1`；
- `_saveQueryState`（:617-621）只按 session_id + 条件性的 user/datasource 过滤。

于是 `userId is None`（匿名/stub 写入）且 session_id 撞上遗留桶时，墓碑行
（`user_id=-1`）会进入候选集。`_stateRowRank` 又把 `-1 is not None` 记作
「scope 完整」，而墓碑因迁移顺序倒置（0129 的 is_stub backfill 跑在 0131 造出
-1 行之前）保持 `is_stub=False` —— 墓碑在排序里就是一条"real 且完整"的行，
existing 分支直接对它 `turn_count += 1` 并覆写 last_question。

读侧本来已经排除了这行，写侧却改了它：读到的锚点与被改的行不是同一行。

修法：`_saveQueryState` 的 existing 查询补 `user_id != -1`，与读侧对齐。
**刻意不排 `is_stub`**：匿名写会落 `is_stub=True`（:670），排掉它会让每次匿名
写都 INSERT 新行（Postgres 视 NULL 之间互不相等，唯一约束拦不住），同一 session
无界增长。由 ② 那条反向断言钉住。
"""

from __future__ import annotations

from sqlalchemy import select

from app.domain.models import SessionQueryState
from app.services.chat_context import ContextMixin

_DS_ID = 4242  # user_id/datasource_id 无 FK，用固定值即可


async def _rows(dbSession, sessionId: str) -> list[SessionQueryState]:
    """取该 session 下全部行（绕开 ORM 身份映射缓存，直接看落库结果）。"""
    result = await dbSession.execute(
        select(SessionQueryState).where(SessionQueryState.session_id == sessionId)
    )
    return list(result.scalars().all())


async def test_save_query_state_does_not_overwrite_tombstone(dbSession) -> None:
    """① 写入不得覆写 user_id=-1 墓碑行；排不到候选时应新插一行。"""
    tombstone = SessionQueryState(
        session_id="tomb-s1",
        user_id=-1,  # M4 墓碑值：P0-3 前的遗留桶，禁止读写
        datasource_id=_DS_ID,
        is_stub=False,  # 0131 造墓碑时 0129 的 backfill 已跑过 ⇒ 保持 false
        last_question="P0-3 之前的旧问题",
        turn_count=7,
    )
    dbSession.add(tombstone)
    await dbSession.commit()
    await dbSession.refresh(tombstone)

    mixin = ContextMixin()
    await mixin._saveQueryState(
        dbSession,
        "tomb-s1",
        question="本轮的新问题",
        plan=None,
        sql="SELECT 1",
        resultColumns=[],
        userId=None,  # 匿名写 ⇒ 不带 user_id 过滤，墓碑才会进候选集
        datasourceId=_DS_ID,
    )

    await dbSession.refresh(tombstone)
    assert tombstone.turn_count == 7, (
        f"墓碑行 turn_count 被改了：{tombstone.turn_count}；"
        "写侧选中并覆写了 user_id=-1 的墓碑行（读侧本就排除它）"
    )
    assert tombstone.last_question == "P0-3 之前的旧问题", (
        f"墓碑行 last_question 被覆写为 {tombstone.last_question!r}；"
        "写侧与读侧口径不一致，读到的锚点与被改的行不是同一行"
    )

    # 排除墓碑后应走 INSERT 分支，本轮状态落在一行新行上
    rows = await _rows(dbSession, "tomb-s1")
    fresh = [r for r in rows if r.user_id is None]
    assert len(fresh) == 1, f"应新插 1 行匿名 state，实际 {len(fresh)} 行：{rows!r}"
    assert fresh[0].last_question == "本轮的新问题"
    assert fresh[0].turn_count == 1


async def test_save_query_state_stub_writes_update_in_place(dbSession) -> None:
    """② 反向断言：匿名连写必须命中 existing 分支，不得每轮 INSERT 一行。

    挡住「顺手把 is_stub 也排掉」这类过宽修法——排掉 is_stub 后 anonymous 写
    永远找不到 existing，于是每次都 INSERT；而 Postgres 视 NULL 之间互不相等，
    `uq_session_query_state_scope` 拦不住，同一 session 的 stub 行会无界增长。
    """
    mixin = ContextMixin()
    for question in ("第一轮", "第二轮", "第三轮"):
        await mixin._saveQueryState(
            dbSession,
            "stub-inplace-s1",
            question=question,
            plan=None,
            sql="SELECT 1",
            resultColumns=[],
            userId=None,
            datasourceId=_DS_ID,
        )

    rows = await _rows(dbSession, "stub-inplace-s1")
    assert len(rows) == 1, (
        f"匿名连写 3 轮应始终是同一行，实际 {len(rows)} 行；"
        "existing 查询把 is_stub=True 也排掉了 ⇒ 每轮都 INSERT，stub 行无界增长"
    )
    assert rows[0].turn_count == 3, f"应累计到第 3 轮，实际 turn_count={rows[0].turn_count}"
    assert rows[0].last_question == "第三轮"
    assert rows[0].is_stub is True
