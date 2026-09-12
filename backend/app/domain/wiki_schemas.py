"""Wiki 知识管理 DTO（feat-wiki-knowledge，Phase 8 M1）。

与 app/domain/schemas.py 的约定一致：snake_case 字段名 + camelCase JSON
（由 CamelModel 的 alias_generator 负责），Create/Update/Read 三模式，
Update 用 UNSET 哨兵区分「未提供」与「显式置空」。

独立成文件的原因同 wiki_models.py：schemas.py 已近 3000 行，继续追加会
突破 800 行/文件的工程约束。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

from pydantic import Field

from app.domain.schemas import UNSET, CamelModel, _UnsetType
from app.domain.wiki_coverage_models import GAP_UNLINKED

# ---------------------------------------------------------------------------
# 输入边界常量
#
# 导入是**唯一**能把「用户输入 × LLM 调用」相乘的入口：一次请求 N 条草稿
# 就是 N 次模型调用。不设上限等于把成本开关交给调用方——一个 10 万条的
# drafts 数组会在服务端触发 10 万次 LLM 调用与 10 万次 INSERT。
# 因此这里给字节量与条数都设硬上限，超限由 Pydantic 直接 422。
# ---------------------------------------------------------------------------

# 单条知识正文上限（字符）。PG 的 TEXT 不截断，但无上限的请求体是 DoS 面。
MAX_CONTENT_CHARS = 200_000

# 预检的原始素材上限（字符）。一篇 Markdown 长文远小于此值。
MAX_SOURCE_CHARS = 1_000_000

# 单次导入的草稿条数上限。
MAX_IMPORT_DRAFTS = 200

# 批量删除**不设产品意义上的条数上限**（2026-09-12 决策，原 MAX_BATCH_DELETE_PAGES=100 已删）。
#
# 原上限的论据是「DTO 边界挡住单请求放大」——但迁移/清理场景（整批撤销一次导入、
# 清空某个来源的全部条目）天然是几千条量级，上限只会逼用户分几十次删，每次都要
# 重新翻页多选，反而更容易误删。破坏性操作的护栏改由**前端二次确认弹窗**
# （AdminWikiPagesPage，明确列出条数与级联范围）承担，不在传输层设卡。
#
# 但**驱动另有硬上限**，不显式挡住就会表现为 500：asyncpg 单条语句的绑定参数
# 不得超过 32767。SQLAlchemy 的 ``in_()`` 把 page_ids 展开成 N 个独立占位符
# （**不是**数组参数），超限即抛
# ``InterfaceError: the number of query arguments cannot exceed 32767`` ——
# 走的正是本文件 ``WikiPageUpdate`` 记的那类「驱动层拒绝 → 无领域异常映射 →
# 500」路径。在 DTO 拦成 422，是本模块一贯的「边界校验换可读错误」做法。
#
# 实测（2026-09-12，真实 PG）：N=32766 通过、N=32767 通过、N=32768 抛上述
# InterfaceError —— 边界是**闭区间** 32767，故 maxLength 取满该值而非留余量。
# 注意 Nginx 的 ``client_max_body_size 100m``（docker/nginx.conf）**挡不住**它：
# 100MB 约合 140 万个 id，是悬崖的 ~40 倍；且 compose 把 8000 端口直连暴露，
# 绕开 Nginx 的路径连这层都没有。此前这里写的「由 web server 的请求体上限兜底」
# 是错的，已按实测更正。
#
# 取满 32767 而非留余量：快照 SELECT / 5 个级联 COUNT / 批量 DELETE 携带的参数数
# 都**恰好**等于 N（无额外参数），故 N=32767 即安全上界。将来若给这些语句再加
# 绑定参数，此常量须同步下调。
MAX_BATCH_DELETE_PAGE_IDS = 32_767

# ---------------------------------------------------------------------------
# Wiki Page
# ---------------------------------------------------------------------------


class WikiPageCreate(CamelModel):
    """创建知识条目。

    业务专家只需提供 title + content；``pageId`` 留空时由 service 生成。
    ``dimension`` 可显式指定（业务专家自己知道归属），留空则由机制 1
    自动分类填建议值。
    """

    page_id: str | None = Field(default=None, max_length=64)
    title: str = Field(..., min_length=1, max_length=200)
    content: str = Field(..., min_length=1, max_length=MAX_CONTENT_CHARS)
    dimension: str | None = Field(default=None, max_length=30)
    authority_level: str | None = Field(default=None, max_length=10)


class WikiPageUpdate(CamelModel):
    """更新知识条目（PATCH 语义，未提供的字段跳过）。

    ``dimension`` 显式提供时即视为「业务专家覆盖自动分类建议」，
    service 会把这次覆盖写回 learning_feedback（机制 1 学习闭环）。

    长度上限与 Create 及 DB 列对齐：少了这道约束，超长 title 会一路走到
    INSERT 才被 asyncpg 以 StringDataRightTruncation 拒绝，而 DataError
    没有领域异常映射，最终表现为 500 而非 422。
    """

    title: _UnsetType | str = Field(default=UNSET, min_length=1, max_length=200)
    content: _UnsetType | str = Field(
        default=UNSET, min_length=1, max_length=MAX_CONTENT_CHARS
    )
    dimension: _UnsetType | str | None = Field(default=UNSET, max_length=30)
    structure_stage: _UnsetType | str = Field(default=UNSET, max_length=20)
    status: _UnsetType | str = Field(default=UNSET, max_length=20)
    authority_level: _UnsetType | str | None = Field(default=UNSET, max_length=10)
    version: _UnsetType | str = Field(default=UNSET, min_length=1, max_length=30)


class WikiPageRead(CamelModel):
    """知识条目读模型。"""

    id: int
    page_id: str
    title: str
    content: str
    dimension: str | None = None
    structure_stage: str
    auto_classification: dict[str, Any] | None = None
    status: str
    authority_level: str | None = None
    version: str
    created_by_user_id: int | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    created_time: datetime | None = None
    updated_time: datetime | None = None


class WikiPageListRead(CamelModel):
    """知识条目分页响应（rows + total，与 AuditLogPageRead 同模式）。"""

    rows: list[WikiPageRead]
    total: int


class WikiPageBatchDeleteRequest(CamelModel):
    """批量删除知识条目。

    ``pageIds`` **去重后**才是真正的目标集合。重复 id 不算入参错误：前端多选
    跨页保留时很容易带上重复项，为此回 422 等于把去重这件实现细节推给调用方。

    ``minLength=1`` 挡住空数组（空数组是调用方 bug，静默成功会让「删了 0 条」
    看起来像成功）。``maxLength`` 是**驱动硬上限**而非产品上限，理由见本模块
    顶部注释。
    """

    page_ids: list[Annotated[str, Field(max_length=64)]] = Field(
        ...,
        min_length=1,
        max_length=MAX_BATCH_DELETE_PAGE_IDS,
        description=(
            "待删除的条目业务键，去重后即为目标集合。无产品意义上的条数上限，"
            f"但受 asyncpg 绑定参数限制，最多 {MAX_BATCH_DELETE_PAGE_IDS} 条"
        ),
    )


class WikiPageBatchDeleteCascadeRead(CamelModel):
    """批量删除时被 DB 级联连带清掉的行数（只报数，不回传内容）。

    只统计**直接**引用 ``wiki_page`` 的 5 张表。``evidence`` 挂在 claim 之下
    （二级级联），随 claim 一起走，不单列 —— 单列会让调用方以为
    ``claims + evidences`` 是两批互不相干的数据。
    """

    claims: int
    relations: int
    suggestions: int
    rules: int
    workflows: int


class WikiPageBatchDeleteRead(CamelModel):
    """批量删除结果。

    刻意**不是 204**：批量操作必须能回答「哪几条没删掉」。
    ``notFound`` 非空不是错误（并发下别的用户先删了同一条很正常），
    调用方据它给用户提示，而不是让整批回滚。
    """

    # 去重后的目标条数。与调用方入参的长度在有重复时不同，故显式回报。
    requested: int
    deleted_page_ids: list[str]
    not_found: list[str]
    cascade: WikiPageBatchDeleteCascadeRead


# ---------------------------------------------------------------------------
# Claim / Evidence
# ---------------------------------------------------------------------------


class EvidenceRead(CamelModel):
    """证据出处读模型。"""

    id: int
    claim_id: int
    source_type: str
    source_id: str | None = None
    page_number: str | None = None
    section_name: str | None = None
    paragraph_no: str | None = None
    content: str | None = None
    created_time: datetime | None = None


class KnowledgeClaimRead(CamelModel):
    """事实原子读模型（含其证据列表）。"""

    id: int
    page_id: str
    claim_text: str
    claim_type: str | None = None
    embedding_ref: str | None = None
    created_time: datetime | None = None
    evidences: list[EvidenceRead] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Knowledge Relation
# ---------------------------------------------------------------------------


class KnowledgeRelationRead(CamelModel):
    """知识关系读模型（``confirmed`` 区分「已生效」与「待审核候选」）。

    三态由两个字段共同表达（见 ``KnowledgeRelation``）：待审核
    ``confirmed=false, rejectedAt=null``；已确认 ``true, null``；
    已打回 ``false, 有时间戳``。单看 ``confirmed`` 会把「待审」与「已打回」
    混成一类，前端就没法把打回的候选从待办列表里摘掉。
    """

    id: int
    upstream_page_id: str
    downstream_type: str
    downstream_id: str
    relation_type: str
    confidence: float | None = None
    auto_detected: bool
    confirmed: bool
    rejected_at: datetime | None = None
    created_time: datetime | None = None


# ---------------------------------------------------------------------------
# 机制 2：关系发现与审核（M4）
# ---------------------------------------------------------------------------


class WikiRelationDiscoverRequest(CamelModel):
    """跑一次关系发现。

    ``model_id`` 为空 = 只跑确定性路径（引用检测），零 token 成本；
    给了模型则额外跑一路 LLM 实体抽取 → 本体类匹配。
    """

    model_id: int | None = None


class WikiRelationDiscoverRead(CamelModel):
    """发现结果。

    ``class_extraction_status`` 把「没要求跑」与「跑了没成」分开报，避免
    LLM 那次静默失败被读成「这篇文章确实没提到任何本体类」。
    """

    candidates: list[KnowledgeRelationRead]
    total: int
    class_extraction_status: str


# ---------------------------------------------------------------------------
# 机制 1：分类调整（M3）
# ---------------------------------------------------------------------------


class WikiReclassifyRequest(CamelModel):
    """业务专家对机制 1 分类结论的处置。

    ``dimension`` 是**必填键**（无默认值）：漏传与「显式传 null」语义相反
    —— 前者是调用方写错了，后者是「打回这个分类」。给个 default=None 就会
    把两者混成一个，用户界面少发一个字段就被静默解释成「清空维度」。
    """

    dimension: str | None = Field(..., max_length=30)


class WikiReclassifyRead(CamelModel):
    """调整结果。``action`` 是本次记录的反馈动作（CONFIRM/REJECT/MODIFY）。

    ``action=None`` 表示没有可反馈的建议（该条目从未被分类过），此时只是
    单纯改了维度 —— 调用方据此区分文案，不被误导成「已记录修正」。
    """

    page: WikiPageRead
    action: str | None = None


# ---------------------------------------------------------------------------
# 导入（M2）
# ---------------------------------------------------------------------------


class WikiImportDraft(CamelModel):
    """一条待导入的知识条目草稿（preview 产出，execute 回传）。"""

    page_id: str | None = Field(default=None, max_length=64)
    title: str = Field(..., min_length=1, max_length=200)
    content: str = Field(..., min_length=1, max_length=MAX_CONTENT_CHARS)


class WikiImportPreviewRequest(CamelModel):
    """预检请求：把原始 Markdown 按标题层级切成草稿，不落库、不调模型。"""

    source: str = Field(..., min_length=1, max_length=MAX_SOURCE_CHARS)


class WikiImportPreviewRead(CamelModel):
    """预检结果。"""

    drafts: list[WikiImportDraft]
    total: int


class WikiImportFileParseRead(CamelModel):
    """上传文件的解析结果：纯文本 + 归一化后的来源类型。

    ``source_type`` 由后端判定（按扩展名/MIME）而**不是**前端自报：向导随后
    要用它落 ``wiki_import_task.source_type`` 做溯源，让调用方自报等于允许
    「上传 .pdf 却记成 MARKDOWN」，台账就不可信了。

    不在这里切草稿：PDF/Word 的文本抽取是有损的，先让用户核对文本，再走
    既有的 ``/preview`` 切分（单一切分逻辑，两条来源路径共用）。
    """

    text: str
    source_type: str


class WikiImportExecuteRequest(CamelModel):
    """执行导入。

    ``auto_classify`` 默认开启：用户既然在向导里选了模型，默认就用它做
    机制 1 分类；关闭则纯落库（此时 ``modelId`` 可省）。
    """

    drafts: list[WikiImportDraft] = Field(
        ..., min_length=1, max_length=MAX_IMPORT_DRAFTS
    )
    model_id: int | None = None
    fallback_model_id: int | None = None
    auto_classify: bool = True
    source_type: str | None = Field(default=None, max_length=30)
    source_ref: str | None = Field(default=None, max_length=500)
    task_type: str = Field(default="BULK_IMPORT", max_length=30)


class WikiImportTaskRead(CamelModel):
    """导入任务读模型。"""

    id: int
    task_type: str
    source_type: str | None = None
    source_ref: str | None = None
    selected_model_id: int | None = None
    fallback_model_id: int | None = None
    status: str
    page_ids: list[str] | None = None
    total_pages: int
    success_pages: int
    failed_pages: int
    total_cost_usd: Decimal
    error_message: str | None = None
    created_by_user_id: int | None = None
    created_time: datetime | None = None
    finished_time: datetime | None = None


class WikiImportTaskListRead(CamelModel):
    """导入任务分页响应。"""

    rows: list[WikiImportTaskRead]
    total: int


class WikiImportModelRead(CamelModel):
    """导入向导可选的模型（``usable=False`` 的模型不可用，仅作解释性展示）。"""

    id: int
    model_name: str
    provider: str
    usable: bool
    is_active: bool


# ---------------------------------------------------------------------------
# 机制 3：冲突检测（M5）
# ---------------------------------------------------------------------------


class KnowledgeConflictRead(CamelModel):
    """冲突读模型。

    ``resolved_at`` 为空 = 未解决；``detected_by`` 区分这条是规则找的还是模型
    找的 —— 两者的误报排查路径完全不同（前者改规则，后者改 prompt），
    不给这个字段就只能去翻日志。
    """

    id: int
    conflict_type: str
    page_ids: list[str]
    description: str | None = None
    severity: str
    detected_by: str
    auto_detected_at: datetime | None = None
    resolved_at: datetime | None = None
    resolution_action: str | None = None
    resolved_by_user_id: int | None = None


class WikiConflictListRead(CamelModel):
    """冲突分页响应。"""

    rows: list[KnowledgeConflictRead]
    total: int


class WikiConflictDetectRequest(CamelModel):
    """跑一次冲突检测。

    ``model_id`` 为空 = 只跑确定性三路（失效/悬空/重复），零 token；
    给了模型才额外跑 LLM 矛盾判定。与机制 2 的 ``WikiRelationDiscoverRequest``
    同形：**模型永远是可选增强**，不给模型也必须能产出确定性的那部分。
    """

    model_id: int | None = None


class WikiConflictDetectRead(CamelModel):
    """检测结果。``llm_status`` 同 M4 的 ``class_extraction_status``：
    把「没要求跑」与「跑了没成」分开报，避免一次静默失败被读成「确实没矛盾」。
    """

    conflicts: list[KnowledgeConflictRead]
    total: int
    llm_status: str


class WikiConflictResolveRequest(CamelModel):
    """处置一条冲突。

    ``action`` 是**必填键**（无默认值），理由同 ``WikiReclassifyRequest``：
    漏传与「传了 IGNORED」是两件事，给默认值就会把调用方的字段遗漏静默解释成
    「系统误报」。冲突的四类里只有 ``IGNORED`` 表示误报，猜错的代价是污染
    机制 3 的准确率统计。

    没有「处置备注」字段：当前 schema 无处存放它，加一个字段就得再加一列，
    而真实需求还没出现（YAGNI）。等确实有人要写备注时再加列 + 迁移。
    """

    action: str = Field(..., max_length=50)


# ---------------------------------------------------------------------------
# 机制 4：结构化建议（M5）
# ---------------------------------------------------------------------------


class StructureSuggestionRead(CamelModel):
    """结构化建议读模型。

    ``extracted_structure`` 的形状随 ``suggested_dimension`` 变化
    （见 ``StructureSuggestion`` 的 docstring），故这里是自由 JSON —— 用
    ``dict`` 而不是为四个维度各建一个嵌套模型：形状来自 LLM，DB 与 DTO 都
    约束不住，真正的校验在 ``structure_suggester`` 里做（不合形状即丢弃，
    不落库）。
    """

    id: int
    page_id: str
    # 建议工作台（跨条目列表）才有值；条目详情里的子列表不 join 条目标题。
    page_title: str | None = None
    suggested_dimension: str
    extracted_structure: dict[str, Any] | None = None
    confidence: float | None = None
    status: str
    suggested_at: datetime | None = None
    resolved_at: datetime | None = None
    resolved_by_user_id: int | None = None

    @classmethod
    def fromRow(
        cls, entity: Any, pageTitle: str | None
    ) -> StructureSuggestionRead:
        """由 ORM 行 + join 来的条目标题构造（不可变：copy 出新对象）。"""
        return cls.model_validate(entity).model_copy(
            update={"page_title": pageTitle}
        )


class WikiSuggestionListRead(CamelModel):
    """建议列表响应（按条目查，不分页：单条知识的建议数天然是个位数）。"""

    rows: list[StructureSuggestionRead]
    total: int


class WikiSuggestionGenerateRequest(CamelModel):
    """跑一次结构化建议抽取（``model_id` 为空则只做确定性预筛，不产建议）。"""

    model_id: int | None = None


class WikiSuggestionGenerateRead(CamelModel):
    """抽取结果。

    ``triggered_kind`` 是确定性预筛命中的维度（``None`` = 没命中任何触发器）。
    单独报出来是因为「没命中触发器所以没调模型」与「调了模型但抽不出结构」
    对用户是完全不同的两件事，而两者的 ``suggestions`` 都是空列表。
    """

    suggestions: list[StructureSuggestionRead]
    total: int
    triggered_kind: str | None = None
    extraction_status: str


# ---------------------------------------------------------------------------
# 机制 5：可执行规则 / 流程 / 渐进结构（M6）
# ---------------------------------------------------------------------------


class WikiRuleRead(CamelModel):
    """可执行规则读模型（Agent 侧直读入口，也是规则工作台的数据源）。"""

    id: int
    page_id: str
    rule_kind: str
    rule_expression: dict[str, Any]
    target_entity: str | None = None
    authority_chain: list[str] | None = None
    version: str
    dry_run_examples: list[dict[str, Any]] | None = None
    created_time: datetime | None = None
    updated_time: datetime | None = None


class ProcessWorkflowRead(CamelModel):
    """结构化流程读模型。"""

    id: int
    page_id: str
    workflow_version: str
    steps: list[dict[str, Any]]
    trigger_condition: str | None = None
    applicable_scope: dict[str, Any] | None = None
    created_time: datetime | None = None
    updated_time: datetime | None = None


class WikiRuleDryRunRequest(CamelModel):
    """dry-run 请求。

    ``examples`` 不传即用规则里存的样例（审核当时给的期望）。``input`` 与
    ``expectedOutput`` 声明成自由 ``dict`` 而**不是**建嵌套模型：形状校验统一
    在 ``wiki_rule_engine.dryRun`` 里做一处。两处校验必然分叉 —— Pydantic 宽松
    模式会把 ``"yes"`` 转成 ``True``，于是 ``expectedOutput.matched`` 写成字符串
    也能过，而引擎的严格检查永远跑不到。
    """

    # 条数上限：求值是 O(样例数 × 条件数) 的纯 Python 循环，响应还会把 input 与每条
    # 条件的明细原样回显 —— 不设上限就是一个单请求放大入口，而全局 30 req/min 兜不住
    # 单个请求内部的放大。审核规则用不到上百条样例。
    examples: list[dict[str, Any]] | None = Field(default=None, max_length=100)


class WikiRuleConditionRead(CamelModel):
    """单个条件的求值明细。

    ``matched`` 是三态：``true`` / ``false`` / ``null``（判不了）。``null`` 必须
    能被前端渲染成「判不了」而不是「不成立」，故这里不用 bool 收敛。
    """

    field: str
    operator: str
    expected: Any = None
    actual: Any = None
    present: bool
    matched: bool | None = None
    undecidable_reason: str | None = None


class WikiRuleDryRunCaseRead(CamelModel):
    """一条样例的 dry-run 结果。"""

    index: int
    input: dict[str, Any]
    expected_matched: bool
    actual_matched: bool
    status: str
    conditions: list[WikiRuleConditionRead]


class WikiRuleDryRunSummaryRead(CamelModel):
    """dry-run 汇总。``undecidable`` 与 ``failed`` 分列，不可合并。"""

    total: int
    passed: int
    failed: int
    undecidable: int


class WikiRuleDryRunRead(CamelModel):
    """dry-run 响应。"""

    rule_kind: str
    results: list[WikiRuleDryRunCaseRead]
    summary: WikiRuleDryRunSummaryRead


class WikiStructureStageRead(CamelModel):
    """阶段同步结果。``changed`` 让调用方能区分「跑了」与「改了」。"""

    page_id: str
    previous: str
    current: str
    changed: bool


# ---------------------------------------------------------------------------
# 机制 6：覆盖度自感知（M7）
# ---------------------------------------------------------------------------


class CoverageCellRead(CamelModel):
    """覆盖度矩阵的一格（带类名 —— 光有 id 的看板没法看）。"""

    dimension: str
    ontology_class_id: int
    class_name: str
    domain: str
    page_count: int
    approved_count: int
    coverage_status: str
    last_refreshed_at: datetime

    @classmethod
    def fromRow(cls, cell: Any, className: str) -> CoverageCellRead:
        return cls(
            dimension=cell.dimension,
            ontology_class_id=cell.ontology_class_id,
            class_name=className,
            domain=cell.domain,
            page_count=cell.page_count,
            approved_count=cell.approved_count,
            coverage_status=cell.coverage_status,
            last_refreshed_at=cell.last_refreshed_at,
        )


class CoverageSummaryRead(CamelModel):
    """看板顶部汇总：各状态格数 + 未标域格数。

    ``byStatus`` 用 ``dict[str, int]`` 而不是固定四个字段：状态词表未来可能
    增补，写死字段会让「新状态没地方放」变成一次契约变更。
    """

    total_cells: int
    by_status: dict[str, int]
    unassigned_cells: int


class CoverageUnlinkedRead(CamelModel):
    """「有维度但没挂到任何已确认业务对象」的条目统计。

    这是矩阵看不见的那部分缺口（矩阵每格都挂了类），却常常是初始阶段最大的
    一个：条目都在，只是 Agent 按业务对象检索不到。
    """

    gap_type: str = GAP_UNLINKED
    page_count: int
    dimensions: dict[str, int]


class CoverageGapRead(CamelModel):
    """一条缺口（某格没有可用知识，或一批知识没有归属）。"""

    dimension: str
    ontology_class_id: int | None
    class_name: str | None
    domain: str
    status: str
    page_count: int
    approved_count: int


class CoverageRefreshRead(CamelModel):
    """一次全量刷新的结果。"""

    cell_count: int
    class_count: int
    unmapped_class_count: int
    removed_count: int
    refreshed_at: datetime


class CoverageOverviewRead(CamelModel):
    """看板一次取齐：汇总 + 缺口 + 孤儿条目统计。

    合成一个响应而不是三个接口：这三块数据是**同时**被渲染的（看板打开就都要），
    分三个接口只会让前端并发三个请求再拼装，且三份数据可能来自不同的刷新时刻。
    """

    summary: CoverageSummaryRead
    gaps: list[CoverageGapRead]
    unlinked: CoverageUnlinkedRead


class ClassDomainMappingRead(CamelModel):
    """一条「本体类 → 业务域」标注。"""

    ontology_class_id: int
    class_name: str
    domain: str
    created_by_user_id: int | None
    created_time: datetime

    @classmethod
    def fromRow(cls, mapping: Any, className: str) -> ClassDomainMappingRead:
        return cls(
            ontology_class_id=mapping.ontology_class_id,
            class_name=className,
            domain=mapping.domain,
            created_by_user_id=mapping.created_by_user_id,
            created_time=mapping.created_time,
        )


class ClassDomainMappingCreate(CamelModel):
    """给本体类标注业务域。

    **不设 domain 白名单**：域是分类轴而非授权词表（见 wiki_coverage_models），
    锁死取值等于把「不锁业务域」这条核心诉求做废。只做 strip+upper 归一化。
    """

    ontology_class_id: int = Field(..., gt=0)
    domain: str = Field(..., min_length=1, max_length=100)


class CoverageDomainListRead(CamelModel):
    """已使用过的业务域词表（去重）。"""

    domains: list[str]


__all__ = [
    "WikiPageCreate",
    "WikiPageUpdate",
    "WikiPageRead",
    "WikiPageListRead",
    "EvidenceRead",
    "KnowledgeClaimRead",
    "KnowledgeRelationRead",
    "WikiRelationDiscoverRequest",
    "WikiRelationDiscoverRead",
    "WikiReclassifyRequest",
    "WikiReclassifyRead",
    "WikiImportDraft",
    "WikiImportPreviewRequest",
    "WikiImportPreviewRead",
    "WikiImportFileParseRead",
    "WikiImportExecuteRequest",
    "WikiImportTaskRead",
    "WikiImportTaskListRead",
    "WikiImportModelRead",
    "KnowledgeConflictRead",
    "WikiConflictListRead",
    "WikiConflictDetectRequest",
    "WikiConflictDetectRead",
    "WikiConflictResolveRequest",
    "StructureSuggestionRead",
    "WikiSuggestionListRead",
    "WikiSuggestionGenerateRequest",
    "WikiSuggestionGenerateRead",
    "WikiRuleRead",
    "ProcessWorkflowRead",
    "WikiRuleDryRunRequest",
    "WikiRuleConditionRead",
    "WikiRuleDryRunCaseRead",
    "WikiRuleDryRunSummaryRead",
    "WikiRuleDryRunRead",
    "WikiStructureStageRead",
    "CoverageCellRead",
    "CoverageSummaryRead",
    "CoverageUnlinkedRead",
    "CoverageGapRead",
    "CoverageRefreshRead",
    "CoverageOverviewRead",
    "ClassDomainMappingRead",
    "ClassDomainMappingCreate",
    "CoverageDomainListRead",
]
