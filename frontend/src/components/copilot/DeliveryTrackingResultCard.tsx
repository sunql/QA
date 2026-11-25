/** 到货跟踪结果卡（P5）：主指标「订单到货率」+ 量级率参考 + 供应商表 + 双 Tab 订单明细。
 *
 * 主指标口径（spec 4.2）：业务看「订单是否到货」，因此大号数字用订单到货率；
 * 量级加权到货率受超大额未到货行影响，仅作参考 —— 两者位置不可对调。
 *
 * 数据源 = delivery_tracking_query 工具的 observation JSON（camelCase）；
 * 坏 JSON / 不匹配 ⇒ null（回退纯文本答案，结果卡不经 LLM 复述）。
 * 脏数据（`excludedRows` / `hasDirtyLines` / `isDirty`）单独标注，不参与统计。
 */

import { Card, Table, Tabs, Tag, Typography } from "antd";
import type { CopilotStepPayload } from "../../api/procurementCopilot";
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

/** 从 delivery_tracking_query 工具步骤解析结构化结果；不匹配/坏 JSON ⇒ null。 */
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
    };
  } catch {
    return null;
  }
}

function fmtPct(value: number | undefined): string {
  if (value === undefined || value === null) return "-";
  return `${value.toFixed(2)}%`;
}

/** `thbi_ods_erp` ⇒ 「数仓层 ODS_ERP」，避免与「待到货」场景的数据层混淆。 */
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

  return (
    <div>
      <SummaryHeader result={result} />

      <Card size="small" title="供应商到货情况" style={{ marginBottom: 12 }}>
        <SupplierTable suppliers={result.bySupplier} />
      </Card>

      <Card size="small">
        {result.ordersTruncated && (
          <Tag color="gold" data-testid="delivery-orders-truncated" style={{ marginBottom: 8 }}>
            {`订单明细已截断：仅展示欠交量前 ${result.byOrder.length} 单（总订单以概览为准）`}
          </Tag>
        )}
        <Tabs
          items={[
            {
              key: "open",
              label: openLabel,
              children: <OrderTable orders={openOrders} />,
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
    </div>
  );
}
