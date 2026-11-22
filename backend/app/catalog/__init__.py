"""Step 1：监管数据目录（Layer 4: Catalog，100% 脱离具体数据库 SQL 方言）。

职责边界与内部单向红线：
- `code_sets/`：码表字典子域（纯函数解析 / 字段对齐 / 抽取复核 / 就绪度 / 发布态值域绑定，严禁反调 `tables/`）；
- `tables/`：表与字段元数据子域（纯草稿契约 / 视图 / 编辑发布合并 / 抽取服务 / 交付就绪度）；
- `extraction.py`：跨子域双轨并发抽取协同（表元数据 + 码表 + 自动对齐）；
- `pipeline_run.py`：Step 0 + Step 1 文档导入与解析统一 Run 控制面（基于 `kernel.run` + `BudgetGuard` + 取消/重启回收）；
- `version_diff.py`：监管制度跨版本表/字段差异对比与影响面分级。
"""
