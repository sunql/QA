"""推理模型思维链（<think>…</think>）剥离 + Think_Hide 系统参数读取。

背景：MiniMax-M3 等推理模型把思维链以 ``<think>…</think>`` 内联在答案正文里，
直接展示给用户。系统参数 ``Think_Hide``（system_config，1=隐藏 / 0=显示，
迁移 0108 补种默认 '0'）控制是否剥离。

读取口径：无缓存、每次现读（对齐 ``_isL4AgentLoopEnabled`` / ``_readBoolConfig``
的既有约定）；缺行/空值/非法值/异常一律按「不隐藏」安全降级。
"""

from __future__ import annotations

import logging
import re

from sqlalchemy import text

logger = logging.getLogger(__name__)

THINK_HIDE_KEY = "Think_Hide"

# 字面量口径与 nl2sql_service._readBoolConfig 保持一致（大小写不敏感）
_FALSY_CONFIG_VALUES: frozenset[str] = frozenset({"0", "false", "off", "no"})
_TRUTHY_CONFIG_VALUES: frozenset[str] = frozenset({"1", "true", "on", "yes"})

_OPEN_TAG = "<think>"
_CLOSE_TAG = "</think>"
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think\s*>", re.IGNORECASE | re.DOTALL)
_UNCLOSED_THINK_RE = re.compile(r"<think>.*\Z", re.IGNORECASE | re.DOTALL)


def stripThinkBlocks(textValue: str | None) -> str:
    """全文剥离：含 ``<think>`` 块则去掉；未闭合视为仍在思维链内整体去除。

    不含标签时字节级透传（零干扰）。剥除发生在开头时收敛前导空白。
    """
    if not textValue or _OPEN_TAG not in textValue.lower():
        return textValue or ""
    stripped = _THINK_BLOCK_RE.sub("", textValue)
    unclosed = _UNCLOSED_THINK_RE.search(stripped)
    if unclosed:
        stripped = stripped[: unclosed.start()]
    return stripped.lstrip()


class ThinkStreamFilter:
    """流式增量剥离器：逐字符状态机（LLM 流速率下性能无虞）。

    标签可能被切成多片（``<thi`` + ``nk>``），因此 NORMAL 态对 ``<`` 起始的
    候选缓冲（不成 ``<think>`` 前缀立即按原文吐出）；IN_THINK 态同理缓冲
    ``</think>`` 尾部候选，其余丢弃。``feed`` 只返回可安全下发的文本；
    ``flush`` 在 isDone 时调用——NORMAL 吐出 pending 缓冲，IN_THINK 返回
    空串（未闭合 = 思维链未结束 = 隐藏）。
    """

    def __init__(self) -> None:
        self._inThink = False
        self._pending = ""
        self._emittedAny = False
        self._stripped = False  # 剥过 think 块后才允许吞后续前导空白（与全文 lstrip 对齐）

    def feed(self, chunk: str) -> str:
        out: list[str] = []
        for ch in chunk:
            if self._inThink:
                self._pending += ch
                self._pending = self._settlePending(out, emit=False)
                if not self._inThink:
                    pass
            elif self._pending:
                self._pending += ch
                self._pending = self._settlePending(out, emit=True)
            elif ch == "<":
                self._pending = ch
            else:
                self._emit(out, ch)
        return "".join(out)

    def flush(self) -> str:
        """流结束：NORMAL 态吐出缓冲的候选（可能是被截断的普通文本）。"""
        if self._inThink:
            return ""
        pending, self._pending = self._pending, ""
        return pending

    # ------------------------------------------------------------------
    def _settlePending(self, out: list[str], *, emit: bool) -> str:
        """检查 pending 是否构成/仍可能是目标标签，返回应保留的缓冲。"""
        target = _CLOSE_TAG if self._inThink else _OPEN_TAG
        if self._pending == target:
            if self._inThink:
                self._inThink = False
            else:
                self._inThink = True
                self._stripped = True
            return ""
        if target.startswith(self._pending):
            return self._pending  # 仍可能是标签，继续缓冲
        # 候选断裂：最后一个 '<' 起的尾巴作为新候选交回，其余按普通内容处理
        # （下发或丢弃）。候选逐字符判别、断裂即清空，故 lastLt>0 只会来自
        # NORMAL 态的「<<」连写；lastLt<=0（无 '<' 或在首位）整体处理。
        lastLt = self._pending.rfind("<")
        if lastLt <= 0:
            if emit:
                self._emit(out, self._pending)
            return ""
        if emit:
            self._emit(out, self._pending[:lastLt])
        return self._pending[lastLt:]

    def _emit(self, out: list[str], value: str) -> None:
        if not value:
            return
        if self._stripped and not self._emittedAny:
            value = value.lstrip()
            if not value:
                return
        self._emittedAny = True
        out.append(value)


async def isThinkHideEnabled(session) -> bool:
    """读 Think_Hide；缺行/空/非法/异常一律 False（不隐藏，安全降级）。"""
    try:
        row = await session.execute(
            text("SELECT value FROM system_config WHERE key = :key").bindparams(
                key=THINK_HIDE_KEY
            )
        )
        raw = row.scalar_one_or_none()
    except Exception:
        logger.warning("读取 %s 失败，按不隐藏处理", THINK_HIDE_KEY, exc_info=True)
        return False
    if raw is None or str(raw).strip() == "":
        return False
    token = str(raw).strip().lower()
    if token in _FALSY_CONFIG_VALUES:
        return False
    if token in _TRUTHY_CONFIG_VALUES:
        return True
    try:
        return int(token) > 0
    except (TypeError, ValueError):
        logger.warning("%s 值非法 %r，按不隐藏处理", THINK_HIDE_KEY, raw)
        return False


async def applyThinkPolicy(session, textValue: str | None) -> str:
    """非流式组合入口：Think_Hide=1 时剥离思维链，否则原样透传。"""
    if not textValue or _OPEN_TAG not in textValue.lower():
        return textValue or ""
    if not await isThinkHideEnabled(session):
        return textValue
    return stripThinkBlocks(textValue)
