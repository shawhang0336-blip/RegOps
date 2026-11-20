"""生产交付与业务库只读校验层（Delivery，阶段三）。

模块导航：
- `production.py`：生产校验包结构定义与只读规则 SQL 组装；
- `production_service.py`：面向已确认 Run 的生产包生成与生产校验执行服务；
- `business_db.py`：业务库只读抽样探查连接器（`ReadOnlyBusinessDatabase`）。
"""
