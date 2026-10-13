"""多步依赖解析 + 失败传播。

L1 限定（multi_step_plan.py:1-12 docstring）：步骤按顺序线性执行，不引入 DAG。
本模块把隐式序号依赖升级为显式字段，但不打破 L1 串行架构。

- topological_groups: 按 execution_group + depends_on 排序，返回分组（每组单步骤）
- validate: 检测 cycle / unknown dep / 越界
- apply_failure: 失败 dep 下游标 skipped
"""

from __future__ import annotations

from app.domain.multi_step_plan import MultiStepPlan, StepPlan, StepResult


class DependencyError(ValueError):
    """依赖图错误（cycle / unknown dep / 越界）。"""


class MultiStepDependencyResolver:
    @staticmethod
    def validate(plan: MultiStepPlan) -> None:
        """Raises DependencyError if:
        - depends_on 引用未知 index（< 0 or >= len(steps)）
        - 检测到 cycle（A depends_on B and B depends_on A）
        """
        n = len(plan.steps)
        for step in plan.steps:
            for dep in step.depends_on:
                # 越界检查
                if dep < 0 or dep >= n:
                    raise DependencyError(
                        f"step {step.index} depends on unknown dep {dep} (plan has {n} steps)"
                    )
        # Cycle 检测：DFS + 路径记录
        visited: dict[int, str] = {}  # index -> "visiting" | "visited"

        def dfs(idx: int, path: list[int]) -> None:
            state = visited.get(idx)
            if state == "visiting":
                # 在当前路径上再次访问 → cycle
                cycle_start = path.index(idx)
                cycle = path[cycle_start:] + [idx]
                raise DependencyError(f"cycle detected: {' -> '.join(map(str, cycle))}")
            if state == "visited":
                return

            visited[idx] = "visiting"
            step = plan.steps[idx]
            for dep in step.depends_on:
                dfs(dep, path + [idx])
            visited[idx] = "visited"

        for step in plan.steps:
            if step.index not in visited:
                dfs(step.index, [])

    @staticmethod
    def topological_groups(plan: MultiStepPlan) -> list[list[int]]:
        """返回分组（每组是该层可执行的步骤 index 列表）。

        L1 串行架构：每组单步骤；execution_group 仅作 metadata 保留。
        顺序：按 plan.steps 原始顺序，depends_on 已满足的前置先排。
        """
        if not plan.steps:
            return []

        n = len(plan.steps)
        # 每个 step 的依赖集合
        deps: dict[int, tuple[int, ...]] = {s.index: s.depends_on for s in plan.steps}
        # 已排好序的 index
        ordered: list[int] = []
        remaining = set(range(n))

        while remaining:
            # 找所有 depends_on 都在 ordered 中的步骤
            ready = [i for i in remaining if all(d in ordered for d in deps.get(i, ()))]
            if not ready:
                # 有剩余但找不到可执行的（理论上被 validate 拦截）
                # 按原始顺序追加剩余的（避免死循环）
                ready = [min(remaining)]
            # L1：每组单步骤，保持原始相对顺序
            for idx in sorted(ready):
                ordered.append(idx)
                remaining.remove(idx)

        # 分组：每组单个 index
        return [[i] for i in ordered]

    @staticmethod
    def apply_failure(
        plan: MultiStepPlan,
        results: list[StepResult],
    ) -> tuple[StepResult, ...]:
        """对 plan 已有 results 应用失败传播：

        - 失败 dep 的下游 step 标 skipped=True + skip_reason
        - 不修改原 results，返回新元组
        - 结果数量 == plan.steps 数（未在 results 中的步骤补成 skipped）
        """
        if not plan.steps:
            return ()

        # 已有结果：按 step_index 索引
        existing: dict[int, StepResult] = {r.step_index: r for r in results}
        # 失败的 step_index 集合（sql is None and error is not None）
        failed: set[int] = {
            r.step_index for r in results
            if r.sql is None and r.error is not None
        }
        # skipped 的 step_index 集合
        already_skipped: set[int] = {r.step_index for r in results if r.skipped}

        # 构建完整结果：按 plan.steps 顺序
        all_step_indices = [s.index for s in plan.steps]
        completed: list[StepResult] = list(results)

        # 需要补全的 index（在 results 中不存在）
        missing_indices = [i for i in all_step_indices if i not in existing]

        # 从 missing 中挑那些依赖链上有失败步骤的，标 skipped
        # 迭代处理：被 skip 的步骤也会导致其下游被 skip
        skipped_by_dep: dict[int, str] = {}  # index -> skip_reason

        def compute_skipped(idx: int, path: list[int]) -> None:
            """递归：如果依赖链上有失败，标 idx 为 skipped"""
            if idx in skipped_by_dep or idx in already_skipped:
                return
            step = plan.steps[idx]
            for dep in step.depends_on:
                if dep in failed:
                    reason = f"依赖步骤 {[d for d in step.depends_on if d in failed]} 失败"
                    skipped_by_dep[idx] = reason
                    return
                if dep in skipped_by_dep:
                    reason = f"依赖步骤 {[d for d in step.depends_on if d in skipped_by_dep or d in failed]} 失败"
                    skipped_by_dep[idx] = reason
                    return
                # 递归向上查
                if dep not in existing:
                    # dep 未执行且未在 results 中，检查 dep 是否会在 missing 中被处理
                    # 实际上应该递归查 dep 的依赖
                    pass

        # 广度优先：按拓扑顺序处理
        for idx in all_step_indices:
            step = plan.steps[idx]
            if step.index in existing or step.index in skipped_by_dep:
                continue
            # 检查直接依赖是否有失败的或已 skip 的
            failed_deps = [d for d in step.depends_on if d in failed]
            skipped_deps = [d for d in step.depends_on if d in skipped_by_dep or d in already_skipped]
            if failed_deps:
                skipped_by_dep[step.index] = f"依赖步骤 {failed_deps} 失败"
            elif skipped_deps:
                skipped_by_dep[step.index] = f"依赖步骤 {skipped_deps} 失败"

        # 构建最终结果元组
        final_results: list[StepResult] = []
        for step in plan.steps:
            idx = step.index
            if idx in existing:
                final_results.append(existing[idx])
            elif idx in skipped_by_dep:
                final_results.append(
                    StepResult(
                        step_index=idx,
                        description=step.description,
                        sub_question=step.sub_question,
                        skipped=True,
                        skip_reason=skipped_by_dep[idx],
                    )
                )
            else:
                # 有 plan 步骤但不在 results 中，也没被标 skip（理论上不会发生）
                final_results.append(
                    StepResult(
                        step_index=idx,
                        description=step.description,
                        sub_question=step.sub_question,
                    )
                )

        return tuple(final_results)
