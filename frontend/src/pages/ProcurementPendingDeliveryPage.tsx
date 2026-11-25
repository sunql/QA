/** 待到货欠交明细页（常驻数据底座页）。
 *
 * 与 Copilot 场景的关系：场景回答「欠交概况 + 建议」，本页回答「逐行明细」——
 * 两者同口径（同一 SQL builder），可互相跳转。
 *
 * 布局参照参考系统（质检缺陷记录页）：顶部筛选卡 + 下方表格 + 服务端分页。
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Button,
  Card,
  DatePicker,
  InputNumber,
  Select,
  Space,
  Table,
  Tag,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import type { Dayjs } from "dayjs";
import { ReloadOutlined, SearchOutlined } from "@ant-design/icons";
import { useTranslation } from "../i18n";
import { useTablePagination } from "../utils/useTablePagination";
import { listPendingDeliverySites } from "../api/procurementCopilot";
import { searchMappings } from "../api/entityMapping";
import {
  listPendingDeliveryLines,
  type PendingDeliveryLine,
} from "../api/pendingDelivery";

const { RangePicker } = DatePicker;
const { Text } = Typography;

interface Option {
  value: string;
  label: string;
}

const EMPTY_OPTIONS: Option[] = [];

function fmtQty(value: number | undefined): string {
  if (value === undefined || value === null) return "-";
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 3 }).format(value);
}

export default function ProcurementPendingDeliveryPage() {
  const { t } = useTranslation();

  const [monthsBack, setMonthsBack] = useState(12);
  const [dateRange, setDateRange] = useState<[Dayjs, Dayjs] | null>(null);
  const [supplierKeys, setSupplierKeys] = useState<string[]>([]);
  const [materialKeys, setMaterialKeys] = useState<string[]>([]);
  const [siteKeys, setSiteKeys] = useState<string[]>([]);
  const [minOverdueDays, setMinOverdueDays] = useState(0);
  const [supplierOptions, setSupplierOptions] = useState<Option[]>(EMPTY_OPTIONS);
  const [materialOptions, setMaterialOptions] = useState<Option[]>(EMPTY_OPTIONS);
  const [siteOptions, setSiteOptions] = useState<Option[]>(EMPTY_OPTIONS);
  const [rows, setRows] = useState<PendingDeliveryLine[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);

  const { pagination, setPage, state } = useTablePagination({ serverTotal: total });

  useEffect(() => {
    listPendingDeliverySites()
      .then((list) =>
        setSiteOptions(
          list.map((item) => ({ value: item.code, label: `${item.name}（${item.code}）` })),
        ),
      )
      .catch(() => setSiteOptions(EMPTY_OPTIONS));
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const page = await listPendingDeliveryLines({
        monthsBack,
        ...(dateRange
          ? {
              dateFrom: dateRange[0].format("YYYY-MM-DD"),
              dateTo: dateRange[1].format("YYYY-MM-DD"),
            }
          : {}),
        supplierKeys,
        materialKeys,
        siteKeys,
        minOverdueDays,
        offset: (state.current - 1) * state.pageSize,
        limit: state.pageSize,
      });
      setRows(page.rows);
      setTotal(page.total);
    } catch {
      /* 拦截器已提示 */
    } finally {
      setLoading(false);
    }
  }, [
    monthsBack, dateRange, supplierKeys, materialKeys, siteKeys,
    minOverdueDays, state.current, state.pageSize,
  ]);

  useEffect(() => {
    void load();
  }, [load]);

  const reset = useCallback(() => {
    setMonthsBack(12);
    setDateRange(null);
    setSupplierKeys([]);
    setMaterialKeys([]);
    setSiteKeys([]);
    setMinOverdueDays(0);
    setPage(1);
  }, [setPage]);

  const remoteSearch = useCallback(
    (
      entityType: "SUPPLIER" | "MATERIAL",
      q: string,
      setter: (options: Option[]) => void,
    ) => {
      if (!q.trim()) return;
      searchMappings(q, { entityType, limit: 20 })
        .then((hits) =>
          setter(
            hits.map((hit) => ({
              value: hit.enterpriseCode,
              label: `${hit.name ?? hit.enterpriseCode}（${hit.enterpriseCode}）`,
            })),
          ),
        )
        .catch(() => undefined);
    },
    [],
  );

  const columns: ColumnsType<PendingDeliveryLine> = useMemo(
    () => [
      { title: t("pendingDelivery.columns.poNo"), dataIndex: "poNo", width: 170 },
      {
        title: t("pendingDelivery.columns.material"),
        dataIndex: "materialDesc",
        render: (value: string | null, row) => (
          <Space direction="vertical" size={0}>
            <Text>{value || row.materialCode}</Text>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {row.materialCode}
            </Text>
          </Space>
        ),
      },
      {
        title: t("pendingDelivery.columns.supplier"),
        render: (_: unknown, row) => (
          <Space direction="vertical" size={0}>
            <Text>{row.supplierName || row.supplierCode}</Text>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {row.supplierCode}
            </Text>
          </Space>
        ),
      },
      {
        title: t("pendingDelivery.columns.openQty"),
        dataIndex: "openQty",
        align: "right",
        render: (value: number) => fmtQty(value),
      },
      {
        title: t("pendingDelivery.columns.overdueDays"),
        dataIndex: "overdueDays",
        align: "right",
        render: (value: number) =>
          value > 90 ? <Tag color="red">{value}</Tag> : value,
      },
      {
        title: t("pendingDelivery.columns.expectedDate"),
        dataIndex: "expectedDate",
        render: (value: string | null) => (value ? value.slice(0, 10) : "-"),
      },
    ],
    [t],
  );

  return (
    <div style={{ padding: 24 }}>
      <Space align="center" style={{ marginBottom: 16 }}>
        <Text strong style={{ fontSize: 18 }}>
          {t("pendingDelivery.title")}
        </Text>
        <Text type="secondary">{t("pendingDelivery.subtitle")}</Text>
      </Space>

      <Card size="small" style={{ marginBottom: 16 }}>
        <Space wrap size={12} align="center">
          <Text type="secondary">{t("pendingDelivery.slotTime")}</Text>
          <RangePicker
            value={dateRange}
            onChange={(value) => {
              setDateRange(value && value[0] && value[1] ? [value[0], value[1]] : null);
              setPage(1);
            }}
          />
          {!dateRange && (
            <>
              <InputNumber
                min={1}
                max={36}
                value={monthsBack}
                onChange={(value) => {
                  setMonthsBack(Number(value) || 12);
                  setPage(1);
                }}
              />
              <Text type="secondary">{t("pendingDelivery.timeMonthsUnit")}</Text>
            </>
          )}
          <Text type="secondary">{t("pendingDelivery.slotSupplier")}</Text>
          <Select
            mode="multiple"
            showSearch
            allowClear
            maxTagCount={1}
            aria-label={t("pendingDelivery.searchSupplier")}
            style={{ minWidth: 200, maxWidth: 320 }}
            placeholder={t("pendingDelivery.searchSupplier")}
            value={supplierKeys}
            onSearch={(q) => remoteSearch("SUPPLIER", q, setSupplierOptions)}
            onChange={(value) => {
              setSupplierKeys(value);
              setPage(1);
            }}
            options={supplierOptions}
            filterOption={false}
          />
          <Text type="secondary">{t("pendingDelivery.slotMaterial")}</Text>
          <Select
            mode="multiple"
            showSearch
            allowClear
            maxTagCount={1}
            aria-label={t("pendingDelivery.searchMaterial")}
            style={{ minWidth: 200, maxWidth: 320 }}
            placeholder={t("pendingDelivery.searchMaterial")}
            value={materialKeys}
            onSearch={(q) => remoteSearch("MATERIAL", q, setMaterialOptions)}
            onChange={(value) => {
              setMaterialKeys(value);
              setPage(1);
            }}
            options={materialOptions}
            filterOption={false}
          />
          <Text type="secondary">{t("pendingDelivery.slotSite")}</Text>
          <Select
            mode="multiple"
            allowClear
            maxTagCount={2}
            aria-label={t("pendingDelivery.searchSite")}
            style={{ minWidth: 200, maxWidth: 340 }}
            placeholder={t("pendingDelivery.searchSite")}
            value={siteKeys}
            onChange={(value) => {
              setSiteKeys(value);
              setPage(1);
            }}
            options={siteOptions}
            optionFilterProp="label"
          />
          <Text type="secondary">{t("pendingDelivery.slotOverdue")}</Text>
          <InputNumber
            min={0}
            value={minOverdueDays}
            onChange={(value) => {
              setMinOverdueDays(Number(value) || 0);
              setPage(1);
            }}
          />
          <Button
            type="primary"
            icon={<SearchOutlined />}
            loading={loading}
            onClick={() => {
              setPage(1);
              void load();
            }}
          >
            {t("pendingDelivery.query")}
          </Button>
          <Button icon={<ReloadOutlined />} onClick={reset}>
            {t("pendingDelivery.reset")}
          </Button>
        </Space>
      </Card>

      <Space align="center" style={{ marginBottom: 8 }}>
        <Text strong>{t("pendingDelivery.listTitle")}</Text>
        <Text type="secondary">{t("common.totalItems", { total })}</Text>
        <Text type="secondary" style={{ fontSize: 12 }}>
          {t("pendingDelivery.sortHint")}
        </Text>
      </Space>

      <Table
        className="pd-table"
        rowKey={(row) => `${row.poNo}/${row.poLine}`}
        loading={loading}
        dataSource={rows}
        columns={columns}
        pagination={pagination}
      />
      <Text type="secondary" style={{ fontSize: 12 }}>
        {t("pendingDelivery.footerNote")}
      </Text>
    </div>
  );
}
