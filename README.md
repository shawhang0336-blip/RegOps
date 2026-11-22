# RegOps（`dq_agent`）— 监管报送规则装配与审计台

面向金融监管报送场景的**确定性工具 + 受控 AI 参数赋值**工作台：覆盖监管规范文档解析、表字段元数据与码表核对、Oracle/MySQL 建表与校验规则装配、人工确权、Oracle 沙箱黄金正例造数与靶向反例变异验证，到生产校验 SQL 部署 ZIP 包一键交付。

---

## 核心特性与架构原则

- **三步端到端工作台**
  1. **规范解析与核对**：支持 PDF / DOCX / XLSX 规范文档接入、AI 结构体检与切块、表字段与公共/内联码表双轨并行提取、人工抽屉修订、CSV/XLSX 批量修正与就绪度检查。
  2. **生成表结构、码表和规则**：确定性预算规划与 DDL 编译、13 类监管算子规则生成、Oracle 沙箱空跑预检、规则手工改参与人工审批确权。
  3. **测试与下载**：AI 黄金正例造数 + 确定性反例变异、Oracle AB 沙箱真跑与三路自愈、测试确认门禁、生产 DDL / 只读校验 SQL 部署 ZIP 包防篡改打包下载，并支持跨版本制度文档 Diff 与跨批次 SQL Diff。
- **后端架构（ProdAgent「机制在内，策略在外」+ 严格单向八层 DAG）**
  - 六大微内核（`Plan` / `Bus` / `EventLog` / `Interrupt` / `Run` / `Scheduler` 真 BSP `Send` 波次引擎）与业务策略彻底隔离；
  - 全链路以只追加哈希链事件账本（`EventLog`）与内容寻址大对象存储（`BlobStore`）为唯一事实源，支持门禁拦截、一等人工中断挂起、横切成本熔断（`BudgetGuard`）、运行中取消、断点续跑与 SSE 实时事件推送；
  - 统一收口全部 `/api/*` 接口（无历史 `/api/v2` 分叉），`api/` 按四大业务域采用**薄路由 + 服务分离**，严禁跨路由互调。
- **前端架构（与后端四大业务域 1:1 对称）**
  - React 19 + Vite 7，路由级 `React.lazy` 懒加载分包；
  - 全站 `/api/*` 请求集中收口在 `frontend/src/api/`，业务模块按 `platform / catalog / generation / delivery` 与后端一一对应，公共组件与纯工具严格单向依赖。

详细架构说明与阅读路线：
- 后端架构导览：[backend/ARCHITECTURE.md](backend/ARCHITECTURE.md)
- 前端架构导览：[frontend/ARCHITECTURE.md](frontend/ARCHITECTURE.md)
- 监管规范背景资料：[docs/SAFE_1.4_修订说明与来源索引.md](docs/SAFE_1.4_修订说明与来源索引.md)

---

## 目录结构

