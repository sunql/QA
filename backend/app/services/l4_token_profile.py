"""L4 Agent Loop 逐轮 token 构成探针（纯观测，绝不改变循环行为）。

目的：任务 3 第 ① 步度量取证——把一次 L4 run 的 prompt token 拆解到
system / tool schemas / 用户问题 / assistant 历史 / 各工具结果，并记录
cached_tokens，为后续优化（工具结果瘦身 / 模型分层 / prompt cache）提供数据。

口径约定：
- 组件 token 一律是 **tiktoken 估值**，每轮用 provider 真实 prompt_tokens 标定
  （scale = P_i / Σest）；报告分母永远以 provider 实测为准。
- cachedTokens: None = 字段缺失/不支持，0 = 字段存在但未命中——二者不混用。
- 所有日志单行 JSON + 固定 TAG，供离线脚本解析；解析失败返 None 不抛。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

TOKEN_PROFILE_TAG = "L4_TOKEN_PROFILE"
TOOL_RESULT_TAG = "L4_TOOL_RESULT"

# OpenAI 协议每条消息的固定结构开销（role/name 等字段的通行经验值）
_MESSAGE_TOKEN_OVERHEAD = 4

# wrapUntrusted 的 HTML 转义实体（prompt_sanitizer._HTML5_ESCAPE 同源）；
# 转义是单遍处理无嵌套，按「实体长度-1」× 出现次数即净增字符
_ESCAPE_ENTITIES = ("&quot;", "&#39;", "&amp;", "&lt;", "&gt;")


@dataclass(frozen=True)
class ComponentStats:
    """单个 prompt 组件的字符数与 token 估值。"""

    name: str
    chars: int
    estimatedTokens: int


@dataclass(frozen=True)
class ToolResultStats:
    """按工具名聚合的工具结果统计（转义净增字符单独量化）。"""

    name: str
    count: int
    chars: int
    escapeChars: int
    estimatedTokens: int


@dataclass(frozen=True)
class TokenProfile:
    """单轮 LLM 调用的 token 构成画像。"""

    runId: str | None
    sessionId: str | None
    iteration: int
    modelName: str
    messagesCount: int
    usagePresent: bool
    providerPromptTokens: int | None
    providerCompletionTokens: int | None
    cachedTokens: int | None
    cacheHitRate: float | None
    components: tuple[ComponentStats, ...]
    toolResults: tuple[ToolResultStats, ...]
    estimatedTotalTokens: int
    scale: float | None

    def asDict(self) -> dict:
        return {
            "event": "iteration",
            "runId": self.runId,
            "sessionId": self.sessionId,
            "iteration": self.iteration,
            "modelName": self.modelName,
            "messagesCount": self.messagesCount,
            "usagePresent": self.usagePresent,
            "providerPromptTokens": self.providerPromptTokens,
            "providerCompletionTokens": self.providerCompletionTokens,
            "cachedTokens": self.cachedTokens,
            "cacheHitRate": _round4(self.cacheHitRate),
            "estimatedTotalTokens": self.estimatedTotalTokens,
            "scale": _round4(self.scale),
            "components": [
                {"name": c.name, "chars": c.chars, "estimatedTokens": c.estimatedTokens}
                for c in self.components
            ],
            "toolResults": [
                {
                    "name": t.name,
                    "count": t.count,
                    "chars": t.chars,
                    "escapeChars": t.escapeChars,
                    "estimatedTokens": t.estimatedTokens,
                }
                for t in self.toolResults
            ],
        }


def _round4(value: float | None) -> float | None:
    return round(value, 4) if value is not None else None


def _getCounter():
    """tiktoken 计数器（deepseek→cl100k_base）；模块级函数便于测试替换。"""
    from app.infrastructure.token_counter.tiktoken_counter import TiktokenCounter

    return TiktokenCounter()


def _escapeOverhead(text: str) -> int:
    """HTML 实体转义的净增字符数。"""
    return sum((len(entity) - 1) * text.count(entity) for entity in _ESCAPE_ENTITIES)


def _toolCallsText(tool_calls) -> str:
    """assistant.tool_calls 按 openai_client 实际发送的 OpenAI 形状序列化。"""
    normalized = []
    for tc in tool_calls or ():
        if isinstance(tc, dict) and "function" in tc:
            normalized.append(tc)
        else:
            normalized.append(
                {
                    "id": tc.get("id"),
                    "type": "function",
                    "function": {
                        "name": tc.get("name"),
                        "arguments": json.dumps(tc.get("args", {}), ensure_ascii=False, default=str),
                    },
                }
            )
    return json.dumps(normalized, ensure_ascii=False, default=str)


def _collectMessages(messages):
    """单遍扫描：按组件桶收集字符与文本；工具结果按 name 聚合。

    返回 (buckets, bucketOrder, tools, toolOrder)。
    """
    buckets: dict[str, dict] = {}
    bucketOrder: list[str] = []
    tools: dict[str, dict] = {}
    toolOrder: list[str] = []

    def _bucket(key: str) -> dict:
        if key not in buckets:
            buckets[key] = {"chars": 0, "texts": []}
            bucketOrder.append(key)
        return buckets[key]

    for m in messages:
        role = m.role
        text = m.content or ""
        if role == "assistant" and getattr(m, "tool_calls", None):
            text = text + _toolCallsText(m.tool_calls)
        if role == "tool":
            name = getattr(m, "name", None) or "unknown"
            if name not in tools:
                tools[name] = {"count": 0, "chars": 0, "escapeChars": 0, "texts": []}
                toolOrder.append(name)
            entry = tools[name]
            entry["count"] += 1
            entry["chars"] += len(text)
            entry["escapeChars"] += _escapeOverhead(text)
            entry["texts"].append(text)
            bucket = _bucket(f"tool:{name}")
            bucket["chars"] += len(text)
            bucket["texts"].append(text)
        else:
            key = role if role in ("system", "user", "assistant") else "user"
            bucket = _bucket(key)
            bucket["chars"] += len(text)
            bucket["texts"].append(text)
    return buckets, bucketOrder, tools, toolOrder


def summarizeRequest(messages, toolSchemas) -> dict:
    """纯字符聚合（原始遥测，不做 token 估算）。"""
    buckets, _, tools, _ = _collectMessages(messages)
    toolChars = sum(b["chars"] for key, b in buckets.items() if key.startswith("tool:"))
    chars = {
        "system": buckets.get("system", {}).get("chars", 0),
        "user": buckets.get("user", {}).get("chars", 0),
        "assistant": buckets.get("assistant", {}).get("chars", 0),
        "tool": toolChars,
    }
    toolSchemaChars = (
        len(json.dumps(list(toolSchemas), ensure_ascii=False, default=str)) if toolSchemas else 0
    )
    return {
        "message_count": len(messages),
        "chars": chars,
        "tools": tools,
        "tool_schema_chars": toolSchemaChars,
        "total_chars": sum(chars.values()) + toolSchemaChars,
    }


def estimateRequestTokens(messages, toolSchemas, modelName) -> tuple[ComponentStats, ...]:
    """按组件做 tiktoken 估值；toolSchemas 组件在最前，其余按首次出现顺序。"""
    counter = _getCounter()
    buckets, bucketOrder, _, _ = _collectMessages(messages)
    components: list[ComponentStats] = []
    schemaText = json.dumps(list(toolSchemas), ensure_ascii=False, default=str) if toolSchemas else ""
    if schemaText:
        components.append(
            ComponentStats("toolSchemas", len(schemaText), counter.countTokens(modelName, schemaText))
        )
    for key in bucketOrder:
        bucket = buckets[key]
        tokens = sum(counter.countTokens(modelName, t) + _MESSAGE_TOKEN_OVERHEAD for t in bucket["texts"])
        components.append(ComponentStats(key, bucket["chars"], tokens))
    return tuple(components)


def buildIterationProfile(
    *,
    runId: str | None,
    sessionId: str | None,
    iteration: int,
    modelName: str,
    messages,
    toolSchemas,
    usage: dict | None,
    cachedTokens: int | None = None,
) -> TokenProfile:
    """构建单轮画像。usage 缺失时 provider 字段保持 None（不伪造 0）。"""
    components = estimateRequestTokens(messages, toolSchemas, modelName)
    estimatedTotal = sum(c.estimatedTokens for c in components)
    _, _, tools, toolOrder = _collectMessages(messages)
    counter = _getCounter()
    toolResults = tuple(
        ToolResultStats(
            name=name,
            count=tools[name]["count"],
            chars=tools[name]["chars"],
            escapeChars=tools[name]["escapeChars"],
            estimatedTokens=sum(
                counter.countTokens(modelName, t) + _MESSAGE_TOKEN_OVERHEAD
                for t in tools[name]["texts"]
            ),
        )
        for name in toolOrder
    )

    promptTokens = None
    completionTokens = None
    usagePresent = False
    if usage:
        rawPrompt = usage.get("prompt_tokens")
        rawCompletion = usage.get("completion_tokens")
        if isinstance(rawPrompt, int):
            promptTokens = rawPrompt
            usagePresent = True
        if isinstance(rawCompletion, int):
            completionTokens = rawCompletion
            usagePresent = True

    scale = (promptTokens / estimatedTotal) if promptTokens is not None and estimatedTotal > 0 else None
    cacheHitRate = (cachedTokens / promptTokens) if (cachedTokens is not None and promptTokens) else None

    return TokenProfile(
        runId=runId,
        sessionId=sessionId,
        iteration=iteration,
        modelName=modelName,
        messagesCount=len(messages),
        usagePresent=usagePresent,
        providerPromptTokens=promptTokens,
        providerCompletionTokens=completionTokens,
        cachedTokens=cachedTokens,
        cacheHitRate=cacheHitRate,
        components=components,
        toolResults=toolResults,
        estimatedTotalTokens=estimatedTotal,
        scale=scale,
    )


def buildLoopEndProfile(
    *,
    runId: str | None,
    sessionId: str | None,
    modelName: str,
    iterations: int,
    terminatedReason: str,
    promptTokens: int,
    completionTokens: int,
    cachedTokens: int | None,
) -> dict:
    """run 结束汇总行：丢轮时用于对照台账校验完整性。"""
    return {
        "event": "loop_end",
        "runId": runId,
        "sessionId": sessionId,
        "modelName": modelName,
        "iterations": iterations,
        "terminatedReason": terminatedReason,
        "promptTokens": promptTokens,
        "completionTokens": completionTokens,
        "cachedTokens": cachedTokens,
    }


def buildToolResultProfile(
    *,
    runId: str | None,
    sessionId: str | None,
    iteration: int,
    toolCallId: str | None,
    toolName: str,
    rawContent: str,
    sentContent: str,
    rowCount: int | None = None,
) -> dict:
    """单条工具结果的 raw vs sent 统计（wrapUntrusted 转义放大 + 行数）。"""
    rawChars = len(rawContent or "")
    sentChars = len(sentContent or "")
    escapeExpansion = (sentChars / rawChars) if rawChars else None
    return {
        "event": "tool_result",
        "runId": runId,
        "sessionId": sessionId,
        "iteration": iteration,
        "toolCallId": toolCallId,
        "toolName": toolName,
        "rawChars": rawChars,
        "sentChars": sentChars,
        "escapeExpansion": _round4(escapeExpansion),
        "rowCount": rowCount,
    }


def _dumpJson(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def logIterationProfile(profile: TokenProfile) -> None:
    if not logger.isEnabledFor(logging.INFO):
        return
    logger.info("%s %s", TOKEN_PROFILE_TAG, _dumpJson(profile.asDict()))


def logLoopEndProfile(record: dict) -> None:
    if not logger.isEnabledFor(logging.INFO):
        return
    logger.info("%s %s", TOKEN_PROFILE_TAG, _dumpJson(record))


def logToolResultProfile(record: dict) -> None:
    if not logger.isEnabledFor(logging.INFO):
        return
    logger.info("%s %s", TOOL_RESULT_TAG, _dumpJson(record))


def parseProfileLine(line: str) -> dict | None:
    """从日志行解析 profile JSON；支持裸行与 docker json-file 包装。

    任何解析失败返回 None（噪声行/截断行都不该让分析脚本崩）。
    """
    text = (line or "").strip()
    if not text:
        return None
    if text.startswith("{"):
        try:
            obj = json.loads(text)
        except ValueError:
            obj = None
        if isinstance(obj, dict) and isinstance(obj.get("log"), str):
            return parseProfileLine(obj["log"])
    for tag in (TOKEN_PROFILE_TAG, TOOL_RESULT_TAG):
        idx = text.find(tag)
        if idx < 0:
            continue
        brace = text.find("{", idx + len(tag))
        if brace < 0:
            return None
        try:
            return json.loads(text[brace:])
        except ValueError:
            return None
    return None
