# B1 Evidence Extension Design（架构升级 v3.1 · 乙任务 B1）

- **日期**：2026-09-28
- **作者**：Claude（乙）
- **关联**：[Harness/changes/2026-09-28-arch-upgrade-v31/plan-person-b.md §B1](../Harness/changes/2026-09-28-arch-upgrade-v31/plan-person-b.md)
- **蓝图**：`docs/系统架构优化思路0928-v3.1.md` §M1' MVP
- **状态**：approved（用户确认）
- **范围**：仅 B1（W1-W2, 4 天），不跨入 B2-B6

---

## 1. 目标

复用现有 `evidence` 表，扩展两种 MVP Evidence 类型（`SQL_QUERY` / `METRIC_RESULT`），保留 Document 旧型 5 元组字段不变；提供 `/evidences` API 供前端 ChatPage 证据展开与管理员审计使用。

不目标（B1 内不做）：
- 业务 SQL 自动落库（B2 范围）
- ChatPage 前端证据展示卡片（B3 范围）
- Confidence 派生（B4 范围）
- 引入 outbox_service（B2 才需要）

## 2. 数据模型变更

### Alembic 0096

迁移文件名：`backend/alembic/versions/0096_evidence_payload.py`

```python
"""evidence payload + session_id

Revision ID: 0096
Revises: 0095
Create Date: 2026-09-28
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0096"
down_revision = "0095"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "evidence",
        sa.Column("payload", JSONB, nullable=True),
    )
    op.add_column(
        "evidence",
        sa.Column("session_id", sa.String(64), nullable=True),
    )
    op.create_index(
        "ix_evidence_session",
        "evidence",
        ["session_id"],
        postgresql_where=sa.text("session_id IS NOT NULL"),
    )
    # 不加 claim_id 索引（已有 ix_evidence_claim）


def downgrade() -> None:
    op.drop_index("ix_evidence_session", table_name="evidence")
    op.drop_column("evidence", "session_id")
    op.drop_column("evidence", "payload")
```

约束：
- `payload` nullable，旧 Document 类型行 `payload IS NULL`
- `session_id` nullable + 部分索引（仅非空行入索引，省空间；session_id 在 chat session 场景才用）
- 不动 source_type 长度 / 默认值 / 旧 5 元组字段（向后兼容）

### ORM（`backend/app/domain/wiki_models.py`）

```python
class Evidence(Base):
    __tablename__ = "evidence"
    __table_args__ = (
        Index("ix_evidence_claim", "claim_id"),
        Index("ix_evidence_session", "session_id",
              postgresql_where=text("session_id IS NOT NULL")),
    )

    # 旧字段保留不变
    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    claim_id: Mapped[int] = mapped_column(BigIntFk,
        ForeignKey("knowledge_claim.id", ondelete="CASCADE"), nullable=False)
    source_type: Mapped[str] = mapped_column(String(30), nullable=False)
    source_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    page_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    section_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    paragraph_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4), nullable=True)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow)

    # 新增（M1'）
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    claim: Mapped[KnowledgeClaim] = relationship(back_populates="evidences")
```

### source_type 取值矩阵

| source_type | payload schema | 旧 5 元组 | 写入路径 |
|---|---|---|---|
| `DOCUMENT` / 文档目录 / 工单 / 邮件（不变） | NULL | 必填 | claim_extractor / wiki 导入（B1 **冻结**） |
| `SQL_QUERY` | `{sql, params, result_hash, row_count, execution_time_ms, datasource_id}` | NULL | B2 才接 |
| `METRIC_RESULT` | `{metric_code, period, value, calc_time}` | NULL | B2+ 才接 |

注：B1 仅写入能力（手测 / 测试 fixture），不接业务写入路径。

## 3. Pydantic Schema（`backend/app/domain/wiki_schemas.py`）

**决策：扩展现有 `EvidenceRead`，不新增 `EvidenceOut`。** `wiki_schemas.py:285` 已有 `EvidenceRead`，但漏掉 ORM 的 `confidence` / `content_hash` 字段，且缺本次新增的 `payload` / `session_id`。在原 schema 上就地补齐，避免重名类与读模型分裂。

