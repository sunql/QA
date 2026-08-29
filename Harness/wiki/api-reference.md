# API 参考

基址：`/api/v1`。JSON 字段为 camelCase（如 `modelName`、`sessionId`、`tokenUsage`）。

## 系统
| Method | Path | 说明 |
|--------|------|------|
| GET | /health | 健康检查 |

## 模型配置
| Method | Path | 说明 |
|--------|------|------|
| GET | /models | 列出（`?activeOnly=true`） |
| POST | /models | 创建（含明文 apiKey，服务端加密） |
| GET | /models/{id} | 获取 |
| PUT | /models/{id} | 更新 |
| DELETE | /models/{id} | 停用（软删除） |

## 会话
| Method | Path | 说明 |
|--------|------|------|
| GET | /sessions/{sessionId}/usage | 聚合摘要（请求数/Token/成本/按模型） |
| GET | /sessions/{sessionId}/usage/list | 明细流水 |

## 后续 Phase
| Method | Path | Phase |
|--------|------|-------|
| POST | /chat | 4 |
| POST | /knowledge/{define,map,metric} | 2 |
| GET/POST | /ontology/{classes,properties,metrics} | 2 |
| GET | /ontology/search | 2 |
| GET/POST | /datasource | 3 |
| POST | /datasource/{id}/test | 3 |
| POST | /datasource/{id}/query | 3 |

## 错误响应

统一 `ErrorResponse`：`{ success: false, error: <message>, detail: <optional> }`。

| 异常 | HTTP |
|------|------|
| NotFoundError | 404 |
| ValidationError | 422 |
| 其他 DomainError | 400 |
