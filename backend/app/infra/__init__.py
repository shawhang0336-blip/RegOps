"""基础设施层（Infra）：配置、数据库、共享领域契约、受控 LLM 客户端与 ORM 数据模型。

本层为全后端最底层（严禁依赖其他任何业务或内核包）：
- `config.py`：环境配置 `Settings` 与 `.env` 加载；
- `db.py`：SQLAlchemy `Base`、`SessionLocal` 与表结构初始化；
- `errors.py`：领域统一异常 `WorkflowError`；
- `identity.py`：操作人身份占位符常量；
- `domain.py`：跨层共享契约（`FieldSpec` / `SpecDocument` / `RuleDefinition` / Binding 模型 / `LLM_OPERATORS` / `field_map`）；
- `sql.py`：跨方言（Oracle / MySQL）安全 SQL 标识符转义；
- `llm.py`：受控 Tool-Call 大模型客户端与并发限流；
- `settings_store.py`：运行时执行预算配置（`DEFAULT_BUDGET` / `get_budget`）；
- `task_runner.py`：带任务键去重的进程内有界后台任务执行器（`TaskRunner` / `task_runner`）；
- `models.py`：核心运行时 ORM（`RunModel` / `EventModel` / `RuleModel` / `SpecificationModel`）；
- `asset_models.py`：文档、元数据、码表、审批与产物资产 ORM。
"""
