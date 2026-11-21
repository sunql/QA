"""0164：文档关联的 entity_type 允许「知识对象」，并补去重唯一索引。

背景（2026-11-22 用户反馈）：一份制度/合同往往关联的是**本体类/指标/属性**，
而不是某个业务单据实例。但原表的 ``entity_type`` 上有外键指向
``business_object(code)`` ⇒ 插入 ``ONTOLOGY_CLASS`` 直接违反外键。

同时发现：``createRelation`` 把**所有** IntegrityError 都映射成「关联已存在」，
于是这个外键冲突被报成 409「已存在」——比真实原因更难排查。

变更：
1. 删除 ``fk_document_entity_relation_entity_type``。合法性改由应用层校验
   （``schemas._validateRelationEntityType``：业务对象码过注册表，知识对象走白名单）；
2. 补 ``uq_doc_rel_document_entity`` 唯一索引 —— 原表的「查重」是
   SELECT-then-INSERT，**没有唯一约束兜底**，并发下会产生重复关联；
   有唯一索引后，冲突才真的是「已存在」。

Revision ID: 0164
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0164"
down_revision = "0163"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "fk_document_entity_relation_entity_type",
        "document_entity_relation",
        type_="foreignkey",
    )
    # 先清历史重复（保留最早一条），否则唯一索引建不起来。
    op.execute(
        """
        DELETE FROM document_entity_relation a
        USING document_entity_relation b
        WHERE a.id > b.id
          AND a.document_id = b.document_id
          AND a.entity_type = b.entity_type
          AND a.entity_key = b.entity_key
          AND a.relation_type = b.relation_type
        """
    )
    op.create_index(
        "uq_doc_rel_document_entity",
        "document_entity_relation",
        ["document_id", "entity_type", "entity_key", "relation_type"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_doc_rel_document_entity", table_name="document_entity_relation"
    )
    # 回滚前必须清掉知识对象类型的行（外键无法接受 ONTOLOGY_*）
    op.execute(
        """
        DELETE FROM document_entity_relation
        WHERE entity_type LIKE 'ONTOLOGY_%'
        """
    )
    op.create_foreign_key(
        "fk_document_entity_relation_entity_type",
        "document_entity_relation",
        "business_object",
        ["entity_type"],
        ["code"],
        ondelete="RESTRICT",
    )
