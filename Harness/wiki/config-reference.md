# 配置参考

所有配置通过环境变量注入（`docker/.env`），由 `app/config.py::Settings` 加载。

## 基础设施
| 变量 | 默认 | 说明 |
|------|------|------|
| DATABASE_URL | postgresql+asyncpg://... | 元数据库 |
| NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD | bolt://... | 本体图 |
| MILVUS_URI / MILVUS_COLLECTION | http://...:19530 | 向量库 |
| SECRET_KEY | - | Fernet 密钥，加密 API Key/数据源密码。**必填且须为合法 Fernet key**；代码默认值 `development-insecure-key-change-me` 非法（33 字符）。本地/测试用 `docker/.env` 的同一密钥（`export SECRET_KEY="$(grep '^SECRET_KEY=' ../docker/.env \| cut -d= -f2)"`），换密钥会让已加密的 `data_source.password_encrypted` 解不开 |
| SESSION_BUDGET | 0.1 | 会话累计成本上限（美元） |
| DATASOURCE_HOST_ALLOWLIST | (空) | 数据源主机白名单，逗号分隔 |
| QUERY_ROW_LIMIT | 5000 | 查询行数上限 |
| QUERY_TIMEOUT_SECONDS | 30 | 查询超时 |
| NL2SQL_NO_SCOPE_ROW_LIMIT | 100 | 无范围明细查询兜底行数（0=关闭兜底注入；详见 changes/feat-scope-aware-row-limit） |

## LLM
| 变量 | 说明 |
|------|------|
| OPENAI_API_KEY / OPENAI_BASE_URL | OpenAI |
| AZURE_OPENAI_API_KEY / AZURE_OPENAI_ENDPOINT / AZURE_OPENAI_API_VERSION | Azure |
| DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL | DeepSeek |
| QWEN_API_KEY / QWEN_BASE_URL | 通义千问 |
| OLLAMA_BASE_URL | 本地 Ollama |

## Embedding（语义检索）
| 变量 | 默认 | 说明 |
|------|------|------|
| EMBEDDING_MODEL | text-embedding-3-small | embedding 模型名 |
| EMBEDDING_API_BASE | (空) | embedding 端点；空则回退 OPENAI_BASE_URL |
| EMBEDDING_API_KEY | (空) | embedding key；空则回退 OPENAI_API_KEY |

## 路由
| 变量 | 默认 | 说明 |
|------|------|------|
| SESSION_AFFINITY_TURNS | 3 | 会话亲和轮数 |
| NL2SQL_MAX_RETRIES | 2 | NL2SQL 重试次数 |

## 生成 Fernet 密钥

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## 运行环境
| 变量 | 说明 |
|------|------|
| APP_ENV | development / test / production |
| LOG_LEVEL | DEBUG / INFO / WARNING / ERROR |
