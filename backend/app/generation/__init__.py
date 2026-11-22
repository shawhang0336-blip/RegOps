"""Step 2：规则生成与沙箱验证（Layer 5: Generation，对应前端「02 生成表结构、码表和规则」+「03 沙箱测试」）。

三大正交子包 + 跨批次差异对比：
- `core/`：纯离线规则校验、确定性派生、绑定与反例变异内核（AST 锁死零 DB/LLM/SQL/Workflow 依赖）；
- `sql/`：MySQL 8 / Oracle 23 校验 SQL 编译器、码表 DDL/UPSERT 编译与 Oracle 沙箱空跑/实战执行器；
- `workflow/`：18 节点 BSP 编排流水线与 Run 生命周期唯一业务门面（含全部 6 个受控 AI 介入点）；
- `run_diff.py`：跨验证批次规则与 SQL 差异对比引擎。
"""
