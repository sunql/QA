/**
 * Wiki ↔ Ontology 链接管理页（Task 7 / feat-wiki-category）。
 *
 * 左侧：分类树 + 每个分类下的 wiki pages（叶子可点）。
 * 右侧：选中 page 的 class/property/metric 链接列表 + 添加/编辑 Modal。
 *
 * 树结构来源（前端两次请求合并）：
 *  1. GET /wiki/categories/tree — 全量分类树
 *  2. GET /wiki/pages?limit=50 — **最近 50 页**（按 categoryId 客户端挂到分类下）
 * 没挂分类的 page 放进「未分类」伪根，避免丢页。
 * 超过 50 页时树只是部分视图 ⇒ 顶部提示 + 搜索框走 /wiki/pages/search 检索全量。
 *
 * 节点 key 约定：
 *  - 分类节点  ``cat-<id>``（不可点，仅目录）
 *  - page 叶子  ``<pageId>``（可点 → 查 ontology links）
 */
import { useState, useEffect, useCallback, useMemo, useRef } from "react";
import {
  Button,
  Form,
  Input,
  Modal,
  Radio,
  Select,
  Slider,
  Space,
  Tabs,
  Tag,
  Tree,
  message,
} from "antd";
import type { DataNode } from "antd/es/tree";
import { useTranslation } from "../i18n";
import { ontologyObjectLabel } from "../utils/ontologyLabel";
import {
  createWikiLink,
  listLinkableTargets,
  listWikiLinks,
  revokeWikiLink,
  updateWikiLink,
} from "../api/adminWikiLinks";
import {
  listWikiCategoryTree,
  listWikiPages,
  searchWikiPages,
} from "../api/wikiPages";
import type { WikiCategoryNode, WikiPage } from "../types/wikiPages";
import type {
  WikiLink,
  WikiLinkType,
  WikiLinkableTarget,
} from "../types/wikiLink";

/** 分类节点 key 前缀（前端区分「分类节点」与「page 叶子」用）。 */
const CATEGORY_KEY_PREFIX = "cat-";
/** 没挂分类的 page 放进这个伪根。 */
const UNCATEGORIZED_KEY = "cat-0";
/** 左树默认加载页数。超过这个数的页面只能通过搜索框检索。 */
const PAGE_LIST_LIMIT = 50;
/** 本体对象下拉的服务端检索防抖。 */
const LINKABLE_DEBOUNCE_MS = 300;

/** 链接类型 → i18n 键（t() 键为 string，映射表避免模板串拼接的键类型问题）。 */
const TYPE_LABEL_KEY: Record<WikiLinkType, string> = {
  class: "wikiLinks.tabs.class",
  property: "wikiLinks.tabs.property",
  metric: "wikiLinks.tabs.metric",
};

/** 全量 pages → 按 categoryId 分组（key=0 表示未分类）。 */
function pagesByCategory(pages: WikiPage[]): Map<number, WikiPage[]> {
  const out = new Map<number, WikiPage[]>();
  for (const p of pages) {
    const cid = p.categoryId ?? 0;
    if (!out.has(cid)) out.set(cid, []);
    out.get(cid)!.push(p);
  }
  return out;
}

/** 把 pages 挂到对应分类节点下作为叶子；无分类页 → "未分类"伪根。 */
function attachPagesToTree(
  cats: WikiCategoryNode[],
  pages: WikiPage[],
  uncategorizedTitle: string,
): DataNode[] {
  const byCat = pagesByCategory(pages);
  const uncat = byCat.get(0) ?? [];

  const attach = (node: WikiCategoryNode): DataNode => {
    const catPages = byCat.get(node.id) ?? [];
    return {
      key: `${CATEGORY_KEY_PREFIX}${node.id}`,
      title: `${node.name}（${catPages.length}）`,
      selectable: false,
      children: [
        ...node.children.map(attach),
        ...catPages.map(pageToLeaf),
      ],
    };
  };

  const roots = cats.map(attach);
  if (uncat.length > 0) {
    roots.push({
      key: UNCATEGORIZED_KEY,
      title: `${uncategorizedTitle}（${uncat.length}）`,
      selectable: false,
      children: uncat.map(pageToLeaf),
    });
  }
  return roots;
}

