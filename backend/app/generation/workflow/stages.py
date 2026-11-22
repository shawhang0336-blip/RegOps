"""阶段二执行阶段兼容聚合门面（Workflow Stages Facade）。

原 1,355 行单文件已按职责拆分为四个独立模块：
- `stage_plan_schema.py`：预算规划与确定性 Schema 编译；
- `stage_rules.py`：单表规则 BSP Worker、跨表外键汇聚、Oracle 资产预建、去重与靶向自愈；
- `stage_data.py`：单表黄金造数 BSP Worker、跨表外键对齐与确定性规则回填；
- `stage_audit.py`：单表审计变异 BSP Worker 与用例屏障汇聚。

本模块保留全部兼容导出，避免外部调用方与单测改路径。
"""

from app.generation.core.rule_backfill import (
    _code_set_enum_backfill_candidates,
    _declaration_quote,
    _fixed_value_candidates,
    _length_backfill_candidates,
    _required_backfill_candidates,
)
from app.generation.workflow.stage_audit import (
    _audit_stage,
    bind_table_audit_worker,
    join_table_audit_barrier,
)
from app.generation.workflow.stage_data import (
    _GENERATOR_BATCH_SIZE,
    _SEMANTIC_GENERATOR_OPERATORS,
    _backfill_fixed_value_rules,
    _backfill_length_rules,
    _backfill_required_rules,
    _coupled_field_names,
    _data_stage,
    _deterministic_generator_item,
    _next_asset_version,
    _partition_coupled_chunks,
    bind_table_data_worker,
    join_table_data_barrier,
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
    "_GENERATOR_BATCH_SIZE",
    "_SEMANTIC_GENERATOR_OPERATORS",
    "_audit_stage",
    "_backfill_fixed_value_rules",
    "_backfill_length_rules",
    "_backfill_required_rules",
    "_coupled_field_names",
    "_data_stage",
    "_dedupe_approved_rules",
    "_deterministic_generator_item",
    "_drop_unsatisfiable_field_compare_eq",
    "_ensure_oracle_assets",
    "_next_asset_version",
    "_partition_coupled_chunks",
    "_plan_stage",
    "_provider_name",
    "_rebind_failed_rules",
    "_rules_stage",
    "_schema_stage",
    "_summarize_deterministic_rule",
    "_validate_plan",
    "bind_table_audit_worker",
    "bind_table_data_worker",
    "bind_table_rules_worker",
    "join_table_audit_barrier",
    "join_table_data_barrier",
    "join_table_rules_barrier",
    "prepare_deterministic_rule_index",
]
