import ReactECharts from "echarts-for-react";
import { Button, Collapse, Space, Table, Typography, message } from "antd";
import { DownloadOutlined } from "@ant-design/icons";
import { useMemo, useRef } from "react";
import { useTranslation } from "../../i18n";
import { useThemeStore } from "../../stores/themeStore";
import { applyChartTheme } from "../../theme/chartTheme";
import { DARK_TOKEN, LIGHT_TOKEN } from "../../theme/tokens";
import type { ChartType, TablePayload, VisualRationale } from "../../types/chat";
import { downloadBlob, downloadCsv } from "../../utils/download";
import { visualRationaleText } from "../../utils/visualRationale";
import KpiCard, { parseKpiPayload } from "./KpiCard";

interface ChartRendererProps {
  chartType?: ChartType | null;
  chartOption?: Record<string, unknown> | null;
  data?: Record<string, unknown>[] | null;
  tableOption?: TablePayload | null;
  visualRationale?: VisualRationale | null;
}

/** 导出文件名（本轮固定，后续可按图表 title 命名）。 */
const CSV_FILENAME = "result.csv";
const PNG_FILENAME = "chart.png";

function errorMessageOf(error: unknown): string {
  return error instanceof Error ? error.message : "";
}

/** 表格负载里的行（`chartOption = {columns, rows}`，服务端 TABLE 契约）。 */
function payloadRows(chartOption: Record<string, unknown> | null | undefined): Record<string, unknown>[] {
  const rows = chartOption?.rows;
  return Array.isArray(rows) ? (rows as Record<string, unknown>[]) : [];
}

/** 表格负载里的列名；未声明或全非字符串时返回 null（退回首行推断）。 */
function payloadColumns(chartOption: Record<string, unknown> | null | undefined): string[] | null {
  const columns = chartOption?.columns;
  if (!Array.isArray(columns)) return null;
  const names = columns.filter((column): column is string => typeof column === "string");
  return names.length > 0 ? names : null;
}

/** 数据表渲染（antd Table + 截断披露）：TABLE 主表与图形下的折叠明细表共用同一份
 *  列/行渲染，避免复制漂移（DRY）。columns 为空时退回首行键推断。 */
interface DataTableProps {
  columns: string[];
  rows: Record<string, unknown>[];
  truncated?: boolean;
}

function DataTable({ columns, rows, truncated }: DataTableProps) {
  const { t } = useTranslation();
  const columnDefs = (columns.length > 0 ? columns : rows.length > 0 ? Object.keys(rows[0]) : []).map(
    (key) => ({ title: key, dataIndex: key, key })
  );
  const tableRows = rows.map((row, index) => ({ ...row, __rowKey: `row-${index}` }));
  return (
    <>
      <Table
        rowKey="__rowKey"
        size="small"
        dataSource={tableRows}
        columns={columnDefs}
        pagination={{ pageSize: 10 }}
      />
      {truncated === true ? (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {t("chartExport.truncated", { count: rows.length })}
        </Typography.Text>
      ) : null}
    </>
  );
}

