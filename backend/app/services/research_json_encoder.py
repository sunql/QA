"""研究 payload 的 JSON 序列化口径（Task 3）：驱动标量 → JSON 安全标量。

问题：SSE 帧由 ``api/v1/research.py::_sseFrame`` 渲染，而数据来自**业务库驱动**
（``research_sql_runner.ResearchSqlRunner.executeReadonlySql`` →
``adapter.execute_read_only``）。驱动返回的行值不是 JSON 原生类型 —— ``Decimal``
（numeric）、``date``/``datetime``（date/timestamp）、``uuid.UUID``（uuid）。
``json.dumps`` 对它们一律 ``TypeError``，而 ``_sseFrame`` 的异常会炸掉**整帧**
（不只是那一个字段）：用户看到的是通道静默，不是「某列没显示」。batching 点在
``research_agent_execution.runStep`` 发出的 ``EVENT_STEP_DATA``
（``{"index", "data": rows, "rowCount"}``）。

口径（controller 裁定 D7）：
- ``Decimal`` → ``str``，**不是 ``float``**：本项目已有「Decimal/float 阈值边界」
  缺陷记录，线上丢精度不可接受；字符串是唯一无损的 JSON 载体，前端按需解析。
- ``date`` / ``datetime`` → ``isoformat()``：JSON 无时间类型，ISO 8601 是通用交换格式。
- ``uuid.UUID`` → ``str``。
- **未知类型抛 ``TypeError`` 且点名类型**，不退回 ``str()``/``repr()``：静默字符串化
  会把「契约破坏」（有人往 payload 里塞了一个新类型）伪装成正常渲染，问题只会更晚、
  更贵地暴露。显式失败是设计意图，不是遗漏。
- **非有限 float（NaN/Inf）抛 ``ValueError``**（``allow_nan=False``）：``float`` 走 json
  **原生**编码器（``default`` 不触发），默认会写出裸 ``NaN``/``Infinity`` —— 那不是合法
  JSON（RFC 8259 无此字面量），浏览器 ``JSON.parse`` 直接抛 ⇒ 整帧丢失，正是本模块要
  消灭的失败类。可达：PG ``double precision`` 列能存 ``'NaN'``，asyncpg 原样回
  ``float('nan')``。

**非有限 ``Decimal``（``NaN``/``Infinity``）与 float 刻意不同口径：接受，走 ``str``。**
理由是不对称的契约而非口味：float 在线上是 JSON **数字字面量**，而 JSON 数字没有
NaN/Inf 的表示 ⇒ 只能显式失败；``Decimal`` 在本模块的线上形态**本来就是字符串**
（精度原因，见上），``"NaN"`` 是它忠实且**可解析**的表示 —— 浏览器拿到明确的
``"NaN"`` 文本，而不是一个坏帧。另外 ``numeric`` 列合法地能存 ``'NaN'``，把它升级成
硬失败会让**合法数据状态**拉黑整条通道。故 ``Decimal('NaN')`` 的输出就是 ``"NaN"``，
**这是有意的**，不是漏网。

**刻意不做** ``{"__type__": ...}`` tag 包装：``__type__`` 在前后端均无消费者，事件总线
（``research_event_bus.ResearchEventBus.publish``）投递的是**进程内 Python 对象**，
不经任何序列化，``_sseFrame`` 是唯一产出 SSE JSON 的地方 ⇒ 不存在需要「类型还原」的
往返；而消费者是浏览器 ``JSON.parse``，tag 对象会直接渲染成坏表格单元格。
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
    （``default`` 与 ``allow_nan`` 都不会改变有限值），因此对既有帧是 drop-in 替换。

    ``allow_nan=False`` 管住「``default`` 够不着」的那条路：非有限 ``float`` 是 json 的
    **原生**类型，只有它能在写出裸 ``NaN``/``Infinity``（非法 JSON）之前拦住（见模块
    docstring）。``Decimal('NaN')`` 不受影响 —— 它走 ``default``，成为可解析的字符串 ``"NaN"``。
    """
    return json.dumps(value, ensure_ascii=False, default=_wireScalar, allow_nan=False)
