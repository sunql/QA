/** 待到货场景的结构化结果卡：数据直接来自工具 outputJson，不经 LLM 复述。
 *
 * 背景（2026-11-23 生产实测）：planner 把 bySupplier/topLines 复述成大段文字，
 * 不可读。修法：TOOL 步骤的 observation（`{"text": "<json>", "truncated": …}`）
 * 里本来就有结构化数据 ⇒ 前端解析渲染（统计块 + 供应商表 + TOP 行表）；
 * LLM 的文字只保留「建议」部分，显示在卡片下方。
 */

import { Button, Card, Col, Row, Statistic, Table, Tag, Typography } from "antd";
import { DownloadOutlined } from "@ant-design/icons";
import { downloadCsv } from "../../utils/download";
import type { CopilotStepPayload } from "../../api/procurementCopilot";

const { Text } = Typography;

export interface PendingDeliverySummary {
  openLines: number;
  openQty: number;
  overdueLines: number;
  excludedRows: number;
}

export interface PendingDeliverySupplier {
  supplierCode: string;
  openLines: number;
  openQty: number;
  overdueLines: number;
}

export interface PendingDeliveryLine {
  poNo: string;
  poLine: number;
  materialCode: string;
  supplierCode: string;
  orderedQty: number;
  receivedQty: number;
  openQty: number;
  expectedDate: string | null;
  overdueDays: number;
}

export interface PendingDeliveryResult {
  summary: PendingDeliverySummary;
  bySupplier: PendingDeliverySupplier[];
  topLines: PendingDeliveryLine[];
}

/** 从 TOOL 步骤里解析待到货结构化结果；任何不匹配/坏 JSON ⇒ null（回退纯文本）。 */
export function parsePendingDeliveryStep(
  step: CopilotStepPayload,
): PendingDeliveryResult | null {
  if (step.toolName !== "pending_delivery_query") return null;
  if (step.status !== "SUCCEEDED") return null;
  const text = (step.output as { text?: string } | null | undefined)?.text;
  if (!text) return null;
  try {
    const parsed = JSON.parse(text) as {
      data?: { summary?: PendingDeliverySummary; bySupplier?: PendingDeliverySupplier[]; topLines?: PendingDeliveryLine[] };
    };
    const data = parsed.data;
    if (!data || !data.summary || !Array.isArray(data.bySupplier) || !Array.isArray(data.topLines)) {
      return null;
    }
    return {
      summary: data.summary,
      bySupplier: data.bySupplier,
      topLines: data.topLines,
    };
  } catch {
    return null;
  }
}

function fmtQty(value: number | undefined): string {
  if (value === undefined || value === null) return "-";
  // 源侧数量最多 3 位小数（探针实测 209,421,924.058），多于此位才截断
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 3 }).format(value);
}

export default function PendingDeliveryResultCard({
  result,
}: {
  result: PendingDeliveryResult;
}) {
  const { summary, bySupplier, topLines } = result;

  /** 查询数据集导出：明细行为主表 + 空行分隔的供应商汇总段（CSV，Excel 可直接打开）。 */
  const handleExport = () => {
    const detailRows = topLines.map((row) => ({
      订单号: row.poNo,
      行号: row.poLine,
      物料编码: row.materialCode,
      供应商: row.supplierCode,
      订购量: row.orderedQty,
      已收量: row.receivedQty,
      欠交量: row.openQty,
      预计到货: row.expectedDate ?? "",
      逾期天数: row.overdueDays,
    }));
    const supplierRows = bySupplier.map((row) => ({
      订单号: `[供应商汇总] ${row.supplierCode}`,
      行号: "",
      物料编码: "",
      供应商: row.supplierCode,
      订购量: "",
      已收量: "",
      欠交量: row.openQty,
      预计到货: "",
      逾期天数: row.overdueLines,
    }));
    downloadCsv([...detailRows, ...supplierRows], `待到货明细_${Date.now()}.csv`);
  };

  return (
    <Card
      size="small"
      style={{ marginBottom: 8 }}
      title="待到货分析结果"
      extra={
        <Button size="small" icon={<DownloadOutlined />} onClick={handleExport}>
          导出明细(CSV)
        </Button>
      }
    >
      <Row gutter={12}>
        <Col span={6}>
          <Statistic title="欠交行数" value={summary.openLines} />
        </Col>
        <Col span={6}>
          {/* 金额/数量带小数：antd Statistic 会按默认精度重排，这里原样输出 */}
          <div>
            <Text type="secondary" style={{ display: "block", fontSize: 12 }}>
              欠交总量
            </Text>
            <Text strong style={{ fontSize: 20 }}>
              {fmtQty(summary.openQty)}
            </Text>
          </div>
        </Col>
        <Col span={6}>
          <Statistic
            title="逾期行数"
            value={summary.overdueLines}
            valueStyle={summary.overdueLines > 0 ? { color: "#cf1322" } : undefined}
          />
        </Col>
        <Col span={6}>
          <Statistic title="剔除异常行" value={summary.excludedRows} />
        </Col>
      </Row>

      <Text strong style={{ display: "block", margin: "12px 0 4px" }}>
        按供应商汇总（欠交量 TOP）
      </Text>
      <Table
        size="small"
        pagination={false}
        rowKey={(row) => `${row.supplierCode}`}
        dataSource={bySupplier}
        columns={[
          { title: "供应商", dataIndex: "supplierCode" },
          { title: "欠交行数", dataIndex: "openLines" },
          { title: "欠交量", render: (_: unknown, row) => fmtQty(row.openQty) },
          {
            title: "逾期行数",
            render: (_: unknown, row) =>
              row.overdueLines > 0 ? (
                <Tag color="red">{row.overdueLines}</Tag>
              ) : (
                row.overdueLines
              ),
          },
        ]}
      />

      <Text strong style={{ display: "block", margin: "12px 0 4px" }}>
        TOP 欠交订单行
      </Text>
      <Table
        size="small"
        pagination={{ pageSize: 10, hideOnSinglePage: true }}
        rowKey={(row) => `${row.poNo}/${row.poLine}`}
        dataSource={topLines}
        columns={[
          { title: "订单号", dataIndex: "poNo" },
          { title: "物料", dataIndex: "materialCode", ellipsis: true },
          { title: "供应商", dataIndex: "supplierCode", width: 80 },
          { title: "订购量", render: (_: unknown, row) => fmtQty(row.orderedQty) },
          { title: "已收", render: (_: unknown, row) => fmtQty(row.receivedQty) },
          { title: "欠交量", render: (_: unknown, row) => fmtQty(row.openQty) },
          {
            title: "预计到货",
            dataIndex: "expectedDate",
            render: (value: string | null) => (value ? value.slice(0, 10) : "-"),
          },
          {
            title: "逾期天数",
            dataIndex: "overdueDays",
            render: (value: number) =>
              value > 90 ? <Tag color="red">{value}</Tag> : value,
          },
        ]}
      />
    </Card>
  );
}
