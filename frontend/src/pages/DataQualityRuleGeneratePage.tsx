/**
 * 数据质量规则自动生成向导页。
 * 四步：选本体类 → 选数据源 → 预览规则/采纳AI建议 → 确认落库。
 */

import { useCallback, useEffect, useState } from "react";
import {
  Button,
  Collapse,
  InputNumber,
  message,
  Select,
  Space,
  Steps,
  Table,
  Tag,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useTranslation } from "../i18n";
import { listClasses } from "../api/ontology";
import { listDataSources } from "../api/datasource";
import {
  applySuggestion,
  confirmRules,
  parseDescriptions,
  previewRules,
} from "../api/dataQualityGenerate";
import type { DataSource } from "../types/datasource";
import type { OntologyClass } from "../types/ontology";
import type {
  BlockedProperty,
  GenerateConfirmResponse,
  GeneratePreviewResponse,
  PropertyConstraintSuggestion,
  RuleSuggestion,
} from "../types/dataQualityGenerate";

const STEP_CLASS = 0;
const STEP_DATASOURCE = 1;
const STEP_PREVIEW = 2;
const STEP_CONFIRM = 3;

// ---------------------------------------------------------------------------
// 子组件
// ---------------------------------------------------------------------------

interface SuggestionTableProps {
  suggestions: RuleSuggestion[];
  selectedIds: Set<string>;
  onToggle: (code: string) => void;
  onThresholdChange: (code: string, threshold: number) => void;
  onSeverityChange: (code: string, severity: string) => void;
  t: (key: string) => string;
}

function SuggestionTable({
  suggestions,
  selectedIds,
  onToggle,
  onThresholdChange,
  onSeverityChange,
  t,
}: SuggestionTableProps) {
  const columns: ColumnsType<RuleSuggestion> = [
    {
      title: t("dataQualityGenerate.columns.select"),
      key: "select",
      width: 60,
      render: (_, row) => (
        <input
          type="checkbox"
          checked={selectedIds.has(row.ruleCode)}
          onChange={() => onToggle(row.ruleCode)}
        />
      ),
    },
    { title: t("dataQualityGenerate.columns.ruleCode"), dataIndex: "ruleCode", key: "ruleCode", width: 220 },
    { title: t("dataQualityGenerate.columns.ruleType"), dataIndex: "ruleType", key: "ruleType", width: 160 },
    {
      title: t("dataQualityGenerate.columns.targetColumn"),
      dataIndex: "targetColumn",
      key: "targetColumn",
      width: 140,
      render: (v: string | null) => v ?? "—",
    },
    {
      title: t("dataQualityGenerate.columns.threshold"),
      key: "threshold",
      width: 160,
      render: (_, row) => (
        <InputNumber
          min={0}
          max={100}
          value={row.threshold}
          style={{ width: "100%" }}
          onChange={(val) => onThresholdChange(row.ruleCode, val ?? 0)}
        />
      ),
    },
    {
      title: t("dataQualityGenerate.columns.severity"),
      key: "severity",
      width: 120,
      render: (_, row) => (
        <Select
          value={row.severity}
          style={{ width: "100%" }}
          onChange={(val) => onSeverityChange(row.ruleCode, val)}
        >
          <Select.Option value="HIGH">HIGH</Select.Option>
          <Select.Option value="MEDIUM">MEDIUM</Select.Option>
          <Select.Option value="LOW">LOW</Select.Option>
        </Select>
      ),
    },
    {
      title: t("dataQualityGenerate.columns.status"),
      key: "status",
      width: 90,
      render: (_, row) => (
        <Tag color={row.status === "NEW" ? "green" : "default"}>
          {row.status}
        </Tag>
      ),
    },
    { title: t("dataQualityGenerate.columns.reason"), dataIndex: "reason", key: "reason" },
  ];

  return (
    <Table
      rowKey="ruleCode"
      size="small"
      columns={columns}
      dataSource={suggestions}
      pagination={false}
      scroll={{ x: 1000 }}
    />
  );
}

interface LlmPanelProps {
  classId: number;
  t: (key: string) => string;
  onApplied: () => void;
}

