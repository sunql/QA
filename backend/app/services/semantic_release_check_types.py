"""发布校验项的**公共类型与常量**（M1-1.3 批次 B 拆出的叶子模块）。

为什么拆出来
------------
批次 B 要加「域级 + 绑定级」校验项（切片 / 绑定 / 孤岛判据），那部分逻辑的
口径与「指标级校验」完全独立，代码量约等于原模块的一半 ⇒ 塞进同一个文件会让
两组口径互相淹没，且域级逻辑要 import 切片模块（体量不小）。

拆成三层，依赖方向单向、**无环**：

    semantic_release_check_types        纯类型与常量（零业务依赖）
        ↑                    ↑
semantic_release_domain_checks   semantic_release_checks
        ↑                    ↑
        └──── semantic_release_service ────┘

⚠️ 类型住在叶子模块而不是 `semantic_release_checks` 里：域级模块要 import
`PublishCheck`，而 `semantic_release_checks` 又要 import 域级模块 ——
若类型只在后者，两边只能靠「函数参数注入」绕环（可读性差）或直接成环。

⚠️ 绑定的**规范身份键**也住这里：校验侧（`target_key` 列）与差异侧
（`SemanticChange.target`）必须用**同一个**渲染器 ⇒ 同一个绑定在两张表 /
两个接口里是同一个名字（可对账）。两边各写一份 ⇒ 迟早漂移 ⇒ 差异报告里的
一个绑定在 check 表里查不到。
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# ---- 严重级别 / 结论 / 目标类型 ----
# severity = 该校验项失败时的严重级别（ERROR 阻断发布 / WARNING 治理债）
# result   = 该校验项本次的结论（PASS / FAIL）
SEVERITY_ERROR = "ERROR"
SEVERITY_WARNING = "WARNING"
RESULT_PASS = "PASS"
RESULT_FAIL = "FAIL"
TARGET_RELEASE = "RELEASE"
TARGET_METRIC = "METRIC"
TARGET_BINDING = "BINDING"
# 设计稿 §5.5 校验码（G2）的被校验对象类型
TARGET_CLASS = "CLASS"
TARGET_BUSINESS_OBJECT = "BUSINESS_OBJECT"
TARGET_KPI = "KPI"
TARGET_AGENT = "AGENT"

# `semantic_release_check.target_key` 的列宽（String(200)）
MAX_TARGET_KEY_LENGTH = 200


def bindingTargetKey(binding: Mapping[str, Any]) -> str:
    """绑定的**规范身份键**：`类名@数据源.schema.表#角色`（校验侧与差异侧共用）。

    ⚠️ 必须带**角色**与**数据源**：一个类可以绑到同一张表的多个角色
    （BASE / NUMERATOR …），也可以在**两个数据源**里各绑一次；
    而唯一索引是 `(release_id, check_code, COALESCE(target_key,''))` ——
    少一个维度 ⇒ 两行撞唯一索引 ⇒ 整次发布 500。

    ⚠️ 列宽是 `String(200)`：类名最长 200 ⇒ 拼出来的键可能超宽 ⇒
    超宽时改用「类名摘要 + 完整身份哈希」，**不截断**（截断会让两个不同的
    绑定撞成同一个键）。
    """
    className = str(binding.get("className") or "")
    datasourceName = str(binding.get("datasourceName") or "")
    schemaName = str(binding.get("schemaName") or "")
    sourceTable = str(binding.get("sourceTable") or "")
    bindingRole = str(binding.get("bindingRole") or "")
    key = f"{className}@{datasourceName}.{schemaName}.{sourceTable}#{bindingRole}"
    if len(key) <= MAX_TARGET_KEY_LENGTH:
        return key
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return f"{className[:64]}~{digest}"


@dataclass(frozen=True, slots=True)
class PublishCheck:
    """一条发布校验结论（落表的一行）。

    `severity` 恒定非空（即便 `result=PASS`）；`targetType`/`targetKey` 定位被校验对象。
    """

    code: str
    item: str
    severity: str
    result: str
    targetType: str | None = None
    targetKey: str | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class PublishGateResult:
    """发布闸门结论。`blockers` 非空 = 拒绝进入 VALIDATED / ACTIVE。"""

    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def canPublish(self) -> bool:
        return not self.blockers
