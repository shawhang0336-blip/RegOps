import { useOperator } from '../context/OperatorContext.jsx'

export default function PageHeader({
  eyebrow = 'REGOPS · 监管数据与规则工程工作台',
  title,
  description,
  actions,
  hideOperator = false,
}) {
  const { operator, setOperator, theme, toggleTheme } = useOperator()

  return (
    <header className="topbar">
      <div className="topbar-title-group">
        <p className="eyebrow">{eyebrow}</p>
        <h1>{title}</h1>
        {description && <p className="subtitle">{description}</p>}
      </div>
      <div className="topbar-right">
        <button
          type="button"
          className="theme-toggle-btn"
          onClick={toggleTheme}
          title="切换深色/浅色外观模式（快捷键 Alt + D）"
        >
          <span className="theme-icon" aria-hidden="true">
            {theme === 'dark' ? '☀️' : '🌙'}
          </span>
          <span>{theme === 'dark' ? '浅色模式' : '深色模式'}</span>
          <kbd className="kbd-hint">Alt+D</kbd>
        </button>

        {!hideOperator && (
          <label className="operator-pill" title="操作人/审批人身份会在全站各步骤自动复用并保存在浏览器本地">
            <span className="operator-dot" />
            <span className="operator-label">当前操作/审批人</span>
            <input
              value={operator}
              onChange={(e) => setOperator(e.target.value)}
              placeholder="输入姓名或工号..."
            />
          </label>
        )}
        {actions && <div className="actions">{actions}</div>}
      </div>
    </header>
  )
}
