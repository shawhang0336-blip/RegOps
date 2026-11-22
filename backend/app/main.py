"""FastAPI 应用唯一装配入口（仅负责启动生命周期、中间件与路由挂载）。"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import register_routers
from app.catalog.pipeline_run import recover_interrupted_ingestion_runs
from app.delivery.workflow import recover_interrupted_generic_runs
from app.infra.config import get_settings
from app.infra.db import init_db
from app.infra.llm import close_http_client
from app.infra.task_runner import task_runner


def _mark_interrupted_runs() -> None:
    """进程重启后把仍在运行态的文档解析 / 规则生成批次统一记为失败（基于 kernel.run）。"""
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
    allow_origins=[origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

register_routers(app)
