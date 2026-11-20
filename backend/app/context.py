"""RunContext & Channel 兼容门面：
- 数据通道微内核（ChannelSpec / Reducer / last_value / merge_dict / append_list / Identity / RunContext）位于 `app.kernel.channel`；
- RegOps 16 个业务通道定义（DEFAULT_CHANNELS）、断点折叠恢复（recover_channels）与上下文组装（build_context）位于 `app.policy.regops_channels`。
"""

from .kernel.channel import (
    ChannelSpec,
    Identity,
    Reducer,
    RunContext,
    append_list,
    last_value,
    merge_dict,
    resolve_event_tables,
)
from .policy.regops_channels import DEFAULT_CHANNELS, build_context, recover_channels

__all__ = [
    "ChannelSpec",
    "DEFAULT_CHANNELS",
    "Identity",
    "Reducer",
    "RunContext",
    "append_list",
    "build_context",
    "last_value",
    "merge_dict",
    "recover_channels",
    "resolve_event_tables",
]
