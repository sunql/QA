/** 到货跟踪结果卡的订单明细子表格（从 DeliveryTrackingResultCard 拆出，控制单文件体积）。
 *
 * 双 Tab 的订单表 + 可展开的物料行明细；脏行保留供核查但不参与统计，
 * 因此行内标「脏行·不参与统计」并弱化视觉。
 */

import { Collapse, Table, Tag, Typography } from "antd";
import type {
  DeliveryOrderStatus,
  DeliveryTrackingLine,
  DeliveryTrackingOrder,
} from "./DeliveryTrackingResultCard";

const { Text } = Typography;

const STATUS_COLOR: Record<DeliveryOrderStatus, string> = {
  all: "green",
  partial: "orange",
  notArrived: "red",
  // 全脏行订单：summary 不把它算进任何一态 ⇒ 单独标记，避免与未到货混淆
  dirtyOnly: "gold",
};

const STATUS_LABEL: Record<DeliveryOrderStatus, string> = {
  all: "已到货",
  partial: "部分到货",
  notArrived: "未到货",
  dirtyOnly: "仅脏数据行",
};

const DIRTY_ROW_COLOR = "#999999";

/** 数量最多 3 位小数（源侧探针实测 209,421,924.058），多于此位才截断。 */
export function fmtQty(value: number | undefined): string {
  if (value === undefined || value === null) return "-";
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 3 }).format(value);
}

function fmtDate(value?: string | null): string {
  if (!value) return "-";
  return value.slice(0, 10);
}

/** 供应商展示：名称在上、编码在下（无名称时退化为编码）。 */
export function SupplierCell({ name, code }: { name?: string | null; code: string }) {
  return (
    <div>
      <div>{name ?? code}</div>
      {name && (
        <Text type="secondary" style={{ fontSize: 12 }}>
          {code}
        </Text>
      )}
    </div>
  );
}

export function StatusTag({ status }: { status: DeliveryOrderStatus }) {
  return <Tag color={STATUS_COLOR[status]}>{STATUS_LABEL[status]}</Tag>;
}

function OrderLinesTable({ lines }: { lines: DeliveryTrackingLine[] }) {
  return (
    <Table<DeliveryTrackingLine>
      size="small"
      pagination={false}
      rowKey="poLine"
      dataSource={lines}
      onRow={(line) => (line.isDirty ? { style: { color: DIRTY_ROW_COLOR } } : {})}
      columns={[
        { title: "行号", dataIndex: "poLine" },
        {
          title: "物料",
          render: (_: unknown, line: DeliveryTrackingLine) => (
            <div>
              <div>{line.materialDesc ?? "-"}</div>
              {line.materialCode && (
                <Text type="secondary" style={{ fontSize: 12 }}>
                  {line.materialCode}
                </Text>
              )}
            </div>
          ),
        },
        {
          title: "订购",
          render: (_: unknown, line: DeliveryTrackingLine) => fmtQty(line.orderedQty),
        },
        {
          title: "已收",
          render: (_: unknown, line: DeliveryTrackingLine) => fmtQty(line.receivedQty),
        },
        {
          title: "欠交",
          render: (_: unknown, line: DeliveryTrackingLine) => fmtQty(line.openQty),
        },
        {
          title: "预计到货",
          render: (_: unknown, line: DeliveryTrackingLine) => fmtDate(line.expectedDate),
        },
        {
          title: "最后收货",
          render: (_: unknown, line: DeliveryTrackingLine) => fmtDate(line.lastReceiptDate),
        },
        {
          title: "状态",
          render: (_: unknown, line: DeliveryTrackingLine) => (
            <span>
              <StatusTag status={line.status} />
              {line.isDirty && <Tag>脏行·不参与统计</Tag>}
            </span>
          ),
        },
      ]}
    />
  );
}

export function OrderTable({ orders }: { orders: DeliveryTrackingOrder[] }) {
  return (
    <Table<DeliveryTrackingOrder>
      size="small"
      pagination={false}
      rowKey="poNo"
      dataSource={orders}
      columns={[
        { title: "订单号", dataIndex: "poNo" },
        {
          title: "供应商",
          render: (_: unknown, row: DeliveryTrackingOrder) => (
            <SupplierCell name={row.supplierName} code={row.supplierCode} />
          ),
        },
        {
          title: "下单日期",
          render: (_: unknown, row: DeliveryTrackingOrder) => fmtDate(row.orderDate),
        },
        {
          title: "状态",
          render: (_: unknown, row: DeliveryTrackingOrder) => (
            <span>
              <StatusTag status={row.status} />
              {row.hasDirtyLines && <Tag color="gold">含脏数据行</Tag>}
            </span>
          ),
        },
        {
          title: "行数",
          render: (_: unknown, row: DeliveryTrackingOrder) =>
            `${row.arrivedLines}/${row.totalLines}`,
        },
        {
          title: "订购",
          render: (_: unknown, row: DeliveryTrackingOrder) => fmtQty(row.orderedQty),
        },
        {
          title: "已收",
          render: (_: unknown, row: DeliveryTrackingOrder) => fmtQty(row.receivedQty),
        },
        {
          title: "明细",
          render: (_: unknown, row: DeliveryTrackingOrder) => (
            <Collapse
              size="small"
              items={[
                {
                  key: "lines",
                  label: `物料明细（${row.lines.length} 行）`,
                  children: <OrderLinesTable lines={row.lines} />,
                },
              ]}
            />
          ),
        },
      ]}
    />
  );
}
