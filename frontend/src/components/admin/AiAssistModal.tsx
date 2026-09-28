import { useState } from "react";
import {
    Modal,
    Form,
    Input,
    Select,
    message,
    Space,
    Button,
    Alert,
    Typography,
} from "antd";
import { useTranslation } from "react-i18next";
import {
    parseFeatureRuleDescription,
} from "../../api/featureRules";
import type {
    FeatureRuleParseDescriptionRequest,
    FeatureRuleParseDescriptionResponse,
    FeatureRuleThresholdSuggestion,
} from "../../types/featureRules";

const { TextArea } = Input;
const { Text } = Typography;

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

export interface AiAssistModalProps {
    open: boolean;
    onCancel: () => void;
    /** Called with parsed response so parent can prefill the form. */
    onApply: (result: FeatureRuleParseDescriptionResponse) => void;
}

interface FormValues {
    dataObject: string;
    dataLayer: string;
    targetLevel: string;
    naturalLanguage: string;
}

export default function AiAssistModal({
    open,
    onCancel,
    onApply,
}: AiAssistModalProps): JSX.Element {
    const { t } = useTranslation();
    const [loading, setLoading] = useState(false);
    const [result, setResult] =
        useState<FeatureRuleParseDescriptionResponse | null>(null);
    const [form] = Form.useForm<FormValues>();

    const handleSubmit = async (values: FormValues) => {
        setLoading(true);
        setResult(null);
        try {
            const data: FeatureRuleParseDescriptionRequest = {
                dataObject: values.dataObject,
                dataLayer: values.dataLayer,
                targetLevel: values.targetLevel,
                naturalLanguage: values.naturalLanguage,
            };
            const response = await parseFeatureRuleDescription(data);
            setResult(response);
        } catch (e) {
            const err = e as Error & { message?: string };
            void message.error(
                t("featureRules.errors.llmUnavailable") +
                    ": " +
                    (err.message ?? String(e)),
            );
        } finally {
            setLoading(false);
        }
    };

    const handleApply = () => {
        if (result) {
            onApply(result);
            setResult(null);
            form.resetFields();
            onCancel();
        }
    };

    const handleCancel = () => {
        setResult(null);
        form.resetFields();
        onCancel();
    };

    return (
        <Modal
            open={open}
            title={t("featureRules.actions.aiAssist")}
            onCancel={handleCancel}
            footer={
                result
                    ? [
                          <Button key="cancel" onClick={handleCancel}>
                              {t("common.cancel", { ns: "common" })}
                          </Button>,
                          <Button
                              key="apply"
                              type="primary"
                              onClick={handleApply}
                          >
                              {t("featureRules.form.aiAssistApply", {
                                  defaultText: "应用建议",
                              })}
                          </Button>,
                      ]
                    : undefined
            }
            destroyOnHidden
            width={640}
        >
            {!result ? (
                <Form
                    form={form}
                    layout="vertical"
                    onFinish={handleSubmit}
                    initialValues={{
                        dataObject: "SUPPLIER",
                        dataLayer: "FEATURE",
                        targetLevel: "RISK",
                    }}
                >
                    <Form.Item
                        name="dataObject"
                        label={t("featureRules.form.data_object")}
                        rules={[{ required: true }]}
                    >
                        <Select options={DATA_OBJECT_OPTIONS} />
                    </Form.Item>
                    <Form.Item
                        name="dataLayer"
                        label={t("featureRules.form.data_layer")}
                        rules={[{ required: true }]}
                    >
                        <Select options={DATA_LAYER_OPTIONS} />
                    </Form.Item>
                    <Form.Item
                        name="targetLevel"
                        label={t("featureRules.form.target_level")}
                        rules={[{ required: true }]}
                    >
                        <Select options={TARGET_LEVEL_OPTIONS} />
                    </Form.Item>
                    <Form.Item
                        name="naturalLanguage"
                        label={t("featureRules.form.policy_description")}
                        rules={[{ required: true }]}
                    >
                        <TextArea
                            rows={4}
                            placeholder={t(
                                "featureRules.form.policyDescriptionPlaceholder",
                            )}
                        />
                    </Form.Item>
                    <Space>
                        <Button
                            type="primary"
                            htmlType="submit"
                            loading={loading}
                        >
                            {t("common.parse", { ns: "common" })}
                        </Button>
                    </Space>
                </Form>
            ) : (
                <Space direction="vertical" style={{ width: "100%" }}>
                    <Alert
                        type="info"
                        message={`${t("common.confidence", { ns: "common" })}: ${
                            result.overallConfidence
                        }%`}
                    />
                    {result.warnings.length > 0 && (
                        <Alert
                            type="warning"
                            message={
                                <ul style={{ margin: 0, paddingLeft: 16 }}>
                                    {result.warnings.map((w, i) => (
                                        <li key={i}>{w}</li>
                                    ))}
                                </ul>
                            }
                        />
                    )}
                    <Text strong>
                        {t("common.reasoning", { ns: "common" })}
                    </Text>
                    <Text>{result.reasoning}</Text>
                    {result.suggestedThresholds.length > 0 && (
                        <>
                            <Text strong>
                                {t("featureRules.form.suggestedThresholds", {
                                    defaultText: "建议阈值",
                                })}
                            </Text>
                            {result.suggestedThresholds.map(
                                (s: FeatureRuleThresholdSuggestion, i: number) => (
                                    <Alert
                                        key={i}
                                        type="success"
                                        message={
                                            <span>
                                                <strong>{s.featureName}</strong>{" "}
                                                — {s.severity} / {s.operator} /{" "}
                                                {s.thresholdValue}
                                                {s.unit ? ` ${s.unit}` : ""}{" "}
                                                <br />
                                                <Text type="secondary">
                                                    {t(
                                                        "common.rationale",
                                                        { ns: "common" },
                                                    )}
                                                    : {s.rationale}
                                                </Text>
                                            </span>
                                        }
                                    />
                                ),
                            )}
                        </>
                    )}
                </Space>
            )}
        </Modal>
    );
}
