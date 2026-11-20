import { BrowserRouter, Navigate, NavLink, Route, Routes, useLocation } from 'react-router-dom'
import { ThemeProvider, useTheme } from './context/ThemeContext.jsx'
import SettingsPage from './pages/SettingsPage.jsx'
import PreparationWorkspace from './pages/PreparationWorkspace.jsx'
import ArtifactsPage from './pages/ArtifactsPage.jsx'
import RunsPage from './pages/RunsPage.jsx'
import VersionsPage from './pages/VersionsPage.jsx'

const pipelineNavigation = [
  ['/documents', '规范解析与核对', '01', '上传文件，自动解析并核对字段和码表'],
  ['/generation', '生成表结构、码表和规则', '02', 'Oracle 建表，生成并确认校验规则'],
  ['/runs', '测试与下载', '03', '生成测试数据，确认结果并下载 SQL'],
]

const governanceNavigation = [
  ['/versions', '变更记录', '历'],
  ['/settings', '系统设置', '设'],
]

function BrandMark() {
  return (
    <svg viewBox="0 0 40 40" role="img" aria-hidden="true">
      <defs>
        <linearGradient id="regops-mark-bg" x1="6" y1="3" x2="35" y2="38" gradientUnits="userSpaceOnUse">
          <stop stopColor="#f0182c" />
          <stop offset="1" stopColor="#a90012" />
        </linearGradient>
      </defs>
      <rect width="40" height="40" rx="11" fill="url(#regops-mark-bg)" />
      <path d="M12.2 8.5h10.1l6 6V30a1.8 1.8 0 0 1-1.8 1.8H12.2A1.8 1.8 0 0 1 10.4 30V10.3a1.8 1.8 0 0 1 1.8-1.8Z" fill="none" stroke="#fff" strokeWidth="2" />
      <path d="M22.2 8.7V15h5.9" fill="none" stroke="#fff" strokeWidth="2" strokeLinejoin="round" />
      <path d="M14.8 18.3h8.4M14.8 22.4h6.2" stroke="#fff" strokeWidth="1.7" strokeLinecap="round" opacity=".92" />
      <circle cx="29" cy="28.5" r="6.2" fill="#16875b" stroke="#b80014" strokeWidth="1.7" />
      <path d="m26.3 28.5 1.8 1.8 3.7-3.7" fill="none" stroke="#fff" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

function PreparationRedirect({ view }) {
  const location = useLocation()
  const params = new URLSearchParams(location.search)
  params.set('view', view)
  return <Navigate to={`/documents?${params.toString()}`} replace />
}

function WorkbenchLayout() {
  const { theme, toggleTheme } = useTheme()
  const location = useLocation()

  return (
    <div className="app-shell horizontal-shell">
      <header className="workbench-header">
        <NavLink to="/documents" className="workbench-brand" aria-label="RegOps 监管报送规则生成工具">
          <span className="workbench-brand-mark"><BrandMark /></span>
          <span className="workbench-brand-name"><span>Reg</span><span className="workbench-brand-accent">Ops</span></span>
        </NavLink>

        <nav className="workbench-steps" aria-label="使用流程">
          {pipelineNavigation.map(([path, label, number, hint]) => (
            <NavLink
              key={path}
              to={path}
              title={`${label}：${hint}`}
              aria-label={`${number} ${label}：${hint}`}
              className={({ isActive }) => `workbench-step ${isActive || (path === '/documents' && location.pathname === '/') ? 'active' : ''}`}
            >
              <span className="workbench-step-number">{number}</span>
              <span className="workbench-step-label">{label}</span>
            </NavLink>
          ))}
        </nav>

        <nav className="workbench-secondary-nav" aria-label="其他功能">
          {governanceNavigation.map(([path, label, code]) => (
            <NavLink
              key={path}
              to={path}
              title={label}
              className={({ isActive }) => `workbench-secondary ${isActive ? 'active' : ''}`}
            >
              <span aria-hidden="true">{code}</span>
              <strong>{label}</strong>
            </NavLink>
          ))}
        </nav>

        <span className="workbench-status" title="数据与验证环境可用">
          <span className="workbench-status-dot" />
          <span className="workbench-status-text">已连接</span>
        </span>
        <button
          type="button"
          className="workbench-theme-toggle"
          onClick={toggleTheme}
          title="切换深色/浅色外观模式"
        >
          {theme === 'dark' ? '浅色' : '深色'}
        </button>
      </header>

      <main className="main-content">
        <Routes>
          <Route path="/" element={<PreparationWorkspace />} />
          <Route path="/documents" element={<PreparationWorkspace />} />
          <Route path="/metadata" element={<PreparationRedirect view="fields" />} />
          <Route path="/code-sets" element={<PreparationRedirect view="codes" />} />
          <Route path="/generation" element={<ArtifactsPage />} />
          <Route path="/runs" element={<RunsPage />} />
          <Route path="/versions" element={<VersionsPage />} />
          <Route path="/settings" element={<SettingsPage />} />
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
