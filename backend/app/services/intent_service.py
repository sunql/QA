"""意图识别服务（关键词匹配，不调用 LLM 以节省 Token）。

多轮五类意图（在原有 QUERY / CHITCHAT 基础上增强）：
- CHITCHAT：问候/帮助/寒暄；消息过短。
- CLARIFY：询问概念含义（"X 是什么意思"），不进 NL2SQL 流水线。
- DEFINE / MAP / METRIC：本体治理指令（设计稿保留意图，此处接入流水线）——
  DEFINE 定义指标（"定义指标 X = 公式"）、MAP 映射属性到类（"把 X 映射到 Y"）、
  METRIC 查询/列举指标（"有哪些指标"）。
- REFINE：基于上一轮查询微调（排序 / 筛选 / 行数），需有历史状态。
- FOLLOW_UP：追问上一轮结果（指代 / 原因 / 比较），需有历史状态。
- QUERY / NEW_QUERY：全新查询；NEW_QUERY 表示有历史状态时开启的新一轮。

REFINE / FOLLOW_UP 仅在 hasPriorState=True 时成立；否则视为全新查询，
避免把"排序"这类调整词误判为对不存在历史的微调。

classifyResult 在返回意图之外，best-effort 抽取查询实体（维度/指标/图表类型）
与领域命令参数（DEFINE 的指标名与公式、MAP 的源与目标），供 ChatService 使用。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.domain.enums import ChartType, IntentType
from app.services.step_query_planner import StepQueryPlanner

# 命中任一关键词（前缀匹配）即判为闲聊
CHITCHAT_KEYWORDS: tuple[str, ...] = (
    "你好",
    "您好",
    "hi",
    "hello",
    "谢谢",
    "感谢",
    "再见",
    "拜拜",
    "帮助",
    "help",
    "能做什么",
    "你是谁",
)

_SHORT_MESSAGE_LIMIT = 3

# CLARIFY：询问概念 / 术语含义（3-1 收紧，优先于 REFINE 判断）。
# 修复 N5：宽泛关键词（"是什么"）把"最高的是什么产品"这类实体查询误判为概念解释。
# 仅保留明确的含义/解释句式；含最高级形容词的"X 是什么"视为实体查询，降级 QUERY。
_CLARIFY_MEANING_SUFFIXES: tuple[str, ...] = (
    "是什么意思",
    "什么意思",
    "什么含义",
    "有什么含义",
    "指什么",
    "指的是什么",
    "如何理解",
    "怎么理解",
)
# "周转率是什么"（术语 + 是什么 结尾）→ 概念解释；但含最高级时是"哪个最高"类实体查询
_CLARIFY_TERM_IS_RE = re.compile(r"^(.{1,15})是什么$")
_CLARIFY_SUPERLATIVE_RE = re.compile(r"(最高|最低|最大|最小|最多|最少|最好|最差|最畅销|最新)")

# REFINE：对上一轮查询的调整（排序 / 筛选 / 行数）
_REFINE_KEYWORDS: tuple[str, ...] = (
    "排序",
    "升序",
    "降序",
    "从小到大",
    "从大到小",
    "筛选",
    "过滤",
    "只看",
    "只显示",
    "只要",
    "去掉",
    "排除",
    "改成",
    "改为",
    "调整为",
    "换成",
    "重新",
)
# 行数 / 名次调整：只看前 N 条 / top N / 前 N 名 / 限 N 条
_REFINE_LIMIT_RE = re.compile(r"(前\s*\d|top\s*\d|前\s*\d+\s*[名条个]|限\s*\d)", re.IGNORECASE)

# FOLLOW_UP：追问上一轮结果（需有历史状态）
_FOLLOW_UP_KEYWORDS: tuple[str, ...] = (
    "为什么",
    "原因",
    "分别",
    "各自",
    "其中",
    "哪个",
    "哪些",
    "最多",
    "最少",
    "最高",
    "最低",
    "最大",
    "最小",
    "占比",
    "比例",
    "还有",
    "继续",
    "接着",
    "另外",
    "再",
)
# 指代词：对上一轮结果的直接指代（3-2 收紧后，FOLLOW_UP 须命中其一）。
# "它"用负向断言排除"其他/其它"里的它（那是形容词"其他"，不是指代）。
_FOLLOW_UP_REFERENT_RE = re.compile(r"(?:它们|他们|这个|那个|这些|那些|(?<!其)它)")
# 纯承接词：本身即指代上一轮操作，无需再带指代词。
# 仅保留语义上必然指代的"继续/接着"；"还有/另外"歧义大（"还有库存""另外的仓库"是
# 全新查询），仍在 _FOLLOW_UP_KEYWORDS 中，须带指代词才按追问处理。
_FOLLOW_UP_CONTINUATION_WORDS: tuple[str, ...] = (
    "继续",
    "接着",
)

# =============================================================================
# 领域命令意图（Phase 2）：DEFINE / MAP / METRIC
# =============================================================================

# DEFINE：定义/新增指标
_DEFINE_KEYWORDS: tuple[str, ...] = (
    "定义指标",
    "新增指标",
    "创建指标",
    "新指标",
    "加一个指标",
    "建一个指标",
)
# 提取"指标 <名> = <公式>"；允许中文冒号分隔，名称为中文/字母数字/下划线
_DEFINE_FORMULA_RE = re.compile(
    r"指标\s*[:：]?\s*([\w一-龥]{1,30})\s*=\s*(.+)"
)

# MAP：把源属性/类映射到目标类
_MAP_RE = re.compile(
    r"(?:把|将)?\s*([\w一-龥]{1,30})\s*(?:映射到|关联到|绑定到|->)\s*([\w一-龥]{1,30})"
)

# METRIC：查询/列举指标——须同时命中"指标"与查询性提示词，避免
# "各业务线的销售额指标汇总"这类数据查询被误判为 METRIC
_METRIC_QUERY_MARKERS: tuple[str, ...] = (
    "查看",
    "查询",
    "有哪些",
    "列表",
    "看看",
    "数据",
    "多少",
    "什么",
)

# =============================================================================
# 供应商 360° 视图（Phase 5.3）：chat 拦截路径，跳过 NL2SQL
# =============================================================================

# 命中关键词：含 "360" / "供应商" / "全貌" 任一即视为 supplier_360 候选。
# 注意：避免把 "供应商 100001 的订单数" 这类查询误判；要求问题意图明确指向 360 视图。
# 优先级高于普通 QUERY（先判 supplier_360 → 再判 query）。
_SUPPLIER_360_RE = re.compile(
    r"(?:供应商|supplier)[^\n。?]*?(?P<key>\d{5,9})[^\n。?]*?(?:360|全貌|360°|整体|全维度)"
    r"|(?:360|全貌|360°|整体视图)[^\n。?]*?(?:供应商|supplier)[^\n。?]*?(?P<key2>\d{5,9})"
    r"|(?:供应商|supplier)\s*(?P<key3>\d{5,9})\s*的\s*(?:360|全貌|整体)"
)

# =============================================================================
# 供应商风险 Agent（Phase 5.4）：chat 拦截路径，跳过 NL2SQL
# =============================================================================

# 命中关键词：含「风险 / 风险等级 / 健康度 / 风险评分」任一，且问题上下文中出现
# 「供应商 X」或「supplier X」。避免与 supplier_360 重叠（不命中 360 / 全貌 等词）。
# 优先级：与 supplier_360 并列（先 supplier_360 再 supplier_risk）。
_SUPPLIER_RISK_RE = re.compile(
    r"(?:供应商|supplier)[^\n。?]*?(?P<key>\d{5,9})[^\n。?]*?(?:风险|健康度|评分)"
    r"|(?:风险|健康度|评分)[^\n。?]*?(?:供应商|supplier)[^\n。?]*?(?P<key2>\d{5,9})"
    r"|(?:供应商|supplier)\s*(?P<key3>\d{5,9})\s*的\s*(?:风险|健康度|评分)"
)

# =============================================================================
# 知识图谱多跳推理（Phase 6.3）：chat 拦截路径，跳过 NL2SQL
# =============================================================================

# 命中关键词：含「涉及 / 关联 / 关系 / 图谱 / 链路 / 路径」任一，且上下文
# 出现「供应商 X」或「supplier X」。避免与 supplier_360 / supplier_risk 重叠：
# 不命中 360 / 全貌 / 风险 / 健康度 / 评分 等词（优先级在前两者之后）。
# 排除「相关 / 相连 / 有关」等形容词性宽泛词（code-reviewer HIGH：会把
# 「供应商 X 相关的订单金额」这类普通聚合查询误吸为图推理）。
# 「关联 / 关系」等名词 + 聚合量词残留风险由 _SUPPLIER_AGGREGATION_RE 兜底。
_SUPPLIER_GRAPH_RE = re.compile(
    r"(?:供应商|supplier)[^\n。?]*?(?P<key>\d{5,9})[^\n。?]*?(?:涉及|关联|关系|图谱|链路|路径)"
    r"|(?:涉及|关联|关系|图谱|链路|路径)[^\n。?]*?(?:供应商|supplier)[^\n。?]*?(?P<key2>\d{5,9})"
    r"|(?:供应商|supplier)\s*(?P<key3>\d{5,9})\s*(?:涉及|关联|关系)"
)

# 聚合量词兜底：图推理回答「与谁关联」的实体集合，从不回答数值指标；
# 命中任一量词（金额 / 数量 / 合计 / 成本 / 占比…）即视为数值型聚合查询，
# 交由 NL2SQL 处理，避免「供应商 X 关联的订单总金额」被吸为图推理卡片。
_SUPPLIER_AGGREGATION_RE = re.compile(
    r"(?:总金额|金额|总数量|数量|总额|合计|总计|均值|平均数|平均|总数|总价|成本|多少钱|多少元|占比|比例)"
)

# Phase 7 G3: 口语跳数短语提取（「3 跳」「三跳」「深度 5」「最多 3 跳」→ 数字）。
# 独立于 _SUPPLIER_GRAPH_RE：是否消费由 GRAPH_REASONING 意图判定把关
# （非图问法即使含「N 跳」也不提取）。返回原始值，越界由 service 层 clamp。
# 前缀只收「最多/不超过」这类上限语义：maxHops 是遍历深度上限，「至少 3 跳」
# 是下限，映射到上限会低估，故不收（code-reviewer LOW，语义倒置）。
_GRAPH_HOP_RE = re.compile(
    r"(?:最多|不超过)?\s*(?P<n>\d{1,2}|[一二三四五六七八九十两])\s*(?:跳|层|度)"
    r"|深度\s*(?P<n2>\d{1,2}|[一二三四五六七八九十两])"
)

_CN_HOP_NUM = {
    "一": 1,
    "两": 2,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}

# Phase 6.4: Agent 显式指名（如「用 supplier_risk_agent 评估供应商 100001」）。
# 独立 token + _AGENT 后缀 + 词边界，误中普通问法的概率极低；
# 匹配后统一大写（Agent 编码约定全大写下划线，见 AgentDefinitionCreate）。
_AGENT_RUN_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?P<code>[A-Za-z][A-Za-z0-9_]{2,63}_AGENT)(?![A-Za-z0-9_])",
    re.IGNORECASE,
)

# Phase 6.4: 通用供应商编码抽取（Agent 显式指名回退用，无意图关键词约束）。
# 仅要求「供应商/supplier」后紧跟 5-9 位企业编码，见 extractSupplierAnyKey。
_SUPPLIER_ANY_KEY_RE = re.compile(r"(?:供应商|supplier)\s*[:：]?\s*(?P<key>\d{5,9})")


def extractSupplierKey(message: str) -> str | None:
    """模块级 supplier enterprise_key 抽取（Phase 5.3 供应商 360°）。"""
    match = _SUPPLIER_360_RE.search(message)
    if match is None:
        return None
    return match.group("key") or match.group("key2") or match.group("key3")


def extractSupplierRiskKey(message: str) -> str | None:
    """模块级 supplier enterprise_key 抽取（Phase 5.4 风险 Agent）。"""
    match = _SUPPLIER_RISK_RE.search(message)
    if match is None:
        return None
    return match.group("key") or match.group("key2") or match.group("key3")


def extractSupplierGraphKey(message: str) -> str | None:
    """模块级 supplier enterprise_key 抽取（Phase 6.3 图推理，含聚合量词兜底）。"""
    match = _SUPPLIER_GRAPH_RE.search(message)
    if match is None:
        return None
    if _SUPPLIER_AGGREGATION_RE.search(message) is not None:
        return None
    return match.group("key") or match.group("key2") or match.group("key3")


def extractGraphMaxHops(message: str) -> int | None:
    """口语跳数提取（Phase 7 G3）：「3 跳 / 三跳 / 深度 5 / 最多 3 跳」→ int。

    未命中返回 None（默认值由 service 层决定）；越界值原样返回，
    在 ``resolveChatMaxHops`` clamp（chat 口语越界比 4xx 友好）。
    """
    match = _GRAPH_HOP_RE.search(message)
    if match is None:
        return None
    raw = match.group("n") or match.group("n2")
    if raw is None:
        return None
    if raw in _CN_HOP_NUM:
        return _CN_HOP_NUM[raw]
    return int(raw)


def extractAgentCode(message: str) -> str | None:
    """从用户问句提取显式指名的 Agent 编码（Phase 6.4）。

    - 「用 supplier_risk_agent 评估供应商 100001」→ SUPPLIER_RISK_AGENT
    - 「让 GRAPH_REASONING_AGENT 查 100001 的链路」→ GRAPH_REASONING_AGENT
    - 未命中返回 None；命中即优先路由到 AgentRuntimeService。
    """
    match = _AGENT_RUN_RE.search(message)
    if match is None:
        return None
    return match.group("code").upper()


def extractSupplierAnyKey(message: str) -> str | None:
    """通用 supplier enterprise_key 抽取（Phase 6.4 Agent 显式指名回退）。

    只要求「供应商/supplier」后紧跟 5-9 位企业编码，不做意图关键词约束。
    用途：用户在 chat 显式指名 Agent（如「用 supplier_risk_agent 评估供应商 100001」）
    时，工具已被确定性解析，专用 extractor（360/risk/graph 的关键词正则）匹配
    失败并无意义——回退到本函数即可拿到 key 执行工具。
    """
    match = _SUPPLIER_ANY_KEY_RE.search(message)
    if match is None:
        return None
    return match.group("key")

# =============================================================================
# 斜杠指令（Phase 5）：优先级最高，跳过所有自然语言关键词匹配
# =============================================================================

# /metric <name> = <formula>  —— 定义指标
_SLASH_METRIC_RE = re.compile(
    r"^/metric\s+(?P<name>[\w一-龥]{1,30})\s*=\s*(?P<formula>.+)$"
)
# /metric <name>  —— 无公式（ChatService 层补引导文案）
_SLASH_METRIC_NAME_ONLY_RE = re.compile(
    r"^/metric\s+(?P<name>[\w一-龥]{1,30})\s*$"
)
# /define <class_name> [alias=xxx] [desc=xxx]  —— 创建本体类
_SLASH_DEFINE_RE = re.compile(
    r"^/define\s+(?P<name>[\w一-龥][\w一-龥]{0,29})(?:\s+(?P<rest>.+))?$"
)
# /define <class_name> 额外标注：alias=xxx / desc=xxx
_SLASH_DEFINE_KV_RE = re.compile(r"(?P<key>alias|desc)\s*=\s*(?P<val>[^\s]+)")
# /map <property> -> <class> | /map <property> 映射到 <class>
_SLASH_MAP_RE = re.compile(
    r"^/map\s+(?P<source>[\w一-龥]{1,30})\s*(?:->|映射到|关联到|绑定到)\s*(?P<target>[\w一-龥]{1,30})\s*$"
)

# =============================================================================
# 查询实体抽取（best-effort，未命中返回 None）
# =============================================================================

# 维度：按<X>分组/汇总/统计/看；每个<X>的
_DIMENSION_BY_RE = re.compile(
    r"按\s*(.{1,12}?)\s*(?:分组|汇总|统计|来看|来统计|查看|显示|展示|看)"
)
_DIMENSION_PER_RE = re.compile(r"每\s*个?\s*(.{1,12}?)\s*(?:的|分)")

# 指标量词后缀：取最靠右的后缀，再逐字回退抽取其前短语（见 _extractMetric）
_METRIC_SUFFIXES: tuple[str, ...] = (
    "的总和",
    "总金额",
    "总数量",
    "总量",
    "总数",
    "总额",
    "合计",
    "均值",
    "平均数",
    "平均",
    "总和",
    "汇总",
)
# 维度动词：指标短语的左侧边界（配合"的"与空白一起作为分隔符）
_DIMENSION_VERBS: tuple[str, ...] = ("汇总", "统计", "分组", "展示", "查看", "显示")
_METRIC_PHRASE_LIMIT = 8

# 图表类型关键词 → ChartType
_CHART_TYPE_PATTERNS: tuple[tuple[tuple[str, ...], ChartType], ...] = (
    (("柱状图", "柱图", "柱形图"), ChartType.BAR),
    (("饼图", "环形图", "占比图"), ChartType.PIE),
    (("折线图", "趋势图", "线图"), ChartType.LINE),
    (("散点图", "散点"), ChartType.SCATTER),
    (("表格", "列表"), ChartType.TABLE),
)


@dataclass(frozen=True)
class IntentResult:
    """分类结果：意图 + 抽取的查询实体 / 领域命令参数（best-effort，可为 None）。

    dimension / metric / chartType：查询类意图的实体。
    metric 另用于 DEFINE 的指标名；source / target 用于 MAP；formula 用于 DEFINE。
    supplierKey：仅 SUPPLIER_360 意图时填充（提取的 enterprise_key，BIGINT 字符串）。
    """

    intent: IntentType
    dimension: str | None = None
    metric: str | None = None
    chartType: ChartType | None = None
    source: str | None = None
    target: str | None = None
    formula: str | None = None
    supplierKey: str | None = None
    # Phase 5.4: SUPPLIER_RISK 复用 supplierKey 字段（与 supplier_360 同语义）。
    # Phase 6.4: AGENT_RUN 填充（提取的 agent_code，如 SUPPLIER_RISK_AGENT）。
    agent_code: str | None = None
    # Phase 7 G3: 仅 GRAPH_REASONING 填充（口语跳数，如「3 跳」→ 3）；
    # None = 未指名，由 service 层默认 2。
    max_hops: int | None = None


class IntentService:
    """根据消息文本分类用户意图。

    hasPriorState 表示该会话上一轮是否有成功查询（存在 session_query_state）。
    仅在有历史状态时 REFINE / FOLLOW_UP 才成立；否则回退为全新查询。
    """

    def classify(self, message: str, *, hasPriorState: bool = False) -> IntentType:
        """兼容入口：仅返回意图类型（旧调用点）。"""
        return self.classifyResult(message, hasPriorState=hasPriorState).intent

    def classifyResult(self, message: str, *, hasPriorState: bool = False) -> IntentResult:
        """完整分类：意图 + 抽取实体/命令参数。

        关键词判断用小写化文本（兼容 "HELP"/"help" 等），但 DEFINE 公式与
        MAP 源/目标等需保留原始大小写的实体，从原始消息抽取。
        """
        original = message.strip()
        # 斜杠指令优先匹配（Phase 5）：以 / 起头，跳过所有自然语言分类
        if original.startswith("/"):
            slashResult = self._matchSlashCommand(original)
            if slashResult is not None:
                return slashResult
        normalized = message.strip().lower()
        if any(normalized.startswith(kw) for kw in CHITCHAT_KEYWORDS):
            return IntentResult(intent=IntentType.CHITCHAT)
        if len(normalized) <= _SHORT_MESSAGE_LIMIT:
            return IntentResult(intent=IntentType.CHITCHAT)
        if self._isClarify(normalized):
            return IntentResult(intent=IntentType.CLARIFY)
        # Phase 6.4: Agent 显式指名检测（优先于 supplier_360 / risk / graph：
        # 指名是最高优先级领域信号，如「用 supplier_risk_agent 评估供应商 100001」）。
        agentCode = extractAgentCode(original)
        if agentCode is not None:
            return IntentResult(intent=IntentType.AGENT_RUN, agent_code=agentCode)
        # Phase 5.3: supplier-360 检测（优先于 REFINE/METRIC/QUERY，避免「供应商 100001 的订单数」
        # 这类普通查询被误判）。extractSuppplierKey 返回 None → 不命中，走下层判定。
        supplierKey = self._extractSupplierKey(original)
        if supplierKey is not None:
            return IntentResult(
                intent=IntentType.SUPPLIER_360, supplierKey=supplierKey
            )
        # Phase 5.4: supplier-risk 检测（紧跟 supplier_360 之后，避免「供应商 100001 的 360°」
        # 被 risk 误吸；regex 已限定不含 360/全貌 等词）。
        riskKey = self._extractSupplierRiskKey(original)
        if riskKey is not None:
            return IntentResult(
                intent=IntentType.SUPPLIER_RISK, supplierKey=riskKey
            )
        # Phase 6.3: 知识图谱多跳推理检测（在 supplier_360 / supplier_risk 之后，
        # 兜住「供应商 100001 涉及哪些物料 / 关联什么」类推理问法）。
        graphKey = self._extractSupplierGraphKey(original)
        if graphKey is not None:
            return IntentResult(
                intent=IntentType.GRAPH_REASONING,
                supplierKey=graphKey,
                max_hops=extractGraphMaxHops(original),
            )
        if any(kw in normalized for kw in _DEFINE_KEYWORDS):
            name, formula = self._extractDefine(original)
            return IntentResult(intent=IntentType.DEFINE, metric=name, formula=formula)
        mapMatch = _MAP_RE.search(original)
        if mapMatch:
            return IntentResult(
                intent=IntentType.MAP, source=mapMatch.group(1), target=mapMatch.group(2)
            )
        # 调整意图优先于 METRIC：避免"按指标排序"被误判为指标列举
        if hasPriorState and self._isRefine(normalized):
            return self._queryResult(IntentType.REFINE, original)
        if self._isMetricQuery(normalized):
            return IntentResult(intent=IntentType.METRIC)
        if hasPriorState:
            if self._isFollowUp(normalized):
                return self._queryResult(IntentType.FOLLOW_UP, original)
            return self._queryResult(IntentType.NEW_QUERY, original)
        return self._queryResult(IntentType.QUERY, original)

    @staticmethod
    def _queryResult(intent: IntentType, normalized: str) -> IntentResult:
        return IntentResult(
            intent=intent,
            dimension=IntentService._extractDimension(normalized),
            metric=IntentService._extractMetric(normalized),
            chartType=IntentService._extractChartType(normalized),
        )

    @staticmethod
    def _matchSlashCommand(original: str) -> IntentResult | None:
        """斜杠指令解析：以 / 起头时优先匹配；不匹配返回 None，回退自然语言。

        返回结构化 IntentResult，复用现有 IntentType 枚举：
        - /metric <name> = <formula> → METRIC(metric, formula)
        - /metric <name>            → METRIC(metric)   # 公式为空由 ChatService 引导
        - /define <class> [alias=…] [desc=…] → DEFINE(target=class, source=alias, formula=desc)
          # 设计01：/define = 创建本体类（区别于 Phase 2 的"定义指标"）
        - /map <property> -> <class> → MAP(source, target)
        """
        match = _SLASH_METRIC_RE.match(original)
        if match:
            return IntentResult(
                intent=IntentType.METRIC,
                metric=match.group("name"),
                formula=match.group("formula").strip(),
            )
        match = _SLASH_METRIC_NAME_ONLY_RE.match(original)
        if match:
            return IntentResult(
                intent=IntentType.METRIC,
                metric=match.group("name"),
                formula=None,
            )
        match = _SLASH_DEFINE_RE.match(original)
        if match:
            name = match.group("name")
            rest = match.group("rest") or ""
            alias = None
            description = None
            for kv in _SLASH_DEFINE_KV_RE.finditer(rest):
                key, value = kv.group("key"), kv.group("val")
                if key == "alias":
                    alias = value
                elif key == "desc":
                    description = value
            # source 复用为别名；formula 复用为描述（语义独立、不冲突）
            return IntentResult(
                intent=IntentType.DEFINE,
                target=name,
                source=alias,
                formula=description,
            )
        match = _SLASH_MAP_RE.match(original)
        if match:
            return IntentResult(
                intent=IntentType.MAP,
                source=match.group("source"),
                target=match.group("target"),
            )
        return None

    @staticmethod
    def _isClarify(normalized: str) -> bool:
        """收紧版 CLARIFY（3-1）：仅明确的术语含义/解释句式，其余降级 QUERY。

        - "X 是什么意思/什么含义/指什么/如何理解" → 含义
        - "解释(一下) X" → 请求解释
        - "什么是 X"（开头）→ 请求解释；但 X 含最高级形容词（"什么是最畅销的产品"）除外，
          此时问的是实体而非概念，应走 QUERY。
        - "X 和 Y 的区别" / "X 的含义/概念/的意思" → 名词性含义
        - "X 是什么"（结尾、X 为短术语）→ 含义；但含最高级形容词（"最高的是什么"）除外，
          用户问的是实体（哪款产品最高），应走 QUERY。
        """
        if any(suffix in normalized for suffix in _CLARIFY_MEANING_SUFFIXES):
            return True
        if normalized.startswith(("解释", "解释一下", "说明一下")):
            return True
        if normalized.startswith("什么是"):
            remainder = normalized[3:]
            return not bool(_CLARIFY_SUPERLATIVE_RE.search(remainder))
        # 区别类需"和"连接（毛利和净利区别）；"各供应商销售额的区别"这类数据比较无"和"，走 QUERY
        if re.search(r"(的含义|的概念|的意思|和.{1,15}区别)", normalized):
            return True
        termIs = _CLARIFY_TERM_IS_RE.match(normalized)
        return bool(termIs and not _CLARIFY_SUPERLATIVE_RE.search(normalized))

    def _extractSupplierKey(self, message: str) -> str | None:
        """从用户问句提取 supplier enterprise_key（Phase 5.3）。

        匹配模式（_SUPPLIER_360_RE）：
        - 「供应商 100001 的 360° 视图」
        - 「supplier 100001 全貌」
        - 「360 视图 供应商 100001」
        - 「供应商 100001 的 360」

        返回字符串形式的 key（service 层转 int）；未命中返回 None。
        使用 ORIGINAL 文本（不归一化大小写）；enterprise_key 是 BIGINT，
        5-9 位数字限制避免误中日期/年份等。
        """
        return extractSupplierKey(message)

    def _extractSupplierRiskKey(self, message: str) -> str | None:
        """从用户问句提取 supplier enterprise_key（Phase 5.4 Risk Agent）。

        匹配模式（_SUPPLIER_RISK_RE）：
        - 「供应商 100001 的风险等级」
        - 「supplier 100001 健康度」
        - 「风险评分 供应商 100001」
        - 「供应商 100001 的评分」

        与 _extractSupplierKey 同数字边界（5-9 位），使用 ORIGINAL 文本。
        未命中返回 None。
        """
        return extractSupplierRiskKey(message)

    def _extractSupplierGraphKey(self, message: str) -> str | None:
        """从用户问句提取 supplier enterprise_key（Phase 6.3 图推理）。

        匹配模式（_SUPPLIER_GRAPH_RE）：
        - 「供应商 100001 涉及哪些物料」
        - 「supplier 100001 的关联订单」
        - 「图谱上 供应商 100001 有什么关系」
        - 「供应商 100001 涉及什么」

        与 supplier_360 / supplier_risk 同数字边界（5-9 位），使用 ORIGINAL 文本。
        优先级在两者之后：命中 360 / 风险词的问句已被前序分支消费。
        聚合量词兜底：命中 _SUPPLIER_AGGREGATION_RE 的数值型聚合查询不判为图推理
        （如「供应商 100001 关联的采购订单总金额是多少」走 NL2SQL）。
        未命中返回 None。
        """
        return extractSupplierGraphKey(message)

    @staticmethod
    def _isExplicitMultiStep(normalized: str) -> bool:
        """显式分步问题（≥2 个「第X步」/序数副词标号）必须走全新查询进多步流水线。

        「top10」命中 _REFINE_LIMIT_RE、「这些…占比」命中追问关键词，但整句
        是一条独立的多步新问题，不应锚定到上一轮历史状态。
        """
        return StepQueryPlanner.rule_based_split(normalized) is not None

    @staticmethod
    def _isRefine(normalized: str) -> bool:
        if IntentService._isExplicitMultiStep(normalized):
            return False
        if any(kw in normalized for kw in _REFINE_KEYWORDS):
            return True
        return _REFINE_LIMIT_RE.search(normalized) is not None

    @staticmethod
    def _isFollowUp(normalized: str) -> bool:
        """3-2 收紧：FOLLOW_UP 需命中指代词（它/这个/这些…），否则视为全新查询。

        修复 N6：泛化关键词（为什么/分别/最高）把"为什么A公司最多"这类有前置状态的
        新查询锚定到旧上下文。仅当消息含指代词（确实指向上一轮）时才按追问处理；
        纯承接词（继续/接着）本身即指代上一轮，免于指代词要求。

        权衡（有意为之）：无指代词的追问（"为什么1月最高"）会被重分类为新查询——
        纯关键词匹配无法区分"新实体"与"上一轮维度"，宁可不锚定也不误锚定。
        """
        if IntentService._isExplicitMultiStep(normalized):
            return False
        if any(cw in normalized for cw in _FOLLOW_UP_CONTINUATION_WORDS):
            return True
        if _FOLLOW_UP_REFERENT_RE.search(normalized) is None:
            return False
        return any(kw in normalized for kw in _FOLLOW_UP_KEYWORDS)

    @staticmethod
    def _isMetricQuery(normalized: str) -> bool:
        if "指标" not in normalized:
            return False
        return any(marker in normalized for marker in _METRIC_QUERY_MARKERS)

    @staticmethod
    def _extractDefine(normalized: str) -> tuple[str | None, str | None]:
        match = _DEFINE_FORMULA_RE.search(normalized)
        if match is None:
            return None, None
        return match.group(1).strip(), match.group(2).strip()

    @staticmethod
    def _extractDimension(normalized: str) -> str | None:
        for regex in (_DIMENSION_BY_RE, _DIMENSION_PER_RE):
            match = regex.search(normalized)
            if match:
                return match.group(1).strip()
        return None

    @staticmethod
    def _extractMetric(normalized: str) -> str | None:
        """抽取量词后缀前的指标短语（best-effort）。

        选最靠右的量词后缀（如"总额"优先于句中更早的"汇总"），然后从后缀前
        逐字回退，遇分隔符（的 / 空白 / 维度动词起点）停止，最多取 8 字。
        """
        bestIdx, bestLen = -1, 0
        for suffix in _METRIC_SUFFIXES:
            idx = normalized.rfind(suffix)
            if idx > bestIdx:
                bestIdx, bestLen = idx, len(suffix)
        if bestIdx <= 0:
            return None
        i = bestIdx - 1
        while i >= 0 and (bestIdx - i) <= _METRIC_PHRASE_LIMIT:
            ch = normalized[i]
            if ch in ("的", " ", "　"):
                break
            verbLen = next(
                (len(verb) for verb in _DIMENSION_VERBS if normalized.startswith(verb, i)),
                0,
            )
            if verbLen:
                # 跳过维度动词：指标短语从动词之后开始（如"汇总销售额总额"→"销售额"）
                i += verbLen - 1
                break
            i -= 1
        phrase = normalized[i + 1:bestIdx]
        return phrase.strip() or None

    @staticmethod
    def _extractChartType(normalized: str) -> ChartType | None:
        for keywords, chartType in _CHART_TYPE_PATTERNS:
            if any(kw in normalized for kw in keywords):
                return chartType
        return None
