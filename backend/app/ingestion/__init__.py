"""文档接入与结构解析层（Ingestion，Layer 3，阶段一）。

目录结构（按“纯离线解析器”与“落库/AI 服务”清晰隔离）：
- `parsers/`：纯函数文档拆箱、PDF 目录/字典解析与确定性硬伤体检（零 DB、零 LLM）；
  - `extractors.py`：PDF / Word / Excel 原始块提取与文档摘要 survey；
  - `pdf_catalog.py`：PDF 章节目录与附录公共码表扫描；
  - `pdf_parser.py`：PDF `pdftotext` 提取、表格折行重排与字段精拆；
  - `quality.py`：原始块零 AI 确定性硬伤体检；
- `structure_service.py`：AI 辅助整文档体检、跨页合并/拆分切块与单块内部语义细分；
- `import_progress.py`：解析实时进度跟踪与 `document.progress` 总线广播。
"""
