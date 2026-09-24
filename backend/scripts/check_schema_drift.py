"""向后兼容 shim —— 实现已搬到 `app/infrastructure/schema_drift.py`。

保留本文件仅为不打断既有的命令行用法、CI 脚本与文档引用：

    cd backend && python scripts/check_schema_drift.py --database-url $DATABASE_URL

**运行时（`app/main.py` 的 lifespan）不再走这里**，它直接 import
`app.infrastructure.schema_drift`。原因：本模块与 `main.py` 是一对必须同版本部署的
两半，放在 `scripts/` 时 `docker cp backend/app/.` 只会带上 main.py，容器里留下旧
实现 —— 实测两个错配方向都会让容器起不来。搬进 `app/` 后一次 cp 即带全，
详见 `app/infrastructure/schema_drift.py` 的「为什么住在 app/infrastructure/」。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 以 `python scripts/check_schema_drift.py` 直接执行时 sys.path[0] 是 scripts/，
# 够不到 app 包（脚本执行的 cwd 不进 sys.path），故显式补上 backend 根目录。
_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.infrastructure.schema_drift import (  # noqa: E402
    _SEVERITY_BY_CODE,
    _checkDriftAsync,
    _splitBySeverity,
    main,
)

__all__ = ["_SEVERITY_BY_CODE", "_checkDriftAsync", "_splitBySeverity", "main"]


if __name__ == "__main__":
    sys.exit(main())
