/** DataQualityPage — 数据质量（规则维护 + 质量评分）。
 *
 * Tab 0「规则」：规则列表（**六字段级联筛选** + 模糊查询）、新建/编辑/停用、
 *   单条评估与批量评估（feat-dq-rule-list-filters + 评估结果弹窗）。
 * Tab 1「质量评分」：评分列表 + 一键计算。
 *
 * 筛选栏那六个下拉是本页的既有能力，改动本文件时不要顺手简化掉：`listRules` 的
 * 入参是 `DataQualityRuleListParams` 的全部可选字段，后端 `data_quality.py` 逐个支持。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { HistoryOutlined } from "@ant-design/icons";
import {
  App,
  Badge,
  Button,
  Card,
  Col,
  Descriptions,
  Drawer,
  Empty,
  Form,
  Input,
  InputNumber,
  List,
  Modal,
  Row,
  Select,
  Space,
  Statistic,
  Switch,
  Table,
  Tabs,
  Tag,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useTranslation } from "../i18n";
import DataQualityRuleGeneratePage from "./DataQualityRuleGeneratePage";
import DataQualityReportListPage from "./DataQualityReportListPage";
import {
  createRule,
  disableRule,
  listRules,
  updateRule,
} from "../api/dataQuality";
import {
  computeScore,
  evaluateBatch,
  evaluateRule,
  listScores,
} from "../api/dataQualityScore";
import { listDataSources, getDatasourceSchema, introspectDatasource } from "../api/datasource";
import { listPropertiesByClass } from "../api/ontology";
import { fetchNextRuleCode } from "../api/dataQualityRuleNextCode";
import { suggestRuleExpression } from "../utils/ruleExpressionTemplates";
import type {
  DataQualityRule,
  DataQualityRuleCreate,
  DataQualityRuleListParams,
  RuleType,
  Severity,
} from "../types/dataQuality";
import type {
  DataQualityScore,
  EvaluateBatchResponse,
  EvaluationResult,
  ScoreListParams,
  ScoreType,
} from "../types/dataQualityScore";
import type { DataSource } from "../types/datasource";
import { useDataQualityFilterOptions } from "../hooks/useDataQualityFilterOptions";
import { downloadCsv } from "../utils/download";
import {
  appendBatchEvalHistory,
  countBatchEvalHistory,
  listBatchEvalHistory,
  type BatchEvalRecord,
} from "../utils/batchEvalHistory";

// 规则 tab 分页 localStorage key（feat-dq-rules-pagination-bug，2026-09-15）
const PAGE_SIZE_STORAGE_KEY = "qa.dq.rules.pageSize";
const DEFAULT_PAGE_SIZE = 20;
const PAGE_SIZE_OPTIONS = [20, 50, 100, 200, 500];

type EnabledFilter = "all" | "enabled" | "disabled";

interface FilterValues {
  ruleName: string | undefined;
  datasourceId: number | undefined;
  sourceClassId: number | undefined;
  targetTables: string[];
  ruleType: RuleType | undefined;
  severity: Severity | undefined;
  enabled: EnabledFilter;
}

const EMPTY_FILTERS: FilterValues = {
  ruleName: undefined,
  datasourceId: undefined,
  sourceClassId: undefined,
  targetTables: [],
  ruleType: undefined,
  severity: undefined,
  enabled: "all",
};

const RULE_TYPES: RuleType[] = [
  "COMPLETENESS",
  "VALIDITY",
  "UNIQUENESS",
  "CONSISTENCY",
  "REFERENTIAL",
  "TIMELINESS",
];

const SEVERITIES: Severity[] = ["HIGH", "MEDIUM", "LOW", "INFO"];

const SCORE_TYPES: ScoreType[] = ["TABLE", "GLOBAL"];

function severityColor(s: Severity): string {
  switch (s) {
    case "HIGH":
      return "red";
    case "MEDIUM":
      return "orange";
    case "LOW":
      return "blue";
    default:
      return "default";
  }
}

/** 综合评分配色：>=90 绿 / >=70 橙 / 其余红。 */
function scoreColor(score: string): string {
  const n = parseFloat(score);
  if (n >= 90) return "green";
  if (n >= 70) return "orange";
  return "red";
}

// ---------------------------------------------------------------------------
// Tab 0：规则
// ---------------------------------------------------------------------------

