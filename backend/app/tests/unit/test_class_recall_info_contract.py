"""ClassRecallInfo 披露字段契约（2026-11-19 数据源过滤+锚定批次）。

默认值保证向后兼容：旧响应（无这些字段）反序列化不受影响；wire 格式
camelCase（CamelModel），前端按 datasourceFiltered / datasourceFilterSkipped /
anchoredCount 读。
"""

from app.domain.schemas import ClassRecallInfo


def test_new_disclosure_fields_default_neutral() -> None:
    info = ClassRecallInfo(mode="recall")
    assert info.datasourceFiltered == 0
    assert info.datasourceFilterSkipped is None
    assert info.anchoredCount == 0


def test_new_disclosure_fields_serialize_camel_case() -> None:
    info = ClassRecallInfo(
        mode="expanded", datasourceFiltered=3,
        datasourceFilterSkipped="no_cache", anchoredCount=2,
    )
    payload = info.model_dump(by_alias=True)
    assert payload["datasourceFiltered"] == 3
    assert payload["datasourceFilterSkipped"] == "no_cache"
    assert payload["anchoredCount"] == 2
    # 旧字段不受影响
    assert payload["mode"] == "expanded"
