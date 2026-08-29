"""导入冲突检测器。"""

from __future__ import annotations

from app.domain.schemas import ConflictType, ImportConflict


class ImportConflictResolver:
    def detect_conflicts(
        self,
        proposed_classes: list[dict],
        proposed_properties: list[dict],
        existing_classes: list,
        existing_properties: list,
    ) -> list[ImportConflict]:
        conflicts: list[ImportConflict] = []
        existing_class_by_table = {
            (c.source_table or "").lower(): c for c in existing_classes if c.source_table
        }
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
