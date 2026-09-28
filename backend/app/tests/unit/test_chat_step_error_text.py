"""步骤错误文案脱敏 + 重试 token/二次失败携带的纯函数单元测试（不依赖 DB）。

对应 C3/C4 收尾复审的两条 MEDIUM：
1. `StepResult.error` 会进非流式响应与流式 `step_result` 事件（前端直接渲染），
   而 DB 驱动异常经 SQLAlchemy 包装后 `str()` 含 `[SQL: ...]`（内部表/列名）与
   `[parameters: ...]`（查询字面量）⇒ 用户可见文案必须剥掉这两段。
2. 重试生成成功、重试执行失败时，那次生成的 token 随异常交回调用方记账。

M7 补充：回灌重试的**二次失败**（重试生成失败 / 重试执行仍失败）此前只进服务端
日志，用户可见文案只报首次错误 —— 同一处文案必须同时给出两段原因，且两段都要脱敏。
"""

from __future__ import annotations

from sqlalchemy.exc import ProgrammingError

from app.services.chat_service import (
    _attachRetryFailure,
    _attachRetryGenTokens,
    _retryFailure,
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


def _retryErr() -> ProgrammingError:
    """重试那一次的语句异常（与首次可区分：ORA 码不同）。"""
    return ProgrammingError(
        "SELECT OTHER_COL FROM APP.OTHER_SECRET WHERE N=:n",
        {"n": "ACME-另一个机密"},
        RuntimeError("ORA-00904: 标识符无效"),
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


class TestRetryFailureDetails:
    """M7：二次失败（重试生成失败 / 重试执行仍失败）的携带与提取。"""

    def test_absent_returns_none(self) -> None:
        assert _retryFailure(RuntimeError("boom")) is None

    def test_attached_details_round_trip_without_changing_error_semantics(self) -> None:
        exc = RuntimeError("boom")
        inner = ValueError("重试也没救回来")

        _attachRetryFailure(exc, "后仍执行失败", inner)

        details = _retryFailure(exc)
        assert details is not None
        assert details.stageLabel == "后仍执行失败"
        assert details.error is inner
        # 类型与消息不变（同上：异常类型承载对外状态映射）
        assert type(exc) is RuntimeError
        assert str(exc) == "boom"


class TestStepFailedErrorWithRetryFailure:
    """M7：步骤文案必须同时给出首次与重试两段原因，两段都脱敏。"""

    def test_carries_both_failures_and_redacts_both(self) -> None:
        exc = _sqlalchemyErr()

        _attachRetryFailure(exc, "后仍执行失败", _retryErr())

        text = _stepFailedError(exc, "该步骤执行失败：")
        assert text.startswith("该步骤执行失败：首次：")
        assert "ORA-00942" in text  # 首次原因
        assert "ORA-00904" in text  # 重试原因（此前整段丢失）
        assert "重试后仍执行失败：" in text
        for leaked in (
            "SELECT SECRET_COL", "SECRET_TABLE", "ACME-机密客户",
            "OTHER_SECRET", "ACME-另一个机密", "[SQL:", "[parameters:",
        ):
            assert leaked not in text, f"用户可见文案泄漏了 {leaked}：{text}"

    def test_gen_stage_label_wording(self) -> None:
        exc = RuntimeError("首次原因")

        _attachRetryFailure(exc, "生成失败", RuntimeError("重试原因"))

        assert _stepFailedError(exc, "该步骤执行失败：") == (
            "该步骤执行失败：首次：首次原因；重试生成失败：重试原因"
        )

    def test_without_retry_failure_text_is_unchanged(self) -> None:
        """向后兼容：未触发重试 / 重试成功 → 文案仍是「前缀 + 首次原因」。"""
        assert _stepFailedError(RuntimeError("连接超时"), "该步骤执行失败：") == (
            "该步骤执行失败：连接超时"
        )

    def test_two_failures_both_redacted_fall_back_independently(self) -> None:
        """两段各自脱敏：某段剥完为空时用自己的兜底文案，不影响另一段。"""
        exc = RuntimeError("首次原因")

        _attachRetryFailure(exc, "后仍执行失败", RuntimeError("[SQL: SELECT 1]"))

        assert _stepFailedError(exc, "该步骤执行失败：") == (
            "该步骤执行失败：首次：首次原因；重试后仍执行失败：执行失败（详见服务端日志）"
        )

    def test_truncation_still_bounds_combined_text(self) -> None:
        exc = RuntimeError("x" * 500)

        _attachRetryFailure(exc, "后仍执行失败", RuntimeError("y" * 500))

        text = _stepFailedError(exc, "该步骤执行失败：")
        prefix = "该步骤执行失败："
        # 硬上限仍在（前缀之外不超过 _STEP_FAILED_ERROR_LIMIT）
        assert len(text) <= len(prefix) + 200
        assert text.endswith("...")

    def test_long_first_cause_does_not_crowd_out_retry_cause(self) -> None:
        """首次原因很长时，重试原因不能被整体挤掉。

        整段尾部截断会让「重试为什么也没救回来」（M7 要暴露的信息）被首次原因吃掉；
        两段各自限量才能保证它一定出现。
        """
        exc = RuntimeError("x" * 500)

        _attachRetryFailure(exc, "后仍执行失败", RuntimeError("重试的病根"))

        text = _stepFailedError(exc, "该步骤执行失败：")
        assert text.endswith("；重试后仍执行失败：重试的病根"), text
        assert "首次：xxx" in text
