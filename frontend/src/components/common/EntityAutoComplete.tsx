/** Phase 6.x：按 enterprise_code/source_code/enterprise_key 模糊搜索的 AutoComplete。
 *
 * 解决「用户不知道 magic 5-9 位 BIGINT」录入 enterprise_key 的痛点：用户输入供应商
 * 名称/编码/部分数字 → 后端 ILIKE → 下拉显示 name + enterprise_code + entity_type
 * → 选中后通过 `onChange(value: number)` 把 enterprise_key 回传给上层。
 *
 * 设计要点：
 * - 300ms debounce，避免每个按键都打后端
 * - 1 字符即触发（用户已确认需求；后端 enterprise_code/source_code 已建索引）
 * - 自定义 `notFoundContent`：1+ 字符无命中 → 友好提示而非空白
 * - 受控组件（value + onChange），状态归属上层
 * - typing.ts 的严格规范：props 用 interface，回调签名显式标注
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { AutoComplete, Input } from "antd";
import type { AutoCompleteProps } from "antd";
import { searchMappings } from "../../api/entityMapping";
import type { EntityMappingSearchHit, EntityType } from "../../types/entityMapping";

interface EntityAutoCompleteProps {
  value: number | null;
  onChange: (value: number | null) => void;
  /** 限定搜索实体类型（如供应商页只搜 SUPPLIER/MATERIAL） */
  entityType?: EntityType;
  /** 输入框 placeholder */
  placeholder?: string;
  /** 命中后调用，拿到完整 hit（用于上层卡片预渲染等） */
  onSelect?: (hit: EntityMappingSearchHit) => void;
  /** 失焦/Enter 触发的"确认"回调（value 已是合法 enterprise_key 后上层决定查询时机） */
  onPressEnter?: () => void;
  /** 是否禁用 */
  disabled?: boolean;
  /** 额外的 AutoComplete 选项透传 */
  autoCompleteProps?: Partial<AutoCompleteProps>;
}

const DEBOUNCE_MS = 300;

export default function EntityAutoComplete(props: EntityAutoCompleteProps): JSX.Element {
  const {
    value,
    onChange,
    entityType,
    placeholder,
    onSelect,
    onPressEnter,
    disabled,
    autoCompleteProps,
  } = props;

  // 输入框里实际显示的字符串（与 enterprise_key 解耦：选中后才填进去）
  const [inputText, setInputText] = useState<string>(value ? String(value) : "");
  const [options, setOptions] = useState<AutoCompleteProps["options"]>([]);
  const [loading, setLoading] = useState(false);
  const [open, setOpen] = useState(false);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // 记住最近一次发出去的 q，避免 stale 响应覆盖新结果
  const latestQRef = useRef<string>("");

  // 外部 value 改变时（例如上层清空）同步到 inputText
  useEffect(() => {
    if (value === null) {
      setInputText("");
    } else if (!inputText || inputText !== String(value)) {
      // 仅在 inputText 不是合法 enterprise_key 字符串时才覆盖（避免用户输入时被打断）
      setInputText(String(value));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value]);

  const search = useMemo(
    () =>
      async (q: string): Promise<void> => {
        const trimmed = q.trim();
        if (trimmed.length === 0) {
          setOptions([]);
          return;
        }
        latestQRef.current = trimmed;
        setLoading(true);
        try {
          const hits = await searchMappings(trimmed, { entityType, limit: 20 });
          // 防止乱序：如果用户已经输入了更新的 q，丢弃旧响应
          if (latestQRef.current !== trimmed) return;
          setOptions(
            hits.map((h) => ({
              value: String(h.enterpriseKey),
              label: (
                <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
                  <div style={{ display: "flex", justifyContent: "space-between", gap: 8 }}>
                    <strong style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      {h.name || h.enterpriseCode}
                    </strong>
                    <span style={{ color: "#999", fontSize: 12, whiteSpace: "nowrap" }}>
                      {h.entityType}/{h.sourceSystem}
                    </span>
                  </div>
                  <div style={{ color: "#999", fontSize: 12 }}>
                    {h.name ? h.enterpriseCode : h.sourceCode}
                  </div>
                </div>
              ),
              // 把原始 hit 挂在 option 上，onSelect 时取出
              _hit: h,
            })) as AutoCompleteProps["options"],
          );
        } catch {
          // 错误由 axios interceptor 提示
          setOptions([]);
        } finally {
          setLoading(false);
        }
      },
    [entityType],
  );

  const handleSearch = (text: string): void => {
    setInputText(text);
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => {
      void search(text);
    }, DEBOUNCE_MS);
  };

  const handleSelect: AutoCompleteProps["onSelect"] = (
    _selection: string,
    option: unknown,
  ): void => {
    const hit = (option as { _hit?: EntityMappingSearchHit })._hit;
    if (!hit) return;
    setInputText(String(hit.enterpriseKey));
    onChange(hit.enterpriseKey);
    onSelect?.(hit);
    setOpen(false);
  };

  const handleChange = (v: string): void => {
    // 用户清空输入 → 通知上层清空 enterprise_key
    if (v === "") {
      setInputText("");
      onChange(null);
      setOptions([]);
      return;
    }
    // 用户键入 → 只更新本地文本，不立即触发 onChange（避免半成品 enterprise_key）
    handleSearch(v);
  };

  const notFoundContent = loading ? "搜索中…" : "无匹配实体";

  return (
    <AutoComplete
      value={inputText}
      options={options}
      open={open}
      onFocus={() => setOpen(true)}
      onBlur={() => setOpen(false)}
      onChange={handleChange}
      onSelect={handleSelect}
      notFoundContent={notFoundContent}
      style={{ width: 320 }}
      disabled={disabled}
      {...autoCompleteProps}
    >
      <Input
        placeholder={placeholder}
        onPressEnter={onPressEnter}
        allowClear
      />
    </AutoComplete>
  );
}