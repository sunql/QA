/** 到货跟踪结果卡（P5）：主指标「订单到货率」+ 量级率参考 + 供应商表 + 双 Tab 订单明细。
 *
 * 主指标口径（spec 4.2）：业务看「订单是否到货」，因此大号数字用订单到货率；
 * 量级加权到货率受超大额未到货行影响，仅作参考 —— 两者位置不可对调。
 *
 * 数据源 = delivery_tracking_query 工具的 observation JSON（camelCase）；
 * 坏 JSON / 不匹配 ⇒ null（回退纯文本答案，结果卡不经 LLM 复述）。
 * 脏数据（`excludedRows` / `hasDirtyLines` / `isDirty`）单独标注，不参与统计。
 *
 * **降级（第二道防线）**：观测 JSON 解析失败（多半是被编排按
 * `max_observation_chars` 截断）⇒ 返回 `truncated` 结果并抢救出**概要**
 * （summary / window 在载荷靠前，通常仍完整）⇒ 卡片只展示概要 + 提示，
 * 绝不空白、绝不崩。
 */

import { Alert, Button, Card, Table, Tabs, Tag, Typography, message } from "antd";
import { useState, type Key as ReactKey } from "react";
import { useTranslation } from "react-i18next";
import type { CopilotStepPayload } from "../../api/procurementCopilot";
import {
  createChaseFromPending,
  type ChaseLineInput,
} from "../../api/procurementChase";
import { useAuthStore } from "../../stores/authStore";
import { OrderTable, SupplierCell, fmtQty } from "./DeliveryTrackingOrderTables";

const { Text } = Typography;

export type DeliveryOrderStatus = "all" | "partial" | "notArrived" | "dirtyOnly";

export interface DeliveryTrackingWindow {
  dateFrom: string;
  dateTo: string;
  mode: string;
}

export interface DeliveryTrackingSummary {
  totalLines: number;
  totalOrders: number;
  orderedQty: number;
  receivedQty: number;
  orderArrivalRate: number;
  qtyArrivalRate: number;
  allArrivedOrders: number;
  partialArrivedOrders: number;
  notArrivedOrders: number;
  excludedRows: number;
}

export interface DeliveryTrackingSupplier {
  supplierCode: string;
  supplierName?: string | null;
  totalOrders: number;
  allArrivedOrders: number;
  partialArrivedOrders: number;
  notArrivedOrders: number;
  orderArrivalRate: number;
  orderedQty: number;
  receivedQty: number;
}

export interface DeliveryTrackingLine {
  poLine: number;
  materialCode?: string | null;
  materialDesc?: string | null;
  orderedQty: number;
  receivedQty: number;
  openQty: number;
  expectedDate?: string | null;
  lastReceiptDate?: string | null;
  status: DeliveryOrderStatus;
  /** 任务 4 新增：该行是脏行（欠交量超阈值），保留供核查但不参与统计。 */
  isDirty?: boolean;
}

export interface DeliveryTrackingOrder {
  poNo: string;
  supplierCode: string;
  supplierName?: string | null;
  orderDate?: string | null;
  status: DeliveryOrderStatus;
  /** 任务 4 新增：该订单含脏行。 */
  hasDirtyLines?: boolean;
  /** 任务 12 新增：该订单的行明细被体积预算裁剪（只展示部分行）。 */
  linesTruncated?: boolean;
  totalLines: number;
  arrivedLines: number;
  notArrivedLines: number;
  orderedQty: number;
  receivedQty: number;
  lines: DeliveryTrackingLine[];
}

