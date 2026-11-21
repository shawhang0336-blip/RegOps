"""运行时系统设置存储读取（如执行预算 execution_budget）。

位于基础设施层，供 api、engine 等上层共享，避免引擎层反向依赖 HTTP 路由层。
"""

from typing import Dict

from sqlalchemy.orm import Session

from app.infra.asset_models import SystemSettingModel

DEFAULT_BUDGET: Dict[str, int] = {
    "max_rules_per_table": 50,
    "max_cases_per_table": 1000,
    "max_retries": 3,
    "max_llm_concurrency": 4,
    # ProdAgent BudgetGuard 成本熔断阈值（0 表示仅统计不熔断）
    "max_llm_calls": 120,
    "max_total_tokens": 1500000,
    "max_elapsed_seconds": 900,
}


def get_budget(session: Session) -> Dict[str, int]:
    """读取确定性执行预算配置，缺失项以 DEFAULT_BUDGET 兜底。"""
    row = session.get(SystemSettingModel, "execution_budget")
    return {**DEFAULT_BUDGET, **(row.value_json if row else {})}
