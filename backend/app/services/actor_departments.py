"""actor_departments 归一：消除「tuple 契约」与「逗号分隔字符串契约」并存。

历史背景（2026-10-30 修复）：仓内对 `actor_departments` 同时存在两种形态——

- **tuple 契约**：`AuditService.record` / `OutboxService.enqueue` 声明
  ``tuple[str, ...] | None``，内部用 ``",".join(...)``（审计侧）；
- **字符串契约**：`OntologyService.*` / `KpiCatalogService.ensureDraftForMetric`
  声明 ``str | None``，本体侧 ``actor_departments.split(",")[0]`` 取首个部门
  作 `derivedOwner`（业务侧），API 层按 ``",".join(user.departments)`` 传。

两套契约的**下游互串**导致数据失真：本体侧把逗号分隔字符串透传给
`AuditService.record`，后者执行 ``",".join("采购部")`` ⇒ 审计落库值变成
``采,购,部``（逐字符逗号化）。反向（tuple 传入本体侧）则直接 AttributeError。

本模块提供唯一归一入口：任何形态先转成 **list[str]**，再由各落库侧按自身
需要渲染（审计侧逗号拼接字符串、outbox 侧 JSONB list）。

语义约定：
- `None` / 空 → `None`（表示"无部门"，不是空列表）；
- 字符串按逗号切分并去掉首尾空白，丢弃空片段（``"A,,B"`` → ``["A","B"]``）；
- tuple/list 保留原样（元素转 str，不去空白——历史行为保持不变）。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TypeAlias

DepartmentsInput: TypeAlias = "tuple[str, ...] | list[str] | str | None"


def departmentsToList(value: DepartmentsInput) -> list[str] | None:
    """把任意形态的部门参数归一为 ``list[str]``；无部门返回 None。

    归一后各落库侧自行渲染：
    - 审计字符串列：``",".join(list)``；
    - JSONB 列：直接存 list。
    """
    if value is None:
        return None
    if isinstance(value, str):
        parts = [part.strip() for part in value.split(",")]
        cleaned = [part for part in parts if part]
        return cleaned or None
    if isinstance(value, Sequence):
        items = [str(item) for item in value]
        return items or None
    # 非预期类型：降级为单元素列表（不静默丢弃——调用方仍能看出值没丢）
    return [str(value)]


def departmentsToString(value: DepartmentsInput) -> str | None:
    """归一为逗号分隔字符串（审计列口径）；无部门返回 None。"""
    items = departmentsToList(value)
    if not items:
        return None
    return ",".join(items)
