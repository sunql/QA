"""采购范围裁决目录（M1-1.6 / 准则 H + D1–D3 的**可执行 SSOT**）。

为什么要有这个模块
------------------
V0.3 §7.3 草案列了 14 张 ``procurement_*`` 表；准则 H（范围裁决：**不建重复语义的表**）
逐条裁决后：**10 张不建**（复用既有表，双写 = 双口径；计划文档准则 H 表的
workflow 三行展开为 3 张）、**3 张建**（其中 1 张改名）、
**2 张延后到 M6** 决策（D3）。原文是计划文档里的一张表（M0-M7 §准则 H / D1）——
文档挡不住「下一个人照草案把表建出来」：双写双口径正是裁决二要防的事。

⇒ 本模块把裁决**数据化**，三个守卫消费同一份事实（``test_procurement_scope_ruling.py``
+ ``test_workflow_write_gate.py`` 的 ORM 定义点唯一守卫）：

1. **FORBIDDEN**：这些表名不许出现在任何 ``__tablename__`` 与迁移 ``CREATE TABLE`` 里；
2. **APPROVED**：这三张表**必须真实存在**（对应迁移文件里真有 ``CREATE TABLE``）
   —— 守卫不许空集假绿；
3. **DEFERRED**：M6 前不许出现；M6 若裁决要建，**先改本目录再建表**。

血统：替代目标全是**既有表**（``semantic_release`` / ``workflow_*`` / ``business_command``
/ ``approval_task`` / ``workflow_outbox`` / ``audit_log`` / ``agent_orchestration_run``）；
``workflow_definition.domain_code`` 已预留 ``'procurement'``。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TableRuling:
    """一张 V0.3 草案表的裁决结果。

    - ``tableName``：V0.3 §7.3 草案里的表名；
    - ``replacement``：不建时复用的既有表（含承载迁移号），建/延后时说明承载处；
    - ``reason``：一句话裁决理由（评审/复核用）。
    """

    tableName: str
    replacement: str
    reason: str


# 不建（准则 H：与既有表语义重复 ⇒ 双写双口径）。**守卫红线**：
# 任何 ``__tablename__`` / 迁移 ``CREATE TABLE`` 出现这些名字 ⇒ 测试红。
FORBIDDEN_DUPLICATE_TABLES: tuple[TableRuling, ...] = (
    TableRuling(
        tableName="procurement_ontology_release",
        replacement="semantic_release（0142）",
        reason="本体发布已有语义发布主表；D1 确认复用",
    ),
    TableRuling(
        tableName="procurement_ontology_release_event",
        replacement="audit_log",
        reason="发布事件走审计日志；单独事件表 = 第二套事件口径",
    ),
    TableRuling(
        tableName="procurement_agent_run_context",
        replacement="agent_orchestration_run 三列（0145）",
        reason="运行上下文已由编排运行表三列承载",
    ),
    TableRuling(
        tableName="procurement_workflow_definition",
        replacement="workflow_definition（domain_code='procurement'）",
        reason="工作流定义按域区分即可；D1 确认复用",
    ),
    TableRuling(
        tableName="procurement_workflow_instance",
        replacement="workflow_instance",
        reason="工作流实例复用；D1 确认",
    ),
    TableRuling(
        tableName="procurement_workflow_task",
        replacement="workflow_step",
        reason="工作流步骤复用；D1 确认",
    ),
    TableRuling(
        tableName="procurement_business_command",
        replacement="business_command（0150）",
        reason="命令表已是通用形态；D1 确认",
    ),
    TableRuling(
        tableName="procurement_approval_record",
        replacement="approval_task（0151）",
        reason="审批记录复用审批任务表；D1 确认",
    ),
    TableRuling(
        tableName="procurement_outbox_event",
        replacement="workflow_outbox（0151，消费者待补）",
        reason="outbox 复用；缺消费者是既有已知欠账，不是新建表的理由",
    ),
    TableRuling(
        tableName="procurement_key_binding",
        replacement="procurement_source_binding.key_columns JSON + entity_mapping",
        reason="键绑定并入源绑定 JSON 列与既有映射表；独立表 = 冗余键口径",
    ),
)

FORBIDDEN_DUPLICATE_TABLE_NAMES: frozenset[str] = frozenset(
    ruling.tableName for ruling in FORBIDDEN_DUPLICATE_TABLES
)

# 建（D1/D2 已定）。**必须真实存在**：守卫据此核对迁移文件里真有 CREATE TABLE。
APPROVED_TABLES: tuple[TableRuling, ...] = (
    TableRuling(
        tableName="semantic_release_check",
        replacement="0155（改名自草案 procurement_ontology_release_check）",
        reason="发布校验落表；改名以并入语义发布家族",
    ),
    TableRuling(
        tableName="procurement_source_binding",
        replacement="0154",
        reason="采购源绑定（D2：两张表方案）",
    ),
    TableRuling(
        tableName="procurement_property_binding",
        replacement="0154",
        reason="属性级绑定独立建模；source_column 单字符串表达不了多源/表达式/单位",
    ),
    TableRuling(
        tableName="procurement_rfq_draft",
        replacement="0160",
        reason=(
            "询价草稿（M5-1，设计稿 §6.4 的 RFQ 半边）：草稿与业务流状态机分离——"
            "草稿是**候选对象**（DRAFT/VALIDATING/PENDING_APPROVAL…），审批通过也只到 "
            "READY_TO_SEND（D-4 DRAFT-only，不触发外部发送）⇒ 不与 workflow_instance "
            "重复语义（后者是通用流程实例，承载不了 RFQ 的 payload/幂等/行项）"
        ),
    ),
    TableRuling(
        tableName="procurement_rfq_decision",
        replacement="0160",
        reason=(
            "询价决策留痕（M5-2，DoD 4：谁/何时/批驳/意见，不覆盖历史）："
            "approval_task 是 P5 通用审批矩阵（需已发布 definition + matrix），"
            "RFQ 首期没有矩阵化审批 ⇒ 决策事实自带留痕表，避免第二套审批语义"
        ),
    ),
)

# 延后（D3：M6 决策）。**M6 前不许建**；若 M6 裁决要建，先改本目录再动表。
DEFERRED_TO_M6_TABLES: tuple[TableRuling, ...] = (
    TableRuling(
        tableName="procurement_rule_definition",
        replacement="候选：扩展 feature_rule 加 version 字段",
        reason="feature_rule 已有 evaluator + registry + 阈值表 + CRUD；新表 = 第二套规则语义",
    ),
    TableRuling(
        tableName="procurement_rule_version",
        replacement="候选：随 feature_rule 扩展",
        reason="同上；D3 建议先扩展 feature_rule",
    ),
)

# 所有受裁决管控的表名（守卫用；含建/延后 —— 建表迁移必须与
# 本目录对齐，不许绕开裁决新增 procurement_* 表）。注意：含 1 张**改名**的批准表
# ``semantic_release_check``（不带 procurement_ 前缀，改名自草案
# procurement_ontology_release_check）。
GOVERNED_PROCUREMENT_TABLE_NAMES: frozenset[str] = frozenset(
    ruling.tableName for ruling in (*FORBIDDEN_DUPLICATE_TABLES, *APPROVED_TABLES, *DEFERRED_TO_M6_TABLES)
)


def rulingFor(tableName: str) -> TableRuling | None:
    """按表名取裁决；不在管控范围返回 ``None``。"""
    for ruling in (*FORBIDDEN_DUPLICATE_TABLES, *APPROVED_TABLES, *DEFERRED_TO_M6_TABLES):
        if ruling.tableName == tableName:
            return ruling
    return None
