import { IMPORT_STAGES, formatDuration } from './documentHelpers.js'

export default function ImportProgressPanel({ progress, onClose, onCancel, cancelling = false }) {
  if (!progress) return null

  const stages = progress.stages?.length
    ? progress.stages
    : IMPORT_STAGES.map(([label, description], index) => ({ index: index + 1, label, description }))

  const stage = Number(progress.stage || 0)
  const counters = progress.counters || {}
  const budget = progress.budget_usage || null
  const status = String(progress.status || 'WORKING').toUpperCase()
  const working = status === 'WORKING'
  const failed = status === 'FAILED'

  const blocksTotal = Number(counters.blocks_total || counters.blocks || 0)
  const blocksDone = Number(counters.blocks_done || 0)
  const ratio = blocksTotal > 0 ? Math.min(1, blocksDone / blocksTotal) : 0

  const base = Math.max(0, stage - 1) * 25
  const inStage = stage === 4 && blocksTotal > 0 ? ratio * 25 : working ? 8 : (status === 'READY' ? 25 : 0)
  const percent = Math.min(100, Math.round(status === 'DONE' ? 100 : base + inStage))

  const headline = status === 'DONE'
    ? '解析完成'
    : failed
      ? '解析已中断'
      : status === 'READY'
        ? '结构已就绪，等待解析'
        : working
          ? `正在${stages[Math.min(stage, stages.length) - 1]?.label || '处理'}`
          : '准备中'

  return (
    <section className={`import-progress-panel ${status.toLowerCase()}`} aria-live="polite">
      <header className="import-progress-head">
        <div className="import-progress-title">
          <strong>{headline}</strong>
          <span className="import-progress-file">{progress.file_name || progress.fileName || ''}</span>
        </div>
        <div className="import-progress-meta">
          {progress.elapsed > 0 && <span className="import-timer">{formatDuration(progress.elapsed)}</span>}
          {working && onCancel && (
            <button
              type="button"
              className="button danger secondary sm"
              disabled={cancelling}
              onClick={onCancel}
            >
              {cancelling ? '正在中止...' : '中止解析'}
            </button>
          )}
          {!working && <button type="button" className="button-link" onClick={onClose}>关闭</button>}
        </div>
      </header>

      <div className="import-progress-bar" role="progressbar" aria-valuenow={percent} aria-valuemin={0} aria-valuemax={100}>
        <div className={`import-progress-fill ${working ? 'striped' : ''}`} style={{ width: `${percent}%` }} />
      </div>

      <div className="import-progress-summary">
        <span>{progress.detail || '—'}</span>
        <div className="import-progress-counters">
          {counters.blocks ? <span>{counters.blocks} 个内容块</span> : null}
          {counters.tables ? <span>{counters.tables} 张报表</span> : null}
          {counters.fields ? <span>{counters.fields} 个字段</span> : null}
          {counters.code_sets ? <span>{counters.code_sets} 张码表</span> : null}
          {budget && budget.llm_calls > 0 ? (
            <span>
              AI {budget.llm_calls} 次 · {(budget.total_tokens || 0).toLocaleString()} tok
            </span>
          ) : null}
        </div>
      </div>

      <ol className="import-timeline">
        {stages.map((item) => {
          const index = item.index
          const state = status === 'DONE' || index < stage
            ? 'done'
            : index === stage
              ? (working ? 'working' : failed ? 'failed' : 'done')
              : 'pending'
          const stageLogs = (progress.logs || []).filter((entry) => Number(entry.stage) === index)
          return (
            <li className={`import-timeline-item ${state}`} key={item.index}>
              <span className="import-timeline-dot" aria-hidden="true">
                {state === 'done' ? '✓' : state === 'failed' ? '!' : ''}
              </span>
              <div className="import-timeline-body">
                <div className="import-timeline-row">
                  <strong>{item.label}</strong>
                  <em>{state === 'done' ? '完成' : state === 'working' ? '进行中' : state === 'failed' ? '中断' : '等待'}</em>
                </div>
                <small>{item.description}</small>
                {index === 4 && blocksTotal > 0 && (
                  <div className="import-timeline-detail">
                    <span>已处理 {blocksDone}/{blocksTotal} 个内容块</span>
                    <div className="import-mini-bar"><div style={{ width: `${Math.round(ratio * 100)}%` }} /></div>
                  </div>
                )}
                {stageLogs.length > 0 && (
                  <ul className="import-log">
                    {stageLogs.slice(-4).map((entry, logIndex) => (
                      <li key={logIndex}>{entry.text}</li>
                    ))}
                  </ul>
                )}
              </div>
            </li>
          )
        })}
      </ol>

      {progress.logs?.length > 0 && (
        <details className="import-progress-log">
          <summary>查看完整记录（{progress.logs.length} 条）</summary>
          <ul>
            {progress.logs.map((entry, index) => (
              <li key={index}>{entry.text}</li>
            ))}
          </ul>
        </details>
      )}

      {failed && (
        <p className="import-progress-foot">
          已保存的内容不会丢失。处理后可以重新点击“解析当前文件”。
        </p>
      )}
    </section>
  )
}
