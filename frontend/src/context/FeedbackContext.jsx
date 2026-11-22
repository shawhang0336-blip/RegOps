import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'

const FeedbackContext = createContext(null)

let nextToastId = 1

export function FeedbackProvider({ children }) {
  const [toasts, setToasts] = useState([])
  const [dialog, setDialog] = useState(null)
  const resolverRef = useRef(null)
  const toastTimersRef = useRef(new Map())

  const clearToastTimer = useCallback((id) => {
    const timer = toastTimersRef.current.get(id)
    if (timer) {
      window.clearTimeout(timer)
      toastTimersRef.current.delete(id)
    }
  }, [])

  const dismissToast = useCallback((id) => {
    clearToastTimer(id)
    setToasts((prev) => prev.filter((item) => item.id !== id))
  }, [clearToastTimer])

  const pushToast = useCallback((message, { tone = 'success', title = '', duration = 4200 } = {}) => {
    const text = typeof message === 'string' ? message : JSON.stringify(message)
    if (!text) return null
    const id = nextToastId++
    setToasts((prev) => {
      // 避免短时间内同一条文案重复刷屏
      const deduped = prev.filter((item) => {
        const dup = item.text === text && item.tone === tone
        if (dup) clearToastTimer(item.id)
        return !dup
      })
      const kept = deduped.slice(-3)
      const dropped = deduped.slice(0, Math.max(0, deduped.length - 3))
      dropped.forEach((item) => clearToastTimer(item.id))
      return [...kept, { id, tone, title, text }]
    })
    if (duration > 0) {
      const timer = window.setTimeout(() => {
        toastTimersRef.current.delete(id)
        setToasts((prev) => prev.filter((item) => item.id !== id))
      }, duration)
      toastTimersRef.current.set(id, timer)
    }
    return id
  }, [clearToastTimer])

  const notify = useCallback(
    (message, title = '已完成') => pushToast(message, { tone: 'success', title, duration: 4200 }),
    [pushToast],
  )

  const notifyError = useCallback(
    (message, title = '操作提示') => pushToast(message, { tone: 'error', title, duration: 0 }),
    [pushToast],
  )

  const confirm = useCallback((options) => {
    const normalized = typeof options === 'string' ? { message: options } : (options || {})
    // 若已有前序确认弹窗尚未关闭，先以 false 结算旧 Promise，避免调用方永久挂起
    if (resolverRef.current) {
      const staleResolve = resolverRef.current
      resolverRef.current = null
      staleResolve(false)
    }
    return new Promise((resolve) => {
      resolverRef.current = resolve
      setDialog({
        title: normalized.title || '请确认操作',
        message: normalized.message || '',
        details: normalized.details || '',
        confirmLabel: normalized.confirmLabel || '确认',
        cancelLabel: normalized.cancelLabel || '取消',
        tone: normalized.tone || 'primary',
      })
    })
  }, [])

  const closeDialog = useCallback((accepted) => {
    const resolve = resolverRef.current
    resolverRef.current = null
    setDialog(null)
    resolve?.(Boolean(accepted))
  }, [])

  useEffect(() => () => {
    toastTimersRef.current.forEach((timer) => window.clearTimeout(timer))
    toastTimersRef.current.clear()
    if (resolverRef.current) {
      const staleResolve = resolverRef.current
      resolverRef.current = null
      staleResolve(false)
    }
  }, [])

  useEffect(() => {
    if (!dialog) return undefined
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    const onKeyDown = (event) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        closeDialog(false)
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => {
      document.body.style.overflow = previousOverflow
      window.removeEventListener('keydown', onKeyDown)
    }
  }, [dialog, closeDialog])

  const contextValue = useMemo(
    () => ({ notify, notifyError, pushToast, dismissToast, confirm }),
    [notify, notifyError, pushToast, dismissToast, confirm],
  )

  return (
    <FeedbackContext.Provider value={contextValue}>
      {children}

      {toasts.length > 0 && (
        <div className="toast-viewport" aria-live="polite">
          {toasts.map((item) => (
            <div
              key={item.id}
              className={`toast-item toast-${item.tone}`}
              role={item.tone === 'error' ? 'alert' : 'status'}
            >
              <div className="toast-body">
                {item.title && <strong className="toast-title">{item.title}</strong>}
                <span className="toast-text">{item.text}</span>
              </div>
              <button
                type="button"
                className="toast-close"
                aria-label="关闭提示"
                onClick={() => dismissToast(item.id)}
              >
                ×
              </button>
            </div>
          ))}
        </div>
      )}

      {dialog && (
        <div className="modal-backdrop confirm-backdrop" onClick={() => closeDialog(false)}>
          <section
            className="modal confirm-dialog"
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="confirm-dialog-title"
            onClick={(event) => event.stopPropagation()}
          >
            <header className="confirm-dialog-head">
              <h2 id="confirm-dialog-title">{dialog.title}</h2>
            </header>
            <div className="confirm-dialog-body">
              {dialog.message && <p className="confirm-dialog-message">{dialog.message}</p>}
              {dialog.details && <p className="confirm-dialog-details">{dialog.details}</p>}
            </div>
            <footer className="confirm-dialog-actions">
              <button type="button" className="button secondary" onClick={() => closeDialog(false)}>
                {dialog.cancelLabel}
              </button>
              <button
                type="button"
                className={`button ${dialog.tone === 'danger' ? 'danger' : 'primary'}`}
                autoFocus
                onClick={() => closeDialog(true)}
              >
                {dialog.confirmLabel}
              </button>
            </footer>
          </section>
        </div>
      )}
    </FeedbackContext.Provider>
  )
}

export function useFeedback() {
  const ctx = useContext(FeedbackContext)
  if (!ctx) {
    throw new Error('useFeedback must be used within FeedbackProvider')
  }
  return ctx
}
