import { useTheme } from '../context/ThemeContext.jsx'

export default function PageHeader({
  eyebrow = '',
  title,
  description,
  actions,
}) {
  const { theme, toggleTheme } = useTheme()

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
        {actions && <div className="actions">{actions}</div>}
      </div>
    </header>
  )
}
