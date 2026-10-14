/** 历史会话管理（独立路由 /research/history）。
 *
 * 与 ResearchListPage 拆开：列表页只做「发起新研究」，本页面承载「历史会话
 * 全部」——查看、多选、删除、对比。主内容区是研究列表，无右侧 Drawer。
 *
 * 多选 / 删除 / 对比都用 store 的 sessions 缓存 + loadSessions。本页加载
 * loadSessions 一次（mount 时），后续打开即吃缓存（与 ResearchListPage
 * 不再重复打接口）。
 */
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { App, Button, Checkbox, Empty, List, Popconfirm, Space, Tag, Typography } from "antd";
import { ArrowLeftOutlined, DeleteOutlined } from "@ant-design/icons";
import { useTranslation } from "react-i18next";
import { useResearchStore } from "../../stores/researchStore";
import { useDatasourceOptions, datasourceName } from "../../hooks/useDatasourceOptions";
import type { DataSource } from "../../types/datasource";
import type { ResearchSession } from "../../types/research";

interface SessionDeleteActionProps {
  sessionId: string;
  rowLabel: string;
  onDelete: (id: string) => void;
}

/** 行内删除动作（二次确认）。可访问名必须带行标识 —— 扁平 aria-label 会让每行同名，
 *  读屏用户无法区分删的是哪条；`rowLabel` 与行内可见标题同源（title 空则回落 question）。 */
function SessionDeleteAction({ sessionId, rowLabel, onDelete }: SessionDeleteActionProps) {
  const { t } = useTranslation();
  return (
    <Popconfirm
      title={t("research.list.deleteConfirm")}
      okText={t("common.confirm")}
      cancelText={t("common.cancel")}
      onConfirm={() => onDelete(sessionId)}
    >
      <Button
        type="link"
        danger
        icon={<DeleteOutlined />}
        aria-label={t("research.list.deleteAriaLabel", { name: rowLabel })}
        onClick={(event) => event.stopPropagation()}
      >
        {t("common.delete")}
      </Button>
    </Popconfirm>
  );
}

interface SessionRowProps {
  session: ResearchSession;
  sources: DataSource[];
  selected: boolean;
  onToggleSelect: (id: string) => void;
  onDelete: (id: string) => void;
  onOpen: (id: string) => void;
}

/** 单条会话行：复选框 + 标题/问题 + 数据源 Tag + 模式 Tag + 三动作。点行体也可打开会话。 */
function SessionRow({ session, sources, selected, onToggleSelect, onDelete, onOpen }: SessionRowProps) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  // 行标识与行内可见标题同源：既作复选框标签，也作删除按钮的可访问名。同时保证逐行唯一，
  // 扁平名会让每一行的删除按钮同名。
  const rowLabel = session.title || session.question;
  // 数据源名每行只算一次：守卫与 Tag 共用同一结果。
  const sourceLabel = datasourceName(sources, session.datasourceId);

  return (
    <List.Item
      // 点行体（除按钮/复选框外）打开会话：行体是整行大按钮，行为更直观。
      // 守卫：行内按钮 / 复选框的 click event 也会冒泡到 List.Item，这里主动
      // 排除 —— 比让每个子组件都 stopPropagation 健壮（少一个漏 stopPropagation
      // 就多一个 bug）。Portal 里 Popconfirm 的确定/取消按钮 click 不会冒泡
      // 到这里（portal 在 body 直系下），所以不影响删除确认。
      onClick={(event) => {
        const target = event.target as HTMLElement | null;
        if (target?.closest("button, .ant-checkbox-wrapper, a, [role='button']")) return;
        onOpen(session.id);
      }}
      style={{ cursor: "pointer" }}
      actions={[
        <Button
          key="open"
          type="link"
          onClick={(event) => {
            event.stopPropagation();
            onOpen(session.id);
          }}
        >
          {t("research.list.open")}
        </Button>,
        <Button
          key="report"
          type="link"
          onClick={(event) => {
            event.stopPropagation();
            navigate(`/research/${session.id}/report`);
          }}
        >
          {t("research.list.report")}
        </Button>,
        <SessionDeleteAction
          key="delete"
          sessionId={session.id}
          rowLabel={rowLabel}
          onDelete={onDelete}
        />,
      ]}
    >
      <Checkbox
        checked={selected}
        onChange={(event) => {
          event.stopPropagation();
          onToggleSelect(session.id);
        }}
        aria-label={rowLabel}
      />
      <List.Item.Meta title={rowLabel} description={session.question} />
      {sourceLabel ? <Tag>{sourceLabel}</Tag> : null}
      <Tag>{t(`research.list.mode.${session.mode}`)}</Tag>
    </List.Item>
  );
}

