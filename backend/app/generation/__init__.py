"""Step 2：表结构、码表 SQL 与校验规则生成层（Layer 5: Generation，对应前端 02 /generation）。

三大正交子包：
- `core/`：纯离线领域核（`rules.py` 规则校验 / `binding.py` 契约绑定与预判 / `planning.py` 确定性规划与语义闸门 / `rule_backfill.py` 四类机械规则派生）；
- `sql/`：数据库方言编译与 Oracle 空跑准备（`compiler.py` / `code_set_sql.py` / `oracle.py`）；
- `workflow/`：Step 2 前半段 BSP 规则生成节点与运行时（`runtime.py` / `stage_plan_schema.py` / `stage_rules.py` / `nodes_front.py`）。

注：Step 3 的测试数据生成、AB 沙箱真跑验证、全流程调度入口与生产包导出全部收归 Layer 6 `delivery/`。
"""
