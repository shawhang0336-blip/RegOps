import { BrowserRouter, NavLink, Route, Routes } from 'react-router-dom'
import { OperatorProvider } from './context/OperatorContext.jsx'
import SettingsPage from './pages/SettingsPage.jsx'
import DocumentsPage from './pages/DocumentsPage.jsx'
import MetadataPage from './pages/MetadataPage.jsx'
import CodeSetsPage from './pages/CodeSetsPage.jsx'
import GenerationPage from './pages/GenerationPage.jsx'
import RunsPage from './pages/RunsPage.jsx'
import VersionsPage from './pages/VersionsPage.jsx'
import LegacyApp from './LegacyApp.jsx'

const pipelineNavigation = [
  ['/documents', '规范导入与切块', '01', '上传 PDF/Word/Excel 提取原始块'],
  ['/metadata', '元数据与版本台', '02', 'AI 初值复核 · 字段精修 · 版本发布'],
  ['/code-sets', '公共码值中心', '03', '消解未决码值 · Excel 批量导入'],
  ['/generation', '生成范围与预检', '04', '码值阻断预检 · 启动 AI 规则装配'],
  ['/runs', '执行与审计中心', '05', '8 阶进度看板 · 人工确权 · 部署包'],
]

const governanceNavigation = [
  ['/versions', '全局版本审计', 'AU'],
  ['/settings', '系统与执行预算', 'SY'],
]

function WorkbenchLayout() {
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">R</div>
          <div>
            <strong>RegOps</strong>
            <span>监管元数据与规则工程台</span>
          </div>
        </div>

        <div className="nav-group">
          <p className="nav-group-title">主干工程流水线</p>
          <nav className="nav-list">
            {pipelineNavigation.map(([path, label, number, hint]) => (
              <NavLink
                key={path}
                to={path}
                title={hint}
                className={({ isActive }) => (isActive ? 'nav-item active' : 'nav-item')}
              >
                <span className="nav-step-badge">{number}</span>
                <div className="nav-item-text">
                  <strong>{label}</strong>
                  <small>{hint}</small>
                </div>
              </NavLink>
            ))}
          </nav>
        </div>

        <div className="nav-group nav-group-secondary">
          <p className="nav-group-title">审计与系统底座</p>
          <nav className="nav-list compact">
            {governanceNavigation.map(([path, label, code]) => (
              <NavLink
                key={path}
                to={path}
                className={({ isActive }) => (isActive ? 'nav-item active' : 'nav-item')}
              >
                <span className="nav-step-badge subtle">{code}</span>
                <strong>{label}</strong>
              </NavLink>
            ))}
          </nav>
        </div>

        <div className="sidebar-bottom">
          <div className="environment-dot" />
          <div>
            <strong>GLM 结构化赋值引擎</strong>
            <span>确定性算子 · 沙箱双表审计</span>
          </div>
        </div>
      </aside>

      <main className="main-content">
        <Routes>
          <Route path="/" element={<DocumentsPage />} />
          <Route path="/documents" element={<DocumentsPage />} />
          <Route path="/metadata" element={<MetadataPage />} />
          <Route path="/code-sets" element={<CodeSetsPage />} />
          <Route path="/generation" element={<GenerationPage />} />
          <Route path="/runs" element={<RunsPage />} />
          <Route path="/versions" element={<VersionsPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="/legacy" element={<LegacyApp />} />
        </Routes>
      </main>
    </div>
  )
}

export default function App() {
  return (
    <OperatorProvider>
      <BrowserRouter>
        <WorkbenchLayout />
      </BrowserRouter>
    </OperatorProvider>
  )
}
