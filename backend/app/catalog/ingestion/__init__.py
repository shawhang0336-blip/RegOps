"""文档拆箱与结构解析子域（Catalog Ingestion）。

职责边界（严禁反向依赖 `catalog/tables/` 或 `catalog/code_sets/`）：
- `parsers/`：纯离线文件解析、通用三级 PDF 切块与确定性硬伤体检（零 DB、零 LLM、零 kernel/policy）；
- `structure_service.py`：AI 辅助整文档体检（产出切块蓝图）、跨页合并/切块与单块语义细分；
- `import_progress.py`：文档导入实时进度跟踪与 `document.progress` 总线广播。
"""
