"""Wiki 学习机制的支撑表（feat-wiki-knowledge，Phase 8 M2）。

与 ``wiki_models.py`` 分离的原因：后者是「知识条目本体」（4 张核心表），
本模块是「驱动本体生长的管线表」（导入任务 + LLM 计量 + 反馈流 + 冲突/建议），
生命周期与变更节奏都不同。M5 的机制 3/4 产出（``KnowledgeConflict`` /
``StructureSuggestion``）落在本文件；M7 的覆盖度表将另起同级文件，把这个文件
的膨胀控制住。

设计要点：

- ``wiki_token_usage`` **刻意不复用** ``session_token_usage``：导入/分类
  没有 chat session，硬塞一个假 sessionId 会污染会话维度的成本报表。
- ``wiki_import_task.selected_model_id`` 记录用户当次选的模型，用于审计
  「这条知识是哪个模型处理的」；实际生效模型也会回写到
  ``wiki_page.processing_model_id``（fallback 命中时两者不一致，是刻意保留
  的信息——能看出降级发生过）。
- ``learning_feedback``（M3）是**反馈事件流**，append-only；与
  ``wiki_token_usage`` 一样属于「管线表」而非「知识本体表」。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy import (
    text as saText,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.models import Base, BigIntFk, BigIntPk, _utcnow
from app.domain.wiki_models import JsonColumn

# 导入任务类型
IMPORT_TASK_TYPES: tuple[str, ...] = (
    "SINGLE_CREATE",   # 单条创建（走 LLM 分类）
    "BULK_IMPORT",     # 批量导入
    "RECLASSIFY",      # 重新分类
)

# 导入任务状态机：PENDING → RUNNING → SUCCEEDED / PARTIAL / FAILED
IMPORT_TASK_STATUSES: tuple[str, ...] = (
    "PENDING",
    "RUNNING",
    "SUCCEEDED",
    "PARTIAL",
    "FAILED",
)

# 导入来源类型。当前 M2 只实现 MARKDOWN（preview 接口直接收原文），
# PDF/WORD/CSV/API 是 M2 之后解析器的预留位——但**预留不等于放行**：
# 白名单外的值一律 422，避免库里出现拼错的来源类型无法聚合。
IMPORT_SOURCE_TYPES: tuple[str, ...] = (
    "MARKDOWN",
    "PDF",
    "WORD",
    "CSV",
    "API",
)

# 计量来源机制（与 learning 包下的各 service 一一对应）
LEARNING_MECHANISMS: tuple[str, ...] = (
    "CLASSIFY",    # 机制 1 自动分类
    "RELATE",      # 机制 2 关系发现
    "CONFLICT",    # 机制 3 冲突检测
    "STRUCTURE",   # 机制 4 结构化建议
)

# 冲突类型（机制 3）。四类的**检测手段不同**——只有 CONTRADICTION 必须调模型，
# 其余三类都能用确定性规则判出来（见 conflict_detector 的说明）。混在一起只按
# 「都要 LLM」理解会白花很多 token。
CONFLICT_TYPES: tuple[str, ...] = (
    "CONTRADICTION",  # 同一主题上的两条知识互相矛盾（需 LLM）
    "STALENESS",      # 引用了已失效/过期的知识（确定性）
    "GAP",            # 关系指向的目标不存在，悬空引用（确定性）
    "OVERLAP",        # 两条知识标题归一化后重复，疑似同一件事（确定性）
)

# 冲突严重度。白名单而非 DB enum：与 KNOWLEDGE_DIMENSIONS 同思路，
# 且 severity 是看板排序轴，脏值会污染统计。
CONFLICT_SEVERITIES: tuple[str, ...] = (
    "CRITICAL",
    "HIGH",
    "MEDIUM",
    "LOW",
)

# 冲突的发现手段：记下来才能回答「哪条冲突是人找的还是机器找的」，
# 也便于在 LLM 路径出错时定位是规则误报还是模型误报。
CONFLICT_DETECTORS: tuple[str, ...] = (
    "RULE",   # 确定性规则
    "LLM",    # 模型判定
)

# 冲突的处置动作。IGNORED 与 RESOLVED 的区别很重要：前者是「用户判定这不是冲突」
# （系统误报），后者是「确实是冲突且已处理」——两者对机制 3 的准确率统计方向相反，
# 合并成一个「已关闭」会让准确率永远算不出来。
CONFLICT_RESOLUTION_ACTIONS: tuple[str, ...] = (
    "RESOLVED",  # 确实是冲突，已处理
    "IGNORED",   # 系统误报，不是冲突
    "MERGED",    # 重复条目已合并
)

# 结构化建议的生命周期
SUGGESTION_STATUSES: tuple[str, ...] = (
    "PENDING",    # 待业务专家处置
    "ACCEPTED",   # 已接受
    "REJECTED",   # 已拒绝
)

# 反馈指向的实体类型。**多态**：entity_id 的含义随本字段变化，故 learning_feedback
# 刻意不建 DB 级 FK（同 knowledge_relation.downstream_id 的思路）。
LEARNING_ENTITY_TYPES: tuple[str, ...] = (
    "WIKI_PAGE",   # entity_id = wiki_page.page_id
    "RELATION",    # entity_id = knowledge_relation.id（十进制字符串）
    "CONFLICT",    # entity_id = knowledge_conflict.id（M5）
    "SUGGESTION",  # entity_id = structure_suggestion.id（M5）
)

# 用户对系统建议的处置动作。
# CONFIRM = 原样采纳；REJECT = 打回；MODIFY = 采纳但改过内容（改成了什么看
# user_modification）——三者是全序而非重叠：MODIFY 必须带 user_modification，
# 另两者必须不带，由 FeedbackLoop.record 强校验，保证下游统计口径不混。
LEARNING_USER_ACTIONS: tuple[str, ...] = (
    "CONFIRM",
    "REJECT",
    "MODIFY",
)

# 可执行规则的形态（机制 5）。**只影响算子的归一化与展板分组**，不影响求值主
# 路径——``wiki_rule_engine`` 对所有 kind 都走「逐条件比较 + 全部成立则命中」。
# 这样新增 kind 不用碰求值逻辑，也就不会出现「某种规则忘了走校验」的洞。
#
# COMPUTED 目前**无写入路径**：它对应机制 4 的 METRIC 建议，而 METRIC 不物化
# 成规则（公式不是准入判决）。保留在词表里是因为 Agent 工具会按 kind 分组展示，
# 词表缺项会让未来补写入路径时冒出脏值。宁可留白，不要留后门。
RULE_KINDS: tuple[str, ...] = (
    "THRESHOLD",        # 数值比较（≥ / ≤ / > / < / =）
    "SET_MEMBERSHIP",   # 集合归属（IN / NOT IN）
    "COMPUTED",         # 公式计算（预留，暂无写入路径）
    "LOOKUP",           # 引用他处取值后比较
)

# 规则的比较算子白名单。**白名单而非黑名单**：算子最终要交给求值器执行，
# 未知算子必须挡在校验层，不能落到求值器里变成「不认识 → 当作不成立」的静默失败。
RULE_OPERATORS: tuple[str, ...] = (
    ">=",
    "<=",
    ">",
    "<",
    "=",
    "!=",
    "IN",
    "NOT IN",
)


class WikiImportTask(Base):
    """一次知识导入的作业记录（含模型选择与成本汇总）。

    本表是幂等可重放的作业台账：``page_ids`` 记录产出的知识条目业务键，
    失败重跑时用来跳过已成功项（部分成功 → status=PARTIAL）。
    """

    __tablename__ = "wiki_import_task"
    __table_args__ = (
        Index("ix_wiki_import_task_status", "status"),
    )

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    task_type: Mapped[str] = mapped_column(String(30), nullable=False)
    source_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    source_ref: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 用户当次选择的模型：primary 失败时降级到 fallback（可空=不降级）
    selected_model_id: Mapped[int | None] = mapped_column(
        BigIntFk, ForeignKey("llm_config.id"), nullable=True
    )
    fallback_model_id: Mapped[int | None] = mapped_column(
        BigIntFk, ForeignKey("llm_config.id"), nullable=True
    )

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="PENDING", server_default="PENDING"
    )
    page_ids: Mapped[list[str] | None] = mapped_column(
        ARRAY(String(64)), nullable=True
    )
    total_pages: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    success_pages: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    failed_pages: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # P1 起：重复项（page_id 撞车且 content_hash 相同）计入跳过而非失败。
    # 与 success_pages 分开记，是因为「这次跑了但一条都没新建」与「这次确实
    # 新建了 N 条」是两种运维结论，混进一个计数就分不出来。
    skipped_pages: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    total_cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, default=Decimal("0"), server_default="0"
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_by_user_id: Mapped[int | None] = mapped_column(BigIntFk, nullable=True)
    created_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    finished_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def __repr__(self) -> str:
        return (
            f"<WikiImportTask id={self.id} type={self.task_type} "
            f"status={self.status} {self.success_pages}/{self.total_pages}>"
        )


class WikiTokenUsage(Base):
    """学习机制的 LLM 调用计量（独立于 session_token_usage）。

    ``cost`` 由调用方按 ``llm_config.cost_per_1k_input/output`` 算好后传入
    ——``LlmResponse`` 本身不携带 cost（见 base_client.LlmResponse 的扁平字段）。
    """

    __tablename__ = "wiki_token_usage"
    __table_args__ = (
        Index("ix_wiki_token_usage_mechanism_time", "mechanism", "request_time"),
        Index("ix_wiki_token_usage_task", "import_task_id"),
    )

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    import_task_id: Mapped[int | None] = mapped_column(
        BigIntFk,
        ForeignKey("wiki_import_task.id", ondelete="SET NULL"),
        nullable=True,
    )
    mechanism: Mapped[str] = mapped_column(String(50), nullable=False)
    model_config_id: Mapped[int | None] = mapped_column(
        BigIntFk, ForeignKey("llm_config.id"), nullable=True
    )
    model_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    prompt_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    completion_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    cost: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, default=Decimal("0"), server_default="0"
    )
    purpose: Mapped[str | None] = mapped_column(String(50), nullable=True)
    request_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    def __repr__(self) -> str:
        return (
            f"<WikiTokenUsage id={self.id} mech={self.mechanism} "
            f"tokens={self.prompt_tokens}+{self.completion_tokens} cost={self.cost}>"
        )


class LearningFeedback(Base):
    """学习闭环的反馈事件（机制 1~4 共用，append-only）。

    一条记录 = 一次「系统建议 → 用户处置」。**只增不改不删**：训练分类器要的是
    「历史上被改过多少次、从什么改成了什么」，而建议表自身的状态列只表达当前态
    （如 ``wiki_page.dimension`` 只知道现在是 RULE，不知道曾经建议成 PROCESS 被改）。
    把历史态塞进建议表会把当前态与历史态搅在一起，故独立成流。

    ``entity_id`` 是多态业务键（见 ``LEARNING_ENTITY_TYPES``），刻意不建 FK：
    同一张表要同时指向 wiki_page / knowledge_relation / 冲突 / 建议四种目标。
    """

    __tablename__ = "learning_feedback"
    __table_args__ = (
        Index("ix_learning_feedback_entity", "mechanism", "entity_type", "entity_id"),
        Index("ix_learning_feedback_time", "feedback_at"),
    )

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    mechanism: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(128), nullable=False)

    # 建议生成时的输入快照（如 {"title": ..., "content_head": ...}）。
    # 存快照而非引用：Page 正文后续会被编辑，事后回溯必须能看当时的输入长什么样。
    input_snapshot: Mapped[dict[str, Any] | None] = mapped_column(
        JsonColumn, nullable=True
    )
    # 系统当时的原始输出（如 classification.toDict()），不因用户修改而改写
    system_output: Mapped[dict[str, Any] | None] = mapped_column(
        JsonColumn, nullable=True
    )
    user_action: Mapped[str] = mapped_column(String(50), nullable=False)
    # MODIFY 时必填：用户改成了什么（如 {"dimension": "PROCESS"}）
    user_modification: Mapped[dict[str, Any] | None] = mapped_column(
        JsonColumn, nullable=True
    )

    feedback_user_id: Mapped[int | None] = mapped_column(BigIntFk, nullable=True)
    feedback_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    def __repr__(self) -> str:
        return (
            f"<LearningFeedback id={self.id} mech={self.mechanism} "
            f"{self.entity_type}:{self.entity_id} action={self.user_action}>"
        )


class KnowledgeConflict(Base):
    """机制 3 的冲突产出（矛盾 / 失效 / 缺口 / 重叠）。

    **page_ids 必须排序后写入**（``sorted()``，由 service 层保证）。原因见
    ``uq_knowledge_conflict_open``：部分唯一索引按数组值比较，``['A','B']`` 与
    ``['B','A']`` 是两个不相等的值，不排序则唯一索引形同虚设，同一对冲突每次
    检测都会新增一行。这是「幂等」在这里的全部代价——排序是 O(n log n) 的
    字符串比较，比去重查询便宜得多。

    终态用 ``resolved_at`` 时间戳 + ``resolution_action`` 表达，不用布尔：
    历史冲突要能留多条（同一对知识可以反复冲突、反复解决），布尔会让「上次的
    处置」被本次覆盖掉。
    """

    __tablename__ = "knowledge_conflict"
    __table_args__ = (
        Index(
            "uq_knowledge_conflict_open",
            "conflict_type",
            "page_ids",
            unique=True,
            postgresql_where=saText("resolved_at IS NULL"),
        ),
        Index(
            "ix_knowledge_conflict_open",
            "severity",
            "auto_detected_at",
            postgresql_where=saText("resolved_at IS NULL"),
        ),
        Index(
            "ix_knowledge_conflict_page_ids",
            "page_ids",
            postgresql_using="gin",
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    conflict_type: Mapped[str] = mapped_column(String(30), nullable=False)
    page_ids: Mapped[list[str]] = mapped_column(ARRAY(String(64)), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    severity: Mapped[str] = mapped_column(
        String(20), nullable=False, default="MEDIUM", server_default="MEDIUM"
    )
    detected_by: Mapped[str] = mapped_column(
        String(20), nullable=False, default="RULE", server_default="RULE"
    )
    auto_detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolution_action: Mapped[str | None] = mapped_column(String(50), nullable=True)
    resolved_by_user_id: Mapped[int | None] = mapped_column(BigIntFk, nullable=True)

    def __repr__(self) -> str:
        return (
            f"<KnowledgeConflict id={self.id} type={self.conflict_type} "
            f"severity={self.severity} pages={self.page_ids} resolved={self.resolved_at is not None}>"
        )


class StructureSuggestion(Base):
    """机制 4 的结构化建议（这条弱结构知识可以升成什么结构）。

    ``extracted_structure`` 的形状随 ``suggested_dimension`` 变化：

    - RULE    → ``{"conditions": [{"field", "operator", "value", "unit"}], "action": {...}}``
    - PROCESS → ``{"steps": [{"seq", "name", "actor_role", "action"}], "trigger": ...}``
    - METRIC  → ``{"formula": ..., "numerator": ..., "denominator": ..., "unit": ...}``
    - CONCEPT → ``{"term": ..., "definition": ..., "aliases": [...]}``

    刻意不拆成四张表：它们的生命周期、审核流程、反馈落点完全一致，差异只在
    一个 JSON 的**形状**上。拆表会得到四份几乎相同的 CRUD，而收益只是「DB 层
    能约束形状」——但形状本来就来自 LLM 输出，DB 约束挡不住模型乱填，校验只能
    在 service 层做（见 ``structure_suggester.validateStructure``）。

    ``status`` 离开 PENDING 即终态；``uq_structure_suggestion_pending`` 保证
    同一条知识的同一维度同时只有一条待处置的建议，重复跑机制 4 不会堆积。
    """

    __tablename__ = "structure_suggestion"
    __table_args__ = (
        Index(
            "uq_structure_suggestion_pending",
            "page_id",
            "suggested_dimension",
            unique=True,
            postgresql_where=saText("status = 'PENDING'"),
        ),
        Index("ix_structure_suggestion_page", "page_id", "suggested_at"),
        Index("ix_structure_suggestion_pending", "status", "suggested_at"),
    )

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    page_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("wiki_page.page_id", ondelete="CASCADE"),
        nullable=False,
    )
    suggested_dimension: Mapped[str] = mapped_column(String(30), nullable=False)
    extracted_structure: Mapped[dict[str, Any] | None] = mapped_column(
        JsonColumn, nullable=True
    )
    confidence: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="PENDING", server_default="PENDING"
    )
    suggested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolved_by_user_id: Mapped[int | None] = mapped_column(BigIntFk, nullable=True)

    def __repr__(self) -> str:
        return (
            f"<StructureSuggestion id={self.id} page={self.page_id} "
            f"dim={self.suggested_dimension} status={self.status}>"
        )


class WikiRuleExecutable(Base):
    """机制 5 的结构化产物：一条可执行的业务规则（条件 → 动作）。

    **为什么是独立表而不是 ``feature_rule``**：后者是特征工程的阈值表
    （``feature_name`` + ``operator/threshold_value/unit``，没有表达式列），
    语义是「某个特征在某个阈值上乘个系数」。本表的语义是「一组条件全部成立时
    对一个业务实体做什么」，两者在条件数量、动作语义、消费方（Agent 直读 vs
    特征计算）上都不同。硬并表要么丢条件、要么把 feature_rule 撑成通用表达式表。

    **为什么 page_id 是 UNIQUE**：产物是那条知识的*当前*结构化形态，不是历史
    版本流。版本历史在 ``learning_feedback``（谁在何时接受了什么建议）。重复
    物化走 UPSERT 覆盖，唯一约束就是「一条知识最多一份可执行规则」的强制表达。

    ``rule_expression`` 的形状由 ``rule_kind`` 决定，且**始终**是
    ``{"conditions": [...], "action": {...}}`` 这一层外壳；kind 只描述条件的
    比较方式。这样 ``wiki_rule_engine`` 只有一条求值主路径，kind 仅影响算子
    归一化与展示分组。

    ``dry_run_examples`` 随规则一起存：dry-run 的样例是**审核这条规则的人**给的
    期望值，属于规则本身（下次改规则要能看出原来期望什么），不该只活在请求体里。
    """

    __tablename__ = "wiki_rule_executable"
    __table_args__ = (
        Index("ix_wiki_rule_executable_target", "target_entity"),
    )

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    page_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("wiki_page.page_id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    rule_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    rule_expression: Mapped[dict[str, Any]] = mapped_column(JsonColumn, nullable=False)
    target_entity: Mapped[str | None] = mapped_column(String(100), nullable=True)
    dry_run_examples: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JsonColumn, nullable=True
    )
    # 权威链 L5→L0：同一条规则可能由多级文件共同支撑，顺序即优先级。
    authority_chain: Mapped[list[str] | None] = mapped_column(
        ARRAY(String(10)), nullable=True
    )
    version: Mapped[str] = mapped_column(
        String(30), nullable=False, default="v1.0", server_default="v1.0"
    )
    created_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def __repr__(self) -> str:
        return (
            f"<WikiRuleExecutable id={self.id} page={self.page_id} "
            f"kind={self.rule_kind}>"
        )


class ProcessWorkflow(Base):
    """机制 5 的结构化产物：一个可执行流程（步骤序列 + 触发条件）。

    ``steps`` 的元素形状 ``{seq, name, actor_role, action, sla_hours?, condition?}``，
    ``seq`` 由 ``StructureSuggester`` 按数组下标重建（模型给的序号不可信，见
    机制 4 的 ``_validateProcess``），故这里可以直接按 ``steps`` 顺序渲染。

    与 ``WikiRuleExecutable`` 同样是 page_id UNIQUE + UPSERT 覆盖：一条流程
    知识只对应一个当前流程。
    """

    __tablename__ = "process_workflow"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    page_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("wiki_page.page_id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    workflow_version: Mapped[str] = mapped_column(
        String(30), nullable=False, default="v1.0", server_default="v1.0"
    )
    steps: Mapped[list[dict[str, Any]]] = mapped_column(
        JsonColumn, nullable=False, default=list, server_default=saText("'[]'::jsonb")
    )
    trigger_condition: Mapped[str | None] = mapped_column(Text, nullable=True)
    applicable_scope: Mapped[dict[str, Any] | None] = mapped_column(
        JsonColumn, nullable=True
    )
    created_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def __repr__(self) -> str:
        return (
            f"<ProcessWorkflow id={self.id} page={self.page_id} "
            f"steps={len(self.steps or [])}>"
        )


__all__ = [
    "IMPORT_TASK_TYPES",
    "IMPORT_TASK_STATUSES",
    "LEARNING_MECHANISMS",
    "LEARNING_ENTITY_TYPES",
    "LEARNING_USER_ACTIONS",
    "CONFLICT_TYPES",
    "CONFLICT_SEVERITIES",
    "CONFLICT_DETECTORS",
    "CONFLICT_RESOLUTION_ACTIONS",
    "SUGGESTION_STATUSES",
    "RULE_KINDS",
    "RULE_OPERATORS",
    "WikiImportTask",
    "WikiTokenUsage",
    "LearningFeedback",
    "KnowledgeConflict",
    "StructureSuggestion",
    "WikiRuleExecutable",
    "ProcessWorkflow",
]
