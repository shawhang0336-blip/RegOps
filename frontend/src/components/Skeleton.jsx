export function ListSkeleton({ rows = 6 }) {
  return (
    <div className="skeleton-stack" aria-busy="true" aria-label="正在加载列表">
      {Array.from({ length: rows }).map((_, idx) => (
        <div className="skeleton-row-card" key={idx}>
          <div className="skeleton-line wide" />
          <div className="skeleton-line medium" />
          <div className="skeleton-line short" />
        </div>
      ))}
    </div>
  )
}

export function DetailSkeleton({ blocks = 4 }) {
  return (
    <div className="skeleton-detail" aria-busy="true" aria-label="正在加载详情">
      <div className="skeleton-line title" />
      <div className="skeleton-line medium" />
      <div className="skeleton-grid">
        <div className="skeleton-box" />
        <div className="skeleton-box" />
        <div className="skeleton-box" />
      </div>
      {Array.from({ length: blocks }).map((_, idx) => (
        <div className="skeleton-line wide" key={idx} />
      ))}
    </div>
  )
}
