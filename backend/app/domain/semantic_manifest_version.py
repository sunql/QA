"""Manifest Schema 的**版本字面量**（无依赖叶子模块）。

为什么独立成一个文件：`semantic_release.schema_version` 的库侧默认值要用
`SCHEMA_VERSION`，而 ORM 聚合模块（`app/domain/models.py`）在文件末尾 import
本域各子模型 ⇒ 子模型一旦 import 契约模块（`app/domain/semantic_manifest_schema.py`），
就会把 `app.domain.schemas`（度量聚合白名单）→ `app.services.business_object_registry`
→ `app.domain.models` 这条**回到自身的 import 环**拉进来，只因导入顺序侥幸成立。
本模块**零 import** ⇒ 环被切断，ORM 侧与契约侧共用同一份版本真值。

⚠️ **版本字面量必须冻结**（code-review HIGH-1）：契约按版本分发
（`MANIFEST_SCHEMAS`），历史版本的 Schema 与它的 `schemaVersion` 常量
**不得**跟着「当前版本」走 —— 否则下一次升版会把上一版从注册表里抹掉，
已发布的旧快照当场变成「未知版本」⇒ 无法再校验 / 激活 / 回放。
"""

from __future__ import annotations

SCHEMA_VERSION_V1 = "v1"
SCHEMA_VERSION_V2 = "v2"

#: **当前**契约版本。新增字段 / 新增枚举 / 改类型 ⇒ 这里 +1，
#: 并在 `semantic_manifest_schema.py` 里新增对应版本的 Schema 并登记进
#: `MANIFEST_SCHEMAS`（漏登记会在 import 期直接炸，不会等到发版时才发现）。
SCHEMA_VERSION = SCHEMA_VERSION_V2
