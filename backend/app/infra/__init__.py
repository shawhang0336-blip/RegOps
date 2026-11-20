"""基础设施层（Infra）：配置、数据库连接与会话、异常体系、操作人身份与 ORM 数据模型。

模块导航：
- `config.py`：环境配置 `Settings` 与 `.env` 加载；
- `db.py`：SQLAlchemy `Base`、`SessionLocal` 与表结构初始化；
- `errors.py`：领域统一异常 `WorkflowError`；
- `identity.py`：操作人身份占位符常量；
- `models.py`：核心运行时 ORM（`RunModel` / `EventModel` / `RuleModel` / `SpecificationModel` / `SettingModel`）；
- `asset_models.py`：文档、元数据、码表、审批与产物资产 ORM。
"""
