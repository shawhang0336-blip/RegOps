import { useState } from 'react'
import { formatTime } from '../utils/dateTime.js'
import { FIELD_LABELS, REVISION_LABELS } from '../utils/labels.js'
import { StatusBadge } from './StatusBadge.jsx'

function formatDiffValue(key, val) {
  if (val === null || val === undefined || val === '') return '（空）'
  if (key === 'required') return val ? '是（必填）' : '否（选填）'
  return String(val)
}

export function RevisionDiffView({ revision }) {
  const [showRaw, setShowRaw] = useState(false)
  const kind = revision.revision_kind
  const oldVal = revision.old_value || {}
  const newVal = revision.new_value || {}

  const renderStructuredBody = () => {
    if (kind === 'AI_EXTRACTION') {
      const draft = newVal.draft || {}
      const trace = newVal.trace || {}
      return (
        <div className="revision-summary-box">
          <div className="revision-kv-grid">
            <div><span>解析表名</span><strong>{draft.table_name || '—'} ({draft.table_code || '—'})</strong></div>
            <div><span>提取字段数</span><strong>{Array.isArray(draft.fields) ? `${draft.fields.length} 个字段` : '—'}</strong></div>
            <div><span>调用模型</span><strong>{trace.model || trace.provider || 'AI 模型'}</strong></div>
            <div><span>并发批次</span><strong>{trace.batch_count ? `${trace.batch_count} 批` : '单批'}</strong></div>
          </div>
          {draft.table_definition && <p className="revision-note">表定义：{draft.table_definition}</p>}
        </div>
      )
    }

    if (kind === 'AI_REVIEW') {
      const issues = Array.isArray(newVal.issues) ? newVal.issues : []
      return (
        <div className="revision-summary-box">
          {issues.length === 0 ? (
            <div className="validation-pass-banner">AI 独立复核全部字段通过，初稿与复核稿完全一致。</div>
          ) : (
            <div className="issue-card-list">
              {issues.map((issue, idx) => (
                <div key={idx} className={`issue-card severity-${(issue.severity || 'WARNING').toLowerCase()}`}>
                  <StatusBadge status={issue.severity || 'WARNING'} />
                  <div>
                    <strong>{issue.field_en || '整表级'} · {issue.code}</strong>
                    <p>{issue.message}</p>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )
    }

    if (kind === 'PUBLISHED' || kind === 'DRAFT_CREATED') {
      return (
        <div className="revision-summary-box">
          <p className="revision-note">
            {kind === 'PUBLISHED'
              ? `已发布版本 v${newVal.version || '—'}，可用于生成规则和数据库验证。`
              : `已创建可修改版本 v${newVal.version || '—'}。`}
          </p>
        </div>
      )
    }

    if (kind === 'HUMAN_DELETE') {
      return (
        <div className="revision-summary-box">
          <p className="revision-note danger-text">
            已删除字段：<strong>{oldVal.field_cn || ''} ({oldVal.field_en || '未知字段'})</strong>
          </p>
        </div>
      )
    }

    if (kind === 'HUMAN_ADD') {
      return (
        <div className="revision-summary-box">
          <p className="revision-note">
            新增字段：<strong>{newVal.field_cn} ({newVal.field_en})</strong> · {newVal.data_type}({newVal.length}) · {newVal.required ? '必填' : '选填'}
          </p>
        </div>
      )
    }

    // Default: HUMAN_EDIT or field-level diff
    const keys = Object.keys(FIELD_LABELS)
    const changedKeys = keys.filter((k) => {
      if (oldVal[k] === undefined && newVal[k] === undefined) return false
      return String(oldVal[k] ?? '') !== String(newVal[k] ?? '')
    })

    if (changedKeys.length === 0) {
      return <p className="revision-note">保存了字段快照（无核心属性值变更）。</p>
    }

    return (
      <div className="diff-table-wrap">
        <table className="diff-table">
          <thead>
            <tr>
              <th>变更属性 ({newVal.field_en || oldVal.field_en || '字段'})</th>
              <th>修改前 (Old)</th>
              <th>修改后 (New)</th>
            </tr>
          </thead>
          <tbody>
            {changedKeys.map((key) => (
              <tr key={key}>
                <td className="diff-field-name">{FIELD_LABELS[key] || key}</td>
                <td className="diff-cell-old"><del>{formatDiffValue(key, oldVal[key])}</del></td>
                <td className="diff-cell-new"><ins>{formatDiffValue(key, newVal[key])}</ins></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    )
  }

  return (
    <div className="revision-card">
      <div className="revision-card-header">
        <div className="revision-meta">
          <StatusBadge
            tone={
              kind === 'PUBLISHED'
                ? 'green'
                : kind === 'AI_EXTRACTION' || kind === 'AI_REVIEW'
                  ? 'violet'
                  : 'blue'
            }
          >
            {REVISION_LABELS[kind] || kind}
          </StatusBadge>
          <strong>{revision.reason || '变更记录'}</strong>
          <span className="muted-text">{revision.editor || '系统'}</span>
        </div>
        <div className="revision-actions">
          <span className="revision-time">{formatTime(revision.created_at)}</span>
          <button
            type="button"
            className="button-link"
            onClick={() => setShowRaw(!showRaw)}
          >
            {showRaw ? '隐藏原始 JSON' : '原始报文'}
          </button>
        </div>
      </div>
      {renderStructuredBody()}
      {showRaw && (
        <div className="diff-grid raw-diff-drawer">
          <div>
            <span className="raw-label">变更前 (old_value)</span>
            <pre>{JSON.stringify(revision.old_value, null, 2)}</pre>
          </div>
          <div>
            <span className="raw-label">变更后 (new_value)</span>
            <pre>{JSON.stringify(revision.new_value, null, 2)}</pre>
          </div>
        </div>
      )}
    </div>
  )
}

export default RevisionDiffView
