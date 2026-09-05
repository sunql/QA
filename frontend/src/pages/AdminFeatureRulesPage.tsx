/**
 * AdminFeatureRulesPage — Feature Rule configuration page.
 *
 * Structure mirrors AdminToolsPage (feat-agent-tool-config-db):
 * - AntD Table listing all FeatureRule records
 * - Header with "新建" + "刷新" buttons
 * - Drawer (create / edit) with Form.List for nested thresholds
 * - "AI 辅助填写" button inside drawer that opens AiAssistModal
 * - On parse-description success the modal calls back with suggestions,
 *   which prefill the drawer's thresholds array.
 *
 * Error handling (message.error):
 * - 409 → featureRules.errors.versionConflict
 * - other → featureRules.messages.failed
 */
import { useEffect, useState, useCallback } from "react";
import {
    Table,
    Button,
    Drawer,
    Form,
    Input,
    InputNumber,
    Select,
    Switch,
    Tag,
    message,
    Popconfirm,
    Space,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useTranslation } from "react-i18next";
import {
    listFeatureRules,
    createFeatureRule,
    updateFeatureRule,
    deleteFeatureRule,
    toggleFeatureRule,
} from "../api/featureRules";
import type {
    FeatureRule,
    FeatureRuleCreate,
    FeatureRuleUpdate,
    FeatureRuleThreshold,
    FeatureRuleParseDescriptionResponse,
} from "../types/featureRules";
import AiAssistModal from "../components/admin/AiAssistModal";

const { TextArea } = Input;

const DATA_OBJECT_OPTIONS = [
    { value: "SUPPLIER" },
    { value: "MATERIAL" },
    { value: "PURCHASE_ORDER" },
    { value: "INVOICE" },
];

const DATA_LAYER_OPTIONS = [
    { value: "DIM" },
    { value: "DWD" },
    { value: "FEATURE" },
];

const TARGET_LEVEL_OPTIONS = [
    { value: "RISK" },
    { value: "QUALITY" },
    { value: "LOGISTICS" },
    { value: "PROCUREMENT" },
    { value: "FINANCIAL" },
];

const SEVERITY_OPTIONS = [
    { value: "HIGH" },
    { value: "MEDIUM" },
    { value: "LOW" },
    { value: "INFO" },
];

const OPERATOR_OPTIONS = [
    { value: "lt" },
    { value: "lte" },
    { value: "gt" },
    { value: "gte" },
    { value: "lt_inverse" },
];