export interface DeliveryTrackingResult {
  dataLayer?: string | null;
  window: DeliveryTrackingWindow;
  summary: DeliveryTrackingSummary;
  bySupplier: DeliveryTrackingSupplier[];
  byOrder: DeliveryTrackingOrder[];
  /** 明细只是欠交量前 N 单样本（还有更多单）——不得把样本当全量计数。 */
  ordersTruncated?: boolean;
  /** 供应商分组被裁剪（只展示未到货订单数最多的前 N 家）。 */
  suppliersTruncated?: boolean;
  /**
   * 观测 JSON 被截断/损坏 ⇒ 只展示概要（summary / window）。
   * 载荷里若还有 summary / window 就抢救出来展示，其余部分留空。
   */
  truncated?: boolean;
  /** 降级提示文案（给用户看的原因说明）。 */
  truncatedNotice?: string;
}

/** 三态排序权重：未到货 0、部分 1、已到货 2、仅脏数据 3（越小越靠前）。 */
const STATUS_RANK: Record<DeliveryOrderStatus, number> = {
  notArrived: 0,
  partial: 1,
  all: 2,
  dirtyOnly: 3,
};

const PRIMARY_RATE_FONT_SIZE = 28;
const REFERENCE_RATE_FONT_SIZE = 12;
const SECONDARY_COLOR = "#8c8c8c";
const DATA_LAYER_PREFIX = "thbi_";

/** 降级提示文案（观测被截断 ⇒ 明细不可用，只展示概要）。 */
const TRUNCATED_NOTICE = "结果数据过大，仅展示概要";

/**
 * 从**损坏/截断**的观测文本里抢救概要（summary / window）。
 *
 * 载荷的字段顺序是 `window` → `summary` → `bySupplier` → `byOrder`，所以被
 * 截断时**概要通常仍然完整** —— 抢救出来就能继续展示主指标，而不是整卡空白。
 */
function salvageOverview(text: string): {
  window?: DeliveryTrackingWindow;
  summary?: DeliveryTrackingSummary;
} {
  const overview: { window?: DeliveryTrackingWindow; summary?: DeliveryTrackingSummary } = {};
  const windowMatch = /"window"\s*:\s*(\{[^}]*\})/.exec(text);
  if (windowMatch) {
    try {
      overview.window = JSON.parse(windowMatch[1]) as DeliveryTrackingWindow;
    } catch {
      // 概要也坏了 ⇒ 交给调用方走空态提示
    }
  }
  const summaryMatch = /"summary"\s*:\s*(\{[^{}]*\})/.exec(text);
  if (summaryMatch) {
    try {
      overview.summary = JSON.parse(summaryMatch[1]) as DeliveryTrackingSummary;
    } catch {
      // 同上
    }
  }
  return overview;
}

/** 从 delivery_tracking_query 工具步骤解析结构化结果；不匹配 ⇒ null。 */
export function parseDeliveryTrackingStep(
  step: CopilotStepPayload,
): DeliveryTrackingResult | null {
  if (step.toolName !== "delivery_tracking_query") return null;
  if (step.status !== "SUCCEEDED") return null;
  const text = (step.output as { text?: string } | null | undefined)?.text;
  if (!text) return null;
  try {
    const parsed = JSON.parse(text) as { data?: Partial<DeliveryTrackingResult> };
    const data = parsed.data;
    if (
      !data?.window ||
      !data.summary ||
      !Array.isArray(data.bySupplier) ||
      !Array.isArray(data.byOrder)
    ) {
      return null;
    }
    return {
      dataLayer: data.dataLayer,
      window: data.window,
      summary: data.summary,
      bySupplier: data.bySupplier,
      byOrder: data.byOrder,
      ordersTruncated: data.ordersTruncated === true,
      suppliersTruncated: data.suppliersTruncated === true,
    };
  } catch {
    // 观测被截断（多半是编排的 max_observation_chars）⇒ 降级：只展示概要。
    const salvaged = salvageOverview(text);
    if (!salvaged.summary && !salvaged.window) return null;
    return {
      window: salvaged.window ?? { dateFrom: "", dateTo: "", mode: "unknown" },
      summary:
        salvaged.summary ??
        ({
          totalLines: 0,
          totalOrders: 0,
          orderedQty: 0,
          receivedQty: 0,
          orderArrivalRate: 0,
          qtyArrivalRate: 0,
          allArrivedOrders: 0,
          partialArrivedOrders: 0,
          notArrivedOrders: 0,
          excludedRows: 0,
        } satisfies DeliveryTrackingSummary),
      bySupplier: [],
      byOrder: [],
      ordersTruncated: true,
      suppliersTruncated: true,
      truncated: true,
      truncatedNotice: TRUNCATED_NOTICE,
    };
  }
}

