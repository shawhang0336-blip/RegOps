import { useEffect, useRef } from 'react'
import { useFeedback } from '../context/FeedbackContext.jsx'

export default function StatusMessage({ error, notice, onClose }) {
  const { notify } = useFeedback()
  const lastNoticeRef = useRef('')

  // 成功提示自动同步到全局悬浮 Toast，避免用户滚动在页面下方或抽屉里看不到反馈
  useEffect(() => {
    if (notice && notice !== lastNoticeRef.current) {
      lastNoticeRef.current = notice
      notify(notice)
    } else if (!notice) {
      lastNoticeRef.current = ''
    }
  }, [notice, notify])

  if (!error) return null
  return (
    <div className="status-banner-stack">
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
    </div>
  )
}
