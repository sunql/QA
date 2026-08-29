import ReactECharts from "echarts-for-react";
import { Button, Space, Table, message } from "antd";
import { DownloadOutlined } from "@ant-design/icons";
import { useRef } from "react";
import { useTranslation } from "../../i18n";
import type { ChartType } from "../../types/chat";
import { downloadBlob, downloadCsv } from "../../utils/download";

interface ChartRendererProps {
  chartType?: ChartType | null;
  chartOption?: Record<string, unknown> | null;
  data?: Record<string, unknown>[] | null;
}

/** 导出文件名（本轮固定，后续可按图表 title 命名）。 */
const CSV_FILENAME = "result.csv";
const PNG_FILENAME = "chart.png";

function errorMessageOf(error: unknown): string {
  return error instanceof Error ? error.message : "";
}

export default function ChartRenderer({ chartType, chartOption, data }: ChartRendererProps) {
  const { t } = useTranslation();
  const chartRef = useRef<InstanceType<typeof ReactECharts>>(null);

  const rows = data ?? [];
  const isTable = chartType === "table";
  const isChart = Boolean(chartType && chartOption);
  if (!isTable && !isChart) {
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

  // 表格空数据时不展示导出按钮；图表无 option 已被上方早退拦截
  const showToolbar = isTable ? rows.length > 0 : true;
  const columns =
    rows.length > 0
      ? Object.keys(rows[0]).map((key) => ({
          title: key,
          dataIndex: key,
          key,
        }))
      : [];

  return (
    <>
      {showToolbar && (
        <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 8 }}>
          <Space>
            {isTable ? (
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
        <Table
          rowKey={(record, index) => index?.toString() ?? JSON.stringify(record)}
          size="small"
          dataSource={rows}
          columns={columns}
          pagination={{ pageSize: 10 }}
        />
      ) : (
        <ReactECharts ref={chartRef} option={chartOption} style={{ height: 320 }} notMerge />
      )}
    </>
  );
}
