"""REFINE 方言参数的 chat_service 接线守卫（P0-1）。

applyRefineDirect 已按 datasourceType 选择方言，但只有 chat_service 把
`pc.ds.type` 真正传下去，Oracle 用户才能得到合法 SQL。本测试钉死这条接线：
漏传参数时方言退回默认，Oracle FETCH FIRST 又会被改成 LIMIT。
"""

from __future__ import annotations

from types import SimpleNamespace

from app.domain.enums import DataSourceType
from app.services.chat_service import ChatService


class _CapturingNl2Sql:
    """记录 applyRefineDirect 收到的 datasourceType。"""

    def __init__(self) -> None:
        self.datasourceType: DataSourceType | str | None = "unset"
        self.sql: str | None = None

    def applyRefineDirect(
        self,
        sql: str,
        plan: object,
        question: str,
        *,
        datasourceType: DataSourceType | str | None = None,
    ) -> str:
        self.datasourceType = datasourceType
        self.sql = sql
        return "SELECT 1"


def test_tryRefineDirect_passes_datasource_type_to_rewriter() -> None:
    fake = _CapturingNl2Sql()
    service = ChatService(nl2sqlService=fake)  # type: ignore[arg-type]
    state = SimpleNamespace(
        last_sql="SELECT NAME FROM ZJTH.T FETCH FIRST 10 ROWS ONLY",
        last_plan=None,
    )

    out = service._tryRefineDirect(state, "只要前 3 条", DataSourceType.ORACLE)

    assert out == "SELECT 1"
    assert fake.datasourceType is DataSourceType.ORACLE
    assert fake.sql == "SELECT NAME FROM ZJTH.T FETCH FIRST 10 ROWS ONLY"


def test_tryRefineDirect_without_datasource_type_keeps_none() -> None:
    fake = _CapturingNl2Sql()
    service = ChatService(nl2sqlService=fake)  # type: ignore[arg-type]
    state = SimpleNamespace(last_sql="SELECT NAME FROM ZJTH.T", last_plan=None)

    service._tryRefineDirect(state, "按 NAME 升序")

    assert fake.datasourceType is None
