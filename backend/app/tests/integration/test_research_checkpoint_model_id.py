"""spec N4 - Research checkpoint 接受 model_id 字段，续跑用新模型。"""
import pytest

from app.domain.research_schemas import CheckpointAnswerRequest


class TestCheckpointAnswerRequestModelId:
    def test_accepts_model_id_camelcase(self) -> None:
        """payload 含 modelId 字段应被接受（Field alias 生效）。"""
        req = CheckpointAnswerRequest.model_validate({
            "action": "confirm",
            "choice": {},
            "modelId": 2,
        })
        assert req.model_id == 2

    def test_model_id_optional(self) -> None:
        """不传 modelId 也合法（保持向后兼容）。"""
        req = CheckpointAnswerRequest.model_validate({
            "action": "confirm",
            "choice": {},
        })
        assert req.model_id is None

    def test_model_id_must_be_positive_int(self) -> None:
        """modelId <= 0 应被 Pydantic 拒。"""
        with pytest.raises(Exception):
            CheckpointAnswerRequest.model_validate({
                "action": "confirm",
                "choice": {},
                "modelId": 0,
            })

    def test_extra_fields_forbidden(self) -> None:
        """unknown 字段被 extra=forbid 拒。

        注：snake_case ``model_id`` 因 CamelModel ``populate_by_name=True`` 被接受
        （Python 字段名直接走 populate_by_name）。故改用真正未声明字段 ``bogusField``
        验证 ``extra=forbid`` 行为。
        """
        with pytest.raises(Exception):
            CheckpointAnswerRequest.model_validate({
                "action": "confirm",
                "choice": {},
                "bogusField": 1,
            })