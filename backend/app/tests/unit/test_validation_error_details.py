"""ValidationError.details 扩展单测（Phase 6.5 Task 1）。

DomainError.detail（str）与 ValidationError.details（dict）是两个通道：
- detail  → 既有折叠展示用（前端 client.ts 读 detail 字符串）
- details → 结构化数据（candidates 列表等），仅 ValidationError 支持
"""

from __future__ import annotations

from app.domain.exceptions import ValidationError


class TestValidationErrorDetails:
    def test_details_kwarg_stored_and_defaults_none(self):
        err = ValidationError("m", details={"candidates": [["10105", "x"]]})
        assert err.details == {"candidates": [["10105", "x"]]}
        assert err.message == "m"
        assert err.detail is None  # 既有通道不受影响

    def test_details_defaults_to_none(self):
        err = ValidationError("m")
        assert err.details is None

    def test_detail_and_details_coexist(self):
        err = ValidationError("m", detail="str", details={"n": 1})
        assert err.detail == "str"
        assert err.details == {"n": 1}

    def test_other_domain_errors_have_no_details_attr_semantics(self):
        # 非 ValidationError 的 DomainError 不受影响（details 属性不存在语义）
        from app.domain.exceptions import ConflictError

        err = ConflictError("m")
        assert not hasattr(err, "details")
