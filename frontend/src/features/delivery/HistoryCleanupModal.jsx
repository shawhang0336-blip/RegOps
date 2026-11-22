import { useEffect, useRef, useState } from 'react'
import { cleanupHistoryRuns, getHistoryCleanupPreview } from '../../api/delivery.js'
import { StatusBadge } from '../../components/StatusBadge.jsx'
import { formatTime } from '../../utils/dateTime.js'

export default function HistoryCleanupModal({ open, onClose, onCompleted }) {
  const [preview, setPreview] = useState(null)
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const busyRef = useRef(false)
  const onCloseRef = useRef(onClose)
  busyRef.current = busy
  onCloseRef.current = onClose

  useEffect(() => {
    if (!open) return undefined
    let cancelled = false
    setLoading(true)
    setError('')
    getHistoryCleanupPreview()
      .then((data) => {
        if (!cancelled) setPreview(data)
      })
      .catch((err) => {
        if (!cancelled) setError(err.message)
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [open])

  useEffect(() => {
    if (!open) return undefined
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    const onKeyDown = (event) => {
      if (event.key === 'Escape' && !busyRef.current) onCloseRef.current?.()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => {
      document.body.style.overflow = previousOverflow
      window.removeEventListener('keydown', onKeyDown)
    }
  }, [open])

  if (!open) return null

  async function executeCleanup() {
    setBusy(true)
    setError('')
    try {
      const result = await cleanupHistoryRuns()
      onCompleted?.(result)
      onClose()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="modal-backdrop" onClick={() => !busy && onClose()}>
      <section
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="cleanup-modal-title"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="panel-heading">
          <div>
            <h2 id="cleanup-modal-title">清理历史验证批次</h2>
            <p>保留每组报表的最新有效批次与正在运行的批次，清理早期重复或过期历史及其 Oracle 沙箱表。</p>
          </div>
          <button type="button" className="button secondary sm" disabled={busy} onClick={onClose}>
            关闭
          </button>
        </div>

        {loading && <div className="empty">正在分析可清理的历史批次...</div>}
        {error && <p className="danger-text">{error}</p>}

        {!loading && preview && (
          <>
            <div className="cleanup-preview-summary">
              <div>
                <span>全部批次</span>
                <strong>{preview.total}</strong>
              </div>
              <div>
                <span>保留最新</span>
                <strong className="success-text">{preview.keep_count}</strong>
              </div>
              <div>
                <span>待清理历史</span>
                <strong className={preview.delete_count > 0 ? 'danger-text' : ''}>{preview.delete_count}</strong>
              </div>
            </div>

            {preview.delete_count > 0 ? (
              <ul className="cleanup-run-list">
                {(preview.runs || []).map((run) => (
                  <li key={run.id}>
                    <div>
                      <strong>
                        {(run.tables || []).map((t) => t.table_code).join('、') || run.id.slice(0, 8)}
                      </strong>
                      <small className="muted-text" style={{ marginLeft: 8 }}>{formatTime(run.created_at)}</small>
                    </div>
                    <StatusBadge status={run.status} />
                  </li>
                ))}
              </ul>
            ) : (
              <div className="empty">当前没有可清理的历史冗余批次。</div>
            )}

            <div className="actions" style={{ justifyContent: 'flex-end' }}>
              <button type="button" className="button secondary" disabled={busy} onClick={onClose}>
                取消
              </button>
              <button
                type="button"
                className="button danger"
                disabled={busy || preview.delete_count === 0}
                onClick={executeCleanup}
              >
                {busy ? '正在清理...' : `确认清理 ${preview.delete_count} 个历史批次`}
              </button>
            </div>
          </>
        )}
      </section>
    </div>
  )
}
