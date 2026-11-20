# RegOps 后端架构导览（`backend/app/`）

后端严格遵循 **ProdAgent“机制在内，策略在外”** + **清晰业务域分层** 原则。  
`backend/app/` 根目录只保留唯一 FastAPI 启动入口 [`main.py`](app/main.py) 与 **8 个职责正交的子包**，单文件职责清晰，所有跨层引用均使用显式绝对导入（`from app.<层>.<模块> import ...`），打开任何文件即可一眼看清其依赖方向。

---

## 一、目录结构与分层依赖图

```text
backend/app/
├── main.py                      # 极简 FastAPI 启动入口（~65 行：lifespan / CORS / 挂载路由）
│
├── kernel/                      # ① ProdAgent 微内核（零业务依赖，可独立驱动任意工作流）
│   ├── event_log.py             #    只追加哈希链事件账本（唯一事实源）+ 折叠引擎 + Interrupt
│   ├── blob_store.py            #    SHA-256 内容寻址不可变大对象存储
│   ├── bus.py                   #    全生命周期事件总线（check/adjudicate 门禁 + fire/subscribe 广播）
│   ├── channel.py               #    带 Reducer（last_value / merge_dict / append_list）的共享数据通道与 RunContext
│   ├── plan.py                  #    静态只读工作流蓝图（PlanNode / PlanEdge / WorkflowPlan）
│   └── scheduler.py             #    零业务硬编码调度器（屏障排空 BUS_REJECTED → 落账 → 合并 Channel → 折叠）
│
├── policy/                      # ② RegOps 领域策略层（以插件方式装配到 kernel 上）
│   ├── regops_plan.py           #    12 节点装配流水线拓扑（DEFAULT_PLAN）与业务事件状态折叠（_apply）
│   ├── regops_bus.py            #    五大确定性事前门禁裁决器（字段/跨表/算子/码表/切块 ID 白名单）
│   ├── regops_channels.py       #    16 个命名数据通道与 EventLog + BlobStore 断点折叠恢复
│   └── rule_backfill.py         #    必填 / 列宽上限（LENGTH_MAX）/ 固定值确定性规则补齐策略
│
├── infra/                       # ③ 基础设施与 ORM 数据模型
│   ├── config.py                #    环境配置 Settings（MySQL / Oracle / LLM / Blob 目录）
│   ├── db.py                    #    SQLAlchemy Engine / SessionLocal / Alembic 初始化
│   ├── errors.py                #    统一领域异常 WorkflowError
│   ├── identity.py              #    操作人身份占位符
│   ├── models.py                #    核心运行时表（runs / events / rules / specifications / settings）
│   └── asset_models.py          #    资产表（文档 / 原始块 / 元数据 / 码表 / 审批 / 产物）
│
├── ingestion/                   # ④ 业务域一：监管文档接入与结构解析（阶段一）
│   ├── extractors.py            #    PDF / Word / Excel 原始块提取
│   ├── catalog.py               #    文档目录与章节识别
│   ├── spec_parser.py           #    数据字典表结构与字段解析
│   ├── spec_quality.py          #    原始块解析质量体检
│   ├── structure_service.py     #    AI 辅助文档体系切块
│   ├── segment_service.py       #    父子层级段落切分
│   └── import_progress.py       #    文档解析实时进度与 document.progress 总线广播
│
├── assets/                      # ⑤ 业务域二：元数据资产与码表中心
│   ├── metadata_domain.py       #    元数据草稿 Pydantic 契约
│   ├── metadata_views.py        #    原文证据降级核查、工作台视图序列化与 metadata_to_spec
│   ├── metadata_mutations.py    #    字段编辑、表删除、版本发布与 CSV/XLSX 批量类型导入
│   ├── metadata_service.py      #    确定性优先 + 疑难字段 AI 辅助提取服务
│   ├── readiness.py             #    表资产就绪度检查
│   └── code_sets/               #    码表子域
│       ├── parser.py            #      纯函数码值文本解析（内联枚举 / 固定值 / 条件句 / 名称归一化）
│       ├── linking.py           #      字段-码表自动对齐、序号分配、状态刷新与阻断检查
│       ├── service.py           #      码表抽取、批量并发、AI 复核与摘要/删除服务
│       ├── readiness.py         #      码表就绪度评估与自动发布
│       └── oracle.py            #      已发布码表加载与 Oracle 沙箱码表 DDL 编译
│
├── engine/                      # ⑥ 业务域三：规则装配、造数与 Oracle 沙箱执行（阶段二）
│   ├── domain.py                #    核心 RuleDefinition / SpecDocument 与 LLM_OPERATORS 白名单
│   ├── rules.py                 #    单条规则校验与样本违规判定
│   ├── compiler.py              #    规则 → Oracle / MySQL 校验 SQL 编译器
│   ├── binding.py               #    Schema / Rule / Generator / AuditCase 确定性绑定校验
│   ├── llm.py                   #    受控 Tool-Call 大模型客户端与并发控制
│   ├── oracle.py                #    OracleSandbox 物理表管理 + GenericOracleExecutor 真跑审计
│   └── workflow/                #    阶段二编排流水线
│       ├── runtime.py           #      _call_validated（退避重试+Bus门禁）与 _fanout（BSP 并发扇出）
│       ├── stages.py            #      规划 / Schema / 规则 / Oracle 资产 / 造数 / 审计用例 / 靶向重绑
│       ├── nodes.py             #      7 个 ProdAgent NodeBody（body(input, ctx) -> Outcome）
│       └── entrypoints.py       #      drive / start / approve / confirm / retry 唯一业务入口
│
├── delivery/                    # ⑦ 业务域四：生产交付与业务库只读校验（阶段三）
│   ├── production.py            #    生产校验包结构定义与只读 SQL 组装
│   ├── production_service.py    #    面向已确认 Run 的生产包打包服务
│   └── business_db.py           #    业务库只读抽样探查连接器
│
└── api/                         # ⑧ HTTP 接口层（v1 与 v2 统一收口）
    ├── deps.py                  #    公共 Session 依赖、请求体契约与视图序列化
    ├── system.py                #    /api/health / /api/blobs/* / /api/business/preview
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

[`tests/test_kernel_policy_isolation.py`](tests/test_kernel_policy_isolation.py) 在每次回归中强制守卫以下三条红线：

1. **内核纯净度**：`app/kernel/*.py` 严禁 `import` 任何业务层（`policy` / `ingestion` / `assets` / `engine` / `delivery` / `api`）；
2. **内核独立可运行**：仅依赖 `app.kernel` 即可编排、门禁拦截、归约 Channel 并驱动非监管工作流；
3. **根目录不退化**：`backend/app/` 根目录只允许 `main.py` 与上述 8 个架构包，禁止重新平铺散落业务文件。

---

## 三、看代码推荐阅读顺序

1. **看启动与路由**：[`app/main.py`](app/main.py) → [`app/api/runs.py`](app/api/runs.py) → [`app/api/v2/generation.py`](app/api/v2/generation.py)
2. **看微内核机制**：[`app/kernel/plan.py`](app/kernel/plan.py) → [`app/kernel/event_log.py`](app/kernel/event_log.py) → [`app/kernel/bus.py`](app/kernel/bus.py) → [`app/kernel/channel.py`](app/kernel/channel.py) → [`app/kernel/scheduler.py`](app/kernel/scheduler.py)
3. **看 RegOps 策略装配**：[`app/policy/regops_plan.py`](app/policy/regops_plan.py) → [`app/policy/regops_bus.py`](app/policy/regops_bus.py) → [`app/policy/regops_channels.py`](app/policy/regops_channels.py)
4. **看阶段二流水线执行**：[`app/engine/workflow/entrypoints.py`](app/engine/workflow/entrypoints.py) → [`app/engine/workflow/nodes.py`](app/engine/workflow/nodes.py) → [`app/engine/workflow/runtime.py`](app/engine/workflow/runtime.py) → [`app/engine/workflow/stages.py`](app/engine/workflow/stages.py)