function pageToLeaf(p: WikiPage): DataNode {
  return {
    key: p.pageId,
    title: p.title,
    selectable: true,
    isLeaf: true,
  };
}

/** 收集全部分类节点 key（含子分类），用作 defaultExpandedKeys —— 让首次打开就把整棵树铺平。 */
function allKeys(cats: WikiCategoryNode[]): string[] {
  const out: string[] = [];
  const visit = (c: WikiCategoryNode) => {
    out.push(`${CATEGORY_KEY_PREFIX}${c.id}`);
    c.children.forEach(visit);
  };
  cats.forEach(visit);
  return out;
}

/** 分类树扁平化：id → name（搜索结果标注分类用）。 */
function categoryNameMap(cats: WikiCategoryNode[]): Map<number, string> {
  const out = new Map<number, string>();
  const visit = (c: WikiCategoryNode) => {
    out.set(c.id, c.name);
    c.children.forEach(visit);
  };
  cats.forEach(visit);
  return out;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------
export function WikiLinksPage() {
  const { t, locale } = useTranslation();
  const [selectedPageId, setSelectedPageId] = useState<string | null>(null);
  const [linkType, setLinkType] = useState<WikiLinkType>("class");
  const [links, setLinks] = useState<WikiLink[]>([]);
  const [linkables, setLinkables] = useState<WikiLinkableTarget[]>([]);
  const [loadingLinkables, setLoadingLinkables] = useState(false);
  const [selectedTarget, setSelectedTarget] = useState<WikiLinkableTarget | null>(null);
  const [modalOpen, setModalOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [tree, setTree] = useState<WikiCategoryNode[]>([]);
  const [allPages, setAllPages] = useState<WikiPage[]>([]);
  const [pagesTotal, setPagesTotal] = useState(0);
  // 用 controlled expandedKeys：tree 数据回来后立刻铺开整树，
  // 避免 antd v5 defaultExpandAll 在「首次 render 数据空 + 二次 render 数据齐」
  // 这条链上失效（实测 defaultExpandedKeys/defaultExpandAll 都救不回来）。
  const [expandedKeys, setExpandedKeys] = useState<string[]>([]);
  const [form] = Form.useForm();

  // 全量页面检索（树只覆盖前 50 页）
  const [pageSearchInput, setPageSearchInput] = useState("");
  const [pageSearchQuery, setPageSearchQuery] = useState("");
  const [pageSearchResults, setPageSearchResults] = useState<WikiPage[]>([]);
  const [pageSearchTotal, setPageSearchTotal] = useState(0);
  const [pageSearching, setPageSearching] = useState(false);
  const pageSearchSeqRef = useRef(0);

  // 本体对象服务端检索：防抖 + 丢弃乱序响应
  const latestLinkableQRef = useRef("");
  const linkableTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // 编辑权重/备注
  const [editingLink, setEditingLink] = useState<WikiLink | null>(null);
  const [editSubmitting, setEditSubmitting] = useState(false);
  const [editForm] = Form.useForm();

  // 两次请求合并：分类树 + 前 50 页（按 categoryId 客户端挂载）
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [cats, pageList] = await Promise.all([
          listWikiCategoryTree(),
          listWikiPages({ limit: PAGE_LIST_LIMIT }),
        ]);
        if (cancelled) return;
        setTree(cats);
        setAllPages(pageList.rows);
        setPagesTotal(pageList.total);
        setExpandedKeys(allKeys(cats));
      } catch {
        if (!cancelled) message.error(t("wikiLinks.treeLoadFailed"));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [t]);

  // 组装 antd tree data：分类节点 + page 叶子
  const treeData = useMemo(
    () => attachPagesToTree(tree, allPages, t("wikiLinks.uncategorized")),
    [tree, allPages, t],
  );

  const categoryNames = useMemo(() => categoryNameMap(tree), [tree]);

  // Fetch links when page or type changes
  const refresh = useCallback(async () => {
    if (!selectedPageId) {
      setLinks([]);
      return;
    }
    try {
      const rows = await listWikiLinks({ page_id: selectedPageId });
      setLinks(rows);
    } catch {
      message.error(t("wikiLinks.loadFailed"));
    }
  }, [selectedPageId, t]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // 组件卸载时清掉待执行的本体检索防抖
  useEffect(
    () => () => {
      if (linkableTimerRef.current) clearTimeout(linkableTimerRef.current);
    },
    [],
  );

  /** 拉可链接对象。q 为空 ⇒ 默认 50 条；非空 ⇒ 服务端全表检索。 */
  const loadLinkables = useCallback(
    async (q?: string) => {
      const key = q ?? "";
      latestLinkableQRef.current = key;
      setLoadingLinkables(true);
      try {
        const rows = q
          ? await listLinkableTargets(linkType, q)
          : await listLinkableTargets(linkType);
        // 丢弃过期响应：慢的旧请求不得覆盖新结果
        if (latestLinkableQRef.current !== key) return;
        setLinkables(rows);
      } catch {
        if (latestLinkableQRef.current === key) {
          message.error(t("wikiLinks.loadTargetsFailed"));
        }
      } finally {
        if (latestLinkableQRef.current === key) setLoadingLinkables(false);
      }
    },
    [linkType, t],
  );

  const handleLinkableSearch = (raw: string) => {
    const q = raw.trim();
    if (linkableTimerRef.current) clearTimeout(linkableTimerRef.current);
    if (!q) {
      // 清空搜索 ⇒ 立即回到默认 50 条（不要发空串 q）
      void loadLinkables();
      return;
    }
    // 先作废在途旧请求，再防抖发新请求
    latestLinkableQRef.current = q;
    linkableTimerRef.current = setTimeout(() => {
      void loadLinkables(q);
    }, LINKABLE_DEBOUNCE_MS);
  };

  // 选中项始终保留在 options 首位：options 被搜索结果替换后，
  // rc-select 找不到原 option 会把 label 退化成裸 id。
  const allTargets = useMemo(() => {
    const head = selectedTarget ? [selectedTarget] : [];
    const seen = new Set(head.map((x) => x.id));
    return [...head, ...linkables.filter((x) => !seen.has(x.id))];
  }, [linkables, selectedTarget]);

  const targetOptions = allTargets.map((tgt) => ({
    value: tgt.id,
    label: ontologyObjectLabel(tgt.name, tgt.alias, locale),
  }));

  // Open add modal — fetch linkable targets (默认 50 条)
  const openAddModal = async () => {
    try {
      setSelectedTarget(null);
      await loadLinkables();
      setModalOpen(true);
    } catch {
      message.error(t("wikiLinks.loadTargetsFailed"));
    }
  };

  const closeAddModal = () => {
    setModalOpen(false);
    setSelectedTarget(null);
    form.resetFields();
  };

  // Submit new link
  const submit = async (values: Record<string, unknown>) => {
    if (!selectedPageId) return;
    setSubmitting(true);
    try {
      await createWikiLink({
        page_id: selectedPageId,
        ontology_type: linkType,
        ontology_id: values.ontology_id as number,
        weight: values.weight as number,
        chunk_id:
          values.scope === "chunk" ? (values.chunk_id as string) : null,
        note: (values.note as string) || null,
      });
      message.success(t("wikiLinks.addSuccess"));
      setModalOpen(false);
      setSelectedTarget(null);
      form.resetFields();
      refresh();
    } catch {
      message.error(t("wikiLinks.addFailed"));
    } finally {
      setSubmitting(false);
    }
  };

  // Revoke a link
  const revoke = async (id: number) => {
    try {
      await revokeWikiLink(id);
      message.success(t("wikiLinks.revokeSuccess"));
      refresh();
    } catch {
      message.error(t("wikiLinks.revokeFailed"));
    }
  };

  // Edit weight / note（PATCH；note 传空串表示清空）
  const submitEdit = async () => {
    if (!editingLink) return;
    const values = await editForm.validateFields();
    setEditSubmitting(true);
    try {
      await updateWikiLink(editingLink.id, {
        weight: values.weight as number,
        note: ((values.note as string | undefined) ?? "").trim(),
      });
      message.success(t("wikiLinks.updateSuccess"));
      setEditingLink(null);
      refresh();
    } catch {
      message.error(t("wikiLinks.updateFailed"));
    } finally {
      setEditSubmitting(false);
    }
  };

  /** 全量页面检索。空/纯空白 ⇒ 不发请求、作废在途请求、恢复分类树。 */
  const runPageSearch = async (raw: string) => {
    const q = raw.trim();
    setPageSearchQuery(q);
    if (!q) {
      pageSearchSeqRef.current += 1;
      setPageSearchResults([]);
      setPageSearchTotal(0);
      setPageSearching(false);
      return;
    }
    const seq = ++pageSearchSeqRef.current;
    setPageSearching(true);
    try {
      const res = await searchWikiPages({ query: q, limit: PAGE_LIST_LIMIT });
      if (seq !== pageSearchSeqRef.current) return;
      setPageSearchResults(res.rows);
      setPageSearchTotal(res.total);
    } catch {
      if (seq === pageSearchSeqRef.current) {
        message.error(t("wikiLinks.searchFailed"));
      }
    } finally {
      if (seq === pageSearchSeqRef.current) setPageSearching(false);
    }
  };

  const filteredLinks = links.filter((l) => l.ontology_type === linkType);

  return (
    <div style={{ padding: 24 }}>
      <h2>{t("wikiLinks.title")}</h2>
      <div style={{ display: "flex", gap: 16, marginTop: 16 }}>
        {/* Left: wiki page tree / search results */}
        <div style={{ width: 320, border: "1px solid #f0f0f0", padding: 8 }}>
          <div
            style={{ marginBottom: 8, color: "#666", fontSize: 12 }}
          >
            {t("wikiLinks.pageTreeLabel")}
          </div>
          <Input.Search
            value={pageSearchInput}
            onChange={(e) => {
              const next = e.target.value;
              setPageSearchInput(next);
              // 清空输入 ⇒ 立即恢复分类树
              if (!next.trim() && pageSearchQuery) {
                void runPageSearch("");
              }
            }}
            onSearch={(v) => void runPageSearch(v)}
            placeholder={t("wikiLinks.pageSearchPlaceholder")}
            allowClear
            loading={pageSearching}
            style={{ marginBottom: 8 }}
          />

          {pageSearchQuery ? (
            <div>
              <div style={{ color: "#666", fontSize: 12, marginBottom: 8 }}>
                {t("wikiLinks.searchResultCount", { total: pageSearchTotal })}
              </div>
              {pageSearchResults.length === 0 ? (
                <div style={{ color: "#999", padding: "12px 0" }}>
                  {t("wikiLinks.noSearchResults")}
                </div>
              ) : (
                pageSearchResults.map((p) => (
                  <div
                    key={p.pageId}
                    role="button"
                    tabIndex={0}
                    onClick={() => setSelectedPageId(p.pageId)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") setSelectedPageId(p.pageId);
                    }}
                    style={{
                      cursor: "pointer",
                      padding: "6px 8px",
                      borderBottom: "1px solid #f5f5f5",
                      background:
                        p.pageId === selectedPageId ? "#e6f4ff" : undefined,
                    }}
                  >
                    <div>{p.title}</div>
                    <Tag style={{ marginTop: 4 }}>
                      {p.categoryId != null
                        ? categoryNames.get(p.categoryId) ??
                          t("wikiLinks.uncategorized")
                        : t("wikiLinks.uncategorized")}
                    </Tag>
                  </div>
                ))
              )}
            </div>
          ) : (
            <>
              {pagesTotal > allPages.length && (
                <div
                  style={{ color: "#999", fontSize: 12, marginBottom: 8 }}
                >
                  {t("wikiLinks.treeLimitHint", {
                    shown: allPages.length,
                    total: pagesTotal,
                  })}
                </div>
              )}
              <Tree
                treeData={treeData}
                expandedKeys={expandedKeys}
                onExpand={(keys) => setExpandedKeys(keys as string[])}
                selectedKeys={selectedPageId ? [selectedPageId] : []}
                onSelect={(keys) => {
                  const k = keys[0] as string | undefined;
                  // 分类节点（cat- 前缀）不可点；只有 page 叶子更新 selectedPageId
                  if (k && !k.startsWith(CATEGORY_KEY_PREFIX)) {
                    setSelectedPageId(k);
                  }
                }}
              />
            </>
          )}
        </div>

        {/* Right: links panel */}
        <div style={{ flex: 1 }}>
          <Tabs
            activeKey={linkType}
            onChange={(k) => {
              setLinkType(k as WikiLinkType);
              // 切类型时清掉上一类型的选中项，避免跨类型 id 残留
              setSelectedTarget(null);
              form.setFieldValue("ontology_id", undefined);
            }}
            items={[
              { key: "class", label: t("wikiLinks.tabs.class") },
              { key: "property", label: t("wikiLinks.tabs.property") },
              { key: "metric", label: t("wikiLinks.tabs.metric") },
            ]}
          />
          <Space style={{ marginBottom: 12 }}>
            <Button
              type="primary"
              onClick={openAddModal}
              disabled={!selectedPageId}
            >
              + {t("wikiLinks.addBinding")}
            </Button>
          </Space>

          {filteredLinks.length === 0 ? (
            <div style={{ color: "#999", padding: "24px 0" }}>
              {selectedPageId
                ? t("wikiLinks.noLinks")
                : t("wikiLinks.selectPageHint")}
            </div>
          ) : (
            filteredLinks.map((link) => (
              <div
                key={link.id}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 8,
                  padding: "8px 12px",
                  borderBottom: "1px solid #f0f0f0",
                }}
              >
                <Tag color={link.ontology_type === "class" ? "blue" : "green"}>
                  {t(TYPE_LABEL_KEY[link.ontology_type])}
                </Tag>
                <span>
                  {link.ontology_name
                    ? ontologyObjectLabel(link.ontology_name, link.ontology_alias, locale)
                    : `ID:${link.ontology_id}`}
                </span>
                <span style={{ color: "#666" }}>
                  weight={link.weight.toFixed(2)}
                </span>
                {link.note && (
                  <span style={{ color: "#999", fontSize: 12 }}>
                    {link.note}
                  </span>
                )}
                <Space style={{ marginLeft: "auto" }}>
                  <Button
                    size="small"
                    onClick={() => setEditingLink(link)}
                  >
                    {t("wikiLinks.edit")}
                  </Button>
                  <Button
                    danger
                    size="small"
                    onClick={() => revoke(link.id)}
                  >
                    {t("wikiLinks.revoke")}
                  </Button>
                </Space>
              </div>
            ))
          )}
        </div>
      </div>

      {/* Add link modal */}
      <Modal
        title={t("wikiLinks.addBinding")}
        open={modalOpen}
        onCancel={closeAddModal}
        footer={null}
      >
        <Form form={form} onFinish={submit} layout="vertical">
          <Form.Item name="scope" label={t("wikiLinks.scopeLabel")} initialValue="page">
            <Radio.Group>
              <Radio value="page">{t("wikiLinks.scopePage")}</Radio>
              <Radio value="chunk">{t("wikiLinks.scopeChunk")}</Radio>
            </Radio.Group>
          </Form.Item>

          <Form.Item
            noStyle
            shouldUpdate={(prev, curr) => prev.scope !== curr.scope}
          >
            {({ getFieldValue }) =>
              getFieldValue("scope") === "chunk" ? (
                <Form.Item
                  name="chunk_id"
                  label={t("wikiLinks.chunkIdLabel")}
                  rules={[
                    { required: true, message: t("wikiLinks.chunkIdRequired") },
                  ]}
                >
                  <Input placeholder={t("wikiLinks.chunkIdPlaceholder")} />
                </Form.Item>
              ) : null
            }
          </Form.Item>

          <Form.Item
            name="ontology_id"
            label={t("wikiLinks.ontologyObject")}
            rules={[
              { required: true, message: t("wikiLinks.ontologyRequired") },
            ]}
          >
            <Select
              options={targetOptions}
              placeholder={t("wikiLinks.ontologyPlaceholder")}
              showSearch
              filterOption={false}
              onSearch={handleLinkableSearch}
              loading={loadingLinkables}
              onChange={(id: number) =>
                setSelectedTarget(allTargets.find((x) => x.id === id) ?? null)
              }
            />
          </Form.Item>

          <Form.Item
            name="weight"
            label={t("wikiLinks.weight")}
            initialValue={1.0}
          >
            <Slider min={0} max={1} step={0.1} />
          </Form.Item>

          <Form.Item name="note" label={t("wikiLinks.note")}>
            <Input.TextArea maxLength={200} rows={3} />
          </Form.Item>

          <Form.Item style={{ marginBottom: 0 }}>
            <Space>
              <Button type="primary" htmlType="submit" loading={submitting}>
                {t("wikiLinks.submit")}
              </Button>
              <Button onClick={closeAddModal}>{t("wikiLinks.cancel")}</Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>

      {/* Edit weight / note modal（对象与作用范围只读展示，不允许改） */}
      <Modal
        title={t("wikiLinks.editBinding")}
        open={editingLink !== null}
        onCancel={() => setEditingLink(null)}
        onOk={() => void submitEdit()}
        confirmLoading={editSubmitting}
        okText={t("wikiLinks.submit")}
        cancelText={t("wikiLinks.cancel")}
        destroyOnHidden
        afterOpenChange={(open) => {
          if (open && editingLink) {
            editForm.setFieldsValue({
              weight: editingLink.weight,
              note: editingLink.note ?? "",
            });
          }
        }}
      >
        {editingLink && (
          <div style={{ marginBottom: 12, color: "#666" }}>
            <Tag color={editingLink.ontology_type === "class" ? "blue" : "green"}>
              {t(TYPE_LABEL_KEY[editingLink.ontology_type])}
            </Tag>
            <span>
              {editingLink.ontology_name
                ? ontologyObjectLabel(
                    editingLink.ontology_name,
                    editingLink.ontology_alias,
                    locale,
                  )
                : `ID:${editingLink.ontology_id}`}
            </span>
            <span style={{ marginLeft: 8 }}>
              {editingLink.chunk_id
                ? `${t("wikiLinks.scopeChunk")}: ${editingLink.chunk_id}`
                : t("wikiLinks.scopePage")}
            </span>
          </div>
        )}
        <Form form={editForm} layout="vertical" preserve={false}>
          <Form.Item name="weight" label={t("wikiLinks.weight")}>
            <Slider min={0} max={1} step={0.1} />
          </Form.Item>
          <Form.Item name="note" label={t("wikiLinks.note")}>
            <Input.TextArea maxLength={200} rows={3} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
