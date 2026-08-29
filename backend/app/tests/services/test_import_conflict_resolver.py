from app.domain.models import OntologyClass, OntologyProperty
from app.domain.schemas import ConflictType
from app.services.import_conflict_resolver import ImportConflictResolver


def test_detect_class_conflict_by_source_table():
    resolver = ImportConflictResolver()
    proposed = [{"source_table": "wms_inventory", "class_name": "wms_inventory"}]
    existing = [OntologyClass(id=1, class_name="库存", source_table="wms_inventory")]
    conflicts = resolver.detect_conflicts(proposed, [], existing, [])
    assert len(conflicts) == 1
    assert conflicts[0].type == ConflictType.CLASS
    assert conflicts[0].existing_id == 1


def test_no_conflict_for_new_table():
    resolver = ImportConflictResolver()
    proposed = [{"source_table": "wms_inventory", "class_name": "wms_inventory"}]
    existing = [OntologyClass(id=1, class_name="库存", source_table="wms_material")]
    conflicts = resolver.detect_conflicts(proposed, [], existing, [])
    assert len(conflicts) == 0


def test_detect_property_conflict_by_table_and_column():
    resolver = ImportConflictResolver()
    proposed_properties = [
        {"source_table": "wms_inventory", "source_column": "qty", "property_name": "qty"}
    ]
    existing_class = OntologyClass(id=1, class_name="库存", source_table="wms_inventory")
    existing_properties = [
        OntologyProperty(
            id=10,
            property_name="数量",
            source_column="qty",
            ontology_class=existing_class,
        )
    ]
    conflicts = resolver.detect_conflicts([], proposed_properties, [existing_class], existing_properties)
    assert len(conflicts) == 1
    assert conflicts[0].type == ConflictType.PROPERTY
    assert conflicts[0].existing_id == 10
    assert conflicts[0].existing_name == "数量"
    assert conflicts[0].proposed_name == "qty"
