/** M4-2 绑定向导（admin 管理台）：源绑定/属性绑定的创建 → 确认 → 失效。
 *
 * 三个诉求（消费 M1-1.1 的 10 条路由）：
 * 1. **列表**：候选/已确认/失效三态过滤 + 属性绑定数；
 * 2. **向导**（Steps 3 步）：选类与源 → 表/角色/过滤 → 属性映射 ⇒ 创建；
 * 3. **确认/失效**：**确认 = 版本冻结点**（Popconfirm 警示文案）——冻结后改绑
 *    必须先失效再建新版本（后端语义，前端入口警示）。
 *
 * 口径：绑定管理是 **admin 面**（后端 requireAdmin）——页面不判角色，
 * 非 admin 调用得到 403（列表路由语义）。
 */

import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  Button,
  Drawer,
  Form,
  Input,
  message,
  Modal,
  Popconfirm,
  Select,
  Space,
  Steps,
  Table,
  Tag,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useTranslation } from "../i18n";
import {
  confirmPropertyBinding,
  confirmSourceBinding,
  createPropertyBinding,
  createSourceBinding,
  expireSourceBinding,
  getSourceBinding,
  listSourceBindings,
  type BindingRole,
  type PropertyBinding,
  type SourceBinding,
  type SourceBindingDetail,
} from "../api/procurementBindings";
import { listClasses } from "../api/ontology";
import { listDataSources } from "../api/datasource";
import { listPropertiesByClass } from "../api/ontology";

const ROLE_COLOR: Record<string, string> = {
  BASE: "blue",
  NUMERATOR: "green",
  DENOMINATOR: "orange",
  FILTER: "purple",
};

interface WizardState {
  step: number;
  classId: number | null;
  datasourceId: number | null;
  schemaName: string | null;
  sourceTable: string | null;
  bindingRole: BindingRole | null;
  filterExpression: string | null;
  columnByProperty: Record<number, string>;
  createdBindingId: number | null;
}

const WIZARD_INITIAL: WizardState = {
  step: 0,
  classId: null,
  datasourceId: null,
  schemaName: null,
  sourceTable: null,
  bindingRole: "BASE",
  filterExpression: null,
  columnByProperty: {},
  createdBindingId: null,
};

