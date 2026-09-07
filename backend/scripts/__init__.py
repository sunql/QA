"""scripts 包：一次性运维/种子脚本。

每个脚本保持自包含（standalone 可运行），通过 `sys.path.insert` 引入 backend 根。
加 __init__.py 使脚本中的可测函数（如 seedEntityMappings）可被集成测试导入。
"""
from scripts.seed_business_objects import seedBusinessObjects

__all__ = ["seedBusinessObjects"]
