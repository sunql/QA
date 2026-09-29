/**
 * 数据质量规则自动生成向导页。
 * 四步：选本体类 → 选数据源 → 预览规则/采纳AI建议 → 确认落库。
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
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
    {
      title: t("dataQualityGenerate.columns.ruleType"),
      dataIndex: "ruleType",
      key: "ruleType",
      width: 160,
      // 列表展示走中文文案（与 DataQualityPage 规则列表一致），
      // value 仍为英文 enum，confirm 时按 enum 发往后端。
      render: (rt: string) => t(`dataQuality.ruleTypeLabels.${rt}`),
    },
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
          {/* 展示走 i18n 中文文案，value 保持英文 enum（HIGH/MEDIUM/LOW），
              confirm 时按 enum 发往后端。INFO 不出现在向导可选项（生成规则
              只取 HIGH/MEDIUM/LOW 三档）。 */}
          <Select.Option value="HIGH">
            {t("dataQuality.severityLabels.HIGH")}
          </Select.Option>
          <Select.Option value="MEDIUM">
            {t("dataQuality.severityLabels.MEDIUM")}
          </Select.Option>
          <Select.Option value="LOW">
            {t("dataQuality.severityLabels.LOW")}
          </Select.Option>
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
  const navigate = useNavigate();
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
    // 早返回检查 1：当前 suggestion 类型是否可自动沉淀（按 kind 派发）。
    // 4 类 kind 全部走 applySuggestion；非值域约束不再走「请联系管理员」文案。
    const payload = buildApplyPayload(item);
    if (payload.kind === null) {
      // LLM 输出残缺（缺 min/max 或 regex_pattern）→ 给 warning 但不报错。
      message.warning(t("dataQualityGenerate.messages.adoptNotApplicable"));
      return;
    }
    // 早返回检查 2：已采纳项再点。
    if (adoptedIds.has(item.propertyId)) {
      // 用户对已采纳项再点：给 info 提示而非静默 return，避免「按钮没反应」的错觉。
      message.info(t("dataQualityGenerate.messages.alreadyAdopted"));
      return;
    }
    try {
      await applySuggestion(item.propertyId, payload.payload);
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

  /**
   * 把 PropertyConstraintSuggestion 按 kind 转成 applySuggestion payload。
   * 返回 kind=null = LLM 输出残缺（缺关键字段），不发送请求。
   */
  function buildApplyPayload(item: PropertyConstraintSuggestion):
    | { kind: "allowed_values"; payload: { kind: "allowed_values"; allowedValues: string[] } }
    | { kind: "not_null"; payload: { kind: "not_null" } }
    | { kind: "range"; payload: { kind: "range"; minValue: string; maxValue: string } }
    | { kind: "pattern"; payload: { kind: "pattern"; regexPattern: string } }
    | { kind: null } {
    switch (item.kind) {
      case "allowed_values":
        return { kind: "allowed_values", payload: { kind: "allowed_values", allowedValues: item.values ?? [] } };
      case "not_null":
        return { kind: "not_null", payload: { kind: "not_null" } };
      case "range":
        if (!item.minValue || !item.maxValue) return { kind: null };
        return { kind: "range", payload: { kind: "range", minValue: item.minValue, maxValue: item.maxValue } };
      case "pattern":
        if (!item.regexPattern) return { kind: null };
        return { kind: "pattern", payload: { kind: "pattern", regexPattern: item.regexPattern } };
    }
  }

  const headerExtra = (
    <Select
      size="small"
      style={{ width: 240 }}
      value={selectedModelId ?? undefined}
      onChange={(v: number) => handleModelChange(v)}
      placeholder={t("dataQualityGenerate.llmModelSelectPlaceholder")}
      disabled={models.length === 0}
      popupMatchSelectWidth={false}
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
                    // 复合 key：同一 property 可能产生多条不同 kind 的合法建议
                  // （如 not_null + allowed_values），仅用 propertyId 会撞
                  // React duplicate key 警告。propertyId-kind 保证唯一。
                  // 后端 parsePropertyDescriptions 已按 (propertyId, kind) 去重
                  // 抖动重复，但复合 key 是对未来 / 未覆盖 corner case 的双保险。
                    key={`${item.propertyId}-${item.kind}`}
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
                          style={{ marginLeft: 8, cursor: "pointer" }}
                          data-testid={`adopted-hint-${item.propertyId}`}
                          onClick={() =>
                            navigate(`/ontology-properties?classId=${classId}`)
                          }
                          title={t("dataQualityGenerate.adoptedHintNavTitle")}
                        >
                          ✓ {t("dataQualityGenerate.adoptedHint")} →
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

  // 全选按钮：仅操作 status="NEW" 的建议（EXISTS 规则已落库，不能再确认，
  // 勾上也没有意义；只对 NEW 操作能避免用户误以为能再次提交）。
  // 状态机：所有 NEW 都已勾选 → 点击取消；否则 → 全选。
  const newSuggestions = useMemo(
    () => suggestions.filter((s) => s.status === "NEW"),
    [suggestions],
  );
  const allNewSelected = useMemo(
    () => newSuggestions.length > 0 && newSuggestions.every((s) => selectedIds.has(s.ruleCode)),
    [newSuggestions, selectedIds],
  );
  const handleToggleAll = useCallback(() => {
    setSelectedIds((prev) => {
      if (allNewSelected) {
        // 全取消：去掉所有 NEW（保留已存在的 EXISTS 勾选——本来就不该有，
        // 这里双保险：selectedIds 仅来自 toggle 的合法 row）。
        const next = new Set(prev);
        for (const s of newSuggestions) next.delete(s.ruleCode);
        return next;
      }
      const next = new Set(prev);
      for (const s of newSuggestions) next.add(s.ruleCode);
      return next;
    });
  }, [allNewSelected, newSuggestions]);

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
              <div
                style={{
                  fontWeight: 500,
                  marginBottom: 8,
                  display: "flex",
                  alignItems: "center",
                  gap: 12,
                }}
              >
                <span>
                  {t("dataQualityGenerate.suggestions")}（{suggestions.length}）
                </span>
                {/* 全选按钮：仅 NEW 可被确认；EXISTS 已落库，勾上无效。
                    把按钮放在标题右侧，符合 antd 中「行操作列与表头平齐」惯例。 */}
                {newSuggestions.length > 0 && (
                  <Button
                    size="small"
                    data-testid="dq-rule-toggle-all"
                    onClick={handleToggleAll}
                  >
                    {allNewSelected
                      ? t("dataQualityGenerate.deselectAll")
                      : t("dataQualityGenerate.selectAll")}
                  </Button>
                )}
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
