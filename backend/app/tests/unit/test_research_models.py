"""research_models 元数据守卫：5 张表 + 关键列必须存在（unit，不连库）。"""
from sqlalchemy.dialects import postgresql

from app.domain.research_models import (
    ResearchCheckpoint,
    ResearchFinding,
    ResearchReport,
    ResearchSession,
    ResearchTurn,
)


def test_five_tables_registered() -> None:
    assert ResearchSession.__tablename__ == "research_session"
    assert ResearchTurn.__tablename__ == "research_turn"
    assert ResearchCheckpoint.__tablename__ == "research_checkpoint"
    assert ResearchFinding.__tablename__ == "research_finding"
    assert ResearchReport.__tablename__ == "research_report"


def test_session_columns() -> None:
    cols = {c.name for c in ResearchSession.__table__.columns}
    assert {"id", "title", "mode", "status", "created_by", "input_seed", "updated_at"} <= cols


def test_checkpoint_columns() -> None:
    cols = {c.name for c in ResearchCheckpoint.__table__.columns}
    assert {"id", "session_id", "turn_id", "phase", "status",
            "options", "user_choice", "decided_at"} <= cols


def test_finding_columns() -> None:
    cols = {c.name for c in ResearchFinding.__table__.columns}
    assert {"id", "session_id", "turn_id", "claim_text",
            "supporting_sql", "supporting_data", "confidence",
            "created_at"} <= cols


def test_report_partial_unique_index() -> None:
    idx = {i.name for i in ResearchReport.__table__.indexes}
    assert "uq_research_report_session_published" in idx
    target = next(i for i in ResearchReport.__table__.indexes
                  if i.name == "uq_research_report_session_published")
    assert target.dialect_options["postgresql"]["where"] is not None
    assert target.unique is True
    assert "status = 'published'" in str(
        target.dialect_options["postgresql"]["where"])


def test_turn_content_is_jsonb() -> None:
    col = ResearchTurn.__table__.columns["content"]
    assert isinstance(col.type, postgresql.JSONB)
