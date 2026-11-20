"""HTTP 接口层（API）：统一收口 v1 与 v2 FastAPI 路由。

模块导航：
- `deps.py`：公共数据库依赖（`db_session`）、后台 Run 推进入口（`spawn_run_driver`）、请求体契约与视图序列化；
- `system.py`：`/api/health`、`/api/events/stream`（Bus 实时 SSE 推送）、`/api/blobs/*`、`/api/business/preview`；
- `runs.py`：`/api/runs/*`（批次创建/查询/清理、人工审批、测试确认、重试/重审、事件回放与 Bus 事件流）；
- `v2/`：`/api/v2/*` 工作台资产路由（documents / metadata / code_sets / generation / artifacts / settings）。
"""