function RulesTab() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  // 走 AntdApp 上下文拿 message 实例；避免 antd 5 静态 message API 看不到动态
  // theme token 时报「Static function can not consume context」警告（dev-only，
  // prod 不会阻断功能但 dev console 噪音+ rc-util scrollTo 在某些 React 18
  // StrictMode 时序下访问 undefined.startTime 报红）。
  const { message } = App.useApp();
  const [rules, setRules] = useState<DataQualityRule[]>([]);
  const [loading, setLoading] = useState(false);
  const [filters, setFilters] = useState<FilterValues>(EMPTY_FILTERS);
  const [editing, setEditing] = useState<DataQualityRule | null>(null);
  const [creating, setCreating] = useState(false);
  const [datasources, setDatasources] = useState<DataSource[]>([]);
  const [form] = Form.useForm<DataQualityRuleCreate>();
  const { options: filterOptions } = useDataQualityFilterOptions();
  const [evaluating, setEvaluating] = useState<number | null>(null);
  const [evalModal, setEvalModal] = useState<EvaluationResult | null>(null);
  const [batchLoading, setBatchLoading] = useState(false);
  // 多选批量评估：保存用户勾选的 rule.id。空 Set 表示按老逻辑「全选已启用」。
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());

  // ===== 新建/编辑规则 Modal 自动填充状态（feat-rule-create-form-autofill，2026-09-15）=====
  // 当前 Modal 已选的数据源 schema（tables[].columns[{columnName, dataType, isPrimaryKey?}]）。
  // datasourceId 切换时拉取；target_table → target_column 联动。
  const [dsSchema, setDsSchema] = useState<
    { tableName: string; columns: { columnName: string; dataType: string; isPrimaryKey?: boolean }[] }[]
  >([]);
  const [schemaLoading, setSchemaLoading] = useState(false);
  const [ontologyProperties, setOntologyProperties] = useState<
    { sourceColumn: string; isForeignKey?: boolean; refClassId?: number; minValue?: string | number | null; maxValue?: string | number | null }[]
  >([]);
  // 后端 next-code 端点返回的建议编码（用户选类或数据源后异步拿）
  const [codeSuggestion, setCodeSuggestion] = useState("");
  // 用户手改过 → 锁住 auto-fill，避免覆盖用户输入
  const [userEditedCode, setUserEditedCode] = useState(false);
  const [userEditedName, setUserEditedName] = useState(false);

  // feat-dq-rules-pagination-bug (2026-09-15)：分页数量修复。
  // 受控 pageSize + localStorage 记忆，刷新后保留。
  const [pageSize, setPageSize] = useState<number>(() => {
    if (typeof window === "undefined") return DEFAULT_PAGE_SIZE;
    try {
      const stored = window.localStorage.getItem(PAGE_SIZE_STORAGE_KEY);
      if (stored) {
        const n = parseInt(stored, 10);
        if (PAGE_SIZE_OPTIONS.includes(n)) return n;
      }
    } catch {
      // localStorage 不可用（隐私模式 / 异常），用默认值
    }
    return DEFAULT_PAGE_SIZE;
  });
  useEffect(() => {
    if (typeof window === "undefined") return;
    try {
      window.localStorage.setItem(PAGE_SIZE_STORAGE_KEY, String(pageSize));
    } catch {
      // 写入失败不阻断主流程
    }
  }, [pageSize]);

  // feat-eval-batch-result (2026-09-15)：批量评估结果 modal + 历史抽屉。
  const [batchResult, setBatchResult] = useState<EvaluateBatchResponse | null>(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [historyRefreshTick, setHistoryRefreshTick] = useState(0);
  const [historyCount, setHistoryCount] = useState(() => countBatchEvalHistory());
  // 当 batchResult 来自历史回看时为 true → 「保存到历史」按钮禁用
  const [batchResultFromHistory, setBatchResultFromHistory] = useState(false);

  // 加载数据源列表（用于表单下拉 + 筛选下拉回填）
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const dsList = await listDataSources(true);
        if (!cancelled) setDatasources(dsList);
      } catch {
        // 静默：数据源列表为空时表单仍可用，只是没有下拉选项
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const updateFilter = useCallback(
    <K extends keyof FilterValues>(key: K, value: FilterValues[K]) => {
      setFilters((prev) => {
        const next = { ...prev, [key]: value };
        // 切换数据源时清空目标表（级联）
        if (key === "datasourceId") {
          next.targetTables = [];
        }
        return next;
      });
    },
    [],
  );

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const params: DataQualityRuleListParams = {};
      if (filters.ruleName) params.ruleName = filters.ruleName;
      if (filters.datasourceId !== undefined) params.datasourceId = filters.datasourceId;
      if (filters.sourceClassId !== undefined) params.sourceClassId = filters.sourceClassId;
      // 多选 targetTables 优先于单值；不传列表走全量
      if (filters.targetTables.length > 0) params.targetTables = filters.targetTables;
      if (filters.ruleType) params.ruleType = filters.ruleType;
      if (filters.severity) params.severity = filters.severity;
      if (filters.enabled !== "all") params.enabled = filters.enabled;
      const data = await listRules(params);
      setRules(data);
      // 列表刷新后剔除已不存在的 id（avoid stale rowSelection 选中已删除行）
      setSelectedIds((prev) => {
        if (prev.size === 0) return prev;
        const alive = new Set(data.map((r) => r.id));
        const next = new Set<number>();
        for (const id of prev) if (alive.has(id)) next.add(id);
        return next.size === prev.size ? prev : next;
      });
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      void message.error(msg);
    } finally {
      setLoading(false);
    }
  }, [filters]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // 数据源下的目标表选项（DISTINCT 全量 + 前端按 datasourceId 过滤）
  const availableTargetTables = useMemo(() => {
    if (!filterOptions.targetTables) return [];
    return filterOptions.targetTables;
  }, [filterOptions.targetTables]);

  const resetFilters = useCallback(() => {
    setFilters(EMPTY_FILTERS);
    // 重置筛选时不勾选清掉——筛选条件改完后旧选择可能已不再当前页可见
    setSelectedIds(new Set());
  }, []);

  const handleCreate = async () => {
    const values = await form.validateFields();
    try {
      await createRule(values);
      void message.success(t("dataQuality.createSuccess"));
      setCreating(false);
      form.resetFields();
      // feat-rule-create-form-autofill（2026-09-15）：保存成功后重置自动填充 state，
      // 下次新建从干净状态开始（避免残留 userEditedCode=true 让自动编码不生效）
      setUserEditedCode(false);
      setUserEditedName(false);
      setCodeSuggestion("");
      setDsSchema([]);
      setOntologyProperties([]);
      await refresh();
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      void message.error(msg);
    }
  };

  const handleUpdate = async () => {
    if (!editing) return;
    const values = await form.validateFields();
    try {
      await updateRule(editing.id, values);
      void message.success(t("dataQuality.updateSuccess"));
      setEditing(null);
      form.resetFields();
      setDsSchema([]);
      setOntologyProperties([]);
      await refresh();
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      void message.error(msg);
    }
  };

  const handleDisable = async (rule: DataQualityRule) => {
    try {
      await disableRule(rule.id);
      void message.success(t("dataQuality.disableSuccess"));
      await refresh();
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      void message.error(msg);
    }
  };

  const handleEvaluate = async (rule: DataQualityRule) => {
    setEvaluating(rule.id);
    try {
      const result = await evaluateRule(rule.id);
      setEvalModal(result);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      void message.error(msg);
    } finally {
      setEvaluating(null);
    }
  };

  /** 批量评估**用户当前勾选的规则** —— 后端会按 rule_ids 执行。
   *
   *  行为分支：
   *  - 用户没勾任何行 → 警告「请先勾选要批量评估的规则」不发请求。
   *  - 勾选里含未启用规则 → 警告「已忽略 N 条未启用规则」（不发，仅提示）；
   *    把已启用的子集送给后端。
   *  - 全是启用 → 直接送。 */
  const handleBatchEvaluate = async () => {
    if (selectedIds.size === 0) {
      void message.warning(t("dataQuality.evaluateBatchNoSelection"));
      return;
    }
    const selectedRules = rules.filter((r) => selectedIds.has(r.id));
    const disabledInSelection = selectedRules.filter((r) => !r.isEnabled);
    const enabledInSelection = selectedRules.filter((r) => r.isEnabled);
    if (enabledInSelection.length === 0) {
      void message.warning(t("dataQuality.evaluateBatchNoEnabled"));
      return;
    }
    if (disabledInSelection.length > 0) {
      void message.warning(
        t("dataQuality.evaluateBatchHasDisabled", {
          count: disabledInSelection.length,
        }),
      );
    }
    setBatchLoading(true);
    try {
      const result = await evaluateBatch({
        ruleIds: enabledInSelection.map((r) => r.id),
      });
      // feat-eval-batch-result (2026-09-15)：弹窗显示详细结果，让用户能看每条 PASS/FAIL + 失败原因 + 导出。
      setBatchResult(result);
      setBatchResultFromHistory(false);
      void message.success(
        t("dataQuality.evaluateBatchSuccess", {
          total: result.summaryTotal,
          passed: result.summaryPassed,
        }),
      );
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      void message.error(msg);
    } finally {
      setBatchLoading(false);
    }
  };

  // 把规则值写入表单。把规则对象作为依赖存进 ref，而不是触发整个组件重渲。
  // 写入时机放在 Modal.afterOpenChange(open=true) 回调里——此时 Modal 已经完全
  // 打开、Form 子组件已经挂载完成，setFieldsValue 不会被 destroyOnHidden
  // + preserve=false 的重挂载时序吞掉（jsdom 不复现这条路径，测试通过 ≠ 真机可用）。
  const editingRef = useRef<DataQualityRule | null>(null);

  const openEdit = (rule: DataQualityRule) => {
    editingRef.current = rule;
    setEditing(rule);
  };

  // 弹窗完全打开后写值；threshold 是后端 DECIMAL 字符串，InputNumber 期望 number，
  // 用 Number() 强转避免类型不匹配导致字段空白。
  const writeEditingToForm = useCallback(() => {
    const rule = editingRef.current;
    if (!rule) return;
    const thresholdNum =
      rule.threshold === null || rule.threshold === undefined
        ? undefined
        : Number(rule.threshold);
    form.setFieldsValue({
      ruleName: rule.ruleName,
      ruleCode: rule.ruleCode,
      sourceClassId: rule.sourceClassId ?? undefined,
      datasourceId: rule.datasourceId,
      targetTable: rule.targetTable,
      targetColumn: rule.targetColumn ?? undefined,
      ruleType: rule.ruleType,
      ruleExpression: rule.ruleExpression ?? undefined,
      threshold: thresholdNum,
      severity: rule.severity,
      isEnabled: rule.isEnabled,
      version: rule.version,
      owner: rule.owner ?? undefined,
      description: rule.description ?? undefined,
    } as Partial<DataQualityRuleCreate>);
  }, [form]);

  // 兜底：editing 变化时也写一次，覆盖创建→编辑状态切换的边缘场景。
  useEffect(() => {
    if (editing) writeEditingToForm();
  }, [editing, writeEditingToForm]);

  // ===== 新建/编辑规则 Modal 自动填充 effect（feat-rule-create-form-autofill，2026-09-15）=====
  // 仅在「新建」模式下生效（编辑模式禁用 6 个字段，effect 不该覆盖写入的值）。
  const isCreating = creating && !editing;

  // 当前 Modal 内 form 字段的快照（用 Form.useWatch 拿受控值，避免 getFieldsValue 异步）
  const watchedDatasourceId = Form.useWatch("datasourceId", form);
  const watchedTargetTable = Form.useWatch("targetTable", form);
  const watchedTargetColumn = Form.useWatch("targetColumn", form);
  const watchedRuleType = Form.useWatch("ruleType", form);
  const watchedSourceClassId = Form.useWatch("sourceClassId", form);

  // 选数据源 → 加载 schema（与批量创建页 RuleBatchStepBasic 同模式：先 getSchema，
  // 失败 fallback introspectDatasource）
  useEffect(() => {
    if (!isCreating || !watchedDatasourceId) {
      setDsSchema([]);
      return;
    }
    const dsId = watchedDatasourceId;
    let cancelled = false;
    setSchemaLoading(true);
    (async () => {
      try {
        let resp;
        try {
          resp = await getDatasourceSchema(dsId);
        } catch {
          resp = await introspectDatasource(dsId);
        }
        if (!cancelled) setDsSchema(resp.tables);
      } catch (err: unknown) {
        if (!cancelled) {
          const msg = err instanceof Error ? err.message : String(err);
          void message.error(msg);
          setDsSchema([]);
        }
      } finally {
        if (!cancelled) setSchemaLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [isCreating, watchedDatasourceId, message]);

  // 选类 → 加载 ontology properties（用于 suggestRuleExpression 推断 min/max）
  useEffect(() => {
    if (!isCreating || watchedSourceClassId == null) {
      setOntologyProperties([]);
      return;
    }
    const cid = watchedSourceClassId;
    let cancelled = false;
    (async () => {
      try {
        const props = await listPropertiesByClass(cid);
        if (!cancelled) {
          setOntologyProperties(
            props
              .filter((p): p is typeof p & { sourceColumn: string } => !!p.sourceColumn)
              .map((p) => ({
                sourceColumn: p.sourceColumn,
                isForeignKey: p.isForeignKey,
                refClassId: p.refClassId ?? undefined,
                minValue: p.minValue,
                maxValue: p.maxValue,
              })),
          );
        }
      } catch {
        if (!cancelled) setOntologyProperties([]);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [isCreating, watchedSourceClassId]);

  // 选类 → 异步拿 next-code 建议
  useEffect(() => {
    if (!isCreating) {
      setCodeSuggestion("");
      return;
    }
    const cls = filterOptions.classOptions.find((c) => c.id === watchedSourceClassId);
    const key = cls?.className || "";
    if (!key) {
      setCodeSuggestion("");
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const r = await fetchNextRuleCode({ className: key });
        if (!cancelled) setCodeSuggestion(r.code);
      } catch {
        if (!cancelled) setCodeSuggestion("");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [isCreating, watchedSourceClassId, filterOptions.classOptions]);

  // 自动填 ruleCode：新建 + 未被用户手改过 → 用 codeSuggestion
  useEffect(() => {
    if (!isCreating || userEditedCode || !codeSuggestion) return;
    form.setFieldsValue({ ruleCode: codeSuggestion });
  }, [isCreating, userEditedCode, codeSuggestion, form]);

  // 自动拼 ruleName：新建 + 未被用户手改过 → 智能拼接 dsName-table-column-ruleTypeCN
  // CONSISTENCY / REFERENTIAL 可能没 column，无列名时跳过该段
  useEffect(() => {
    if (!isCreating || userEditedName) return;
    const ds = datasources.find((d) => d.id === watchedDatasourceId);
    const dsName = ds?.name || "";
    const table = (watchedTargetTable || "").trim();
    const column = (watchedTargetColumn || "").trim();
    const ruleType = (watchedRuleType || "").trim();
    if (!dsName && !table && !ruleType) {
      // 全空时不动（不抹掉用户已经填好的名称）
      return;
    }
    const parts = [dsName, table, column, ruleType].filter((p) => p.length > 0);
    // 用 buildRuleName 的「智能拼接」语义：去尾空段。
    // 但 buildRuleName 强依赖「列名必须有」——这里放宽：任何空段都跳过。
    const name = parts.join("-");
    form.setFieldsValue({ ruleName: name });
  }, [
    isCreating,
    userEditedName,
    watchedDatasourceId,
    watchedTargetTable,
    watchedTargetColumn,
    watchedRuleType,
    datasources,
    form,
  ]);

  // 自动填 ruleExpression：选完 ruleType + targetColumn + targetTable 后跑模板
  // 注意：只在新建模式生效（编辑模式用户可手改）；只在用户没手改过且模板能自动填时覆盖
  useEffect(() => {
    if (!isCreating) return;
    const ruleType = (watchedRuleType || "").trim();
    const table = (watchedTargetTable || "").trim();
    const column = (watchedTargetColumn || "").trim();
    if (!ruleType) return;
    const currentTable = dsSchema.find((t) => t.tableName === table);
    const col = currentTable?.columns.find((c) => c.columnName === column);
    if (!col) {
      // 缺列元信息（用户可能没选 column，或 column 不在 schema 中）→ 不自动填
      return;
    }
    const prop = ontologyProperties.find((p) => p.sourceColumn === col.columnName);
    const candidate = suggestRuleExpression({
      column: { name: col.columnName, dataType: col.dataType },
      ruleType: ruleType as "COMPLETENESS" | "VALIDITY" | "UNIQUENESS" | "CONSISTENCY" | "REFERENTIAL" | "TIMELINESS",
      ontologyProperty: prop
        ? {
            isForeignKey: prop.isForeignKey,
            minValue: prop.minValue ?? null,
            maxValue: prop.maxValue ?? null,
          }
        : null,
    });
    if (candidate.canAutoFill && candidate.expression !== null) {
      // 只有在 form 当前值为空或上一次也是自动填时覆盖——避免覆盖用户手改
      const current = form.getFieldValue("ruleExpression");
      if (!current) {
        form.setFieldsValue({ ruleExpression: candidate.expression });
      }
    }
  }, [
    isCreating,
    watchedRuleType,
    watchedTargetColumn,
    watchedTargetTable,
    dsSchema,
    ontologyProperties,
    form,
  ]);

  // 切换数据源 / 表 → 清掉 column（schema 已变，旧 column 可能不存在）
  useEffect(() => {
    if (!isCreating) return;
    if (!watchedTargetTable) {
      form.setFieldsValue({ targetColumn: undefined });
    }
  }, [isCreating, watchedTargetTable, form]);

  const columns: ColumnsType<DataQualityRule> = [
    {
      title: t("dataQuality.ruleCode"),
      dataIndex: "ruleCode",
      key: "ruleCode",
      width: 200,
    },
    {
      title: t("dataQuality.ruleName"),
      dataIndex: "ruleName",
      key: "ruleName",
    },
    {
      title: t("dataQuality.datasource"),
      dataIndex: "datasourceId",
      key: "datasourceId",
      width: 100,
      render: (id: number) => {
        const ds = datasources.find((d) => d.id === id);
        return ds ? ds.name : `#${id}`;
      },
    },
    {
      title: t("dataQuality.targetTable"),
      dataIndex: "targetTable",
      key: "targetTable",
      width: 140,
    },
    {
      title: t("dataQuality.ruleType"),
      dataIndex: "ruleType",
      key: "ruleType",
      width: 130,
      render: (rt: RuleType) => t(`dataQuality.ruleTypeLabels.${rt}`),
    },
    {
      title: t("dataQuality.severity"),
      dataIndex: "severity",
      key: "severity",
      width: 100,
      render: (s: Severity) => (
        <Tag color={severityColor(s)}>{t(`dataQuality.severityLabels.${s}`)}</Tag>
      ),
    },
    {
      title: t("dataQuality.threshold"),
      dataIndex: "threshold",
      key: "threshold",
      width: 100,
    },
    {
      title: t("dataQuality.enabled"),
      dataIndex: "isEnabled",
      key: "isEnabled",
      width: 90,
      render: (e: boolean) => (e ? t("common.yes") : t("common.no")),
    },
    {
      title: t("dataQuality.owner"),
      dataIndex: "owner",
      key: "owner",
      width: 120,
    },
    {
      title: t("common.actions"),
      key: "actions",
      width: 220,
      render: (_: unknown, record: DataQualityRule) => (
        <Space>
          <Button
            size="small"
            loading={evaluating === record.id}
            onClick={() => void handleEvaluate(record)}
          >
            {t("dataQuality.evaluate")}
          </Button>
          <Button size="small" onClick={() => openEdit(record)}>
            {t("common.edit")}
          </Button>
          <Button
            size="small"
            danger
            disabled={!record.isEnabled}
            onClick={() => void handleDisable(record)}
          >
            {t("common.disabled")}
          </Button>
        </Space>
      ),
    },
  ];

  return (
    <>
      <Space style={{ marginBottom: 16 }} wrap>
        <Select
          allowClear
          showSearch
          data-testid="filter-class-name"
          placeholder={t("dataQuality.filterClassName")}
          style={{ width: 200 }}
          value={filters.sourceClassId}
          onChange={(v) =>
            updateFilter("sourceClassId", v === undefined ? undefined : Number(v))
          }
          options={filterOptions.classOptions.map((c) => ({
            label: c.className,
            value: c.id,
          }))}
        />
        <Select
          allowClear
          showSearch
          data-testid="filter-rule-name"
          placeholder={t("dataQuality.filterRuleName")}
          style={{ width: 200 }}
          value={filters.ruleName}
          onChange={(v) => updateFilter("ruleName", v)}
          options={filterOptions.ruleNames.map((n) => ({ label: n, value: n }))}
        />
        <Select
          allowClear
          showSearch
          data-testid="filter-datasource"
          placeholder={t("dataQuality.filterDatasource")}
          style={{ width: 200 }}
          value={filters.datasourceId}
          onChange={(v) =>
            updateFilter("datasourceId", v === undefined ? undefined : Number(v))
          }
          options={filterOptions.datasourceIds.map((d) => ({
            label: d.name,
            value: d.id,
          }))}
        />
        <Select
          mode="multiple"
          allowClear
          showSearch
          data-testid="filter-target-table"
          disabled={filters.datasourceId === undefined}
          placeholder={
            filters.datasourceId === undefined
              ? t("dataQuality.filterTargetTableDisabled")
              : t("dataQuality.filterTargetTable")
          }
          style={{ width: 240 }}
          value={filters.targetTables}
          onChange={(v) => updateFilter("targetTables", v as string[])}
          options={availableTargetTables.map((tbl) => ({
            label: tbl,
            value: tbl,
          }))}
        />
        <Select
          allowClear
          data-testid="filter-rule-type"
          placeholder={t("dataQuality.filterType")}
          style={{ width: 160 }}
          value={filters.ruleType}
          onChange={(v) => updateFilter("ruleType", v as RuleType | undefined)}
          options={RULE_TYPES.map((rt) => ({
            label: t(`dataQuality.ruleTypeLabels.${rt}`),
            value: rt,
          }))}
        />
        <Select
          allowClear
          data-testid="filter-severity"
          placeholder={t("dataQuality.filterSeverity")}
          style={{ width: 140 }}
          value={filters.severity}
          onChange={(v) => updateFilter("severity", v as Severity | undefined)}
          options={filterOptions.severities.map((s) => ({
            label: t(`dataQuality.severityLabels.${s}`),
            value: s,
          }))}
        />
        <Select
          data-testid="filter-enabled"
          placeholder={t("dataQuality.filterEnabled")}
          style={{ width: 130 }}
          value={filters.enabled}
          onChange={(v) => updateFilter("enabled", (v ?? "all") as EnabledFilter)}
          options={[
            { label: t("dataQuality.filterEnabledOptions.all"), value: "all" },
            { label: t("dataQuality.filterEnabledOptions.enabled"), value: "enabled" },
            { label: t("dataQuality.filterEnabledOptions.disabled"), value: "disabled" },
          ]}
        />
        <Button onClick={resetFilters}>{t("common.reset")}</Button>
        <Button type="primary" onClick={() => setCreating(true)}>
          {t("dataQuality.createRule")}
        </Button>
        <Button onClick={() => navigate("/data-quality/rules/batch-create")}>
          {t("dataQuality.batchCreate.enterButton")}
        </Button>
        <Button onClick={() => void refresh()}>
          {t("common.refresh")}
        </Button>
        <Button loading={batchLoading} onClick={() => void handleBatchEvaluate()}>
          {selectedIds.size > 0
            ? t("dataQuality.evaluateBatchSelected", { count: selectedIds.size })
            : t("dataQuality.evaluateBatch")}
        </Button>
        <Button
          icon={<HistoryOutlined />}
          onClick={() => setHistoryOpen(true)}
          data-testid="batch-eval-history-btn"
        >
          <Badge
            count={historyCount}
            size="small"
            offset={[6, -2]}
            title={t("dataQuality.batchResult.history.countTitle")}
          >
            {t("dataQuality.batchResult.history.button")}
          </Badge>
        </Button>
      </Space>

      <Table
        rowKey="id"
        loading={loading}
        columns={columns}
        dataSource={rules}
        rowSelection={{
          selectedRowKeys: [...selectedIds],
          onChange: (keys) => setSelectedIds(new Set(keys as number[])),
          preserveSelectedRowKeys: true,
        }}
        pagination={{
          pageSize,
          pageSizeOptions: PAGE_SIZE_OPTIONS,
          showSizeChanger: true,
          showTotal: (total) => t("common.totalItems", { total }),
          onChange: (_nextPage, nextSize) => {
            setPageSize(nextSize);
            // antd Table 自己管理 current；切换 size 不重置到第 1 页的行为由 antd 默认处理
          },
        }}
      />

      {/* 评估结果 */}
      <Modal
        title={t("dataQuality.evalResult.title")}
        open={evalModal !== null}
        onCancel={() => setEvalModal(null)}
        footer={
          <Button onClick={() => setEvalModal(null)}>{t("common.close")}</Button>
        }
        width={560}
      >
        {evalModal && (
          <Descriptions column={2} bordered size="small">
            <Descriptions.Item label={t("dataQuality.evalResult.ruleCode")}>
              {evalModal.ruleCode}
            </Descriptions.Item>
            <Descriptions.Item label={t("dataQuality.evalResult.ruleType")}>
              {evalModal.ruleType}
            </Descriptions.Item>
            <Descriptions.Item label={t("dataQuality.evalResult.totalCount")}>
              {evalModal.totalCount.toLocaleString()}
            </Descriptions.Item>
            <Descriptions.Item label={t("dataQuality.evalResult.passedCount")}>
              {evalModal.passedCount.toLocaleString()}
            </Descriptions.Item>
            <Descriptions.Item label={t("dataQuality.evalResult.passRate")}>
              <Badge
                status={evalModal.status === "PASS" ? "success" : "error"}
                text={`${evalModal.passRate.toFixed(2)}%`}
              />
            </Descriptions.Item>
            <Descriptions.Item label={t("dataQuality.evalResult.status")}>
              <Tag color={evalModal.status === "PASS" ? "green" : "red"}>
                {evalModal.status === "PASS"
                  ? t("dataQuality.evalResult.pass")
                  : t("dataQuality.evalResult.fail")}
              </Tag>
            </Descriptions.Item>
            <Descriptions.Item label={t("dataQuality.evalResult.duration")}>
              {evalModal.durationMs} ms
            </Descriptions.Item>
            {evalModal.message && (
              <Descriptions.Item
                label={t("dataQuality.evalResult.message")}
                span={2}
              >
                {evalModal.message}
              </Descriptions.Item>
            )}
          </Descriptions>
        )}
      </Modal>

      {/* feat-eval-batch-result (2026-09-15)：批量评估结果 modal。
          - 顶部 summary 4 个 statistic
          - 中间 Table 列出每条 result，message 列显示失败原因
          - footer：导出 CSV / 保存到历史 / 关闭 */}
      <Modal
        title={t("dataQuality.batchResult.title")}
        open={batchResult !== null}
        onCancel={() => setBatchResult(null)}
        width={960}
        footer={
          <Space>
            <Button
              disabled={!batchResult || (batchResult.results ?? []).length === 0}
              onClick={() => {
                if (!batchResult) return;
                const rows = (batchResult.results ?? []).map((r) => ({
                  ruleCode: r.ruleCode,
                  ruleType: r.ruleType,
                  status: r.status,
                  totalCount: r.totalCount,
                  passedCount: r.passedCount,
                  passRate: r.passRate.toFixed(2),
                  durationMs: r.durationMs,
                  message: r.message ?? "",
                }));
                const ts = new Date()
                  .toISOString()
                  .replace(/[:.]/g, "-")
                  .slice(0, 19);
                downloadCsv(rows, `evaluation-batch-${ts}.csv`);
              }}
            >
              {t("dataQuality.batchResult.exportCsv")}
            </Button>
            <Button
              type="primary"
              disabled={batchResultFromHistory || !batchResult}
              onClick={() => {
                if (!batchResult) return;
                const saved = appendBatchEvalHistory(batchResult);
                if (saved) {
                  void message.success(t("dataQuality.batchResult.savedToHistory"));
                  setHistoryRefreshTick((n) => n + 1);
                  setHistoryCount(countBatchEvalHistory());
                } else {
                  void message.warning(t("dataQuality.batchResult.saveFailed"));
                }
              }}
            >
              {t("dataQuality.batchResult.saveToHistory")}
            </Button>
            <Button onClick={() => setBatchResult(null)}>{t("common.close")}</Button>
          </Space>
        }
      >
        {batchResult && (
          <>
            <Row gutter={16} style={{ marginBottom: 16 }}>
              <Col span={6}>
                <Card size="small">
                  <Statistic
                    title={t("dataQuality.batchResult.summary.total")}
                    value={batchResult.summaryTotal}
                  />
                </Card>
              </Col>
              <Col span={6}>
                <Card size="small">
                  <Statistic
                    title={t("dataQuality.batchResult.summary.passed")}
                    value={batchResult.summaryPassed}
                    valueStyle={{ color: "#52c41a" }}
                  />
                </Card>
              </Col>
              <Col span={6}>
                <Card size="small">
                  <Statistic
                    title={t("dataQuality.batchResult.summary.failed")}
                    value={
                      batchResult.results.filter((r) => r.status === "FAIL")
                        .length
                    }
                    valueStyle={{ color: "#fa8c16" }}
                  />
                </Card>
              </Col>
              <Col span={6}>
                <Card size="small">
                  <Statistic
                    title={t("dataQuality.batchResult.summary.errored")}
                    value={
                      batchResult.results.filter((r) => r.status === "ERROR")
                        .length
                    }
                    valueStyle={{ color: "#cf1322" }}
                  />
                </Card>
              </Col>
            </Row>
            <Table<EvaluationResult>
              rowKey="ruleId"
              size="small"
              pagination={{ pageSize: 10 }}
              dataSource={batchResult.results}
              columns={[
                {
                  title: t("dataQuality.batchResult.columns.ruleCode"),
                  dataIndex: "ruleCode",
                  key: "ruleCode",
                  width: 160,
                },
                {
                  title: t("dataQuality.batchResult.columns.ruleType"),
                  dataIndex: "ruleType",
                  key: "ruleType",
                  width: 120,
                },
                {
                  title: t("dataQuality.batchResult.columns.status"),
                  dataIndex: "status",
                  key: "status",
                  width: 90,
                  render: (v: EvaluationResult["status"]) => {
                    const color =
                      v === "PASS" ? "green" : v === "FAIL" ? "orange" : "red";
                    return <Tag color={color}>{v}</Tag>;
                  },
                },
                {
                  title: t("dataQuality.batchResult.columns.passRate"),
                  dataIndex: "passRate",
                  key: "passRate",
                  width: 100,
                  render: (v: number) => `${v.toFixed(2)}%`,
                },
                {
                  title: t("dataQuality.batchResult.columns.total"),
                  dataIndex: "totalCount",
                  key: "totalCount",
                  width: 80,
                  render: (v: number) => v.toLocaleString(),
                },
                {
                  title: t("dataQuality.batchResult.columns.passed"),
                  dataIndex: "passedCount",
                  key: "passedCount",
                  width: 80,
                  render: (v: number) => v.toLocaleString(),
                },
                {
                  title: t("dataQuality.batchResult.columns.duration"),
                  dataIndex: "durationMs",
                  key: "durationMs",
                  width: 80,
                  render: (v: number) => `${v} ms`,
                },
                {
                  title: t("dataQuality.batchResult.columns.message"),
                  dataIndex: "message",
                  key: "message",
                  render: (v: string | null) =>
                    v ? (
                      <span style={{ whiteSpace: "pre-wrap" }}>{v}</span>
                    ) : (
                      <span style={{ color: "#999" }}>{t("common.dash")}</span>
                    ),
                },
              ]}
            />
          </>
        )}
      </Modal>

      {/* 历史抽屉（feat-eval-batch-result）：点历史条目 → 重新打开当时的 modal（只读 + 可再导 CSV）。 */}
      <Drawer
        title={t("dataQuality.batchResult.history.title")}
        open={historyOpen}
        onClose={() => setHistoryOpen(false)}
        width={480}
      >
        <HistoryList
          refreshTick={historyRefreshTick}
          onOpen={(record) => {
            // 把历史 record 还原成 EvaluateBatchResponse 形态，复用 batchResult modal
            setBatchResult({
              results: record.results,
              summaryTotal: record.summary.total,
              summaryPassed: record.summary.passed,
            });
            setBatchResultFromHistory(true);
            setHistoryOpen(false);
          }}
        />
      </Drawer>

      <Modal
        title={editing ? t("dataQuality.editRule") : t("dataQuality.createRule")}
        open={creating || editing !== null}
        onCancel={() => {
          setCreating(false);
          setEditing(null);
          editingRef.current = null;
          form.resetFields();
          // feat-rule-create-form-autofill（2026-09-15）：关闭 Modal 时重置自动填充状态，
          // 下次打开从干净状态开始；避免上次手改标记 + 类选择残留到新建流。
          setUserEditedCode(false);
          setUserEditedName(false);
          setCodeSuggestion("");
          setDsSchema([]);
          setOntologyProperties([]);
        }}
        onOk={editing ? handleUpdate : handleCreate}
        okText={t("common.save")}
        cancelText={t("common.cancel")}
        width={640}
        destroyOnHidden
        afterOpenChange={(open) => {
          // 弹窗完全打开后再写值——此时 Form 子组件已挂载，
          // setFieldsValue 不会再被 destroyOnHidden 的重挂载时序吞掉。
          if (open) writeEditingToForm();
        }}
      >
        <Form form={form} layout="vertical" preserve={false}>
          <Form.Item
            name="ruleCode"
            label={t("dataQuality.ruleCode")}
            rules={[
              { required: true },
              {
                pattern: /^[A-Z][A-Z0-9_-]*$/,
                message: t("dataQuality.ruleCodePattern"),
              },
            ]}
            extra={
              !editing && codeSuggestion ? (
                <span style={{ color: "#999", fontSize: 12 }}>
                  {t("dataQuality.ruleCodeAutoHint", { code: codeSuggestion })}
                </span>
              ) : null
            }
          >
            <Input
              disabled={!!editing}
              onChange={() => {
                if (!editing) setUserEditedCode(true);
              }}
            />
          </Form.Item>
          <Form.Item
            name="ruleName"
            label={t("dataQuality.ruleName")}
            rules={[{ required: true }]}
          >
            <Input
              disabled={!!editing}
              onChange={() => {
                if (!editing) setUserEditedName(true);
              }}
            />
          </Form.Item>
          {/* feat-rule-create-form-autofill（2026-09-15）：选类后异步拿 next-code
              作为编码建议；选类不影响其它字段，仅触发编码 + ontology property 加载。
              必须由 Form.Item 控制 value/onChange —— 之前用 React useState 控制导致
              sourceClassId 没写进 form.values，createRule payload 漏字段，DB 不存。 */}
          <Form.Item name="sourceClassId" label={t("dataQuality.sourceClass")}>
            <Select
              disabled={!!editing}
              allowClear
              placeholder={t("dataQuality.sourceClassPlaceholder")}
              options={filterOptions.classOptions.map((c) => ({
                label: c.className,
                value: c.id,
              }))}
            />
          </Form.Item>
          <Form.Item
            name="datasourceId"
            label={t("dataQuality.datasource")}
            rules={[{ required: true }]}
          >
            <Select
              disabled={!!editing}
              placeholder={t("dataQuality.datasourcePlaceholder")}
              options={datasources.map((ds) => ({
                label: `${ds.name} (${ds.type})`,
                value: ds.id,
              }))}
            />
          </Form.Item>
          {/* targetTable 改为 Select，联动 datasource schema 的 tables。
              editing 时 disabled——避免误改 target_table 导致评估器跑错表。 */}
          <Form.Item
            name="targetTable"
            label={t("dataQuality.targetTable")}
            rules={[{ required: true }]}
          >
            <Select
              disabled={!!editing}
              showSearch
              loading={schemaLoading}
              allowClear
              placeholder={t("dataQuality.targetTablePlaceholder")}
              options={dsSchema.map((t) => ({
                value: t.tableName,
                label: t.tableName,
              }))}
              filterOption={(input, option) =>
                String(option?.value ?? "").toLowerCase().includes(input.toLowerCase())
              }
            />
          </Form.Item>
          <Form.Item name="targetColumn" label={t("dataQuality.targetColumn")}>
            <Select
              disabled={!!editing}
              allowClear
              placeholder={t("dataQuality.targetColumnPlaceholder")}
              options={
                dsSchema
                  .find((t) => t.tableName === watchedTargetTable)
                  ?.columns.map((c) => ({
                    value: c.columnName,
                    label: `${c.columnName} (${c.dataType})`,
                  })) ?? []
              }
            />
          </Form.Item>
          <Form.Item
            name="ruleType"
            label={t("dataQuality.ruleType")}
            rules={[{ required: true }]}
          >
            <Select
              disabled={!!editing}
              options={RULE_TYPES.map((rt) => ({
                label: t(`dataQuality.ruleTypeLabels.${rt}`),
                value: rt,
              }))}
            />
          </Form.Item>
          <Form.Item name="ruleExpression" label={t("dataQuality.ruleExpression")}>
            <Input.TextArea rows={2} />
          </Form.Item>
          <Form.Item name="threshold" label={t("dataQuality.threshold")}>
            <InputNumber min={0} max={100} step={0.01} />
          </Form.Item>
          <Form.Item name="severity" label={t("dataQuality.severity")}>
            <Select
              options={SEVERITIES.map((s) => ({
                label: t(`dataQuality.severityLabels.${s}`),
                value: s,
              }))}
            />
          </Form.Item>
          <Form.Item
            name="isEnabled"
            label={t("dataQuality.enabled")}
            valuePropName="checked"
          >
            <Switch />
          </Form.Item>
          <Form.Item name="owner" label={t("dataQuality.owner")}>
            <Input />
          </Form.Item>
          <Form.Item name="description" label={t("common.description")}>
            <Input.TextArea rows={2} />
          </Form.Item>
        </Form>
      </Modal>
    </>
  );
}

// ---------------------------------------------------------------------------
// Tab 1：质量评分
// ---------------------------------------------------------------------------

/** 批量评估历史列表（feat-eval-batch-result，2026-09-15）。
 * 每次 refreshTick 变化就重读 localStorage；点条目 → onOpen(record)。 */
function HistoryList({
  refreshTick,
  onOpen,
}: {
  refreshTick: number;
  onOpen: (record: BatchEvalRecord) => void;
}): JSX.Element {
  const { t } = useTranslation();
  const [records, setRecords] = useState<BatchEvalRecord[]>([]);

  useEffect(() => {
    setRecords(listBatchEvalHistory());
  }, [refreshTick]);

  if (records.length === 0) {
    return <Empty description={t("dataQuality.batchResult.history.empty")} />;
  }

  return (
    <List
      dataSource={records}
      renderItem={(item) => (
        <List.Item
          key={item.id}
          actions={[
            <Button
              key="open"
              type="link"
              size="small"
              onClick={() => onOpen(item)}
            >
              {t("dataQuality.batchResult.history.open")}
            </Button>,
          ]}
        >
          <List.Item.Meta
            title={
              <Space>
                <span>{new Date(item.evaluatedAt).toLocaleString()}</span>
                <Tag color="blue">{item.summary.total}</Tag>
                <Tag color="green">{item.summary.passed} PASS</Tag>
                {item.summary.failed > 0 && (
                  <Tag color="orange">{item.summary.failed} FAIL</Tag>
                )}
                {item.summary.errored > 0 && (
                  <Tag color="red">{item.summary.errored} ERROR</Tag>
                )}
              </Space>
            }
            description={`${item.results.length} ${t("dataQuality.batchResult.columns.ruleCode")}s`}
          />
        </List.Item>
      )}
    />
  );
}

function ScoresTab() {
  const { t } = useTranslation();
  // 走 AntdApp 上下文拿 message 实例（同 RulesTab 注释）
  const { message } = App.useApp();
  const { options: filterOptions } = useDataQualityFilterOptions();
  const [scores, setScores] = useState<DataQualityScore[]>([]);
  const [loading, setLoading] = useState(false);
  const [computeLoading, setComputeLoading] = useState(false);
  const [filterTable, setFilterTable] = useState<string | undefined>();
  const [filterType, setFilterType] = useState<ScoreType | undefined>();
  const [latestOnly, setLatestOnly] = useState(false);

  // feat-dq-scores-scope（2026-09-15）+ multiselect：计算评分前置 scope 过滤。
  // datasource_id 单选；target_tables / rule_types 多选（list）。
  // 全空 = 全量（向后兼容旧行为）。
  const [scopeDatasourceId, setScopeDatasourceId] = useState<number | undefined>();
  const [scopeTargetTables, setScopeTargetTables] = useState<string[]>([]);
  const [scopeRuleTypes, setScopeRuleTypes] = useState<RuleType[]>([]);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const filters: ScoreListParams = {
        table: filterTable,
        scoreType: filterType,
        latest: latestOnly,
        limit: 100,
      };
      const data = await listScores(filters);
      setScores(data);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      void message.error(msg);
    } finally {
      setLoading(false);
    }
  }, [filterTable, filterType, latestOnly]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const handleCompute = async () => {
    setComputeLoading(true);
    try {
      // scope 三字段全空 → payload 为空对象 → 后端走全量（向后兼容）
      const result = await computeScore({
        datasourceId: scopeDatasourceId,
        targetTables: scopeTargetTables.length > 0 ? scopeTargetTables : undefined,
        ruleTypes: scopeRuleTypes.length > 0 ? scopeRuleTypes : undefined,
      });
      void message.success(
        t("dataQuality.computeScoreSuccess", { count: result.savedScores }),
      );
      await refresh();
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      void message.error(msg);
    } finally {
      setComputeLoading(false);
    }
  };

  const dimKeys = [
    "completenessScore",
    "validityScore",
    "uniquenessScore",
    "consistencyScore",
    "timelinessScore",
    "referentialScore",
  ] as const;

  const scoreColumns: ColumnsType<DataQualityScore> = [
    {
      title: t("dataQuality.scores.targetTable"),
      dataIndex: "targetTable",
      key: "targetTable",
      width: 160,
    },
    {
      title: t("dataQuality.scores.scoreType"),
      dataIndex: "scoreType",
      key: "scoreType",
      width: 100,
      render: (v: ScoreType) => t(`dataQuality.scores.${v.toLowerCase()}`),
    },
    {
      title: t("dataQuality.scores.overallScore"),
      dataIndex: "overallScore",
      key: "overallScore",
      width: 100,
      render: (v: string) => (
        <Tag color={scoreColor(v)}>{parseFloat(v).toFixed(1)}</Tag>
      ),
    },
    ...dimKeys.map((k) => ({
      title: t(`dataQuality.scores.dimensions.${k.replace("Score", "")}`),
      dataIndex: k,
      key: k,
      width: 90,
      render: (v: string | null) =>
        v !== null ? parseFloat(v).toFixed(1) : t("common.dash"),
    })),
    {
      title: t("dataQuality.scores.rulesCount"),
      dataIndex: "rulesCount",
      key: "rulesCount",
      width: 90,
    },
    {
      title: t("dataQuality.scores.evaluatedAt"),
      dataIndex: "evaluatedAt",
      key: "evaluatedAt",
      width: 180,
      render: (v: string) => (v ? new Date(v).toLocaleString() : t("common.dash")),
    },
    {
      title: t("dataQuality.scores.duration"),
      dataIndex: "evaluationDurationMs",
      key: "evaluationDurationMs",
      width: 100,
      render: (v: number) => `${v} ms`,
    },
  ];

  return (
    <>
      <Space style={{ marginBottom: 16 }} wrap>
        <Input
          allowClear
          placeholder={t("dataQuality.scoresFilter.table")}
          style={{ width: 200 }}
          value={filterTable}
          onChange={(e) => setFilterTable(e.target.value || undefined)}
        />
        <Select
          allowClear
          placeholder={t("dataQuality.scoresFilter.scoreType")}
          style={{ width: 160 }}
          value={filterType}
          onChange={(v) => setFilterType(v as ScoreType | undefined)}
          options={SCORE_TYPES.map((st) => ({
            label: t(`dataQuality.scores.${st.toLowerCase()}`),
            value: st,
          }))}
        />
        {/* feat-dq-scores-scope：3 个 scope Select 控制 compute 范围。
            全 None = 全量（向后兼容）；全 allowClear。 */}
        <Select
          allowClear
          showSearch
          placeholder={t("dataQuality.scoresFilter.scopeDatasource")}
          style={{ width: 200 }}
          value={scopeDatasourceId}
          onChange={(v) =>
            setScopeDatasourceId(v === undefined ? undefined : Number(v))
          }
          options={filterOptions.datasourceIds.map((d) => ({
            label: d.name,
            value: d.id,
          }))}
        />
        <Select
          mode="multiple"
          allowClear
          showSearch
          maxTagCount="responsive"
          placeholder={t("dataQuality.scoresFilter.scopeTargetTable")}
          style={{ width: 240 }}
          value={scopeTargetTables}
          onChange={(v) => setScopeTargetTables(v as string[])}
          options={filterOptions.targetTables.map((t) => ({
            label: t,
            value: t,
          }))}
        />
        <Select
          mode="multiple"
          allowClear
          maxTagCount="responsive"
          placeholder={t("dataQuality.scoresFilter.scopeRuleType")}
          style={{ width: 220 }}
          value={scopeRuleTypes}
          onChange={(v) => setScopeRuleTypes(v as RuleType[])}
          options={RULE_TYPES.map((rt) => ({
            label: t(`dataQuality.ruleTypeLabels.${rt}`),
            value: rt,
          }))}
        />
        <Button
          type="primary"
          loading={computeLoading}
          onClick={() => void handleCompute()}
        >
          {t("dataQuality.computeScore")}
        </Button>
        <Button onClick={() => void refresh()}>{t("common.refresh")}</Button>
        <Select
          allowClear
          placeholder={t("dataQuality.scoresFilter.latestOnly")}
          style={{ width: 140 }}
          value={latestOnly ? "true" : undefined}
          onChange={(v) => setLatestOnly(v === "true")}
          options={[
            { label: t("dataQuality.scoresFilter.latestOnly"), value: "true" },
          ]}
        />
      </Space>

      <Table
        rowKey="id"
        loading={loading}
        columns={scoreColumns}
        dataSource={scores}
        pagination={{ pageSize: 20 }}
      />
    </>
  );
}

// ---------------------------------------------------------------------------
// 页面
// ---------------------------------------------------------------------------

export default function DataQualityPage() {
  const { t } = useTranslation();
  // 4 个 tab 用 ?tab= 同步到 URL，方便直接书签到「生成向导」「评估报告」。
  // 旧路由 /data-quality/generate 和 /data-quality/reports 由 App.tsx 重定向到 ?tab=。
  const [searchParams, setSearchParams] = useSearchParams();
  const tabFromUrl = searchParams.get("tab");
  const VALID_TABS = ["generate", "rules", "scores", "reports"] as const;
  const activeKey = (VALID_TABS as readonly string[]).includes(tabFromUrl ?? "")
    ? (tabFromUrl as (typeof VALID_TABS)[number])
    : "rules";
  const handleTabChange = useCallback(
    (key: string) => {
      const next = new URLSearchParams(searchParams);
      next.set("tab", key);
      setSearchParams(next, { replace: true });
    },
    [searchParams, setSearchParams],
  );

  const items = [
    {
      key: "generate",
      label: t("menu.item.dataQualityGenerate"),
      children: <DataQualityRuleGeneratePage />,
    },
    {
      key: "rules",
      label: t("dataQuality.tabs.rules"),
      children: <RulesTab />,
    },
    {
      key: "scores",
      label: t("dataQuality.tabs.scores"),
      children: <ScoresTab />,
    },
    {
      key: "reports",
      label: t("menu.item.dataQualityReport"),
      children: <DataQualityReportListPage />,
    },
  ];

  return (
    <Tabs
      activeKey={activeKey}
      items={items}
      onChange={handleTabChange}
      // 左 padding 24px 让 tab 距离侧边栏有一定呼吸空间（原 4px 太贴边）
      style={{ padding: "8px 24px" }}
    />
  );
}
