export default function PageHeader({
  eyebrow = '',
  title,
  description,
  actions,
}) {
  return (
    <header className="topbar">
      <div className="topbar-title-group">
        {eyebrow && <p className="eyebrow">{eyebrow}</p>}
        <h1>{title}</h1>
        {description && <p className="subtitle">{description}</p>}
      </div>
      <div className="topbar-right">
        {actions && <div className="actions">{actions}</div>}
      </div>
    </header>
  )
}
