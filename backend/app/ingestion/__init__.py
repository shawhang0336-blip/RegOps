"""文档接入与结构解析层（Ingestion，Layer 3，阶段一）。

目录结构（按“纯离线解析器”与“落库/AI 服务”清晰隔离）：
- `parsers/`：纯函数文档拆箱、PDF 目录/字典解析与确定性硬伤体检（零 DB、零 LLM）；
  - `extractors.py`：PDF / Word / Excel 统一抽块入口（消费 AI ① 切块蓝图）与文档摘要 survey；
  - `pdf_catalog.py`：外管局已知规范高精插件（目录与附录公共码表扫描）；
  - `pdf_generic.py`：通用监管 PDF 二级章节切块（`HEADING_SECTIONS`）与三级分页兜底（`PAGE_WINDOWS`）；
  - `pdf_parser.py`：PDF `pdftotext` 提取、表格折行重排与字段精拆；
  - `quality.py`：原始块零 AI 确定性硬伤体检；
- `structure_service.py`：AI 辅助整文档体检、跨页合并/拆分切块与单块内部语义细分；
- `import_progress.py`：解析实时进度跟踪与 `document.progress` 总线广播。
"""