```python
class EvidenceRead(CamelModel):
    """证据出处读模型（扩展自 v3.1 M1'）。"""

    id: int
    claim_id: int
    source_type: str
    source_id: str | None = None
    page_number: int | None = None
    section_name: str | None = None
    paragraph_no: int | None = None
    content: str | None = None
    # 补齐 ORM 漏字段
    content_hash: str | None = None
    confidence: Decimal | None = None
    # v3.1 新增（M1'）
    payload: dict | None = None
    session_id: str | None = None
    created_time: datetime | None = None


class EvidenceListOut(CamelModel):
    """证据列表响应（含分页 total）。"""

    items: list[EvidenceRead] = Field(default_factory=list)
    total: int


class EvidenceQuery(BaseModel):
    """证据查询参数（防 KPI 空关键词 substring "" 副作用）。"""

    session_id: str | None = Field(None, max_length=64)
    claim_id: int | None = Field(None, ge=1)
    source_type: Literal["DOCUMENT", "SQL_QUERY", "METRIC_RESULT"] | None = None
    limit: int = Field(50, ge=1, le=200)
    offset: int = Field(0, ge=0)

    @field_validator("session_id")
    @classmethod
    def _strip_session_id(cls, v: str | None) -> str | None:
        if v is None:
            return None
        stripped = v.strip()
        if not stripped:
            raise ValueError("session_id must not be blank")
        return stripped
```

> 注：KnowledgeClaimRead.evidences 字段类型保留为 `list[EvidenceRead]`，扩展后自动获得新字段（from_attributes 兼容）。

## 4. API（`backend/app/api/v1/evidences.py` 新文件）

```
GET   /api/v1/evidences                       # 列表 + 过滤
GET   /api/v1/evidences/{evidence_id}         # 详情
GET   /api/v1/evidences/by-session/{sid}      # 便捷端点：按 session 查所有证据
                                                # ⚠️ 必须在 /{evidence_id} 路由前注册
                                                # （wiki search endpoint 教训）
```

> 注意：Person A 的 M0-P0.4 写路径将来会调用本接口落 SQL_QUERY Evidence（B2 实施）。当前 B1 只暴露**只读**端点。

实现要点：
- 路由注册顺序：`/by-session/{sid}` 在 `/{evidence_id}` 之前
- 过滤组装：query params → EvidenceQuery → SQLAlchemy select
- source_type / session_id / claim_id 任意组合 AND
- 默认 limit=50, offset=0；前端分页可调

## 5. 测试策略

### 单元测试（`backend/app/tests/unit/test_evidence_schema.py`）
- EvidenceQuery 校验：blank session_id 抛 ValueError
- limit 范围（1-200）、offset 非负
- source_type 字面量校验
- EvidenceOut.from_attributes 兼容 Evidence ORM（含 Decimal→str 序列化）

### 集成测试（`backend/app/tests/integration/test_evidence_api.py`）
真实 `qa_metadata_test`（CLAUDE.md 恒空测试库）：
- 写 fixture：claim（KnowledgeClaim）+ 3 条 Evidence（DOCUMENT/SQL_QUERY/METRIC_RESULT）
- GET /evidences?session_id=X → 仅返回该 session 的 SQL_QUERY 行
- GET /evidences?claim_id=Y&source_type=SQL_QUERY → 精确过滤
- GET /evidences/by-session/{sid} → 路径参数生效
- GET /evidences/{id} 命中整数 ID；非整数 422
- 路由顺序：by-session 不被 /{evidence_id} 遮蔽
- Document 型 evidence 行为零变化（旧测试集保留不退）

### 测试隔离红线
- **不与 unit 同进程混跑**（TRUNCATE 抹 ontology_class 教训）
- alembic upgrade head 必须显式在 integration fixture 里跑（不留未迁移 schema）
- 使用 `qa_metadata_test` 而非 prod qa_metadata（两库使用策略）

## 6. 风险与缓解

| 风险 | 概率 | 缓解 |
|---|---|---|
| 0096 号段冲突 | 极低 | README §2 已划清：甲 0095/0097、乙 0096/0098；先合者占号，仍重 |
| Person A 在 `EvidenceRead` 上同时改字段 | 中 | 提前与甲对一次（merge 日前 1 天），告知本任务会补 confidence/content_hash/payload/session_id |
| 现有 Document 写入路径踩到 payload 字段 | 低 | payload nullable + ORM 默认 None；不动 claim_extractor |
| JSONB 字段 Alembic autogen 漏列 | 中 | 手写 upgrade/downgrade，不依赖 autogenerate |
| session_id 字符串太长拖性能 | 低 | String(64) 截断 + 部分索引 |

## 7. 不在本任务范围

- 业务 SQL 执行钩子（B2）
- ChatPage 前端证据卡片（B3）
- Evidence LLM 评分 / 冲突前置（B4）
- Alembic 0098（claim confidence_level，B4）

## 8. 验收

- [ ] `alembic upgrade head` 在 prod qa_metadata 与 test qa_metadata_test 都成功
- [ ] `/evidences` 三个端点返回结构正确（curl 手动验证）
- [ ] 单元测试覆盖率 ≥ 80%
- [ ] 集成测试全绿（real PG）
- [ ] Document 旧路径回归测试不退
- [ ] PR 合入 `epic/v31-upgrade`（不在 main）