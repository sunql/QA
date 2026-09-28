import { useEffect, useState } from "react";
import { useLocation } from "react-router-dom";
import { Button, Input, Select, Spin, Typography, message } from "antd";
import { SendOutlined } from "@ant-design/icons";
import { getSuggestions } from "../../api/chat";
import { listDataSources } from "../../api/datasource";
import { listModels } from "../../api/modelConfig";
import type { DataSource } from "../../types/datasource";
import type { ModelConfig } from "../../types/modelConfig";
import type { ChartType, SimilarQuery } from "../../types/chat";
import { useTranslation } from "../../i18n";

const { Text } = Typography;

interface ChatPanelProps {
  datasourceId: number | null;
  selectedModelId: number | null;
  onDatasourceChange: (id: number | null) => void;
  onModelChange: (id: number | null) => void;
  onSend: (question: string, chartType: ChartType | null) => void;
  loading: boolean;
}

// 图表类型 value 与字典 key 的映射（label 通过 t() 运行时解析）
const CHART_TYPE_KEYS: { value: string; labelKey: keyof typeof import("../../i18n").zhCN.chatPanel.chartTypes }[] = [
  { value: "auto", labelKey: "auto" },
  { value: "table", labelKey: "table" },
  { value: "bar", labelKey: "bar" },
  { value: "pie", labelKey: "pie" },
  { value: "line", labelKey: "line" },
  { value: "scatter", labelKey: "scatter" },
];

// 相似问法建议的防抖间隔（毫秒）
const SUGGEST_DEBOUNCE_MS = 300;

