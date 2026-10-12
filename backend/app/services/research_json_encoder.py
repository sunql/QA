"""研究 payload 的 JSON 序列化口径（Task 3）：驱动标量 → JSON 安全标量。

问题：SSE 帧由 ``api/v1/research.py::_sseFrame`` 渲染，而数据来自**业务库驱动**
（``research_sql_runner.executeReadonlySql`` → ``adapter.execute_read_only``）。驱动
返回的行值不是 JSON 原生类型 —— ``Decimal``（numeric）、``date``/``datetime``
（date/timestamp）、``uuid.UUID``（uuid）。``json.dumps`` 对它们一律 ``TypeError``，
而 ``_sseFrame`` 的异常会炸掉**整帧**（不只是那一个字段）：用户看到的是通道静默，
不是「某列没显示」。batching 点在 ``research_agent_execution.py`` 的
``EVENT_STEP_DATA``（``{"index", "data": rows, "rowCount"}``）。

口径（controller 裁定 D7）：
- ``Decimal`` → ``str``，**不是 ``float``**：本项目已有「Decimal/float 阈值边界」
  缺陷记录，线上丢精度不可接受；字符串是唯一无损的 JSON 载体，前端按需解析。
- ``date`` / ``datetime`` → ``isoformat()``：JSON 无时间类型，ISO 8601 是通用交换格式。
- ``uuid.UUID`` → ``str``。
- **未知类型抛 ``TypeError`` 且点名类型**，不退回 ``str()``/``repr()``：静默字符串化
  会把「契约破坏」（有人往 payload 里塞了一个新类型）伪装成正常渲染，问题只会更晚、
  更贵地暴露。显式失败是设计意图，不是遗漏。

**刻意不做** ``{"__type__": ...}`` tag 包装：``__type__`` 在前后端均无消费者，事件总线
传的是**进程内 Python 对象**（``research_event_bus.py:53``），``_sseFrame`` 是唯一产出
SSE JSON 的地方 ⇒ 不存在需要「类型还原」的往返；而消费者是浏览器 ``JSON.parse``，
tag 对象会直接渲染成坏表格单元格。
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any


def _wireScalar(value: Any) -> str:
    """``json.dumps(default=...)`` 的兜底：把驱动标量映射为 JSON 安全字符串。

    只在该值无法被 ``json.dumps`` 原生序列化时被调用（JSON 原生类型走不到这里），
    容器（dict / list）由 ``json.dumps`` 自行递归 ⇒ 无需手工遍历。
    """
    # `datetime` 必须是第一个分支：它是 `date` 的子类，反过来会丢掉时分秒。
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    raise TypeError(
        f"research_json 不支持的类型: {type(value).__module__}.{type(value).__name__}"
        "；请把它映射为 JSON 安全标量，或显式扩展 research_json_encoder 的白名单"
    )


def research_json(value: Any) -> str:
    """把 payload 序列化为 SSE 帧用的 JSON 文本（``ensure_ascii=False`` 保留中文）。

    对 JSON 安全 payload，输出与 ``json.dumps(value, ensure_ascii=False)`` **逐字节相同**
    （``default`` 不会被调用），因此对既有帧是 drop-in 替换。
    """
    return json.dumps(value, ensure_ascii=False, default=_wireScalar)
