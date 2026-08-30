/** 中文文案字典（i18n 单语言起步）。
 *
 * 与后端 ``app/services/messages_zh.py`` 同源思路：把 user-facing 中文字面量集中，
 * 调用方按 ``t('key')`` 引用。未来若需多语言，把本模块替换为按 locale 索引的字典即可，
 * 业务代码无需改动。
 *
 * 设计要点：
 * - 嵌套对象 + 点分键（``t('forms.datasource.name')``），与 react-i18next 风格兼容，
 *   便于将来无痛迁移到标准库。
 * - 占位符用 ``{name}`` 形态（与后端 ``str.format()`` 一致），调用时 ``t('k', { name })``。
 * - key 按"层级 + 业务域"分组：``common`` 全局按钮/通用词，``appLayout`` 导航/品牌，
 *   ``pages`` 页面标题，``forms`` 表单 label/placeholder/校验，``chat`` 聊天界面文案，
 *   ``toast`` 异步消息，``errors`` 错误兜底，``enums`` 枚举下拉 label，``queryPlan`` 查询计划面板。
 */

export const zhCN = {
  common: {
    refresh: "刷新",
    edit: "编辑",
    delete: "删除",
    version: "版本",
    close: "关闭",
    cancel: "取消",
    confirm: "确定",
    send: "发送",
    copy: "复制",
    copied: "已复制",
    enabled: "启用",
    disabled: "停用",
    active: "当前",
    none: "（无）",
    dash: "-",
    emDash: "—",
    next: "下一步",
    prev: "上一步",
  },

  appLayout: {
    brand: "AI服务平台",
    menu: {
      models: "模型配置",
      embeddings: "Embedding 服务",
      chat: "AIChatService",
      ontology: "本体管理",
      datasource: "数据源",
      dataQuality: "数据质量",
      lineage: "数据血缘",
      usage: "用量看板",
      status: "服务状态",
      graph: "Neo4j 图库",
      vectors: "Milvus 向量库",
    },
    themeToggle: "切换暗色模式",
    themeDark: "暗",
    themeLight: "亮",
    languageSwitch: "切换语言",
  },

  semanticSearchButton: "语义搜索",

  errors: {
    requestFailed: "请求失败",
    requestFailedHttp: "请求失败 (HTTP {status})",
    networkError: "网络异常，请稍后重试",
    unknownError: "未知错误",
    noStreamSupport: "当前浏览器不支持流式响应",
  },

  pages: {
    chat: "AIChatService",
    ontology: "本体管理",
    datasource: "数据源管理",
    dataQuality: "数据质量管理",
    modelConfig: "模型配置管理",
    embeddings: "Embedding 服务管理",
    usage: "用量看板",
    status: "服务状态",
    searchEmpty: "未匹配到相关本体。请确认已通过编辑表单或 /embeddings/sync 写入向量。",
  },

  toast: {
    created: "创建成功",
    updated: "更新成功",
    deleted: "已删除",
    enabled: "已启用",
    disabled: "已停用",
    connectSuccess: "连接成功：{message}",
    connectFailed: "连接失败：{message}",
    networkError: "网络异常",
    pleaseSelectDatasource: "请先选择数据源",
    pleaseFillPassword: "请先填写密码再测试连接",
    sqlCopied: "SQL 已复制",
    copyFailed: "复制失败：{message}",
    versionUpdated: "更新成功（新版本已创建）",
    previewFailed: "生成导入预览失败，请稍后重试",
    importFailed: "导入失败，请查看错误详情",
  },

  queryPlan: {
    panelLabel: "查看查询计划",
    interpretation: "理解",
    target: "查询目标",
    tables: "涉及表",
    selectedFields: "选用字段",
    aggregation: "聚合",
    groupBy: "分组",
    filters: "筛选条件",
    sort: "排序",
    joins: "JOIN 关系",
    rowLimit: "行数限制",
    viewSql: "查看 SQL",
    dataQuality: {
      unevaluated: "DQ: 未评估",
      tooltip: {
        unevaluated: "该表尚未评估数据质量，可去数据质量页触发评估",
        view: "点击查看明细",
      },
    },
  },

  multiStep: {
    panelLabel: "查看分析计划",
    statusPending: "待执行",
    statusRunning: "执行中",
    statusDone: "已完成",
    statusError: "失败",
    summaryStep: "汇总",
  },

  messageItem: {
    cost: "成本: ${amount}",
    model: "模型: {name}",
    typing: "正在输入",
    validationDetail: "校验失败详情",
    affinityLocked: "🔒 锁定 {model} · 剩 {turns} 轮",
  },

  messageList: {
    empty: "输入问题开始对话",
  },

  sqlPreview: {
    viewSql: "查看 SQL",
    copy: "复制",
    copied: "已复制",
    copiedToast: "SQL 已复制",
    copyFailedToast: "复制失败：{message}",
    unknownError: "未知错误",
  },

  chartExport: {
    csv: "导出 CSV",
    png: "导出 PNG",
    success: "已导出",
    failed: "导出失败：{message}",
  },

  chatPanel: {
    chartTypes: {
      auto: "自动",
      table: "表格",
      bar: "柱状图",
      pie: "饼图",
      line: "折线图",
      scatter: "散点图",
    },
    chartTypePlaceholder: "图表类型",
    chartTypeAriaLabel: "图表类型",
    modelAutoRoute: "自动路由",
    modelPlaceholder: "选择模型",
    datasourcePlaceholder: "选择数据源",
    suggesting: "正在检索相似问题…",
    suggestTitle: "点击回填该问法",
    inputPlaceholder:
      "输入自然语言问题，或斜杠指令：/metric 销售额 = SUM(order.amount) · /define 类名 · /map 属性 -> 类",
    sendButton: "发送",
    pleaseSelectDatasource: "请先选择数据源",
  },

  chat: {
    history: {
      title: "历史问答",
      newChat: "新对话",
      deleteConfirm: "确定删除该会话吗？",
      empty: "暂无历史对话",
      loadError: "加载历史失败",
      deleteError: "删除失败",
      collapse: "收起历史",
      expand: "展开历史",
      messageCount: "{count} 条消息",
      currentBadge: "当前",
      untitledQuestion: "（无标题）",
      deleteAriaLabel: "删除该会话",
    },
    exportPdf: {
      fullButton: "导出 PDF",
      singleButton: "导出此条",
      fullAriaLabel: "导出当前会话为 PDF",
      singleAriaLabel: "导出该条问答为 PDF",
      downloading: "正在生成 PDF…",
      failed: "PDF 导出失败：{message}",
      emptySession: "当前会话暂无消息可导出",
    },
  },

  termDictionary: {
    addButton: "添加到术语词典",
    openManager: "术语词典",
    managerTitle: "术语词典",
    createTitle: "添加术语",
    empty: "暂无术语",
    created: "已添加到术语词典",
    deleteConfirm: "确认删除该术语？",
    labels: {
      term: "术语",
      definition: "含义解释",
      mappedClassName: "映射类（英文）",
      mappedPropertyName: "映射属性（英文）",
      formulaHint: "公式 / 结构提示",
    },
    placeholders: {
      term: "如 占比 / 实际到货 / 订的数量",
      definition: "如 某值占总量的比例",
      mappedClassName: "如 PRECEIPTD",
      mappedPropertyName: "如 收货数量",
      formulaHint: "如 SUM(数量) / SUM(SUM(数量)) OVER ()",
    },
  },

  dataQuality: {
    createRule: "新建规则",
    editRule: "编辑规则",
    ruleCode: "规则编码",
    ruleName: "规则名称",
    datasource: "数据源",
    datasourcePlaceholder: "请选择数据源",
    targetTable: "目标表",
    targetColumn: "目标列",
    ruleType: "规则类型",
    ruleExpression: "规则表达式",
    threshold: "阈值（%）",
    severity: "严重级别",
    enabled: "启用",
    owner: "责任方",
    description: "说明",
    filterType: "按规则类型过滤",
    ruleCodePattern: "编码必须以大写字母开头，仅含大写字母/数字/下划线",
    createSuccess: "规则已创建",
    updateSuccess: "规则已更新",
    disableSuccess: "规则已停用",
  },

  lineage: {
    page: {
      title: "数据血缘",
    },
    filter: {
      layers: "层级筛选",
      objects: "对象筛选",
      objectPlaceholder: "按对象筛选（如 PORDER）",
      summary:
        "已选 {selected}/{total} 层、{objects} 个对象，当前显示 {edges} 条边（共 {allEdges} 条）",
    },
    empty: {
      noData: "暂无血缘数据，请先在本体管理或调用 /api/v1/lineage/edges 创建",
      filteredOut: "当前筛选条件下无血缘边，请调整层级或对象筛选",
    },
  },

  forms: {
    required: "必填项",

    embeddingProviders: {
      columns: {
        id: "ID",
        name: "名称",
        type: "类型",
        baseUrl: "接入地址",
        modelName: "模型名",
        dimension: "维度",
        status: "状态",
        actions: "操作",
      },
      addButton: "新增服务",
      deactivateConfirm: "确认停用该服务？",
      addModalTitle: "新增服务",
      editModalTitle: "编辑服务",
      labels: {
        name: "名称",
        providerType: "类型",
        baseUrl: "接入地址",
        modelName: "模型名",
        dimension: "输出维度",
        dimensionHint: "须与 Milvus 集合维度一致（当前 1024）",
        apiKey: "API Key",
        apiKeyKeep: "API Key（留空表示不修改）",
      },
      placeholders: {
        name: "如 Ollama bge-m3",
        baseUrl: "如 http://localhost:11434/v1",
        modelName: "如 bge-m3:latest",
        apiKey: "本地免鉴权服务可留空",
      },
    },

    modelConfig: {
      columns: {
        id: "ID",
        modelName: "模型名称",
        provider: "供应商",
        apiEndpoint: "接入地址",
        inputCost: "输入成本/1k",
        outputCost: "输出成本/1k",
        weight: "权重",
        costThreshold: "成本阈值",
        status: "状态",
        actions: "操作",
      },
      addButton: "新增模型",
      deactivateConfirm: "确认停用该模型？",
      addModalTitle: "新增模型",
      editModalTitle: "编辑模型",
      labels: {
        modelName: "模型名称",
        provider: "供应商",
        apiEndpoint: "接入地址",
        apiKey: "API Key",
        apiKeyKeep: "API Key（留空表示不修改）",
        costPer1KInput: "输入成本/1k ($)",
        costPer1KOutput: "输出成本/1k ($)",
        maxInputTokens: "最大输入 Token",
        weight: "路由权重",
        costThreshold: "成本阈值 ($)",
      },
      placeholders: {
        modelName: "如 gpt-4o-mini / deepseek-chat / qwen2.5:7b",
        apiEndpoint: "https://api.openai.com/v1 或 http://localhost:11434",
        apiKey: "sk-...",
      },
    },

    datasource: {
      columns: {
        id: "ID",
        name: "名称",
        type: "类型",
        host: "主机",
        port: "端口",
        databaseName: "数据库/Service",
        username: "用户名",
        isDefault: "默认",
        status: "状态",
        actions: "操作",
        default: "默认",
      },
      addButton: "新增数据源",
      addModalTitle: "新增数据源",
      editModalTitle: "编辑数据源",
      testConnection: "测试连接",
      setAsDefault: "设为默认",
      deleteConfirm: "确认删除该数据源？",
      labels: {
        name: "名称",
        type: "类型",
        host: "主机",
        port: "端口",
        databaseName: "数据库 / Service",
        oracleVersion: "Oracle 版本",
        username: "用户名",
        password: "密码",
        passwordKeep: "密码（留空表示不修改）",
        description: "描述",
      },
      placeholders: {
        name: "如 ZJTH-Oracle / RuoYi-MySQL",
        host: "IP 或域名",
        databaseName: "PG/MySQL 填数据库名，Oracle 填 service_name",
        oracleVersion: "不选默认 12c+（FETCH FIRST）",
        password: "连接密码",
        passwordKeep: "留空表示不修改",
        description: "可选",
      },
      tooltips: {
        oracleVersion: "决定分页语法：11g 及以下用 ROWNUM，12c 及以上用 FETCH FIRST。不选默认按 12c+ 处理。",
      },
      validation: {
        portRange: "端口范围 1-65535",
      },
    },

    ontology: {
      tabs: {
        classes: "类",
        properties: "属性",
        metrics: "指标",
        joins: "关联",
      },
      addClassButton: "新增类",
      addClassModalTitle: "新增类",
      editClassModalTitle: "编辑类",
      addPropertyButton: "新增属性",
      addPropertyModalTitle: "新增属性",
      editPropertyModalTitle: "编辑属性",
      addMetricButton: "新增指标",
      addMetricModalTitle: "新增指标",
      editMetricModalTitle: "编辑指标",
      addJoinButton: "新增关联",
      addJoinModalTitle: "新增关联",
      versionsModalTitle: "版本历史：{name}",
      semanticSearchTab: "语义搜索",
      semanticResultsCardTitle: "语义检索结果",
      semanticSearchPlaceholder: "语义搜索：如 客户销售额",
      deleteConfirm: "确认删除？",

      classColumns: {
        id: "ID",
        className: "类名",
        classAlias: "别名",
        version: "版本",
        validFrom: "生效时间",
        parentClassId: "父类",
        sourceTable: "数据表",
        description: "描述",
        actions: "操作",
        versionTag: "v{version}",
        versionCurrentTag: "v{version} 当前",
      },

      propertyColumns: {
        id: "ID",
        classId: "所属类",
        propertyName: "属性名",
        propertyAlias: "别名",
        dataType: "数据类型",
        sourceColumn: "源字段",
        flags: "标签",
        actions: "操作",
      },

      propertyEmptyHint:
        "提示：请先在「类」标签页中创建本体类，再添加属性。",

      metricColumns: {
        id: "ID",
        metricName: "指标名",
        metricAlias: "别名",
        formula: "公式",
        aggFunction: "聚合函数",
        targetClassId: "目标类",
        dimensionDefaults: "默认维度",
        actions: "操作",
      },

      joinColumns: {
        id: "ID",
        source: "源类 → 目标类",
        sourceColumns: "源列",
        targetColumns: "目标列",
        joinType: "连接类型",
        relationType: "来源",
        description: "描述",
        actions: "操作",
      },

      joinLabels: {
        sourceClassId: "源类",
        sourceColumns: "源列（逗号分隔）",
        targetClassId: "目标类",
        targetColumns: "目标列（逗号分隔）",
        joinType: "连接类型",
        relationType: "来源",
        description: "描述",
      },
      joinPlaceholders: {
        sourceClassId: "请选择源类",
        targetClassId: "请选择目标类",
        sourceColumns: "如 BPTNUM_0",
        targetColumns: "如 BPRNUM_0",
        description: "如 收货单供应商号关联供应商主数据",
      },

      versionColumns: {
        version: "版本",
        validFrom: "生效",
        validTo: "失效",
        description: "描述",
      },

      searchColumns: {
        type: "类型",
        name: "名称",
        alias: "别名",
        description: "描述",
        score: "相似度",
      },

      classLabels: {
        className: "类名",
        classAlias: "别名",
        sourceTable: "数据表",
        parentClassId: "父类（继承）",
        description: "描述",
      },
      classPlaceholders: {
        className: "如 Customer / Order / Product",
        classAlias: "如 客户 / 订单 / 商品",
        sourceTable: "如 t_customer / t_order",
        parentClassId: "可选，选择父类以继承语义",
        description: "类用途说明",
      },

      propertyLabels: {
        classId: "所属类",
        propertyName: "属性名",
        propertyAlias: "别名",
        dataType: "数据类型",
        sourceColumn: "源字段",
        isPrimaryKey: "主键",
        isForeignKey: "外键",
      },
      propertyPlaceholders: {
        classId: "请选择本体类",
        propertyName: "如 customer_name / order_id",
        propertyAlias: "如 客户名称 / 订单ID",
        sourceColumn: "数据库列名",
      },

      metricLabels: {
        metricName: "指标名",
        metricAlias: "别名",
        aggFunction: "聚合函数",
        targetClassId: "目标类",
        formula: "公式",
        dimensionDefaults: "默认维度（可选）",
      },
      metricPlaceholders: {
        targetClassId: "可选",
        metricName: "如 sales_amount / order_count",
        metricAlias: "如 销售额 / 订单量",
        formula: "如 SUM(order_amount) / COUNT(DISTINCT customer_id)",
        dimensionDefaults: "格式: region:华东,product:手机",
      },

      classOptionLabel: "{name}（{alias}）",
      classOptionLabelNoAlias: "{name}",
      filter: {
        reset: "重置",
      },
    },

    usage: {
      columns: {
        lastQuestion: "最近提问",
        sessionId: "会话 ID",
        totalRequests: "调用次数",
        totalTokens: "Token",
        totalCost: "成本($)",
        lastRequestTime: "最近活动",
        requestTime: "时间",
        purpose: "用途",
        modelName: "模型",
        promptTokens: "Prompt",
        completionTokens: "Completion",
        actions: "合计",
      },
      globalCards: {
        sessions: "会话数",
        requests: "请求数",
        totalTokens: "总 Token",
        totalCost: "总成本($)",
      },
      detailCards: {
        requests: "调用次数",
        totalTokens: "累计 Token",
        totalCost: "累计成本($)",
      },
      trendCards: {
        tokenTrend: "近 30 天 Token 趋势",
        modelDistribution: "模型用量分布",
      },
      detailCardTitle: "会话用量明细：{sessionId}",
      byModelSummary: "按模型汇总",
      byModelRow: "{requests} 次 · {totalTokens} tokens · ${cost}",
      chartLegendTokens: "Tokens",
    },

    serviceStatus: {
      services: {
        postgresql: "PostgreSQL 元数据库",
        neo4j: "Neo4j 图库",
        milvus: "Milvus 向量库",
        embedding: "Embedding 服务",
      },
      status: {
        up: "正常",
        down: "异常",
        notConfigured: "未配置",
      },
      latency: "延迟 {ms} ms",
      checkedAt: "探测时间：{time}",
    },
  },

  localImport: {
    steps: {
      rule: "规则配置",
      preview: "预览确认",
      confirm: "导入完成",
    },
    confirmImport: "确认导入",
    confirm: {
      errorTitle: "导入未完全成功",
      errorSummary:
        "已创建 {createdClasses} 个类、{createdProperties} 个属性、{createdJoins} 个关联，失败 {errorCount} 项",
      errorType: "类型",
      errorName: "名称",
      errorMessage: "错误信息",
    },
    preview: {
      sourceTable: "源表",
      className: "类名",
      propertyCount: "属性数",
      searchPlaceholder: "搜索表名/类名",
      selectFiltered: "全选当前筛选",
      selectedCount: "已选 {count} / 共 {total} 张表",
    },
  },

  datasource: {
    importToOntology: "智能导入到本体",
  },

  enums: {
    datasourceType: {
      oracle: "Oracle",
      postgresql: "PostgreSQL",
      mysql: "MySQL",
    },
    oracleVersion: {
      modern: "12c 及以上（FETCH FIRST）",
      legacy: "11g 及以下（ROWNUM）",
    },
    provider: {
      OPENAI: "OpenAI",
      AZURE_OPENAI: "Azure OpenAI",
      OPENAI_COMPATIBLE_PROXY: "兼容代理 (DeepSeek/通义/Qwen)",
      OLLAMA: "本地 Ollama",
    },
    dataType: {
      STRING: "字符串 STRING",
      INT: "整数 INT",
      DECIMAL: "小数 DECIMAL",
      DATETIME: "日期 DATETIME",
      BOOLEAN: "布尔 BOOLEAN",
    },
    aggFunction: {
      SUM: "求和 SUM",
      AVG: "均值 AVG",
      COUNT: "计数 COUNT",
      MAX: "最大值 MAX",
      MIN: "最小值 MIN",
    },
    entityType: {
      class: "类",
      property: "属性",
      metric: "指标",
    },
  },
} as const;