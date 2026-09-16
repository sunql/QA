/** 跨系统编码映射批量导入弹窗（feat-entity-mapping-bulk-import 2026-09-16）。
 *
 * 工作流：
 *   1) 用户粘贴 CSV / TSV（来自 docs/entity-mapping-template.csv）
 *   2) 前端解析 → 实时显示「按行」校验结果（headers + 每行解析状态）
 *   3) 用户点「导入」→ 调用后端 bulkImportMappings
 *   4) 后端返回每行实际处理（inserted / updated / skipped / failed），弹窗底部表格展示
 *
 * 设计取舍：
 *   - 解析 / 校验都跑前端**纯函数**（csvTsvParser），不发起任何后端请求；预览足够快
 *   - 真导入只调一次后端 bulk（而不是逐行），单事务、行级隔离
 *   - 上限 1000 行（后端硬限）；前端解析后若超量，按 chunk 自动切（保留简单：超过 1000 仍允许点击，由后端拒）
 *   - enterpriseKey 传 0 → 后端按 enterprise_code + entity_type 派生（前端不用算）
 *   - sourceKey 缺省回退 sourceCode（多数场景 sourceKey 与 sourceCode 同值）
 */

import { useMemo, useState } from "react";
import {
  Alert,
  Button,
  Input,
  Modal,
  Space,
  Table,
  Tabs,
  Tag,
  Typography,
  Upload,
  message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { bulkImportMappings } from "../../api/entityMapping";
import type {
  EntityMappingBulkResult,
  EntityMappingBulkResultRow,
  EntityMappingCreate,
  EntityType,
  MatchRule,
  SourceSystem,
} from "../../types/entityMapping";
import { parseDelimited, rowsToRecords } from "../../utils/csvTsvParser";
import { downloadCsv, readFileAsText } from "../../utils/csvDownload";

const { Text } = Typography;

const ENTITY_TYPES: EntityType[] = [
  "SUPPLIER",
  "MATERIAL",
  "PO",
  "GR",
  "IQC",
  "NCR",
];

const SOURCE_SYSTEMS: SourceSystem[] = ["ERP", "SRM", "QMS", "MDM", "PLM"];

const MATCH_RULES: MatchRule[] = ["MDM_MASTER", "BUSINESS_KEY", "MAPPING"];

const DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;

// 模板 SSOT（feat-entity-mapping-bulk-import 2026-09-16）：
// 与 docs/entity-mapping-template.csv 字段集对齐；下载到本地即合法模板文件。
// 表头行（必填）+ 4 行示例（可删），enterprise_key 不需要手填（后端按 enterprise_code 派生）。
const TEMPLATE_HEADER =
  "entity_type,enterprise_code,source_system,source_code,match_rule,name,effective_date,expiry_date";
const TEMPLATE_SAMPLE_ROWS = [
  "SUPPLIER,SUP000001,SRM,SRM-2024-001,MDM_MASTER,北京XX有限公司,2026-01-01,",
  "SUPPLIER,SUP000002,SRM,SRM-2024-002,BUSINESS_KEY,上海YY贸易,2026-01-01,",
  "MATERIAL,MAT000001,ERP,M-1001,MDM_MASTER,螺栓 M8x20,2026-01-01,",
];
const TEMPLATE_CSV = [TEMPLATE_HEADER, ...TEMPLATE_SAMPLE_ROWS].join("\n");

// 「加载示例」用：Tab 分隔 + 4 行示例，让用户直观看到分隔符兼容。
const SAMPLE_TSV = [
  "entity_type\tenterprise_code\tsource_system\tsource_code\tmatch_rule\tname\teffective_date\texpiry_date",
  "SUPPLIER\tSUP000001\tSRM\tSRM-2024-001\tMDM_MASTER\t北京XX有限公司\t2026-01-01\t",
  "SUPPLIER\tSUP000002\tSRM\tSRM-2024-002\tBUSINESS_KEY\t上海YY贸易\t2026-01-01\t",
  "MATERIAL\tMAT000001\tERP\tM-1001\tMDM_MASTER\t螺栓 M8x20\t2026-01-01\t",
].join("\n");

/** 前端逐行校验（不调后端，schema 错误的行不进入导入）。
 *  - row：原始行号（1-based，含表头 → 数据行从 2 开始）
 *  - values：key 为 CSV 表头（trim + 小写）
 */
function validateRow(
  values: Record<string, string>,
):
  | { ok: true; payload: EntityMappingCreate }
  | { ok: false; reason: string } {
  const entityType = (values.entity_type ?? "").trim();
  const enterpriseCode = (values.enterprise_code ?? "").trim();
  const sourceSystem = (values.source_system ?? "").trim();
  const sourceCode = (values.source_code ?? "").trim();
  const sourceKey = (values.source_key ?? sourceCode).trim(); // 未指定 → 回退 source_code
  const matchRuleRaw = (values.match_rule ?? "").trim() || "MAPPING";
  const name = (values.name ?? "").trim() || null;
  const effectiveDate = (values.effective_date ?? "").trim() || null;
  const expiryDate = (values.expiry_date ?? "").trim() || null;

  if (!entityType) return { ok: false, reason: "缺少 entity_type 列值" };
  if (!ENTITY_TYPES.includes(entityType as EntityType))
    return { ok: false, reason: `entity_type 不在合法范围：${entityType}` };
  if (!enterpriseCode) return { ok: false, reason: "缺少 enterprise_code" };
  if (enterpriseCode.length > 100)
    return { ok: false, reason: `enterprise_code 长度 ${enterpriseCode.length} > 100` };
  if (!sourceSystem)
    return { ok: false, reason: "缺少 source_system" };
  if (!SOURCE_SYSTEMS.includes(sourceSystem as SourceSystem))
    return { ok: false, reason: `source_system 不在合法枚举：${sourceSystem}` };
  if (!sourceCode) return { ok: false, reason: "缺少 source_code" };
  if (sourceCode.length > 100)
    return { ok: false, reason: `source_code 长度 ${sourceCode.length} > 100` };
  if (sourceKey.length > 100)
    return { ok: false, reason: `source_key 长度 ${sourceKey.length} > 100` };
  if (!MATCH_RULES.includes(matchRuleRaw as MatchRule))
    return { ok: false, reason: `match_rule 不在合法枚举：${matchRuleRaw}` };
  if (effectiveDate && !DATE_PATTERN.test(effectiveDate))
    return { ok: false, reason: `effective_date 格式不对（期望 YYYY-MM-DD）：${effectiveDate}` };
  if (expiryDate && !DATE_PATTERN.test(expiryDate))
    return { ok: false, reason: `expiry_date 格式不对（期望 YYYY-MM-DD）：${expiryDate}` };
  if (effectiveDate && expiryDate && effectiveDate > expiryDate)
    return { ok: false, reason: `effective_date(${effectiveDate}) > expiry_date(${expiryDate})` };
  if (name && name.length > 200)
    return { ok: false, reason: `name 长度 ${name.length} > 200` };

  const payload: EntityMappingCreate = {
    entityType: entityType as EntityType,
    enterpriseKey: 0, // 占位；后端 bulkImportMappings 按 (entity_type, enterprise_code) 派生
    enterpriseCode,
    sourceSystem: sourceSystem as SourceSystem,
    sourceKey,
    sourceCode,
    matchRule: matchRuleRaw as MatchRule,
    effectiveDate,
    expiryDate,
    name,
  };
  return { ok: true, payload };
}

interface ParsedRow {
  row: number;
  ok: boolean;
  reason?: string;
  payload?: EntityMappingCreate;
  raw: Record<string, string>;
}

interface BulkImportModalProps {
  open: boolean;
  onClose: () => void;
  onSuccess: () => void; // 导入成功后由父组件刷新列表
}

export default function BulkImportModal({
  open,
  onClose,
  onSuccess,
}: BulkImportModalProps) {
  const [text, setText] = useState<string>("");
  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState<EntityMappingBulkResult | null>(null);

  // 解析 + 校验（每次 text 变都重算，useMemo 避免无关重渲染）
  const { parseReport, parsedRows } = useMemo(() => {
    if (!text.trim()) {
      return { parseReport: null, parsedRows: [] as ParsedRow[] };
    }
    const parsed = parseDelimited(text);
    const records = rowsToRecords(parsed);
    const rows: ParsedRow[] = records.map((rec) => {
      // 标准化 keys：小写 + trim（parser 已 trim value，但 header 可能带空格）
      const values: Record<string, string> = {};
      for (const [k, v] of Object.entries(rec.values)) {
        values[k.toLowerCase().trim()] = v;
      }
      const result = validateRow(values);
      return result.ok
        ? { row: rec.row, ok: true, payload: result.payload, raw: values }
        : { row: rec.row, ok: false, reason: result.reason, raw: values };
    });
    return { parseReport: parsed, parsedRows: rows };
  }, [text]);

  const validRows = parsedRows.filter((r) => r.ok);
  const invalidCount = parsedRows.length - validRows.length;

  function handleClose() {
    if (submitting) return;
    setText("");
    setResult(null);
    onClose();
  }

  function loadSample() {
    setText(SAMPLE_TSV);
    setResult(null);
  }

  /**
   * 下载模板（feat-entity-mapping-bulk-import 2026-09-16）：
   * - 内容与 docs/entity-mapping-template.csv 字段集对齐（SSOT 维护在前端常量）
   * - CSV 逗号分隔（Excel / WPS / Numbers 直接打开）
   * - 通过 utils/csvDownload 复用（单测覆盖；前端 Blob + a[download]，不依赖后端）
   */
  function downloadTemplate() {
    downloadCsv(TEMPLATE_CSV, "entity-mapping-template.csv");
  }

  /**
   * 上传 CSV 文件：异步读为字符串灌入 textarea，复用现有解析+校验。
   * - 校验逻辑（大小 / 后缀）下沉到 utils/csvDownload.readFileAsText
   */
  async function handleFileUpload(file: File | null) {
    if (!file) return;
    try {
      const content = await readFileAsText(file);
      setText(content);
      setResult(null);
      message.success(`已读取文件：${file.name}（${(file.size / 1024).toFixed(1)} KB）`);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      message.error(msg);
    }
  }

  async function handleImport() {
    if (validRows.length === 0) {
      message.warning("没有可导入的有效行");
      return;
    }
    if (validRows.length > 1000) {
      message.error(
        `单批最多 1000 行，当前 ${validRows.length} 行，请先在 CSV 中拆分。`,
      );
      return;
    }
    setSubmitting(true);
    try {
      const payloads = validRows.map((r) => r.payload as EntityMappingCreate);
      const resp = await bulkImportMappings(payloads);
      setResult(resp);
      message.success(
        `导入完成：插入 ${resp.inserted} / 更新 ${resp.updated} / 跳过 ${resp.skipped} / 失败 ${resp.failed}`,
      );
      if (resp.failed === 0) {
        onSuccess();
      }
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      message.error(`导入失败：${msg}`);
    } finally {
      setSubmitting(false);
    }
  }

  const previewColumns: ColumnsType<ParsedRow> = [
    { title: "行号", dataIndex: "row", width: 60 },
    {
      title: "状态",
      dataIndex: "ok",
      width: 80,
      render: (ok: boolean) =>
        ok ? <Tag color="green">OK</Tag> : <Tag color="red">FAIL</Tag>,
    },
    { title: "entity_type", dataIndex: "raw", width: 110, render: (raw: Record<string, string>) => raw.entity_type || "" },
    { title: "enterprise_code", dataIndex: "raw", width: 140, render: (raw: Record<string, string>) => raw.enterprise_code || "" },
    { title: "source_system", dataIndex: "raw", width: 110, render: (raw: Record<string, string>) => raw.source_system || "" },
    { title: "source_code", dataIndex: "raw", width: 140, render: (raw: Record<string, string>) => raw.source_code || "" },
    { title: "match_rule", dataIndex: "raw", width: 110, render: (raw: Record<string, string>) => raw.match_rule || "MAPPING" },
    {
      title: "错误 / 备注",
      dataIndex: "reason",
      render: (_: unknown, r: ParsedRow) =>
        r.ok ? <Text type="success">可导入</Text> : <Text type="danger">{r.reason}</Text>,
    },
  ];

  const resultColumns: ColumnsType<EntityMappingBulkResultRow> = [
    { title: "行号", dataIndex: "row", width: 60 },
    {
      title: "状态",
      dataIndex: "status",
      width: 90,
      render: (s: EntityMappingBulkResultRow["status"]) => {
        const color =
          s === "inserted"
            ? "green"
            : s === "updated"
              ? "blue"
              : s === "skipped"
                ? "default"
                : "red";
        return <Tag color={color}>{s}</Tag>;
      },
    },
    { title: "entity_type", dataIndex: "entityType", width: 110 },
    { title: "enterprise_code", dataIndex: "enterpriseCode", width: 140 },
    { title: "id", dataIndex: "id", width: 70 },
    {
      title: "变更字段",
      dataIndex: "changedFields",
      render: (cf: string[] | null | undefined) =>
        cf && cf.length ? cf.join(", ") : "—",
    },
    {
      title: "备注 / 错误",
      dataIndex: "reason",
      render: (_: unknown, r: EntityMappingBulkResultRow) => (
        <Text type={r.status === "failed" ? "danger" : "secondary"}>
          {r.error ?? r.reason ?? ""}
        </Text>
      ),
    },
  ];

  return (
    <Modal
      title="批量导入编码映射"
      open={open}
      onCancel={handleClose}
      footer={null}
      width={1000}
      destroyOnClose
      maskClosable={!submitting}
    >
      <Tabs
        defaultActiveKey="input"
        items={[
          {
            key: "input",
            label: "粘贴内容",
            children: (
              <Space direction="vertical" style={{ width: "100%" }}>
                <Space>
                  <Button size="small" onClick={loadSample}>
                    加载示例
                  </Button>
                  <Button size="small" onClick={downloadTemplate}>
                    下载模板
                  </Button>
                  <Upload
                    accept=".csv,.tsv,.txt"
                    showUploadList={false}
                    beforeUpload={(file) => {
                      handleFileUpload(file);
                      return false; // 阻止 antd Upload 默认上传
                    }}
                    maxCount={1}
                  >
                    <Button size="small">上传文件</Button>
                  </Upload>
                  <Button size="small" onClick={() => setText("")}>
                    清空
                  </Button>
                </Space>
                <Input.TextArea
                  rows={10}
                  placeholder="把 CSV / TSV 内容粘贴到这里（首行表头）"
                  value={text}
                  onChange={(e) => {
                    setText(e.target.value);
                    setResult(null);
                  }}
                  data-testid="bulk-import-input"
                />
                {parseReport && parseReport.warnings.length > 0 && (
                  <Alert
                    type="warning"
                    showIcon
                    message="解析告警"
                    description={
                      <ul style={{ margin: 0, paddingLeft: 20 }}>
                        {parseReport.warnings.map((w, i) => (
                          <li key={i}>{w}</li>
                        ))}
                      </ul>
                    }
                  />
                )}
                {parseReport && (
                  <Space size="large">
                    <Text>分隔符：{parseReport.delimiter === "\t" ? "Tab" : "逗号"}</Text>
                    <Text>列数：{parseReport.headers.length}</Text>
                    <Text>解析行数：{parsedRows.length}</Text>
                    <Text type={validRows.length > 0 ? "success" : "warning"}>
                      可导入：{validRows.length}
                    </Text>
                    {invalidCount > 0 && <Text type="danger">失败：{invalidCount}</Text>}
                  </Space>
                )}
              </Space>
            ),
          },
          {
            key: "preview",
            label: `预览 (${parsedRows.length})`,
            children: (
              <Table<ParsedRow>
                rowKey="row"
                size="small"
                columns={previewColumns}
                dataSource={parsedRows}
                pagination={{ pageSize: 50 }}
              />
            ),
          },
          {
            key: "result",
            label: result ? `结果 (${result.total})` : "结果",
            disabled: !result,
            children: result ? (
              <Space direction="vertical" style={{ width: "100%" }}>
                <Space size="large">
                  <Text>总行数：{result.total}</Text>
                  <Text type="success">插入：{result.inserted}</Text>
                  <Text strong style={{ color: "#1677ff" }}>更新：{result.updated}</Text>
                  <Text>跳过：{result.skipped}</Text>
                  <Text type={result.failed > 0 ? "danger" : "secondary"}>
                    失败：{result.failed}
                  </Text>
                </Space>
                <Table<EntityMappingBulkResultRow>
                  rowKey="row"
                  size="small"
                  columns={resultColumns}
                  dataSource={result.results}
                  pagination={{ pageSize: 50 }}
                />
              </Space>
            ) : null,
          },
        ]}
      />
      <div style={{ marginTop: 16, textAlign: "right" }}>
        <Space>
          <Button onClick={handleClose} disabled={submitting}>
            关闭
          </Button>
          <Button
            type="primary"
            onClick={handleImport}
            loading={submitting}
            disabled={validRows.length === 0}
          >
            导入 ({validRows.length} 行)
          </Button>
        </Space>
      </div>
    </Modal>
  );
}