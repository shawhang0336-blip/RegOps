"""第 1 步业务域：监管规范解析与数据目录（Layer 3: Catalog，1:1 对应前端「01 规范解析与核对」+「变更记录」）。

职责边界与内部严格单向依赖（由 AST 红线测试强制守卫）：
- `ingestion/`：文档拆箱、PDF/Word/Excel 解析（`parsers/` 零 DB/LLM）、AI 结构体检/切块与实时进度（严禁反调 `tables/` 或 `code_sets/`）；
- `code_sets/`：码表字典子域（纯函数解析 / 字段对齐 / 抽取复核 / 就绪度 / 发布态值域绑定，严禁反调 `tables/`）；
- `tables/`：表与字段元数据子域（纯草稿契约 / 视图 / 编辑发布合并 / 抽取服务 / 交付就绪度）；
- `extraction.py`：跨子域双轨并发抽取协同（表元数据 + 码表 + 自动对齐）；
- `pipeline_run.py`：Step 1 文档导入/解析统一 Run 控制面（原 `engine/ingestion_run.py` 归位，基于 `kernel.run` + `BudgetGuard` + 取消/重启回收）；
- `version_diff.py`：监管制度跨版本表/字段差异对比与影响面分级。
"""
