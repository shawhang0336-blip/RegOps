import { useState } from 'react'
import { StatusBadge } from '../../components/StatusBadge.jsx'
import { formatTime } from '../../utils/dateTime.js'

function toneForKind(kind) {
  const k = String(kind || '').toUpperCase()
  if (k.includes('FAIL') || k.includes('ERROR') || k.includes('REJECT')) return 'red'
  if (k.includes('REDIRECT') || k.includes('INTERRUPT') || k.includes('APPROVAL') || k.includes('RESUMED')) return 'amber'
  if (k.includes('COMPLETED') || k.includes('PASSED') || k.includes('APPROVED') || k.includes('CONFIRMED')) return 'green'
  if (k.includes('BACKFILL') || k.includes('FANOUT') || k.includes('BARRIER')) return 'violet'
  return 'blue'
}

function summarizeLedgerEvent(ev) {
  const p = ev.payload || {}
  if (ev.kind === 'BUS_REJECTED') return `拦截 ${p.tool || '工具'}：${p.reason || ''}`
  if (ev.kind === 'GO_REDIRECT') return `跳转至 ${p.target || '目标节点'}${p.reason ? `（${p.reason}）` : ''}`
  if (ev.kind === 'RUN_RESUMED') return `从节点 ${p.resume_from || ev.node} 断点续跑`
  if (ev.kind === 'APPROVAL_REQUIRED' || ev.kind === 'TEST_CONFIRMATION_REQUIRED') return p.question || '等待人工确认'
  if (ev.kind === 'RULE_BACKFILL') return `确定性补齐 ${p.total ?? 0} 条规则（${p.kind || 'metadata'}）`
  if (ev.kind === 'RULES_BOUND') return `绑定规则，待确认 ${p.pending_count ?? 0} 条`
  if (ev.kind === 'BSP_FANOUT') return `并发扇出 ${(p.table_codes || []).length} 张表`
  if (ev.kind === 'ORACLE_AUDITED') return p.result?.audit_summary?.all_passed ? 'Oracle 沙箱验证全部通过' : 'Oracle 沙箱验证完成（含未通过项）'
  if (ev.kind === 'RUN_CANCELLED') return `已人工中止：${p.reason || p.result?.error || 'Run cancelled'}`
  if (ev.kind === 'RUN_FAILED') return p.result?.error || p.error || '执行中断'
  if (ev.kind === 'RUN_COMPLETED') return '流程抵达终态，产出就绪'
  if (p.reason) return String(p.reason)
  if (p.error) return String(p.error)
  const keys = Object.keys(p)
  return keys.length ? keys.slice(0, 4).join(' / ') : '—'
}

function summarizeBusEvent(ev) {
  if (ev.topic === 'budget.exceeded') return `预算熔断：${ev.metric} = ${ev.used}（上限 ${ev.limit}）`
  if (ev.topic === 'run.cancelled') return `运行已中止：${ev.reason || 'operator cancelled'}`
  if (ev.reason) return `${ev.tool ? `${ev.tool} · ` : ''}${ev.reason}`
  if (ev.kind) return `落账 ${ev.kind}`
  if (ev.target) return `跳转 → ${ev.target}`
  if (ev.stage && ev.table_codes) return `阶段 ${ev.stage} · ${ev.table_codes.length} 张表`
  if (ev.status) return `状态 → ${ev.status}`
  if (ev.tool) return `调用 ${ev.tool}`
  return ev.node ? `节点 ${ev.node}` : '生命周期信号'
}

function formatTs(value) {
  if (!value) return ''
  if (typeof value === 'number') {
    return new Date(value * 1000).toLocaleTimeString('zh-CN', { hour12: false })
  }
  return formatTime(value)
}

export default function RunTraceTimeline({ items = [], mode = 'ledger', emptyText = '暂无记录。' }) {
  const [rawMode, setRawMode] = useState(false)
  const [expandedSeqs, setExpandedSeqs] = useState({})

  const recent = items.slice(-40).reverse()

  if (!items.length) {
    return <div className="empty">{emptyText}</div>
  }

  function toggleRow(key) {
    setExpandedSeqs((prev) => ({ ...prev, [key]: !prev[key] }))
  }

  return (
    <div>
      <div className="trace-toolbar">
        <small className="muted-text">
          按时间倒序展示最近 {recent.length} / {items.length} 条记录，点击单行可展开原始载荷
        </small>
        <button type="button" className="button ghost sm" onClick={() => setRawMode((v) => !v)}>
          {rawMode ? '切回时间线视图' : '查看原始 JSON'}
        </button>
      </div>

      {rawMode ? (
        <pre className="run-json">{JSON.stringify(items.slice(-30), null, 2)}</pre>
      ) : (
        <ul className="trace-timeline">
          {recent.map((item, idx) => {
            const key = `${mode}-${item.seq ?? idx}`
            const kind = mode === 'ledger' ? item.kind : item.topic
            const summary = mode === 'ledger' ? summarizeLedgerEvent(item) : summarizeBusEvent(item)
            const timeText = mode === 'ledger' ? formatTs(item.created_at) : formatTs(item.timestamp)
            const open = Boolean(expandedSeqs[key])
            return (
              <li className="trace-item" key={key}>
                <button
                  type="button"
                  className="trace-item-head"
                  aria-expanded={open}
                  onClick={() => toggleRow(key)}
                >
                  <span className="trace-seq">#{item.seq ?? idx + 1}</span>
                  <StatusBadge tone={toneForKind(kind)}>{kind}</StatusBadge>
                  {item.node && <span className="trace-node">{item.node}</span>}
                  <span className="trace-summary" title={summary}>{summary}</span>
                  {timeText && <span className="trace-time">{timeText}</span>}
                </button>
                {open && (
                  <pre className="trace-payload">{JSON.stringify(item, null, 2)}</pre>
                )}
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}
