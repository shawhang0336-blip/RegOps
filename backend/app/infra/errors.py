from typing import Any, Dict, Optional


class WorkflowError(RuntimeError):
    """工作流错误。可携带结构化 context，用于失败定位与前端展示。

    context 约定字段：stage / tool_name / error_kind / table_code / field / retried。
    """

    def __init__(self, message: str, *, context: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.context = context or {}
