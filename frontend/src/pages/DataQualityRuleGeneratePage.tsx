/**
 * 数据质量规则自动生成向导页。
 * 四步：选本体类 → 选数据源 → 预览规则/采纳AI建议 → 确认落库。
 */

import { useCallback, useEffect, useMemo, useState } from "react";
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
  listLlmModels,
  parseDescriptions,
  previewRules,
} from "../api/dataQualityGenerate";
import type { DataSource } from "../types/datasource";
import type { OntologyClass } from "../types/ontology";
import type {
  BlockedProperty,
  GenerateConfirmResponse,
  GeneratePreviewResponse,
  LlmModelOption,
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
  // 已采纳的 propertyId 集合：applySuggestion 成功写入 ontology_property 后
  // 把 id 加入，UI 立即变为 disabled + "已采纳"，避免用户重复点击或不知道已沉淀。
  const [adoptedIds, setAdoptedIds] = useState<Set<number>>(new Set());

  // LLM 模型选择器状态：
  // - models：可用模型列表（仅取需要的 id/modelName/provider 字段）
  // - modelsAttempted：listLlmModels 是否已结束（成功或失败），用于 race-safe gate
  // - modelsLoadFailed：拉取失败时面板内显示红字，不弹全局错误
  // - selectedModelId：用户当前选择的模型 id；null 表示「未选 / 跟随默认」
  const [models, setModels] = useState<LlmModelOption[]>([]);
  const [modelsAttempted, setModelsAttempted] = useState(false);
  const [modelsLoadFailed, setModelsLoadFailed] = useState(false);
  const [selectedModelId, setSelectedModelId] = useState<number | null>(null);

  // 挂载时拉取模型列表；默认选第一个非 ollama provider（fallback: 第一个）。
  // 失败时静默处理，仅把 modelsLoadFailed 置 true，由面板显示错误文案。
  useEffect(() => {
    let cancelled = false;
    listLlmModels()
      .then((ms) => {
        if (cancelled) return;
        setModels(ms);
        if (ms.length > 0) {
          // 默认选第一个非 ollama provider（fallback: 第一个），避免走本地模型慢/失败。
          // 后端返回 provider 大写（如 "OPENAI"/"OLLAMA"），比较时把类型放宽为 string，
          // 兼容未来后端可能返回小写（契约表达式：provider !== "ollama"）。
          const preferred = (() => {
            for (const m of ms) {
              const provider = m.provider as string;
              if (provider !== "ollama") return m;
            }
            return ms[0];
          })();
          setSelectedModelId(preferred.id);
        }
      })
      .catch(() => {
        if (cancelled) return;
        setModelsLoadFailed(true);
      })
      .finally(() => {
        if (cancelled) return;
        setModelsAttempted(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      // race-safe：优先用用户选定，否则取列表第一个；undefined 让后端走默认 env
      // 这样在 modelsAttempted=true 前面板不会发起请求，避免传 null 触发 503。
      const effectiveModelId = selectedModelId ?? models[0]?.id ?? undefined;
      const result = await parseDescriptions(classId, effectiveModelId);
      setItems(result.suggestions);
      // 用后端返回的已沉淀 propertyId 初始化 adoptedIds，
      // 让刷新页面也保持已采纳状态（不依赖 session-local Set）。
      setAdoptedIds((prev) => {
        const next = new Set(prev);
        for (const id of result.persistedPropertyIds) next.add(id);
        return next;
      });
    } catch (err) {
      message.error(t("dataQualityGenerate.messages.parseFailed") + ": " + String(err));
    } finally {
      setLoading(false);
    }
  }, [classId, selectedModelId, models, t]);

  // 仅在模型列表尝试结束后再触发首次 load，避免 listLlmModels race
  // 导致 selectedModelId=null 时走默认 env 路径 → 503。
  useEffect(() => {
    if (!collapsed && modelsAttempted) {
      void load();
    }
  }, [collapsed, modelsAttempted, load]);

  const handleModelChange = (id: number) => {
    setSelectedModelId(id);
    if (!collapsed) {
      // 切换模型：先折叠再展开，触发 useEffect 重 load
      setCollapsed(true);
      setTimeout(() => setCollapsed(false), 0);
    }
  };

  const handleApply = async (item: PropertyConstraintSuggestion) => {
    if (item.kind !== "allowed_values" || !item.values) {
      // 不能静默 return：用户点击后无任何反馈会以为按钮坏了。
      // 仅 allowed_values 类型可自动沉淀到 ontology_property.allowed_values；
      // not_null 等类型需业务方在本体管理页手动处理。
      message.warning(t("dataQualityGenerate.messages.adoptNotApplicable"));
      return;
    }
    if (adoptedIds.has(item.propertyId)) {
      // 用户对已采纳项再点：给 info 提示而非静默 return，避免「按钮没反应」的错觉。
      message.info(t("dataQualityGenerate.messages.alreadyAdopted"));
      return;
    }
    try {
      await applySuggestion(item.propertyId, item.values);
      // 写入成功后立即把 propertyId 加入已采纳集合，
      // 让 button disabled + label 变为「已采纳」，并显示「已沉淀」提示。
      setAdoptedIds((prev) => {
        const next = new Set(prev);
        next.add(item.propertyId);
        return next;
      });
      message.success(t("dataQualityGenerate.messages.applied"));
      onApplied();
    } catch (err) {
      message.error(t("dataQualityGenerate.messages.applyFailed") + ": " + String(err));
    }
  };

  const headerExtra = (
    <Select
      size="small"
      style={{ width: 240 }}
      value={selectedModelId ?? undefined}
      onChange={(v: number) => handleModelChange(v)}
      placeholder={t("dataQualityGenerate.llmModelSelectPlaceholder")}
      disabled={models.length === 0}
      dropdownMatchSelectWidth={false}
      onClick={(e) => e.stopPropagation()}
      options={models.map((m) => ({
        value: m.id,
        label: `${m.modelName}（${m.provider}）`,
      }))}
    />
  );

  return (
    <Collapse
      activeKey={collapsed ? undefined : "panel"}
      onChange={(keys) => setCollapsed(!keys.includes("panel"))}
      items={[
        {
          key: "panel",
          label: t("dataQualityGenerate.llmPanel"),
          extra: headerExtra,
          children: modelsLoadFailed ? (
            <span style={{ color: "#ff4d4f" }}>
              {t("dataQualityGenerate.llmModelsLoadFailed")}
            </span>
          ) : items.length === 0 && !loading ? (
            <span>{t("dataQualityGenerate.noSuggestions")}</span>
          ) : (
            <Space direction="vertical" style={{ width: "100%" }}>
              {items.map((item) => {
                const isAdopted = adoptedIds.has(item.propertyId);
                return (
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
                      {isAdopted && (
                        <Tag
                          color="green"
                          style={{ marginLeft: 8 }}
                          data-testid={`adopted-hint-${item.propertyId}`}
                        >
                          ✓ {t("dataQualityGenerate.adoptedHint")}
                        </Tag>
                      )}
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
                      disabled={isAdopted}
                      onClick={() => void handleApply(item)}
                    >
                      {isAdopted
                        ? `✓ ${t("dataQualityGenerate.adopted")}`
                        : t("dataQualityGenerate.adopt")}
                    </Button>
                  </div>
                );
              })}
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

  // 派生「实际能落库的勾选项」：必须同时被勾选且 status="NEW"。
  // 在 render scope 算一次，让按钮 disabled 与 handleConfirm 用同一份数据；
  // 避免「selectedIds 有勾选但全是 EXISTS」时按钮亮但 POST 空数组 → 422。
  const toSubmit = useMemo(
    () => suggestions.filter((s) => selectedIds.has(s.ruleCode) && s.status === "NEW"),
    [suggestions, selectedIds],
  );

  const handleConfirm = useCallback(async () => {
    if (!selectedDatasourceId) return;
    if (toSubmit.length === 0) {
      // 防御：用户可能勾选的都是 EXISTS 规则（已落库），不能空数组 POST。
      message.warning(t("dataQualityGenerate.messages.confirmNothingSelected"));
      return;
    }
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
  }, [selectedDatasourceId, toSubmit, t]);

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
                    disabled={toSubmit.length === 0}
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
