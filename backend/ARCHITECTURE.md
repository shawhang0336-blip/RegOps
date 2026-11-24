# Data Rule Studio 后端架构导览（`backend/app/`）

后端严格遵循 **ProdAgent“机制在内，策略在外”** + **严格单向八层 DAG（3 层底座 + Step 0/1/2/3 四业务阶段 + 1 层接口）** 原则。  
`backend/app/` 根目录只保留唯一 FastAPI 启动入口 [`main.py`](app/main.py) 与 **8 个职责正交的子包**，所有跨层引用均使用显式绝对导入（`from app.<层>.<模块> import ...`），且依赖方向严格单向无环：

```text
infra (0) ← kernel (1) ← policy (2) ← ingestion (3) ← catalog (4) ← generation (5) ← delivery (6) ← api (7) ← main (8)
```

---

## 一、目录结构与分层说明

```text
backend/app/
├── main.py                      # 极简 FastAPI 启动入口（lifespan / CORS / 挂载路由）
│
├── infra/                       # 层 0：基础设施与跨层共享契约（7 模块 + llm/ 子包，零上层依赖）
│   ├── blob_store.py            #    SHA-256 内容寻址不可变大对象存储（原子临时文件替换，并发零竞态）
│   ├── config.py                #    环境配置 Settings、运行预算（DEFAULT_BUDGET / get_budget）与操作人占位常量
│   ├── contracts.py             #    跨层共享 Pydantic 数据契约（FieldSpec / SpecDocument / RuleDefinition / Binding / LLM_OPERATORS）
│   ├── db.py                    #    MySQL/Oracle 生产连接池、Base / SessionLocal / Alembic 初始化与 SQL 安全转义（quote_identifier）
│   ├── db_tables.py             #    全部 20 张数据库 ORM 表统一定义（运行时账本 / 文档与资产元数据 / 生成规划与产物）
│   ├── errors.py                #    统一领域异常 WorkflowError（携带结构化错误定位 context）
│   ├── task_runner.py           #    带任务键去重的进程内有界后台线程池（TaskRunner）
│   └── llm/                     #    受控大模型子包
│       ├── client.py            #      OpenAI 兼容 HTTP 客户端（httpx 连接池复用、并发信号量、502/504 非 JSON 容错）
│       ├── schemas.py           #      12 个受控工具 JSON Schema、spec_payload 与作用域 Prompt 契约
│       └── guard.py             #      跨三阶段受控 LLM 调用守卫（分轨退避重试、上下文作用域透传、鸭子类型 Bus 门禁回喂）
│
├── kernel/                      # 层 1：ProdAgent 六大核心微内核（仅依赖 infra，零业务依赖）
│   ├── plan.py                  #    静态只读工作流蓝图（PlanNode / PlanEdge / RetryPolicy / timeout / WorkflowPlan）
│   ├── bus.py                   #    全生命周期事件总线（check/adjudicate 门禁 + fire/subscribe 广播 + node.retry + 预算检查钩子）
│   ├── event_log.py             #    只追加哈希链事件账本（唯一事实源）+ 按 reducer 隔离的投影缓存 + 零成本 Trace/Span 瀑布折叠
│   ├── interrupt.py             #    一等人工中断协议（Interrupt / answer_interrupt / record_interrupt_answer）
│   ├── run.py                   #    Run 控制面（RunContext / ChannelSpec / AmbiguousWriteError / BudgetGuard / CancellationToken / 生命周期原语）
│   └── scheduler.py             #    真 BSP 波次调度引擎（Send 动态扇出 / Goto / Interrupt / 节点级 Retry+Timeout / 屏障冲突校验）
│
├── policy/                      # 层 2：RegOps 领域策略层（3 模块，严格一一对应 kernel 插槽，零 import 期全局副作用）
│   ├── regops_plan.py           #    Step 2 18 节点拓扑 + 人工中断门协议 + Step 1 / Step 2 统一事件状态折叠（显式 reducer）
│   ├── regops_bus.py            #    五大确定性事前门禁裁决器（字段/跨表字段/算子/批量码表/切块 ID 白名单）与 default_bus
│   └── regops_channels.py       #    24 个命名数据通道（含单表增量检查点）与 EventLog + BlobStore 单次查询断点恢复
│
├── ingestion/                   # 层 3（Step 0：原始规范文件拆箱、体检与切块，前端归入「01 规范解析与核对」上半段）
│   ├── parsers/                 #    纯离线文件拆箱、通用三级 PDF 切块与硬伤体检（零 DB、零 LLM）
│   │   ├── extractors.py        #      PDF / Word / Excel 统一抽块入口（消费 AI ① 切块蓝图）与全文摘要 survey
│   │   ├── pdf_catalog.py       #      外管局已知规范高精插件（目录与附录公共码表扫描）
│   │   ├── pdf_generic.py       #      通用监管 PDF 二级章节切块（HEADING_SECTIONS）与三级分页兜底（PAGE_WINDOWS）
│   │   ├── pdf_parser.py        #      PDF pdftotext 提取、表格折行重排与字段精拆
│   │   └── quality.py           #      切块后零 AI 确定性硬伤体检
│   ├── structure_service.py     #    AI 辅助文档体检、跨页合并/切块与单块内语义细分
│   └── import_progress.py       #    文档解析实时进度与 document.progress 总线广播
│
├── catalog/                     # 层 4（Step 1：监管数据目录，前端「01 规范解析与核对」下半段 +「变更记录」，100% 脱离 SQL 方言）
│   ├── tables/                  #    子域一：表与字段元数据
│   │   ├── domain.py            #      元数据草稿 Pydantic 契约与校验（零 DB/LLM）
│   │   ├── views.py             #      原文证据降级核查、工作台视图序列化与 metadata_to_spec
│   │   ├── mutations.py         #      字段编辑、表删除、子块表合并、版本发布与 CSV/XLSX 批量类型导入
│   │   ├── service.py           #      确定性优先 + 疑难字段/纯文本块 AI 辅助提取服务
│   │   └── readiness.py         #      表资产阶段 1→2 交付物就绪度检查
│   ├── code_sets/               #    子域二：公共与内联码表字典（严禁反向依赖 tables/）
│   │   ├── parser.py            #      纯函数码值文本解析（内联枚举 / 固定值 / 条件句 / 名称归一化，零 DB/LLM）
│   │   ├── linking.py           #      字段-码表自动对齐、序号分配、状态刷新与阻断检查
│   │   ├── service.py           #      码表抽取、AI 复核防幻觉护栏与摘要/删除服务
│   │   ├── readiness.py         #      码表就绪度评估与自动发布
│   │   └── bindings.py          #      数据库无关的已发布码表加载、值域绑定与规则/造数用量校验
│   ├── extraction.py            #    跨子域协同：表元数据 + 码表双轨并发抽取与自动对齐
│   ├── pipeline_run.py          #    Step 0 + Step 1 文档导入/解析统一 Run 控制面（基于 kernel.run + BudgetGuard）
│   └── version_diff.py          #    跨版本监管制度文档差异对比与影响面分级（返回增量受影响表清单）
│
├── generation/                  # 层 5（Step 2，对应前端 02「生成表结构、码表和规则」）：DDL、码表 SQL、规则装配与 Oracle 空跑
│   ├── core/                    #    ① 纯离线领域内核（AST 锁死：零 DB/LLM/Catalog/SQL/Workflow/Delivery 依赖）
│   │   ├── rules.py             #      13 类监管算子确定性校验、Oracle/POSIX 正则规范化与基础构造
│   │   ├── planning.py          #      确定性预算规划（零 LLM）与规则绑定语义分流闸门
│   │   ├── rule_backfill.py     #      前置四类确定性规则派生与回填（尊重人工 REJECTED）
│   │   └── binding.py           #      确定性 Schema 编译 + Rule / Generator / 黄金自洽门禁 / AuditCase 校验
│   ├── sql/                     #    ② 数据库方言编译与空跑资产准备（MySQL 8 / Oracle 23）
│   │   ├── compiler.py          #      规则 → Oracle / MySQL 8 原生校验 SQL 编译器（CHAR_LENGTH / REGEXP_LIKE 'c' / Decimal）
│   │   ├── code_set_sql.py      #      ≤64 字符码表命名与 Oracle MERGE/INSERT ALL + MySQL 8 DDL/UPSERT 编译
│   │   └── oracle.py            #      OracleSandbox 物理表管理 + OracleAssetPreparer（Step 2 建表+灌码值+规则空跑）
│   └── workflow/                #    ③ Step 2 前半段 BSP 规则生成流水线
│       ├── runtime.py           #      _call_validated、_map_with_context（透传取消令牌）、_fanout 与规则落库
│       ├── stage_plan_schema.py #      确定性生成规划与确定性 Schema 编译
│       ├── stage_rules.py       #      单表规则 BSP Worker（语义分流）、跨表外键汇聚、Oracle 空跑准备、去重与靶向自愈
│       ├── stages.py            #      Step 2 阶段聚合重导出门面
│       └── nodes_front.py       #      Step 2 前半段节点体（规划 → 表结构 → 规则扇出/汇聚 → Oracle 空跑 & 人工确权门）
│
├── delivery/                    # 层 6（Step 3，对应前端 03「测试与下载」）：造测试数据 → AB 规则沙箱测试 → 打包下载
│   ├── testing/                 #    ① Step 3.1 造数 & Step 3.2 AB 沙箱真跑验证
│   │   ├── mutations.py         #      确定性反例变异引擎（黄金正例 A 基线 + 11 类非正则算子反例 B + 正则兜底）
│   │   ├── stage_data.py        #      单表黄金正例造数 BSP Worker（独立 NUM_RANGE/UNIQUE_KEY 纯程序求解）与外键对齐
│   │   ├── stage_audit.py       #      单表变异反例 BSP Worker 与用例屏障汇聚
│   │   ├── sandbox_audit.py     #      GenericOracleExecutor（继承 OracleAssetPreparer，执行 AB 沙箱真跑与多黄金校验）
│   │   └── nodes_back.py        #      Step 3.1/3.2 后半段节点体（造数 → 变异 → AB 沙箱审计 + 三路自愈 → 测试确认门）
│   ├── workflow/                #    ② 全流程调度入口（单向拼装 Layer 5 Step 2 + Layer 6 Step 3 节点体）
│   │   └── entrypoints.py       #      RegOps Run 生命周期唯一业务门面（创建/确权/测试确认/三路重试/重审/取消/删除/回放）
│   ├── run_diff.py              #    跨验证批次规则与生产 SQL（含码表 NOT EXISTS）差异对比引擎
│   ├── production.py            #    Step 3.3 生产 DDL / 只读 SQL 编译与参数内联
│   ├── production_service.py    #    Step 3.3 生产部署 ZIP 包打包与完整性门禁
│   └── business_db.py           #    业务库只读抽样探查连接器
│
└── api/                         # 层 7：HTTP 接口层（按 4 个业务域分组 + 薄路由/服务分离，统一由 register_routers 挂载 /api/*）
    ├── __init__.py              #    唯一路由装配入口 register_routers(app)
    ├── common.py                #    跨域公共依赖（db_session）、spawn_run_driver 与通用 Run 视图序列化
    ├── platform/                #    域 1：平台底座与配置
    │   ├── system.py            #      /api/health、/api/events/stream（SSE）、/api/blobs/*、/api/business/preview
    │   └── settings.py          #      /api/settings/*（连接探活与执行预算校验/持久化）
    ├── catalog/                 #    域 2：监管资产目录（文档 / 表字段元数据 / 码表）
    │   ├── documents.py         #      /api/documents/* 薄路由（上传、切块、提取、进度、版本对比）
    │   ├── documents_service.py #      文档接入、原子落盘、初始切块与状态校验服务
    │   ├── metadata.py          #      /api/metadata/* 薄路由（报表字段、就绪度、类型修正与发布）
    │   ├── metadata_service.py  #      表字段统计聚合、CSV/XLSX 导入、强外键删除、草稿克隆与批量发布服务
    │   ├── code_sets.py         #      /api/code-sets/* 薄路由（码表、码值、引用对齐与导入）
    │   └── code_sets_service.py #      码表明细、直传文档抽取、引用对齐与大小写去重导入服务
    ├── generation/              #    域 3：规则与产物装配
    │   ├── generation.py        #      /api/generation/* 薄路由（发起生成、规划与生产部署 ZIP 打包）
    │   ├── generation_service.py#      生成前置阻断检查、快照装配与自动发布服务
    │   ├── artifacts.py         #      /api/artifacts/* 薄路由（产物状态、规则总览与手工改参）
    │   └── artifacts_service.py #      产物批量聚合（防 N+1）、规则改参校验与 EventLog 留痕服务
    └── delivery/                #    域 4：运行批次控制与审计交付
        ├── runs.py              #      /api/runs/* 薄路由（生命周期、审批、测试确认、重试、回放、trace、bus-events）
        └── runs_service.py      #      批次列表批量折叠、多轮 Oracle 沙箱表清理与校验明细拼装服务
```