export default function ProcurementBindingsPage() {
  const { t } = useTranslation();
  const [bindings, setBindings] = useState<SourceBinding[]>([]);
  const [loading, setLoading] = useState(false);
  const [detail, setDetail] = useState<SourceBindingDetail | null>(null);
  const [wizardOpen, setWizardOpen] = useState(false);
  const [wizard, setWizard] = useState<WizardState>({ ...WIZARD_INITIAL });
  const [classes, setClasses] = useState<{ id: number; className: string }[]>([]);
  const [datasources, setDatasources] = useState<{ id: number; name: string }[]>([]);
  const [properties, setProperties] = useState<
    { id: number; propertyName: string | null; sourceColumn: string | null }[]
  >([]);
  const [submitting, setSubmitting] = useState(false);

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      setBindings(await listSourceBindings());
    } catch {
      message.error(t("procurement.bindings.loadFailed"));
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const openWizard = useCallback(async () => {
    setWizard({ ...WIZARD_INITIAL });
    setWizardOpen(true);
    try {
      const [cls, ds] = await Promise.all([listClasses(), listDataSources()]);
      setClasses(cls.map((c) => ({ id: c.id, className: c.className })));
      setDatasources(ds.map((d) => ({ id: d.id, name: d.name })));
    } catch {
      message.error(t("procurement.bindings.loadFailed"));
    }
  }, [t]);

  const loadProperties = useCallback(async (classId: number) => {
    try {
      setProperties(await listPropertiesByClass(classId));
    } catch {
      setProperties([]);
    }
  }, []);

  const submitWizard = useCallback(async () => {
    setSubmitting(true);
    try {
      const binding = await createSourceBinding({
        classId: wizard.classId!,
        datasourceId: wizard.datasourceId!,
        schemaName: wizard.schemaName ?? "",
        sourceTable: wizard.sourceTable ?? "",
        bindingRole: (wizard.bindingRole ?? "BASE") as BindingRole,
        filterExpression: wizard.filterExpression || null,
      });
      // 属性绑定：列到列映射逐条创建（候选态——确认在下一批动作里）
      for (const prop of properties) {
        const column = wizard.columnByProperty[prop.id];
        if (column && column.trim()) {
          await createPropertyBinding(binding.id, {
            propertyId: prop.id,
            sourceColumn: column.trim(),
          });
        }
      }
      message.success(t("procurement.bindings.wizard.created"));
      setWizard((w) => ({ ...w, step: 2, createdBindingId: binding.id }));
      await reload();
    } catch (err) {
      message.error(
        err instanceof Error ? err.message : t("procurement.bindings.actionFailed"),
      );
    } finally {
      setSubmitting(false);
    }
  }, [properties, reload, t, wizard]);

  const confirmBinding = useCallback(
    async (id: number) => {
      try {
        await confirmSourceBinding(id);
        message.success(t("procurement.bindings.done.confirm"));
        setDetail(null);
        await reload();
      } catch (err) {
        message.error(
          err instanceof Error
            ? err.message
            : t("procurement.bindings.actionFailed"),
        );
      }
    },
    [reload, t],
  );

  const confirmProp = useCallback(
    async (sourceBindingId: number, propId: number) => {
      try {
        await confirmPropertyBinding(sourceBindingId, propId);
        message.success(t("procurement.bindings.done.confirmProp"));
        if (detail != null && detail.binding.id === sourceBindingId) {
          setDetail(await getSourceBinding(sourceBindingId));
        }
        await reload();
      } catch (err) {
        message.error(
          err instanceof Error
            ? err.message
            : t("procurement.bindings.actionFailed"),
        );
      }
    },
    [detail, reload, t],
  );

  const expireBinding = useCallback(
    async (id: number) => {
      try {
        await expireSourceBinding(id);
        message.success(t("procurement.bindings.done.expire"));
        setDetail(null);
        await reload();
      } catch (err) {
        message.error(
          err instanceof Error
            ? err.message
            : t("procurement.bindings.actionFailed"),
        );
      }
    },
    [reload, t],
  );

  const openDetail = useCallback(
    async (id: number) => {
      try {
        setDetail(await getSourceBinding(id));
      } catch {
        message.error(t("procurement.bindings.loadFailed"));
      }
    },
    [t],
  );

  const columns: ColumnsType<SourceBinding> = [
    { title: "ID", dataIndex: "id", key: "id", width: 70 },
    {
      title: t("procurement.bindings.col.className"),
      dataIndex: "className",
      key: "className",
    },
    {
      title: t("procurement.bindings.col.table"),
      key: "table",
      render: (_, r) => `${r.schemaName}.${r.sourceTable}`,
    },
    {
      title: t("procurement.bindings.col.role"),
      dataIndex: "bindingRole",
      key: "role",
      render: (r: string) => <Tag color={ROLE_COLOR[r] ?? "default"}>{r}</Tag>,
    },
    {
      title: t("procurement.bindings.col.state"),
      key: "state",
      render: (_, r) =>
        r.validTo != null ? (
          <Tag>{t("procurement.bindings.state.expired")}</Tag>
        ) : r.isCandidate ? (
          <Tag color="warning">{t("procurement.bindings.state.candidate")}</Tag>
        ) : (
          <Tag color="success">{t("procurement.bindings.state.confirmed")}</Tag>
        ),
    },
    {
      title: t("procurement.bindings.col.properties"),
      dataIndex: "propertyCount",
      key: "propertyCount",
    },
    {
      title: "",
      key: "actions",
      render: (_, r) => (
        <Button size="small" onClick={() => void openDetail(r.id)}>
          {t("procurement.bindings.openDetail")}
        </Button>
      ),
    },
  ];

  const propColumns: ColumnsType<PropertyBinding> = [
    {
      title: t("procurement.bindings.prop.property"),
      dataIndex: "propertyName",
      key: "propertyName",
    },
    {
      title: t("procurement.bindings.prop.column"),
      dataIndex: "sourceColumn",
      key: "sourceColumn",
    },
    {
      title: t("procurement.bindings.prop.keyOrdinal"),
      dataIndex: "keyOrdinal",
      key: "keyOrdinal",
      render: (v: number | null) => (v != null ? `#${v}` : "-"),
    },
    {
      title: t("procurement.bindings.prop.state"),
      key: "state",
      render: (_, r) =>
        r.isCandidate ? (
          <Popconfirm
            title={t("procurement.bindings.confirm.propTitle")}
            onConfirm={() =>
              void confirmProp(detail!.binding.id, r.id)
            }
          >
            <Button size="small" type="primary">
              {t("procurement.bindings.action.confirmProp")}
            </Button>
          </Popconfirm>
        ) : (
          <Tag color="success">{t("procurement.bindings.state.confirmed")}</Tag>
        ),
    },
  ];

  const detailActions = detail && detail.binding.validTo == null && (
    <Space>
      {detail.binding.isCandidate && detail.properties.some((p) => p.isCandidate) && (
        <Alert
          type="warning"
          showIcon
          message={t("procurement.bindings.warn.confirmPropsFirst")}
        />
      )}
      {detail.binding.isCandidate && (
        <Popconfirm
          title={t("procurement.bindings.confirm.title")}
          description={t("procurement.bindings.confirm.description")}
          onConfirm={() => void confirmBinding(detail.binding.id)}
        >
          <Button type="primary">
            {t("procurement.bindings.action.confirm")}
          </Button>
        </Popconfirm>
      )}
      <Popconfirm
        title={t("procurement.bindings.confirm.expireTitle")}
        onConfirm={() => void expireBinding(detail.binding.id)}
      >
        <Button danger>{t("procurement.bindings.action.expire")}</Button>
      </Popconfirm>
    </Space>
  );

  return (
    <div style={{ padding: 24 }}>
      <Space direction="vertical" style={{ width: "100%" }} size="large">
        <Space style={{ width: "100%", justifyContent: "space-between" }}>
          <h2>{t("procurement.bindings.title")}</h2>
          <Button type="primary" onClick={() => void openWizard()}>
            {t("procurement.bindings.wizard.open")}
          </Button>
        </Space>
        <Table
          rowKey="id"
          columns={columns}
          dataSource={bindings}
          loading={loading}
          pagination={{ pageSize: 10, showSizeChanger: false }}
        />
      </Space>

      <Modal
        open={wizardOpen}
        title={t("procurement.bindings.wizard.title")}
        width={720}
        footer={
          wizard.step < 2 ? (
            <Space>
              <Button onClick={() => setWizardOpen(false)}>
                {t("procurement.bindings.wizard.cancel")}
              </Button>
              {wizard.step === 0 && (
                <Button
                  type="primary"
                  disabled={
                    wizard.classId == null ||
                    wizard.datasourceId == null ||
                    !wizard.sourceTable?.trim() ||
                    !wizard.schemaName?.trim()
                  }
                  onClick={() => {
                    void loadProperties(wizard.classId!);
                    setWizard((w) => ({ ...w, step: 1 }));
                  }}
                >
                  {t("procurement.bindings.wizard.next")}
                </Button>
              )}
              {wizard.step === 1 && (
                <Button
                  type="primary"
                  loading={submitting}
                  onClick={() => void submitWizard()}
                >
                  {t("procurement.bindings.wizard.createBinding")}
                </Button>
              )}
            </Space>
          ) : (
            <Button
              type="primary"
              onClick={() => {
                setWizardOpen(false);
                if (wizard.createdBindingId != null) {
                  void openDetail(wizard.createdBindingId);
                }
              }}
            >
              {t("procurement.bindings.wizard.toConfirm")}
            </Button>
          )
        }
        onCancel={() => setWizardOpen(false)}
        destroyOnHidden
      >
        <Steps
          current={wizard.step}
          items={[
            { title: t("procurement.bindings.wizard.step1") },
            { title: t("procurement.bindings.wizard.step2") },
            { title: t("procurement.bindings.wizard.step3") },
          ]}
          size="small"
          style={{ marginBottom: 16 }}
        />
        {wizard.step === 0 && (
          <Form layout="vertical">
            <Form.Item label={t("procurement.bindings.wizard.class")} required>
              <Select
                showSearch
                optionFilterProp="label"
                options={classes.map((c) => ({
                  value: c.id,
                  label: c.className,
                }))}
                onChange={(v) => setWizard((w) => ({ ...w, classId: v }))}
              />
            </Form.Item>
            <Form.Item label={t("procurement.bindings.wizard.datasource")} required>
              <Select
                showSearch
                optionFilterProp="label"
                options={datasources.map((d) => ({
                  value: d.id,
                  label: d.name,
                }))}
                onChange={(v) => setWizard((w) => ({ ...w, datasourceId: v }))}
              />
            </Form.Item>
            <Form.Item label={t("procurement.bindings.wizard.schema")} required>
              <Input
                value={wizard.schemaName ?? ""}
                onChange={(e) =>
                  setWizard((w) => ({ ...w, schemaName: e.target.value }))
                }
                placeholder="THBI"
              />
            </Form.Item>
            <Form.Item label={t("procurement.bindings.wizard.table")} required>
              <Input
                value={wizard.sourceTable ?? ""}
                onChange={(e) =>
                  setWizard((w) => ({ ...w, sourceTable: e.target.value }))
                }
                placeholder="DWD_PURCHASE_ORDER_LINE"
              />
            </Form.Item>
            <Form.Item label={t("procurement.bindings.wizard.role")} required>
              <Select
                value={wizard.bindingRole ?? "BASE"}
                options={(["BASE", "NUMERATOR", "DENOMINATOR", "FILTER"] as const).map(
                  (r) => ({ value: r, label: r }),
                )}
                onChange={(v) => setWizard((w) => ({ ...w, bindingRole: v }))}
              />
            </Form.Item>
            <Form.Item label={t("procurement.bindings.wizard.filter")}>
              <Input
                value={wizard.filterExpression ?? ""}
                onChange={(e) =>
                  setWizard((w) => ({ ...w, filterExpression: e.target.value }))
                }
                placeholder={t("procurement.bindings.wizard.filterPlaceholder")}
              />
            </Form.Item>
          </Form>
        )}
        {wizard.step === 1 && (
          <>
            <Alert
              type="info"
              showIcon
              message={t("procurement.bindings.wizard.mappingHint")}
              style={{ marginBottom: 12 }}
            />
            <Table
              rowKey="id"
              size="small"
              pagination={false}
              dataSource={properties}
              columns={[
                {
                  title: t("procurement.bindings.prop.property"),
                  dataIndex: "propertyName",
                },
                {
                  title: t("procurement.bindings.prop.column"),
                  key: "column",
                  width: 260,
                  render: (_, r) => (
                    <Input
                      value={wizard.columnByProperty[r.id] ?? ""}
                      placeholder={r.sourceColumn ?? ""}
                      onChange={(e) =>
                        setWizard((w) => ({
                          ...w,
                          columnByProperty: {
                            ...w.columnByProperty,
                            [r.id]: e.target.value,
                          },
                        }))
                      }
                    />
                  ),
                },
              ]}
            />
          </>
        )}
        {wizard.step === 2 && (
          <Alert
            type="success"
            showIcon
            message={t("procurement.bindings.wizard.created")}
            description={t("procurement.bindings.wizard.createdHint")}
          />
        )}
      </Modal>

      <Drawer
        open={detail != null}
        title={detail ? `${detail.binding.className ?? ""} → ${detail.binding.sourceTable}` : ""}
        width={680}
        onClose={() => setDetail(null)}
        destroyOnHidden
      >
        {detail != null && (
          <Space direction="vertical" style={{ width: "100%" }} size="middle">
            {detailActions}
            <Table
              rowKey="id"
              size="small"
              pagination={false}
              columns={propColumns}
              dataSource={detail.properties}
            />
          </Space>
        )}
      </Drawer>
    </div>
  );
}
