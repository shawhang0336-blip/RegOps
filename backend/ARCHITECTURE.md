# RegOps 后端架构导览（`backend/app/`）

后端严格遵循 **ProdAgent“机制在内，策略在外”** + **严格单向八层 DAG** 原则。  
`backend/app/` 根目录只保留唯一 FastAPI 启动入口 [`main.py`](app/main.py) 与 **8 个职责正交的子包**，所有跨层引用均使用显式绝对导入（`from app.<层>.<模块> import ...`），且依赖方向严格单向无环：

```text
infra (0) ← kernel (1) ← policy (2) ← ingestion (3) ← assets (4) ← engine (5) ← delivery (6) ← api (7) ← main (8)
```

---

## 一、目录结构与分层说明

```text
backend/app/
├── main.py                      # 极简 FastAPI 启动入口（lifespan / CORS / 挂载路由）
│
├── infra/                       # 层 0：基础设施与跨层共享契约（零上层依赖）
│   ├── config.py                #    环境配置 Settings（MySQL / Oracle / LLM / Blob / 规范路径）
│   ├── db.py                    #    SQLAlchemy Engine / SessionLocal / Alembic 初始化
│   ├── errors.py                #    统一领域异常 WorkflowError
│   ├── identity.py              #    操作人身份占位符
│   ├── domain.py                #    跨层共享值对象（FieldSpec / SpecDocument / RuleDefinition / Binding / field_map）
│   ├── sql.py                   #    跨方言 SQL 安全标识符转义（quote_identifier）
│   ├── llm.py                   #    受控 Tool-Call 大模型客户端与并发限流
│   ├── settings_store.py        #    运行时执行预算配置读取（DEFAULT_BUDGET / get_budget）
│   ├── task_runner.py           #    带任务键去重的进程内有界后台任务执行器（TaskRunner）
│   ├── models.py                #    核心运行时表（runs / events / rules / specifications）
│   └── asset_models.py          #    资产表（文档 / 原始块 / 元数据 / 码表 / 审批 / 产物 / 系统设置）
│
├── kernel/                      # 层 1：ProdAgent 微内核（仅依赖 infra，零业务依赖）
│   ├── event_log.py             #    只追加哈希链事件账本（唯一事实源）+ 状态折叠引擎
│   ├── blob_store.py            #    SHA-256 内容寻址不可变大对象存储
│   ├── bus.py                   #    全生命周期事件总线（check/adjudicate 门禁 + fire/subscribe 广播 + 预算检查钩子）
│   ├── channel.py               #    带 Reducer（last_value / merge_dict / append_list）的共享数据通道与 RunContext
│   ├── plan.py                  #    静态只读工作流蓝图（PlanNode / PlanEdge / WorkflowPlan）
│   ├── interrupt.py             #    一等人工中断协议（Interrupt / answer_interrupt / record_interrupt_answer）
│   ├── budget.py                #    挂在 Bus 上的横切成本观测与熔断守卫（BudgetGuard / BudgetLimits / BudgetExceededError）
│   ├── run.py                   #    统一 Run 生命周期控制面（创世/提交/活跃注册表/取消/恢复/钩子删除/回放）
│   └── scheduler.py             #    真 BSP 波次调度引擎（Send 动态扇出 / Goto / Interrupt / execute_wave 屏障归并）
│
├── policy/                      # 层 2：RegOps 领域策略层（仅依赖 infra + kernel，以插件方式装配到 kernel）
│   ├── regops_plan.py           #    18 节点真 BSP 装配流水线拓扑（含单表 Send Worker/Join 与三路自愈回边）与状态折叠
│   ├── ingestion_plan.py        #    Step 1 文档导入与解析 Run 的事件折叠策略（重启可从 EventLog 无损还原）
│   ├── regops_bus.py            #    五大确定性事前门禁裁决器（字段/跨表字段/算子/批量码表/切块 ID 白名单）
│   ├── regops_channels.py       #    24 个命名数据通道（含单表增量检查点）与 EventLog + BlobStore 断点折叠恢复
│   └── llm_guard.py             #    跨三阶段统一受控模型调用守卫（退避重试 + Bus 门禁 + 作用域 Budget/Cancel 透传）
│
├── ingestion/                   # 层 3：监管文档接入与结构解析（阶段一）
│   ├── extractors.py            #    PDF / Word / Excel 原始块提取
│   ├── catalog.py               #    文档目录与章节识别
│   ├── spec_parser.py           #    数据字典表结构与字段解析
│   ├── spec_quality.py          #    原始块解析质量体检
│   ├── structure_service.py     #    AI 辅助文档体系切块
│   ├── segment_service.py       #    父子层级段落切分
│   └── import_progress.py       #    文档解析实时进度与 document.progress 总线广播
│
├── assets/                      # 层 4：元数据资产与码表中心
│   ├── metadata_domain.py       #    元数据草稿 Pydantic 契约
│   ├── metadata_views.py        #    原文证据降级核查、工作台视图序列化与 metadata_to_spec
│   ├── metadata_mutations.py    #    字段编辑、表删除、版本发布与 CSV/XLSX 批量类型导入
│   ├── metadata_service.py      #    确定性优先 + 疑难字段 AI 辅助提取服务
│   ├── readiness.py             #    表资产就绪度检查
│   ├── version_diff.py          #    跨版本监管制度文档差异对比与影响面分级（返回增量受影响表清单）
│   └── code_sets/               #    码表子域
│       ├── parser.py            #      纯函数码值文本解析（内联枚举 / 固定值 / 条件句 / 名称归一化）
│       ├── linking.py           #      字段-码表自动对齐、序号分配、状态刷新与阻断检查
│       ├── service.py           #      码表抽取、批量并发、AI 复核与摘要/删除服务
│       ├── readiness.py         #      码表就绪度评估与自动发布
│       └── oracle.py            #      已发布码表加载与 Oracle 沙箱码表 DDL 编译
│
├── engine/                      # 层 5：规则装配、造数、文档 Run 控制面与 Oracle 沙箱执行
│   ├── ingestion_run.py         #    Step 1 文档导入/解析统一 Run 控制面（基于 kernel.run + BudgetGuard + 取消/重启回收）
│   ├── run_diff.py              #    跨验证批次规则与编译后 Oracle SQL 差异对比引擎
│   ├── rules.py                 #    13 类监管算子确定性校验（含 ARITHMETIC_BALANCE / COND_VALUE / UNIQUE_KEY）
│   ├── compiler.py              #    规则 → Oracle / MySQL 原生校验 SQL 编译器
│   ├── binding.py               #    确定性 Schema 编译 + Rule / Generator / 黄金自洽门禁 / AuditCase 校验
│   ├── mutations.py             #    确定性审计变异引擎（黄金基线 + 12 类非正则算子反例构造 + 正则兜底）
│   ├── rule_backfill.py         #    前置四类确定性规则派生（必填 / 列宽上限 / 固定值 / 已发布码表 ENUM）
│   ├── oracle.py                #    OracleSandbox 物理表管理 + GenericOracleExecutor 真跑审计
│   └── workflow/                #    阶段二编排流水线
│       ├── runtime.py           #      _call_validated（委托 llm_guard）与 _fanout（委托 kernel.scheduler.execute_wave）
│       ├── stage_plan_schema.py #      预算规划与确定性 Schema 编译
│       ├── stage_rules.py       #      单表规则 BSP Worker、跨表外键汇聚、Oracle 资产预建、去重与靶向自愈
│       ├── stage_data.py        #      单表黄金造数 BSP Worker、跨表外键对齐与确定性规则回填
│       ├── stage_audit.py       #      单表审计变异 BSP Worker 与用例屏障汇聚
│       ├── stages.py            #      向后兼容聚合重导出门面
│       ├── nodes.py             #      13 个 ProdAgent NodeBody（真 BSP Send 扇出/Join 增量续跑 + 三路确定性归因自愈）
│       └── entrypoints.py       #      RegOps Run 生命周期唯一业务门面（基于 kernel.run 装配全量操作）
│
├── delivery/                    # 层 6：生产交付与业务库只读校验（阶段三）
│   ├── production.py            #    生产校验包结构定义与只读 SQL 组装
│   ├── production_service.py    #    面向已确认 Run 的生产包打包服务
│   └── business_db.py           #    业务库只读抽样探查连接器
│
└── api/                         # 层 7：HTTP 接口层（v1 与 v2 统一收口）
    ├── deps.py                  #    公共 Session 依赖（db_session）、spawn_run_driver 与视图序列化
    ├── system.py                #    /api/health / /api/events/stream（SSE） / /api/blobs/* / /api/business/preview
    ├── runs.py                  #    /api/runs/*（生命周期 / 清理 / 审批 / 确认 / 重试 / 回放 / bus-events）
    └── v2/                      #    /api/v2/* 工作台资产与生成路由
        ├── documents.py
        ├── metadata.py
        ├── code_sets.py
        ├── generation.py
        ├── artifacts.py
        └── settings.py
```

