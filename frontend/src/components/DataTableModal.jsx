import { useEffect, useRef, useState } from 'react'

export function DataTableModal({ artifact, onClose }) {
  const [filter, setFilter] = useState('')
  const [rawMode, setRawMode] = useState(false)
  const [copied, setCopied] = useState(false)
  const copyTimerRef = useRef(null)

  useEffect(() => {
    setFilter('')
    setRawMode(false)
    setCopied(false)
  }, [artifact])

  useEffect(() => () => {
    if (copyTimerRef.current) window.clearTimeout(copyTimerRef.current)
  }, [])

  useEffect(() => {
    if (!artifact) return undefined
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    const onKeyDown = (event) => {
      if (event.key === 'Escape') onClose?.()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => {
      document.body.style.overflow = previousOverflow
      window.removeEventListener('keydown', onKeyDown)
    }
  }, [artifact, onClose])

  if (!artifact) return null

  const rows = Array.isArray(artifact.rows) ? artifact.rows : null
  const columns = rows && rows.length > 0 ? Object.keys(rows[0]) : []
  const filteredRows = rows
    ? rows.filter((row) =>
        !filter.trim() ||
        Object.values(row).some((v) => String(v ?? '').toLowerCase().includes(filter.toLowerCase()))
      )
    : []

  async function copyContent() {
    const text = rows ? JSON.stringify(rows, null, 2) : artifact.content || ''
    if (!text) return
    try {
      await navigator.clipboard.writeText(text)
    } catch {
      const area = document.createElement('textarea')
      area.value = text
      area.style.position = 'fixed'
      area.style.opacity = '0'
      document.body.appendChild(area)
      area.select()
      document.execCommand('copy')
      document.body.removeChild(area)
    }
    setCopied(true)
    if (copyTimerRef.current) window.clearTimeout(copyTimerRef.current)
    copyTimerRef.current = window.setTimeout(() => setCopied(false), 2000)
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <section className="modal modal-wide" onClick={(e) => e.stopPropagation()}>
        <div className="panel-heading modal-header">
          <div>
            <h2>{artifact.title}</h2>
            {rows && (
              <p>
                共加载 {rows.length} 行记录 · {columns.length} 个字段
                {filter && `（筛选后 ${filteredRows.length} 行）`}
              </p>
            )}
          </div>
          <div className="actions">
            {rows && (
              <input
                type="search"
                placeholder="搜索表格内容..."
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
              />
            )}
            {rows && (
              <button
                type="button"
                className="button secondary"
                onClick={() => setRawMode(!rawMode)}
              >
                {rawMode ? '切换表格视图' : '查看 JSON'}
              </button>
            )}
            <button type="button" className="button secondary" onClick={copyContent}>
              {copied ? '已复制' : '复制内容'}
            </button>
            <button type="button" className="button primary" onClick={onClose}>
              关闭窗口
            </button>
          </div>
        </div>

        {rows && !rawMode ? (
          rows.length > 0 ? (
            <div className="table-wrap modal-table-wrap">
              <table className="data-preview-table">
                <thead>
                  <tr>
                    <th>#</th>
                    {columns.map((col) => (
                      <th key={col}>{col}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {filteredRows.map((row, idx) => (
                    <tr key={idx}>
                      <td className="row-index">{idx + 1}</td>
                      {columns.map((col) => (
                        <td key={col}>
                          {row[col] === null || row[col] === undefined ? (
                            <span className="null-pill">NULL</span>
                          ) : typeof row[col] === 'object' ? (
                            <code>{JSON.stringify(row[col])}</code>
                          ) : (
                            String(row[col])
                          )}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="empty-state">该沙箱表当前暂无数据行。</div>
          )
        ) : (
          <pre className="modal-code-viewer">
            {rows ? JSON.stringify(rows, null, 2) : artifact.content}
          </pre>
        )}
      </section>
    </div>
  )
}

export default DataTableModal
