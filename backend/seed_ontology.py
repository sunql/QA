"""预置本体（Ontology）种子数据：基于 Sage X3 业务库 17 张核心表。

覆盖表（业务数据库信息.md 第2点 + ROUOPE + 收货单据4表 + 关键表4表 + 采购订单2表）：
  ITMMASTER(物料)  BPCUSTOMER(客户)  BPARTNER(合作伙伴)  BPSUPPLIER(供应商)
  BOM(物料清单)    BOMD(BOM明细)     ITMFACILIT(物料地点)  FACILITY(地点)  ROUOPE(工艺工序)
  YPRECEIPT(到货单) YPRECEIPTD(到货明细) PRECEIPT(收货单/入库单) PRECEIPTD(收货明细)
  PREQUISD(采购需求明细) PREQUISO(请购订单关联) PPRICFICH(供应商价格单) PPRICLIST(供应商价格明细)
  PPRICCONF(供应商价格配置) PORDER(采购订单) PORDERQ(采购订单明细)

每类录入主要字段（主键/外键/SQL样例字段/核心业务字段），跳过 DIE/CCE/INVDTA/DISCRG/CLCAMT/DCGVAL 等扩展槽位。
外键关系按 Sage X3 命名约定 + SQL JOIN 推断（库内无声明 PK/FK 约束）。

幂等：按 source_table 复用类、按 (class_id, property_name) 跳过已有属性。
PG 写完后同步 Neo4j 本体图（20 类 + 属性 + HAS_PROPERTY/REFERENCES 关系，幂等 MERGE）。

当前仅预置类与属性，暂无指标（Metric）种子数据；如后续补充，需同步扩展 _syncToNeo4j 的
DERIVED_FROM 关系处理。

运行: uv run python seed_ontology.py
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy import select

from app.domain.models import OntologyClass, OntologyJoin, OntologyProperty
from app.infrastructure import neo4j_client as neo4j
from app.infrastructure.database import getEngine, getSessionFactory
from app.services.ontology_service import makeJoinKey

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("seed_ontology")

# =============================================================================
# 类定义：source_table -> 元数据
# =============================================================================
CLASSES = [
    {
        "class_name": "ItemMaster",
        "class_alias": "物料",
        "source_table": "ITMMASTER",
        "description": "物料/产品主数据表，承载物料编码、描述、分类、单位、状态等核心属性。",
    },
    {
        "class_name": "Customer",
        "class_alias": "客户",
        "source_table": "BPCUSTOMER",
        "description": "客户信息表，记录客户编码、名称、类型、开票与地址等。",
    },
    {
        "class_name": "BusinessPartner",
        "class_alias": "合作伙伴",
        "source_table": "BPARTNER",
        "description": "合作伙伴表，作为客户/供应商/承运人的统一主档，含税号、国家、行业等。",
    },
    {
        "class_name": "Supplier",
        "class_alias": "供应商",
        "source_table": "BPSUPPLIER",
        "description": "供应商信息表，记录供应商编码、名称、类型、开票与付款等。",
    },
    {
        "class_name": "Carrier",
        "class_alias": "承运人",
        "source_table": "BPCARRIER",
        "description": "承运人主档，记录承运人编码、名称、地址、运输方式、税号等。",
    },
    {
        "class_name": "BOM",
        "class_alias": "物料清单",
        "source_table": "BOM",
        "description": "BOM 表，一个物料对应若干替代 BOM；BOMALT_0||ITMREF_0||BOMALTTYP_0 唯一。",
    },
    {
        "class_name": "BOMDetail",
        "class_alias": "BOM明细",
        "source_table": "BOMD",
        "description": "BOM 明明细表，记录每个 BOM 的组件物料、用量、工序与生效区间。",
    },
    {
        "class_name": "ItemFacility",
        "class_alias": "物料地点",
        "source_table": "ITMFACILIT",
        "description": "物料地点表，管理物料在各基地的库存参数（安全库存、提前期、仓库等）。",
    },
    {
        "class_name": "Facility",
        "class_alias": "地点",
        "source_table": "FACILITY",
        "description": "地点信息表，基地编码与名称对应，含制造/采购/仓储/财务等业务标志。",
    },
    {
        "class_name": "RoutingOperation",
        "class_alias": "工艺工序",
        "source_table": "ROUOPE",
        "description": "工艺路线工序表，记录物料在各基地的工序、工作中心、工时与合作伙伴。",
    },
    {
        "class_name": "ArrivalNotice",
        "class_alias": "到货单",
        "source_table": "YPRECEIPT",
        "description": "到货单表头，记录供应商到货通知（ASN），含供应商、收货地点、收货日期、质检员等。",
    },
    {
        "class_name": "ArrivalNoticeDetail",
        "class_alias": "到货明细",
        "source_table": "YPRECEIPTD",
        "description": "到货单明细，记录每笔到货的物料、收货数量、采购员、仓库收货员及质量确认等。",
    },
    {
        "class_name": "Receipt",
        "class_alias": "收货单",
        "source_table": "PRECEIPT",
        "description": "收货单/入库单表头，记录正式入库的供应商、收货地点、采购类型、运输、重量体积及过账等。",
    },
    {
        "class_name": "ReceiptDetail",
        "class_alias": "收货明细",
        "source_table": "PRECEIPTD",
        "description": "收货单/入库单明细，记录每行收货物料的数量、价格、税额、仓库、采购订单与到货单关联等。",
    },
    {
        "class_name": "PurchaseRequisitionDetail",
        "class_alias": "采购需求明细",
        "source_table": "PREQUISD",
        "description": "采购需求/请购明细表，记录请购物料、数量、供应商、价格、要求交货日及下单/关闭状态等。",
    },
    {
        "class_name": "RequisitionOrderLink",
        "class_alias": "请购订单关联",
        "source_table": "PREQUISO",
        "description": "请购明细与采购订单的关联表，记录请购行被转单到采购订单的数量与订单序列。",
    },
    {
        "class_name": "SupplierPriceList",
        "class_alias": "供应商价格单",
        "source_table": "PPRICFICH",
        "description": "供应商价格表头，记录价格表号、记录、生效/失效日期与有效状态等。",
    },
    {
        "class_name": "SupplierPriceDetail",
        "class_alias": "供应商价格明细",
        "source_table": "PPRICLIST",
        "description": "供应商价格表明细，记录物料、单价、数量区间、免费物料、佣金系数及生效区间等。",
    },
    {
        "class_name": "SupplierPriceConf",
        "class_alias": "供应商价格配置",
        "source_table": "PPRICCONF",
        "description": "供应商价格配置表，定义价格清单的取价条件维度（供应商/物料等字段组合）、取价优先级与价格类型。",
    },
    {
        "class_name": "PurchaseOrder",
        "class_alias": "采购订单",
        "source_table": "PORDER",
        "description": "采购订单主表（PORDER），记录订单号、日期、供应商、采购员、交货条款与收货地点等。",
    },
    {
        "class_name": "PurchaseOrderDetail",
        "class_alias": "采购订单明细",
        "source_table": "PORDERQ",
        "description": "采购订单明细表（PORDERQ），记录每张采购订单下的物料行、数量、单价、金额与交货/收货需求等。",
    },
]

# =============================================================================
# 属性定义：每条 = {name(中文), alias(Oracle列), type, pk, fk(目标source_table), col}
# 允许的 dtype: STRING | INT | DECIMAL | DATETIME | BOOLEAN
# =============================================================================
def P(name, alias, dtype, *, pk=False, fk=None, col=None, aliases=None, desc=None):
    """构造属性定义的简写。

    aliases：业务别名列表（消歧缩写列名，如报价类列 ["报价", "供应商报价"]）；
    desc：列说明，注入 schema 文本供 LLM 理解列语义。
    """
    return {
        "name": name,
        "alias": alias,
        "type": dtype,
        "pk": pk,
        "fk": fk,
        "col": col or alias,
        "aliases": aliases,
        "desc": desc,
    }

PROPERTIES = {
    # ---------- ITMMASTER 物料 ----------
    "ITMMASTER": [
        P("物料编号", "ITMREF_0", "STRING", pk=True),
        P("物料描述1", "ITMDES1_0", "STRING"),
        P("物料描述2", "ITMDES2_0", "STRING"),
        P("物料描述3", "ITMDES3_0", "STRING"),
        P("分类码1", "TSICOD_0", "STRING"),
        P("分类码2", "TSICOD_1", "STRING"),
        P("车型编码", "TSICOD_2", "STRING"),
        P("分类码4", "TSICOD_3", "STRING"),
        P("分类码5", "TSICOD_4", "STRING"),
        P("物料类型代码", "TCLCOD_0", "STRING"),
        P("公司", "CPY_0", "STRING"),
        P("物料状态", "ITMSTA_0", "INT"),
        P("库存单位", "STU_0", "STRING"),
        P("采购单位", "PUU_0", "STRING"),
        P("销售单位", "SAU_0", "STRING"),
        P("统计单位", "SSU_0", "STRING"),
        P("物料重量", "ITMWEI_0", "DECIMAL"),
        P("重量单位", "WEU_0", "STRING"),
        P("条码", "EANCOD_0", "STRING"),
        P("采购员", "BUY_0", "STRING"),
        P("计划员", "PLANNER_0", "STRING"),
        P("物料分类", "YITMCAT_0", "STRING"),
        P("物料类型", "YMATTYP_0", "STRING"),
        P("颜色", "YCOLOR_0", "STRING"),
        P("版本", "YVERSION_0", "STRING"),
        P("原始物料标志", "YORIITM_0", "STRING"),
        P("创建日期", "CREDAT_0", "DATETIME"),
        P("创建人", "CREUSR_0", "STRING"),
        P("更新日期", "UPDDAT_0", "DATETIME"),
        P("更新人", "UPDUSR_0", "STRING"),
    ],
    # ---------- BPCUSTOMER 客户 ----------
    "BPCUSTOMER": [
        P("客户编号", "BPCNUM_0", "STRING", pk=True),
        P("客户名称", "BPCNAM_0", "STRING"),
        P("客户简称", "BPCSHO_0", "STRING"),
        P("客户类型", "BPCTYP_0", "INT"),
        P("客户状态", "BPCSTA_0", "INT"),
        P("客户组", "BCGCOD_0", "STRING"),
        P("开票客户", "BPCINV_0", "STRING"),
        P("开票地址", "BPAINV_0", "STRING"),
        P("地址", "BPAADD_0", "STRING"),
        P("联系人", "CNTNAM_0", "STRING"),
        P("货币", "CUR_0", "STRING"),
        P("客户备注", "BPCREM_0", "STRING"),
        P("销售员", "REP_0", "STRING"),
        P("ABC分类", "ABCCLS_0", "INT"),
        P("统计维度1", "TSCCOD_0", "STRING"),
        P("税则", "VACBPR_0", "STRING"),
        P("结算折扣", "DEP_0", "STRING"),
        P("创建日期", "CREDAT_0", "DATETIME"),
        P("创建人", "CREUSR_0", "STRING"),
        P("更新日期", "UPDDAT_0", "DATETIME"),
        P("更新人", "UPDUSR_0", "STRING"),
    ],
    # ---------- BPARTNER 合作伙伴 ----------
    "BPARTNER": [
        P("合作伙伴编号", "BPRNUM_0", "STRING", pk=True),
        P("名称", "BPRNAM_0", "STRING"),
        P("名称2", "BPRNAM_1", "STRING"),
        P("简称", "BPRSHO_0", "STRING"),
        P("地点", "FCY_0", "STRING", fk="FACILITY"),
        P("国家", "CRY_0", "STRING"),
        P("国家注册号", "CRN_0", "STRING"),
        P("行业", "NAF_0", "STRING"),
        P("货币", "CUR_0", "STRING"),
        P("语言", "LAN_0", "STRING"),
        P("增值税号", "VATNUM_0", "STRING"),
        P("财务编码", "FISCOD_0", "STRING"),
        P("是否客户", "BPCFLG_0", "INT"),
        P("是否供应商", "BPSFLG_0", "INT"),
        P("启用标志", "ENAFLG_0", "INT"),
        P("联系人", "CNTNAM_0", "STRING"),
        P("地址", "BPAADD_0", "STRING"),
        P("区域分组", "GRUGPY_0", "STRING"),
        P("分组", "GRUCOD_0", "STRING"),
        P("创建日期", "CREDAT_0", "DATETIME"),
        P("创建人", "CREUSR_0", "STRING"),
        P("更新日期", "UPDDAT_0", "DATETIME"),
        P("更新人", "UPDUSR_0", "STRING"),
    ],
    # ---------- BPCARRIER 承运人 ----------
    "BPCARRIER": [
        P("承运人编号", "BCRNUM_0", "STRING", pk=True),
        P("承运人名称", "BCRNAM_0", "STRING"),
        P("简称", "BCRSHO_0", "STRING"),
        P("联系人", "CNTNAM_0", "STRING"),
        P("地址", "BPAADD_0", "STRING"),
        P("税号", "VATNUM_0", "STRING"),
        P("货币", "CUR_0", "STRING"),
        P("运输方式", "MDL_0", "STRING"),
        P("国家", "CRY_0", "STRING"),
        P("电话", "TEL_0", "STRING"),
        P("创建日期", "CREDAT_0", "DATETIME"),
        P("创建人", "CREUSR_0", "STRING"),
        P("更新日期", "UPDDAT_0", "DATETIME"),
        P("更新人", "UPDUSR_0", "STRING"),
    ],
    # ---------- BPSUPPLIER 供应商 ----------
    "BPSUPPLIER": [
        P("供应商编号", "BPSNUM_0", "STRING", pk=True),
        P("供应商名称", "BPSNAM_0", "STRING"),
        P("供应商简称", "BPSSHO_0", "STRING"),
        P("供应商类型", "BPSTYP_0", "INT"),
        P("收货模式", "YPTHFLGM_0", "INT",
          aliases=["零库存标志", "到货模式", "收货管理方式"],
          desc="供应商收货模式标志：1=非零库存供应商（先建到货单，质检合格后再建收货单入库）"
               "；2=零库存供应商（采购订单直接生成收货单，生产时由领用系统自动入库/出库，无独立库存）"
               "。订单完成率统计必须按此字段拆分零库存与非零库存两组口径"),
        P("供应商组", "BSGCOD_0", "STRING"),
        P("开票供应商", "BPSINV_0", "STRING"),
        P("开票地址", "BPAINV_0", "STRING"),
        P("地址", "BPAADD_0", "STRING"),
        P("联系人", "CNTNAM_0", "STRING"),
        P("位置", "LOC_0", "STRING"),
        P("货币", "CUR_0", "STRING"),
        P("承运人", "BPTNUM_0", "STRING", fk="BPARTNER"),
        P("ABC分类", "ABCCLS_0", "INT"),
        P("统计维度", "UVYCOD_0", "STRING"),
        P("供应商备注", "BPSREM_0", "STRING"),
        P("发货模式", "MDL_0", "STRING"),
        P("税则", "VACBPR_0", "STRING"),
        P("结算折扣", "DEP_0", "STRING"),
        P("创建日期", "CREDAT_0", "DATETIME"),
        P("创建人", "CREUSR_0", "STRING"),
        P("更新日期", "UPDDAT_0", "DATETIME"),
        P("更新人", "UPDUSR_0", "STRING"),
    ],
    # ---------- BOM 物料清单 ----------
    "BOM": [
        P("物料编号", "ITMREF_0", "STRING", pk=True, fk="ITMMASTER"),
        P("BOM替代号", "BOMALT_0", "INT", pk=True),
        P("BOM替代类型", "BOMALTTYP_0", "INT", pk=True),
        P("BOM描述", "BOMDES_0", "STRING"),
        P("标识", "IDENT1_0", "STRING"),
        P("使用状态", "USESTA_0", "INT"),
        P("生效日期", "BOHSTRDAT_0", "DATETIME"),
        P("失效日期", "BOHENDDAT_0", "DATETIME"),
        P("BOM规则", "BOMRLE_0", "STRING"),
        P("基础数量", "BASQTY_0", "DECIMAL"),
        P("数量类型", "QTYCOD_0", "INT"),
        P("消耗类型", "ACSCOD_0", "STRING"),
        P("创建日期", "CREDAT_0", "DATETIME"),
        P("创建人", "CREUSR_0", "STRING"),
        P("更新日期", "UPDDAT_0", "DATETIME"),
        P("更新人", "UPDUSR_0", "STRING"),
    ],
    # ---------- BOMD BOM明细 ----------
    "BOMD": [
        P("父物料编号", "ITMREF_0", "STRING", pk=True, fk="ITMMASTER"),
        P("BOM替代号", "BOMALT_0", "INT", pk=True),
        P("序号", "BOMSEQ_0", "INT", pk=True),
        P("序号编号", "BOMSEQNUM_0", "INT"),
        P("组件物料编号", "CPNITMREF_0", "STRING", fk="ITMMASTER"),
        P("组件类型", "CPNTYP_0", "INT"),
        P("简称", "BOMSHO_0", "STRING"),
        P("用量", "BOMQTY_0", "DECIMAL"),
        P("单位", "BOMUOM_0", "STRING"),
        P("单位换算系数", "BOMSTUCOE_0", "DECIMAL"),
        P("损耗数量", "LIKQTY_0", "DECIMAL"),
        P("报废率", "SCA_0", "DECIMAL"),
        P("工序号", "CPNOPE_0", "INT"),
        P("生效日期", "BOMSTRDAT_0", "DATETIME"),
        P("失效日期", "BOMENDDAT_0", "DATETIME"),
        P("成本标志", "CSTFLG_0", "INT"),
        P("备注", "YNOTE_0", "STRING"),
        P("创建日期", "CREDAT_0", "DATETIME"),
        P("创建人", "CREUSR_0", "STRING"),
        P("更新日期", "UPDDAT_0", "DATETIME"),
        P("更新人", "UPDUSR_0", "STRING"),
    ],
    # ---------- ITMFACILIT 物料地点 ----------
    "ITMFACILIT": [
        P("物料编号", "ITMREF_0", "STRING", pk=True, fk="ITMMASTER"),
        P("库存地点", "STOFCY_0", "STRING", pk=True, fk="FACILITY"),
        P("ABC分类", "ABCCLS_0", "INT"),
        P("安全库存", "SAFSTO_0", "DECIMAL"),
        P("最大库存", "MAXSTO_0", "DECIMAL"),
        P("覆盖天数", "DAYCOV_0", "INT"),
        P("采购提前期", "PLH_0", "DECIMAL"),
        P("采购提前期单位", "PLHUOT_0", "INT"),
        P("制造提前期", "MFGLTI_0", "DECIMAL"),
        P("采购计划提前期", "PRPLTI_0", "DECIMAL"),
        P("计划员", "PLANNER_0", "STRING"),
        P("采购员", "BUY_0", "STRING"),
        P("补货地点", "REOFCY_0", "STRING"),
        P("订单仓库", "ORDWRH_0", "STRING"),
        P("物料仓库", "MATWRH_0", "STRING"),
        P("发货仓库", "SHIWRH_0", "STRING"),
        P("制造仓库", "MFGWRH_0", "STRING"),
        P("库存管理类型", "STOMGTCOD_0", "INT"),
        P("创建日期", "CREDAT_0", "DATETIME"),
        P("创建人", "CREUSR_0", "STRING"),
        P("更新日期", "UPDDAT_0", "DATETIME"),
        P("更新人", "UPDUSR_0", "STRING"),
    ],
    # ---------- FACILITY 地点 ----------
    "FACILITY": [
        P("地点编码", "FCY_0", "STRING", pk=True),
        P("地点名称", "FCYNAM_0", "STRING"),
        P("简称", "FCYSHO_0", "STRING"),
        P("国家", "CRY_0", "STRING"),
        P("国家注册号", "CRN_0", "STRING"),
        P("合作伙伴", "BPTNUM_0", "STRING", fk="BPARTNER"),
        P("制造标志", "MFGFLG_0", "INT"),
        P("销售标志", "SALFLG_0", "INT"),
        P("采购标志", "PURFLG_0", "INT"),
        P("仓储标志", "WRHFLG_0", "INT"),
        P("财务标志", "FINFLG_0", "INT"),
        P("法人公司", "LEGCPY_0", "STRING"),
        P("法人", "LEG_0", "STRING"),
        P("管辖地点", "DADFCY_0", "STRING"),
        P("付款银行", "PAYBAN_0", "STRING"),
        P("创建日期", "CREDAT_0", "DATETIME"),
        P("创建人", "CREUSR_0", "STRING"),
        P("更新日期", "UPDDAT_0", "DATETIME"),
        P("更新人", "UPDUSR_0", "STRING"),
    ],
    # ---------- ROUOPE 工艺工序 ----------
    "ROUOPE": [
        P("物料编号", "ITMREF_0", "STRING", pk=True, fk="ITMMASTER"),
        P("工艺替代号", "ROUALT_0", "INT", pk=True),
        P("工序号", "OPENUM_0", "INT", pk=True),
        P("地点", "FCY_0", "STRING", fk="FACILITY"),
        P("工序名称", "ROODES_0", "STRING"),
        P("工作中心", "WST_0", "STRING"),
        P("人工工作中心", "LABWST_0", "STRING"),
        P("工序单位", "OPEUOM_0", "STRING"),
        P("基础数量", "BASQTY_0", "DECIMAL"),
        P("工序时间", "OPETIM_0", "DECIMAL"),
        P("准备时间", "SETTIM_0", "DECIMAL"),
        P("等待时间", "WAITIM_0", "DECIMAL"),
        P("效率", "EFF_0", "DECIMAL"),
        P("合作伙伴", "BPRNUM_0", "STRING", fk="BPARTNER"),
        P("地址", "BPAADD_0", "STRING"),
        P("生效日期", "VALSTRDAT_0", "DATETIME"),
        P("失效日期", "VALENDDAT_0", "DATETIME"),
        P("创建日期", "CREDAT_0", "DATETIME"),
        P("创建人", "CREUSR_0", "STRING"),
        P("更新日期", "UPDDAT_0", "DATETIME"),
        P("更新人", "UPDUSR_0", "STRING"),
    ],
    # ---------- YPRECEIPT 到货单 ----------
    "YPRECEIPT": [
        P("到货单号", "YPTHNUM_0", "STRING", pk=True),
        P("公司", "CPY_0", "STRING"),
        P("收货地点", "PRHFCY_0", "STRING", fk="FACILITY"),
        P("收货日期", "RCPDAT_0", "DATETIME"),
        P("供应商", "BPSNUM_0", "STRING", fk="BPARTNER"),
        P("供应商地址", "BPAADD_0", "STRING"),
        P("货币", "CUR_0", "STRING"),
        P("销售订单号", "SOHNUM_0", "STRING"),
        P("审核标志", "APPFLG_0", "STRING"),
        P("到货标志", "YPTHFLG_0", "STRING"),
        P("质检员", "QUSR_0", "STRING"),
        P("质检日期", "QDAT_0", "DATETIME"),
        P("备注", "YNOTE_0", "STRING"),
        P("ASN号", "XSRMID_0", "STRING"),
        P("创建人", "CREUSR_0", "STRING"),
        P("创建日期", "CREDAT_0", "DATETIME"),
        P("更新人", "UPDUSR_0", "STRING"),
        P("更新日期", "UPDDAT_0", "DATETIME"),
        P("导出编号", "EXPNUM_0", "INT"),
        P("日期时间", "CREDATTIM_0", "DATETIME"),
        P("更新时间", "UPDDATTIM_0", "DATETIME"),
        P("唯一标识符", "AUUID_0", "STRING"),
        # === 从 Excel 字段字典补全 4 条 ===
    ],
    # ---------- YPRECEIPTD 到货明细 ----------
    "YPRECEIPTD": [
        P("到货单号", "YPTHNUM_0", "STRING", pk=True, fk="YPRECEIPT"),
        P("行号", "YPTDLIN_0", "INT", pk=True),
        P("公司", "CPY_0", "STRING"),
        P("收货地点", "PRHFCY_0", "STRING"),
        P("采购地点", "POHFCY_0", "STRING"),
        P("采购订单号", "POHNUM_0", "STRING"),
        P("订单行", "POPLIN_0", "INT"),
        P("订单序列数", "POQSEQ_0", "INT"),
        P("订单类型", "POHTYP_0", "STRING"),
        P("供应商", "BPSNUM_0", "STRING", fk="BPARTNER"),
        P("物料编号", "ITMREF_0", "STRING", fk="ITMMASTER"),
        P("采购货币", "CPRCUR_0", "STRING"),
        P("收货单位", "UOM_0", "STRING"),
        P("收货数量", "QTYUOM_0", "DECIMAL"),
        P("计划数量", "YQTYUOM_0", "DECIMAL"),
        P("已接收数量", "AQTYUOM_0", "DECIMAL"),
        P("退货数量", "RTNQTYPUU_0", "DECIMAL"),
        P("批次", "LOT_0", "STRING"),
        P("质检标志", "QUAFLG_0", "STRING"),
        P("描述", "DES_0", "STRING"),
        P("收货单号", "PTHNUM_0", "STRING"),
        P("收货行", "PTDLIN_0", "INT"),
        P("创建人", "CREUSR_0", "STRING"),
        P("创建日期", "CREDAT_0", "DATETIME"),
        P("更新人", "UPDUSR_0", "STRING"),
        P("更新日期", "UPDDAT_0", "DATETIME"),
        P("导出编号", "EXPNUM_0", "INT"),
        P("日期时间", "CREDATTIM_0", "DATETIME"),
        P("更新时间", "UPDDATTIM_0", "DATETIME"),
        P("唯一标识符", "AUUID_0", "STRING"),
        P("技术列表", "TECCRD_0", "STRING"),
        P("接收建议", "STOTST_0", "STRING", fk="TABSTASTO"),
        P("退货日期", "RTNDAT_0", "DATETIME"),
        P("收货标志", "PTHFLG_0", "STRING"),
        P("状态", "YPTDFLG_0", "STRING"),
        P("是否退货", "YRETFLG_0", "STRING"),
        P("质检人", "ZJAUS_0", "STRING", fk="AUTILIS"),
        P("质检日期", "ZJDAT_0", "DATETIME"),
        P("SRM Line", "XSRMLIN_0", "INT"),
        # === 从 Excel 字段字典补全 16 条 ===
    ],
    # ---------- PRECEIPT 收货单/入库单 ----------
    "PRECEIPT": [
        P("收货单号", "PTHNUM_0", "STRING", pk=True),
        P("公司", "CPY_0", "STRING"),
        P("收货地点", "PRHFCY_0", "STRING", fk="FACILITY"),
        P("采购类型", "PURTYP_0", "STRING"),
        P("供应商包装单号", "BPSNDE_0", "STRING"),
        P("包装单日期", "NDEDAT_0", "DATETIME"),
        P("收货日期", "RCPDAT_0", "DATETIME"),
        P("供应商", "BPSNUM_0", "STRING", fk="BPARTNER"),
        P("地址", "BPAADD_0", "STRING"),
        P("装运地址", "BPOADD_0", "STRING"),
        P("供应商公司名称", "BPONAM_0", "STRING"),
        P("国家", "BPOCRY_0", "STRING"),
        P("国家名称", "BPOCRYNAM_0", "STRING"),
        P("开票供应商", "BPSINV_0", "STRING"),
        P("开票地址", "BPAINV_0", "STRING"),
        P("付款供应商", "BPRPAY_0", "STRING"),
        P("付款地址", "BPAPAY_0", "STRING"),
        P("统计组", "TSSCOD_0", "STRING"),
        P("货币", "CUR_0", "STRING"),
        P("汇率类型", "CHGTYP_0", "STRING"),
        P("汇率", "CHGCOE_0", "DECIMAL"),
        P("税则", "VACBPR_0", "STRING"),
        P("税则类型", "VACTYP_0", "STRING"),
        P("发货模式", "MDL_0", "STRING"),
        P("国际贸易术语", "EECICT_0", "STRING"),
        P("重量单位", "WEU_0", "STRING"),
        P("体积单位", "VOU_0", "STRING"),
        P("毛重", "TOTGROWEI_0", "DECIMAL"),
        P("净重", "TOTNETWEI_0", "DECIMAL"),
        P("行数", "LINNBR_0", "INT"),
        P("已开票行数", "INVLINCTR_0", "INT"),
        P("已过账行数", "PSTLINNBR_0", "INT"),
        P("打印标志", "PRNFLG_0", "STRING"),
        P("发票标志", "INVFLG_0", "STRING"),
        P("过账标志", "PSTFLG_0", "STRING"),
        P("过账日期", "PSTDAT_0", "DATETIME"),
        P("备注", "ZNOTE_0", "STRING"),
        P("自动发货标志", "ZAUTOFLG_0", "STRING"),
        P("自动发货日志", "ZAUTOLOG_0", "STRING"),
        P("创建人", "CREUSR_0", "STRING"),
        P("创建日期", "CREDAT_0", "DATETIME"),
        P("更新人", "UPDUSR_0", "STRING"),
        P("更新日期", "UPDDAT_0", "DATETIME"),
        P("地址行", "BPOADDLIG_0", "STRING"),
        P("EEC 传送模式", "EECTRN_0", "STRING"),
        P("公司发票行编号", "INVLINNBR_0", "INT"),
        P("地点间", "BETFCY_0", "STRING"),
        P("公司间", "BETCPY_0", "STRING"),
        P("交易代码", "TRSCOD_0", "STRING", fk="ATABDIV"),
        P("自动凭证代码", "ENTCOD_0", "STRING", fk="GAUTACE"),
        P("CAI有效日期", "DATVLYCAI_0", "DATETIME"),
        P("分析元类型代码", "DIE_0", "DATETIME", fk="GDIE"),
        P("重量单位(重量单位)", "DSPWEU_0", "STRING", fk="TABUNIT"),
        P("体积单位(体积单位)", "DSPVOU_0", "STRING", fk="TABUNIT"),
        P("不含税行总计", "TOTLINAMT_0", "DECIMAL"),
        P("总货品数量", "TOTLINQTY_0", "DECIMAL"),
        P("行称重合计", "TOTLINWEU_0", "DECIMAL"),
        P("行体积合计", "TOTLINVOU_0", "DECIMAL"),
        P("不含税总计", "TOTAMTNOT_0", "DECIMAL"),
        P("公司货币不含税总计", "TOTAMTNOTL_0", "DECIMAL"),
        P("税费总计", "TOTTAXAMT_0", "DECIMAL"),
        P("含税总计", "TOTAMTATI_0", "DECIMAL"),
        P("含税总额/币种", "TOTAMTATIL_0", "DECIMAL"),
        P("仓库", "WRHE_0", "STRING", fk="WAREHOUSE"),
        P("导出编号", "EXPNUM_0", "INT"),
        P("日期时间", "CREDATTIM_0", "DATETIME"),
        P("更新时间", "UPDDATTIM_0", "DATETIME"),
        P("唯一标识符", "AUUID_0", "STRING"),
        # === 从 Excel 字段字典补全 44 条 ===
    ],
    # ---------- PRECEIPTD 收货明细/入库明细 ----------
    "PRECEIPTD": [
        P("收货单号", "PTHNUM_0", "STRING", pk=True, fk="PRECEIPT"),
        P("行号", "PTDLIN_0", "INT", pk=True),
        P("公司", "CPY_0", "STRING"),
        P("收货地点", "PRHFCY_0", "STRING"),
        P("收货日期", "RCPDAT_0", "DATETIME"),
        P("采购地点", "POHFCY_0", "STRING"),
        P("采购订单号", "POHNUM_0", "STRING"),
        P("订单行", "POPLIN_0", "INT"),
        P("订单序列数", "POQSEQ_0", "INT"),
        P("订单类型", "POHTYP_0", "STRING"),
        P("供应商", "BPSNUM_0", "STRING", fk="BPARTNER"),
        P("开票供应商", "BPSINV_0", "STRING"),
        P("开票地址", "BPAINV_0", "STRING"),
        P("项目", "PJT_0", "STRING"),
        P("物料编号", "ITMREF_0", "STRING", fk="ITMMASTER"),
        P("物料描述1", "ITMDES1_0", "STRING"),
        P("物料描述", "ITMDES_0", "STRING"),
        P("采购价", "GROPRI_0", "DECIMAL"),
        P("净价", "NETPRI_0", "DECIMAL"),
        P("净价单位价", "NETPRIPUU_0", "DECIMAL"),
        P("行金额", "LINAMT_0", "DECIMAL"),
        P("含税金额", "LINATIAMT_0", "DECIMAL"),
        P("行成本", "LINCSTPUR_0", "DECIMAL"),
        P("库存数量", "QTYUOM_0", "DECIMAL", aliases=["收货数量", "入库数量"]),
        P("采购单位数量", "QTYPUU_0", "DECIMAL"),
        P("存货单位数量", "QTYSTU_0", "DECIMAL"),
        P("重量", "QTYWEU_0", "DECIMAL"),
        P("体积", "QTYVOU_0", "DECIMAL"),
        P("单位", "UOM_0", "STRING"),
        P("采购单位", "PUU_0", "STRING"),
        P("存货单位", "STU_0", "STRING"),
        P("退货数量", "RTNQTYPUU_0", "DECIMAL"),
        P("已开票数量", "INVQTYPUU_0", "DECIMAL"),
        P("仓库", "WRH_0", "STRING"),
        P("质检标志", "QUAFLG_0", "STRING"),
        P("打印标志", "LINPRNFLG_0", "STRING"),
        P("发票标志", "LININVFLG_0", "STRING"),
        P("过账标志", "LINPSTFLG_0", "STRING"),
        P("过账日期", "LINPSTDAT_0", "DATETIME"),
        P("税码", "VAT_0", "STRING"),
        P("统计分类", "TSICOD_0", "STRING"),
        P("送货单号", "SDHNUM_0", "STRING"),
        P("送货行", "SDDLIN_0", "INT"),
        P("到货单号", "YPTHNUM_0", "STRING", fk="YPRECEIPT"),
        P("到货行号", "YPTDLIN_0", "INT"),
        P("行类型", "LINTYP_0", "STRING"),
        P("行类别", "LINCAT_0", "STRING"),
        P("创建人", "CREUSR_0", "STRING"),
        P("创建日期", "CREDAT_0", "DATETIME"),
        P("更新人", "UPDUSR_0", "STRING"),
        P("更新日期", "UPDDAT_0", "DATETIME"),
        P("定价理由", "PRIREN_0", "STRING", fk="PPREASON"),
        P("存货成本", "LINAMTCPR_0", "DECIMAL"),
        P("税基 1", "BASTAXLIN1_0", "DECIMAL"),
        P("税额 1", "AMTTAXLIN1_0", "DECIMAL"),
        P("税额 2", "AMTTAXLIN2_0", "DECIMAL"),
        P("税号3", "AMTTAXLIN3_0", "DECIMAL"),
        P("接收税金额", "AMTTAXRCP_0", "DECIMAL"),
        P("付款税金额", "AMTTAXISS_0", "DECIMAL"),
        P("金额其他税1", "AMTTAXOTH1_0", "DECIMAL"),
        P("金额其他税2", "AMTTAXOTH2_0", "DECIMAL"),
        P("可减免税1", "DEDTAXLIN1_0", "DECIMAL"),
        P("可减免税2", "DEDTAXLIN2_0", "DECIMAL"),
        P("可减免税3", "DEDTAXLIN3_0", "DECIMAL"),
        P("可扣除税", "DEDTAXRCP_0", "DECIMAL"),
        P("可扣除税(已开票)", "DEDTAXISS_0", "DECIMAL"),
        P("可扣除税(其他1)", "DEDTAXOTH1_0", "DECIMAL"),
        P("可扣除税(其他2)", "DEDTAXOTH2_0", "DECIMAL"),
        P("生产成本SAL", "CPRPRI_0", "DECIMAL"),
        P("生产成本SAL(SAL)", "CPR_0", "DECIMAL"),
        P("已计算的存货成本", "CPRCLC_0", "DECIMAL"),
        P("单位采购成本", "CSTPUR_0", "DECIMAL"),
        P("近似费用系数", "CPRCOE_0", "DECIMAL"),
        P("单位固定成本", "CPRAMT_0", "DECIMAL"),
        P("成本结构", "STCNUM_0", "STRING"),
        P("货币", "NETCUR_0", "STRING", fk="TABCUR"),
        P("公司货币", "CPRCUR_0", "STRING", fk="TABCUR"),
        P("采购成本总计", "FCSCSTPUR_0", "DECIMAL"),
        P("存货成本总计", "FCSCPR_0", "DECIMAL"),
        P("已入账的存货成本", "FCSCPRCPT_0", "DECIMAL"),
        P("重量单位", "LINWEU_0", "STRING", fk="TABUNIT"),
        P("体积(明细)", "LINVOU_0", "STRING", fk="TABUNIT"),
        P("库存-采购单位转换", "UOMPUUCOE_0", "DECIMAL"),
        P("计量单位/存货单位系数", "UOMSTUCOE_0", "DECIMAL"),
        P("采购R数量", "RRRQTYPUU_0", "DECIMAL"),
        P("库存R数量", "RRRQTYSTU_0", "DECIMAL"),
        P("存货单位退货数量", "RTNQTYSTU_0", "DECIMAL"),
        P("已开票存货单位数量", "INVQTYSTU_0", "DECIMAL"),
        P("借方行", "LINEECFLG_0", "STRING"),
        P("来自 QC 退货", "QUARTNFLG_0", "STRING"),
        P("采购类型", "LINPURTYP_0", "STRING"),
        P("EEC价格上涨", "EECINCRAT_0", "DECIMAL"),
        P("原产地", "ORICRY_0", "STRING", fk="TABCOUNTRY"),
        P("国家(装运)", "BPOCRY_0", "STRING", fk="TABCOUNTRY"),
        P("发送区域", "SATISS_0", "STRING", fk="ATABDIV"),
        P("交易组", "TRSFAM_0", "STRING", fk="ATABDIV"),
        P("进项税", "TAXRCP_0", "STRING", fk="TABVAT"),
        P("销项税", "TAXISS_0", "STRING", fk="TABVAT"),
        P("其他税费1", "TAXOTH1_0", "STRING", fk="TABVAT"),
        P("其他税费2", "TAXOTH2_0", "STRING", fk="TABVAT"),
        P("文本", "LINTEX_0", "STRING"),
        P("装运地点", "LINSTOFCY_0", "STRING", fk="FACILITY"),
        P("已发放产品", "ITMREFORI_0", "STRING", fk="ITMMASTER"),
        P("原始凭证类型", "VCRTYPORI_0", "STRING"),
        P("原始凭证", "VCRNUMORI_0", "STRING"),
        P("原始凭证行号", "VCRLINORI_0", "INT"),
        P("原始凭证序列号", "VCRSEQORI_0", "INT"),
        P("存货管理", "STOMGTCOD_0", "STRING"),
        P("版本标识", "VERFLG_0", "INT"),
        P("相关质量参数", "LIKQTYCOE_0", "DECIMAL"),
        P("导出编号", "EXPNUM_0", "INT"),
        P("三向匹配", "MATTOL_0", "STRING", fk="MATCHTOL"),
        P("日期时间", "CREDATTIM_0", "DATETIME"),
        P("更新时间", "UPDDATTIM_0", "DATETIME"),
        P("唯一标识符", "AUUID_0", "STRING"),
        P("过账的外协成本", "SCOCSTCPT_0", "DECIMAL"),
        P("不暂估", "YPIFLG_0", "STRING"),
        P("TotalLINK PK", "XWSPK_0", "STRING"),
        # === 从 Excel 字段字典补全 102 条 ===
    ],
    # ---------- PREQUISD 采购需求明细 ----------
    "PREQUISD": [
        P("请购单号", "PSHNUM_0", "STRING", pk=True),
        P("行号", "PSDLIN_0", "INT", pk=True),
        P("公司", "CPY_0", "STRING"),
        P("请购地点", "PSHFCY_0", "STRING"),
        P("物料编号", "ITMREF_0", "STRING", fk="ITMMASTER"),
        P("物料描述1", "ITMDES1_0", "STRING"),
        P("物料描述", "ITMDES_0", "STRING"),
        P("供应商", "BPSNUM_0", "STRING", fk="BPARTNER"),
        P("采购单位数量", "QTYPUU_0", "DECIMAL"),
        P("存货单位数量", "QTYSTU_0", "DECIMAL"),
        P("已下单数量", "ORDQTYPUU_0", "DECIMAL"),
        P("已下单存货数量", "ORDQTYSTU_0", "DECIMAL"),
        P("采购单位", "PUU_0", "STRING"),
        P("存货单位", "STU_0", "STRING"),
        P("税则", "VACBPR_0", "STRING"),
        P("税则类型", "VACTYP_0", "INT"),
        P("货币", "CUR_0", "STRING"),
        P("汇率类型", "CHGTYP_0", "STRING"),
        P("汇率", "CHGCOE_0", "DECIMAL"),
        P("采购价", "GROPRI_0", "DECIMAL"),
        P("净价", "NETPRI_0", "DECIMAL"),
        P("行金额", "LINAMT_0", "DECIMAL"),
        P("含税金额", "LINATIAMT_0", "DECIMAL"),
        P("要求交货日", "EXTORDDAT_0", "DATETIME"),
        P("要求收货日", "EXTRCPDAT_0", "DATETIME"),
        P("项目", "PJT_0", "STRING"),
        P("收货地点", "PRHFCY_0", "STRING", fk="FACILITY"),
        P("关闭标志", "LINCLEFLG_0", "STRING"),
        P("下单标志", "LINORDFLG_0", "STRING"),
        P("审核标志", "LINAPPFLG_0", "STRING"),
        P("报价单号", "PQHNUM_0", "STRING"),
        P("报价行", "PPDLIN_0", "INT"),
        P("工单号", "WIPNUM_0", "STRING"),
        P("创建人", "CREUSR_0", "STRING"),
        P("创建日期", "CREDAT_0", "DATETIME"),
        P("更新人", "UPDUSR_0", "STRING"),
        P("更新日期", "UPDDAT_0", "DATETIME"),
        P("定价理由", "PRIREN_0", "STRING", fk="PPREASON"),
        P("税基 1", "BASTAXLIN1_0", "DECIMAL"),
        P("税额 1", "AMTTAXLIN1_0", "DECIMAL"),
        P("可减免税1", "DEDTAXLIN1_0", "DECIMAL"),
        P("承诺类型", "CMMPRPTAX_0", "STRING"),
        P("日期时间", "CREDATTIM_0", "DATETIME"),
        P("更新时间", "UPDDATTIM_0", "DATETIME"),
        P("唯一标识符", "AUUID_0", "STRING"),
        # === 从 Excel 字段字典补全 55 条 ===
    ],
    # ---------- PREQUISO 请购订单关联 ----------
    "PREQUISO": [
        P("请购单号", "PSHNUM_0", "STRING", pk=True, fk="PREQUISD"),
        P("行号", "PSDLIN_0", "INT", pk=True),
        P("采购订单号", "POHNUM_0", "STRING", pk=True),
        P("订单行", "POPLIN_0", "INT", pk=True),
        P("订单序列数", "POQSEQ_0", "INT", pk=True),
        P("采购单位数量", "QTYPUU_0", "DECIMAL"),
        P("采购单位", "PUU_0", "STRING"),
        P("存货单位数量", "QTYSTU_0", "DECIMAL"),
        P("存货单位", "STU_0", "STRING"),
        P("创建人", "CREUSR_0", "STRING"),
        P("更新人", "UPDUSR_0", "STRING"),
        P("日期时间", "CREDATTIM_0", "DATETIME"),
        P("更新时间", "UPDDATTIM_0", "DATETIME"),
        P("唯一标识符", "AUUID_0", "STRING"),
        # === 从 Excel 字段字典补全 3 条 ===
    ],
    # ---------- PPRICFICH 供应商价格单 ----------
    "PPRICFICH": [
        P("价格表号", "PLI_0", "STRING", pk=True),
        P("价格表记录", "PLICRD_0", "STRING", pk=True),
        P("生效日期", "PLISTRDAT_0", "DATETIME"),
        P("失效日期", "PLIENDDAT_0", "DATETIME"),
        P("行数", "LINNBR_0", "INT"),
        P("有效状态", "VALSTA_0", "INT"),
        P("创建人", "CREUSR_0", "STRING"),
        P("创建日期", "CREDAT_0", "DATETIME"),
        P("更新人", "UPDUSR_0", "STRING"),
        P("更新日期", "UPDDAT_0", "DATETIME"),
        P("导出编号", "EXPNUM_0", "INT"),
        P("日期时间", "CREDATTIM_0", "DATETIME"),
        P("更新时间", "UPDDATTIM_0", "DATETIME"),
        P("唯一标识符", "AUUID_0", "STRING"),
        # === 从 Excel 字段字典补全 4 条 ===
    ],
    # ---------- PPRICLIST 供应商价格明细 ----------
    "PPRICLIST": [
        P("价格表号", "PLI_0", "STRING", pk=True, fk="PPRICCONF"),
        P("价格表记录", "PLICRD_0", "STRING", pk=True),
        P("行号", "PLILIN_0", "INT", pk=True),
        P("价格条件2", "PLICRI1_0", "STRING"),
        P("物料编码", "PLICRI2_0", "STRING", fk="ITMMASTER",
          aliases=["价格条件3", "物料编号"],
          desc="价格条件3列在本库实际存放物料编码（实证：CPNITMREF_0 恒为空格），接 PORDERQ.ITMREF_0 取报价/比价"),
        P("生效日期", "PLISTRDAT_0", "DATETIME",
          aliases=["生效日", "起始日期", "生效起始日", "开始日期"],
          desc="报价行的生效起始日期；查询当期价格须同时满足 生效日期 不晚于查询日 且 失效日期 不早于查询日，勿把不同时间段的报价混在一起取平均"),
        P("失效日期", "PLIENDDAT_0", "DATETIME",
          aliases=["失效日", "截止日期", "生效结束日", "结束日期"],
          desc="报价行的生效截止日期；配合 生效日期 界定报价的有效时间段，已过期报价不应计入当期价格"),
        P("单位", "UOM_0", "STRING"),
        P("货币", "CUR_0", "STRING"),
        P("单价", "PRI_0", "DECIMAL",
          aliases=["报价", "供应商报价", "采购报价"],
          desc="供应商报价单明细中的单价（含价格条件/生效失效日期约束，取数前须校验有效期）"),
        P("创建人", "CREUSR_0", "STRING"),
        P("创建日期", "CREDAT_0", "DATETIME"),
        P("更新人", "UPDUSR_0", "STRING"),
        P("更新日期", "UPDDAT_0", "DATETIME"),
        P("导出编号", "EXPNUM_0", "INT"),
        P("日期时间", "CREDATTIM_0", "DATETIME"),
        P("更新时间", "UPDDATTIM_0", "DATETIME"),
        P("唯一标识符", "AUUID_0", "STRING"),
        # === 从 Excel 字段字典补全 6 条 ===
    ],

    # ---------- PPRICCONF 供应商价格配置 ----------
    "PPRICCONF": [
        P("价格表号", "PLI_0", "STRING", pk=True,
          aliases=["价格清单", "价格清单号", "T10", "T11", "T20", "T21",
                   "含税价", "不含税价", "含税价(地点)", "不含税价(地点)"],
          desc="价格配置对应的价格清单号，T10=含税价、T11=不含税价、T20=含税价(地点)、T21=不含税价(地点)；关联 PPRICLIST/PPRICFICH"),
        P("价格清单说明", "LANDESSHO_0", "STRING",
          desc="价格清单说明，带语言前缀（如 CHI~含税价）"),
        P("价格清单状态", "PLISTC_0", "STRING"),
        P("取价优先级", "PIO_0", "INT",
          desc="多价格清单取价时的优先级，值越小越优先"),
        P("启用标志", "PLIENAFLG_0", "INT"),
        P("价格清单类型", "PLITYP_0", "INT"),
        P("价格类型", "PRITYP_0", "INT"),
        P("价格字段", "PRIFLD_0", "STRING"),
        P("货币", "CUR_0", "STRING"),
        P("条件数量", "CRINBR_0", "INT",
          desc="价格清单定义的取价条件维度数量"),
        P("条件1简称", "ABB_0", "STRING"),
        P("条件1表", "FIL_0", "STRING",
          desc="取价条件1对应的表（BPSUPPLIER=供应商、ITMMASTER=物料）"),
        P("条件1字段", "FLD_0", "STRING",
          desc="取价条件1对应的字段（BPSNUM=供应商编号、ITMREF=物料编号）"),
        P("条件1描述", "CRIDES_0", "STRING",
          desc="取价条件1的中文描述（如 供应商、产品）"),
        P("货币条件数", "CRICURNUM_0", "INT"),
        P("单位条件数", "CRIUOMNUM_0", "INT"),
        P("物料条件数", "CRIITMNUM_0", "INT"),
        P("供应商条件数", "CRIBPSNUM_0", "INT"),
        P("创建人", "CREUSR_0", "STRING"),
        P("创建日期", "CREDAT_0", "DATETIME"),
        P("更新人", "UPDUSR_0", "STRING"),
        P("更新日期", "UPDDAT_0", "DATETIME"),
    ],

    # ---------- PORDER 采购订单 ----------
    "PORDER": [
        P("采购订单号", "POHNUM_0", "STRING", pk=True),
        P("公司", "CPY_0", "STRING"),
        P("采购地点", "POHFCY_0", "STRING", fk="FACILITY"),
        P("订单日期", "ORDDAT_0", "DATETIME"),
        P("供应商", "BPSNUM_0", "STRING", fk="BPARTNER"),
        P("供应商名称", "BPRNAM_0", "STRING"),
        P("开票供应商", "BPSINV_0", "STRING"),
        P("采购员", "BUY_0", "STRING"),
        P("结算条款", "PTE_0", "STRING"),
        P("需求收货日期", "EXTRCPDAT1_0", "DATETIME"),
        P("创建人", "CREUSR_0", "STRING"),
        P("创建日期", "CREDAT_0", "DATETIME"),
        P("更新人", "UPDUSR_0", "STRING"),
        P("更新日期", "UPDDAT_0", "DATETIME"),
        P("地址", "BPAADD_0", "STRING", fk="BPADDRESS"),
        P("地址行", "BPAADDLIG_0", "STRING"),
        P("国家", "CRY_0", "STRING", fk="TABCOUNTRY"),
        P("国家名称", "CRYNAM_0", "STRING"),
        P("装运地址", "BPOADD_0", "STRING", fk="BPADDRESS"),
        P("公司名称", "BPONAM_0", "STRING"),
        P("地址行(装运)", "BPOADDLIG_0", "STRING"),
        P("邮政编码(装运)", "BPOPOSCOD_0", "STRING"),
        P("国家(装运)", "BPOCRY_0", "STRING", fk="TABCOUNTRY"),
        P("国家名称(装运)", "BPOCRYNAM_0", "STRING"),
        P("开票地点", "INVFCY_0", "STRING", fk="FACILITY"),
        P("收货地点", "RCPFCY_0", "STRING", fk="FACILITY"),
        P("统计组", "TSSCOD_0", "STRING", fk="ATABDIV"),
        P("付至", "BPRPAY_0", "STRING", fk="BPARTNER"),
        P("支付BP地址", "BPAPAY_0", "STRING", fk="BPADDRESS"),
        P("开票地址", "BPAINV_0", "STRING", fk="BPADDRESS"),
        P("分析元类型代码", "DIE_0", "DATETIME", fk="GDIE"),
        P("分析元", "CCE_0", "STRING", fk="CACCE"),
        P("语言", "LAN_0", "STRING", fk="TABLAN"),
        P("货币", "CUR_0", "STRING", fk="TABCUR"),
        P("汇率类型", "CHGTYP_0", "STRING"),
        P("汇率", "CHGCOE_0", "DECIMAL"),
        P("税则", "VACBPR_0", "STRING", fk="TABVACBPR"),
        P("税则类型", "VACTYP_0", "INT"),
        P("行数", "LINNBR_0", "INT"),
        P("已收货行数", "RCPLINNBR_0", "INT"),
        P("已关闭行数", "CLELINNBR_0", "INT"),
        P("已开票行数", "INVLINNBR_0", "INT"),
        P("收货次数", "RCPNBR_0", "INT"),
        P("发票数", "INVNBR_0", "INT"),
        P("已签字", "APPFLG_0", "STRING"),
        P("已结转", "CLEFLG_0", "STRING"),
        P("已收货", "RCPFLG_0", "STRING"),
        P("已开票", "INVFLG_0", "STRING"),
        P("已打印", "PRNFLG_0", "STRING"),
        P("确认函日期", "OCNDAT_0", "DATETIME"),
        P("订单确认函编号", "OCNNUM_0", "STRING"),
        P("订单确认函备注", "OCNREM_0", "STRING"),
        P("确认函提醒", "OCNFLG_0", "STRING"),
        P("发货提醒", "FUPFLG_0", "STRING"),
        P("公司间", "BETCPY_0", "STRING"),
        P("地点间", "BETFCY_0", "STRING"),
        P("来源地点", "ORIFCY_0", "STRING", fk="FACILITY"),
        P("装运地点", "STOFCY_0", "STRING", fk="FACILITY"),
        P("销售地点", "SALFCY_0", "STRING", fk="FACILITY"),
        P("订单类型", "SOHCAT_0", "STRING"),
        P("订单客户", "BPCORD_0", "STRING", fk="BPARTNER"),
        P("部分发货", "DME_0", "STRING"),
        P("重量单位(显示)", "DSPWEU_0", "STRING", fk="TABUNIT"),
        P("体积单位(显示)", "DSPVOU_0", "STRING", fk="TABUNIT"),
        P("含税行总计", "TOTLINATI_0", "DECIMAL"),
        P("不含税行总计", "TOTLINAMT_0", "DECIMAL"),
        P("总货品数量", "TOTLINQTY_0", "DECIMAL"),
        P("行称重合计", "TOTLINWEU_0", "DECIMAL"),
        P("行体积合计", "TOTLINVOU_0", "DECIMAL"),
        P("税费总计", "TOTTAXAMT_0", "DECIMAL"),
        P("订单总计不含税", "TOTORD_0", "DECIMAL"),
        P("订单总计 +税", "TTVORD_0", "DECIMAL"),
        P("公司货币不含税总计", "TOTORDL_0", "DECIMAL"),
        P("含税总额/币种", "TTVORDL_0", "DECIMAL"),
        P("有效从", "STRDAT_0", "DATETIME"),
        P("有效至", "ENDDAT_0", "DATETIME"),
        P("导出编号", "EXPNUM_0", "INT"),
        P("日期时间", "CREDATTIM_0", "DATETIME"),
        P("更新时间", "UPDDATTIM_0", "DATETIME"),
        P("唯一标识符", "AUUID_0", "STRING"),
        P("审核状态", "VALSTA_0", "STRING"),
        P("库位", "XLOCDET_0", "STRING"),
        P("SRMID", "XSRMID_0", "STRING"),
        # === 从 Excel 字段字典补全 96 条 ===
    ],

    # ---------- PORDERQ 采购订单明细 ----------
    "PORDERQ": [
        P("采购订单号", "POHNUM_0", "STRING", pk=True, fk="PORDER"),
        P("行号", "POPLIN_0", "INT", pk=True),
        P("订单日期", "ORDDAT_0", "DATETIME"),
        P("供应商", "BPSNUM_0", "STRING", fk="BPARTNER"),
        P("物料编号", "ITMREF_0", "STRING", fk="ITMMASTER"),
        P("供应商物料编号", "ITMREFBPS_0", "STRING"),
        P("单位", "UOM_0", "STRING"),
        P("采购数量", "QTYUOM_0", "DECIMAL"),
        P("采购单位数量", "QTYPUU_0", "DECIMAL"),
        P("库存单位数量", "QTYSTU_0", "DECIMAL"),
        P("重量", "QTYWEU_0", "DECIMAL"),
        P("合同单价", "CPRPRI_0", "DECIMAL"),
        P("行金额", "LINAMT_0", "DECIMAL"),
        P("含税金额", "LINATIAMT_0", "DECIMAL"),
        P("采购成本", "CSTPUR_0", "DECIMAL"),
        P("收货数量", "RCPQTYPUU_0", "DECIMAL"),
        P("收货库存数量", "RCPQTYSTU_0", "DECIMAL"),
        P("已入库数量", "INVQTYPUU_0", "DECIMAL"),
        P("退货数量", "RETQTYPUU_0", "DECIMAL"),
        P("创建人", "CREUSR_0", "STRING"),
        P("创建日期", "CREDAT_0", "DATETIME"),
        P("更新人", "UPDUSR_0", "STRING"),
        P("更新日期", "UPDDAT_0", "DATETIME"),
        P("序列号", "POQSEQ_0", "INT", fk="PORDERP"),
        P("行＋序列", "POQLNK_0", "STRING"),
        P("公司", "CPY_0", "STRING", fk="COMPANY"),
        P("采购地点", "POHFCY_0", "STRING", fk="FACILITY"),
        P("订单类型", "POHTYP_0", "STRING"),
        P("开票至", "BPSINV_0", "STRING", fk="BPARTNER"),
        P("开票地址", "BPAINV_0", "STRING", fk="BPADDRESS"),
        P("采购单位", "PUU_0", "STRING", fk="TABUNIT"),
        P("存货单位", "STU_0", "STRING", fk="TABUNIT"),
        P("订单PAC", "UOMFLG_0", "STRING"),
        P("库存-采购单位转换", "UOMPUUCOE_0", "DECIMAL"),
        P("相关质量参数", "LIKQTYCOE_0", "DECIMAL"),
        P("重量单位", "LINWEU_0", "STRING", fk="TABUNIT"),
        P("体积(明细)", "LINVOU_0", "STRING", fk="TABUNIT"),
        P("需要的库存数量", "RETQTYSTU_0", "DECIMAL"),
        P("已开票存货单位数量", "INVQTYSTU_0", "DECIMAL"),
        P("需求日期", "RETRCPDAT_0", "DATETIME"),
        P("预期收货日期", "EXTRCPDAT_0", "DATETIME"),
        P("装运地点", "LINSTOFCY_0", "STRING", fk="FACILITY"),
        P("收货地点", "PRHFCY_0", "STRING", fk="FACILITY"),
        P("收货地址", "FCYADD_0", "STRING", fk="BPADDRESS"),
        P("外协地址", "SCOADD_0", "STRING", fk="BPADDRESS"),
        P("订单类型(工单)", "WIPTYP_0", "STRING"),
        P("在产品状态", "WIPSTA_0", "STRING"),
        P("订单号", "WIPNUM_0", "STRING"),
        P("采购类型", "LINPURTYP_0", "STRING"),
        P("上次收货日期", "LASRCPDAT_0", "DATETIME"),
        P("上次发票日期", "LASINVDAT_0", "DATETIME"),
        P("发票收货号", "INVRCPNBR_0", "INT"),
        P("收货次数", "LINRCPNBR_0", "INT"),
        P("发票数", "LININVNBR_0", "INT"),
        P("货币", "NETCUR_0", "STRING", fk="TABCUR"),
        P("采购成本(SAL)", "CPR_0", "DECIMAL"),
        P("公司货币", "CPRCUR_0", "STRING", fk="TABCUR"),
        P("采购成本总计", "FCSCSTPUR_0", "DECIMAL"),
        P("存货成本总计", "FCSCPR_0", "DECIMAL"),
        P("存货成本", "LINAMTCPR_0", "DECIMAL"),
        P("采购成本(明细)", "LINCSTPUR_0", "DECIMAL"),
        P("实际采购成本", "REACSTPUR_0", "DECIMAL"),
        P("含税行金额", "LINATI_0", "DECIMAL"),
        P("税基 1", "BASTAXLIN1_0", "DECIMAL"),
        P("税额 1", "AMTTAXLIN1_0", "DECIMAL"),
        P("可减免税1", "DEDTAXLIN1_0", "DECIMAL"),
        P("销售订单号", "SOHNUM_0", "STRING"),
        P("销售订单货品", "SOPLIN_0", "INT"),
        P("序列号(销售)", "SOQSEQ_0", "INT"),
        P("发货号", "SDHNUM_0", "STRING"),
        P("装运行", "SDDLIN_0", "INT"),
        P("行状态", "LINSTA_0", "STRING"),
        P("行类型", "LINTYP_0", "STRING"),
        P("已发放产品", "ITMREFORI_0", "STRING", fk="ITMMASTER"),
        P("原始凭证类型", "VCRTYPORI_0", "STRING"),
        P("原始凭证", "VCRNUMORI_0", "STRING"),
        P("原始凭证行号", "VCRLINORI_0", "INT"),
        P("原始凭证序列号", "VCRSEQORI_0", "INT"),
        P("收货号", "PTHNUM_0", "STRING"),
        P("行", "PTDLIN_0", "INT"),
        P("导出编号", "EXPNUM_0", "INT"),
        P("日期时间", "CREDATTIM_0", "DATETIME"),
        P("更新时间", "UPDDATTIM_0", "DATETIME"),
        P("唯一标识符", "AUUID_0", "STRING"),
        P("到货数量", "ZQTYUOM_0", "DECIMAL"),
        P("钢厂", "YMILLS_0", "STRING"),
        P("TotalLINK PK", "XWSPK_0", "STRING"),
        P("SRM Line", "XSRMLIN_0", "INT"),
        # === 从 Excel 字段字典补全 123 条 ===
    ],
}

# =============================================================================
# 非外键业务流转 join（跨单据）+ 复合主键外键：source_table -> target_table
# 列按顺序一一配对（source_columns[i] 对应 target_columns[i]）。
# relation_type：foreign_key（复合主键外键）| business（无外键标志的业务流转）。
# =============================================================================
BUSINESS_JOINS = [
    # 到货明细 → 收货明细（收货单号 + 收货行）
    ("YPRECEIPTD", ["PTHNUM_0", "PTDLIN_0"], "PRECEIPTD", ["PTHNUM_0", "PTDLIN_0"],
     "business", "到货明细关联收货明细（收货单号+收货行）"),
    # 到货明细 → 采购订单明细（采购订单号 + 订单行）
    ("YPRECEIPTD", ["POHNUM_0", "POPLIN_0"], "PORDERQ", ["POHNUM_0", "POPLIN_0"],
     "business", "到货明细关联采购订单明细（采购订单号+订单行）"),
    # 收货明细 → 采购订单明细（采购订单号 + 订单行）
    ("PRECEIPTD", ["POHNUM_0", "POPLIN_0"], "PORDERQ", ["POHNUM_0", "POPLIN_0"],
     "business", "收货明细关联采购订单明细（采购订单号+订单行）"),
    # 请购订单关联 → 请购明细（复合主键）
    ("PREQUISO", ["PSHNUM_0", "PSDLIN_0"], "PREQUISD", ["PSHNUM_0", "PSDLIN_0"],
     "foreign_key", "请购订单关联到请购明细（复合主键）"),
    # 请购订单关联 → 采购订单明细（采购订单号 + 订单行）
    ("PREQUISO", ["POHNUM_0", "POPLIN_0"], "PORDERQ", ["POHNUM_0", "POPLIN_0"],
     "business", "请购订单关联到采购订单明细（采购订单号+订单行）"),
    # 供应商价格明细 → 供应商价格单（复合主键）
    ("PPRICLIST", ["PLI_0", "PLICRD_0"], "PPRICFICH", ["PLI_0", "PLICRD_0"],
     "foreign_key", "供应商价格明细关联供应商价格单（复合主键）"),
    # 采购订单明细 → 供应商价格明细（物料编码在 PLICRI2_0，实测 CPNITMREF_0 恒为空格）
    ("PORDERQ", ["ITMREF_0"], "PPRICLIST", ["PLICRI2_0"],
     "business", "采购订单明细按物料编码(PLICRI2_0)直连供应商价格明细，取报价/采购价对比"),
    # 价格配置 → 价格单 / 价格明细（价格表号 PLI_0）
    ("PPRICCONF", ["PLI_0"], "PPRICFICH", ["PLI_0"],
     "business", "价格配置按价格表号关联价格单（取价条件/优先级定义）"),
    ("PPRICCONF", ["PLI_0"], "PPRICLIST", ["PLI_0"],
     "business", "价格配置按价格表号关联价格明细（取价条件维度/优先级）"),
]

async def _seedClasses(session: Any) -> dict[str, int]:
    """幂等创建/复用类，返回 source_table -> class_id 映射。"""
    cid: dict[str, int] = {}
    for c in CLASSES:
        stmt = select(OntologyClass).where(OntologyClass.source_table == c["source_table"])
        existing = (await session.execute(stmt)).scalar_one_or_none()
        if existing:
            cid[c["source_table"]] = existing.id
            logger.info("class exists: %s (id=%d, name=%s)", c["source_table"], existing.id, existing.class_name)
        else:
            obj = OntologyClass(created_by="seed_ontology", **c)
            session.add(obj)
            await session.flush()
            cid[c["source_table"]] = obj.id
            logger.info("class created: %s (id=%d)", c["source_table"], obj.id)
    await session.commit()
    logger.info("classes ready: %d", len(cid))
    return cid

async def _seedProperties(
    session: Any, cid: dict[str, int]
) -> dict[tuple[int, str], int]:
    """幂等创建属性，返回 (class_id, property_name) -> property_id 供 Neo4j 同步。"""
    pid: dict[tuple[int, str], int] = {}
    n_created = 0
    n_skipped = 0
    for src_table, props in PROPERTIES.items():
        class_id = cid[src_table]
        for p in props:
            stmt = select(OntologyProperty).where(
                OntologyProperty.class_id == class_id,
                OntologyProperty.property_name == p["name"],
            )
            existing = (await session.execute(stmt)).scalar_one_or_none()
            if existing:
                pid[(class_id, p["name"])] = existing.id
                # 增量同步业务别名/描述（幂等可重放）：已存在属性仅「创建」时写
                # business_aliases，重跑 seed 会跳过，seed 中新增的别名会静默丢失
                # （真实回归 2026-08-17：PRECEIPTD.库存数量 补 aliases=["收货数量","入库数量"]）。
                # 只增不删：seed 未提供别名（None）时保留 DB 既有别名，避免抹掉
                # 管理端后续补充的同义词（如 YPRECEIPTD.收货数量 的 DB 别名「到货数量」）。
                seedAliases = p.get("aliases")
                if seedAliases and existing.business_aliases != seedAliases:
                    existing.business_aliases = seedAliases
                if p.get("desc") is not None and existing.description != p["desc"]:
                    existing.description = p["desc"]
                n_skipped += 1
                continue
            ref_id = cid.get(p["fk"]) if p.get("fk") else None
            obj = OntologyProperty(
                class_id=class_id,
                property_name=p["name"],
                property_alias=p["alias"],
                data_type=p["type"],
                is_primary_key=p.get("pk", False),
                is_foreign_key=bool(p.get("fk")),
                ref_class_id=ref_id,
                source_column=p["col"],
                business_aliases=p.get("aliases"),
                description=p.get("desc"),
            )
            session.add(obj)
            await session.flush()
            pid[(class_id, p["name"])] = obj.id
            n_created += 1
        await session.commit()
    logger.info("properties: created=%d skipped=%d", n_created, n_skipped)
    return pid

async def _seedJoins(session: Any, cid: dict[str, int]) -> None:
    """幂等物化 join 目录：外键（写对目标主键列）+ curated 业务流转。

    - 外键物化：目标表单主键 → 自动生成边（source=外键列, target=目标主键列），
      修复"源列名 ≠ 目标主键名"导致的 JOIN 接错（如 PRECEIPT.BPTNUM_0 → BPARTNER.BPRNUM_0）；
      目标表复合主键 → 跳过，交由 BUSINESS_JOINS 显式给出完整列对。
    - 业务流转：BUSINESS_JOINS 覆盖跨单据（无外键标志）与复合主键外键。
    按 join_key 幂等去重（与 service 共用 makeJoinKey），重跑不增行。
    """
    # 目标表主键列：{source_table: [pk_col, ...]}（单主键目标才能由单列外键自动物化）
    pk_cols: dict[str, list[str]] = {
        src: [p["col"] for p in props if p.get("pk")]
        for src, props in PROPERTIES.items()
    }

    # desc 可空：外键物化边无说明，BUSINESS_JOINS 带说明
    edges: list[tuple[str, list[str], str, list[str], str, str | None]] = []

    # 1) 外键物化：目标表单主键 → source=外键列, target=[目标主键列]
    for src_table, props in PROPERTIES.items():
        for p in props:
            fk_table: str = p.get("fk")
            if not fk_table:
                continue
            tgt_pks = pk_cols.get(fk_table, [])
            if len(tgt_pks) != 1:
                continue  # 复合主键：交由 BUSINESS_JOINS
            edges.append((src_table, [p["col"]], fk_table, tgt_pks, "foreign_key", None))

    # 2) curated 业务流转（含复合主键外键）
    edges.extend(BUSINESS_JOINS)

    n_created = 0
    n_skipped = 0
    for src_table, src_cols, tgt_table, tgt_cols, rel_type, desc in edges:
        src_id = cid[src_table]
        tgt_id = cid[tgt_table]
        key = makeJoinKey(src_id, src_cols, tgt_id, tgt_cols)
        existing = (await session.execute(
            select(OntologyJoin).where(OntologyJoin.join_key == key)
        )).scalar_one_or_none()
        if existing:
            n_skipped += 1
            continue
        session.add(OntologyJoin(
            source_class_id=src_id,
            source_columns=src_cols,
            target_class_id=tgt_id,
            target_columns=tgt_cols,
            join_type="INNER",
            relation_type=rel_type,
            description=desc,
            join_key=key,
            created_by="seed_ontology",
        ))
        n_created += 1
    await session.commit()
    logger.info("joins: created=%d skipped=%d", n_created, n_skipped)

def _syncToNeo4j(
    cid: dict[str, int], pid: dict[tuple[int, str], int]
) -> None:
    """将种子本体同步到 Neo4j（幂等 MERGE + 关系）。

    cid: source_table -> class_id；pid: (class_id, property_name) -> property_id。
    单个节点/属性失败只跳过该项并告警，不中断其余同步（PG 已就绪，可重跑补齐）。
    """
    for c in CLASSES:
        try:
            neo4j.upsertClassNode(
                id=cid[c["source_table"]],
                name=c["class_name"],
                alias=c["class_alias"],
                description=c["description"],
                sourceTable=c["source_table"],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Neo4j 同步失败 Class %s: %s", c["source_table"], exc)
    for src_table, props in PROPERTIES.items():
        class_id = cid[src_table]
        for p in props:
            try:
                prop_id = pid[(class_id, p["name"])]
                neo4j.upsertPropertyNode(
                    id=prop_id,
                    name=p["name"],
                    alias=p["alias"],
                    dataType=p["type"],
                    sourceColumn=p["col"],
                    isPrimaryKey=p.get("pk", False),
                    isForeignKey=bool(p.get("fk")),
                )
                neo4j.linkClassHasProperty(class_id, prop_id)
                if p.get("fk"):
                    neo4j.linkPropertyReferences(prop_id, cid[p["fk"]])
            except Exception as exc:  # noqa: BLE001
                logger.warning("Neo4j 同步失败属性 %s.%s: %s", src_table, p["name"], exc)

async def seed() -> None:
    engine = getEngine()
    engine.echo = False  # 关闭 SQL 回显，避免 seed 输出刷屏
    factory = getSessionFactory()

    async with factory() as session:
        cid = await _seedClasses(session)
        pid = await _seedProperties(session, cid)
        await _seedJoins(session, cid)
        # Neo4j 本体图同步（阻塞 I/O 移到线程，避免阻塞事件循环）
        await asyncio.to_thread(_syncToNeo4j, cid, pid)

    await engine.dispose()
    logger.info("done.")

if __name__ == "__main__":
    asyncio.run(seed())
