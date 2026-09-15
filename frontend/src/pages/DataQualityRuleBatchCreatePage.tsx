/** 批量新建规则页面（feat-rule-batch-create，2026-09-15）
 *
 * 三步骤布局：基础配置 → 列与规则 → 预览确认。
 * - 步骤 1：选数据源 + 本体类（自动填 source_table）+ 表（fuzzy 搜索）
 * - 步骤 2：列清单 + 规则类型 + 自动表达式
 * - 步骤 3：预览 N 条规则 + 全局默认值 + 批量保存
 */

import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  Alert,
  Button,
  Card,
  Result,
  Space,
  Spin,
  Steps,
  Tag,
  Typography,
  message,
} from "antd";
import {
  CheckCircleOutlined,
  ExclamationCircleOutlined,
  ReloadOutlined,
} from "@ant-design/icons";
import { useNavigate } from "react-router-dom";

import { listClasses, listPropertiesByClass } from "../api/ontology";
import {
  getDatasourceSchema,
  introspectDatasource,
  listDataSources,
} from "../api/datasource";
import { createRule, listRuleOptions } from "../api/dataQuality";
import { fetchNextRuleCode } from "../api/dataQualityRuleNextCode";

import { RuleBatchStepBasic } from "../components/dq/RuleBatchStepBasic";
import { RuleBatchStepColumns } from "../components/dq/RuleBatchStepColumns";
import { RuleBatchStepPreview } from "../components/dq/RuleBatchStepPreview";

import { findDuplicateNameIndices } from "../utils/ruleNameDedup";
import type { ColumnWithRule } from "../components/dq/RuleBatchStepColumns";

import type { DataSource } from "../types/datasource";
import type { OntologyClass } from "../types/ontology";
import type { RuleType, Severity } from "../types/dataQuality";

const { Title } = Typography;

export interface BatchDraftRule {
  /** 行 key（用列名 + 类型 + 索引稳定） */
  key: string;
  ruleCode: string;
  ruleName: string;
  ruleType: string;
  ruleExpression: string | null;
  targetColumn: string;
  targetTable: string;
  dataType: string;
  templateId: string | null;
  isAutoExpression: boolean;
  isAutoName: boolean;
  /** 写入时填 */
  threshold: string;
  severity: string;
  isEnabled: boolean;
  owner: string;
  description: string;
  /** 失败标记 */
  saveError?: string;
}

