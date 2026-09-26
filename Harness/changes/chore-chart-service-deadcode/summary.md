# 变更：chore-chart-service-deadcode

- **日期**：2026-09-26
- **作者**：Claude / 启琳
- **Phase**：Phase 4 图表渲染（`app/services/chart_service.py` + 新增守卫 `app/tests/unit/test_no_duplicate_methods.py`）
- **状态**：done
- **关联变更**：[refactor-score-ssot](../refactor-score-ssot/summary.md)（同批次）、[fix-plan-drop-observability](../fix-plan-drop-observability/summary.md)（同批次）
- **迁移版本**：无
- **SSOT 出处**：`Harness/wiki/chat-service-assessment.md` §2.3 **M6** + §3 P2 第 12 项（评估日期 2026-09-25）
- **commit**：`aefabd3`

---

## 1. 需求

`ChartService` 里 `_buildOptionPrompt` **定义了两遍**（`:121`、`:226`，**两份都是 `@staticmethod`，签名与函数体逐字节相同**），Python 对类体重复方法**零告警**，后者静默覆盖前者 ⇒ 前一份约 26 行是死代码，且让读者以为有两套逻辑。同文件还堆积了同类腐化：2 处未使用导入（`is_number`、`looks_like_datetime`，ruff F401）、一个零调用的 `_inferColumnType`（单数，真正在用的是复数 `_inferColumnTypes`）及其专用导入、导入块顺序违规（I001）。

用户视角的影响：**没有直接的用户可见缺陷** —— 这正是它危险的地方：死代码不会报错，只会让后续改动「改了一半不生效」（改到被覆盖的那份上），且 `chart_service.py` 的 ruff 状态长期非零，使新增告警淹没在噪声里。

**验收标准**：`_buildOptionPrompt` 只剩一处；同文件零调用符号与未用导入清零；`chart_service.py` 的 ruff 告警归零；**新增一道能拦住下一处同类腐化的常驻守卫**。

## 2. 设计评审

### 候选方案（守卫的形态，本批的主要设计决策）

| # | 方案 | 优点 | 否决理由 |
|---|---|---|---|
| A | 只删重复方法，不加守卫 | diff 最小 | 本缺陷的成因是「Python 不报错 + 人读漏」，删完下次照样复发；且守不住「改到被覆盖那份」这种更隐蔽的变体 |
| B | **AST 扫描 `app/` 全树的类体，同名方法出现 ≥ 2 次即失败**（选定） | 零依赖（标准库 `ast`）、扫全树而非单文件、报错信息带行号可直接定位 | 需要定义例外（`@overload` / `@property`+`setter` 等**合法**的同名重定义），否则误报 |
| C | 用 ruff/pylint 现成规则 | 不写代码 | 现装工具链（ruff）**没有**这条规则；引入 pylint 是为一件事加一个工具与一套配置 |
| D | 只在 `chart_service.py` 加一个针对性断言 | 最简单 | 只防一个文件、只防一种形态，属「给已修的 bug 写纪念碑」，不是守卫 |

### 例外口径（方案 B 的必要组成）

- 允许同名重定义的装饰器后缀白名单：`overload` / `property` / `.setter` / `.deleter` / `.getter`（覆盖 `cached_property`）。
- 方法体内嵌套的 `def`（局部函数）不算类体方法；`if`/`try`/`with`/`for` 等复合语句内部的 `def` **算**（`if TYPE_CHECKING:` 下定义同名方法是真实风险）。
- **「守卫的守卫」**：守卫自身带一个合成重复方法的用例，证明它真能报出来（否则守卫退化成永远通过的空断言）。

### 最终决定

方案 B，同时按用户口径把同文件死代码一并清掉（2 处 F401 + 零调用 `_inferColumnType` + I001），避免「清一半」后 ruff 仍非零。

## 3. 数据模型变更

无。不涉及表、列、索引、迁移脚本（`迁移版本：无`）。

## 4. 接口契约变更

无。`ChartService.buildOption(...)`（唯一调用点 `:72`）签名、行为、返回结构均未变；删除的 `_buildOptionPrompt` 是**从未被执行过**的那一份，被删除的 `_inferColumnType` 与两处导入零引用（grep 确认，见 §6）。

## 5. 实现要点

