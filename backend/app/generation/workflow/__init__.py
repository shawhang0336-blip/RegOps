"""Step 2 规则生成工作流（Layer 5: generation/workflow，对应前端 02 /generation）。

职责边界：
- `runtime.py`：受控 LLM 调用、并发上下文映射、BSP 扇出与规则落库；
- `stage_plan_schema.py`：确定性生成规划与 Schema 编译；
- `stage_rules.py`：单表规则 BSP Worker、跨表外键汇聚、语义去重、靶向自愈重绑与 Oracle 空跑准备；
- `nodes_front.py`：Step 2 前半段节点体装配（`_front_bodies`）。

注：Step 3 的造数、AB 沙箱真跑与全流程调度入口已收归 Layer 6 `app.delivery.testing` 与 `app.delivery.workflow`。
"""

from app.generation.core.rule_backfill import (
    _code_set_enum_backfill_candidates,
    _declaration_quote,
    _fixed_value_candidates,
    _length_backfill_candidates,
    _required_backfill_candidates,
)
from app.generation.workflow.nodes_front import _front_bodies
from app.generation.workflow.runtime import (
    _BACKOFF_SECONDS,
    _TRANSIENT_ATTEMPTS,
    _VALIDATION_ATTEMPTS,
    ERROR_SUGGESTIONS,
    STAGE_BY_TOOL,
    _call_validated,
    _code_set_context,
    _emit,
    _error_details,
    _fail_context,
    _fanout,
    _inner_width,
    _latest_payload,
    _locate_payload,
    _map_with_context,
    _pending_rules,
    _resolve_budget,
    _specs,
    _upsert_rule,
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
    "_BACKOFF_SECONDS",
    "_TRANSIENT_ATTEMPTS",
    "_VALIDATION_ATTEMPTS",
    "ERROR_SUGGESTIONS",
    "STAGE_BY_TOOL",
    "_call_validated",
    "_code_set_context",
    "_code_set_enum_backfill_candidates",
    "_declaration_quote",
    "_dedupe_approved_rules",
    "_drop_unsatisfiable_field_compare_eq",
    "_emit",
    "_ensure_oracle_assets",
    "_error_details",
    "_fail_context",
    "_fanout",
    "_fixed_value_candidates",
    "_front_bodies",
    "_inner_width",
    "_latest_payload",
    "_length_backfill_candidates",
    "_locate_payload",
    "_map_with_context",
    "_pending_rules",
    "_plan_stage",
    "_provider_name",
    "_rebind_failed_rules",
    "_required_backfill_candidates",
    "_resolve_budget",
    "_rules_stage",
    "_schema_stage",
    "_specs",
    "_summarize_deterministic_rule",
    "_upsert_rule",
    "_validate_plan",
    "bind_table_rules_worker",
    "join_table_rules_barrier",
    "prepare_deterministic_rule_index",
]