export default function DataQualityRuleBatchCreatePage() {
  const { t } = useTranslation();
  const navigate = useNavigate();

  // ===== 数据加载 =====
  const [datasources, setDatasources] = useState<DataSource[]>([]);
  const [classes, setClasses] = useState<OntologyClass[]>([]);
  const [loadingBoot, setLoadingBoot] = useState(true);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [ds, cls, _opts] = await Promise.all([
          listDataSources(false),
          listClasses({ includeExpired: false }),
          listRuleOptions(),
        ]);
        if (cancelled) return;
        setDatasources(ds);
        setClasses(cls);
      } catch (e: unknown) {
        if (!cancelled) {
          const msg = e instanceof Error ? e.message : String(e);
          message.error(`${t("dataQuality.batchCreate.saveFailed")}: ${msg}`);
        }
      } finally {
        if (!cancelled) setLoadingBoot(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [t]);

  // ===== 步骤 1 状态 =====
  const [datasourceId, setDatasourceId] = useState<number | null>(null);
  const [classId, setClassId] = useState<number | null>(null);
  const [className, setClassName] = useState<string>("");
  const [targetTable, setTargetTable] = useState<string>("");
  const [tableQuery, setTableQuery] = useState("");
  const [schema, setSchema] = useState<{ tableName: string; columns: { columnName: string; dataType: string; isPrimaryKey?: boolean }[] }[]>([]);
  const [loadingSchema, setLoadingSchema] = useState(false);

  // 选类 → 自动填 source_table
  useEffect(() => {
    if (classId == null) return;
    const cls = classes.find((c) => c.id === classId);
    if (cls?.sourceTable) setTargetTable(cls.sourceTable);
    setClassName(cls?.className ?? "");
  }, [classId, classes]);

  // 选数据源 → 加载 schema
  useEffect(() => {
    if (datasourceId == null) {
      setSchema([]);
      return;
    }
    let cancelled = false;
    (async () => {
      setLoadingSchema(true);
      try {
        let resp;
        try {
          resp = await getDatasourceSchema(datasourceId);
        } catch {
          resp = await introspectDatasource(datasourceId);
        }
        if (cancelled) return;
        setSchema(resp.tables);
      } catch (e: unknown) {
        if (!cancelled) {
          const msg = e instanceof Error ? e.message : String(e);
          message.error(msg);
        }
      } finally {
        if (!cancelled) setLoadingSchema(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [datasourceId]);

  // 选类 → 加载 properties（用于表达式模板的 min_value / ref 推断）
  const [properties, setProperties] = useState<{ sourceColumn: string; isForeignKey?: boolean; refClassId?: number; minValue?: string | number | null; maxValue?: string | number | null }[]>([]);
  useEffect(() => {
    if (classId == null) {
      setProperties([]);
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const props = await listPropertiesByClass(classId);
        if (!cancelled) {
          setProperties(
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
        if (!cancelled) setProperties([]);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [classId]);

  // 选类 → 异步拿 next-code
  const [codeSuggestion, setCodeSuggestion] = useState<string>("");
  useEffect(() => {
    if (!className) {
      setCodeSuggestion("");
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const r = await fetchNextRuleCode({ className });
        if (!cancelled) setCodeSuggestion(r.code);
      } catch {
        if (!cancelled) setCodeSuggestion("");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [className]);

  const filteredTables = useMemo(() => {
    const q = tableQuery.trim().toLowerCase();
    if (!q) return schema;
    return schema.filter((t) => t.tableName.toLowerCase().includes(q));
  }, [schema, tableQuery]);

  const currentTable = useMemo(
    () => schema.find((t) => t.tableName === targetTable) ?? null,
    [schema, targetTable],
  );

  // ===== 步骤 2 状态 =====
  const [columns, setColumns] = useState<ColumnWithRule[]>([]);

  // 切换表 → 重置列
  useEffect(() => {
    if (!currentTable) {
      setColumns([]);
      return;
    }
    setColumns(
      currentTable.columns.map((c) => ({
        columnName: c.columnName,
        dataType: c.dataType,
        isPrimaryKey: !!c.isPrimaryKey,
        selected: false,
        ruleType: "VALIDITY",
        templateId: null,
        expression: null,
        isAutoExpression: false,
        noTemplate: false,
      })),
    );
  }, [currentTable]);

  // ===== 步骤 3 状态（draft 规则）=====
  const [drafts, setDrafts] = useState<BatchDraftRule[]>([]);
  const [globalOwner, setGlobalOwner] = useState("");
  const [globalDescription, setGlobalDescription] = useState("");
  const [globalSeverity, setGlobalSeverity] = useState("MEDIUM");
  const [globalThreshold, setGlobalThreshold] = useState("100.00");
  const [globalEnabled, setGlobalEnabled] = useState(true);

  // ===== 步骤指示 =====
  const [step, setStep] = useState<0 | 1 | 2>(0);

  // ===== 保存 =====
  const [saving, setSaving] = useState(false);
  const [saveResult, setSaveResult] = useState<{
    success: number;
    failed: number;
  } | null>(null);

  // 当前选的列 + 规则类型 → 生成 drafts
  useEffect(() => {
    if (step !== 2 || !className || !targetTable) return;
    const propertyByCol = new Map(properties.map((p) => [p.sourceColumn, p]));
    // 同列同一规则类型计数（保证同一列同一规则类型的 rule_code 流水递增）
    let seq = 0;
    const newDrafts: BatchDraftRule[] = [];
    columns.forEach((col, idx) => {
      if (!col.selected) return;
      seq += 1;
      const prop = propertyByCol.get(col.columnName);
      // 找 ref 表/列
      const refProp = prop?.refClassId
        ? properties.find((p2) => p2.refClassId === prop.refClassId)
        : null;
      const refSourceTable = refProp?.sourceColumn
        ? classes.find((c) => c.id === prop?.refClassId)?.sourceTable ?? ""
        : "";
      const refSourceColumn = refProp?.sourceColumn ?? "";

      const newDraft: BatchDraftRule = {
        key: `${col.columnName}-${col.ruleType}-${idx}`,
        ruleCode: codeSuggestion
          ? `${codeSuggestion.replace(/-00001$/, "")}-${String(seq).padStart(5, "0")}`
          : "",
        ruleName: `${className}-${col.columnName}-${t(
          `dataQuality.ruleTypeLabels.${col.ruleType}`,
          { defaultValue: col.ruleType },
        )}`,
        ruleType: col.ruleType,
        ruleExpression: col.expression,
        targetColumn: col.columnName,
        targetTable,
        dataType: col.dataType,
        templateId: col.templateId,
        isAutoExpression: col.isAutoExpression,
        isAutoName: true,
        threshold: globalThreshold,
        severity: globalSeverity,
        isEnabled: globalEnabled,
        owner: globalOwner,
        description: globalDescription,
      };
      // refTable 信息携带（REFERENTIAL 类型）
      if (col.ruleType === "REFERENTIAL" && prop?.isForeignKey) {
        newDraft.ruleExpression = `REF ${refSourceTable}.${refSourceColumn}`;
      }
      newDrafts.push(newDraft);
    });
    setDrafts(newDrafts);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    step,
    columns,
    className,
    targetTable,
    codeSuggestion,
    globalThreshold,
    globalSeverity,
    globalEnabled,
    globalOwner,
    globalDescription,
    properties,
    classes,
    t,
  ]);

  const dupIndices = useMemo(() => findDuplicateNameIndices(drafts), [drafts]);

  if (loadingBoot) {
    return (
      <div style={{ padding: 24 }}>
        <Spin tip="Loading..." />
      </div>
    );
  }

  async function handleSave(): Promise<void> {
    if (!datasourceId || drafts.length === 0) return;
    if (!globalOwner.trim()) {
      message.warning(t("dataQuality.batchCreate.ownerRequired"));
      return;
    }
    setSaving(true);
    setSaveResult(null);
    const settled = await Promise.allSettled(
      drafts.map((d) =>
        createRule({
          ruleCode: d.ruleCode,
          ruleName: d.ruleName,
          datasourceId,
          targetTable: d.targetTable,
          targetColumn: d.targetColumn,
          ruleType: d.ruleType as RuleType,
          ruleExpression: d.ruleExpression,
          threshold: d.threshold,
          severity: d.severity as Severity,
          isEnabled: d.isEnabled,
          owner: d.owner,
          description: d.description,
        }),
      ),
    );
    const failed = settled.filter((s) => s.status === "rejected");
    const ok = settled.length - failed.length;

    if (failed.length > 0) {
      const failedIndices = new Set<number>();
      settled.forEach((s, i) => {
        if (s.status === "rejected") {
          failedIndices.add(i);
          const reason =
            s.reason instanceof Error
              ? s.reason.message
              : String(s.reason);
          setDrafts((prev) =>
            prev.map((d, j) =>
              j === i ? { ...d, saveError: reason } : d,
            ),
          );
        }
      });
      if (ok > 0) {
        message.warning(
          t("dataQuality.batchCreate.savePartial", {
            ok,
            failed: failed.length,
          }),
        );
      } else {
        message.error(t("dataQuality.batchCreate.saveFailed"));
      }
      setSaveResult({ success: ok, failed: failed.length });
    } else {
      message.success(
        t("dataQuality.batchCreate.saveSuccess", { count: ok }),
      );
      setSaveResult({ success: ok, failed: 0 });
    }
    setSaving(false);
  }

  // 完成后跳回
  if (saveResult && saveResult.failed === 0 && saveResult.success > 0) {
    return (
      <div style={{ padding: 24 }}>
        <Result
          status="success"
          title={t("dataQuality.batchCreate.saveSuccess", {
            count: saveResult.success,
          })}
          icon={<CheckCircleOutlined />}
          extra={[
            <Button
              key="back"
              type="primary"
              onClick={() => navigate("/data-quality?tab=rules")}
            >
              {t("common.back", { defaultValue: "Back" })}
            </Button>,
          ]}
        />
      </div>
    );
  }

  return (
    <div style={{ padding: 24, maxWidth: 1200, margin: "0 auto" }}>
      <Space direction="vertical" size={16} style={{ width: "100%" }}>
        <div>
          <Title level={3} style={{ margin: 0 }}>
            {t("dataQuality.batchCreate.pageTitle")}
          </Title>
          <Typography.Text type="secondary">
            {t("dataQuality.batchCreate.pageSubtitle")}
          </Typography.Text>
        </div>

        <Steps
          current={step}
          items={[
            {
              title: t("dataQuality.batchCreate.step1"),
            },
            {
              title: t("dataQuality.batchCreate.step2"),
            },
            {
              title: t("dataQuality.batchCreate.step3"),
            },
          ]}
        />

        {step === 0 && (
          <Card>
            <RuleBatchStepBasic
              datasources={datasources}
              classes={classes}
              datasourceId={datasourceId}
              classId={classId}
              targetTable={targetTable}
              tableQuery={tableQuery}
              filteredTables={filteredTables}
              loadingSchema={loadingSchema}
              schemaCount={schema.length}
              onChangeDatasource={setDatasourceId}
              onChangeClass={setClassId}
              onChangeTargetTable={setTargetTable}
              onChangeTableQuery={setTableQuery}
            />
            <div style={{ marginTop: 16, textAlign: "right" }}>
              <Button
                type="primary"
                disabled={
                  datasourceId == null ||
                  classId == null ||
                  !targetTable ||
                  !currentTable
                }
                onClick={() => setStep(1)}
              >
                {t("dataQuality.batchCreate.goStep2")}
              </Button>
            </div>
          </Card>
        )}

        {step === 1 && currentTable && (
          <Card>
            <RuleBatchStepColumns
              columns={columns}
              onChangeColumns={setColumns}
              tableName={currentTable.tableName}
            />
            <div
              style={{
                marginTop: 16,
                display: "flex",
                justifyContent: "space-between",
              }}
            >
              <Button onClick={() => setStep(0)}>
                {t("dataQuality.batchCreate.back")}
              </Button>
              <Button
                type="primary"
                disabled={!columns.some((c) => c.selected)}
                onClick={() => setStep(2)}
              >
                {t("dataQuality.batchCreate.goStep3")}
              </Button>
            </div>
          </Card>
        )}

        {step === 2 && (
          <Card>
            <RuleBatchStepPreview
              drafts={drafts}
              duplicateIndices={dupIndices}
              className={className}
              targetTable={targetTable}
              globalOwner={globalOwner}
              globalDescription={globalDescription}
              globalSeverity={globalSeverity}
              globalThreshold={globalThreshold}
              globalEnabled={globalEnabled}
              onChangeGlobalOwner={setGlobalOwner}
              onChangeGlobalDescription={setGlobalDescription}
              onChangeGlobalSeverity={setGlobalSeverity}
              onChangeGlobalThreshold={setGlobalThreshold}
              onChangeGlobalEnabled={setGlobalEnabled}
              onChangeDrafts={setDrafts}
            />
            {drafts.length > 0 && dupIndices.length > 0 && (
              <Alert
                type="warning"
                showIcon
                icon={<ExclamationCircleOutlined />}
                style={{ marginTop: 16 }}
                message={t("dataQuality.batchCreate.duplicateName")}
                description={
                  <span>
                    {dupIndices.length} 行规则名冲突，请改名后再保存
                  </span>
                }
              />
            )}
            <div
              style={{
                marginTop: 16,
                display: "flex",
                justifyContent: "space-between",
              }}
            >
              <Space>
                <Button onClick={() => setStep(1)}>
                  {t("dataQuality.batchCreate.back")}
                </Button>
                <Button
                  icon={<ReloadOutlined />}
                  onClick={() => setSaveResult(null)}
                >
                  {t("common.refresh", { defaultValue: "Refresh" })}
                </Button>
              </Space>
              <Button
                type="primary"
                loading={saving}
                disabled={
                  drafts.length === 0 ||
                  dupIndices.length > 0 ||
                  !globalOwner.trim()
                }
                onClick={handleSave}
              >
                {saving
                  ? t("dataQuality.batchCreate.saving")
                  : t("dataQuality.batchCreate.save", {
                      count: drafts.length,
                    })}
              </Button>
            </div>
            {saveResult && saveResult.failed > 0 && (
              <Alert
                type="error"
                style={{ marginTop: 16 }}
                showIcon
                message={t("dataQuality.batchCreate.savePartial", {
                  ok: saveResult.success,
                  failed: saveResult.failed,
                })}
              />
            )}
          </Card>
        )}
      </Space>
      <Tag style={{ display: "none" }}>feat-rule-batch-create</Tag>
    </div>
  );
}