```text
dq_agent/
├── backend/                          # FastAPI 后端（严格单向八层 DAG）
│   ├── ARCHITECTURE.md               # 后端八层架构导览与 AST 红线说明
│   ├── app/
│   │   ├── main.py                   # 极简 FastAPI 启动入口（lifespan / CORS / register_routers）
│   │   ├── infra/                    # 层 0：基础设施、20 张 ORM 表、BlobStore、受控 LLM 客户端
│   │   ├── kernel/                   # 层 1：ProdAgent 六大核心微内核（零业务依赖）
│   │   ├── policy/                   # 层 2：RegOps 策略装配（Plan / Bus 门禁 / Channels）
│   │   ├── ingestion/                # 层 3：文档拆箱、纯离线解析器、AI 结构切块与进度广播
│   │   ├── catalog/                  # 层 4：监管资产目录（tables / code_sets / 并发提取 / 版本 Diff）
│   │   ├── generation/               # 层 5：纯离线规则内核（core）、MySQL/Oracle SQL 编译与前段 BSP
│   │   ├── delivery/                 # 层 6：造数变异与 AB 沙箱审计（testing）、总调度、Run Diff 与生产打包
│   │   └── api/                      # 层 7：统一 /api/* 接口层（common.py + 4 大业务域薄路由与服务）
│   │       ├── __init__.py           #   唯一路由装配入口 register_routers(app)
│   │       ├── common.py             #   跨域公共依赖（db_session / spawn_run_driver / run_view）
│   │       ├── platform/             #   /api/health、/api/events/stream、/api/blobs/*、/api/settings/*
│   │       ├── catalog/              #   /api/documents/*、/api/metadata/*、/api/code-sets/*
│   │       ├── generation/           #   /api/generation/*、/api/artifacts/*
│   │       └── delivery/             #   /api/runs/*
│   ├── migrations/                   # Alembic 数据库迁移脚本
│   └── tests/                        # 后端全量单测、深审回归与 AST 架构红线守卫
├── frontend/                         # React + Vite 前端工作台（与后端四域 1:1 对称）
│   ├── ARCHITECTURE.md               # 前端目录分层与依赖守卫说明
│   ├── src/
│   │   ├── main.jsx                  # 唯一挂载入口
│   │   ├── app/                      # 应用外壳、顶栏导航、路由表与健康探测
│   │   ├── api/                      # 全站统一接口客户端（client / platform / catalog / generation / delivery）
│   │   ├── components/               # 通用 UI 组件（PageHeader / StatusMessage / StatusBadge / Modal 等）
│   │   ├── context/                  # ThemeContext / FeedbackContext
│   │   ├── hooks/                    # Bus SSE 订阅与进度监听（useEventStream）
│   │   ├── utils/                    # 纯工具函数与文案词典（dateTime / budget / activeDocument / labels）
│   │   ├── styles/                   # 全局样式收口（index.css 固定层叠顺序）
│   │   └── features/                 # 按 4 大业务域组织的页面模块（catalog / generation / delivery / platform）
│   └── tests/                        # 前端单测与架构边界自动化守卫（node --test）
├── docs/                             # 监管规范背景资料与修订索引
├── data/specs/                       # 本地监管 PDF 原件目录（.gitignore 忽略，不入库）
├── .env.example                      # 环境变量模板
└── README.md
```

### 前后端四大业务域对照

| 业务域 | 前端页面（`frontend/src/features/`） | 前端接口（`frontend/src/api/`） | 后端接口（`backend/app/api/`） | 职责范围 |
| --- | --- | --- | --- | --- |
| `catalog` | `catalog/`（工作台、文档、元数据、码表、变更记录） | `catalog.js` | `catalog/`（`documents` / `metadata` / `code_sets`） | 规范接入、切块提取、表字段/码表核对与文档 Diff |
| `generation` | `generation/`（规则工作台与规则编辑抽屉） | `generation.js` | `generation/`（`generation` / `artifacts`） | 规则/表结构/码表产物聚合、规则改参、发起生成与打包下载 |
| `delivery` | `delivery/`（测试与下载、追踪瀑布、历史清理） | `delivery.js` | `delivery/`（`runs`） | 运行批次控制、规则审批、测试确认、沙箱预览与批次 Diff |
| `platform` | `platform/`（系统设置） | `platform.js` | `platform/`（`system` / `settings`） | 健康探测、SSE 事件流、Blob 读取、连接探活与执行预算 |

---

## 本地启动

### 1. 环境配置

复制环境变量模板并按本地环境填写数据库与模型配置：

```bash
cp .env.example .env
```

常用配置项（详见 [`.env.example`](.env.example)）：
- `DATABASE_URL`：应用管理库连接串（本地默认 SQLite，生产/联调推荐 MySQL 8+ `mysql+pymysql://...`）
- `ORACLE_SANDBOX_URL`：Oracle 验证沙箱连接串（用于真跑建表、造数与规则验证）
- `LLM_PROVIDER` / `LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY`：OpenAI 兼容大模型配置（`scripted` 为离线脚本模式）
- `SAFE_13_SPEC_PATH`：可选，如需运行外管 1.3 规范全量解析测试，可将 `金融机构外汇业务数据采集规范_1.3版_全文.pdf` 放入 `data/specs/`

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

默认前端地址为 `http://127.0.0.1:5173`，Vite 会将 `/api/*` 请求代理到 `http://127.0.0.1:8000`。

---

## 测试与架构守卫

```bash
# 后端全量测试（含八层 DAG、内核纯净度、API 零跨路由依赖等 AST 架构守卫）
cd backend && pytest -q

# 前端单测与架构边界守卫（含目录结构、/api/* 统一收口与单向依赖校验）
cd frontend && npm test

# 前端生产构建验证
cd frontend && npm run build
```
