"""P5 工作流定义契约（``workflow_definition.definition_json``）的解析。

契约（**纯数据**，改审批规则只改数据不改代码）：

```json
{
  "riskLevel": "MEDIUM",
  "steps": [
    {"code": "collect", "order": 0},
    {"code": "approve", "order": 1, "requiresApproval": true}
  ],
  "approvalMatrix": [
    {
      "matrixCode": "po_medium_manager",
      "subjectType": "workflow_instance",
      "capabilityCode": "purchase_order_approve",
      "riskLevel": "MEDIUM",
      "approverRole": "purchase_manager",
      "timeoutHours": 24
    }
  ]
}
```

设计约束（D-1）：

- **矩阵键固定三段**：``(subject_type, capability_code, risk_level)``。
  只泛化 subject 一维（给 WP-ONT-AI A4 的「四眼审核」预留），**不做可编程规则引擎**。
- **失败关闭**：矩阵选不中 ⇒ 调用方必须拒绝，**绝不「匹配不到就放行」**。
- **必须有审批人**：``approverRole`` / ``approverUser`` 至少有一个 ⇒ 否则会生成
  一条**谁也批不了**的审批任务（死等）。
- **提交人不得自审批**：四眼原则是**全局不变量**，不做成矩阵选项
  ⇒ 不需要往 ``approval_task`` 加列，也就没有「快照与实时解析不一致」的问题。
- **风险级别由服务端绑定**：定义体顶层的 ``riskLevel`` 决定用哪一条矩阵
  ⇒ **客户端不得自选**（自选风险级别 = 自选审批层级 = 挑低风险绕过高层审批）。

本模块是**纯函数**（不触 DB / 网络）⇒ 可穷举校验。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.domain.exceptions import ValidationError

# 字段长度上限（对齐库层列宽 String(64) / 审批超时上限）。
_MATRIX_CODE_MAX_LENGTH = 64
_KEY_MAX_LENGTH = 64
_ROLE_MAX_LENGTH = 64
_USER_MAX_LENGTH = 64

# 审批超时：默认 24 小时，最大 720 小时（30 天）。
DEFAULT_TIMEOUT_HOURS = 24
MAX_TIMEOUT_HOURS = 720

# definition_json 的顶层键。
KEY_STEPS = "steps"
KEY_APPROVAL_MATRIX = "approvalMatrix"
KEY_RISK_LEVEL = "riskLevel"


@dataclass(frozen=True)
class WorkflowStepSpec:
    """编排里的一步（**只读**，不改库）。"""

    code: str
    order: int
    requiresApproval: bool


@dataclass(frozen=True)
class ApprovalMatrixEntry:
    """审批矩阵的一条规则。

    字段名用 camelCase（与 definition_json 的键一致）⇒ 解析时无需转写。
    """

    matrixCode: str
    subjectType: str
    capabilityCode: str
    riskLevel: str
    approverRole: str | None
    approverUser: str | None
    timeoutHours: int


def _requireText(value: Any, field: str, maxLength: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"审批矩阵字段 {field} 必须是非空字符串")
    text = value.strip()
    if len(text) > maxLength:
        raise ValidationError(f"审批矩阵字段 {field} 超长（{len(text)} > {maxLength}）")
    return text


def _optionalText(value: Any, field: str, maxLength: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"审批矩阵字段 {field} 必须是非空字符串")
    text = value.strip()
    if len(text) > maxLength:
        raise ValidationError(f"审批矩阵字段 {field} 超长（{len(text)} > {maxLength}）")
    return text


def _parseTimeoutHours(value: Any) -> int:
    if value is None:
        return DEFAULT_TIMEOUT_HOURS
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError("审批矩阵字段 timeoutHours 必须是整数")
    if value < 1 or value > MAX_TIMEOUT_HOURS:
        raise ValidationError(f"审批矩阵字段 timeoutHours 越界（{value}，允许 1..{MAX_TIMEOUT_HOURS}）")
    return value


def parseApprovalMatrix(
    definitionJson: Mapping[str, Any] | None,
) -> tuple[ApprovalMatrixEntry, ...]:
    """解析 ``definition_json`` 里的审批矩阵。

    缺 ``approvalMatrix`` ⇒ 空元组（**不是错误**：没有审批环节的定义合法）。
    条目非法 ⇒ ValidationError（422）—— 让作者在保存定义时就看到问题，
    而不是等审批任务生成时才炸。
    """

    if not definitionJson:
        return ()
    raw = definitionJson.get(KEY_APPROVAL_MATRIX)
    if raw is None:
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ValidationError("definition_json.approvalMatrix 必须是数组")

    entries: list[ApprovalMatrixEntry] = []
    seenCodes: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise ValidationError(f"审批矩阵第 {index} 条必须是对象")
        matrixCode = _requireText(item.get("matrixCode"), "matrixCode", _MATRIX_CODE_MAX_LENGTH)
        if matrixCode in seenCodes:
            raise ValidationError(f"审批矩阵编码重复：{matrixCode}")
        seenCodes.add(matrixCode)

        approverRole = _optionalText(item.get("approverRole"), "approverRole", _ROLE_MAX_LENGTH)
        approverUser = _optionalText(item.get("approverUser"), "approverUser", _USER_MAX_LENGTH)
        if approverRole is None and approverUser is None:
            raise ValidationError(
                f"审批矩阵条目 {matrixCode} 既无 approverRole 也无 approverUser"
                "（会生成一条谁也批不了的审批任务）"
            )

        entries.append(
            ApprovalMatrixEntry(
                matrixCode=matrixCode,
                subjectType=_requireText(item.get("subjectType"), "subjectType", _KEY_MAX_LENGTH),
                capabilityCode=_requireText(item.get("capabilityCode"), "capabilityCode", _KEY_MAX_LENGTH),
                riskLevel=_requireText(item.get("riskLevel"), "riskLevel", _KEY_MAX_LENGTH),
                approverRole=approverRole,
                approverUser=approverUser,
                timeoutHours=_parseTimeoutHours(item.get("timeoutHours")),
            )
        )
    return tuple(entries)


def selectMatrixEntries(
    entries: Sequence[ApprovalMatrixEntry],
    *,
    subjectType: str,
    capabilityCode: str,
    riskLevel: str,
) -> tuple[ApprovalMatrixEntry, ...]:
    """按三段键选中矩阵条目（**精确匹配**，不做通配）。

    选不中返回空元组 —— **调用方必须据此失败关闭**（拒绝发起审批）。
    """

    return tuple(
        entry
        for entry in entries
        if entry.subjectType == subjectType
        and entry.capabilityCode == capabilityCode
        and entry.riskLevel == riskLevel
    )


def parseDefinitionRiskLevel(definitionJson: Mapping[str, Any] | None) -> str | None:
    """解析定义体顶层的 ``riskLevel``（**服务端绑定的风险级别**）。

    风险级别决定「走哪一条审批矩阵」⇒ 也决定了「谁有资格批」。
    因此它**只能由定义（发布方）决定**，客户端不得自选：客户端自选风险级别
    等于自选审批层级（挑低风险 = 绕过高层审批）。

    - 缺失 ⇒ 返回 ``None``（调用方据此**失败关闭**：没有风险级别就没有矩阵命中）；
    - 存在但非法 ⇒ ValidationError（发布时就该看到，而不是发起审批时才炸）。
    """

    if not definitionJson:
        return None
    raw = definitionJson.get(KEY_RISK_LEVEL)
    if raw is None:
        return None
    return _requireText(raw, KEY_RISK_LEVEL, _KEY_MAX_LENGTH)


def matrixEntryByCode(
    entries: Sequence[ApprovalMatrixEntry],
    matrixCode: str,
) -> ApprovalMatrixEntry | None:
    """按 ``matrixCode`` 取矩阵条目；取不到 ⇒ ``None``（调用方据此失败关闭）。

    审批任务的 ``matrix_code`` 指向定义体里的条目 key ⇒ **矩阵才是「谁有资格批」
    的唯一权威**（任务行只快照角色，不快照指定人）。
    """

    for entry in entries:
        if entry.matrixCode == matrixCode:
            return entry
    return None


def parseWorkflowSteps(
    definitionJson: Mapping[str, Any] | None,
) -> tuple[WorkflowStepSpec, ...]:
    """解析 ``definition_json`` 里的步骤编排。

    缺 ``steps`` ⇒ 空元组（无步骤的定义合法：一步即完成）。
    步骤编码在实例内必须唯一 ⇒ 重复直接拒绝（解析期发现，别等落库撞唯一约束）。
    """

    if not definitionJson:
        return ()
    raw = definitionJson.get(KEY_STEPS)
    if raw is None:
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ValidationError("definition_json.steps 必须是数组")

    specs: list[WorkflowStepSpec] = []
    seenCodes: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise ValidationError(f"步骤第 {index} 条必须是对象")
        code = _requireText(item.get("code"), "steps[].code", _KEY_MAX_LENGTH)
        if code in seenCodes:
            raise ValidationError(f"步骤编码重复：{code}")
        seenCodes.add(code)

        order = item.get("order", index)
        if isinstance(order, bool) or not isinstance(order, int) or order < 0:
            raise ValidationError(f"步骤 {code} 的 order 必须是非负整数")

        requiresApproval = item.get("requiresApproval", False)
        if not isinstance(requiresApproval, bool):
            raise ValidationError(f"步骤 {code} 的 requiresApproval 必须是布尔值")

        specs.append(WorkflowStepSpec(code=code, order=order, requiresApproval=requiresApproval))
    return tuple(specs)