---

## 二、核心架构红线（自动化测试守卫）

[`tests/test_kernel_policy_isolation.py`](tests/test_kernel_policy_isolation.py) 在每次回归中强制守卫以下六条红线：

1. **内核纯净度与六模块边界**：`app/kernel/` 严格收敛为六大核心模块（`plan` / `bus` / `event_log` / `interrupt` / `run` / `scheduler`），严禁 `import` 任何业务层或策略层（`policy` / `ingestion` / `catalog` / `engine` / `delivery` / `api`）；
2. **策略层三模块与零全局污染**：`app/policy/` 严格收敛为三大插槽模块（`regops_plan` / `regops_bus` / `regops_channels`），严禁在 import 期污染 `global_bus()` 或隐式绑定全局默认 reducer；
3. **四业务阶段纯离线边界**：`app/infra/` 收敛为 7 个核心底座模块 + `llm/` 子包；`app/ingestion/parsers/`、`app/catalog/tables/domain.py`、`app/catalog/code_sets/parser.py`、`app/generation/core/` 均零 DB/LLM 依赖（且 `catalog/code_sets/` 严禁反调 `tables/`，`generation/core/` 严禁反调 `generation/sql/` 或 `generation/workflow/`）；
4. **API 域隔离与零跨路由依赖**：`app/api/` 严格收敛为 `common.py` + 四大业务域子包（`platform/` / `catalog/` / `generation/` / `delivery/`），路由文件严禁彼此 `import`，服务文件严禁反调路由文件；
5. **内核独立可运行**：仅依赖 `app.kernel` 即可编排、门禁拦截、归约 Channel 并驱动非监管工作流；
6. **根目录整洁与全层严格单向八层 DAG**：`backend/app/` 根目录只允许 `main.py` 与 8 个架构包，AST 静态扫描确保所有跨层引用严格从高层指向低层（`infra < kernel < policy < ingestion < catalog < generation < delivery < api < main`），零倒挂、零循环依赖。