function fmtPct(value: number | undefined): string {
  if (value === undefined || value === null) return "-";
  return `${value.toFixed(2)}%`;
}

/**
 * `thbi_dwd` ⇒ 「数仓层 DWD」。
 *
 * **事实更正（2026-11-26）**：原值 `thbi_ods_erp` 来自设计期「ODS_ERP 是原始表、
 * DWD 是宽表」的对照，但本体迁移后**两个采购工具读的是同一批 DWD 宽表**
 * ⇒ 「源层 vs 数仓层」的区分已不存在，角标继续写 ODS_ERP 属事实错误。
 */
function dataLayerText(layer?: string | null): string | null {
  if (!layer) return null;
  const bare = layer.startsWith(DATA_LAYER_PREFIX)
    ? layer.slice(DATA_LAYER_PREFIX.length)
    : layer;
  return `数仓层 ${bare.toUpperCase()}`;
}

function SummaryHeader({ result }: { result: DeliveryTrackingResult }) {
  const { summary, window, dataLayer } = result;
  const layerText = dataLayerText(dataLayer);

  return (
    <div data-testid="delivery-primary-metric" style={{ marginBottom: 12 }}>
      <Text strong style={{ fontSize: 16 }}>
        到货达成概览
      </Text>
      <Text type="secondary" style={{ marginLeft: 8, fontSize: 12 }}>
        {`窗口 ${window.dateFrom} ~ ${window.dateTo}`}
      </Text>
      {layerText && <Tag style={{ marginLeft: 8 }}>{layerText}</Tag>}

      <div style={{ marginTop: 8 }}>
        <Text type="secondary">订单到货率</Text>
        <span
          data-testid="delivery-primary-rate"
          style={{
            fontSize: PRIMARY_RATE_FONT_SIZE,
            fontWeight: 600,
            marginLeft: 8,
          }}
        >
          {fmtPct(summary.orderArrivalRate)}
        </span>
      </div>
      <div style={{ marginTop: 4 }}>
        <span
          data-testid="delivery-qty-rate-reference"
          style={{ fontSize: REFERENCE_RATE_FONT_SIZE, color: SECONDARY_COLOR }}
        >
          {"量级加权到货率 "}
          <span style={{ fontWeight: 600 }}>{fmtPct(summary.qtyArrivalRate)}</span>
          {"（受超大额未到货行影响，仅作参考）"}
        </span>
      </div>

      <div style={{ marginTop: 8 }}>
        <Tag color="green">{`全到货 ${summary.allArrivedOrders}`}</Tag>
        <Tag color="orange">{`部分到货 ${summary.partialArrivedOrders}`}</Tag>
        <Tag color="red">{`未到货 ${summary.notArrivedOrders}`}</Tag>
        <Tag>{`订单 ${summary.totalOrders}`}</Tag>
        <Tag>{`总行数 ${summary.totalLines}`}</Tag>
        {summary.excludedRows > 0 && (
          <Tag>{`脏数据剔除 ${summary.excludedRows} 行`}</Tag>
        )}
      </div>
    </div>
  );
}

