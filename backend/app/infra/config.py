"""运行配置、预算存储与身份占位常量（基础设施层 Layer 0）。"""

from functools import lru_cache
from pathlib import Path
from typing import Dict, Optional

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.orm import Session

ROOT_DIR = Path(__file__).resolve().parents[3]
DEFAULT_SPEC_PATH = (
    ROOT_DIR
    / "data"
    / "03_国家外汇局_SAFE"
    / "02_金融机构外汇业务数据采集规范"
    / "金融机构外汇业务数据采集规范_1.3版_全文.pdf"
)

# 统一登录/工号尚未接入前的操作人审计字段占位值；接入鉴权后从会话上下文替换
OPERATOR_PLACEHOLDER = "操作员"

DEFAULT_BUDGET: Dict[str, int] = {
    "max_rules_per_table": 50,
    "max_cases_per_table": 1000,
    "max_retries": 3,
    "max_llm_concurrency": 4,
    "max_llm_rpm": 0,
    "max_calls_per_table": 0,
    # ProdAgent BudgetGuard 成本熔断阈值（0 表示仅统计不熔断）
    "max_llm_calls": 120,
    "max_total_tokens": 1500000,
    "max_elapsed_seconds": 900,
}


class Settings(BaseSettings):
    app_name: str = "Data Rule Studio — 数据规则装配与验证台"
    database_url: str = "sqlite:///./var/data_rule_studio.db"
    business_database_url: Optional[str] = None
    oracle_sandbox_url: Optional[str] = None
    business_allowed_tables: str = ""
    blob_dir: str = str(ROOT_DIR / "backend" / "var" / "blobs")
    safe_13_spec_path: str = str(DEFAULT_SPEC_PATH)
    llm_provider: str = "scripted"
    llm_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    llm_model: str = "glm-4-plus"
    llm_api_key: Optional[str] = None
    # 是否启用 GLM thinking（推理增强）。默认 None = 不发送 thinking 参数。
    # 实测发现硬编码 {"type": "low"} 会导致部分请求 400（code 1210）且显著拉高延迟，
    # 因此默认关闭；确需启用时再设置（取值以所接入模型文档为准，如 "low"/"enabled"）。
    llm_thinking: Optional[str] = None
    # LLM 单次工具调用超时（秒）。实测单字段约 11~70s，个别复杂字段（含条件规则）可达 120s，
    # 默认 120s 覆盖波动；超过此值视为真实失败并报错（不做确定性降级）。
    llm_timeout: int = 120
    # tool_choice 模式：auto = 模型自决（deepseek thinking 模型只支持 auto）；
    # forced = 强制调用指定工具（GLM 支持，可提升小任务稳定性）。
    llm_tool_choice: str = "auto"
    # 结构切块交给 AI 的范围：adaptive = 只把"有判断空间"的章节交给 AI（多块、无结构、有告警）；
    # all = 每个块都让 AI 重新决策（成本高，适合首次校准切块规则时用）。
    structure_ai_scope: str = "adaptive"
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    model_config = SettingsConfigDict(env_file=str(ROOT_DIR / ".env"), extra="ignore")

    @property
    def allowed_business_tables(self) -> set:
        return {item.strip().upper() for item in self.business_allowed_tables.split(",") if item.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()


def get_budget(session: Session) -> Dict[str, int]:
    """读取确定性执行预算配置，缺失或为 None 的项以 DEFAULT_BUDGET 兜底。"""
    from app.infra.db_tables import SystemSettingModel

    row = session.get(SystemSettingModel, "execution_budget")
    stored = row.value_json if (row is not None and isinstance(row.value_json, dict)) else {}
    return {**DEFAULT_BUDGET, **{k: v for k, v in stored.items() if v is not None}}
