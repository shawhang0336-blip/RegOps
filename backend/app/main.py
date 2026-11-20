"""FastAPI 应用唯一装配入口（仅负责启动生命周期、中间件与路由挂载）。"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import runs as runs_v1
from app.api import system as system_v1
from app.api.deps import ACTIVE_RUN_STATUSES
from app.api.v2 import artifacts as artifacts_v2
from app.api.v2 import code_sets as code_sets_v2
from app.api.v2 import documents as documents_v2
from app.api.v2 import generation as generation_v2
from app.api.v2 import metadata as metadata_v2
from app.api.v2 import settings as settings_v2
from app.infra.config import get_settings
from app.infra.db import SessionLocal, init_db
from app.infra.models import RunModel
from app.kernel.event_log import append_event
from app.policy.regops_plan import project_many


def _mark_interrupted_runs() -> None:
    """进程重启后把仍在运行态的批次记为失败，等待人工触发断点续跑。"""
    with SessionLocal() as session:
        runs = session.query(RunModel).all()
        states = project_many(session, runs)
        interrupted = [run for run in runs if states[run.id].status in ACTIVE_RUN_STATUSES]
        for run in interrupted:
            append_event(
                session,
                run,
                "RUN_FAILED",
                "startup",
                {"node": "startup", "result": {"error": "Run interrupted by process restart"}},
            )
        if interrupted:
            session.commit()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    _mark_interrupted_runs()
    yield


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
