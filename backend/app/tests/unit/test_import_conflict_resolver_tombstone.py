"""ImportConflictResolver 墓碑检测单元测试（fix-class-tombstone-restore）。

不动真实 DB —— 构造 OntologyClass 实例即可走完整匹配逻辑（resolver 不查 DB）。
每个用例命名锚在场景上，便于后续维护。

fix-import-tombstone-name-guard：匹配口径对齐 createClass 的**类名**判重。
createClass 按 class_name 占用（含墓碑，唯一约束 uq_ontology_class_name_version
只认 class_name+version，与 source_table 无关），故 resolver 必须**同时**按类名
匹配；只按 source_table 匹配会漏报 ⇒ 用户勾了行、到执行期才炸「类名 X 已存在」，
而那个类在「本体管理」页又查不到（墓碑默认隐藏）⇒ 死结。
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
    version: int = 1,
) -> OntologyClass:
    """构造一个 ORM 实例；valid_from 必填（NOT NULL）。其他字段保持空。"""
    return OntologyClass(
        id=id,
        class_name=name,
        source_table=source_table,
        valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        valid_to=valid_to,
        version=version,
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


def test_tombstone_matched_by_class_name_when_table_differs():
    """墓碑**类名**与提议类名相同（源表不同）→ 仍应报 CLASS_TOMBSTONED。

    口径对齐 createClass：它按 class_name 判重（唯一约束 uq_ontology_class_name_version
    只认 class_name+version，与 source_table 无关）。只按 source_table 匹配会漏报
    ⇒ 执行期才炸「类名 X 已存在」，而那个墓碑在「本体管理」页默认看不见 ⇒ 死结。
    """
    resolver = ImportConflictResolver()
    when = datetime(2026, 9, 19, 4, 6, 4, tzinfo=timezone.utc)
    tomb = [_class(id=9, name="X", source_table="DWD_OTHER", valid_to=when)]
    proposed = [{"class_name": "X", "source_table": "DWD_CUSTOMER"}]
    conflicts = resolver.detect_conflicts(
        proposed, [], [], [], existing_classes_all=tomb
    )
    assert len(conflicts) == 1
    assert conflicts[0].type == ConflictType.CLASS_TOMBSTONED
    assert conflicts[0].existing_id == 9
    assert conflicts[0].existing_valid_to == when
    # 冲突落在「提议的那张表」上：前端按 sourceTable 定位要禁勾的行
    assert conflicts[0].source_table == "DWD_CUSTOMER"


def test_live_class_matched_by_class_name_when_table_differs():
    """活类**占名**（源表不同）→ CLASS 冲突（执行期 createClass 同样按名判重）。"""
    resolver = ImportConflictResolver()
    live = [_class(id=1, name="X", source_table="DWD_OTHER")]
    proposed = [{"class_name": "X", "source_table": "DWD_CUSTOMER"}]
    conflicts = resolver.detect_conflicts(
        proposed, [], live, [], existing_classes_all=live
    )
    assert len(conflicts) == 1
    assert conflicts[0].type == ConflictType.CLASS
    assert conflicts[0].existing_id == 1
    assert conflicts[0].source_table == "DWD_CUSTOMER"


def test_tombstone_without_source_table_still_blocks_by_name():
    """墓碑 source_table 为空但类名占用 → 仍命中（唯一约束只看 class_name）。"""
    resolver = ImportConflictResolver()
    when = datetime(2026, 9, 19, tzinfo=timezone.utc)
    tomb = [_class(id=9, name="X", source_table=None, valid_to=when)]
    proposed = [{"class_name": "X", "source_table": "DWD_CUSTOMER"}]
    conflicts = resolver.detect_conflicts(
        proposed, [], [], [], existing_classes_all=tomb
    )
    assert len(conflicts) == 1
    assert conflicts[0].type == ConflictType.CLASS_TOMBSTONED
    assert conflicts[0].existing_id == 9


def test_live_class_without_source_table_still_blocks_by_name():
    """活类 source_table 为空但类名占用 → CLASS（对称于上面的墓碑用例）。"""
    resolver = ImportConflictResolver()
    live = [_class(id=1, name="X", source_table=None)]
    proposed = [{"class_name": "X", "source_table": "DWD_CUSTOMER"}]
    conflicts = resolver.detect_conflicts(
        proposed, [], live, [], existing_classes_all=live
    )
    assert len(conflicts) == 1
    assert conflicts[0].type == ConflictType.CLASS
    assert conflicts[0].existing_id == 1


def test_no_conflict_when_neither_name_nor_table_matches():
    """类名与源表都不撞 → 无冲突（真正的阴性用例）。"""
    resolver = ImportConflictResolver()
    when = datetime(2026, 9, 19, tzinfo=timezone.utc)
    tomb = [_class(id=9, name="X", source_table="DWD_OTHER", valid_to=when)]
    proposed = [{"class_name": "Y", "source_table": "DWD_CUSTOMER"}]
    conflicts = resolver.detect_conflicts(
        proposed, [], [], [], existing_classes_all=tomb
    )
    assert conflicts == []


def test_live_class_name_wins_over_tombstone_with_same_name():
    """同名活类 + 同名墓碑 → 只报一条 CLASS（活类是真冲突，墓碑不双报）。"""
    resolver = ImportConflictResolver()
    when = datetime(2026, 9, 19, tzinfo=timezone.utc)
    live = [_class(id=1, name="X", source_table="DWD_CUSTOMER")]
    tomb = [_class(id=2, name="X", source_table="DWD_OLD", valid_to=when)]
    proposed = [{"class_name": "X", "source_table": "DWD_CUSTOMER"}]
    conflicts = resolver.detect_conflicts(
        proposed, [], live, [], existing_classes_all=live + tomb
    )
    assert [c.type for c in conflicts] == [ConflictType.CLASS]
    assert conflicts[0].existing_id == 1

# ===== ===== =====
# fix-import-tombstone-name-guard 第二轮（code-reviewer HIGH）：
# 冲突必须区分「**类名**被占（createClass 必然拒）」与「仅**源表**相同（能建成）」。
# 前端据此决定禁勾与否 ⇒ 判据必须落在**类名**上，否则会把本可成功的导入堵死。

def test_name_collision_is_flagged_when_class_name_is_occupied():
    """类名被墓碑占 ⇒ name_collision=True（前端据此禁勾）。"""
    resolver = ImportConflictResolver()
    when = datetime(2026, 9, 19, 4, 6, 4, tzinfo=timezone.utc)
    tomb = [_class(id=9, name="DWD_CUSTOMER", source_table="DWD_CUSTOMER", valid_to=when)]
    proposed = [{"class_name": "DWD_CUSTOMER", "source_table": "DWD_CUSTOMER"}]
    conflicts = resolver.detect_conflicts(
        proposed, [], [], [], existing_classes_all=tomb
    )
    assert len(conflicts) == 1
    assert conflicts[0].type == ConflictType.CLASS_TOMBSTONED
    assert conflicts[0].name_collision is True


def test_table_only_tombstone_hit_is_not_a_name_collision():
    """仅源表相同、**类名没被占** ⇒ 不是 name_collision（createClass 会成功）。

    真实形态：手工建的类 class_name 与 source_table 不同（如 class_name=
    CUSTOMER_MASTER / source_table=DWD_CUSTOMER），删掉后重导该表。
    提议类名恒等于源表（local_import_service: class_name=table_name）
    ⇒ 类名没被占 ⇒ 导入能成功 ⇒ 前端**不得**禁勾这一行。
    """
    resolver = ImportConflictResolver()
    when = datetime(2026, 9, 19, 4, 6, 4, tzinfo=timezone.utc)
    tomb = [_class(id=7, name="CUSTOMER_MASTER", source_table="DWD_CUSTOMER", valid_to=when)]
    proposed = [{"class_name": "DWD_CUSTOMER", "source_table": "DWD_CUSTOMER"}]
    conflicts = resolver.detect_conflicts(
        proposed, [], [], [], existing_classes_all=tomb
    )
    assert len(conflicts) == 1
    assert conflicts[0].type == ConflictType.CLASS_TOMBSTONED
    assert conflicts[0].name_collision is False
    # 冲突仍落在提议的那张表上（前端按它定位行）
    assert conflicts[0].source_table == "DWD_CUSTOMER"


def test_name_match_wins_over_table_match():
    """类名命中的占用者**优先**于源表命中的占用者（后者未必挡路）。

    墓碑A(name=X, table=T1)、墓碑B(name=Y, table=T2)；提议(name=Y, table=T1)
    ⇒ 真正挡 createClass 的是墓碑B（占名 Y）⇒ 报告它。
    """
    resolver = ImportConflictResolver()
    when = datetime(2026, 9, 19, tzinfo=timezone.utc)
    tomb = [
        _class(id=7, name="X", source_table="T1", valid_to=when),
        _class(id=8, name="Y", source_table="T2", valid_to=when),
    ]
    proposed = [{"class_name": "Y", "source_table": "T1"}]
    conflicts = resolver.detect_conflicts(
        proposed, [], [], [], existing_classes_all=tomb
    )
    assert len(conflicts) == 1
    assert conflicts[0].existing_id == 8
    assert conflicts[0].existing_name == "Y"
    assert conflicts[0].name_collision is True


def test_tombstone_name_collision_wins_over_live_table_match():
    """活类只占源表、墓碑占类名 ⇒ 报**墓碑**（它才是挡 createClass 的那个）。

    否则前端只看到活类（可见、可改名）而不禁勾 ⇒ 用户执行期才撞墓碑 ⇒
    「预览期看不见、执行期才炸」的原样残留。
    """
    resolver = ImportConflictResolver()
    when = datetime(2026, 9, 19, tzinfo=timezone.utc)
    live = [_class(id=1, name="OTHER", source_table="T1")]
    tomb = [_class(id=8, name="Y", source_table="T2", valid_to=when)]
    proposed = [{"class_name": "Y", "source_table": "T1"}]
    conflicts = resolver.detect_conflicts(
        proposed, [], live, [], existing_classes_all=live + tomb
    )
    assert len(conflicts) == 1
    assert conflicts[0].type == ConflictType.CLASS_TOMBSTONED
    assert conflicts[0].existing_id == 8
    assert conflicts[0].name_collision is True


def test_live_table_only_match_is_not_a_name_collision():
    """活类仅占源表、类名没被占 ⇒ name_collision=False（导入可成功）。"""
    resolver = ImportConflictResolver()
    live = [_class(id=1, name="OTHER", source_table="T1")]
    proposed = [{"class_name": "Y", "source_table": "T1"}]
    conflicts = resolver.detect_conflicts(
        proposed, [], live, [], existing_classes_all=live
    )
    assert len(conflicts) == 1
    assert conflicts[0].type == ConflictType.CLASS
    assert conflicts[0].name_collision is False


# ===== ===== =====
# fix-import-tombstone-name-guard 第三轮（code-reviewer：占用者选取必须确定，
# 且与 createClass 报错的那一行**同 id**）——否则前端提示「去恢复 id=N」会指错行。

def test_tombstone_selection_matches_create_class_order():
    """同名多个墓碑 ⇒ 取**最早删除**的那个（= createClass 的取行顺序）。

    createClass: `order_by(valid_to ASC NULLS FIRST)` ⇒ 活类优先；多个墓碑时
    取 valid_to 最小（最早删除）的那一个。resolver 必须报同一个 id。
    """
    resolver = ImportConflictResolver()
    early = datetime(2026, 9, 1, tzinfo=timezone.utc)
    late = datetime(2026, 9, 19, tzinfo=timezone.utc)
    tomb = [
        _class(id=7, name="X", source_table="T", valid_to=late),
        _class(id=8, name="X", source_table="T", valid_to=early),
    ]
    proposed = [{"class_name": "X", "source_table": "T"}]
    conflicts = resolver.detect_conflicts(
        proposed, [], [], [], existing_classes_all=tomb
    )
    assert len(conflicts) == 1
    assert conflicts[0].existing_id == 8
    assert conflicts[0].existing_valid_to == early


def test_selection_is_independent_of_input_order():
    """输入顺序不影响选出的占用者（否则同一个库两次预览报出不同的 id）。"""
    resolver = ImportConflictResolver()
    early = datetime(2026, 9, 1, tzinfo=timezone.utc)
    late = datetime(2026, 9, 19, tzinfo=timezone.utc)
    first = _class(id=7, name="X", source_table="T", valid_to=late)
    second = _class(id=8, name="X", source_table="T", valid_to=early)
    proposed = [{"class_name": "X", "source_table": "T"}]
    a = resolver.detect_conflicts(proposed, [], [], [], existing_classes_all=[first, second])
    b = resolver.detect_conflicts(proposed, [], [], [], existing_classes_all=[second, first])
    assert a[0].existing_id == b[0].existing_id == 8


def test_duplicate_live_class_name_picks_highest_version():
    """同名多个活类（不同 version）⇒ 取 version 最大的（与 createClass 的兜底排序一致）。"""
    resolver = ImportConflictResolver()
    live = [
        _class(id=1, name="X", source_table="T", version=1),
        _class(id=2, name="X", source_table="T", version=2),
    ]
    proposed = [{"class_name": "X", "source_table": "T"}]
    conflicts = resolver.detect_conflicts(
        proposed, [], live, [], existing_classes_all=live
    )
    assert len(conflicts) == 1
    assert conflicts[0].existing_id == 2


def test_naive_valid_to_does_not_break_occupant_ordering():
    """墓碑 valid_to 是 naive datetime 时，占用者排序不能炸 TypeError。

    DB 列是 timestamptz（asyncpg 返回 aware），但排序键里一旦混入 naive 值，
    naive vs aware 的 datetime 比较会抛 TypeError ⇒ 整个预览 500。
    归一化到 UTC 之后再比。
    """
    resolver = ImportConflictResolver()
    naive = _class(id=1, name="X", source_table="X", valid_to=datetime(2026, 1, 1))
    aware = _class(
        id=2,
        name="X",
        source_table="X",
        valid_to=datetime(2026, 9, 19, 4, 6, 4, tzinfo=timezone.utc),
    )
    proposed = [{"class_name": "X", "source_table": "X"}]
    conflicts = resolver.detect_conflicts(
        proposed, [], [], [], existing_classes_all=[naive, aware]
    )
    assert len(conflicts) == 1
    # naive(2026-01-01) 归一化成 UTC 后更早 ⇒ 最早删除的那个排前
    assert conflicts[0].existing_id == 1


def test_class_name_matching_is_case_sensitive_like_postgres():
    """类名匹配必须**大小写敏感**（对齐 createClass 的 PG `=` 等值比较）。

    反向守卫：若有人把 `_indexByName` 改成 `.lower()`，resolver 会比 createClass
    更严 ⇒ 报出并不存在的占名 ⇒ 前端按 nameCollision=true 禁勾 ⇒ 真的禁掉一个
    本可成功导入的行（本次修复要防的反向风险）。
    """
    resolver = ImportConflictResolver()
    live = [_class(id=1, name="X", source_table="T")]
    tomb = [
        _class(
            id=2,
            name="X",
            source_table="T",
            valid_to=datetime(2026, 9, 19, tzinfo=timezone.utc),
        )
    ]
    # 提议类名 "x" ≠ 既有类名 "X"（PG 等值比较下不相等）⇒ 没占名
    proposed = [{"class_name": "x", "source_table": "T"}]
    conflicts = resolver.detect_conflicts(
        proposed, [], live, [], existing_classes_all=live + tomb
    )
    assert len(conflicts) == 1
    assert conflicts[0].name_collision is False
