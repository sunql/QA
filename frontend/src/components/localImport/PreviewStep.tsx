import { useMemo, useState } from "react";
import { Alert, Button, Input, Space, Table, Tag, Tooltip, Typography } from "antd";
import type { TableColumnsType, TableProps } from "antd";
import { useTranslation } from "../../i18n";
import type {
  ImportConflict,
  ImportExecuteRequest,
  ImportPreviewResponse,
  ProposedClass,
  ProposedJoin,
} from "../../types/localImport";
import { buildExecuteRequest, joinKey, validJoins } from "./importSelection";

const { Text } = Typography;

interface PreviewStepProps {
  preview: ImportPreviewResponse;
  onExecute: (request: ImportExecuteRequest) => void;
}

/** 类冲突（属性冲突也带 sourceTable，不能混进类冲突索引里）。 */
function isClassConflict(conflict: ImportConflict): boolean {
  return conflict.type === "class" || conflict.type === "class_tombstoned";
}

/** 占名三态：
 * - name：类名被占 ⇒ createClass 必然拒 ⇒ 禁勾；
 * - table：仅源表相同 ⇒ 类名没被占 ⇒ 导入能成功 ⇒ 只提示；
 * - unknown：后端没给 nameCollision（灰度期）⇒ 不下结论，执行期再确认。
 */
type CollisionState = "name" | "table" | "unknown";

function collisionState(conflict: ImportConflict): CollisionState {
  if (conflict.nameCollision === true) return "name";
  if (conflict.nameCollision === false) return "table";
  return "unknown";
}

/** 按当前语言格式化时间；非法时间戳 / 非法 locale 都不能炸掉整张表。 */
function formatWhen(iso: string | null | undefined, locale: string): string {
  if (!iso) return "—";
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return "—";
  try {
    return parsed.toLocaleString(locale);
  } catch {
    return parsed.toLocaleString();
  }
}