export default function ChatPanel({
  datasourceId,
  selectedModelId,
  onDatasourceChange,
  onModelChange,
  onSend,
  loading,
}: ChatPanelProps) {
  const { t } = useTranslation();
  const location = useLocation();
  const [sources, setSources] = useState<DataSource[]>([]);
  const [models, setModels] = useState<ModelConfig[]>([]);
  const [question, setQuestion] = useState("");
  const [chartType, setChartType] = useState<ChartType | null>(null);
  // 历史相似问法建议（Phase 4）：防抖请求，点击回填输入框
  const [suggestions, setSuggestions] = useState<SimilarQuery[]>([]);
  const [suggestLoading, setSuggestLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    listDataSources(true)
      .then((data) => {
        if (cancelled) return;
        setSources(data);
        if (datasourceId === null) {
          const defaultSource = data.find((s) => s.isDefault) ?? data[0];
          if (defaultSource) {
            onDatasourceChange(defaultSource.id);
          }
        }
      })
      .catch(() => {
        // 数据源加载失败由拦截器提示，不阻塞聊天
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    let cancelled = false;
    listModels(true)
      .then((data) => {
        if (cancelled) return;
        setModels(data);
      })
      .catch(() => {
        // 模型列表加载失败不影响聊天功能
      });
    return () => {
      cancelled = true;
    };
  }, [location.pathname]);

  // 模型列表更新后，若当前选中的模型已不在 active 列表（被禁用 / 软删除），
  // 自动切回"自动路由"，避免 Select 显示一个 options 里没有的 value。
  // models.length > 0 守卫：避免初次挂载（models 还没拉回来）就误清空 store。
  useEffect(() => {
    if (
      selectedModelId !== null &&
      models.length > 0 &&
      !models.some((m) => m.id === selectedModelId)
    ) {
      onModelChange(null);
    }
  }, [models, selectedModelId, onModelChange]);

  // 相似问法建议：question 停顿 SUGGEST_DEBOUNCE_MS 后请求；空问题或无数据源时清空
  useEffect(() => {
    const q = question.trim();
    if (!q || datasourceId === null) {
      setSuggestions([]);
      setSuggestLoading(false);
      return;
    }
    let cancelled = false;
    const timer = window.setTimeout(() => {
      setSuggestLoading(true);
      getSuggestions(q, datasourceId)
        .then((items) => {
          if (!cancelled) setSuggestions(items);
        })
        .catch(() => {
          // 建议属锦上添花，失败静默（拦截器已提示，不阻塞输入）
          if (!cancelled) setSuggestions([]);
        })
        .finally(() => {
          if (!cancelled) setSuggestLoading(false);
        });
    }, SUGGEST_DEBOUNCE_MS);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [question, datasourceId]);

  const handleSend = () => {
    const q = question.trim();
    if (!q) return;
    if (datasourceId === null) {
      void message.warning(t("chatPanel.pleaseSelectDatasource"));
      return;
    }
    onSend(q, chartType);
    setQuestion("");
    // 发送后清空建议（防抖 effect 亦会因 question 置空而清空，此处显式兜底）
    setSuggestions([]);
  };

  const modelOptions = [
    { label: t("chatPanel.modelAutoRoute"), value: -1 },
    ...models.map((m) => ({
      label: `${m.modelName} (${m.provider})`,
      value: m.id,
    })),
  ];

  const chartTypeOptions = CHART_TYPE_KEYS.map(({ value, labelKey }) => ({
    value,
    label: t(`chatPanel.chartTypes.${labelKey}` as const),
  }));

  return (
    <div style={{ borderTop: "1px solid #f0f0f0", padding: 12 }}>
      <div style={{ display: "flex", gap: 8, marginBottom: 8 }}>
        <Select
          style={{ width: 200, flexShrink: 0 }}
          placeholder={t("chatPanel.datasourcePlaceholder")}
          value={datasourceId}
          onChange={(value) => onDatasourceChange(typeof value === "number" ? value : null)}
          options={sources.map((s) => ({ label: s.name, value: s.id }))}
        />
        <Select
          style={{ width: 200, flexShrink: 0 }}
          placeholder={t("chatPanel.modelPlaceholder")}
          value={selectedModelId ?? -1}
          onChange={(value) => onModelChange(value === -1 ? null : value)}
          options={modelOptions}
        />
        <Select
          style={{ width: 120, flexShrink: 0 }}
          placeholder={t("chatPanel.chartTypePlaceholder")}
          aria-label={t("chatPanel.chartTypeAriaLabel")}
          value={chartType ?? "auto"}
          onChange={(value) => setChartType(value === "auto" ? null : (value as ChartType))}
          options={chartTypeOptions}
        />
      </div>
      {suggestLoading || suggestions.length > 0 ? (
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 8 }}>
          {suggestLoading ? (
            <Text type="secondary" style={{ fontSize: 12 }}>
              <Spin size="small" style={{ marginRight: 4 }} />
              {t("chatPanel.suggesting")}
            </Text>
          ) : null}
          {suggestions.map((s, idx) => (
            <Button
              // key 加 idx 后缀：SimilarQuery 只有 question/sql/similarity 三字段，
              // 后端偶发会返回相同 question 的多条（如同一问题多次命中），
              // 用 idx 保证 React key 唯一，避免 duplicate key 警告
              key={`${idx}-${s.question}`}
              size="small"
              onClick={() => setQuestion(s.question)}
              title={s.sql ? t("chatPanel.suggestTitle") : undefined}
            >
              {s.question}
            </Button>
          ))}
        </div>
      ) : null}
      <div style={{ display: "flex", gap: 8 }}>
        <Input.TextArea
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder={t("chatPanel.inputPlaceholder")}
          style={{ flex: 1 }}
          autoSize={{ minRows: 2, maxRows: 6 }}
          onPressEnter={(e) => {
            if (!e.shiftKey) {
              e.preventDefault();
              handleSend();
            }
          }}
        />
        <Button
          type="primary"
          icon={<SendOutlined />}
          loading={loading}
          onClick={handleSend}
          style={{ flexShrink: 0 }}
        >
          {t("chatPanel.sendButton")}
        </Button>
      </div>
    </div>
  );
}