function SupplierTable({ suppliers }: { suppliers: DeliveryTrackingSupplier[] }) {
  return (
    <div data-testid="delivery-supplier-table">
      <Table<DeliveryTrackingSupplier>
        size="small"
        pagination={false}
        rowKey="supplierCode"
        dataSource={suppliers}
        columns={[
          {
            title: "供应商",
            render: (_: unknown, row: DeliveryTrackingSupplier) => (
              <SupplierCell name={row.supplierName} code={row.supplierCode} />
            ),
          },
          { title: "订单数", dataIndex: "totalOrders" },
          { title: "全到", dataIndex: "allArrivedOrders" },
          { title: "部分", dataIndex: "partialArrivedOrders" },
          { title: "未到", dataIndex: "notArrivedOrders" },
          {
            title: "订单到货率",
            render: (_: unknown, row: DeliveryTrackingSupplier) =>
              fmtPct(row.orderArrivalRate),
          },
          {
            title: "欠交量",
            render: (_: unknown, row: DeliveryTrackingSupplier) =>
              fmtQty(row.orderedQty - row.receivedQty),
          },
        ]}
      />
    </div>
  );
}

export default function DeliveryTrackingResultCard({
  result,
}: {
  result: DeliveryTrackingResult;
}) {
  // 催货闭环（feat/delivery-chase-closed-loop）：勾选未到货行 → 按供应商创建催货单。
  // 本组件只负责「选行 + 提交身份对」；数量/供应商分组由服务端回查决定（防篡改）。
  const { t } = useTranslation();
  const [selectedKeys, setSelectedKeys] = useState<ReactKey[]>([]);
  const [creating, setCreating] = useState(false);
  const isAdmin = useAuthStore((s) => (s.user?.roles ?? []).includes("admin"));
  const selectedCount = selectedKeys.length;
  // filter/sort 返回新数组，不改入参；未到货订单排在前（业务先看问题单）
  const openOrders = result.byOrder
    .filter((order) => order.status === "partial" || order.status === "notArrived")
    .sort((a, b) => STATUS_RANK[a.status] - STATUS_RANK[b.status]);
  const arrivedOrders = result.byOrder.filter((order) => order.status === "all");
  // 全脏行订单：summary 不把它计入任何一态（LINE_COUNT=0）⇒ 单独成组，
  // 否则 tab 计数会比 summary 的 notArrivedOrders 多。
  const dirtyOrders = result.byOrder.filter((order) => order.status === "dirtyOnly");
  const openLabel = result.ordersTruncated
    ? `未到货订单（TOP ${openOrders.length}，已截断）`
    : `未到货订单（${openOrders.length}）`;

  /** 把勾选的未到货行提交为催货单（按供应商分组在服务端）。
   *  只传 {poNo, poLine} 身份对 —— 数量/供应商由服务端回查，前端传的会被忽略。 */
  const createChases = async () => {
    if (selectedCount === 0) {
      void message.warning(t("chase.create.empty"));
      return;
    }
    setCreating(true);
    try {
      const lines: ChaseLineInput[] = [];
      // key 必须与明细表 rowKey **逐字同格式**（`poNo:poLine`）——
      // 两边格式不一致 ⇒ 「勾了但一个都找不到」⇒ 误报「请先勾选」。
      // 用 String() 归一比较（antd 的 Key 可能是 number）。
      const selected = new Set(selectedKeys.map((key) => String(key)));
      for (const order of openOrders) {
        for (const line of order.lines) {
          const key = `${order.poNo}:${line.poLine}`;
          // ⚠️ **不按状态/脏行过滤**（用户 2026-11-27 明确：所有订单行都可下催单）。
          if (selected.has(key)) {
            lines.push({ poNo: order.poNo, poLine: String(line.poLine) });
          }
        }
      }
      if (lines.length === 0) {
        void message.warning(t("chase.create.empty"));
        return;
      }
      const created = await createChaseFromPending({ lines });
      setSelectedKeys([]);
      // 成功提示里带「查看催货单」跳转链接（antd message 支持 ReactNode）：
      // 催货单落在**另一个页面**，不给出路的话用户不知道东西去了哪。
      // ⚠️ 用**普通 <a href>** 而不是 router 的 <Link>：antd 的静态 message
      //    渲染在**独立的 React root** 里 ⇒ 拿不到 Router context（<Link>
      //    会抛 "Cannot destructure property 'basename' of useContext(...)"）。
      //    href 由 nginx 的 SPA fallback（try_files … /index.html）兜住。
      const viewLink = (
        <>
          {" "}
          <a href="/procurement/chase">{t("chase.viewList")}</a>
        </>
      );
      if (created.unresolvedSuppliers.length > 0) {
        void message.warning(
          <>
            {t("chase.create.partial", {
              count: String(created.drafts.length),
              unresolved: created.unresolvedSuppliers.join(", "),
            })}
            {viewLink}
          </>,
        );
      } else {
        void message.success(
          <>
            {t("chase.create.success", {
              count: String(created.drafts.length),
              suppliers: String(new Set(created.drafts.map((d) => d.supplierCode)).size),
            })}
            {viewLink}
          </>,
        );
      }
    } catch {
      // interceptor 已提示
    } finally {
      setCreating(false);
    }
  };;
  // 降级（观测被截断）⇒ 只有概要，明细表格没有意义 ⇒ 不渲染空表。
  const degraded = result.truncated === true;

  return (
    <div>
      {degraded && (
        <Alert
          type="warning"
          showIcon
          data-testid="delivery-truncated-notice"
          message={result.truncatedNotice ?? TRUNCATED_NOTICE}
          description="明细数据超出单次观测上限，已省略；上方概要仍为完整口径。"
          style={{ marginBottom: 12 }}
        />
      )}

      <SummaryHeader result={result} />

      {!degraded && (
        <Card size="small" title="供应商到货情况" style={{ marginBottom: 12 }}>
          <SupplierTable suppliers={result.bySupplier} />
        </Card>
      )}

      {!degraded && (
        <Card size="small">
          {result.ordersTruncated && (
            <Tag color="gold" data-testid="delivery-orders-truncated" style={{ marginBottom: 8 }}>
              {`订单明细已截断：仅展示欠交量前 ${result.byOrder.length} 单（总订单以概览为准）`}
            </Tag>
          )}
          {result.suppliersTruncated && (
            <Tag color="gold" data-testid="delivery-suppliers-truncated" style={{ marginBottom: 8 }}>
              {`供应商分组已截断：仅展示未到货订单数最多的 ${result.bySupplier.length} 家`}
            </Tag>
          )}
          <Tabs
            items={[
              {
                key: "open",
                label: openLabel,
                children: (
                  <>
                    <OrderTable
                      orders={openOrders}
                      rowSelection={{
                        selectedRowKeys: selectedKeys,
                        onChange: (keys) => setSelectedKeys([...keys]),
                        // ⚠️ **不按状态/脏行禁用**（用户 2026-11-27 明确：
                        //    所有订单行都可下催单 —— 不管是不是脏数据、不管是不是部分到货）。
                        //    脏行与已到货行照样能催（催单快照里带 openQty 供人核对）。
                        getCheckboxProps: () => ({ disabled: false }),
                      }}
                    />
                    {isAdmin && (
                      <div style={{ marginTop: 8, textAlign: "right" }}>
                        <Button
                          type="primary"
                          disabled={selectedCount === 0}
                          loading={creating}
                          onClick={() => void createChases()}
                        >
                          {t("chase.create.button")}（{selectedCount}）
                        </Button>
                      </div>
                    )}
                  </>
                ),
              },
              {
                key: "arrived",
                label: `已到货订单（${arrivedOrders.length}）`,
                children: <OrderTable orders={arrivedOrders} />,
              },
              ...(dirtyOrders.length > 0
                ? [
                    {
                      key: "dirty",
                      label: `数据异常订单（${dirtyOrders.length}）`,
                      children: <OrderTable orders={dirtyOrders} />,
                    },
                  ]
                : []),
            ]}
          />
        </Card>
      )}
    </div>
  );
}
