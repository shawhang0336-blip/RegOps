"""Step 2 规则生成阶段聚合门面（Layer 5: generation/workflow/stages）。

仅聚合 Step 2 阶段（预算规划、表结构编译、单表/跨表规则绑定与 Oracle 空跑准备）；
Step 3 的黄金造数（`stage_data.py`）与变异用例（`stage_audit.py`）已归位至 Layer 6 `app.delivery.testing`。
"""

from app.generation.core.rule_backfill import (
    _code_set_enum_backfill_candidates,
    _declaration_quote,
    _fixed_value_candidates,
    _length_backfill_candidates,
    _required_backfill_candidates,
)
from app.generation.workflow.stage_plan_schema import (
    _plan_stage,
    _provider_name,
    _schema_stage,
    _validate_plan,
)
from app.generation.workflow.stage_rules import (
    _dedupe_approved_rules,
    _drop_unsatisfiable_field_compare_eq,
    _ensure_oracle_assets,
    _rebind_failed_rules,
    _rules_stage,
    _summarize_deterministic_rule,
    bind_table_rules_worker,
    join_table_rules_barrier,
    prepare_deterministic_rule_index,
)

__all__ = [
    "_code_set_enum_backfill_candidates",
    "_declaration_quote",
    "_dedupe_approved_rules",
    "_drop_unsatisfiable_field_compare_eq",
    "_ensure_oracle_assets",
    "_fixed_value_candidates",
    "_length_backfill_candidates",
    "_plan_stage",
    "_provider_name",
    "_rebind_failed_rules",
    "_required_backfill_candidates",
    "_rules_stage",
    "_schema_stage",
    "_summarize_deterministic_rule",
    "_validate_plan",
    "bind_table_rules_worker",
    "join_table_rules_barrier",
    "prepare_deterministic_rule_index",
]
