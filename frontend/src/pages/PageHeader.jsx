import { useOperator } from '../context/OperatorContext.jsx'

export default function PageHeader({
  eyebrow = '',
  title,
  description,
  actions,
  hideOperator = false,
}) {
  const { operator, setOperator, theme, toggleTheme } = useOperator()

  return (
    <header className="topbar">
      <div className="topbar-title-group">
        {eyebrow && <p className="eyebrow">{eyebrow}</p>}
        <h1>{title}</h1>
        {description && <p className="subtitle">{description}</p>}
      </div>
      <div className="topbar-right">
        <button
          type="button"
          className="theme-toggle-btn"
          onClick={toggleTheme}
          title="切换深色/浅色外观模式"
        >
          <span>{theme === 'dark' ? '切换浅色' : '切换深色'}</span>
        </button>

        {!hideOperator && (
          <label className="operator-pill" title="用于保存修改和确认规则，会在本机自动记住">
            <span className="operator-dot" />
            <span className="operator-label">操作人</span>
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
