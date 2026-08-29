"""复合问题拆步：显式分步判定 + LLM 结构化拆步。

单步优先策略（配合 ChatService）：
- `is_explicit_multi_step()`：识别"明确要求分步执行"的信号（分步/逐步/拆步/第一步等），
  命中时 ChatService 直接走多步，跳过单步尝试。
- `plan()`：调用 LLM 结构化拆步。仅在两种场景由 ChatService 触发——① 明确要求分步；
  ② 单步执行失败后的回退。plan() 本身不再做关键词快路径（是否走单步由调用方决定）。

拆出 steps >= 2 时返回 MultiStepPlan；拆出单步或拆分失败返回 None（回退单步）。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from app.domain.multi_step_plan import MAX_MULTI_STEP, MultiStepPlan, StepPlan, _clip_text
from app.infrastructure.llm.base_client import BaseLlmClient, LlmMessage

logger = logging.getLogger(__name__)

# 明确要求分步执行的信号关键词：命中即直接走多步（跳过单步尝试）。
# 仅保留"拆分"语义的显式表述，避免把"对比/比较"这类可单条 SQL 完成的查询误判为多步。
_EXPLICIT_MULTI_STEP_KEYWORDS = ("分步", "逐步", "一步步", "拆步", "第一步")

# 顺序指令连词：与「先」同时出现才算显式分步信号（如「先查X，再查Y，对比趋势」）。
# 单独出现（"再查一次""随后排序"）不算，避免把单条 SQL 可完成的查询误判为多步。
_SEQUENTIAL_START = "先"
_SEQUENTIAL_CONJUNCTIONS = ("然后", "再", "接着", "随后", "接下来")

# 规则快路径：用户用「第X步」显式标号时直接切句子，跳过 LLM 拆步判定
# （2026-08-16 回归修复：LLM 对"对比+分析趋势"字样倾向返回 isMultiStep=false，
# 导致多步被静默吞掉）。匹配范围与 MultiStepPlan.is_single_step（≥2 非汇总步）一致。
_RULE_STEP_PATTERN = re.compile(r"第[〇一二三四五六七八九十百千万0-9]+步")
# 序数副词序列：用户用"首先…其次…然后…/再者…最后…"等连接词显式分步时直接切句子
# （2026-08-17 回归修复：原仅识别「第X步」标号，导致「首先统计A，其次统计B，然后看C」
# 这类典型多步被漏到 LLM 判定，而 LLM 倾向返回 isMultiStep=false，单步 SQL 仅覆盖
# 第一部分返回，后续子问题被静默吞掉——用户看到「只能确认 4 月份采购情况」）。
# 同时纳入"然后/接着/接下来"作为第 3+ 步分隔词（"首先A，其次B，然后C" 结构中的
# "然后" 与"首先/其次"并列充当分步词）。这些是 2 字定长词，不会与单字"先"/"再"混淆，
# 避免把"先查 X，再查 Y"误拆为多步（test_no_marker_returns_none 守约）。
_ORDINAL_STEP_PATTERN = re.compile(r"(?:首先|其次|再者|再次|最后|其一|其二|其三|其四|然后|接着|接下来)")
_RULE_DESCRIPTION_LIMIT = 20
_RULE_AGG_HINT = "请基于前序步骤结果汇总对比"
_RULE_STRIP_CHARS = "，,。、：； 　"  # 全角逗号/句号/顿号/冒号/分号 + 全角空格 + 半角空格

_STEP_PLANNER_SYSTEM_PROMPT = (
    "你是查询拆分器。判定用户问题是否需要拆成多个子查询。\n"
    "若需要，返回 JSON: {\"isMultiStep\": true, "
    "\"steps\": [{\"description\": \"...\", \"subQuestion\": \"...\"}], "
    "\"aggregationHint\": \"如何汇总\"}\n"
    "若不需要，返回 {\"isMultiStep\": false}。\n"
    "最多拆 4 个子步骤（汇总步骤不计入）。"
)


def _splitByRulePattern(
    pattern: re.Pattern[str], question: str
) -> MultiStepPlan | None:
    """按给定正则切分问题为多步计划；<2 个命中返回 None。

    共用于「第X步」与「序数副词」两类规则标号。
    """
    matches = list(pattern.finditer(question))
    if len(matches) < 2:
        return None

    parts: list[str] = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(question)
        chunk = question[start:end].strip(_RULE_STRIP_CHARS)
        if chunk:
            parts.append(chunk)
    if len(parts) < 2:
        return None

    data_steps = tuple(
        StepPlan(
            index=i,
            description=_clip_text(part, _RULE_DESCRIPTION_LIMIT),
            sub_question=part,
            aggregation_only=False,
        )
        for i, part in enumerate(parts)
    )
    agg_step = StepPlan(
        index=len(parts),
        description="汇总对比",
        sub_question=_RULE_AGG_HINT,
        aggregation_only=True,
    )
    return MultiStepPlan(
        steps=data_steps + (agg_step,),
        aggregation_hint=_RULE_AGG_HINT,
        original_question=question,
    )


@dataclass(frozen=True)
class StepPlanResult:
    """拆步判定结果：多步计划 + 本次 LLM 调用消耗（供计量）。

    plan 为 None 表示判定为单步或拆分失败（但 LLM 调用已发生，token 仍需计量）。
    """

    plan: MultiStepPlan | None
    prompt_tokens: int = 0
    completion_tokens: int = 0


class StepQueryPlanner:
    """判定并拆分复合问题为多步计划。"""

    def __init__(self) -> None:
        pass

    async def plan(
        self,
        question: str,
        classes: list[Any],
        client: BaseLlmClient,
        model_name: str,
    ) -> StepPlanResult:
        """调用 LLM 结构化拆步；拆出单步或拆分失败时 plan 为 None（token 仍随结果返回）。

        调用方（ChatService）保证仅在需要拆步时调用（明确分步或单步失败回退）。
        拆分是增强而非硬依赖：任何异常都降级返回 plan=None，绝不抛错。不改动入参。
        """
        llm_plan, pt, ct = await self._plan_by_llm(question, client, model_name)
        if llm_plan is None or llm_plan.is_single_step:
            return StepPlanResult(plan=None, prompt_tokens=pt, completion_tokens=ct)
        return StepPlanResult(plan=llm_plan, prompt_tokens=pt, completion_tokens=ct)

    async def plan_explicit(self, question: str) -> StepPlanResult:
        """规则快路径：仅在用户用「第X步」显式标号时命中，零 LLM 调用。

        调用方（ChatService）命中 is_explicit_multi_step 后先调本方法；返回 None
        再调 plan() 走 LLM。保留 plan() 始终调 LLM 的语义不变（守约 test_plan_always_calls_llm）。
        """
        rule_plan = self.rule_based_split(question)
        if rule_plan is not None:
            logger.info("拆步规则命中（第X步标号），跳过 LLM 判定: %s", question)
            return StepPlanResult(plan=rule_plan)
        return StepPlanResult(plan=None)

    @staticmethod
    def rule_based_split(question: str) -> MultiStepPlan | None:
        """规则拆分：用户用「第X步」或序数副词序列显式分步时直接切句子，零 LLM 调用。

        支持两类显式分步标号（命中任一即视为多步）：
        1. 「第X步」标号（X 为中文/阿拉伯数字，2026-08-16 引入）
        2. 序数副词序列（首先/其次/再者/最后/其一/其二/其三/其四，
           2026-08-17 引入）

        返回 None 表示无法规则拆分（仅命中"先...再..."等顺序指令但无显式标号），
        调用方此时回退到 LLM 拆步。匹配 ≥2 个标号才视为多步——与
        MultiStepPlan.is_single_step（≥2 个非汇总步骤）保持一致。
        """
        plan = _splitByRulePattern(_RULE_STEP_PATTERN, question)
        if plan is not None:
            return plan
        return _splitByRulePattern(_ORDINAL_STEP_PATTERN, question)

    @staticmethod
    def is_explicit_multi_step(question: str) -> bool:
        """识别"明确要求分步执行"的信号（零 LLM 成本）。

        命中显式拆分语义关键词（分步/逐步/拆步/第一步），或「先…然后/再/接着…」
        这类顺序指令时返回 True；单独的"对比/比较/各年"等可单条 SQL 完成的表述
        不算显式分步，交由单步优先策略处理（最终是否拆步仍由拆步 LLM 判定）。
        """
        q = question.strip()
        if any(kw in q for kw in _EXPLICIT_MULTI_STEP_KEYWORDS):
            return True
        return _SEQUENTIAL_START in q and any(c in q for c in _SEQUENTIAL_CONJUNCTIONS)

    async def _plan_by_llm(
        self,
        question: str,
        client: BaseLlmClient,
        model_name: str,
    ) -> tuple[MultiStepPlan | None, int, int]:
        """调用 LLM 拆步；返回 (计划, prompt_tokens, completion_tokens)。

        解析失败 / steps < 2 / 超过上限时计划为 None（token 仍返回以计量）。
        JSON 容错：捕获任何解析异常并记录 warning，绝不抛错。
        """
        try:
            resp = await client.complete(
                messages=[
                    LlmMessage(role="system", content=_STEP_PLANNER_SYSTEM_PROMPT),
                    LlmMessage(role="user", content=self._sanitize(question)),
                ],
                model=model_name,
            )
        except Exception:
            logger.warning("拆步 LLM 调用失败，回退单步: %s", question, exc_info=True)
            return None, 0, 0

        pt = resp.promptTokens
        ct = resp.completionTokens

        try:
            data = self._extract_json(resp.content)
        except Exception:
            logger.warning("拆步 LLM 回复解析失败，回退单步: %s", question, exc_info=True)
            return None, pt, ct

        if not data or not data.get("isMultiStep"):
            return None, pt, ct

        steps_raw = data.get("steps") or []
        if not isinstance(steps_raw, list) or len(steps_raw) < 2:
            return None, pt, ct

        steps = self._build_step_plans(steps_raw)
        if len(steps) < 2:
            return None, pt, ct

        # 注入自动汇总步骤（不计入 MAX_MULTI_STEP 上限检查，因为它是必然的最后一步）
        steps.append(
            StepPlan(
                index=len(steps),
                description="汇总对比",
                sub_question=data.get("aggregationHint") or "请基于前序步骤结果汇总",
                aggregation_only=True,
            )
        )

        # 硬上限保护（不含汇总步骤）
        non_agg_count = len(steps) - 1
        if non_agg_count > MAX_MULTI_STEP - 1:
            logger.warning(
                "拆步数量 %d 超过上限 %d，截断: %s",
                non_agg_count,
                MAX_MULTI_STEP - 1,
                question,
            )
            steps = steps[: MAX_MULTI_STEP - 1] + steps[-1:]

        return (
            MultiStepPlan(
                steps=tuple(steps),
                aggregation_hint=data.get("aggregationHint") or "请基于前序步骤结果汇总",
                original_question=question,
            ),
            pt,
            ct,
        )

    @staticmethod
    def _extract_json(text: str) -> dict | None:
        """从 LLM 回复中提取 dict，容忍 ```json 围栏与非 dict 输入。"""
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _build_step_plans(raw_steps: list[Any]) -> list[StepPlan]:
        """把 LLM 返回的 steps 列表解析为 StepPlan 列表；损坏条目跳过。"""
        out: list[StepPlan] = []
        for i, entry in enumerate(raw_steps):
            if not isinstance(entry, dict):
                continue
            desc = entry.get("description")
            sub = entry.get("subQuestion") or entry.get("sub_question")
            if not isinstance(desc, str) or not isinstance(sub, str):
                continue
            out.append(StepPlan(index=i, description=desc, sub_question=sub))
        return out

    @staticmethod
    def _sanitize(text: str) -> str:
        """对未受信用户输入做基本转义，防止 prompt 注入（参考 nl2sql _sanitizeContext）。"""
        return (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&#39;")
        )