---

## 二、核心架构红线（自动化测试守卫）

[`tests/test_kernel_policy_isolation.py`](tests/test_kernel_policy_isolation.py) 在每次回归中强制守卫以下四条红线：

1. **内核纯净度**：`app/kernel/*.py` 严禁 `import` 任何业务层或策略层（`policy` / `ingestion` / `assets` / `engine` / `delivery` / `api`）；
2. **内核独立可运行**：仅依赖 `app.kernel` 即可编排、门禁拦截、归约 Channel 并驱动非监管工作流；
3. **根目录不退化**：`backend/app/` 根目录只允许 `main.py` 与上述 8 个架构包，禁止重新平铺散落业务文件；
4. **全层严格单向 DAG**：AST 静态扫描 `backend/app/**/*.py`，确保所有跨层引用严格从高层指向低层（`infra < kernel < policy < ingestion < assets < engine < delivery < api < main`），零倒挂、零循环依赖。

---

## 三、看代码推荐阅读顺序

1. **看启动与路由**：[`app/main.py`](app/main.py) → [`app/api/runs.py`](app/api/runs.py) → [`app/api/v2/generation.py`](app/api/v2/generation.py)
2. **看微内核机制**：[`app/kernel/plan.py`](app/kernel/plan.py) → [`app/kernel/event_log.py`](app/kernel/event_log.py) → [`app/kernel/bus.py`](app/kernel/bus.py) → [`app/kernel/channel.py`](app/kernel/channel.py) → [`app/kernel/interrupt.py`](app/kernel/interrupt.py) → [`app/kernel/budget.py`](app/kernel/budget.py) → [`app/kernel/run.py`](app/kernel/run.py) → [`app/kernel/scheduler.py`](app/kernel/scheduler.py)
3. **看 RegOps 策略装配**：[`app/policy/regops_plan.py`](app/policy/regops_plan.py) → [`app/policy/regops_bus.py`](app/policy/regops_bus.py) → [`app/policy/regops_channels.py`](app/policy/regops_channels.py)
4. **看阶段二流水线执行**：[`app/engine/workflow/entrypoints.py`](app/engine/workflow/entrypoints.py) → [`app/engine/workflow/nodes.py`](app/engine/workflow/nodes.py) → [`app/engine/workflow/runtime.py`](app/engine/workflow/runtime.py) → [`app/engine/workflow/stages.py`](app/engine/workflow/stages.py)