function LlmPanel({ classId, t, onApplied }: LlmPanelProps) {
  const [loading, setLoading] = useState(false);
  const [items, setItems] = useState<PropertyConstraintSuggestion[]>([]);
  const [collapsed, setCollapsed] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const result = await parseDescriptions(classId);
      setItems(result);
    } catch (err) {
      message.error(t("dataQualityGenerate.messages.parseFailed") + ": " + String(err));
    } finally {
      setLoading(false);
    }
  }, [classId, t]);

  useEffect(() => {
    if (!collapsed) {
      void load();
    }
  }, [collapsed, load]);

  const handleApply = async (item: PropertyConstraintSuggestion) => {
    if (item.kind !== "allowed_values" || !item.values) return;
    try {
      await applySuggestion(item.propertyId, item.values);
      message.success(t("dataQualityGenerate.messages.applied"));
      onApplied();
    } catch (err) {
      message.error(t("dataQualityGenerate.messages.applyFailed") + ": " + String(err));
    }
  };

  return (
    <Collapse
      activeKey={collapsed ? undefined : "panel"}
      onChange={(keys) => setCollapsed(!keys.includes("panel"))}
      items={[
        {
          key: "panel",
          label: t("dataQualityGenerate.llmPanel"),
          children: items.length === 0 && !loading ? (
            <span>{t("dataQualityGenerate.noSuggestions")}</span>
          ) : (
            <Space direction="vertical" style={{ width: "100%" }}>
              {items.map((item) => (
                <div
                  key={item.propertyId}
                  style={{
                    border: "1px solid #d9d9d9",
                    borderRadius: 4,
                    padding: "8px 12px",
                  }}
                >
                  <div style={{ fontWeight: 600, marginBottom: 4 }}>
                    {item.propertyName}
                    <Tag style={{ marginLeft: 8 }}>{item.kind}</Tag>
                  </div>
                  {item.kind === "allowed_values" && item.values && (
                    <div style={{ marginBottom: 8 }}>
                      {t("dataQualityGenerate.suggestedValues")}: {item.values.join(", ")}
                    </div>
                  )}
                  <div style={{ color: "#666", marginBottom: 8 }}>
                    {t("dataQualityGenerate.confidence")}:{" "}
                    {(item.confidence * 100).toFixed(0)}%
                  </div>
                  <div style={{ color: "#666", marginBottom: 8 }}>
                    {t("dataQualityGenerate.rationale")}: {item.rationale}
                  </div>
                  <Button
                    size="small"
                    type="primary"
                    onClick={() => void handleApply(item)}
                  >
                    {t("dataQualityGenerate.adopt")}
                  </Button>
                </div>
              ))}
            </Space>
          ),
        },
      ]}
    />
  );
}

// ---------------------------------------------------------------------------
// 主组件
// ---------------------------------------------------------------------------

