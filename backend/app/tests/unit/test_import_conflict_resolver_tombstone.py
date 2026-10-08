"""ImportConflictResolver 墓碑检测单元测试（fix-class-tombstone-restore）。

不动真实 DB —— 构造 OntologyClass 实例即可走完整匹配逻辑（resolver 不查 DB）。
每个用例命名锚在场景上，便于后续维护。
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.domain.models import OntologyClass
from app.domain.schemas import ConflictType
from app.services.import_conflict_resolver import ImportConflictResolver


def _class(
    *,
    id: int,
    name: str,
    source_table: str | None,
    valid_to: datetime | None = None,
) -> OntologyClass:
    """构造一个 ORM 实例；valid_from 必填（NOT NULL）。其他字段保持空。"""
    return OntologyClass(
        id=id,
        class_name=name,
        source_table=source_table,
        valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        valid_to=valid_to,
        version=1,
    )


# ===== ===== =====

def test_live_class_collision_reports_class_conflict():
    """现存活类同名 → CLASS（真覆盖），不是 CLASS_TOMBSTONED。"""
    resolver = ImportConflictResolver()
    live = [_class(id=1, name="X", source_table="CUSTOMER")]
    proposed = [{"class_name": "X", "source_table": "CUSTOMER"}]
    conflicts = resolver.detect_conflicts(
        proposed, [], live, [], existing_classes_all=live
    )
    assert len(conflicts) == 1
    assert conflicts[0].type == ConflictType.CLASS
    assert conflicts[0].existing_id == 1
    assert conflicts[0].existing_name == "X"
    assert conflicts[0].existing_valid_to is None


def test_tombstoned_class_reports_class_tombstoned_conflict():
    """软删墓碑类同名 → CLASS_TOMBSTONED，附 existing_valid_to。"""
    resolver = ImportConflictResolver()
    when = datetime(2026, 9, 19, 4, 6, 4, tzinfo=timezone.utc)
    tomb = [_class(id=9, name="DWD_BUSINESS_PARTNER", source_table="DWD_BUSINESS_PARTNER", valid_to=when)]
    proposed = [{"class_name": "DWD_BUSINESS_PARTNER", "source_table": "DWD_BUSINESS_PARTNER"}]
    conflicts = resolver.detect_conflicts(
        proposed, [], [], [], existing_classes_all=tomb
    )
    assert len(conflicts) == 1
    assert conflicts[0].type == ConflictType.CLASS_TOMBSTONED
    assert conflicts[0].existing_id == 9
    assert conflicts[0].existing_name == "DWD_BUSINESS_PARTNER"
    assert conflicts[0].existing_valid_to == when
    assert conflicts[0].source_table == "DWD_BUSINESS_PARTNER"


def test_live_and_tombstone_same_table_prefers_live():
    """同名同时存在活 + 墓碑 → 只报 CLASS（活类是真正的覆盖冲突，墓碑被隐藏）。

    设计选择：避免给同一个 source_table 报两条冲突，前端无法消歧。
    """
    resolver = ImportConflictResolver()
    when = datetime(2026, 9, 19, tzinfo=timezone.utc)
    live = [_class(id=1, name="X", source_table="CUSTOMER", valid_to=None)]
    tomb = [_class(id=2, name="X", source_table="CUSTOMER", valid_to=when)]
    proposed = [{"class_name": "X", "source_table": "CUSTOMER"}]
    conflicts = resolver.detect_conflicts(
        proposed, [], live, [], existing_classes_all=live + tomb
    )
    types = [c.type for c in conflicts]
    assert types == [ConflictType.CLASS]


def test_table_name_match_is_case_insensitive():
    """表名匹配不区分大小写（与现有 CLASS/PROPERTY 冲突一致）。"""
    resolver = ImportConflictResolver()
    when = datetime(2026, 9, 19, tzinfo=timezone.utc)
    tomb = [_class(id=9, name="X", source_table="dwd_customer", valid_to=when)]
    proposed = [{"class_name": "X", "source_table": "DWD_CUSTOMER"}]
    conflicts = resolver.detect_conflicts(
        proposed, [], [], [], existing_classes_all=tomb
    )
    assert len(conflicts) == 1
    assert conflicts[0].type == ConflictType.CLASS_TOMBSTONED


def test_no_tombstone_when_table_differs():
    """墓碑类 source_table 与 proposed 不同 → 不命中。"""
    resolver = ImportConflictResolver()
    when = datetime(2026, 9, 19, tzinfo=timezone.utc)
    tomb = [_class(id=9, name="X", source_table="DWD_OTHER", valid_to=when)]
    proposed = [{"class_name": "X", "source_table": "DWD_CUSTOMER"}]
    conflicts = resolver.detect_conflicts(
        proposed, [], [], [], existing_classes_all=tomb
    )
    assert conflicts == []


def test_empty_source_table_excluded():
    """墓碑类 source_table 为空（合法但不应参与匹配）→ proposed 同表也不命中。"""
    resolver = ImportConflictResolver()
    when = datetime(2026, 9, 19, tzinfo=timezone.utc)
    tomb = [_class(id=9, name="X", source_table=None, valid_to=when)]
    proposed = [{"class_name": "X", "source_table": "DWD_CUSTOMER"}]
    conflicts = resolver.detect_conflicts(
        proposed, [], [], [], existing_classes_all=tomb
    )
    assert conflicts == []


def test_class_with_null_source_table_does_not_match_proposed_with_table():
    """对照 test_empty_source_table_excluded：活类空 source_table 也不应误匹配。"""
    resolver = ImportConflictResolver()
    live = [_class(id=1, name="X", source_table=None)]
    proposed = [{"class_name": "X", "source_table": "DWD_CUSTOMER"}]
    conflicts = resolver.detect_conflicts(
        proposed, [], live, [], existing_classes_all=live
    )
    assert conflicts == []