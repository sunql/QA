"""发布校验结论落表服务的**纯函数**部分（M1-1.2）。

落表/查询的**数据库行为**在 `integration/test_semantic_release_service.py`
（真实 PG）；这里钉纯逻辑：
- `toCheckDict` 的字段映射（ORM 行 ⇒ DTO 字典，键名必须与 schema 对上）；
- `summarizeChecksInDb` 的**唯一实现**（页内聚合孪生函数已删除 ⇒ 无口径漂移）。
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.domain.semantic_release_check_models import SemanticReleaseCheck
from app.services.semantic_release_check_service import toCheckDict


def _checkRow(
    *,
    code: str = "CODE_A",
    severity: str = "ERROR",
    result: str = "PASS",
    targetKey: str | None = "M_A",
) -> SemanticReleaseCheck:
    return SemanticReleaseCheck(
        release_id=1,
        check_code=code,
        check_item="校验项",
        severity=severity,
        result=result,
        target_type="METRIC",
        target_key=targetKey,
        detail="明细",
        checked_time=datetime.now(UTC),
        checked_by="tester",
    )


def test_to_check_dict_keeps_every_documented_field() -> None:
    """ORM 行 ⇒ DTO 字典：字段一个不能少（少一个 ⇒ 前端拿到 undefined）。"""
    row = _checkRow()
    row.id = 42

    payload = toCheckDict(row)

    assert payload == {
        "id": 42,
        "release_id": 1,
        "check_code": "CODE_A",
        "check_item": "校验项",
        "severity": "ERROR",
        "result": "PASS",
        "target_type": "METRIC",
        "target_key": "M_A",
        "detail": "明细",
        "checked_time": row.checked_time,
        "checked_by": "tester",
    }


def test_to_check_dict_keeps_null_target_key() -> None:
    """发布级校验项没有 targetKey ⇒ 必须是 None，不是空串（DB 里是 NULL）。"""
    payload = toCheckDict(_checkRow(targetKey=None))

    assert payload["target_key"] is None


def test_to_check_dict_returns_a_plain_dict_not_the_orm_row() -> None:
    """不能把 ORM 实例直接丢给序列化层（延迟加载 + 会话生命周期问题）。"""
    payload = toCheckDict(_checkRow())

    assert isinstance(payload, dict)
    assert not isinstance(payload, SemanticReleaseCheck)