export default function AdminFeatureRulesPage(): JSX.Element {
    const { t } = useTranslation();
    const [rules, setRules] = useState<FeatureRule[]>([]);
    const [loading, setLoading] = useState(false);
    const [drawerOpen, setDrawerOpen] = useState(false);
    const [editing, setEditing] = useState<FeatureRule | null>(null);
    const [aiModalOpen, setAiModalOpen] = useState(false);
    const [form] = Form.useForm();
    const [thresholds, setThresholds] = useState<FeatureRuleThreshold[]>([]);

    const fetchList = useCallback(async () => {
        setLoading(true);
        try {
            setRules(await listFeatureRules());
        } catch (e) {
            const err = e as Error & { message?: string };
            void message.error(
                t("featureRules.messages.failed") +
                    ": " +
                    (err.message ?? String(e)),
            );
        } finally {
            setLoading(false);
        }
    }, [t]);

    useEffect(() => {
        void fetchList();
    }, [fetchList]);

    const onCreate = () => {
        setEditing(null);
        setThresholds([]);
        form.resetFields();
        setDrawerOpen(true);
    };

    const onEdit = (rec: FeatureRule) => {
        setEditing(rec);
        setThresholds(rec.thresholds ?? []);
        form.setFieldsValue({
            ...rec,
            thresholds: rec.thresholds ?? [],
        });
        setDrawerOpen(true);
    };

    const onSubmit = async (
        values: FeatureRuleCreate | FeatureRuleUpdate,
    ) => {
        try {
            if (editing) {
                const payload: FeatureRuleUpdate = {
                    enabled: values.enabled,
                    priority: values.priority,
                    policy_description: values.policy_description,
                    thresholds: thresholds.length > 0 ? thresholds : [],
                    version: editing.version,
                };
                await updateFeatureRule(editing.code, payload);
                void message.success(t("featureRules.messages.updated"));
            } else {
                const payload: FeatureRuleCreate = {
                    code: values.code,
                    data_object: values.data_object,
                    data_layer: values.data_layer,
                    target_level: values.target_level,
                    feature_name: values.feature_name,
                    enabled: values.enabled ?? true,
                    priority: values.priority ?? 100,
                    policy_description: values.policy_description,
                    thresholds: thresholds.length > 0 ? thresholds : [],
                };
                await createFeatureRule(payload);
                void message.success(t("featureRules.messages.created"));
            }
            setDrawerOpen(false);
            void fetchList();
        } catch (e) {
            const err = e as Error & { status?: number };
            if (err.status === 409) {
                void message.error(t("featureRules.errors.versionConflict"));
            } else {
                const err2 = e as Error & { message?: string };
                void message.error(
                    t("featureRules.messages.failed") +
                        ": " +
                        (err2.message ?? String(e)),
                );
            }
        }
    };

    const onDelete = async (code: string) => {
        try {
            await deleteFeatureRule(code);
            void message.success(t("featureRules.messages.deleted"));
            void fetchList();
        } catch (e) {
            const err = e as Error & { status?: number };
            if (err.status === 409) {
                void message.error(t("featureRules.errors.referencing"));
            } else {
                void message.error(t("featureRules.messages.failed"));
            }
        }
    };

    const onToggle = async (rec: FeatureRule, enabled: boolean) => {
        try {
            await toggleFeatureRule(rec.code, enabled);
            void message.success(t("featureRules.messages.toggled"));
            void fetchList();
        } catch {
            void message.error(t("featureRules.messages.failed"));
        }
    };

    /**
     * Called when AI modal returns parsed suggestions.
     * Prefills thresholds array with suggested values.
     */
    const onAiAssistApply = (
        result: FeatureRuleParseDescriptionResponse,
    ) => {
        const mapped: FeatureRuleThreshold[] = result.suggested_thresholds.map(
            (s, idx) => ({
                severity: s.severity,
                operator: s.operator,
                threshold_value: s.threshold_value,
                unit: s.unit ?? null,
                threshold_order: idx + 1,
            }),
        );
        setThresholds(mapped);
        // Also update the form field so Form.List re-renders
        form.setFieldValue("thresholds", mapped);
    };

    const columns: ColumnsType<FeatureRule> = [
        {
            title: t("featureRules.columns.code"),
            dataIndex: "code",
            render: (v: string) => <Tag color="blue">{v}</Tag>,
        },
        {
            title: t("featureRules.columns.data_object"),
            dataIndex: "data_object",
        },
        {
            title: t("featureRules.columns.data_layer"),
            dataIndex: "data_layer",
        },
        {
            title: t("featureRules.columns.target_level"),
            dataIndex: "target_level",
        },
        {
            title: t("featureRules.columns.feature_name"),
            dataIndex: "feature_name",
            ellipsis: true,
        },
        {
            title: t("featureRules.columns.enabled"),
            dataIndex: "enabled",
            render: (v: boolean, rec: FeatureRule) => (
                <Switch
                    checked={v}
                    onChange={(checked) => void onToggle(rec, checked)}
                />
            ),
        },
        {
            title: t("featureRules.columns.priority"),
            dataIndex: "priority",
        },
        {
            title: t("featureRules.columns.actions"),
            key: "actions",
            render: (_, rec) => (
                <Space>
                    <Button size="small" onClick={() => onEdit(rec)}>
                        {t("featureRules.actions.edit")}
                    </Button>
                    <Popconfirm
                        title={
                            t("featureRules.actions.delete") + " " + rec.code + "?"
                        }
                        onConfirm={() => void onDelete(rec.code)}
                    >
                        <Button size="small" danger>
                            {t("featureRules.actions.delete")}
                        </Button>
                    </Popconfirm>
                </Space>
            ),
        },
    ];

    return (
        <div style={{ padding: 24 }}>
            <Space style={{ marginBottom: 16 }}>
                <Button type="primary" onClick={onCreate}>
                    {t("featureRules.actions.create")}
                </Button>
                <Button onClick={() => void fetchList()}>
                    {t("featureRules.actions.refresh")}
                </Button>
            </Space>

            <Table
                rowKey="id"
                loading={loading}
                dataSource={rules}
                columns={columns}
                pagination={{ pageSize: 20 }}
            />

            <Drawer
                open={drawerOpen}
                title={
                    editing
                        ? t("featureRules.actions.edit")
                        : t("featureRules.actions.create")
                }
                onCancel={() => setDrawerOpen(false)}
                width={640}
                destroyOnHidden
            >
                <Form
                    form={form}
                    layout="vertical"
                    onFinish={(values) => void onSubmit(values)}
                    initialValues={{
                        enabled: true,
                        priority: 100,
                    }}
                >
                    <Form.Item
                        name="code"
                        label={t("featureRules.form.code")}
                        rules={[
                            { required: !editing },
                            {
                                pattern: /^[A-Z][A-Z0-9_]*$/,
                                message: "UPPER_SNAKE_CASE",
                            },
                        ]}
                    >
                        <Input
                            disabled={!!editing}
                            placeholder={t("featureRules.form.codePlaceholder")}
                        />
                    </Form.Item>

                    <Form.Item
                        name="data_object"
                        label={t("featureRules.form.data_object")}
                        rules={[{ required: true }]}
                    >
                        <Select options={DATA_OBJECT_OPTIONS} />
                    </Form.Item>

                    <Form.Item
                        name="data_layer"
                        label={t("featureRules.form.data_layer")}
                        rules={[{ required: true }]}
                    >
                        <Select options={DATA_LAYER_OPTIONS} />
                    </Form.Item>

                    <Form.Item
                        name="target_level"
                        label={t("featureRules.form.target_level")}
                        rules={[{ required: true }]}
                    >
                        <Select options={TARGET_LEVEL_OPTIONS} />
                    </Form.Item>

                    <Form.Item
                        name="feature_name"
                        label={t("featureRules.form.feature_name")}
                        rules={[{ required: true }]}
                    >
                        <Input />
                    </Form.Item>

                    <Form.Item
                        name="enabled"
                        label={t("featureRules.form.enabled")}
                        valuePropName="checked"
                    >
                        <Switch />
                    </Form.Item>

                    <Form.Item
                        name="priority"
                        label={t("featureRules.form.priority")}
                    >
                        <InputNumber min={0} max={9999} />
                    </Form.Item>

                    <Form.Item
                        name="policy_description"
                        label={t("featureRules.form.policy_description")}
                    >
                        <TextArea
                            rows={3}
                            placeholder={t(
                                "featureRules.form.policyDescriptionPlaceholder",
                            )}
                        />
                    </Form.Item>

                    <Form.Item
                        label={
                            <Space>
                                <span>{t("featureRules.form.thresholds")}</span>
                                <Button
                                    size="small"
                                    onClick={() => setAiModalOpen(true)}
                                >
                                    {t("featureRules.actions.aiAssist")}
                                </Button>
                            </Space>
                        }
                    >
                        <Form.List name="thresholds">
                            {(fields, { add, remove }) => (
                                <>
                                    {fields.map(({ key, name }) => (
                                        <Space
                                            key={key}
                                            direction="vertical"
                                            style={{
                                                display: "flex",
                                                marginBottom: 8,
                                            }}
                                        >
                                            <Space wrap>
                                                <Form.Item
                                                    name={[name, "severity"]}
                                                    style={{
                                                        marginBottom: 0,
                                                        minWidth: 100,
                                                    }}
                                                    rules={[{ required: true }]}
                                                >
                                                    <Select
                                                        placeholder="Severity"
                                                        options={SEVERITY_OPTIONS}
                                                    />
                                                </Form.Item>
                                                <Form.Item
                                                    name={[name, "operator"]}
                                                    style={{
                                                        marginBottom: 0,
                                                        minWidth: 110,
                                                    }}
                                                    rules={[{ required: true }]}
                                                >
                                                    <Select
                                                        placeholder="Operator"
                                                        options={OPERATOR_OPTIONS}
                                                    />
                                                </Form.Item>
                                                <Form.Item
                                                    name={[
                                                        name,
                                                        "threshold_value",
                                                    ]}
                                                    style={{
                                                        marginBottom: 0,
                                                        width: 100,
                                                    }}
                                                    rules={[{ required: true }]}
                                                >
                                                    <InputNumber
                                                        placeholder="Value"
                                                        precision={4}
                                                    />
                                                </Form.Item>
                                                <Form.Item
                                                    name={[name, "unit"]}
                                                    style={{
                                                        marginBottom: 0,
                                                        width: 80,
                                                    }}
                                                >
                                                    <Input placeholder="Unit" />
                                                </Form.Item>
                                                <Button
                                                    size="small"
                                                    danger
                                                    onClick={() => remove(name)}
                                                >
                                                    {t("common.delete", {
                                                        ns: "common",
                                                    })}
                                                </Button>
                                            </Space>
                                        </Space>
                                    ))}
                                    <Button
                                        type="dashed"
                                        onClick={() =>
                                            add({
                                                severity: "HIGH",
                                                operator: "gt",
                                                threshold_value: 0,
                                                threshold_order:
                                                    fields.length + 1,
                                            })
                                        }
                                        block
                                    >
                                        {t("featureRules.form.addThreshold", {
                                            defaultText: "添加阈值",
                                        })}
                                    </Button>
                                </>
                            )}
                        </Form.List>
                    </Form.Item>

                    <Button type="primary" htmlType="submit" block>
                        {editing
                            ? t("featureRules.actions.edit")
                            : t("featureRules.actions.create")}
                    </Button>
                </Form>
            </Drawer>

            <AiAssistModal
                open={aiModalOpen}
                onCancel={() => setAiModalOpen(false)}
                onApply={onAiAssistApply}
            />
        </div>
    );
}
