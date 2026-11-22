"""Step 0：监管文档接入与结构解析（Layer 3: Ingestion）。

职责边界：
- `parsers/`：纯离线文件拆箱、通用三级 PDF 切块与确定性硬伤体检（零 DB、零 LLM、零 kernel/policy）；
- `structure_service.py`：AI 辅助整文档体检（产出切块蓝图）、跨页合并/切块与单块语义细分；
- `import_progress.py`：文档导入实时进度跟踪与 `document.progress` 总线广播。
"""
