"""P4 知识服务的中文提示语（集中一处，避免各路由各写一份同义句）。

仓内约定：面向用户的提示语放 `app/services/messages_zh.py`；本模块只放
**知识服务内部**的路由级提示（不进领域异常包络，只进响应体的 warnings /
degradedReason）。
"""

from __future__ import annotations

#: 发布面不可用（未发布 / 钉住的版本已删 / 完整性校验失败）。
MSG_RELEASE_UNAVAILABLE = "语义发布不可用（未发布 / 已删除 / 完整性校验失败）"

#: 钉住的语义发布**不可用**（不存在 / 域不匹配 / 状态不可见 / 完整性不过）。
#: ⚠️ **统一口径**：这几种失败必须给**同一句**文案 ⇒ 否则「不存在」与「存在但无权」
#: 的差别就是一个**存在性 oracle**（可二分枚举 release id）。细节只进服务端日志。
MSG_PINNED_RELEASE_UNAVAILABLE = "钉住的语义发布不可用（不存在、域不匹配或状态不可见）"

#: 请求的域对当前用户不可见（或不存在）⇒ 空结果 + 警告（**不是 403**）。
MSG_DOMAIN_NOT_VISIBLE = "域不存在或当前用户无权查看"

#: 通道尚未实现（D32 骨架期占位）。
MSG_CHANNEL_PENDING = "该通道尚未实现"

#: 检索命中但被上限截断。
MSG_TRUNCATED = "命中条数超过上限，已按得分截断"

#: RAG 能力开关未启用（P3a 的 `ENABLE_SEMANTIC_REGISTRY_RAG` 默认关）。
#: 本路**显式声明了作用域** ⇒ 按 P3a 的 fail-closed 口径**不许回落成不过滤的召回**
#: ⇒ 降级（而不是「悄悄给一份没过滤的答案」）。
MSG_RAG_DISABLED = "语义注册表 RAG 能力未启用，本路按权限拒绝策略降级"

#: 查询向量生成失败（embedding 客户端 / 模型不可用）。
MSG_RAG_EMBEDDING_UNAVAILABLE = "查询向量生成失败，RAG 路降级"

#: 向量召回失败（Milvus 不可用等）。
MSG_RAG_SEARCH_UNAVAILABLE = "向量召回失败，RAG 路降级"

#: 发布快照的物理 schema 复原 / 渲染失败。
MSG_SQL_RENDER_FAILED = "发布快照的物理 schema 渲染失败，SQL 路降级"

#: 请求的域不在当前用户的可见域集合内（路由层的兜底，正常由编排层先拦）。
MSG_SCOPE_NOT_VISIBLE = "请求的域不在当前用户可见范围内"

#: 编排层兜底：某一路抛了未预期异常 ⇒ 该路降级（**不许带崩整轮查询**）。
MSG_ROUTE_FAILED = "该路召回异常，本次降级"

#: 请求侧把业务对象收窄到了空集（要的那个业务对象在可见范围内不存在）
#: ⇒ 本次**只返回空结果**（**不许**回落成「全都给」）。
MSG_SCOPE_ENTITY_TYPES_EMPTY = "请求收窄的业务对象在可见范围内为空，本次只返回空结果"

#: 请求显式传了 `maxSecurityLevel`，但本批**没有**可据以过滤的文档维度
#: ⇒ 明确告知该维度被忽略（不许「传了却当没传」）。
MSG_SCOPE_SECURITY_LEVEL_IGNORED = "maxSecurityLevel 本批未生效（用户级安全等级无权威来源），该维度被忽略"
