/** 场景高级参数面板（P4）：槽位表单按 slotSchema 动态生成。
 *
 * 从 ProcurementCopilotPage 抽出（P4 拆分）。渲染策略：
 * - **顺序与标签跟配置走**（slotSchema 数组序 = 表单序）；
 * - 控件按槽位 key 分派（supplierKeys/materialKeys 走远程实体搜索、
 *   siteKeys 走基地字典、monthsBack 等数字走 InputNumber）——
 *   key 是场景配置契约的一部分；未知 type **不渲染**（fail-closed）；
 * - 模型/数据源选择器不是槽位，固定在面板顶部（数据源仅 admin 可见）。
 */

import { useState } from "react";
import { Card, DatePicker, Input, InputNumber, Segmented, Select, Space, Typography } from "antd";
import dayjs from "dayjs";
import type { Dayjs } from "dayjs";
import { useTranslation } from "../../i18n";
import { searchMappings } from "../../api/entityMapping";
import type { CopilotScenarioSlot } from "../../api/copilotScenario";
import type { DataSource } from "../../types/datasource";
import type { ModelConfig } from "../../types/modelConfig";

const { Text } = Typography;
const { RangePicker } = DatePicker;

export type SlotValues = Record<string, unknown>;

const MONTHS_MIN = 1;
const MONTHS_MAX = 36;
const SEARCH_DEBOUNCE_MS = 300;

/** 时间窗/日期区间二选一：months = 近 N 月；range = 显式 dateFrom/dateTo。 */
export type DateMode = "months" | "range";

/** 是否改过高级参数（改了 ⇒ 不写字也能发送）。 */
export function hasAdvancedFilters(values: SlotValues): boolean {
  return Object.entries(values).some(([key, value]) => {
    if (key === "monthsBack") return Number(value) !== 12;
    if (key === "minOverdueDays") return Number(value) > 0;
    if (Array.isArray(value)) return value.length > 0;
    return value !== undefined && value !== null && value !== "";
  });
}

interface ScenarioAdvancedParamsProps {
  slots: CopilotScenarioSlot[];
  values: SlotValues;
  onChange: (key: string, value: unknown) => void;
  dateMode: DateMode;
  onDateModeChange: (mode: DateMode) => void;
  /** 模型与数据源（非槽位）：固定在面板顶部 */
  models: ModelConfig[];
  modelId: number | undefined;
  onModelChange: (id: number) => void;
  isAdmin: boolean;
  datasources: DataSource[];
  datasourceId: number | undefined;
  onDatasourceChange: (id: number) => void;
  /** 基地字典选项（页面按数据源拉取） */
  siteOptions: { code: string; name: string }[];
}

/** 槽位 key → 远程实体搜索类型（pending_delivery 场景契约）。 */
const ENTITY_SEARCH_BY_KEY: Record<string, "SUPPLIER" | "MATERIAL"> = {
  supplierKeys: "SUPPLIER",
  materialKeys: "MATERIAL",
};

