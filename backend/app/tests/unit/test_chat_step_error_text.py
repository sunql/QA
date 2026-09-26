"""步骤错误文案脱敏 + 重试 token 携带的纯函数单元测试（不依赖 DB）。

对应 C3/C4 收尾复审的两条 MEDIUM：
1. `StepResult.error` 会进非流式响应与流式 `step_result` 事件（前端直接渲染），
   而 DB 驱动异常经 SQLAlchemy 包装后 `str()` 含 `[SQL: ...]`（内部表/列名）与
   `[parameters: ...]`（查询字面量）⇒ 用户可见文案必须剥掉这两段。
2. 重试生成成功、重试执行失败时，那次生成的 token 随异常交回调用方记账。
"""

from __future__ import annotations

from sqlalchemy.exc import ProgrammingError

from app.services.chat_service import (
    _attachRetryGenTokens,
    _retryGenTokens,
    _stepFailedError,
    _userFacingErrorText,
)


def _sqlalchemyErr() -> ProgrammingError:
    """真实格式的语句异常：驱动原因 + `[SQL: ...]` + `[parameters: ...]`。"""
    return ProgrammingError(
        "SELECT SECRET_COL FROM APP.SECRET_TABLE WHERE CUST_NAME=:n",
        {"n": "ACME-机密客户"},
        RuntimeError("ORA-00942: 表或视图不存在"),
    )


class TestUserFacingErrorText:
    def test_strips_sql_text_and_parameters(self) -> None:
        text = _userFacingErrorText(_sqlalchemyErr())

        # 驱动给的原因保留（用户据此才能自查）
        assert "ORA-00942" in text
        # SQL 全文 / 表名 / 参数值（业务数据）一律不出现
        assert "[SQL:" not in text
        assert "SECRET_TABLE" not in text
        assert "ACME-机密客户" not in text

    def test_plain_error_message_kept(self) -> None:
        assert _userFacingErrorText(RuntimeError("连接超时")) == "连接超时"

    def test_custom_message_attribute_preferred(self) -> None:
        class _Err(Exception):
            message = "自定义原因"

        assert _userFacingErrorText(_Err("ignored")) == "自定义原因"

    def test_marker_only_text_falls_back(self) -> None:
        # 剥完为空时不能给用户一个空错误（要有可读兜底）
        assert _userFacingErrorText(RuntimeError("[SQL: SELECT 1]")) == "执行失败（详见服务端日志）"


class TestStepFailedError:
    def test_prefixes_redacted_text(self) -> None:
        text = _stepFailedError(_sqlalchemyErr(), "该步骤执行失败：")

        assert text.startswith("该步骤执行失败：")
        assert "ORA-00942" in text
        assert "SECRET_TABLE" not in text

    def test_truncates_overlong_text(self) -> None:
        text = _stepFailedError(RuntimeError("x" * 500), "该步骤执行失败：")

        assert text == "该步骤执行失败：" + "x" * 200 + "..."


class TestRetryGenTokens:
    def test_absent_returns_zeros(self) -> None:
        assert _retryGenTokens(RuntimeError("boom")) == (0, 0)

    def test_attached_tokens_round_trip_without_changing_error_semantics(self) -> None:
        exc = RuntimeError("boom")

        _attachRetryGenTokens(exc, (12, 34))

        assert _retryGenTokens(exc) == (12, 34)
        # 类型与消息不变：API 层按异常类型映射 HTTP 状态，改类型会连带改变对外契约
        assert type(exc) is RuntimeError
        assert str(exc) == "boom"
