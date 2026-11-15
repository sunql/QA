"""P3a D19：语义注册表 RAG 的**过滤维度**（不可变值对象）。

这个对象回答的是「**这一轮召回允许看到哪些治理维度**」。它**不是**授权策略：
本仓至今没有「用户 → 可见域/组织」的策略源（现有 ACL 只有 admin 级的
``assertCanModify``），所以过滤器由**调用方显式给出**；P3a 交付的是
**过滤机制**，策略接线是另一次受控变更。

语义（同维度内 OR、跨维度 AND）
------------------------------
- ``domainCodes``：命中任一数据域即可（主域 + 附域都在向量行的 ``domain_codes`` 里）；
- ``orgCodes``：命中任一组织即可；
- ``entityTypes``：命中任一业务对象码即可；
- ``releaseId``：只取该语义发布版本写入的向量；
- ``validAt``：在该时刻有效（``valid_to == 0`` 视为长期有效）。

未归维的行（空数组 / ``entity_type=""`` / ``release_id=0``）在对应维度上
**不命中** ⇒ 被排除（fail-closed）。这是**可解释的收窄**，不是泄漏：
未归域是数据事实（本地只有 6/55 个类有域），过滤只是如实反映它。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RegistryFilter:
    """一轮向量召回的过滤维度（不可变）。

    Attributes:
        domainCodes: 允许的数据域码（主域 + 附域）。
        orgCodes: 允许的组织码。
        entityTypes: 允许的业务对象码。
        releaseId: 只取该语义发布版本（``None`` = 不按发布过滤）。
        validAt: 只取该时刻有效的行（epoch 毫秒，``None`` = 不按有效期过滤）。
        isScopeBound: 调用方**声明了作用域**（哪怕各维度取值为空）。
            「我这一轮有权看的域是空集」= **什么都不许看到**（deny-all），
            与「没有作用域、随便看」是两件事 —— 必须能区分，否则
            「空作用域」会被当成「无作用域」⇒ 恰好在本该最严格的一轮
            变成全量可见。
    """

    domainCodes: tuple[str, ...] = ()
    orgCodes: tuple[str, ...] = ()
    entityTypes: tuple[str, ...] = ()
    releaseId: int | None = None
    validAt: int | None = None
    isScopeBound: bool = False

    def hasScope(self) -> bool:
        """是否带了任何作用域约束。

        决定降级口径：**有作用域**时，v2 不可用必须 fail-closed（空结果），
        不许回落成「无过滤的 v1」——那等于把越权行放进来。

        「有作用域」= 给了任一维度的取值 **或** 调用方显式声明了作用域
        （``isScopeBound=True``）。
        """
        return bool(
            self.isScopeBound
            or self.domainCodes
            or self.orgCodes
            or self.entityTypes
            or self.releaseId is not None
            or self.validAt is not None
        )
