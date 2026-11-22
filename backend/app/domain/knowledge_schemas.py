"""P4 知识服务 Facade 的 DTO（D32 契约）。

契约来源：[`docs/update/1109/智能采购平台评估与渐进升级计划_跨板块扩展_1109.md`](../../../docs/update/1109/智能采购平台评估与渐进升级计划_跨板块扩展_1109.md) §6 P4
（「接口：`/api/v1/semantic/knowledge/query`，请求带 `domain`、问题、权限上下文；
内部编排 图谱 / RAG / SQL / 指标 四路，统一引用与权限口径」）。

设计口径
--------
1. **字段名 snake_case、JSON 别名 camelCase**（`CamelModel` 的仓内统一约定）。
2. **通道枚举封闭**：`channels` 只认四路；非法值在**请求解析层**被拒
   （漏到编排层会被静默忽略 ⇒ 调用方以为跑了第五路）。
3. **响应对「发布面是否真的被用到」自证**：`release.degraded` + 每条 evidence 的
   `source`（`RELEASE` = 出自某版发布）。
   ⇒ 「引用可回溯到发布版本」这条判据在响应里**可判定**，而不是靠约定。
   ⚠️ `LIVE`（直读治理表）在本批**保留但不会出现**：知识路径不做活数据降级
   （治理表是原地 UPDATE 的活数据，直读它拿到的内容**没有版本可引**
   ⇒ 宁可给「空 + 降级标记」，也不给「看着像发布、其实不是」的东西）。
4. **成功响应不套 `{success, data}` 信封**（仓内成功响应一律裸 DTO；
   只有错误响应才有 `ErrorResponse` 包络）。
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime

from pydantic import Field, field_validator, model_validator

from app.domain.schemas import CamelModel

# --- 通道（四路） ------------------------------------------------------------

KNOWLEDGE_CHANNEL_GRAPH = "GRAPH"
KNOWLEDGE_CHANNEL_METRIC = "METRIC"
KNOWLEDGE_CHANNEL_RAG = "RAG"
KNOWLEDGE_CHANNEL_SQL = "SQL"

#: 编排的四路。顺序固定 ⇒ 输出与测试可逐字比对。
KNOWLEDGE_CHANNELS: tuple[str, ...] = (
    KNOWLEDGE_CHANNEL_GRAPH,
    KNOWLEDGE_CHANNEL_METRIC,
    KNOWLEDGE_CHANNEL_RAG,
    KNOWLEDGE_CHANNEL_SQL,
)

# --- 路由结果状态 ------------------------------------------------------------

KNOWLEDGE_ROUTE_OK = "OK"
KNOWLEDGE_ROUTE_EMPTY = "EMPTY"
KNOWLEDGE_ROUTE_DEGRADED = "DEGRADED"
KNOWLEDGE_ROUTE_PENDING = "PENDING"
#: 域对当前用户不可见 ⇒ **空结果 + 警告**（裁决 4：不是 403）。
#: 与 `EMPTY` 分开 ⇒ 调用方能区分「这个域我没权限」与「这个域里没这些东西」。
KNOWLEDGE_ROUTE_DENIED = "DENIED"

# --- 证据来源 ----------------------------------------------------------------

#: 出自某一版语义发布（可回溯到 releaseId / releaseVersion）。
KNOWLEDGE_SOURCE_RELEASE = "RELEASE"
#: 直读治理表（无发布版本可引）—— **保留值，本批知识路径不会产生**
#: （发布面不可用时是「空结果 + 显式降级标记」，不是直读治理表）。
KNOWLEDGE_SOURCE_LIVE = "LIVE"

# --- 边界常量 ----------------------------------------------------------------

MAX_QUESTION_LENGTH = 500
MAX_DOMAIN_CODE_LENGTH = 20
MAX_ENTITY_HINT_CODE_LENGTH = 128
MAX_ENTITY_HINT_TYPE_LENGTH = 32
MAX_TOP_K = 50
DEFAULT_TOP_K = 8
MAX_SCOPE_ITEMS = 32
MAX_ORG_CODE_LENGTH = 64

#: 文档安全分级（沿用 `document_catalog.security_level` 的既有语义）。
KNOWLEDGE_SECURITY_LEVELS: tuple[str, ...] = ("L1", "L2", "L3")
DEFAULT_MAX_SECURITY_LEVEL = "L1"

#: 采购薄别名**固定**的域码（别名不接受调用方指定域）。
PROCUREMENT_DOMAIN_CODE = "PROCUREMENT"


class KnowledgeEntityHint(CamelModel):
    """实体提示（供应商 / 物料 …）——只做**提示**，不做授权。"""

    type: str = Field(min_length=1, max_length=MAX_ENTITY_HINT_TYPE_LENGTH)
    code: str = Field(min_length=1, max_length=MAX_ENTITY_HINT_CODE_LENGTH)


class KnowledgeScopeRequest(CamelModel):
    """请求侧**收窄**的权限上下文。

    ⚠️ 本对象**只能收窄**服务端推导出的权威域集合，**不能放宽**
    （裁决 3：权威域由服务端从 `CurrentUser` + `data_domain` 推导；
    请求里的 scope 只做交集，不做并集）。
    """

    org_codes: tuple[str, ...] = ()
    entity_types: tuple[str, ...] = ()
    #: **本批不消费**用户级安全等级（用户的安全等级没有权威来源，L-P4-8）
    #: ⇒ 缺省 `None`（= 不指定）；**显式传值也不会被静默忽略**：
    #: 响应里会带一条明确警告（见 `MSG_SCOPE_SECURITY_LEVEL_IGNORED`）。
    max_security_level: str | None = None

    @field_validator("org_codes", "entity_types")
    @classmethod
    def _limitScopeItems(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > MAX_SCOPE_ITEMS:
            raise ValueError(f"scope 项数不得超过 {MAX_SCOPE_ITEMS}")
        return value

    @field_validator("max_security_level")
    @classmethod
    def _checkSecurityLevel(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value not in KNOWLEDGE_SECURITY_LEVELS:
            raise ValueError(
                f"maxSecurityLevel 只能是 {'/'.join(KNOWLEDGE_SECURITY_LEVELS)}，实际：{value}"
            )
        return value


class _KnowledgeQueryBody(CamelModel):
    """请求体的**公共部分**（域之外的一切）。

    拆出基类是为了让主接口与采购薄别名**共用同一套字段与校验**（DRY）：
    别名只是少了 `domain`（域固定），其余语义与边界逐字一致。
    """

    question: str = Field(min_length=1, max_length=MAX_QUESTION_LENGTH)
    channels: tuple[str, ...] | None = None
    entity_hints: tuple[KnowledgeEntityHint, ...] = ()
    top_k: int = Field(default=DEFAULT_TOP_K, ge=1, le=MAX_TOP_K)
    release_id: int | None = Field(default=None, ge=1)
    scope: KnowledgeScopeRequest | None = None

    @field_validator("channels")
    @classmethod
    def _checkChannels(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is None:
            return None
        unknown = [item for item in value if item not in KNOWLEDGE_CHANNELS]
        if unknown:
            raise ValueError(
                f"channels 只允许 {'/'.join(KNOWLEDGE_CHANNELS)}，非法值：{', '.join(unknown)}"
            )
        return value

    def resolvedChannels(self) -> tuple[str, ...]:
        """展开后的通道集合（缺省 = 四路全开）。"""
        return tuple(self.channels) if self.channels else KNOWLEDGE_CHANNELS


class KnowledgeQueryRequest(_KnowledgeQueryBody):
    """`POST /api/v1/semantic/knowledge/query` 的请求体。

    `domain` 是**跨域可用的唯一抓手**（换参数即服务第二个域 ⇒ P6 的扩展性证据）。
    """

    domain: str = Field(min_length=1, max_length=MAX_DOMAIN_CODE_LENGTH)

    @field_validator("domain")
    @classmethod
    def _normalizeDomain(cls, value: str) -> str:
        """去掉首尾空白；**全空白 = 未填**（`min_length=1` 拦不住 `"   "`）。"""
        stripped = value.strip()
        if not stripped:
            raise ValueError("domain 不得为空白")
        return stripped


class ProcurementKnowledgeQueryRequest(_KnowledgeQueryBody):
    """采购薄别名的请求体：**不带 domain**（域固定为采购域）。

    「薄别名 = 只做 domain 固定转发」⇒ 域**根本不出现在请求里**，
    调用方没有任何机会把请求引到别的域。
    """

    def toCanonicalRequest(self) -> KnowledgeQueryRequest:
        """补上固定的域 ⇒ 主接口的请求体。"""
        return KnowledgeQueryRequest(
            domain=PROCUREMENT_DOMAIN_CODE,
            question=self.question,
            channels=self.channels,
            entity_hints=self.entity_hints,
            top_k=self.top_k,
            release_id=self.release_id,
            scope=self.scope,
        )


class KnowledgeReleaseRef(CamelModel):
    """本轮查询**真正用到**的发布版本。

    `degraded=True` ⇒ 这一轮**没有**用发布快照（**空结果 + 显式降级标记**）⇒
    `id` / `version` / `manifest_hash` 必须为空（不许挂一个版本号骗人）。
    """

    id: int | None = None
    version: str | None = None
    manifest_hash: str | None = None
    degraded: bool = True


class KnowledgeEntityRead(CamelModel):
    """命中的实体。"""

    type: str
    code: str
    name: str | None = None


class KnowledgeEvidenceSourceRef(CamelModel):
    """证据的**物理来源**定位（M2-1 契约补全；字段按通道可得性回填，None=无此信息）。

    命名偏差（已登记）：跨板块设计稿的证据里嵌套对象叫 `source{...}`，但本仓
    `KnowledgeEvidenceRead.source: str` 是「发布面出处」（RELEASE/LIVE），二者
    并存 ⇒ 物理来源定位改叫 `sourceRef`，`source` 原样保留（旧调用方零破坏）。

    **I2-B 扩字段**（对齐设计稿 §4.2 各 kind 的「必需溯源字段」）：
    GRAPH 需要节点键/关系类型（`object`/`recordKey`）、`syncBatch`；
    SQL 需要 `datasourceId`/`queryId`/`sqlHash`/`paramsSummary`/`executedAt`；
    DOCUMENT 需要 `documentId`/`chunkId`/`page`/`validAt`；
    METRIC 需要 `period`/`grain`。**按通道可得性回填**（拿不到就是 None，不编）。
    """

    system: str | None = None
    object: str | None = None
    record_key: str | None = None
    document_id: str | None = None
    query_id: str | None = None
    # —— I2-B ——
    page: int | None = None
    chunk_id: str | None = None
    sync_batch: str | None = None
    sql_hash: str | None = None
    params_summary: str | None = None
    executed_at: datetime | None = None
    period: str | None = None
    grain: str | None = None


class KnowledgeEvidenceSecurity(CamelModel):
    """证据的密级与授权范围（DOCUMENT 通道回填；M2-4 文档检索消费）。"""

    level: str | None = None
    scope: tuple[str, ...] = ()


class KnowledgeEntityHintRead(CamelModel):
    """一条实体提示（`type` + `code`），用于证据的「关联对象」。"""

    type: str
    code: str


class KnowledgeEvidenceRead(CamelModel):
    """一条引用（证据）。

    `release_id` / `release_version` 为空 ⇒ 这条引用不是出自发布面
    （本批知识路径**不产生**这种引用 ⇒ `source` 恒为 `RELEASE`）。

    **M2-1 契约补全（加性可选字段，旧调用方零破坏）**：
    - `evidenceId`：**内容确定**（对除自身外的全部字段做 canonical JSON SHA-256，
      未显式提供时自动生成）⇒ 同一证据跨响应 id 相同（可逐条引用、可对账），
      不同证据 id 不同。⚠️ 不能用随机 uuid：ACL 守卫用「两次调用 evidence
      全等」断言访问面不变（entity hints 不得扩权），随机 id 会让这类断言
      永假（CI 净新增 1 条实证，2026-11-20）；
    - `confidence`：[0,1]（RAG 路 = 1 - distance；无分数的通道为 None）；
    - `sourceRef` / `security` / `observedAt` / `validAt` / `ruleVersion`：
      契约字段本批就位，按通道能力**渐进回填**（DOCUMENT 相关随 M2-4）。
    """

    channel: str
    kind: str
    ref: str
    title: str
    #: 一句话结论（§4.2 `summary`）：有可执行结果时含数值；无则 None（不编）
    summary: str | None = None
    snippet: str | None = None
    #: 该证据关联的业务/知识对象（来自 `document_entity_relation`）。
    #: LLM 与前端据此知道「这份文档说的是哪个供应商/本体类/指标」。
    related_entities: tuple["KnowledgeEntityHintRead", ...] = ()
    release_id: int | None = None
    release_version: str | None = None
    source: str = KNOWLEDGE_SOURCE_RELEASE
    evidence_id: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    observed_at: datetime | None = None
    valid_at: datetime | None = None
    rule_version: str | None = None
    security: KnowledgeEvidenceSecurity | None = None
    source_ref: KnowledgeEvidenceSourceRef | None = None

    @model_validator(mode="after")
    def _deriveEvidenceId(self) -> KnowledgeEvidenceRead:
        """内容确定 id：canonical（字段名排序）JSON → SHA-256 → `ev-<hex16>`。"""
        if self.evidence_id:
            return self
        payload = json.dumps(
            self.model_dump(exclude={"evidence_id"}, by_alias=True),
            ensure_ascii=False, sort_keys=True, default=str,
        )
        self.evidence_id = "ev-" + hashlib.sha256(payload.encode()).hexdigest()[:16]
        return self


class ProcurementEntitySourceMappingRead(CamelModel):
    """实体的一条源系统映射（M2-2 `entities/search` 契约，设计稿 §8.3）。"""

    source_system: str
    source_key: str
    source_code: str
    match_rule: str
    effective_date: date | None = None
    expiry_date: date | None = None


class ProcurementEntityMatchRead(CamelModel):
    """一个统一实体（enterprise_key）的解析结果：源映射 + 命中强度。"""

    entity_type: str
    enterprise_key: int
    enterprise_code: str
    name: str | None = None
    match_score: float = Field(ge=0.0, le=1.0)
    source_mappings: list[ProcurementEntitySourceMappingRead] = Field(default_factory=list)


class ProcurementGraphNodeRead(CamelModel):
    """受控邻接视图中的节点（M2-2b，设计稿 §8.3）。"""

    entity_type: str
    key: str
    code: str | None = None
    name: str | None = None


class ProcurementGraphEdgeRead(CamelModel):
    """受控邻接视图中的一条真实边（来自 `GraphTraversalHop`）。"""

    depth: int
    from_type: str
    from_key: str
    from_code: str | None = None
    from_name: str | None = None
    rel_type: str
    to_type: str
    to_key: str
    to_code: str | None = None
    to_name: str | None = None


class ProcurementGraphNeighborsRead(CamelModel):
    """受控邻接响应：nodes[] + edges[] + truncated（自证截断）。

    `effectiveRelationTypes` = 请求 relationTypes ∩ `BUSINESS_RELATION_TYPES`
    （白名单外交互被丢弃时调用方可自证，不会误以为跑了一条被丢弃的路）。
    """

    entity_type: str
    entity_key: str
    max_hops: int
    effective_relation_types: tuple[str, ...] = ()
    nodes: list[ProcurementGraphNodeRead] = Field(default_factory=list)
    edges: list[ProcurementGraphEdgeRead] = Field(default_factory=list)
    truncated: bool = False


class KnowledgeConflictEntryRead(CamelModel):
    """冲突中的一个来源（通道 + 版本 + 观测时间 + 原文片段）——供人工裁决。"""

    channel: str
    snippet: str | None = None
    release_version: str | None = None
    observed_at: datetime | None = None


class KnowledgeConflictRead(CamelModel):
    """一条证据冲突（设计稿 §4.3：**输出冲突提示与来源时间，不让模型自选**）。"""

    key: str
    reason: str
    channels: tuple[str, ...] = ()
    entries: tuple[KnowledgeConflictEntryRead, ...] = ()


class KnowledgeRouteResultRead(CamelModel):
    """单路的执行结果 + 降级痕迹。

    `degraded_reason` 与 `warnings` 是**必须存在**的字段（可为 None / 空）：
    「四路都可单独返回」意味着调用方能区分「查到了 0 条」与「这条路挂了」。
    """

    channel: str
    status: str
    item_count: int = 0
    degraded_reason: str | None = None
    warnings: tuple[str, ...] = ()


class KnowledgeQueryResponse(CamelModel):
    """`POST /api/v1/semantic/knowledge/query` 的响应体（裸 DTO，无信封）。"""

    question: str
    domain: str
    release: KnowledgeReleaseRef
    answer_draft: str = ""
    entities: tuple[KnowledgeEntityRead, ...] = ()
    evidence: tuple[KnowledgeEvidenceRead, ...] = ()
    route_results: tuple[KnowledgeRouteResultRead, ...] = ()
    #: I2-B：跨通道证据冲突（空 = 无冲突）
    conflicts: tuple[KnowledgeConflictRead, ...] = ()
    warnings: tuple[str, ...] = ()

# --- M3-3 采购 Agent 场景目录（§8.4 GET /procurement/agents/catalog） --------


class ProcurementAgentToolRead(CamelModel):
    """目录条目里的工具能力摘要——**不暴露 handlerRef**（能力面非实现面）。"""

    name: str
    description: str | None = None
    data_object: str | None = None
    data_layers: tuple[str, ...] = ()


class ProcurementAgentCatalogEntryRead(CamelModel):
    """一个采购场景 Agent 的目录条目。"""

    agent_code: str
    agent_name: str
    description: str | None = None
    status: str
    runnable: bool = False
    tool: ProcurementAgentToolRead | None = None
