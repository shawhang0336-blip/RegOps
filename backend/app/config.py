from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_SPEC_PATH = (
    ROOT_DIR
    / "外汇业务数据采集规范_1.4版_公开资料"
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
    llm_model: str = "GLM-5.3-Flash"
    llm_api_key: Optional[str] = None
    # 是否启用 GLM thinking（推理增强）。默认 None = 不发送 thinking 参数。
    # 实测发现硬编码 {"type": "low"} 会导致部分请求 400（code 1210）且显著拉高延迟，
    # 因此默认关闭；确需启用时再设置（取值以所接入模型文档为准，如 "low"/"enabled"）。
    llm_thinking: Optional[str] = None
    # LLM 单次工具调用超时（秒）。单字段实测约 40s，默认 60s 留出余量。
    llm_timeout: int = 60
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    model_config = SettingsConfigDict(env_file=str(ROOT_DIR / ".env"), extra="ignore")

    @property
    def allowed_business_tables(self) -> set:
        return {item.strip().upper() for item in self.business_allowed_tables.split(",") if item.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