export default function ScenarioAdvancedParams({
  slots,
  values,
  onChange,
  dateMode,
  onDateModeChange,
  models,
  modelId,
  onModelChange,
  isAdmin,
  datasources,
  datasourceId,
  onDatasourceChange,
  siteOptions,
}: ScenarioAdvancedParamsProps) {
  const { t } = useTranslation();
  const [supplierOptions, setSupplierOptions] = useState<{ code: string; name: string }[]>([]);
  const [materialOptions, setMaterialOptions] = useState<{ code: string; name: string }[]>([]);
  const [searchTimer, setSearchTimer] = useState<ReturnType<typeof setTimeout> | null>(null);

  /** 远程搜索防抖（Select onSearch 高频触发）。 */
  const remoteSearch = (
    entityType: "SUPPLIER" | "MATERIAL",
    q: string,
    setter: (rows: { code: string; name: string }[]) => void,
  ) => {
    if (searchTimer) clearTimeout(searchTimer);
    const timer = setTimeout(() => {
      void searchMappings(q, { entityType, limit: 20 })
        .then((rows) =>
          setter(
            rows.map((row) => ({
              code: row.enterpriseCode,
              name: row.name ?? row.enterpriseCode,
            })),
          ),
        )
        .catch(() => undefined);
    }, SEARCH_DEBOUNCE_MS);
    setSearchTimer(timer);
  };

  const label = (slot: CopilotScenarioSlot) => (
    <div style={{ fontSize: 12, color: "#5a6b87", marginBottom: 4 }}>{slot.label}</div>
  );

  const renderSlot = (slot: CopilotScenarioSlot) => {
    // 隐藏槽位：表单不渲染（dateFrom/dateTo 由时间窗的区间控件写入、
    // datasourceId 由数据源下拉写入）；后端渲染不受影响
    if (slot.hidden) return null;
    if (slot.key === "monthsBack" && slot.type === "number") {
      return (
        <Space key={slot.key} wrap size={8}>
          {label(slot)}
          <Segmented
            value={dateMode}
            onChange={(value) => onDateModeChange(value as DateMode)}
            options={[
              { label: t("procurement.copilot.timeMonths"), value: "months" },
              { label: t("procurement.copilot.timeRange"), value: "range" },
            ]}
          />
          {dateMode === "months" ? (
            <>
              <InputNumber
                min={MONTHS_MIN}
                max={MONTHS_MAX}
                value={Number(values[slot.key] ?? 12)}
                onChange={(v) => onChange(slot.key, Number(v) || 12)}
              />
              <span style={{ fontSize: 12, color: "#5a6b87" }}>
                {t("procurement.copilot.timeMonthsUnit")}
              </span>
            </>
          ) : (
            <DateRangeSlot values={values} onChange={onChange} />
          )}
        </Space>
      );
    }
    if (slot.type === "number") {
      return (
        <Space key={slot.key} wrap size={8}>
          {label(slot)}
          <InputNumber
            min={0}
            value={Number(values[slot.key] ?? 0)}
            onChange={(v) => onChange(slot.key, Number(v) || 0)}
          />
        </Space>
      );
    }
    if (slot.type === "text") {
      return (
        <Space key={slot.key} wrap size={8}>
          {label(slot)}
          <Input
            style={{ width: 140 }}
            value={String(values[slot.key] ?? "")}
            onChange={(e) => onChange(slot.key, e.target.value || undefined)}
          />
        </Space>
      );
    }
    if (slot.type === "entityMulti") {
      const entityType = ENTITY_SEARCH_BY_KEY[slot.key];
      const options =
        slot.key === "supplierKeys" ? supplierOptions
        : slot.key === "materialKeys" ? materialOptions
        : [];
      const setter =
        slot.key === "supplierKeys" ? setSupplierOptions
        : slot.key === "materialKeys" ? setMaterialOptions
        : undefined;
      return (
        <Space key={slot.key} wrap size={8}>
          {label(slot)}
          <Select
            mode="multiple"
            showSearch
            allowClear
            maxTagCount={1}
            style={{ minWidth: 200, maxWidth: 320 }}
            placeholder={slot.label}
            value={Array.isArray(values[slot.key]) ? (values[slot.key] as string[]) : []}
            onSearch={entityType && setter ? (q) => remoteSearch(entityType, q, setter) : undefined}
            onChange={(next: string[]) => onChange(slot.key, next)}
            options={options.map((item) => ({
              value: item.code,
              label: `${item.name}（${item.code}）`,
            }))}
            filterOption={false}
          />
        </Space>
      );
    }
    if (slot.type === "dictMulti") {
      return (
        <Space key={slot.key} wrap size={8}>
          {label(slot)}
          <Select
            mode="multiple"
            showSearch
            allowClear
            maxTagCount={2}
            style={{ minWidth: 220, maxWidth: 340 }}
            placeholder={slot.label}
            value={Array.isArray(values[slot.key]) ? (values[slot.key] as string[]) : []}
            onChange={(next: string[]) => onChange(slot.key, next)}
            optionFilterProp="label"
            options={siteOptions.map((item) => ({
              value: item.code,
              label: `${item.name}（${item.code}）`,
            }))}
          />
        </Space>
      );
    }
    // 未知 type：fail-closed，不渲染（保存时白名单已拦截，这里双保险）
    return null;
  };

  const hasDatePair =
    slots.some((s) => s.key === "monthsBack") &&
    slots.some((s) => s.key === "dateFrom");

  return (
    <Card
      size="small"
      style={{
        marginBottom: 16,
        border: "1.5px solid #91caff",
        background: "#f7faff",
        borderRadius: 8,
      }}
      title={
        <Text strong style={{ color: "#1677ff" }}>
          {t("procurement.copilot.advancedParams")}
        </Text>
      }
    >
      <Space wrap size={[16, 8]} align="center">
        <div style={{ fontSize: 12, color: "#5a6b87", marginBottom: 4 }}>
          {t("procurement.copilot.model")}
        </div>
        <Select
          style={{ minWidth: 160 }}
          placeholder={t("procurement.copilot.model")}
          value={modelId}
          onChange={onModelChange}
          options={models.map((model) => ({
            value: model.id,
            label: model.modelName,
          }))}
        />
        {/* 数据源切换 = 治理动作（可指向别的业务库）⇒ 仅 admin 可见 */}
        {isAdmin && (
          <>
            <div style={{ fontSize: 12, color: "#5a6b87", marginBottom: 4 }}>
              {t("procurement.copilot.slotDatasource")}
            </div>
            <Select
              style={{ minWidth: 150 }}
              placeholder={t("procurement.copilot.slotDatasource")}
              value={datasourceId}
              onChange={onDatasourceChange}
              options={datasources.map((d) => ({
                value: d.id,
                label: d.name,
              }))}
            />
          </>
        )}
        {slots.map(renderSlot)}
        {hasDatePair && dateMode === "range" && !slots.some((s) => s.key === "monthsBack") && (
          <DateRangeSlot values={values} onChange={onChange} />
        )}
      </Space>
    </Card>
  );
}

/** 日期区间控件：写入 dateFrom/dateTo 两个文本槽位（YYYY-MM-DD）。 */
function DateRangeSlot({
  values,
  onChange,
}: {
  values: SlotValues;
  onChange: (key: string, value: unknown) => void;
}) {
  const from = typeof values.dateFrom === "string" ? values.dateFrom : undefined;
  const to = typeof values.dateTo === "string" ? values.dateTo : undefined;
  const value: [Dayjs, Dayjs] | null = from && to ? [dayjs(from), dayjs(to)] : null;
  return (
    <RangePicker
      size="middle"
      value={value}
      onChange={(dates) => {
        if (dates && dates[0] && dates[1]) {
          onChange("dateFrom", dates[0].format("YYYY-MM-DD"));
          onChange("dateTo", dates[1].format("YYYY-MM-DD"));
        } else {
          onChange("dateFrom", undefined);
          onChange("dateTo", undefined);
        }
      }}
    />
  );
}
