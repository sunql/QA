"""采购术语目录（M1-1.5 决策 SSOT）。

为什么要有这个模块
------------------
V0.3 实施计划 1.5「采购术语补齐（``term_dictionary``）：寻源/询价/报价/定标/到货/
收货/零库存供应商等」。术语字典会**原样注入每次 NL2SQL 计划的 prompt**（无分页、
无截断，见 ``app/services/term_dictionary_service.py``）⇒ 词条的定义就是系统的
**业务口径**，不能散落在脚本里、也不能靠人手逐个在后台录入（会漂移）。

本目录是**纯数据 + 纯函数**（不吃 DB、不吃 session）：
- 应用脚本 ``scripts/apply_procurement_terms.py`` 从这里取数 ⇒ 不手抄；
- 单测从这里断言**覆盖度与不变量**（每个计划点名的词都在、无源词条不许瞎挂类）；
- 集成测试拿这份目录逐条走**真实写入口**（POST /api/v1/term-dictionary）。

三条硬规则（都来自本项目踩过的坑）
--------------------------------
1. **无源数据的词条不许挂 ``mappedClassName``**：挂了类就等于告诉 LLM「去查这张表」，
   而源侧根本没有数据 ⇒ 会诱导出「0 行 = 真的是 0」的假答案（比查不到更坏）。
2. **业务没确认的口径要如实写明「未确认」**：C3.3（哪张表是权威价）在问卷里
   是空白（``docs/update/1118/M0/answers/conflicts-and-degradations.md`` G-04）。
3. **「到货」与「收货」是两件事**：非零库存供应商先到货、再质检、再收货；
   零库存供应商没有到货单。指标口径一律以**收货日期 / 收货数量**为准
   （A1.2 / A3.2 答复）。

词表血统：映射的类名都是 **Lineage B**（``DIM_*`` / ``DWD_*``，开发库 5433 /
生产）；测试库 5434 是 Lineage A ⇒ 集成测试必须**先种出这些类**再用写入口
（写入口会校验类存活）。
"""

from __future__ import annotations

from dataclasses import dataclass

V03_TERM_COUNT = 7
"""V0.3 计划 1.5 **点名**的术语数（寻源/询价/报价/定标/到货/收货/零库存供应商）。

目录里的词条可以多于它（计划原文有「等」），但**不能少**
（少一个 = 计划项没交付）。
"""


@dataclass(frozen=True, slots=True)
class ProcurementTerm:
    """一条要落进 ``term_dictionary`` 的采购术语。

    字段与 ``TermDictionaryCreate`` 一一对应（长度约束由写入口再校验一遍）：
    ``term`` ≤ 100、``definition`` / ``formulaHint`` ≤ 2000。

    - ``hasSourceData``：``False`` ⇒ 源系统**没有**这类数据（问卷 C4.2 等）
      ⇒ 此时 ``mappedClassName`` 必须为空（硬规则 1）；
    - ``evidence``：定义出自哪里（问卷题号 / 数据标准 / 本体实测），
      写进仓内文档与变更记录，便于复核「凭什么这么说」。
    """

    term: str
    definition: str
    hasSourceData: bool
    evidence: str
    mappedClassName: str | None = None
    mappedPropertyName: str | None = None
    formulaHint: str | None = None


