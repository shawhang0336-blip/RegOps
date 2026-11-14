import { BrowserRouter, NavLink, Route, Routes } from 'react-router-dom'
import SettingsPage from './pages/SettingsPage.jsx'
import DocumentsPage from './pages/DocumentsPage.jsx'
import MetadataPage from './pages/MetadataPage.jsx'
import CodeSetsPage from './pages/CodeSetsPage.jsx'
import GenerationPage from './pages/GenerationPage.jsx'
import RunsPage from './pages/RunsPage.jsx'
import VersionsPage from './pages/VersionsPage.jsx'
import LegacyApp from './LegacyApp.jsx'

const navigation = [
  ['/settings', '系统设置', '01'],
  ['/documents', '文件中心', '02'],
  ['/metadata', '元数据工作台', '03'],
  ['/code-sets', '码值中心', '04'],
  ['/generation', '生成中心', '05'],
  ['/runs', '执行中心', '06'],
  ['/versions', '版本与审计', '07'],
]

function WorkbenchLayout() {
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand"><div className="brand-mark">R</div><div><strong>RegOps</strong><span>监管元数据与规则工程</span></div></div>
        <nav className="nav-list">
          {navigation.map(([path, label, number]) => <NavLink key={path} to={path} className={({ isActive }) => isActive ? 'active' : ''}><span>{number}</span>{label}</NavLink>)}
        </nav>
        <div className="sidebar-bottom"><div className="environment-dot" /><div><strong>GLM-5.3-Flash</strong><span>语义理解 · 复核 · 结构化赋值</span></div></div>
      </aside>
      <main className="main-content">
        <Routes>
          <Route path="/" element={<DocumentsPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="/documents" element={<DocumentsPage />} />
          <Route path="/metadata" element={<MetadataPage />} />
          <Route path="/code-sets" element={<CodeSetsPage />} />
          <Route path="/generation" element={<GenerationPage />} />
          <Route path="/runs" element={<RunsPage />} />
          <Route path="/versions" element={<VersionsPage />} />
          <Route path="/legacy" element={<LegacyApp />} />
        </Routes>
      </main>
    </div>
  )
}

export default function App() {
  return <BrowserRouter><WorkbenchLayout /></BrowserRouter>
}
