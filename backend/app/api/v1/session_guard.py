"""会话归属守卫（route 层共享）。

从 ``chat.py`` 的私有 ``_assertChatSessionOwnership``（v3.1 B6）提升而来：原先只有
``GET /chat/sessions/{id}/hypotheses`` 一个调用者，于是 ``/chat``、``/chat/stream``
（追问锚点的写入端）与 ``session.py`` 的三个会话端点全都没有归属校验 —— 任何登录用户
只要知道/猜到 sessionId，就能读别人的会话、删别人的会话（含成本台账）、并且**继承
别人会话的服务端追问锚点**。

事实源：``session_message.user_id``（存的是**用户名字符串**，由 chat_service /
chat_stream / rag_qa_service / wiki 侧写消息时打标），经 ``getSessionOwnerUserIds``
读取。**按 ``channel=None`` 取任意渠道的并集** —— 被守卫的会话端点服务 chat / doc_qa /
wiki_qa 三个渠道，只查 "chat" 会让另外两个渠道恒返空集、守卫静默失效（见该函数 docstring）。

语义（与既有 hypotheses 守卫完全一致，不改状态码、不改文案）：

- admin 放行 —— 排障需要跨用户视角；
- 有归属标记且不含当前用户 → **403**，detail 不回显归属者（防侧信道枚举）；
- 无归属标记（存量 NULL 行 / 全新会话）→ **fail-open**。

fail-open 的覆盖面是**有限**的，别把它当成「会话已经隔离了」：user_id 是 2026-09-30
才开始写的（chat 渠道 1032 行里 920 行是 NULL）。守卫保护的是此后所有新会话加上已打标
的老会话（**三个渠道都算**）；存量未打标会话仍对任何人开放。要真正闭环得先定回填口径
（属 prod 数据变更，需单独批准）。

仍未纳入归属的对称端点（见变更 summary §「待拍板」）：``GET /sessions/{id}/usage``、
``/usage/list``（成本台账）、``GET /sessions/chat-history``（会话枚举，仅按 channel 过滤）。
"""

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser
from app.services.evidence_query_service import getSessionOwnerUserIds
from app.services.messages_zh import MSG_SESSION_NOT_OWNED


async def assertSessionOwnership(
    session: AsyncSession,
    sessionId: str,
    user: CurrentUser,
) -> None:
    """会话不属于当前用户时抛 403；无归属标记时放行（fail-open）。

    不限渠道取归属者：调用方（``/messages``、``DELETE``、``export.pdf``、``/chat*``）
    都按 sessionId 操作，不区分渠道。写成只查 "chat" 会让 doc_qa / wiki_qa 会话的
    读与删完全绕过守卫。
    """
    if "admin" in (user.roles or []):
        return
    owners = await getSessionOwnerUserIds(session, sessionId, channel=None)
    if owners and user.userId not in owners:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            # 中性文案：本守卫同时服务 messages / delete / export / chat / hypotheses，
            # 沿用「无权访问该会话的分析假设」会在一半端点上描述错对象（且它已随守卫
            # 提升而失去「只属 hypotheses」的语境）。与 documents.py / wiki.py 的
            # 同义守卫共用一句，措辞刻意不区分「不存在」与「不属于你」。
            detail=MSG_SESSION_NOT_OWNED,
        )

