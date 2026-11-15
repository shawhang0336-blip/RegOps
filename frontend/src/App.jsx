import { BrowserRouter, Navigate, NavLink, Route, Routes, useLocation } from 'react-router-dom'
import { ThemeProvider } from './context/ThemeContext.jsx'
import SettingsPage from './pages/SettingsPage.jsx'
import PreparationWorkspace from './pages/PreparationWorkspace.jsx'
import GenerationPage from './pages/GenerationPage.jsx'
import RunsPage from './pages/RunsPage.jsx'
import VersionsPage from './pages/VersionsPage.jsx'
import LegacyApp from './LegacyApp.jsx'

const pipelineNavigation = [
  ['/documents', '规范解析与核对', '01', '上传文件，自动解析并核对字段和码表'],
  ['/generation', '生成表结构、码表和规则', '02', 'Oracle 建表，生成并确认校验规则'],
  ['/runs', '测试与下载', '03', '生成测试数据，确认结果并下载 SQL'],
]

const governanceNavigation = [
  ['/versions', '变更记录', '历'],
  ['/settings', '系统设置', '设'],
]

function PreparationRedirect({ view }) {
  const location = useLocation()
  const params = new URLSearchParams(location.search)
  params.set('view', view)
  return <Navigate to={`/documents?${params.toString()}`} replace />
}

function WorkbenchLayout() {
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">R</div>
          <div>
            <strong>RegOps</strong>
            <span>监管报送规则生成工具</span>
          </div>
        </div>

        <div className="nav-group">
          <p className="nav-group-title">使用流程</p>
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
          <p className="nav-group-title">其他功能</p>
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
            <strong>服务已连接</strong>
            <span>数据与验证环境可用</span>
          </div>
        </div>
      </aside>

      <main className="main-content">
        <Routes>
          <Route path="/" element={<PreparationWorkspace />} />
          <Route path="/documents" element={<PreparationWorkspace />} />
          <Route path="/metadata" element={<PreparationRedirect view="fields" />} />
          <Route path="/code-sets" element={<PreparationRedirect view="codes" />} />
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
    <ThemeProvider>
      <BrowserRouter>
        <WorkbenchLayout />
      </BrowserRouter>
    </ThemeProvider>
  )
}
