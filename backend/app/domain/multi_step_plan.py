"""L1 线性多步 NL2SQL 计划与执行结构（不可变 frozen dataclass）。

L1 限定：步骤按顺序线性执行，前序步骤的结果可注入后续步骤的 NL2SQL prompt。
不引入 DAG / 循环：MAX_MULTI_STEP=5 硬上限（含汇总步骤）。

主要概念：
- StepPlan：单个子步骤的计划（描述 + 子问题 + 是否为汇总步骤）
- StepResult：单个子步骤的执行结果（SQL / 数据 / 摘要 / 注入文本）
- MultiStepPlan：多步计划（StepQueryPlanner 产物）
- StepExecutionContext：多步执行的共享上下文（不可变，每次 with_step 返回新实例）
"""

from __future__ import annotations

import json as _json
import re as _re
from dataclasses import dataclass, field
from typing import Literal

MAX_MULTI_STEP = 5  # 硬上限：含汇总步骤最多 5 步（防无限循环）

# 2026-08-17 修复：Step N 引用 Step N-1 实体列表作为 WHERE IN 筛选条件
# 实体列表形态：每列一行 `列名: 值1, 值2, ...`，方便 LLM 直接生成 WHERE IN
_ENTITY_LIST_ITEM_LIMIT = 2000  # 实体列表模式每项字符上限（够 Top 30 完整 ID 列表）
_MAX_ENTITY_LIST_ROWS = 50      # 超过此行数强制走 AGGREGATE 模式（防 prompt 爆炸）
_DATA_SAMPLE_FOR_DETECT = 50    # 形态检测时取样行数

StepDataShape = Literal["ENTITY_LIST", "AGGREGATE"]


def _clip_text(text: str, limit: int) -> str:
    """按字符数截断文本，超限加省略号。"""
    return text if len(text) <= limit else text[:limit] + "..."


def _clip_json(data: list[dict], limit: int) -> str:
    """将 JSON 列表序列化为字符串后按字符数截断。空列表/解析失败返回 []。"""
    try:
        text = _json.dumps(data, ensure_ascii=False, default=str)
    except Exception:
        return "[]"
    return _clip_text(text, limit)


def _detect_step_data_shape(data: list[dict]) -> StepDataShape:
    """检测步骤数据的渲染形态。

    ENTITY_LIST 触发条件（同时满足）：
    1. data 非空
    2. ≤ _MAX_ENTITY_LIST_ROWS 行（防 prompt 爆炸）
    3. 至少 1 个字符串类型列（候选主键列；None 与数值不算）

    其他情况一律 AGGREGATE（保留 JSON 摘要）。单行全数值合计、空数据、
    >50 行都走 AGGREGATE。
    """
    if not data:
        return "AGGREGATE"
    if len(data) > _MAX_ENTITY_LIST_ROWS:
        return "AGGREGATE"
    sample = data[:_DATA_SAMPLE_FOR_DETECT]
    for row in sample:
        for v in row.values():
            if isinstance(v, str):
                return "ENTITY_LIST"
    return "AGGREGATE"


def _render_entity_list(data: list[dict], char_limit: int) -> str:
    """实体列表渲染：每列一行 `列名: 值1, 值2, ...` 形式。

    总字符数受 char_limit 限制；超限截断到 char_limit 加省略号。
    字符串值与数值都用 `, ` 分隔；空数据返回「（无数据）」占位。
    """
    if not data:
        return "（无数据）"
    columns = list(data[0].keys())
    lines: list[str] = []
    for col in columns:
        values = [str(row.get(col, "")) for row in data]
        lines.append(f"{col}: " + ", ".join(values))
    text = "\n    ".join(lines)
    return _clip_text(text, char_limit)


@dataclass(frozen=True)
class StepPlan:
    """单个子步骤的计划（不可变）。

    Attributes:
        index: 从 0 开始的步骤序号，与 StepResult.step_index 一一对应。
        description: 自然语言描述，如"2024 年销售额"。
        sub_question: 喂给 NL2SQL 的子问题，可注入前序步骤结果后发给 LLM。
        aggregation_only: True 表示该步是汇总步骤（仅产生最终回答，不执行 SQL）。
    """

    index: int
    description: str
    sub_question: str
    aggregation_only: bool = False


@dataclass(frozen=True)
class StepResult:
    """单个子步骤的执行结果（不可变）。

    Attributes:
        step_index: 对应 StepPlan.index。
        description: 同 StepPlan.description。
        sub_question: 同 StepPlan.sub_question。
        sql: 生成的 SQL（aggregation_only=True 时为 None）。
        data: SQL 执行结果行列表。
        summary: 该步的一句话小结，供后续步骤与汇总步骤引用。
        error: 步骤级错误描述（None 表示执行成功）。
    """

    step_index: int
    description: str
    sub_question: str
    sql: str | None = None
    data: list[dict] = field(default_factory=list)
    summary: str = ""
    error: str | None = None