// 预览确认：分页展示 proposedClasses，支持按表名/类名搜索、勾选子集分批导入。
// 关联关系单独一节：每条 join 一个勾选框，两端都被勾选的类才可选；可整表开关。
// 已选类对应 join 默认全部勾上，落库前由 buildExecuteRequest 收敛为「两端都选且勾选」的边。
//
// fix-class-tombstone-restore：preview.conflicts 中含 type="class_tombstoned"
// 的项触发顶部 Alert（不可关闭），列出已被同名软删除的源表名 + 软删时间。
//
// fix-import-tombstone-name-guard：判重口径对齐 createClass —— 它只按
// class_name 判重（**活类与墓碑一视同仁**）⇒ 占住类名的行在执行期必然被拒。
// 预览期对**占名**的行直接禁勾 + 就地标注占用者（id + 删除时间 + 处置指引），
// 把死结变成可执行的下一步。
// 仅源表相同（类名未被占）的行只提示、不禁勾 ⇒ 导入其实能成功，不该被堵死。
export default function PreviewStep({ preview, onExecute }: PreviewStepProps) {
  const { t, locale } = useTranslation();
  const [searchText, setSearchText] = useState("");
  const [selectedRowKeys, setSelectedRowKeys] = useState<string[]>([]);
  // 被用户手动关掉的 join（joinKey 集合）。有效集合 = 两端已选 join − 此排除集。
  const [excludedJoinKeys, setExcludedJoinKeys] = useState<ReadonlySet<string>>(
    () => new Set<string>()
  );

  // 类冲突（活类 + 墓碑）。
  const classConflicts = useMemo<ImportConflict[]>(
    () => preview.conflicts.filter(isClassConflict),
    [preview.conflicts]
  );
  // 冲突 → 提议行：**按提议表名精确对齐**（后端契约：类冲突的 source_table 恒为
  // 提议的那张表）。表名与类名分属两个命名空间、分别索引 ⇒ 大小写不同的两张表
  // 不会互相牵连（混成一个小写 key 空间 + 先到先得会把本可成功的行误禁掉）。
  const conflictByTable = useMemo<ReadonlyMap<string, ImportConflict>>(() => {
    const map = new Map<string, ImportConflict>();
    classConflicts.forEach((c) => {
      const key = c.sourceTable ?? "";
      if (key.length > 0 && !map.has(key)) map.set(key, c);
    });
    return map;
  }, [classConflicts]);
  // 防御性兜底：按**提议类名**对齐（仍是精确匹配，不折叠大小写）。
  // ⚠️ 当前后端契约下 `conflicts[].source_table` 恒为提议表（且非空）⇒
  // 这个 Map 实际不会被命中；保留只为「契约若变（source_table 可空）」时不炸。
  const conflictByProposedName = useMemo<ReadonlyMap<string, ImportConflict>>(() => {
    const map = new Map<string, ImportConflict>();
    classConflicts.forEach((c) => {
      const key = c.proposedName ?? "";
      if (key.length > 0 && !map.has(key)) map.set(key, c);
    });
    return map;
  }, [classConflicts]);

  /** 找出行对应的类冲突（先按提议表名，再按提议类名） */
  const findConflict = (record: ProposedClass): ImportConflict | undefined =>
    conflictByTable.get(record.sourceTable) ??
    conflictByProposedName.get(record.className);

  /** 阻断的行（禁勾 + 排除出「全选当前筛选」）：nameCollision===true ⇒ createClass 必然拒。 */
  const blockedRowKeys = useMemo<ReadonlySet<string>>(() => {
    const set = new Set<string>();
    preview.proposedClasses.forEach((row) => {
      const conflict = findConflict(row);
      if (conflict?.nameCollision === true) set.add(row.sourceTable);
    });
    return set;
  }, [preview.proposedClasses, conflictByTable, conflictByProposedName]);

  /** 墓碑冲突（顶部 Alert + 墓碑专属文案）。 */
  const tombstonedConflicts = useMemo<ImportConflict[]>(
    () => classConflicts.filter((c) => c.type === "class_tombstoned"),
    [classConflicts]
  );
  /** 活类占名（顶部 Alert + 活类专属文案；文案不能复用墓碑那套）。 */
  const liveNameCollisions = useMemo<ImportConflict[]>(
    () => classConflicts.filter((c) => c.type === "class" && c.nameCollision === true),
    [classConflicts]
  );

  // 搜索为展示层过滤：不改变选中集合，也不影响最终提交范围。
  const filteredClasses = useMemo(() => {
    const query = searchText.trim().toLowerCase();
    if (!query) return preview.proposedClasses;
    return preview.proposedClasses.filter(
      (c) =>
        c.sourceTable.toLowerCase().includes(query) ||
        c.className.toLowerCase().includes(query),
    );
  }, [preview.proposedClasses, searchText]);

  // preserveSelectedRowKeys：搜索/翻页移除当前页后，已勾选的行仍保留在选中集合内。
  // selections 下拉跨页作用于完整 dataSource（过滤后的全部类）。首项复用工具栏
  // 「全选当前筛选」的累加语义：分批导入时并入当前筛选类，而不像 antd 内置
  // SELECTION_ALL 那样「替换为当前筛选集」——否则会静默丢掉此前批次勾选的类。
  const rowSelection: TableProps<ProposedClass>["rowSelection"] = {
    selectedRowKeys,
    // 占名行禁勾：这类行的类名已被别的类占住（活类或墓碑），执行期 createClass
    // 必然拒；允许勾选只会把失败推迟到执行期。
    // onChange 再滤一次 = 纵深防御（即便集合里混进了占名键也不提交）。
    onChange: (keys) =>
      setSelectedRowKeys(keys.map(String).filter((key) => !blockedRowKeys.has(key))),
    preserveSelectedRowKeys: true,
    getCheckboxProps: (record) => {
      const conflict = findConflict(record);
      // 只禁勾「占类名」的行：与 createClass 的拒绝条件一致。
      // 仅源表相同的不禁勾（导入其实能成功）。
      if (!conflict || conflict.nameCollision !== true) return {};
      const occupant =
        conflict.existingName ?? conflict.proposedName ?? record.className;
      const isTombstone = conflict.type === "class_tombstoned";
      return {
        disabled: true,
        title: isTombstone
          ? t("localImport.preview.tombstoneCheckboxDisabled", {
              name: occupant,
              id: conflict.existingId,
            })
          : t("localImport.preview.nameTakenCheckboxDisabled", {
              name: occupant,
              id: conflict.existingId,
            }),
      };
    },
    selections: [
      {
        key: "select-all-filtered",
        text: t("localImport.preview.selectFiltered"),
        onSelect: () => handleSelectFiltered(),
      },
      Table.SELECTION_INVERT,
      Table.SELECTION_NONE,
    ],
  };

  const columns: TableColumnsType<ProposedClass> = [
    { title: t("localImport.preview.sourceTable"), dataIndex: "sourceTable" },
    { title: t("localImport.preview.className"), dataIndex: "className" },
    {
      title: t("localImport.preview.propertyCount"),
      key: "propertyCount",
      render: (_, record) => record.properties.length,
    },
    {
      // 占用状态就地可见：不必去「本体管理」页翻「显示已删除」才知道是谁占了名。
      title: t("localImport.preview.status"),
      key: "status",
      render: (_, record) => {
        const conflict = findConflict(record);
        if (!conflict) return <Text type="secondary">—</Text>;
        const state = collisionState(conflict);
        const isTombstone = conflict.type === "class_tombstoned";
        const when = formatWhen(conflict.existingValidTo, locale);
        const occupant = conflict.existingName ?? conflict.proposedName ?? "";
        const occupantId = conflict.existingId;
        // 占名 ⇒ 硬冲突（禁勾）；仅同源表 ⇒ 仅提示（导入可成功）；
        // unknown（后端未判定）⇒ 不下结论，执行期再确认。
        if (state === "name") {
          return (
            <Tooltip
              title={
                isTombstone
                  ? t("localImport.preview.tombstoneTooltip", {
                      name: occupant,
                      id: occupantId,
                      when,
                    })
                  : t("localImport.preview.nameTakenTooltip", {
                      name: occupant,
                      id: occupantId,
                    })
              }
            >
              <span>
                <Tag color="warning">
                  {isTombstone
                    ? t("localImport.preview.tombstoneTag")
                    : t("localImport.preview.nameTakenTag")}
                </Tag>
                <Text type="secondary" style={{ marginInlineStart: 8 }}>
                  {isTombstone
                    ? t("localImport.preview.tombstoneRowHint", {
                        id: occupantId,
                        when,
                      })
                    : t("localImport.preview.nameTakenRowHint", { id: occupantId })}
                </Text>
              </span>
            </Tooltip>
          );
        }
        // 活类：仅撞表 ⇒ 类名没被占 ⇒ 导入能成功 ⇒ 不提示（沿用既有行为）。
        if (!isTombstone) {
          // ⚠️ 活类 + unknown（后端未升级 ⇒ 没给 nameCollision）⇒ 不能像墓碑那样
          // 落到「—」上装死：这一行**可能**在执行期 422（活类也可能占名）。
          // ⇒ 给中性提示（既不断言占名，也不承诺可导入）。
          if (state !== "unknown") return <Text type="secondary">—</Text>;
          return (
            <Tooltip
              title={t("localImport.preview.nameTakenUnknownTooltip", {
                name: occupant,
                id: occupantId,
              })}
            >
              <span>
                <Tag color="default">
                  {t("localImport.preview.nameTakenUnknownTag")}
                </Tag>
                <Text type="secondary" style={{ marginInlineStart: 8 }}>
                  {t("localImport.preview.nameTakenRowHint", { id: occupantId })}
                </Text>
              </span>
            </Tooltip>
          );
        }
        return (
          <Tooltip
            title={
              state === "table"
                ? t("localImport.preview.tombstoneTableOnlyTooltip", {
                    name: occupant,
                    id: occupantId,
                    when,
                  })
                : t("localImport.preview.tombstoneUnknownTooltip", {
                    name: occupant,
                    id: occupantId,
                    when,
                  })
            }
          >
            <span>
              <Tag color="default">
                {t("localImport.preview.tombstoneTableOnlyTag")}
              </Tag>
              <Text type="secondary" style={{ marginInlineStart: 8 }}>
                {t("localImport.preview.tombstoneTableOnlyHint", {
                  id: occupantId,
                  when,
                })}
              </Text>
            </span>
          </Tooltip>
        );
      },
    },
  ];

  // 累计语义：把当前筛选命中的行并入已选集合（分批导入时按子集逐批勾选）。
  // 取消选中用表头 checkbox（对当前筛选集 toggle）。
  // 只跳过「占类名」的行：执行期 createClass 判重会拒，在这里直接过滤，
  // 避免用户选了一个注定会失败的目标。
  // 仅源表相同的**不跳过** ⇒ 导入其实能成功。
  const handleSelectFiltered = () => {
    setSelectedRowKeys((prev) => {
      const next = new Set(prev);
      filteredClasses.forEach((c) => {
        if (!blockedRowKeys.has(c.sourceTable)) next.add(c.sourceTable);
      });
      return Array.from(next);
    });
  };

  const selectedClassSet = useMemo(
    () => new Set(selectedRowKeys),
    [selectedRowKeys],
  );

  // 两端都在已选类内的 join（可勾选导入）。其余照常展示但禁用，说明缺端。
  const valid = useMemo(
    () => validJoins(preview.proposedJoins, selectedClassSet),
    [preview.proposedJoins, selectedClassSet],
  );

  // 受控勾选集：validKeys − excludedJoinKeys（排除集中已失效的键被自然忽略）。
  const checkedJoinKeys = useMemo(() => {
    const result: string[] = [];
    valid.forEach((j) => {
      const key = joinKey(j);
      if (!excludedJoinKeys.has(key)) result.push(key);
    });
    return result;
  }, [valid, excludedJoinKeys]);

  const isJoinDisabled = (j: ProposedJoin) =>
    !selectedClassSet.has(j.sourceTable) || !selectedClassSet.has(j.targetTable);

  const joinRowSelection: TableProps<ProposedJoin>["rowSelection"] = {
    selectedRowKeys: checkedJoinKeys,
    onChange: (keys) => {
      // 反推出本次「仍被排除」的键：所有 valid 键中不在新选中集合里的。
      const selected = new Set(keys.map(String));
      const nextExcluded = new Set<string>();
      valid.forEach((j) => {
        const key = joinKey(j);
        if (!selected.has(key)) nextExcluded.add(key);
      });
      setExcludedJoinKeys(nextExcluded);
    },
    getCheckboxProps: (j) => ({ disabled: isJoinDisabled(j) }),
  };

  const joinColumns: TableColumnsType<ProposedJoin> = [
    {
      title: t("localImport.preview.joinSource"),
      key: "source",
      render: (_, j) => (
        <Text code>
          {j.sourceTable}.{j.sourceColumns.join("+")}
        </Text>
      ),
    },
    {
      title: t("localImport.preview.joinTarget"),
      key: "target",
      render: (_, j) => (
        <Text code>
          {j.targetTable}.{j.targetColumns.join("+")}
        </Text>
      ),
    },
    {
      title: t("localImport.preview.joinType"),
      dataIndex: "joinType",
      width: 90,
      render: (v: string) => <Tag>{v}</Tag>,
    },
    {
      title: t("localImport.preview.relationType"),
      dataIndex: "relationType",
      width: 100,
      render: (v: string) => <Tag>{v}</Tag>,
    },
    {
      title: t("localImport.preview.inferredBy"),
      key: "inferredBy",
      width: 120,
      render: (_, j) => {
        if (j.inferredBy === "name_convention") {
          return <Tag color="purple">{t("localImport.preview.inferredByNameConvention")}</Tag>;
        }
        if (j.inferredBy === "declared_fk") {
          return <Tag color="blue">{t("localImport.preview.inferredByDeclaredFk")}</Tag>;
        }
        return <Text type="secondary">—</Text>;
      },
    },
  ];

  const handleConfirm = () => {
    // 最终启用集 = checkedJoinKeys（valid 中未被用户排除的键）。
    const effectiveKeys = new Set(checkedJoinKeys);
    onExecute(buildExecuteRequest(preview, selectedClassSet, effectiveKeys));
  };

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      {tombstonedConflicts.length > 0 && (
        <Alert
          type="warning"
          showIcon
          closable={false}
          data-testid="tombstoned-alert"
          message={t("localImport.preview.tombstonedAlert")}
          description={
            <ul style={{ margin: 0, paddingInlineStart: 18 }}>
              {tombstonedConflicts.map((c) => {
                const state = collisionState(c);
                const when = formatWhen(c.existingValidTo, locale);
                return (
                  <li key={`${c.type}-${c.existingId}-${c.sourceTable ?? ""}`}>
                    <Text code>{c.sourceTable}</Text>
                    {/* 只在**真占名**时说「占用类名 X（id=N）」。
                        ⚠️ 不要加 `existingName !== proposedName` 这类条件：
                        name_collision=true 的占用者**必然**来自按类名命中
                        （_indexByName）⇒ occupant.class_name == 提议类名 ⇒
                        该条件恒为假 ⇒ 分支不可达 ⇒ 配套用例是**假绿**。
                        「去恢复哪个类」的可执行线索是 **id**（不是另一个类名），
                        所以这里把 id 一并说出来。 */}
                    {state === "name" && c.existingName ? (
                      <Text type="secondary">
                        {t("localImport.preview.tombstonedAlertOccupiedBy", {
                          name: c.existingName,
                          id: c.existingId,
                        })}
                      </Text>
                    ) : null}
                    {state === "table" ? (
                      <Text type="secondary">
                        {t("localImport.preview.tombstonedAlertTableOnly")}
                      </Text>
                    ) : null}
                    {state === "unknown" ? (
                      <Text type="secondary">
                        {t("localImport.preview.tombstonedAlertUnknown")}
                      </Text>
                    ) : null}
                    {c.existingValidTo
                      ? t("localImport.preview.tombstonedAlertItem", { when })
                      : null}
                  </li>
                );
              })}
            </ul>
          }
        />
      )}
      {liveNameCollisions.length > 0 && (
        <Alert
          type="warning"
          showIcon
          closable={false}
          data-testid="name-taken-alert"
          message={t("localImport.preview.nameTakenAlert")}
          description={
            <ul style={{ margin: 0, paddingInlineStart: 18 }}>
              {liveNameCollisions.map((c) => (
                <li key={`${c.type}-${c.existingId}-${c.sourceTable ?? ""}`}>
                  <Text code>{c.sourceTable}</Text>
                  <Text type="secondary">
                    {t("localImport.preview.nameTakenAlertItem", {
                      name: c.existingName ?? "",
                      id: c.existingId,
                    })}
                  </Text>
                </li>
              ))}
            </ul>
          }
        />
      )}
      <Space>
        <Input
          allowClear
          placeholder={t("localImport.preview.searchPlaceholder")}
          style={{ width: 260 }}
          onChange={(e) => setSearchText(e.target.value)}
        />
        <Button onClick={handleSelectFiltered}>
          {t("localImport.preview.selectFiltered")}
        </Button>
        <span>
          {t("localImport.preview.selectedCount", {
            count: selectedRowKeys.length,
            total: preview.proposedClasses.length,
          })}
        </span>
      </Space>
      <div data-testid="classTable">
        <Table
          rowKey="sourceTable"
          dataSource={filteredClasses}
          columns={columns}
          rowSelection={rowSelection}
          // defaultPageSize 仅作初始值；受控 pageSize 会让 size changer 改动被 props 压回（no-op）。
          pagination={{ defaultPageSize: 50, showSizeChanger: true }}
          size="small"
        />
      </div>

      {preview.proposedJoins.length > 0 && (
        <>
          <Space wrap>
            <Text strong>{t("localImport.preview.joinsTitle")}</Text>
            <Text type="secondary">
              {t("localImport.preview.joinCount", { count: checkedJoinKeys.length })}
            </Text>
          </Space>
          <div data-testid="joinTable">
            <Table<ProposedJoin>
              rowKey={joinKey}
              dataSource={preview.proposedJoins}
              columns={joinColumns}
              rowSelection={joinRowSelection}
              pagination={{ defaultPageSize: 50, showSizeChanger: true }}
              size="small"
            />
          </div>
        </>
      )}

      <div style={{ textAlign: "right" }}>
        <Button
          type="primary"
          onClick={handleConfirm}
          disabled={selectedRowKeys.length === 0}
        >
          {t("localImport.confirmImport")}
        </Button>
      </div>
    </Space>
  );
}