| 位置 | 改动 |
|---|---|
| `app/services/chart_service.py` | 删除 `:120-146`（含 `@staticmethod` 的第一份 `_buildOptionPrompt`，被 `:226` 覆盖的那份）；删除零调用的 `_inferColumnType`（单数）及其专用导入 `infer_column_type`；删除未使用导入 `is_number` / `looks_like_datetime`；重排导入块（I001） |
| `app/tests/unit/test_no_duplicate_methods.py`（新增） | `_APP_DIR = Path(app.__file__).resolve().parent`；`_collectDefs` 递归收集类体 `def`（跳过方法体内嵌套、保留复合语句内的）；`_isAllowedDuplicate` 按装饰器后缀白名单放行；`_findDuplicateMethods` 返回 `文件 :: 类 :: 方法 @ [行号…]`；4 个用例（合成重复必报 / overload·property 不报 / 嵌套 def 不报 / 全树扫描为空） |

删除前用 AST 复核（2026-09-26 实录）：两份均 `@staticmethod`、参数 `['chartType','columns','data','question']`、**去掉 def 行后的源码段逐字节相同**（`ast.get_source_segment` 比对 = `True`）⇒ 删除是**可证**的行为保持；并确认 `_inferColumnType` 零引用（用户口径 3：只删「重复方法 + 同文件死代码」，不顺手改行为）。

## 6. 测试

- **RED（先写守卫，后删代码）**：守卫首次运行报出**唯一**一条告警 —— 把 M6 之前的 `chart_service.py` 喂给守卫函数，重现同一字符串（2026-09-26 复跑留证）：

  ```
  app/services/chart_service.py :: ChartService :: _buildOptionPrompt @ [121, 226]
  ```

- **GREEN**：删除后 `test_no_duplicate_methods.py` + `test_chart_service.py` 全绿。
- **零引用证据**：`grep -c "def _buildOptionPrompt" app/services/chart_service.py` = **1**；`_inferColumnType`（单数）与两处导入在 `app/` 全树零引用。
- **静态检查净改善**：`ruff check app/services/chart_service.py` 由 **3 errors → All checks passed**。
- 覆盖率：本批为删除 + 新增一个测试文件，不引入新的未覆盖生产分支。

### 审查整改：守卫自身的两个缺口（code-reviewer LOW-4，2026-09-26）

守卫是本批**新建的常驻闸门**，审查指出它的例外判定写错了、且下钻不全 —— 这两点都会让守卫
在将来「看似在守、实则不守」，故按 TDD 补齐（`test_no_duplicate_methods.py` 现为 **7 个测试函数**）：

| 缺口 | 症状 | 整改 |
|---|---|---|
| `all(_isAllowedDuplicate(d) for d in defs)` | **误报**：标准 `typing.overload` 形态是「若干 `@overload` 存根 + 1 个无装饰器的实现」，`all(...)` 必为 `False` ⇒ 合法写法被拦；而当前全树无此形态，所以只是**潜伏**的误报 | 改为按**重定义角色**判定（`overload` / `getter` / `setter` / `deleter`）：有普通方法参与时只允许「其余全是 `@overload` 且普通方法恰 1 个」；纯 property 家族则要求**每种角色至多一次** |
| `_COMPOUND_STMTS` 缺 `ast.Match` | **漏检**：`match` 的 case body 也是类体作用域，但不在 `stmt.body` 上 ⇒ 定义在 case 里的同名方法不被收集 | 单独下钻 `stmt.cases[*].body` |

**RED 证据（实测：把 `3309a71~1` 的旧守卫取出来跑同一份合成源码）**：

```
修复前守卫 · match case 源码   -> []                              （[] = 漏检）
修复前守卫 · overload + 实现   -> ['<synthetic> :: A :: f @ [3, 5, 6]']  （非空 = 误报）
```

**整改不得以「放宽」为代价**：放宽 overload 的同时必须**still 报**真重复。已配对偶用例
`test_guard_still_reports_two_getters_alongside_an_implementation`（「两个 `@property` 同名 getter +
一个普通实现」仍照报 `@ [3, 5, 6]`），证明放宽是**有边界的**而非把闸门拆了。

另：守卫现共 7 例，其中 `test_no_duplicate_method_definitions_anywhere_in_app` 扫全 `app/` 树。

## 7. 安全审查

