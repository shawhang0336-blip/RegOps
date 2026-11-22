"""FastAPI 应用唯一装配入口（仅负责启动生命周期、中间件与路由挂载）。"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import runs as runs_v1
from app.api import system as system_v1
from app.api.v2 import artifacts as artifacts_v2
from app.api.v2 import code_sets as code_sets_v2
from app.api.v2 import documents as documents_v2
from app.api.v2 import generation as generation_v2
from app.api.v2 import metadata as metadata_v2
from app.api.v2 import settings as settings_v2
from app.engine.ingestion_run import recover_interrupted_ingestion_runs
from app.engine.workflow import recover_interrupted_generic_runs
from app.infra.config import get_settings
from app.infra.db import init_db
from app.infra.llm import close_http_client
from app.infra.task_runner import task_runner


def _mark_interrupted_runs() -> None:
    """进程重启后把仍在运行态的 Step 1 / Step 2 批次统一记为失败（基于 kernel.run）。"""
    recover_interrupted_generic_runs()
    recover_interrupted_ingestion_runs()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    _mark_interrupted_runs()
    try:
        yield
    finally:
        task_runner().shutdown(wait=False)
        close_http_client()


settings = get_settings()
app = FastAPI(title="监管规则装配与审计台", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in settings.cors_origins.split(",")],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 核心系统与运行批次路由（健康检查 / Blob 读取 / 业务库抽样 / 运行批次 / 回放 / 总线事件流）
app.include_router(system_v1.router)
app.include_router(runs_v1.router)

# v2 工作台资产与生成路由
app.include_router(settings_v2.router, prefix="/api/v2")
app.include_router(documents_v2.router, prefix="/api/v2")
app.include_router(metadata_v2.router, prefix="/api/v2")
app.include_router(code_sets_v2.router, prefix="/api/v2")
app.include_router(generation_v2.router, prefix="/api/v2")
app.include_router(artifacts_v2.router, prefix="/api/v2")