export default function DataQualityRuleGeneratePage() {
  const { t } = useTranslation();
  const [current, setCurrent] = useState(STEP_CLASS);

  // Step 1
  const [ontologyClasses, setOntologyClasses] = useState<OntologyClass[]>([]);
  const [selectedClassId, setSelectedClassId] = useState<number | null>(null);

  // Step 2
  const [datasources, setDatasources] = useState<DataSource[]>([]);
  const [selectedDatasourceId, setSelectedDatasourceId] = useState<number | null>(null);

  // Step 3 — preview result
  const [previewResult, setPreviewResult] = useState<GeneratePreviewResponse | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);

  // Step 3 — editable suggestions
  const [suggestions, setSuggestions] = useState<RuleSuggestion[]>([]);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());

  // Step 4
  const [confirmResult, setConfirmResult] = useState<GenerateConfirmResponse | null>(null);
  const [confirming, setConfirming] = useState(false);

  // Load ontology classes on mount
  useEffect(() => {
    listClasses()
      .then(setOntologyClasses)
      .catch((err) => message.error(t("dataQualityGenerate.messages.loadClassesFailed") + ": " + String(err)));
  }, [t]);

  // Load datasources when reaching step 2
  useEffect(() => {
    if (current >= STEP_DATASOURCE) {
      listDataSources()
        .then(setDatasources)
        .catch((err) => message.error(t("dataQualityGenerate.messages.loadDatasourcesFailed") + ": " + String(err)));
    }
  }, [current, t]);

  const handleClassSelect = useCallback((classId: number) => {
    setSelectedClassId(classId);
    setSelectedDatasourceId(null);
    setPreviewResult(null);
    setSuggestions([]);
    setSelectedIds(new Set());
    setConfirmResult(null);
  }, []);

  const handleDatasourceSelect = useCallback(
    (dsId: number) => {
      setSelectedDatasourceId(dsId);
      if (!selectedClassId) return;
      setPreviewLoading(true);
      previewRules(selectedClassId, dsId)
        .then((res) => {
          setPreviewResult(res);
          setSuggestions(res.suggestions);
          setSelectedIds(new Set(res.suggestions.filter((s) => s.status === "NEW").map((s) => s.ruleCode)));
        })
        .catch((err) => {
          message.error(t("dataQualityGenerate.messages.previewFailed") + ": " + String(err));
        })
        .finally(() => setPreviewLoading(false));
    },
    [selectedClassId, t]
  );

  const handleToggle = useCallback((code: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(code)) next.delete(code);
      else next.add(code);
      return next;
    });
  }, []);

  const handleThresholdChange = useCallback((code: string, threshold: number) => {
    setSuggestions((prev) =>
      prev.map((s) => (s.ruleCode === code ? { ...s, threshold } : s))
    );
  }, []);

  const handleSeverityChange = useCallback((code: string, severity: string) => {
    setSuggestions((prev) =>
      prev.map((s) => (s.ruleCode === code ? { ...s, severity: severity as RuleSuggestion["severity"] } : s))
    );
  }, []);

  const handleConfirm = useCallback(async () => {
    if (!selectedDatasourceId) return;
    const toSubmit = suggestions.filter((s) => selectedIds.has(s.ruleCode) && s.status === "NEW");
    setConfirming(true);
    try {
      const res = await confirmRules(selectedDatasourceId, toSubmit);
      setConfirmResult(res);
      setCurrent(STEP_CONFIRM);
    } catch (err) {
      message.error(t("dataQualityGenerate.messages.confirmFailed") + ": " + String(err));
    } finally {
      setConfirming(false);
    }
  }, [selectedDatasourceId, suggestions, selectedIds, t]);

  const blockedColumns: ColumnsType<BlockedProperty> = [
    { title: t("dataQualityGenerate.columns.propertyName"), dataIndex: "propertyName", key: "propertyName" },
    { title: t("dataQualityGenerate.columns.reason"), dataIndex: "reason", key: "reason" },
  ];

  return (
    <div style={{ padding: 24 }}>
      <h2 style={{ marginBottom: 24 }}>{t("dataQualityGenerate.title")}</h2>

      <Steps
        current={current}
        items={[
          { title: t("dataQualityGenerate.stepClass") },
          { title: t("dataQualityGenerate.stepDatasource") },
          { title: t("dataQualityGenerate.stepPreview") },
          { title: t("dataQualityGenerate.stepConfirm") },
        ]}
        style={{ marginBottom: 32 }}
      />

      {/* Step 1: Select ontology class */}
      {current === STEP_CLASS && (
        <div>
          <label style={{ fontWeight: 500, display: "block", marginBottom: 8 }}>
            {t("dataQualityGenerate.className")}
          </label>
          <Select
            placeholder={t("dataQualityGenerate.selectClassPlaceholder")}
            style={{ width: 320 }}
            value={selectedClassId}
            onChange={(val) => handleClassSelect(val)}
            options={ontologyClasses.map((c) => ({
              value: c.id,
              label: c.classAlias ? `${c.className}（${c.classAlias}）` : c.className,
            }))}
          />
          <div style={{ marginTop: 24 }}>
            <Button
              type="primary"
              disabled={!selectedClassId}
              onClick={() => setCurrent(STEP_DATASOURCE)}
            >
              {t("common.next")}
            </Button>
          </div>
        </div>
      )}

      {/* Step 2: Select datasource */}
      {current === STEP_DATASOURCE && (
        <div>
          <label style={{ fontWeight: 500, display: "block", marginBottom: 8 }}>
            {t("dataQualityGenerate.datasource")}
          </label>
          <Select
            placeholder={t("dataQualityGenerate.selectDatasourcePlaceholder")}
            style={{ width: 320 }}
            value={selectedDatasourceId}
            onChange={(val) => handleDatasourceSelect(val)}
            options={datasources.map((ds) => ({
              value: ds.id,
              label: ds.name,
            }))}
          />
          <div style={{ marginTop: 24 }}>
            <Space>
              <Button onClick={() => setCurrent(STEP_CLASS)}>{t("common.prev")}</Button>
              <Button
                type="primary"
                disabled={!selectedDatasourceId}
                loading={previewLoading}
                onClick={() => setCurrent(STEP_PREVIEW)}
              >
                {t("common.next")}
              </Button>
            </Space>
          </div>
        </div>
      )}

      {/* Step 3: Preview + suggestions */}
      {current === STEP_PREVIEW && (
        <div>
          {previewResult ? (
            <>
              {/* Blocked properties */}
              {previewResult.blocked.length > 0 && (
                <div style={{ marginBottom: 16 }}>
                  <div style={{ fontWeight: 500, marginBottom: 8, color: "#ff4d4f" }}>
                    {t("dataQualityGenerate.blocked")}（{previewResult.blocked.length}）
                  </div>
                  <Table
                    rowKey="propertyName"
                    size="small"
                    columns={blockedColumns}
                    dataSource={previewResult.blocked}
                    pagination={false}
                  />
                </div>
              )}

              {/* Suggestion table */}
              <div style={{ fontWeight: 500, marginBottom: 8 }}>
                {t("dataQualityGenerate.suggestions")}（{suggestions.length}）
              </div>
              <SuggestionTable
                suggestions={suggestions}
                selectedIds={selectedIds}
                onToggle={handleToggle}
                onThresholdChange={handleThresholdChange}
                onSeverityChange={handleSeverityChange}
                t={t}
              />

              {/* LLM panel */}
              {selectedClassId && (
                <div style={{ marginTop: 24 }}>
                  <LlmPanel
                    classId={selectedClassId}
                    t={t}
                    onApplied={() => {
                      if (selectedClassId && selectedDatasourceId) {
                        void previewRules(selectedClassId, selectedDatasourceId).then((res) => {
                          setPreviewResult(res);
                          setSuggestions(res.suggestions);
                        });
                      }
                    }}
                  />
                </div>
              )}

              {/* Actions */}
              <div style={{ marginTop: 24 }}>
                <Space>
                  <Button onClick={() => setCurrent(STEP_DATASOURCE)}>{t("common.prev")}</Button>
                  <Button
                    type="primary"
                    loading={confirming}
                    onClick={() => void handleConfirm()}
                  >
                    {t("dataQualityGenerate.confirmBtn")}
                  </Button>
                </Space>
              </div>
            </>
          ) : (
            <div style={{ color: "#999" }}>{t("dataQualityGenerate.previewEmpty")}</div>
          )}
        </div>
      )}

      {/* Step 4: Confirm result */}
      {current === STEP_CONFIRM && confirmResult && (
        <div>
          <div style={{ fontSize: 16, fontWeight: 500, marginBottom: 16 }}>
            {t("dataQualityGenerate.confirmSuccess")}
          </div>
          <div style={{ marginBottom: 8 }}>
            <Tag color="green">{t("dataQualityGenerate.created")}: {confirmResult.created.length}</Tag>
            <Tag color="default">{t("dataQualityGenerate.skipped")}: {confirmResult.skippedCodes.length}</Tag>
          </div>
          {confirmResult.skippedCodes.length > 0 && (
            <div style={{ color: "#666", marginTop: 8 }}>
              {t("dataQualityGenerate.skippedList")}: {confirmResult.skippedCodes.join(", ")}
            </div>
          )}
          <div style={{ marginTop: 24 }}>
            <Button
              onClick={() => {
                setCurrent(STEP_CLASS);
                setSelectedClassId(null);
                setSelectedDatasourceId(null);
                setPreviewResult(null);
                setSuggestions([]);
                setSelectedIds(new Set());
                setConfirmResult(null);
              }}
            >
              {t("dataQualityGenerate.startOver")}
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
