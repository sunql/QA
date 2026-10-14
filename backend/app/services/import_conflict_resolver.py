"""导入冲突检测器。

fix-class-tombstone-restore：detect_conflicts 接受未过滤的
`existing_classes_all`（含软删墓碑），对同名表且墓碑的命中点报
CLASS_TOMBSTONED 冲突；同名同时存在活 + 墓碑时优先报 CLASS（活类是
真冲突，墓碑被隐藏，避免双报）。
"""

from __future__ import annotations

from app.domain.schemas import ConflictType, ImportConflict


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
        conflicts: list[ImportConflict] = []
        existing_class_by_table = {
            (c.source_table or "").lower(): c for c in existing_classes if c.source_table
        }
        # 墓碑视图：只包含 valid_to != null 的类。表名 → 墓碑行。
        # 同表双行（活 + 墓碑）时此处存的是后者；若活类先匹配，墓碑命中被跳过。
        tomb_by_table: dict[str, object] = {}
        for c in (existing_classes_all or []):
            if c.source_table and c.valid_to is not None:
                tomb_by_table.setdefault((c.source_table or "").lower(), c)
        for pc in proposed_classes:
            table = (pc.get("source_table") or "").lower()
            existing = existing_class_by_table.get(table)
            if existing:
                conflicts.append(
                    ImportConflict(
                        type=ConflictType.CLASS,
                        source_table=pc["source_table"],
                        existing_id=existing.id,
                        existing_name=existing.class_name,
                        proposed_name=pc["class_name"],
                    )
                )
                continue  # 活类是真正的覆盖冲突；不再报墓碑（同表双报会干扰前端消歧）
            tomb = tomb_by_table.get(table)
            if tomb:
                conflicts.append(
                    ImportConflict(
                        type=ConflictType.CLASS_TOMBSTONED,
                        source_table=pc["source_table"],
                        existing_id=tomb.id,
                        existing_name=tomb.class_name,
                        proposed_name=pc["class_name"],
                        existing_valid_to=tomb.valid_to,
                    )
                )

        existing_prop_key = {
            ((p.ontology_class.source_table or "").lower(), (p.source_column or "").lower()): p
            for p in existing_properties
            if p.ontology_class and p.source_column
        }
        for pp in proposed_properties:
            key = (pp.get("source_table", "").lower(), pp.get("source_column", "").lower())
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