---

## 三、看代码推荐阅读顺序

1. **看启动与路由**：[`app/main.py`](app/main.py) → [`app/api/__init__.py`](app/api/__init__.py) → [`app/api/delivery/runs.py`](app/api/delivery/runs.py) → [`app/api/generation/generation.py`](app/api/generation/generation.py)
2. **看微内核机制**：[`app/kernel/plan.py`](app/kernel/plan.py) → [`app/kernel/bus.py`](app/kernel/bus.py) → [`app/kernel/event_log.py`](app/kernel/event_log.py) → [`app/kernel/interrupt.py`](app/kernel/interrupt.py) → [`app/kernel/run.py`](app/kernel/run.py) → [`app/kernel/scheduler.py`](app/kernel/scheduler.py)
3. **看 RegOps 策略装配**：[`app/policy/regops_plan.py`](app/policy/regops_plan.py) → [`app/policy/regops_bus.py`](app/policy/regops_bus.py) → [`app/policy/regops_channels.py`](app/policy/regops_channels.py)
4. **看阶段二/三流水线执行**：[`app/delivery/workflow/entrypoints.py`](app/delivery/workflow/entrypoints.py) → [`app/generation/workflow/nodes_front.py`](app/generation/workflow/nodes_front.py) → [`app/delivery/testing/nodes_back.py`](app/delivery/testing/nodes_back.py) → [`app/generation/workflow/runtime.py`](app/generation/workflow/runtime.py)
