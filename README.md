# RegOps（`dq_agent`）— 监管报送规则装配与审计台

面向金融监管报送场景的**确定性工具 + 受控 AI 参数赋值**工作台：从监管规范文档解析、表元数据与码表核对、规则装配与人工确权、Oracle 沙箱造数与靶向变异验证，到生产校验 SQL 部署包一键交付。

架构核心遵循 **ProdAgent「机制在内，策略在外」**：六大核心微内核（`Plan` / `Bus` / `EventLog` / `Interrupt` / `Run` / `Scheduler` 真 BSP `Send` 波次引擎）与业务策略完全隔离，底层由 `infra/blob_store.py` 承接内容寻址大对象存储，全链路以只追加哈希链事件账本作为唯一事实源，支持门禁拦截、一等人工中断挂起、横切成本熔断、运行中取消与断点续跑。

## 目录结构

```text
dq_agent/
├── backend/                 # FastAPI 后端（ProdAgent 微内核 + 监管装配与审计引擎）
│   ├── ARCHITECTURE.md      # 后端八大分层包导览与架构红线说明
│   ├── app/                 # main.py + kernel / policy / infra / ingestion / assets / engine / delivery / api
│   ├── migrations/          # Alembic 数据库迁移脚本
│   └── tests/               # 单元测试与架构边界守卫测试
├── frontend/                # React + Vite 前端工作台
│   └── src/                 # 规范解析与核对 / 生成表结构与规则 / 测试与下载 / 变更记录 / 系统设置
├── docs/                    # 监管规范背景资料与索引文档
├── data/specs/              # 本地监管 PDF 原件目录（.gitignore 忽略，不入库）
├── .env.example             # 环境变量模板
└── README.md
```

- 后端架构详细说明见 [backend/ARCHITECTURE.md](backend/ARCHITECTURE.md)
- 外汇采集规范参考与来源索引见 [docs/SAFE_1.4_修订说明与来源索引.md](docs/SAFE_1.4_修订说明与来源索引.md)

## 本地启动

### 1. 环境配置

复制环境变量模板并按本地环境填写数据库与大模型配置：

```bash
cp .env.example .env
```

如需运行涉及外管 1.3 规范解析的完整测试集，将 `金融机构外汇业务数据采集规范_1.3版_全文.pdf` 放入 `data/specs/`（或通过 `SAFE_13_SPEC_PATH` 指定路径）。

### 2. 启动后端

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e "backend[dev]"

cd backend
alembic upgrade head
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

### 3. 启动前端

```bash
cd frontend
npm install
npm run dev
```

默认前端地址为 `http://127.0.0.1:5173`，代理请求到 `http://127.0.0.1:8000`。

## 测试验证

```bash
# 后端全量测试（包含 ProdAgent 内核隔离与根目录整洁守卫）
cd backend && pytest -q

# 前端生产构建检查
cd frontend && npm run build
```