interface SessionListProps {
  sessions: ResearchSession[];
  loading: boolean;
  sources: DataSource[];
  selectedIds: string[];
  onToggleSelect: (id: string) => void;
  onDelete: (id: string) => void;
  onOpen: (id: string) => void;
}

/** 会话列表容器（空态文案由 locale 提供）。 */
function SessionList({ sessions, loading, sources, selectedIds, onToggleSelect, onDelete, onOpen }: SessionListProps) {
  const { t } = useTranslation();
  return (
    <List
      loading={loading}
      dataSource={sessions}
      locale={{ emptyText: <Empty description={t("research.list.empty")} /> }}
      renderItem={(session) => (
        <SessionRow
          session={session}
          sources={sources}
          selected={selectedIds.includes(session.id)}
          onToggleSelect={onToggleSelect}
          onDelete={onDelete}
          onOpen={onOpen}
        />
      )}
    />
  );
}

interface SessionSelection {
  selectedIds: string[];
  toggleSelect: (id: string) => void;
  removeSession: (id: string) => Promise<void>;
}

/** 多选与删除。删除成功后必须把该 id 移出多选集合，否则「对比」会带上一个不存在的会话。 */
function useSessionSelection(): SessionSelection {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const deleteSession = useResearchStore((s) => s.deleteSession);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);

  const toggleSelect = (id: string) => {
    setSelectedIds((prev) =>
      prev.includes(id) ? prev.filter((item) => item !== id) : [...prev, id],
    );
  };

  const removeSession = async (id: string) => {
    try {
      await deleteSession(id);
      setSelectedIds((prev) => prev.filter((item) => item !== id));
    } catch {
      message.error(t("research.list.deleteError"));
    }
  };

  return { selectedIds, toggleSelect, removeSession };
}

export default function ResearchHistoryPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const sessions = useResearchStore((s) => s.sessions);
  const sessionsLoading = useResearchStore((s) => s.sessionsLoading);
  const loadSessions = useResearchStore((s) => s.loadSessions);
  const sources = useDatasourceOptions();
  const { selectedIds, toggleSelect, removeSession } = useSessionSelection();

  // 进入历史页：第一次 mount 拉一次列表（store 缓存为空时），后续进直接吃缓存。
  useEffect(() => {
    if (sessions.length === 0 && !sessionsLoading) {
      void loadSessions();
    }
  }, [sessions.length, sessionsLoading, loadSessions]);

  const openCompare = () => {
    if (selectedIds.length < 2) return;
    navigate(`/research/compare?ids=${selectedIds.join(",")}`);
  };

  const openSession = (sessionId: string) => {
    navigate(`/research/${sessionId}`);
  };

  return (
    <div style={{ padding: 16 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
        <Space>
          {/* 返回列表页（发起新研究）。点「开始研究」或这里都能回到 /research。 */}
          <Button
            icon={<ArrowLeftOutlined />}
            onClick={() => navigate("/research")}
          >
            {t("research.history.back")}
          </Button>
          <Typography.Title level={4} style={{ margin: 0 }}>
            {t("research.history.title")}
          </Typography.Title>
        </Space>
        <Button
          type="primary"
          disabled={selectedIds.length < 2}
          onClick={openCompare}
        >
          {t("research.list.compare")}
        </Button>
      </div>
      <SessionList
        sessions={sessions}
        loading={sessionsLoading}
        sources={sources}
        selectedIds={selectedIds}
        onToggleSelect={toggleSelect}
        onDelete={removeSession}
        onOpen={openSession}
      />
    </div>
  );
}
