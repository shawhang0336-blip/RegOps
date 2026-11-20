"""Bus 兼容门面：
- 总线微内核（Bus / check / adjudicate / fire / subscribe）位于 `app.kernel.bus`；
- RegOps 五大确定性业务门禁裁决器与 `default_bus` 位于 `app.policy.regops_bus`。
"""

from .kernel.bus import (
    LIFECYCLE_TOPICS,
    MAX_BUS_HISTORY,
    Adjudicator,
    Bus,
    Subscriber,
    global_bus,
)
from .policy.regops_bus import (
    REGOPS_ADJUDICATORS,
    code_set_values_adjudicator,
    cross_table_adjudicator,
    default_bus,
    field_whitelist_adjudicator,
    operator_whitelist_adjudicator,
    structure_block_adjudicator,
)

__all__ = [
    "Adjudicator",
    "Bus",
    "LIFECYCLE_TOPICS",
    "MAX_BUS_HISTORY",
    "REGOPS_ADJUDICATORS",
    "Subscriber",
    "code_set_values_adjudicator",
    "cross_table_adjudicator",
    "default_bus",
    "field_whitelist_adjudicator",
    "global_bus",
    "operator_whitelist_adjudicator",
    "structure_block_adjudicator",
]
