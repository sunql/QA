"""预置 LLM 模型种子数据：MiniMAX + DeepSeek + oMLX 本地 Qwen3.8-27B。"""
from __future__ import annotations

from decimal import Decimal
from sqlalchemy import text
from app.infrastructure.database import getEngine, getSessionFactory
from app.domain.models import LlmConfig
from app.infrastructure.security.crypto import encryptApiKey
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


MODELS = [
    {
        "model_name": "deepseek-chat",
        "provider": "openai_compatible_proxy",
        "api_endpoint": "https://api.deepseek.com/v1",
        "api_key": "",  # 用户自行填入
        "cost_per_1k_input": Decimal("0.0014"),
        "cost_per_1k_output": Decimal("0.0028"),
        "max_input_tokens": 64000,
        "weight": 10,
        "cost_threshold": Decimal("0.05"),
        "is_active": True,
        "remark": "DeepSeek V3 主力模型，性价比高",
    },
    {
        "model_name": "MiniMax-Text-01",
        "provider": "openai_compatible_proxy",
        "api_endpoint": "https://api.minimax.chat/v1",
        "api_key": "",  # 用户自行填入
        "cost_per_1k_input": Decimal("0.001"),
        "cost_per_1k_output": Decimal("0.005"),
        "max_input_tokens": 32000,
        "weight": 8,
        "cost_threshold": Decimal("0.05"),
        "is_active": True,
        "remark": "MiniMAX 海螺模型，低延迟",
    },
    {
        # 本机 oMLX 起的 Qwen3 MoE 模型（30B-A3B，4bit 量化）。
        # 端口 8888（非 seed 默认 8080）且开启鉴权，调用需 Bearer key：
        # 真实 key 在 ~/.omlx/settings.json 的 auth.api_key；激活前须经
        # PUT /api/v1/models/{id} 写入 apiKey，否则请求 401。
        # 本地推理边际成本≈0，路由权重拉高便于优先使用本地。
        "model_name": "Qwen3.8-27B-4bit",
        "provider": "openai_compatible_proxy",
        "api_endpoint": "http://localhost:8888/v1",
        "api_key": "",  # 用户经 API 写入；空串避免 dev SECRET_KEY 非法 Fernet 抛错
        "cost_per_1k_input": Decimal("0"),
        "cost_per_1k_output": Decimal("0"),
        "max_input_tokens": 32768,  # oMLX settings.json sampling.max_tokens=32768
        "weight": 12,
        "cost_threshold": Decimal("0.05"),
        "is_active": True,
        "remark": "oMLX 本地 Qwen3.8-27B-4bit (MoE, 4bit)，端口 8888 需鉴权",
    },
]


async def seed() -> None:
    engine = getEngine()
    factory = getSessionFactory()

    async with factory() as session:
        for m in MODELS:
            # 检查是否已存在
            result = await session.execute(
                text("SELECT id FROM llm_config WHERE model_name = :name"),
                {"name": m["model_name"]},
            )
            if result.fetchone():
                logger.info("模型已存在，跳过: %s", m["model_name"])
                continue

            encrypted_key = encryptApiKey(m["api_key"]) if m["api_key"] else None
            config = LlmConfig(
                model_name=m["model_name"],
                provider=m["provider"],
                api_endpoint=m["api_endpoint"],
                api_key_encrypted=encrypted_key,
                cost_per_1k_input=m["cost_per_1k_input"],
                cost_per_1k_output=m["cost_per_1k_output"],
                max_input_tokens=m["max_input_tokens"],
                weight=m["weight"],
                cost_threshold=m["cost_threshold"],
                is_active=m["is_active"],
            )
            session.add(config)
            logger.info("新增模型: %s", m["model_name"])

        await session.commit()

    logger.info("种子数据导入完成")


if __name__ == "__main__":
    import asyncio
    asyncio.run(seed())