@dataclass(frozen=True)
class MultiStepPlan:
    """L1 多步计划（StepQueryPlanner 产物）。

    Attributes:
        steps: 所有步骤元组（含末尾汇总步骤 StepPlan），按执行顺序排列。
        aggregation_hint: 汇总提示，如"对比 3 个年份的销售额，给出同比趋势结论"。
        original_question: 保留原始问题，供持久化 / 调试 / 汇总 LLM 使用。
    """

    steps: tuple[StepPlan, ...]
    aggregation_hint: str
    original_question: str

    @property
    def is_single_step(self) -> bool:
        """仅一个非汇总步骤视为单步，直接走原流水线。"""
        non_agg = [s for s in self.steps if not s.aggregation_only]
        return len(non_agg) <= 1

    @property
    def has_aggregation(self) -> bool:
        """是否存在汇总步骤。"""
        return any(s.aggregation_only for s in self.steps)

    @property
    def data_steps(self) -> list[StepPlan]:
        """非汇总的数据查询步骤列表（不含末尾汇总步骤）。"""
        return [s for s in self.steps if not s.aggregation_only]


@dataclass(frozen=True)
class StepExecutionContext:
    """多步执行的共享上下文（不可变，每次 with_step 返回新实例）。

    持有 completed_steps 映射（步骤序号 → StepResult），供后续步骤引用前序结果。

    Attributes:
        datasource_type: 数据源类型（mysql/postgresql/oracle），用于 prompt 构造。
        oracle_version: Oracle 版本号（仅 oracle 类型有值）。
        schema_prefix: 用户名/schema 前缀。
        context: 多轮对话上下文（与单轮 _PipelineContext.contextPrompt 同源）。
        completed_steps: 已完成步骤元组（按执行顺序追加）。
        injection_char_limit: 聚合数值形态每项字符上限（默认 600，2026-08-17 之前一直用此值）。
        injection_char_limit_entity: 实体列表形态每项字符上限（默认 2000，2026-08-17 修复
            Step N 引用 Step N-1 实体列表时新增；够 Top 30 完整 ID 列表）。
    """

    datasource_type: str
    oracle_version: str | None
    schema_prefix: str
    context: str
    completed_steps: tuple[StepResult, ...] = ()
    injection_char_limit: int = 600
    injection_char_limit_entity: int = _ENTITY_LIST_ITEM_LIMIT

    def inject_to_prompt(self, current_index: int) -> str:
        """把当前步骤之前的所有 StepResult 渲染为可注入 prompt 的文本片段。

        渲染策略（2026-08-17）：按数据形态自动分档——
        - 实体列表（≤50 行且含字符串列）：`[entity_list]` 标签，列分别列值，限额 2000 字符
        - 聚合数值（其他）：`[aggregate]` 标签，JSON 摘要，限额 600 字符

        标签用方括号而非尖括号：本产物会作为 priorState 进入 nl2sql 的
        _renderStatePart，后者对全文做 _sanitizeContext（< 转义为 &lt;）--尖括号
        标签会被转义成 &lt;entity_list&gt;，结构化标记被破坏（2026-08-17
        复测回归修复）。方括号不受转义影响，LLM 能看到完好的标签结构。

        头部文案从"仅作参考"改为"可作为后续步骤的筛选条件使用"，配合 NL2SQL prompt
        强指令（WHERE IN ...）让 Step N 正确引用 Step N-1 的主键列。

        格式示例（实体列表形态）：
        前序步骤结果（可作为后续步骤的筛选条件使用；详见 [entity_list] / [aggregate] 标签说明）：
        步骤 2：Top 10 物料占比
          子问题：统计3月份主要top10采购物料的占比
          SQL：SELECT MATERIAL_ID, SUM(QTY) ...
          摘要：Top 10 物料采购量占比 0.85
          [entity_list]
            MATERIAL_ID: M001, M002, ..., M010
            占比: 0.18, 0.15, ..., 0.02
          [/entity_list]

        当前步骤为 0 时或无已完成步骤时返回空串。
        所有未受信数据经 _sanitizeContext 转义（与单轮注入同口径）。
        """
        if current_index == 0 or not self.completed_steps:
            return ""

        # 延迟导入避免循环依赖（nl2sql_service 依赖 multi_step_plan）
        from app.services.nl2sql_service import _sanitizeContext

        prior = [r for r in self.completed_steps if r.step_index < current_index]
        if not prior:
            return ""

        lines = [
            "前序步骤结果（可作为后续步骤的筛选条件使用；"
            "详见 [entity_list] / [aggregate] 标签说明）："
        ]
        for r in prior:
            shape = _detect_step_data_shape(r.data)
            if shape == "ENTITY_LIST":
                per_item = self.injection_char_limit_entity
                data_snippet = _render_entity_list(r.data, per_item)
            else:
                per_item = self.injection_char_limit
                data_snippet = _clip_json(r.data, per_item)
            summary = _clip_text(r.summary or "（无摘要）", per_item)
            sql_snippet = _clip_text(r.sql or "", per_item)
            tag = shape.lower()
            lines.append(
                f"步骤 {r.step_index + 1}：{_sanitizeContext(r.description)}\n"
                f"  子问题：{_sanitizeContext(r.sub_question)}\n"
                f"  SQL：{sql_snippet}\n"
                f"  摘要：{summary}\n"
                f"  [{tag}]\n    {data_snippet}\n  [/{tag}]"
            )
        return "\n".join(lines)

    def with_step(self, result: StepResult) -> "StepExecutionContext":
        """返回包含新步骤的新上下文（不可变），原实例不受影响。"""
        return StepExecutionContext(
            datasource_type=self.datasource_type,
            oracle_version=self.oracle_version,
            schema_prefix=self.schema_prefix,
            context=self.context,
            completed_steps=self.completed_steps + (result,),
            injection_char_limit=self.injection_char_limit,
            injection_char_limit_entity=self.injection_char_limit_entity,
        )
