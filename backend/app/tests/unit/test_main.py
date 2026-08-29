"""main.py 应用级单元测试。

覆盖：
- shutdownCleanup：各清理步骤错误隔离——任一失败不阻断后续清理
"""

from __future__ import annotations

import app.api.v1.chat as chat_module
import app.infrastructure.milvus_client as milvus_client
from app import main as main_module


class _BoomEmbeddingService:
    """close() 抛异常，模拟 embedding 连接清理失败。"""

    async def close(self) -> None:
        raise RuntimeError("embedding close failed")


class TestShutdownCleanup:
    async def test_each_step_isolated_from_failure(self, monkeypatch) -> None:
        """清理步骤逐段隔离：前面的失败不影响后面的步骤执行。"""
        calls: list[str] = []

        # 第 1 步失败 → 第 2、3 步仍须执行
        class _Embedding:
            async def close(self) -> None:
                calls.append("embedding")
                raise RuntimeError("embedding close failed")

        monkeypatch.setattr(chat_module, "_embeddingService", _Embedding())

        def _closeConnection() -> None:
            calls.append("milvus")

        monkeypatch.setattr(milvus_client, "closeConnection", _closeConnection)

        async def _disposeEngine() -> None:
            calls.append("engine")

        monkeypatch.setattr(main_module, "disposeEngine", _disposeEngine)

        # 不应抛出异常（每个步骤的失败被隔离记录）
        await main_module.shutdownCleanup()

        assert calls == ["embedding", "milvus", "engine"]

    async def test_middle_failure_does_not_skip_later_steps(self, monkeypatch) -> None:
        """中间步骤失败也不应跳过 disposeEngine。"""
        calls: list[str] = []

        class _Embedding:
            async def close(self) -> None:
                calls.append("embedding")

        monkeypatch.setattr(chat_module, "_embeddingService", _Embedding())

        def _closeConnection() -> None:
            calls.append("milvus")
            raise RuntimeError("milvus close failed")

        monkeypatch.setattr(milvus_client, "closeConnection", _closeConnection)

        async def _disposeEngine() -> None:
            calls.append("engine")

        monkeypatch.setattr(main_module, "disposeEngine", _disposeEngine)

        await main_module.shutdownCleanup()

        assert calls == ["embedding", "milvus", "engine"]
