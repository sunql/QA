"""Task 1.1b RED测试：验证 SessionQueryState.user_id 列的类型正确性。

根因：P0-3 把 `CurrentUser.userId`（str）传进 `SessionQueryState.user_id`（BigInteger），
PostgreSQL 无 varchar→bigint 隐式转换，导致非流式 `POST /api/v1/chat` 整条 500：
  operator does not exist: bigint = character varying
  [parameters: ('s1', 'anonymous', 99999)]

修复：11 处 `user.userId` → `user.dbUserId`（int | None）。

用例设计（项目强制 TDD）：
  ① 类型守卫（核心）：走 `POST /api/v1/chat` 完整 API 链路，断言 user_id 是 int | None，绝非 str
  ② dbUserId is None 分支：stub 用户查无此人 → user_id 为 NULL，SQL 不报错
  ③ 反向断言：构造 userId="not-an-int" / dbUserId=None，验证落库列是 NULL 而非字符串

fixture 复用：_seed / _installFakes / _PipelineLlm / _FakeAdapter / _RouterFor
（均来自 test_chat_api.py，不重复造）
"""

from __future__ import annotations

from sqlalchemy import select

from app.domain.models import SessionQueryState
from app.models.rbac import User
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


def _chat_payload(question: str, datasourceId: int = 1, sessionId: str = "s1") -> dict:
    return {"sessionId": sessionId, "question": question, "datasourceId": datasourceId}


async def _seedUser(dbSession, username: str) -> User:
    """写一个 enabled 用户，使 getCurrentUser 的 stub 路径能解析出 dbUserId。"""
    user = User(username=username, display_name=username, enabled=True)
    dbSession.add(user)
    await dbSession.commit()
    await dbSession.refresh(user)
    return user


async def _readStateUserId(dbSession, sessionId: str) -> int | None:
    """只 SELECT user_id 列，绕开 ORM 身份映射缓存。"""
    result = await dbSession.execute(
        select(SessionQueryState.user_id).where(SessionQueryState.session_id == sessionId)
    )
    row = result.scalar_one_or_none()
    return row  # int | None


class TestQueryStateUserIdType:
    """验证 user_id 列的类型正确性（int | None），而非 str。"""

    async def test_non_streaming_persists_user_id_as_int(
        self, client, dbSession, monkeypatch
    ) -> None:
        """① 核心：非流式成功查询后，user_id 列是 int，绝非 str。

        走 `POST /api/v1/chat` 完整 API 链路（真实 PG，真实 HTTP 请求，
        不直调 service），验证修复后 bigint = varchar 错误已消除。
        """
        config, ds = await _seed(dbSession)
        user = await _seedUser(dbSession, "alice")
        _installFakes(monkeypatch, config)

        resp = await client.post(
            "/api/v1/chat",
            json=_chat_payload("各供应商的收货数量汇总", ds.id, "s1"),
            headers={"X-User-Id": "alice"},
        )
        assert resp.status_code == 200, (
            f"非流式路径应返回 200，实际 {resp.status_code}；"
            f"body: {resp.text[:200]}"
        )

        user_id = await _readStateUserId(dbSession, "s1")
        assert user_id is not None, "user_id 应为已登录用户的整数 ID，而非 NULL"
        assert isinstance(user_id, int), (
            f"user_id 应为 int，实际 {type(user_id).__name__}（值：{user_id!r}）；"
            "修复未切到 dbUserId，仍在存 userId 字符串"
        )

    async def test_stub_user_no_match_persists_null(
        self, client, dbSession, monkeypatch
    ) -> None:
        """② stub 用户（X-User-Id 无法解析到 DB 用户）→ user_id 为 NULL，SQL 不报错。

        这是本仓的正常降级路径，不要当边界跳过。
        """
        config, ds = await _seed(dbSession)
        _installFakes(monkeypatch, config)

        # "nobody" 不是 DB 里的用户，getCurrentUser 的 stub 路径 dbUserId 为 None
        resp = await client.post(
            "/api/v1/chat",
            json=_chat_payload("各供应商的收货数量汇总", ds.id, "s2"),
            headers={"X-User-Id": "nobody"},
        )
        # 不应因 bigint = varchar 而 500
        assert resp.status_code == 200, (
            f"stub 用户路径应返回 200，实际 {resp.status_code}；"
            f"body: {resp.text[:200]}"
        )

        user_id = await _readStateUserId(dbSession, "s2")
        assert user_id is None, (
            f"无法解析的用户 X-User-Id 应导致 user_id=NULL，"
            f"实际 {user_id!r}（type={type(user_id).__name__}）"
        )

    async def test_dbuserid_none_does_not_store_string(
        self, client, dbSession, monkeypatch
    ) -> None:
        """③ 反向断言：dbUserId=None 时落库列是 NULL 而非字符串。

        证明修复真的切断了字符串来源，而不只是碰巧①②过了。
        场景：在 `_resolveDbUser` 查不到的情况下，userId 可能是任意字符串
        （如客户端伪造的 X-User-Id: "not-an-int"），dbUserId 必为 None。
        修复后应写 NULL，不应写字符串。
        """
        config, ds = await _seed(dbSession)
        _installFakes(monkeypatch, config)

        resp = await client.post(
            "/api/v1/chat",
            json=_chat_payload("各供应商的收货数量汇总", ds.id, "s3"),
            headers={"X-User-Id": "not-an-int"},
        )
        assert resp.status_code == 200, (
            f"dbUserId=None 路径应返回 200，实际 {resp.status_code}；"
            f"body: {resp.text[:200]}"
        )

        user_id = await _readStateUserId(dbSession, "s3")
        assert user_id is None, (
            f"dbUserId=None 时 user_id 应为 NULL，绝非 {user_id!r}；"
            "修复未切到 dbUserId，字符串仍写进去了"
        )
        # 确认数据库里这一列不是字符串 —— 如果是字符串，PostgreSQL 的 bigint 列
        # 不会隐式转换，这里验证的是"没有因为 varchar 进 bigint 列而抛异常"
        # 实际上在未修复的代码里这一步就会 500，不会走到这里
