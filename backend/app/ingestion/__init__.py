"""文档接入与结构解析层（Ingestion，阶段一）。

模块导航：
- `extractors.py`：PDF / Word / Excel 原始文本与块抽取；
- `catalog.py`：文档目录与章节识别；
- `spec_parser.py`：监管数据字典表与字段结构解析；
- `spec_quality.py`：原始块解析质量体检；
- `structure_service.py`：AI 辅助文档切块与体系结构识别；
- `segment_service.py`：父子层级段落切分服务；
- `import_progress.py`：解析实时进度跟踪与 `document.progress` 总线广播。
"""
