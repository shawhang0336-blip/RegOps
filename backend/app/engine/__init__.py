"""规则装配与沙箱执行引擎（Engine，阶段二）。

模块导航：
- `domain.py`：核心 `RuleDefinition` / `SpecDocument` 模型与受控算子白名单 `LLM_OPERATORS`；
- `rules.py`：单条规则校验逻辑与样本违规判定；
- `compiler.py`：规则 → Oracle / MySQL 校验 SQL 编译器；
- `binding.py`：Schema / Rule / Generator / AuditCase 的确定性绑定与校验；
- `llm.py`：受控 Tool-Call 大模型客户端与并发限流；
- `oracle.py`：Oracle 沙箱管理（`OracleSandbox`）与真跑校验执行器（`GenericOracleExecutor`）；
- `workflow`：基于 ProdAgent 微内核（`app.kernel`）与 RegOps 策略（`app.policy`）的流水线节点编排与执行入口。
"""
