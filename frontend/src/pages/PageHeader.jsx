export default function PageHeader({ eyebrow = 'REGULATORY ENGINEERING', title, description, actions }) {
  return (
    <header className="topbar">
      <div>
        <p className="eyebrow">{eyebrow}</p>
        <h1>{title}</h1>
        {description && <p className="subtitle">{description}</p>}
      </div>
      {actions && <div className="actions">{actions}</div>}
    </header>
  )
}
