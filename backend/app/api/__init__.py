"""HTTP 接口层（Layer 7）：按业务域划分为 4 个正交子包，统一通过 `register_routers(app)` 挂载全量 `/api/*` 路由。

目录结构：
- `common.py`：跨域公共依赖（`db_session`）、后台 Run 推进入口（`spawn_run_driver`）与通用 Run 视图序列化；
- `platform/`：底座与配置域（`system.py` 健康/SSE/Blob/业务库预览、`settings.py` 连接探活与执行预算）；
- `catalog/`：监管资产目录域（`documents.py` / `metadata.py` / `code_sets.py` 及其配套 `*_service.py`）；
- `generation/`：规则与产物装配域（`generation.py` / `artifacts.py` 及其配套 `*_service.py`）；
- `delivery/`：批次运行与审计交付域（`runs.py` 及 `runs_service.py`）。

分层约束：每个业务域内路由文件（`*.py`）仅处理 HTTP 契约、参数校验与状态码映射，
复杂查询与编排下沉到同域 `*_service.py`；严禁路由文件之间相互 import。
"""

from fastapi import FastAPI

from app.api.catalog import code_sets, documents, metadata
from app.api.delivery import runs
from app.api.generation import artifacts, generation
from app.api.platform import settings as settings_api, system


def register_routers(app: FastAPI) -> None:
    """统一装配全部 `/api/*` 路由（顺序决定静态路径优先级，严禁随意打乱）。"""
    # 1. 平台底座与系统设置
    app.include_router(system.router)
    app.include_router(settings_api.router, prefix="/api")
    # 2. 监管资产目录（文档 / 表字段元数据 / 码表）
    app.include_router(documents.router, prefix="/api")
    app.include_router(metadata.router, prefix="/api")
    app.include_router(code_sets.router, prefix="/api")
    # 3. 规则与产物装配
    app.include_router(generation.router, prefix="/api")
    app.include_router(artifacts.router, prefix="/api")
    # 4. 运行批次控制与审计交付
    app.include_router(runs.router)


__all__ = ["register_routers"]
