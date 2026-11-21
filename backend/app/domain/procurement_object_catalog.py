"""采购本体对象目录（M1-1.4 决策 SSOT）。

为什么要有这个模块
------------------
V0.3 详细设计方案 §3.2 列了 **10 个采购本体对象**，但「对象清单」此前只存在于
两份**会漂移**的地方：设计文档（docx）与本地开发库的治理表。M1-1.4 要逐条「对现状」
⇒ 必须有一份**仓内可执行、可单测、可被迁移对齐**的目录：

- 核对表（文档）从这里**取数**，不手抄 ⇒ 文档与代码不会各说各话；
- 迁移 ``alembic/versions/0158_procurement_object_reconciliation.py`` **自包含**
  （不 import ``app.*``，见仓内迁移风格），因此它与本目录的一致性由
  **单测里的 AST 漂移守卫**钉住（不是运行时 import）。

本模块是**纯数据 + 纯函数**（不吃 DB、不吃 session）⇒ 可直接单测。

两套本体血统（务必先读）
------------------------
仓内存在**两套互不相容**的本体血统，M1-1.4 的核对以 **Lineage B** 为准：

============  ==========================================  ===========================
血统          类名 / 源表词表                              所在
============  ==========================================  ===========================
Lineage A     业务名类：Supplier / ItemMaster / …，源表      ``backend/seed_ontology.py``
              BPSUPPLIER / ITMMASTER / PORDER              （仓内 SSOT）+ 测试库 5434
Lineage B     表名类：DIM_SUPPLIER / DWD_PURCHASE_ORDER_DTL  开发库 5433（生产由此导入）
              等，源表与类名同词表
============  ==========================================  ===========================

⇒ 本目录里所有类名 / 源表名都是 **Lineage B 词表**；迁移按**类名**锚定 ⇒
在 Lineage A 环境（测试库）里**整批 no-op**（找不到类 ⇒ 什么都不改）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

PROCUREMENT_DOMAIN_CODE = "PROCUREMENT"
"""采购域编码（``data_domain.code``；域归属**只**由 ``business_object.primary_domain_id`` 承担）。"""

V03_OBJECT_COUNT = 10
"""V0.3 §3.2 采购本体对象总数（核对表的分母；改动必须同步设计文档）。"""


class ObjectDisposition(str, Enum):
    """一个采购本体对象的处置口径。

    取值语义（**穷举** ⇒ 核对表不会漏项）：

    - ``EXISTING``：BO 与头类都对 ⇒ 现状正确，不动；
    - ``ADD_BUSINESS_OBJECT``：缺 BO，头类已活跃 ⇒ 补 BO；
    - ``REVIVE_AND_ADD``：头类或 KPI 锚点是墓碑 ⇒ 复活类 **且** 补 BO；
    - ``REVIVE_ONLY``：BO 已有，但 KPI 口径锚点是墓碑 ⇒ 只复活类（不另建 BO）；
    - ``REPOINT``：已有 BO 的头类指错 ⇒ 改指（不新建 BO）；
    - ``NOT_BUILT``：**明确不建**本体对象（必须写清理由与替代承载面）。
    """

    EXISTING = "EXISTING"
    ADD_BUSINESS_OBJECT = "ADD_BUSINESS_OBJECT"
    REVIVE_AND_ADD = "REVIVE_AND_ADD"
    REVIVE_ONLY = "REVIVE_ONLY"
    REPOINT = "REPOINT"
    NOT_BUILT = "NOT_BUILT"


@dataclass(frozen=True, slots=True)
class BusinessObjectSpec:
    """要补进 ``business_object`` 的一行。

    ``headerClassName`` 必须是**活跃**类的类名（墓碑类不在活跃模型里 ⇒
    编译器解析不出头类 ⇒ BO 进不了切片）。
    """

    code: str
    name: str
    headerClassName: str
    graphLabel: str
    description: str


@dataclass(frozen=True, slots=True)
class RepointSpec:
    """改指：某个已有 BO 的头类换成另一个类（不新建 BO）。"""

    businessObjectCode: str
    fromClassName: str
    toClassName: str
    rationale: str


@dataclass(frozen=True, slots=True)
class NotBuiltSpec:
    """明确不建的对象 / 对象族（写清理由与替代承载面）。"""

    objectKey: str
    title: str
    reason: str
    carriedBy: str


@dataclass(frozen=True, slots=True)
class ProcurementObjectSpec:
    """V0.3 §3.2 的一个采购本体对象 + 处置。

    字段语义：

    - ``businessObjectCode``：这个对象**对应**的 BO 码（``None`` = 不建 BO）；
    - ``headerClassName``：该 BO 的**头类**（``None`` = 不建 BO）；
    - ``revivedClassNames``：为保住本对象的 KPI 口径而要**复活**的墓碑类；
    - ``addedBusinessObjectCodes``：本对象要**新增**的 BO 码（明细行对象另立）。
    """

    objectKey: str
    title: str
    disposition: ObjectDisposition
    businessObjectCode: str | None
    headerClassName: str | None
    revivedClassNames: tuple[str, ...]
    addedBusinessObjectCodes: tuple[str, ...]
    rationale: str


# ---------------------------------------------------------------------------
# 10 个对象的处置（V0.3 §3.2 逐条）
# ---------------------------------------------------------------------------

PROCUREMENT_OBJECT_SPECS: tuple[ProcurementObjectSpec, ...] = (
    ProcurementObjectSpec(
        objectKey="SUPPLIER",
        title="供应商",
        disposition=ObjectDisposition.EXISTING,
        businessObjectCode="SUPPLIER",
        headerClassName="DIM_SUPPLIER",
        revivedClassNames=(),
        addedBusinessObjectCodes=(),
        rationale=(
            "BO SUPPLIER → DIM_SUPPLIER（活跃，源侧实测 3,500 行）⇒ 现状正确；"
            "该类同时是 KPI_SUPPLIER_MAINTENANCE_RATIO 的唯一锚点"
            "（ontology_metric_source class_id=3）"
        ),
    ),
    ProcurementObjectSpec(
        objectKey="MATERIAL",
        title="物料",
        disposition=ObjectDisposition.EXISTING,
        businessObjectCode="MATERIAL",
        headerClassName="DIM_IMATERIAL",
        revivedClassNames=(),
        addedBusinessObjectCodes=(),
        rationale=(
            "BO MATERIAL → DIM_IMATERIAL（活跃，源侧实测 350,922 行）⇒ 现状正确；"
            "DWD_ITEM_FACILITY（818,347 行）是它的工厂视图，不是独立对象"
        ),
    ),
    ProcurementObjectSpec(
        objectKey="PURCHASE_REQUEST",
        title="采购申请",
        disposition=ObjectDisposition.REVIVE_AND_ADD,
        businessObjectCode="PR",
        headerClassName="DWD_PURCHASE_REQUISITION_LINE",
        revivedClassNames=(
            "DWD_PURCHASE_REQUISITION_LINE",
            "DWD_REQUISITION_ORDER_LINK",
        ),
        addedBusinessObjectCodes=("PR",),
        rationale=(
            "请购行 181,201 行有数据，但类是墓碑（valid_to=2026-09-19）⇒ "
            "复活 + 补 BO PR；请购行与 PO↔请购关联表（DWD_REQUISITION_ORDER_LINK，"
            "138,118 行）都是 KPI_PURCHASE_CYCLE_TIME 的锚点（关联表只覆盖 13.8% 的订单行）"
        ),
    ),
    ProcurementObjectSpec(
        objectKey="QUOTATION",
        title="报价",
        disposition=ObjectDisposition.ADD_BUSINESS_OBJECT,
        businessObjectCode="QUOT",
        headerClassName="DWD_SUPPLIER_PRICE_LIST_HEADER",
        revivedClassNames=(),
        addedBusinessObjectCodes=("QUOT",),
        rationale=(
            "报价清单头 17,749 行有数据（类已活跃）⇒ 只缺 BO；"
            "询价 / 寻源（RFQ）按 C4.2 业务确认「没有」⇒ 不建 RFQ"
        ),
    ),
    ProcurementObjectSpec(
        objectKey="PURCHASE_ORDER",
        title="采购订单",
        disposition=ObjectDisposition.REVIVE_AND_ADD,
        businessObjectCode="PO",
        headerClassName="DWD_PURCHASE_ORDER_DTL",
        revivedClassNames=("DWD_PURCHASE_ORDER_LINE",),
        addedBusinessObjectCodes=("POL",),
        rationale=(
            "订单宽表 DWD_PURCHASE_ORDER_DTL（1,023,442 行）是现有 BO PO 的头类，"
            "保持不变；但**订单行** DWD_PURCHASE_ORDER_LINE（927,648 行）是 6 个采购 KPI 的"
            "口径锚点（NUMERATOR / DENOMINATOR / FILTER / BASE）且是墓碑 ⇒ "
            "复活它 + 补 BO POL（订单行）"
        ),
    ),
    ProcurementObjectSpec(
        objectKey="RECEIPT",
        title="收货",
        disposition=ObjectDisposition.REVIVE_ONLY,
        businessObjectCode="GR",
        headerClassName="DWD_GOODS_RECEIPT_DTL",
        revivedClassNames=("DWD_GOODS_RECEIPT_LINE",),
        addedBusinessObjectCodes=(),
        rationale=(
            "BO GR → DWD_GOODS_RECEIPT_DTL（活跃，3,977,714 行）保持不变；"
            "收货行 DWD_GOODS_RECEIPT_LINE（3,558,008 行）是 OTD / 逾期率 / "
            "延误天数 / 采购周期的口径锚点且是墓碑 ⇒ 复活它"
            "（收货行是收货对象的明细，不另立业务对象）"
        ),
    ),
    ProcurementObjectSpec(
        objectKey="CONTRACT",
        title="采购合同",
        disposition=ObjectDisposition.NOT_BUILT,
        businessObjectCode=None,
        headerClassName=None,
        revivedClassNames=(),
        addedBusinessObjectCodes=(),
        rationale=(
            "**源缺口**（V0.3 明写「不虚构源表」）：DWD_PURCHASE_CONTRACT 不存在"
            "（ORA-00942）、SRM order_contract 0 行、C4.1 业务确认「采购合同：没有」"
            "⇒ 不建本体类、不建源绑定（决策 D-12）"
        ),
    ),
    ProcurementObjectSpec(
        objectKey="INVOICE",
        title="采购发票",
        disposition=ObjectDisposition.REPOINT,
        businessObjectCode="INVOICE",
        headerClassName="DWD_PURCHASE_INVOICE",
        revivedClassNames=("DWD_PURCHASE_INVOICE", "DWD_PURCHASE_INVOICE_LINE"),
        addedBusinessObjectCodes=(),
        rationale=(
            "BO INVOICE 现指 DWD_AP_INVOICE_DTL —— 该表只有 **46 行**且属性是"
            "FILE_ID / FILE_URL / FILE_SUFFIX / EXTRACT_TIME / TENANT_ID"
            "（文件抽取旁表，时间跨度仅 2026-09-02 → 2026-10-09）⇒ 头类指错；"
            "采购发票主体是 DWD_PURCHASE_INVOICE（38,323 行，头）/ _LINE（2,486,311 行，行）"
            "⇒ 改指（决策 D-15）+ 复活两个墓碑类"
        ),
    ),
    ProcurementObjectSpec(
        objectKey="PAYMENT",
        title="采购付款",
        disposition=ObjectDisposition.REVIVE_AND_ADD,
        businessObjectCode="PAY",
        headerClassName="DWD_SUPPLIER_PAYMENT",
        revivedClassNames=("DWD_SUPPLIER_PAYMENT",),
        addedBusinessObjectCodes=("PAY",),
        rationale=(
            "付款 33,576 行有数据，但类是墓碑 ⇒ 复活 + 补 BO PAY；"
            "它是 KPI_PAYMENT_ON_TIME 的唯一锚点（BANK_DATE 填充 0 ⇒ 口径用 BILL_DATE）"
        ),
    ),
    ProcurementObjectSpec(
        objectKey="SUPPLIER_PERFORMANCE",
        title="供应商绩效",
        disposition=ObjectDisposition.NOT_BUILT,
        businessObjectCode=None,
        headerClassName=None,
        revivedClassNames=(),
        addedBusinessObjectCodes=(),
        rationale=(
            "V0.3 标注 Derived ⇒ **不建**本体对象；其三个指标"
            "（来料不良率 / 一次合格率 / NCR 率）的技术形态缺失"
            "（DWD_INCOMING_INSPECTION、DWD_NCR 在 schema_cache 里都不存在）"
            "⇒ 登记为质量域欠账（单独立项）"
        ),
    ),
)


# ---------------------------------------------------------------------------
# 复活名单：被 ontology_metric_source 引用的墓碑类
# ---------------------------------------------------------------------------
#
# 为什么必须复活（口径不丢）：
#   `ontology_metric_source` 用 **class_id** 引用类；编译器遇到**墓碑类**的源引用
#   会**整条丢弃**（groupMetricSources）⇒ 指标只剩一半口径（例如 OTD 只剩分母、
#   三单匹配只剩 FILTER）⇒ **静默口径漂移**。
#   而「复活」必须**原地**做（清 valid_to）—— 若改成新建 v2 行，class_id 会变，
#   指标源仍指向旧 id ⇒ 照样被丢弃。
#
# 7 个类 = `ontology_metric_source` 里出现的**全部**采购域墓碑类
# （实测：metric 2/3/4/5/6/7/8/9 的 source class 全在这 7 个里）。

REVIVED_CLASS_NAMES: tuple[str, ...] = (
    "DWD_GOODS_RECEIPT_LINE",
    "DWD_PURCHASE_INVOICE",
    "DWD_PURCHASE_INVOICE_LINE",
    "DWD_PURCHASE_ORDER_LINE",
    "DWD_PURCHASE_REQUISITION_LINE",
    "DWD_REQUISITION_ORDER_LINK",
    "DWD_SUPPLIER_PAYMENT",
)


# ---------------------------------------------------------------------------
# 已经是活跃类、且充当 BO 头类的类（Lineage B；实测自开发库 5433）
# ---------------------------------------------------------------------------
#
# BO 的头类**必须**在活跃模型里：墓碑类不在活跃模型 ⇒ 编译器解析不出
# ``headerClassName`` ⇒ 该 BO 进不了域切片 ⇒ 补 BO 前必须先复活（或本来就活跃）。

ACTIVE_HEADER_CLASSES: tuple[str, ...] = (
    "DWD_ARRIVAL_ORDER_DTL",
    "DWD_GOODS_RECEIPT_DTL",
    "DWD_PURCHASE_ORDER_DTL",
    "DWD_SUPPLIER_PRICE_LIST_HEADER",
    "DIM_IMATERIAL",
    "DIM_SUPPLIER",
)


# ---------------------------------------------------------------------------
# 新增业务对象
# ---------------------------------------------------------------------------

NEW_BUSINESS_OBJECTS: tuple[BusinessObjectSpec, ...] = (
    BusinessObjectSpec(
        code="PR",
        name="采购申请",
        headerClassName="DWD_PURCHASE_REQUISITION_LINE",
        graphLabel="DWD_PURCHASE_REQUISITION_LINE",
        description="内部采购需求申请（请购单行）",
    ),
    BusinessObjectSpec(
        code="QUOT",
        name="报价",
        headerClassName="DWD_SUPPLIER_PRICE_LIST_HEADER",
        graphLabel="DWD_SUPPLIER_PRICE_LIST_HEADER",
        description="供应商提供的报价（报价清单头）",
    ),
    BusinessObjectSpec(
        code="POL",
        name="采购订单行",
        headerClassName="DWD_PURCHASE_ORDER_LINE",
        graphLabel="DWD_PURCHASE_ORDER_LINE",
        description="采购订单中的具体物料明细（采购 KPI 的口径锚点）",
    ),
    BusinessObjectSpec(
        code="PAY",
        name="采购付款",
        headerClassName="DWD_SUPPLIER_PAYMENT",
        graphLabel="DWD_SUPPLIER_PAYMENT",
        description="对供应商的付款交易",
    ),
)


# ---------------------------------------------------------------------------
# 改指
# ---------------------------------------------------------------------------

REPOINTED_BUSINESS_OBJECTS: tuple[RepointSpec, ...] = (
    RepointSpec(
        businessObjectCode="INVOICE",
        fromClassName="DWD_AP_INVOICE_DTL",
        toClassName="DWD_PURCHASE_INVOICE",
        rationale=(
            "DWD_AP_INVOICE_DTL 是文件抽取旁表（46 行 / FILE_* 属性）；"
            "采购发票主体在 DWD_PURCHASE_INVOICE（38,323 行）"
        ),
    ),
)


# ---------------------------------------------------------------------------
# 明确不建（含对象族）
# ---------------------------------------------------------------------------

NOT_BUILT_OBJECTS: tuple[NotBuiltSpec, ...] = (
    NotBuiltSpec(
        objectKey="CONTRACT",
        title="采购合同",
        reason=(
            "源缺口：DWD_PURCHASE_CONTRACT 不存在（ORA-00942）、SRM order_contract 0 行、"
            "C4.1 业务确认「采购合同：没有」⇒ V0.3 明写「不虚构源表」"
        ),
        carriedBy=(
            "文档目录：document_catalog.document_type='CONTRACT' + "
            "document_entity_relation.relation_type='CONTRACT'"
            "（枚举已有 DocumentType.CONTRACT）；"
            "⚠️ 实测当前 0 份合同文档、0 条合同关联 ⇒ 首期**不可查**，登记为欠账"
        ),
    ),
    NotBuiltSpec(
        objectKey="RFQ",
        title="询价单 / 寻源",
        reason=(
            "C4.2 业务确认「寻源 / 招标 / 询价：没有」；"
            "DWD_QUOTATION / DWD_QUOTATION_LINE 是**空表**（0 行）⇒ 不建"
        ),
        carriedBy="无（首期不支持询价 / 寻源；D4 询价草稿也不外发、不写 ERP）",
    ),
    NotBuiltSpec(
        objectKey="SUPPLIER_PERFORMANCE",
        title="供应商质量绩效",
        reason=(
            "V0.3 标注 Derived；三个质量指标的技术形态缺失"
            "（DWD_INCOMING_INSPECTION / DWD_NCR 不存在）⇒ 无可绑定的源"
        ),
        carriedBy="质量域的来料检验 / 不合格处理对象（单独立项）",
    ),
    NotBuiltSpec(
        objectKey="OTHER_SOURCED_TABLES",
        title="其它采购相关源表（未建类）",
        reason=(
            "A8 证据 §E 列出 45 张采购相关但**无有效类**的源表"
            "（ADS_ / DWS_ / ODS_ERP_ / ODS_SRM_ / 退货 / 小额采购等）"
            "⇒ 它们是**汇总层 / 其它系统域**的表，不是 V0.3 的 10 个对象"
        ),
        carriedBy="单独立项（按需建类，不在 M1-1.4 范围）",
    ),
)


# ---------------------------------------------------------------------------
# 纯函数辅助
# ---------------------------------------------------------------------------


def revivedClassNames() -> tuple[str, ...]:
    """要复活的墓碑类名（升序，稳定序）。"""
    return tuple(sorted(REVIVED_CLASS_NAMES))


def newBusinessObjectCodes() -> tuple[str, ...]:
    """要新增的 BO 码（升序，稳定序）。"""
    return tuple(sorted(spec.code for spec in NEW_BUSINESS_OBJECTS))


def isRevivedClass(className: str) -> bool:
    """该类是否在复活名单里。"""
    return className in REVIVED_CLASS_NAMES


def allHeaderClassNames() -> tuple[str, ...]:
    """10 个对象涉及的全部 BO 头类（既有 BO + 新增 BO），升序。"""
    names = {
        spec.headerClassName
        for spec in PROCUREMENT_OBJECT_SPECS
        if spec.headerClassName
    }
    names |= {spec.headerClassName for spec in NEW_BUSINESS_OBJECTS}
    return tuple(sorted(names))


def businessObjectHeaderClasses() -> dict[str, str]:
    """BO 码 → 头类名（既有 BO + 新增 BO + 改指后的目标）。"""
    mapping: dict[str, str] = {}
    for spec in PROCUREMENT_OBJECT_SPECS:
        if spec.businessObjectCode and spec.headerClassName:
            mapping[spec.businessObjectCode] = spec.headerClassName
    for spec in NEW_BUSINESS_OBJECTS:
        mapping[spec.code] = spec.headerClassName
    for spec in REPOINTED_BUSINESS_OBJECTS:
        mapping[spec.businessObjectCode] = spec.toClassName
    return mapping


def objectSpec(objectKey: str) -> ProcurementObjectSpec | None:
    """按 V0.3 对象名取处置规格（未知 ⇒ None）。"""
    for spec in PROCUREMENT_OBJECT_SPECS:
        if spec.objectKey == objectKey:
            return spec
    return None
