import { STATUS_META } from '../utils/labels.js'

export function StatusBadge({ status, children, tone }) {
  const meta = STATUS_META[status] || { label: status || '未知', tone: 'neutral' }
  const finalTone = tone || meta.tone
  return <span className={`badge badge-${finalTone}`}>{children ?? meta.label}</span>
}

export default StatusBadge
