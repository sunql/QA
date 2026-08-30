# THBI 数仓建仓

把 ZJTH（Sage X3）中本体映射的 27 张表复制抽取到 THBI 用户，按
`原始表 -> ODS -> DWD -> DIM -> DWS -> ADS` 分层组织。
设计详见 `Harness/changes/feat-dw-layering-thbi/summary.md`。

## 分层结构

| 层 | 文件 | 命名 | 策略 |
|---|---|---|---|
| ODS | `01_ods.sql` | `ODS_<原表名>` | 27 表全字段贴源 + ETL_LOAD_TS，保留 X3 列名 |
| DWD | `02_dwd_master.sql` / `03_dwd_facts.sql` | `DWD_<实体>` | 25 表精选核心字段，英文 snake_case，清洗（1599 日期/空串 -> NULL） |
| DIM | `04_dim.sql` | `DIM_<实体>` | 日期/供应商/物料/工厂/货币，含代理键 |
| DWS | `05_dws.sql` | `DWS_<主题>_MONTHLY` | 交付（OTD）/质量/采购/付款/价格 月度汇总 |
| ADS | `06_ads.sql` | `ADS_<应用>` | Supplier 360° 视图 + 订单明细宽表视图 |

## 执行（backend venv）

```bash
cd backend

# 全量建仓（幂等，可重跑；THBI 密码走环境变量）
THBI_PASSWORD=*** ./.venv/bin/python ../dw/run_build.py --phase all

# 单阶段（依赖顺序：ods -> dwd -> dim -> dws -> ads）
THBI_PASSWORD=*** ./.venv/bin/python ../dw/run_build.py --phase dwd

# 校验：ODS 行数对账 + 清洗校验 + OTD 链路抽样
THBI_PASSWORD=*** ./.venv/bin/python ../dw/run_build.py --phase all --verify
```

## 关键口径备忘

- **OTD**：收货行（`DWD_GOODS_RECEIPT_LINE`）按 `po_no + po_line_no` 关联 PO 行，
  准时 = `receipt_date <= promised_receipt_date`（承诺日期 = `PORDERQ.EXTRCPDAT_0`）
- **拒收率**：`rejected_qty / received_qty_price_uom`（同表 QTYPUU 口径内部一致）
- **X3 清洗**：空日期占位符 `1599-12-31` -> NULL；空串 `' '` -> NULL
- `DEMRCPDAT_0`（需求日期）全为 1599 占位不可用；`PPRICFICH/PPRICCONF` 关联键待业务确认，暂留 ODS
