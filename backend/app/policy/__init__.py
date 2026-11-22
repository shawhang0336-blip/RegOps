"""RegOps 监管数据质量领域策略层（策略在外，Layer 2）。

本包将监管报送业务策略精准装配到 ProdAgent 微内核（`app.kernel`）三大插槽之上，
严格仅依赖 `app.infra`（Layer 0）与 `app.kernel`（Layer 1），零 import 期全局副作用：
1. `regops_plan.py`：18 节点装配流水线拓扑（`DEFAULT_PLAN`）、人工门中断协议与 Step 1 / Step 2 统一事件状态折叠；
2. `regops_bus.py`：五大确定性事前门禁裁决器（字段/跨表/算子/码表值域/切块 ID 白名单）与 `default_bus`；
3. `regops_channels.py`：24 个命名数据通道（`DEFAULT_CHANNELS`）与 EventLog + BlobStore 单次查询断点恢复（`recover_channels` / `build_context`）。
"""

from .regops_bus import (
    REGOPS_ADJUDICATORS,
    code_set_values_adjudicator,
    cross_table_adjudicator,
    default_bus,
    field_whitelist_adjudicator,
    operator_whitelist_adjudicator,
    structure_block_adjudicator,
)
from .regops_channels import DEFAULT_CHANNELS, build_context, recover_channels, resolve_event_tables
from .regops_plan import (
    ACTIVE_INGESTION_STATUSES,
    ACTIVE_RUN_STATUSES,
    DEFAULT_EDGES,
    DEFAULT_NODES,
    DEFAULT_PLAN,
    INTERRUPT_ANSWER_KINDS,
    INTERRUPT_RESUME_NODES,
    approval_target,
    audit_passed_target,
    decided_target,
    fold,
    fold_ingestion_event,
    fold_regops_event,
    interrupt_payload,
    is_gate,
    pending_interrupt,
    project,
    project_ingestion,
    project_ingestion_many,
    project_many,
    self_heal_cases_target,
    self_heal_data_target,
    self_heal_target,
)

__all__ = [
    "ACTIVE_INGESTION_STATUSES",
    "ACTIVE_RUN_STATUSES",
    "DEFAULT_CHANNELS",
    "DEFAULT_EDGES",
    "DEFAULT_NODES",
    "DEFAULT_PLAN",
    "INTERRUPT_ANSWER_KINDS",
    "INTERRUPT_RESUME_NODES",
    "REGOPS_ADJUDICATORS",
    "approval_target",
    "audit_passed_target",
    "build_context",
    "code_set_values_adjudicator",
    "cross_table_adjudicator",
    "decided_target",
    "default_bus",
    "field_whitelist_adjudicator",
    "fold",
    "fold_ingestion_event",
    "fold_regops_event",
    "interrupt_payload",
    "is_gate",
    "operator_whitelist_adjudicator",
    "pending_interrupt",
    "project",
    "project_ingestion",
    "project_ingestion_many",
    "project_many",
    "recover_channels",
    "resolve_event_tables",
    "self_heal_cases_target",
    "self_heal_data_target",
    "self_heal_target",
    "structure_block_adjudicator",
]
