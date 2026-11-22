# RegOps 前端架构导览（`frontend/src/`）

前端严格遵循 **按业务域分层 + 单向依赖 + 后端 1:1 对称** 原则。  
`frontend/src/` 根目录只保留唯一挂载入口 [`main.jsx`](src/main.jsx) 与 **8 个职责正交的目录**，且 4 大业务域（`platform` / `catalog` / `generation` / `delivery`）与后端 [`backend/app/api/`](../backend/app/api/) 一一对应：

```text
utils / api / context  ←  hooks / components  ←  features/{catalog,generation,delivery,platform}  ←  app  ←  main.jsx
```

---

## 一、目录结构与分层说明

```text
frontend/src/
├── main.jsx                         # 入口：挂载 <App /> 与统一加载 styles/index.css
│
├── app/                             # 应用外壳与路由装配（唯一顶层路由表）
│   ├── App.jsx                      #   Provider / WorkbenchLayout / React.lazy 路由懒加载
│   └── BackendHealthPill.jsx        #   顶栏后端健康探测胶囊
│
├── api/                             # 统一后端 API 客户端层（全站唯一允许出现 /api/* 路径的目录）
│   ├── client.js                    #   底层 HTTP 封装（api / ApiError / downloadFile / 422 格式化）
│   ├── platform.js                  #   /api/health、/api/blobs/*、/api/settings/*、SSE URL 构造
│   ├── catalog.js                   #   /api/documents/*、/api/metadata/*、/api/code-sets/*
│   ├── generation.js                #   /api/generation/*、/api/artifacts/*
│   └── delivery.js                  #   /api/runs/*（生命周期、审批、重审、清理、diff、预览）
│
├── components/                      # 全站通用 UI 组件（零业务路由状态，严禁依赖 features/）
│   ├── PageHeader.jsx               #   页头标题与操作栏
│   ├── StatusMessage.jsx            #   错误横幅 + Toast 同步通知
│   ├── StatusBadge.jsx              #   统一状态徽章
│   ├── EvidenceCard.jsx             #   字段原文溯源卡片与 SourceLocatorTag
│   ├── RevisionDiffView.jsx         #   元数据修订差异视图
│   ├── DataTableModal.jsx           #   沙箱数据与文本产物预览弹窗
│   ├── CopyButton.jsx               #   一键复制按钮
│   ├── ErrorBoundary.jsx            #   路由级 React 错误边界
│   └── Skeleton.jsx                 #   列表与详情骨架屏
│
├── context/                         # 全局 Context
│   ├── FeedbackContext.jsx          #   全局 Toast 与异步确认弹窗（confirm）
│   └── ThemeContext.jsx             #   浅色 / 深色主题切换
│
├── hooks/                           # 通用 Hooks
│   └── useEventStream.js            #   Bus SSE 实时事件流订阅、轮询兜底与文档进度监听
│
├── utils/                           # 纯工具函数与领域词典（零 React/DOM 强耦合）
│   ├── activeDocument.js            #   跨工作台记住当前选中的文档 ID
│   ├── budget.js                    #   执行预算字段元信息与边界校验
│   ├── dateTime.js                  #   后端 UTC 时间解析与本地化显示
│   └── labels.js                    #   状态/字段/修订/问题码文案词典与友好错误转换
│
├── styles/                          # 全局样式收口目录
│   ├── index.css                    #   显式固定层叠顺序（base → pages → styles → ux）
│   ├── base.css / pages.css / styles.css / ux.css
│
└── features/                        # 按 4 大业务域组织的页面模块（与后端 api/ 1:1 对齐）
    ├── catalog/                     #   域 1：监管资产目录
    │   ├── PreparationWorkspace.jsx #     文档接入 + 报表/码表核对主工作台
    │   ├── documents/               #     DocumentsPage / CurrentFilePicker / ImportProgressPanel / documentHelpers
    │   ├── metadata/                #     MetadataPage / FieldEditorDrawer / MetadataPanels / metadataHelpers
    │   ├── codeSets/                #     CodeSetsPage
    │   └── versions/                #     VersionsPage（制度文档 Diff / 批次 SQL Diff / 修订历史）
    ├── generation/                  #   域 2：规则与产物装配
    │   ├── ArtifactsPage.jsx        #     规则工作台主页面
    │   ├── ArtifactDetail.jsx       #     表结构 / 码表 / 产物明细
    │   ├── RulesViews.jsx           #     单表与跨表规则审批视图
    │   ├── RuleDrawer.jsx           #     规则参数抽屉与手工改参
    │   └── artifactHelpers.js
    ├── delivery/                    #   域 3：运行批次控制与测试下载
    │   ├── RunsPage.jsx             #     批次列表、验证明细、沙箱预览与生产 ZIP 下载
    │   ├── HistoryCleanupModal.jsx  #     历史冗余批次清理弹窗
    │   └── RunTraceTimeline.jsx     #     EventLog / Bus 耗时瀑布与链路回放
    └── platform/                    #   域 4：系统设置
        └── SettingsPage.jsx         #     MySQL / Oracle / AI 连通性测试与执行预算配置
```

---

## 二、核心架构红线（自动化测试守卫）

[`tests/frontend_audit.test.mjs`](tests/frontend_audit.test.mjs) 在每次 `npm test` 中强制守卫以下三条红线：

1. **根目录整洁与四域对称**：`frontend/src/` 根目录只允许 `main.jsx` 与 8 个架构目录；`src/api/` 与 `src/features/` 严格对齐后端 `platform / catalog / generation / delivery` 四大域；
2. **接口路径集中收口**：严禁在 `src/api/` 之外任何页面、组件或 Hook 中裸写 `/api/*` 路径；
3. **严格单向依赖**：`api/`、`components/`、`context/`、`hooks/`、`utils/` 严禁反向 `import` `features/` 或 `app/`；`utils/` 保持纯函数叶模块，不依赖上层 UI/API/Hook。

---

## 三、看代码推荐阅读顺序

1. **看启动与顶层路由**：[`src/main.jsx`](src/main.jsx) → [`src/app/App.jsx`](src/app/App.jsx)
2. **看前后端接口契约**：[`src/api/client.js`](src/api/client.js) → [`src/api/catalog.js`](src/api/catalog.js) / [`generation.js`](src/api/generation.js) / [`delivery.js`](src/api/delivery.js) / [`platform.js`](src/api/platform.js)
3. **看三大核心业务工作台**：[`features/catalog/PreparationWorkspace.jsx`](src/features/catalog/PreparationWorkspace.jsx) → [`features/generation/ArtifactsPage.jsx`](src/features/generation/ArtifactsPage.jsx) → [`features/delivery/RunsPage.jsx`](src/features/delivery/RunsPage.jsx)