PROCUREMENT_TERMS: tuple[ProcurementTerm, ...] = (
    ProcurementTerm(
        term="寻源",
        definition=(
            "为采购需求寻找、筛选并确定供应商的完整过程"
            "（需求提出 → 邀请供应商 → 询比价 → 定标）。"
            "这是业务活动概念：**当前源系统没有寻源 / 招标 / 询价活动数据**"
            "（问卷 C4.2 答复「没有」）⇒ 库里查不到寻源单、邀请名单、报价截止、定标结论，"
            "不要用订单或报价表反推「寻源过程」。"
        ),
        hasSourceData=False,
        evidence="业务问卷 C（C4.2「寻源/招标/询价活动：没有」）",
    ),
    ProcurementTerm(
        term="询价",
        definition=(
            "向候选供应商发出询价单并收集报价的活动，常与「比价」连用。"
            "**当前无源系统数据**（问卷 C4.2）；且系统首期只生成询价单**草稿**、"
            "不外发给供应商（问卷 B3.1 / 决策 D4）⇒ 询价过程与询价单均不在库内。"
        ),
        hasSourceData=False,
        evidence="业务问卷 C4.2 + B3.1 + 决策 D4（询价草稿 only）",
    ),
    ProcurementTerm(
        term="定标",
        definition=(
            "比价完成后确定中标供应商与成交价的决策动作，是询价活动的终点。"
            "**当前无源系统数据**（问卷 C4.2 答复「没有」）⇒ 库内没有定标结论表，"
            "不得用报价表或订单反推「定标结果」。"
        ),
        hasSourceData=False,
        evidence="业务问卷 C4.2",
    ),
    ProcurementTerm(
        term="报价",
        definition=(
            "供应商对物料给出的价格。库内有**三类**候选载体：报价单 "
            "DWD_QUOTATION（及明细 DWD_QUOTATION_LINE，当前已下线）、"
            "供应商报价清单 DWD_SUPPLIER_PRICE_LIST_HEADER / _DEL、"
            "年度报价 DWD_PURCHASE_QUOTATION_YEARLY_DTL。"
            "本系统把「报价」业务对象的头类定为 DWD_SUPPLIER_PRICE_LIST_HEADER；"
            "**哪张表是权威价尚未经业务确认**（问卷 C3.3 空白）⇒ 回答价格类问题时"
            "要说明所依据的表，不要把它当成唯一事实。"
        ),
        hasSourceData=True,
        mappedClassName="DWD_SUPPLIER_PRICE_LIST_HEADER",
        formulaHint=(
            "候选：DWD_SUPPLIER_PRICE_LIST_HEADER（+ _DEL 明细）、"
            "DWD_PURCHASE_QUOTATION_YEARLY_DTL（年度报价，含不含税列 UNIT_PRICE_EX_TAX）、"
            "DWD_QUOTATION_LINE（已下线）。"
            "权威价口径待业务确认（C3.3）；跨表比价前先确认同一物料同一时间口径。"
        ),
        evidence="问卷 C4（源表清单）+ C3.3（空白）+ M1-1.4 QUOT 对象处置",
    ),
    ProcurementTerm(
        term="到货",
        definition=(
            "货物到达公司收货地点的动作，记录在到货单宽表 DWD_ARRIVAL_ORDER_DTL"
            "（到货单号 / 行号 / 供应商 / 到货日期）。"
            "**到货 ≠ 收货**：非零库存供应商先建到货单、再质检、再建收货单；"
            "零库存供应商没有到货单（采购订单直接驱动收货单）。"
            "统计「准时」等指标一律以**收货日期 / 收货数量**为准，不要用到货日期。"
        ),
        hasSourceData=True,
        mappedClassName="DWD_ARRIVAL_ORDER_DTL",
        formulaHint=(
            "到货状态列 YPTHFLG_0：1=草稿、2=到货确认（可质检）、3=质量确认（可收货入库）；"
            "仅适用于非零库存供应商。KPI 口径**不使用**本表的日期。"
        ),
        evidence="数据标准 20260828 第 3 节 + 问卷 A1.2（准时以收货时间为准）",
    ),
    ProcurementTerm(
        term="收货",
        definition=(
            "货物验收入库的动作，记录在收货宽表 DWD_GOODS_RECEIPT_DTL / "
            "DWD_GOODS_RECEIPT_LINE（收货单号 / PO 号 / PO 行号 / 收货日期 / 数量）。"
            "采购指标的日期与数量口径以**收货日期与收货数量**为准"
            "（准时交付、逾期、采购周期、三单匹配）。注意别与到货单混用。"
        ),
        hasSourceData=True,
        mappedClassName="DWD_GOODS_RECEIPT_DTL",
        formulaHint=(
            "统计期间按 DWD_GOODS_RECEIPT_DTL.RCV_DATE 过滤；数量用 RCV_QTY_PUU；"
            "默认排除内部交易（INTER_COM_CODE='1' AND INTER_SITE_CODE='1'），"
            "生产型物料再限定 DIM_IMATERIAL.TCLCOD_0 IN ('A02','A03','A04','A05')"
            "（见「供货量」词条）。"
        ),
        evidence="问卷 A1.2 / A3.2 + 数据标准 20260828",
    ),
    ProcurementTerm(
        term="零库存供应商",
        definition=(
            "收货模式为「零库存」的供应商：DIM_SUPPLIER.YPTHFLGM_0 = 2（1 = 非零库存）。"
            "零库存供应商不走到货单 / 质检，采购订单直接驱动收货单，到车间后由领用系统"
            "自动入库出库。**统计订单完成率、准时交付率、逾期率时必须按该标志分组**，"
            "混算会同时虚增 / 虚减两侧。"
        ),
        hasSourceData=True,
        mappedClassName="DIM_SUPPLIER",
        mappedPropertyName="YPTHFLGM_0",
        formulaHint=(
            "分组条件：DIM_SUPPLIER.YPTHFLGM_0 = 1（非零库存）/ 2（零库存）。"
            "⚠️ 数值方向容易记反：1 = 非零库存，2 = 零库存。"
        ),
        evidence=("数据标准 20260828（YPTHFLGM_0 语义）+ feat-supplier-receipt-mode 变更记录 + 问卷 A5.1"),
    ),
    ProcurementTerm(
        term="来料检验",
        definition=(
            "对到货 / 收货物料做质量检验（IQC）的活动。"
            "**当前本体与源表都没有**（不存在 DWD_INCOMING_INSPECTION 类）⇒ "
            "质量类指标（来料不良率、一次合格率）首期不可查，不要给出数值。"
        ),
        hasSourceData=False,
        evidence="M1-1.4 核对表 §7 欠账 1（质量域三指标无源）+ 开发库本体实测",
    ),
    ProcurementTerm(
        term="不合格品处理",
        definition=(
            "对检验不合格来料 / 半成品的评审与处置（退货、挑选、让步接收、索赔）。"
            "**当前本体与源表都没有**（不存在 DWD_NCR 类）⇒ 首期不可查。"
        ),
        hasSourceData=False,
        evidence="M1-1.4 核对表 §7 欠账 1 + 开发库本体实测",
    ),
    ProcurementTerm(
        term="呆滞料",
        definition=(
            "长期未发生出入库的库存物料。**当前本体未建对应类、源侧也未接入库存流水**"
            "⇒ 按呆滞口径的统计首期不可查。"
        ),
        hasSourceData=False,
        evidence="开发库本体实测（无库存类）+ 问卷 C4 源表清单",
    ),
)


