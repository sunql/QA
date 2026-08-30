"""数据质量评估器（Phase 1.2）：5 维校验独立模块。

各 evaluator 模块可直接 import；dispatcher 由 `app.services.data_quality_evaluator`
显式 import，避免与本 __init__ 形成循环依赖。
"""

__all__: list[str] = []