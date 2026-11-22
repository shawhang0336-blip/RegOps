"""基础设施层（Infra，Layer 0）：配置、数据库、ORM 模型、共享契约与受控 LLM 底座。

本层为全后端最底层（严禁依赖 kernel / policy 或任何上层业务包），根目录 7 个核心模块 + `llm/` 子包：
- `config.py`：环境配置 `Settings`、运行预算 `DEFAULT_BUDGET`/`get_budget` 与操作人占位常量 `OPERATOR_PLACEHOLDER`；
- `db.py`：MySQL/Oracle 生产连接池、`Base`/`SessionLocal`/`init_db` 与跨方言安全标识符转义 `quote_identifier`；
- `models.py`：全部 20 张 ORM 表统一定义（运行时账本 / 文档与资产元数据 / 生成规划与产物）；
- `domain.py`：跨层共享 Pydantic 契约（`FieldSpec` / `SpecDocument` / `RuleDefinition` / Binding / `LLM_OPERATORS`）；
- `errors.py`：统一领域异常 `WorkflowError`；
- `blob_store.py`：基于 SHA-256 内容寻址与原子临时文件替换的不可变大对象存储（`LocalBlobStore`）；
- `task_runner.py`：带任务键去重的进程内有界后台任务执行器（`TaskRunner` / `task_runner`）；
- `llm/`：受控大模型子包（`client.py` 传输与连接池 / `schemas.py` 12 工具契约 / `guard.py` 容错守卫）。
"""
