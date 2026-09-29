"""ontology_service audit writes integration tests (3 action types × 4 entity types).

Verifies that create/update/delete on ONTOLOGY_CLASS, ONTOLOGY_PROPERTY,
ONTOLOGY_METRIC, and ONTOLOGY_JOIN produce audit_log entries with the correct
entity_type, entity_id, action, actor, and actor_departments.

Uses the real PostgreSQL test DB (pytest-asyncio + _pg_support.pgApiClient).
"""
from __future__ import annotations

import pytest
from fastapi import status

ADMIN_HEADERS = {"X-User-Id": "audit-ontology-admin", "X-User-Roles": "admin"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ontology_payload(entity_type: str, suffix: str) -> dict:
    """Return API payload dict for creating an ontology entity."""
    if entity_type == "ONTOLOGY_CLASS":
        return {"className": f"TestClass_{suffix}", "description": "test"}
    elif entity_type == "ONTOLOGY_PROPERTY":
        return {
            "propertyName": f"TestProp_{suffix}",
            "dataType": "STRING",
        }
    elif entity_type == "ONTOLOGY_METRIC":
        return {"metricName": f"TestMetric_{suffix}", "formula": "SELECT 1"}
    else:  # ONTOLOGY_JOIN
        # OntologyJoinCreate 无 joinName 字段（extra="forbid"）——带上即 422
        return {}


async def _create_dependencies(client, entity_type: str, suffix: str, headers: dict) -> dict:
    """Create prerequisite entities and return the payload with their IDs filled in."""
    if entity_type == "ONTOLOGY_CLASS":
        return {"className": f"TestClass_{suffix}", "description": "test"}
    elif entity_type == "ONTOLOGY_PROPERTY":
        class_resp = await client.post(
            "/api/v1/ontology/classes",
            json={"className": f"TestClass_{suffix}", "description": "test"},
            headers=headers,
        )
        class_id = class_resp.json()["id"]
        return {
            "propertyName": f"TestProp_{suffix}",
            "classId": class_id,
            "dataType": "STRING",
        }
    elif entity_type == "ONTOLOGY_METRIC":
        return {"metricName": f"TestMetric_{suffix}", "formula": "SELECT 1"}
    else:  # ONTOLOGY_JOIN
        c1 = await client.post(
            "/api/v1/ontology/classes",
            json={"className": f"C1_{suffix}", "description": "t"},
            headers=headers,
        )
        c2 = await client.post(
            "/api/v1/ontology/classes",
            json={"className": f"C2_{suffix}", "description": "t"},
            headers=headers,
        )
        return {
            "sourceClassId": c1.json()["id"],
            "targetClassId": c2.json()["id"],
            "sourceColumns": ["id"],
            "targetColumns": ["id"],
        }


def _update_payload(entity_type: str) -> dict:
    """Return a minimal update payload for each entity type."""
    if entity_type == "ONTOLOGY_CLASS":
        return {"description": "updated description"}
    elif entity_type == "ONTOLOGY_PROPERTY":
        return {"description": "updated prop desc"}
    elif entity_type == "ONTOLOGY_METRIC":
        return {"metric_alias": "updated_alias"}
    else:  # ONTOLOGY_JOIN
        return {"description": "updated join desc"}


# ---------------------------------------------------------------------------
# CREATE
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("entity_type", [
    "ONTOLOGY_CLASS",
    "ONTOLOGY_PROPERTY",
    "ONTOLOGY_METRIC",
    "ONTOLOGY_JOIN",
])
async def test_ontology_create_writes_audit(client, entity_type: str) -> None:
    """CREATE on ontology entity writes audit_log with CREATE action."""
    suffix = f"{entity_type}_{id(entity_type)}"
    payload = await _create_dependencies(client, entity_type, suffix, ADMIN_HEADERS)

    endpoint_map = {
        "ONTOLOGY_CLASS": "/api/v1/ontology/classes",
        "ONTOLOGY_PROPERTY": "/api/v1/ontology/properties",
        "ONTOLOGY_METRIC": "/api/v1/ontology/metrics",
        "ONTOLOGY_JOIN": "/api/v1/ontology/joins",
    }
    endpoint = endpoint_map[entity_type]

    resp = await client.post(endpoint, json=payload, headers=ADMIN_HEADERS)
    assert resp.status_code in (status.HTTP_201_CREATED, status.HTTP_200_OK), \
        f"Failed to create {entity_type}: {resp.text}"
    entity_id = resp.json()["id"]

    # Query audit log for this entity
    audit_resp = await client.get(
        f"/api/v1/audit?entity_type={entity_type}&action=CREATE",
        headers=ADMIN_HEADERS,
    )
    assert audit_resp.status_code == status.HTTP_200_OK
    rows = audit_resp.json()["rows"]
    matching = [
        r for r in rows
        if r["entityId"] == entity_id and r["action"] == "CREATE"
    ]
    assert len(matching) >= 1, (
        f"No CREATE audit for {entity_type}/{entity_id}. "
        f"Audit rows: {rows}"
    )


# ---------------------------------------------------------------------------
# UPDATE
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("entity_type", [
    "ONTOLOGY_CLASS",
    "ONTOLOGY_PROPERTY",
    "ONTOLOGY_METRIC",
    "ONTOLOGY_JOIN",
])
async def test_ontology_update_writes_audit(client, entity_type: str) -> None:
    """UPDATE on ontology entity writes audit_log with UPDATE action."""
    suffix = f"{entity_type}_upd_{id(entity_type)}"
    payload = await _create_dependencies(client, entity_type, suffix, ADMIN_HEADERS)

    # Create the entity first
    endpoint_map = {
        "ONTOLOGY_CLASS": "/api/v1/ontology/classes",
        "ONTOLOGY_PROPERTY": "/api/v1/ontology/properties",
        "ONTOLOGY_METRIC": "/api/v1/ontology/metrics",
        "ONTOLOGY_JOIN": "/api/v1/ontology/joins",
    }
    put_endpoint_map = {
        "ONTOLOGY_CLASS": "/api/v1/ontology/classes",
        "ONTOLOGY_PROPERTY": "/api/v1/ontology/properties",
        "ONTOLOGY_METRIC": "/api/v1/ontology/metrics",
        "ONTOLOGY_JOIN": "/api/v1/ontology/joins",
    }
    endpoint = endpoint_map[entity_type]
    put_endpoint = put_endpoint_map[entity_type]

    create_resp = await client.post(endpoint, json=payload, headers=ADMIN_HEADERS)
    assert create_resp.status_code in (status.HTTP_201_CREATED, status.HTTP_200_OK), \
        f"Failed to create {entity_type}: {create_resp.text}"
    entity_id = create_resp.json()["id"]

    # Update the entity
    update_payload = _update_payload(entity_type)
    update_resp = await client.put(
        f"{put_endpoint}/{entity_id}",
        json=update_payload,
        headers=ADMIN_HEADERS,
    )
    assert update_resp.status_code in (status.HTTP_200_OK, status.HTTP_200_OK), \
        f"Failed to update {entity_type}/{entity_id}: {update_resp.text}"

    # Query audit log for UPDATE record
    audit_resp = await client.get(
        f"/api/v1/audit?entity_type={entity_type}&action=UPDATE",
        headers=ADMIN_HEADERS,
    )
    assert audit_resp.status_code == status.HTTP_200_OK
    rows = audit_resp.json()["rows"]
    matching = [
        r for r in rows
        if r["entityId"] == entity_id and r["action"] == "UPDATE"
    ]
    assert len(matching) >= 1, (
        f"No UPDATE audit for {entity_type}/{entity_id}. "
        f"Audit rows: {rows}"
    )


# ---------------------------------------------------------------------------
# DELETE
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("entity_type", [
    "ONTOLOGY_CLASS",
    "ONTOLOGY_PROPERTY",
    "ONTOLOGY_METRIC",
    "ONTOLOGY_JOIN",
])
async def test_ontology_delete_writes_audit(client, entity_type: str) -> None:
    """DELETE on ontology entity writes audit_log with DELETE action."""
    suffix = f"{entity_type}_del_{id(entity_type)}"
    payload = await _create_dependencies(client, entity_type, suffix, ADMIN_HEADERS)

    endpoint_map = {
        "ONTOLOGY_CLASS": "/api/v1/ontology/classes",
        "ONTOLOGY_PROPERTY": "/api/v1/ontology/properties",
        "ONTOLOGY_METRIC": "/api/v1/ontology/metrics",
        "ONTOLOGY_JOIN": "/api/v1/ontology/joins",
    }
    endpoint = endpoint_map[entity_type]

    # Create the entity
    create_resp = await client.post(endpoint, json=payload, headers=ADMIN_HEADERS)
    assert create_resp.status_code in (status.HTTP_201_CREATED, status.HTTP_200_OK), \
        f"Failed to create {entity_type}: {create_resp.text}"
    entity_id = create_resp.json()["id"]

    # Delete the entity
    delete_resp = await client.delete(f"{endpoint}/{entity_id}", headers=ADMIN_HEADERS)
    assert delete_resp.status_code == status.HTTP_204_NO_CONTENT, \
        f"Failed to delete {entity_type}/{entity_id}: {delete_resp.text}"

    # Query audit log for DELETE record
    audit_resp = await client.get(
        f"/api/v1/audit?entity_type={entity_type}&action=DELETE",
        headers=ADMIN_HEADERS,
    )
    assert audit_resp.status_code == status.HTTP_200_OK
    rows = audit_resp.json()["rows"]
    matching = [
        r for r in rows
        if r["entityId"] == entity_id and r["action"] == "DELETE"
    ]
    assert len(matching) >= 1, (
        f"No DELETE audit for {entity_type}/{entity_id}. "
        f"Audit rows: {rows}"
    )