**未触发**：不涉及认证、密钥、SQL、用户输入、文件操作、加解密、支付。本批不改变任何运行时行为（删除的是死代码），因此不引入新的攻击面；反向地，它**减少**了「改动落在被覆盖的旧方法上、以为生效实则没有」这类静默失效风险（属可维护性安全，非漏洞）。同批送审结论见 §8。

## 8. 部署验证（2026-09-26）

- **镜像重建**（非 `docker cp`）：`docker compose build backend && docker compose up -d backend`；
- **镜像级证据**：用镜像 `sha256:b6694892e89e…` 起一次性容器读 `app/services/chart_service.py` ⇒ `grep -c "def _buildOptionPrompt"` = **1**（旧镜像此处为 2）——即「删掉的那一份」确实没有进部署物；
- **仓库 ↔ 容器 md5**：`chart_service.py` **MATCH**（本批 12 个文件 12/12 MATCH）；
- **容器内真机探针**（真实模块）**21 项全 PASS**，其中本变更相关 3 项：

  ```
  PASS  M6 _buildOptionPrompt 只有一处
  PASS  M6 _inferColumnType(单数) 已删
  PASS  M6 _inferColumnTypes(复数) 保留
  ```

- **静态检查**：`ruff check app/services/chart_service.py` 由 **3 errors → All checks passed**（本批不引入新告警，新增的守卫文件亦通过）；
- **测试**：全量 unit（最终 hash `3309a71`）`2 failed, 2411 passed, 1 skipped`，两条失败均为**预存**（与本批无关，见 `../refactor-score-ssot/summary.md` §8 的判别实验）；守卫自身 7 例全绿，其中 `test_no_duplicate_method_definitions_anywhere_in_app` 扫全 `app/` 树零命中 ⇒ 全树已无同类重复方法；
- **网关**：`8000` 直连与经 nginx `5173` 的 `/api/v1/health` 均 200。

## 9. 关联

- SSOT / 评估文档：`Harness/wiki/chat-service-assessment.md` §2.3 M6、§3 P2 第 12 项、新增 §14（本次标 ✅）
- 复核修正记录：§2.3 M6 行内已注明「`_consumedTokens` 重复」一条**不成立**（评估当日笔误，当前只剩一处定义）—— 本批只处理成立的那条
- 代码契约：`app/tests/unit/test_no_duplicate_methods.py`（常驻守卫，扫 `app/` 全树）
- 规则：根 `CLAUDE.md` 核心约束 #5（小文件/清理）、`Harness/rules/编码规范.md`、`Harness/rules/开发流程规范.md`（TDD）
- Memory：`qa-system-score-ssot-plan-drop.md`（新增，含「Python 类体重复方法零告警」这一教训）
- 关联变更：`../refactor-score-ssot/summary.md`、`../fix-plan-drop-observability/summary.md`

## SSOT 校验清单

- [x] 第 1 段 需求：重复方法（同名同签名同函数体）+ 同文件死代码清单 + 「无直接用户缺陷、危险在于静默」
- [x] 第 2 段 4 个候选方案对比（守卫形态为本批主要设计决策）+ 例外口径 + 「守卫的守卫」
- [x] 第 3 段 无 alembic 迁移（纯删除 + 新增测试文件）
- [x] 第 4 段 接口契约变更 = 无（删的是**从未被执行过**的那一份）
- [x] 第 5 段 实现要点逐位置表（删除行号 + 守卫结构）+ **删除可证性**：`ast.get_source_segment` 比对两份函数体逐字节相同
- [x] 第 6 段 测试：RED 实录（守卫在旧文件上报出唯一一条）+ GREEN + 零引用证据 + ruff 3 → 0
- [x] 第 6 段附 审查整改：守卫两个缺口（`overload` 误报 / `match` case 漏检）+ 实测 RED 输出 + 反放宽配对用例
- [x] 第 7 段 安全审查：未触发（并说明属可维护性安全，非漏洞）
- [x] 第 8 段 部署验证：镜像级证据（`grep -c` = 1）+ md5 MATCH + 探针 3 项 + 网关 200
- [x] 第 9 段 跨文件链接（评估文档 §14 / 守卫代码契约 / 规则 / Memory / 关联变更）
- [x] `Harness/wiki/chat-service-assessment.md` §2.3 M6 标 ✅（含「`_consumedTokens` 重复」不成立的复核注）+ §14 批次记录
