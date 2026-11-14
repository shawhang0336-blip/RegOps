export default function StatusMessage({ error, notice, onClose }) {
  if (!error && !notice) return null
  return (
    <div className="status-banner-stack">
      {error && (
        <div className="message error" role="alert">
          <div className="message-content">
            <span className="message-tag">操作提示</span>
            <span>{typeof error === 'string' ? error : JSON.stringify(error)}</span>
          </div>
          {onClose && (
            <button type="button" onClick={onClose}>
              知道了
            </button>
          )}
        </div>
      )}
      {notice && (
        <div className="message notice">
          <div className="message-content">
            <span className="message-tag">已完成</span>
            <span>{notice}</span>
          </div>
          {onClose && (
            <button type="button" onClick={onClose}>
              关闭
            </button>
          )}
        </div>
      )}
    </div>
  )
}