export default function ChartRenderer({
  chartType,
  chartOption,
  data,
  tableOption,
  visualRationale,
}: ChartRendererProps) {
  const { t } = useTranslation();
  const isDark = useThemeStore((state) => state.isDark);
  const chartRef = useRef<InstanceType<typeof ReactECharts>>(null);

  // TABLE 的 chartOption 本身就是 `{columns, rows}`，所以表格并不依赖调用方传 data：
  // 多步每一步不铺全量 data（见 MultiStepPlanCard），表格要从自己的负载里自取自足。
  const rows = useMemo(
    () => (data?.length ? data : payloadRows(chartOption)),
    [data, chartOption]
  );
  const isTable = chartType === "table";
  const isKpi = chartType === "kpi";
  // 落库时行数被截过（`chat_chart_persist`）：只有**存回来的**负载才带这个标记。
  // 实时那一轮拿到的是全量 option（截断发生在写库前，作用在副本上），所以线上
  // 不会误报；历史回放读到 `truncated: true` 时才提示，与后端 PDF 口径一致。
  const isTruncatedTable = chartOption?.truncated === true;
  // 决策 6：服务端只发结构，颜色在这一层补（只补色、不改结构；option 来自 store，
  // 必须走不可变路径，否则同一份消息的其它引用会串台）
  const themedOption = useMemo(
    () => (chartOption ? applyChartTheme(chartOption, isDark ? DARK_TOKEN : LIGHT_TOKEN) : null),
    [chartOption, isDark]
  );
  const kpiPayload = useMemo(
    () => (isKpi ? parseKpiPayload(chartOption) : null),
    [isKpi, chartOption]
  );
  // 无 kpi 负载（后端降级为不发卡）时不渲染空壳卡
  if (isKpi && !kpiPayload) {
    return null;
  }
  const isChart = Boolean(chartType && themedOption) && !isKpi;
  if (!isTable && !isKpi && !isChart) {
    return null;
  }

  const handleExportCsv = () => {
    try {
      downloadCsv(rows, CSV_FILENAME);
      void message.success(t("chartExport.success"));
    } catch (error) {
      const messageText = errorMessageOf(error) || t("errors.unknownError");
      void message.error(t("chartExport.failed", { message: messageText }));
    }
  };

  const handleExportPng = () => {
    try {
      const instance = chartRef.current?.getEchartsInstance();
      if (!instance) {
        throw new Error(t("errors.unknownError"));
      }
      const dataUrl = instance.getDataURL({ type: "png", pixelRatio: 2, backgroundColor: "#fff" });
      downloadBlob(new Blob([dataUrl], { type: "image/png" }), PNG_FILENAME);
      void message.success(t("chartExport.success"));
    } catch (error) {
      const messageText = errorMessageOf(error) || t("errors.unknownError");
      void message.error(t("chartExport.failed", { message: messageText }));
    }
  };

  // 表格空数据时不展示导出按钮；图表无 option 已被上方早退拦截。
  // KPI 不是 ECharts，没有 PNG 可导 —— 它按表格口径给 CSV（底层数据本就是表格）。
  // 注意 KPI 的导出按钮同样要求 `rows` 非空：负载里只有 `{kpi: {...}}` 时导出的是一张
  // 空表，给一个点了没内容的按钮不如不给（现有两条链路都会带 data，故按钮在线上可见）。
  const showToolbar = isTable || isKpi ? rows.length > 0 : true;
  const columnNames = payloadColumns(chartOption) ?? [];
  // 判断依据文案（Task 8）：所有形态（图/TABLE/KPI）都在下方渲染一行说明。
  const rationaleLine = visualRationaleText(visualRationale, t);

  return (
    <>
      {showToolbar && (
        <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 8 }}>
          <Space>
            {isTable || isKpi ? (
              <Button size="small" icon={<DownloadOutlined />} onClick={handleExportCsv}>
                {t("chartExport.csv")}
              </Button>
            ) : (
              <Button size="small" icon={<DownloadOutlined />} onClick={handleExportPng}>
                {t("chartExport.png")}
              </Button>
            )}
          </Space>
        </div>
      )}
      {isTable ? (
        <DataTable columns={columnNames} rows={rows} truncated={isTruncatedTable} />
      ) : isKpi && kpiPayload ? (
        <KpiCard kpi={kpiPayload} />
      ) : (
        <>
          <ReactECharts ref={chartRef} option={themedOption} style={{ height: 320 }} notMerge />
          {/* 图下方的折叠明细表（Task 8，决策 2）：默认折叠，展开看原始数字。
              表负载来自 tableOption（后端只在图型附带；TABLE 的表已在 chartOption、
              KPI 不附表，故此处 tableOption 为 null 时整个面板不渲染）。 */}
          {tableOption ? (
            <Collapse
              size="small"
              defaultActiveKey={[]}
              style={{ marginTop: 8 }}
              items={[
                {
                  key: "data-table",
                  label: t("chat.visual.dataTable"),
                  children: (
                    <DataTable
                      columns={tableOption.columns}
                      rows={tableOption.rows}
                      truncated={tableOption.truncated === true}
                    />
                  ),
                },
              ]}
            />
          ) : null}
        </>
      )}
      {rationaleLine ? (
        <Typography.Text type="secondary" style={{ display: "block", marginTop: 8, fontSize: 12 }}>
          {rationaleLine}
        </Typography.Text>
      ) : null}
    </>
  );
}
