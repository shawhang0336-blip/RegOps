"""数据资产中心（Assets）：元数据模型资产 + 码表子域。

模块导航：
- `metadata_domain.py`：元数据提取草稿 Pydantic 契约；
- `metadata_views.py`：原文证据校验、工作台视图序列化与 `metadata_to_spec` 转换；
- `metadata_mutations.py`：单字段编辑、表删除、版本发布与 CSV/XLSX 批量类型导入；
- `metadata_service.py`：确定性优先 + AI 辅助元数据提取服务及统一聚合入口；
- `readiness.py`：元数据资产就绪度检查；
- `code_sets/`：码表解析、引用自动关联、批量抽取、就绪检查与 Oracle 码表 DDL 编译。
"""
