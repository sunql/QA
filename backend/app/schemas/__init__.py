"""API DTO（Data Transfer Object）包。

新模块请按 domain 拆分，避免把所有 Pydantic schema 塞进 ``app/domain/schemas.py``。
本包下模块统一继承 :class:`app.domain.schemas.CamelModel`，由 alias_generator
自动产出 camelCase JSON 字段名。
"""