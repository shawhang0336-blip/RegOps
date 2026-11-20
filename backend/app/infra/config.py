from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parents[3]
DEFAULT_SPEC_PATH = (
    ROOT_DIR
    / "data"
    / "specs"
    / "金融机构外汇业务数据采集规范_1.3版_全文.pdf"
)


class Settings(BaseSettings):
    app_name: str = "监管规则装配与审计台"
    database_url: str = "sqlite:///./var/dq_agent.db"
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
