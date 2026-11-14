"""导入冲突检测器。

fix-class-tombstone-restore：detect_conflicts 接受未过滤的
`existing_classes_all`（含软删墓碑），对同名表且墓碑的命中点报
CLASS_TOMBSTONED 冲突。

fix-import-tombstone-name-guard：匹配口径对齐 createClass 的**类名**判重。
createClass 的占名校验按 class_name 查**任意**行（含软删墓碑）——唯一约束
`uq_ontology_class_name_version` 只认 class_name+version，与 source_table
无关。故此处除 source_table 外**同时**按 class_name 建索引。

只按 source_table 匹配会漏报 ⇒ 用户勾了行、到执行期才撞「类名 X 已存在」，
而那个墓碑在「本体管理」页默认隐藏（valid_to IS NULL 过滤）⇒ 用户既看不到
也绕不开 ⇒ 死结。

**两条口径（大小写）**：
- 类名匹配**大小写敏感**（与 createClass 的 SQL 等值比较一致）；
- 源表匹配**大小写不敏感**（沿用既有语义）。

**命中优先级**：类名优先于源表。
「类名被占」才是真正会挡住 createClass 的那一个；「仅源表相同」说明**类名
没被占** ⇒ 导入其实能成功 ⇒ 只能作提示，不能当硬冲突。

⇒ 每条冲突带 `name_collision` 标志：**True = createClass 必然拒**（前端据此
禁勾），**False = 仅源表撞名/撞表**（导入可成功，前端只提示不禁勾）。
⚠️ **活类与墓碑一视同仁**：占住类名的是**活类**时同样必然拒（createClass 只按
class_name 判重）⇒ 前端同样禁勾；只按「冲突类型是不是墓碑」来判会漏掉活类占名
（那一行注定在执行期失败，却要等失败才知道）。

**占用者选取口径**：与 createClass 的取行顺序严格一致
（`valid_to ASC NULLS FIRST` ⇒ 活类优先；多个墓碑取最早删除的那个；再按
version DESC、id ASC 兜底）⇒ resolver 报出的 id 就是 createClass 会报的那个 id，
前端提示的「去恢复 id=N」不会指错行。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from app.domain.models import OntologyClass
from app.domain.schemas import ConflictType, ImportConflict

# 排序兜底：valid_to 为 None 的墓碑（理论不该有）排在最前
_MIN_TIME = datetime.min.replace(tzinfo=timezone.utc)


def _asUtc(value: datetime | None) -> datetime:
    """把 valid_to 归一化成 **aware UTC** 再参与比较。

    DB 列是 timestamptz（asyncpg 返回 aware），但排序键里一旦混入 naive 值，
    naive vs aware 的 datetime 比较会抛 TypeError ⇒ 整个预览 500。
    naive 一律按 UTC 解释（与 Postgres 对 timestamptz 的口径一致）。
    """
    if value is None:
        return _MIN_TIME
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _occupantOrderKey(c: OntologyClass) -> tuple:
    """占用者排序键：**必须与 ontology_service.createClass 的取行顺序一致**。

    createClass: ``order_by(valid_to ASC NULLS FIRST)`` ⇒ 活类（valid_to IS NULL)
    优先；多个墓碑时取 valid_to **最小**（最早删除）的那一个。resolver 报出的
    占用者 id 必须与 createClass 报错的那一行相同 ⇒ 前端提示的「去恢复 id=N」
    才指得对（否则前端说恢复 A、执行期报 B ⇒ 用户两头扑空）。

    兜底排序：version DESC、id ASC ⇒ 全序，避免同名多行时结果随输入顺序漂移。
    """
    return (
        c.valid_to is not None,  # 活类排前
        _asUtc(c.valid_to),  # 墓碑按删除时间升序 ⇒ 最早删除的排前
        -(c.version or 0),  # version 大者优先
        c.id,
    )


def _indexBy(
    classes: list[OntologyClass], keyOf: Callable[[OntologyClass], str | None]
) -> dict[str, OntologyClass]:
    """按 keyOf 建索引；同 key 多个候选时取排序键最小者（确定性）。

    keyOf 返回 None 的行不参与索引。
    """
    best: dict[str, OntologyClass] = {}
    for c in classes:
        key = keyOf(c)
        if not key:
            continue
        current = best.get(key)
        if current is None or _occupantOrderKey(c) < _occupantOrderKey(current):
            best[key] = c
    return best


def _indexByTable(classes: list[OntologyClass]) -> dict[str, OntologyClass]:
    """按 source_table 建索引（大小写不敏感）；空 source_table 不参与。"""
    return _indexBy(classes, lambda c: (c.source_table or "").lower() or None)


def _indexByName(classes: list[OntologyClass]) -> dict[str, OntologyClass]:
    """按 class_name 建索引（大小写敏感，对齐 createClass 的等值比较）。"""
    return _indexBy(classes, lambda c: c.class_name or None)


def _mergeClasses(
    live: list[OntologyClass], allClasses: list[OntologyClass] | None
) -> list[OntologyClass]:
    """合并「活类列表」与「含墓碑的全量列表」，按 id 去重。

    allClasses 为 None 时只有活类；否则以 allClasses 为准并补上只在 live 里
    出现的行（防御：调用方可能只传墓碑）。
    """
    if allClasses is None:
        return list(live)
    merged = {c.id: c for c in allClasses}
    for c in live:
        merged.setdefault(c.id, c)
    return list(merged.values())


class ImportConflictResolver:
    def detect_conflicts(
        self,
        proposed_classes: list[dict],
        proposed_properties: list[dict],
        existing_classes: list,
        existing_properties: list,
        *,
        existing_classes_all: list | None = None,
    ) -> list[ImportConflict]:
        """返回提议与既有本体之间的冲突列表（类冲突在前，属性冲突在后）。"""
        conflicts: list[ImportConflict] = []
        conflicts.extend(
            self._classConflicts(proposed_classes, existing_classes, existing_classes_all)
        )
        conflicts.extend(
            self._propertyConflicts(proposed_properties, existing_properties)
        )
        return conflicts

    # ===== 类冲突 =====

    def _classConflicts(
        self,
        proposed_classes: list[dict],
        existing_classes: list,
        existing_classes_all: list | None,
    ) -> list[ImportConflict]:
        """逐个提议类找占用者。

        口径（与 createClass 严格对齐）：
        1. **类名**被占（活类或墓碑）⇒ `name_collision=True` ⇒ createClass 必然拒；
        2. 否则**仅源表**相同 ⇒ `name_collision=False` ⇒ 类名没被占 ⇒ 导入能成功。

        索引统一建立在「活类 ∪ 墓碑」上：活类优先由 `_occupantOrderKey` 保证，
        与 createClass 的 valid_to NULLS FIRST 一致。
        """
        allClasses = _mergeClasses(existing_classes, existing_classes_all)
        by_table = _indexByTable(allClasses)
        by_name = _indexByName(allClasses)

        conflicts: list[ImportConflict] = []
        for pc in proposed_classes:
            occupant, nameCollision = self._findOccupant(pc, by_name, by_table)
            if occupant is None:
                continue
            conflicts.append(self._conflict(pc, occupant, nameCollision=nameCollision))
        return conflicts

    @staticmethod
    def _findOccupant(
        pc: dict,
        by_name: dict[str, OntologyClass],
        by_table: dict[str, OntologyClass],
    ) -> tuple[OntologyClass | None, bool]:
        """找出占用者 + 它是否**占名**（True）还是只撞表（False）。

        活类优先于墓碑：同名同时存在活类 + 墓碑时，活类是用户**看得见**的占用者
        （能改名、能删除），报它更可执行；这也与 createClass 的
        「先取活类」排序（valid_to NULLS FIRST）一致。
        """
        name = pc.get("class_name") or ""
        table = (pc.get("source_table") or "").lower()

        nameOccupant = by_name.get(name)
        if nameOccupant is not None:
            return nameOccupant, True

        tableOccupant = by_table.get(table)
        if tableOccupant is not None:
            return tableOccupant, False
        return None, False

    @staticmethod
    def _conflict(
        pc: dict, occupant: OntologyClass, *, nameCollision: bool
    ) -> ImportConflict:
        """构造类冲突项（source_table 恒为**提议的那张表** = 冲突落点）。"""
        isTombstone = occupant.valid_to is not None
        return ImportConflict(
            type=ConflictType.CLASS_TOMBSTONED if isTombstone else ConflictType.CLASS,
            source_table=pc.get("source_table"),
            existing_id=occupant.id,
            existing_name=occupant.class_name,
            proposed_name=pc.get("class_name"),
            existing_valid_to=occupant.valid_to,
            name_collision=nameCollision,
        )

    # ===== 属性冲突 =====

    @staticmethod
    def _propertyConflicts(
        proposed_properties: list[dict], existing_properties: list
    ) -> list[ImportConflict]:
        """属性冲突：按（源表, 源列）匹配（大小写不敏感），与既有语义一致。"""
        existing_prop_key = {
            (
                (p.ontology_class.source_table or "").lower(),
                (p.source_column or "").lower(),
            ): p
            for p in existing_properties
            if p.ontology_class and p.source_column
        }
        conflicts: list[ImportConflict] = []
        for pp in proposed_properties:
            key = (
                pp.get("source_table", "").lower(),
                pp.get("source_column", "").lower(),
            )
            existing = existing_prop_key.get(key)
            if existing:
                conflicts.append(
                    ImportConflict(
                        type=ConflictType.PROPERTY,
                        source_table=pp["source_table"],
                        source_column=pp["source_column"],
                        existing_id=existing.id,
                        existing_name=existing.property_name,
                        proposed_name=pp["property_name"],
                    )
                )
        return conflicts