# ---------------------------------------------------------------------------
# 纯函数（供脚本 / 测试 / 文档取数）
# ---------------------------------------------------------------------------


def termNames() -> tuple[str, ...]:
    """目录里的全部术语名（稳定序 = 声明序）。"""
    return tuple(spec.term for spec in PROCUREMENT_TERMS)


def sourceBackedTerms() -> tuple[ProcurementTerm, ...]:
    """有源数据、因而可以挂本体类的词条。"""
    return tuple(spec for spec in PROCUREMENT_TERMS if spec.hasSourceData)


def unsourcedTerms() -> tuple[ProcurementTerm, ...]:
    """源系统没有数据的词条（必须不挂类 —— 硬规则 1）。"""
    return tuple(spec for spec in PROCUREMENT_TERMS if not spec.hasSourceData)


def mappedClassNames() -> tuple[str, ...]:
    """目录里出现过的本体类名（去重、稳定序）—— 集成测试据此种类。"""
    seen: dict[str, None] = {}
    for spec in PROCUREMENT_TERMS:
        if spec.mappedClassName:
            seen.setdefault(spec.mappedClassName, None)
    return tuple(seen)


def termSpec(term: str) -> ProcurementTerm | None:
    """按术语名取词条；不存在返回 ``None``（不抛 —— 调用方决定文案）。"""
    return next((spec for spec in PROCUREMENT_TERMS if spec.term == term), None)
