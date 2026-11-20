"""数据资产中心（Assets）：元数据模型资产 + 码表子域。

模块导航：
- `metadata_domain.py`：元数据提取草稿 Pydantic 契约；
- `metadata_service.py`：表/字段提取、编辑、版本发布、类型导入与转 `SpecDocument`；
- `readiness.py`：元数据资产就绪度检查；
- `code_sets/`：码表解析、字段引用关联、抽取发布、就绪检查与 Oracle 码表 DDL 编译。
"""
