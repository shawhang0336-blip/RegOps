import { useEffect, useState } from 'react'
import { v2 } from '../../api.js'
import CopyButton from '../../components/CopyButton.jsx'
import { StatusBadge } from '../SharedUI.jsx'
import { formatValue, ruleStatusLabel } from './artifactHelpers.js'

function RuleEditForm({ rule, onCancel, onSaved }) {
  const [params, setParams] = useState({ ...(rule.params || {}) })
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  function set(key, value) {
    setParams({ ...params, [key]: value })
  }

  async function save() {
    setBusy(true)
    setError('')
    try {
      const payload = { ...params }
      if (Array.isArray(payload.values)) {
        payload.values = payload.values.map((item) => String(item).trim()).filter(Boolean)
      }
      if (Array.isArray(payload.if_values)) {
        payload.if_values = payload.if_values.map((item) => String(item).trim()).filter(Boolean)
      }
      await v2(`/artifacts/rules/${rule.row_id}`, {
        method: 'PATCH',
        body: JSON.stringify({ params: payload, reason }),
      })
      onSaved()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  const operator = rule.operator
  return (
    <div className="rule-edit-form">
      <h4>编辑参数 · {rule.field}（{rule.operator_label}）</h4>

      {operator === 'REQUIRED' && <p className="muted-text">必填规则没有参数，整条规则就是“去掉空白后必须有值”。</p>}

      {(operator === 'LENGTH' || operator === 'LENGTH_MAX') && (
        <label>
          <span>{operator === 'LENGTH' ? '定长' : '最大长度'}（字符数）</span>
          <input type="number" min="1" value={params.length ?? ''} onChange={(e) => set('length', Number(e.target.value))} />
        </label>
      )}

      {(operator === 'ENUM' || operator === 'NOT_IN') && (
        <label>
          <span>允许取值（每行一个）</span>
          <textarea rows={Math.min(8, (params.values || []).length + 1)} value={(params.values || []).join('\n')} onChange={(e) => set('values', e.target.value.split('\n'))} />
        </label>
      )}

      {operator === 'REGEXP' && (
        <label>
          <span>正则（Oracle REGEXP_LIKE 语义）</span>
          <input value={params.pattern ?? ''} onChange={(e) => set('pattern', e.target.value)} placeholder="^\d{8}$" />
        </label>
      )}

      {operator === 'NUM_RANGE' && (
        <>
          <label>
            <span>下限（留空表示不限）</span>
            <input type="number" value={params.min ?? ''} onChange={(e) => set('min', e.target.value === '' ? null : Number(e.target.value))} />
          </label>
          <label>
            <span>上限（留空表示不限）</span>
            <input type="number" value={params.max ?? ''} onChange={(e) => set('max', e.target.value === '' ? null : Number(e.target.value))} />
          </label>
          <label>
            <span>边界</span>
            <select
              value={`${params.min_inclusive === false ? 'ex' : 'in'}-${params.max_inclusive === false ? 'ex' : 'in'}`}
              onChange={(e) => {
                const [minSide, maxSide] = e.target.value.split('-')
                setParams({ ...params, min_inclusive: minSide === 'in', max_inclusive: maxSide === 'in' })
              }}
            >
              <option value="in-in">含下限也含上限</option>
              <option value="in-ex">含下限、不含上限</option>
              <option value="ex-in">不含下限、含上限</option>
              <option value="ex-ex">上下限都不含</option>
            </select>
          </label>
        </>
      )}

      {operator === 'FIELD_COMPARE' && (
        <>
          <label><span>对比字段（右值）</span><input value={params.right_field ?? ''} onChange={(e) => set('right_field', e.target.value)} placeholder="例：起息日字段名" /></label>
          <label>
            <span>必须成立的关系</span>
            <select value={params.operator ?? 'GE'} onChange={(e) => set('operator', e.target.value)}>
              <option value="LT">小于（&lt;）</option>
              <option value="LE">小于等于（≤）</option>
              <option value="GT">大于（&gt;）</option>
              <option value="GE">大于等于（≥）</option>
              <option value="EQ">等于（=）</option>
              <option value="NE">不等于（≠）</option>
            </select>
          </label>
        </>
      )}

      {operator === 'COND_REQUIRED' && (
        <>
          <label><span>条件字段</span><input value={params.if_field ?? ''} onChange={(e) => set('if_field', e.target.value)} /></label>
          <label>
            <span>条件</span>
            <select value={params.if_operator ?? 'EQ'} onChange={(e) => set('if_operator', e.target.value)}>
              <option value="EQ">等于（EQ）</option>
              <option value="NE">不等于（NE）</option>
            </select>
          </label>
          <label>
            <span>条件取值（每行一个）</span>
            <textarea rows={Math.min(6, (params.if_values || []).length + 1)} value={(params.if_values || []).join('\n')} onChange={(e) => set('if_values', e.target.value.split('\n'))} />
          </label>
        </>
      )}

      {operator === 'FOREIGN_KEY' && (
        <>
          <label><span>引用表</span><input value={params.references_table ?? ''} onChange={(e) => set('references_table', e.target.value)} /></label>
          <label><span>引用字段</span><input value={params.references_field ?? ''} onChange={(e) => set('references_field', e.target.value)} /></label>
        </>
      )}

      {operator !== 'REQUIRED' && (
        <label>
          <span>修改原因（可选）</span>
          <input placeholder="例：原文限 28，业务实际允许 32" value={reason} onChange={(e) => setReason(e.target.value)} />
        </label>
      )}

      {error && <p className="danger-text">{error}</p>}
      <div className="actions">
        <button type="button" className="button primary sm" disabled={busy || operator === 'REQUIRED'} onClick={save}>{busy ? '保存中...' : '保存参数'}</button>
        <button type="button" className="button secondary sm" onClick={onCancel}>取消</button>
      </div>
      <p className="muted-text">保存后不会自动重跑验证，请用页面上的「重新验证（保留规则）」确认改动；“重新生成并验证”会让 AI 重新绑定规则并覆盖这里的修改。</p>
    </div>
  )
}

export default function RuleDrawer({ rule, index, total, onPrevious, onNext, onClose, onSaved, onNotice }) {
  const [editing, setEditing] = useState(false)

  useEffect(() => {
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    const onKeyDown = (event) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => {
      document.body.style.overflow = previousOverflow
      window.removeEventListener('keydown', onKeyDown)
    }
  }, [onClose])

  const evidenceLocation = rule.evidence ? [
    rule.evidence.section,
    rule.evidence.page ? `第 ${rule.evidence.page} 页` : null,
    rule.evidence.field ? `字段 ${rule.evidence.field}` : null,
  ].filter(Boolean).join(' · ') : ''

  return (
    <div className="rule-drawer-backdrop" onClick={onClose}>
      <aside className="rule-drawer" role="dialog" aria-modal="true" aria-labelledby="rule-drawer-title" onClick={(event) => event.stopPropagation()}>
        <header className="rule-drawer-header">
          <div>
            <span className="rule-drawer-kicker">
              规则详情{Number.isInteger(index) && total ? ` · 第 ${index + 1} / ${total} 条` : ''}
              {rule._tableName ? ` · ${rule._tableName}` : ''}
            </span>
            <h2 id="rule-drawer-title">{rule.field} · {rule.operator_label}</h2>
            <code>{rule.rule_key}</code>
          </div>
          <button type="button" className="button secondary sm" autoFocus onClick={onClose}>关闭</button>
        </header>

        <div className="rule-drawer-navigation">
          {onPrevious && <button type="button" className="button secondary sm" onClick={onPrevious}>上一条</button>}
          {onNext && <button type="button" className="button secondary sm" onClick={onNext}>下一条</button>}
          <button type="button" className="button primary sm" onClick={() => setEditing(!editing)}>{editing ? '返回详情' : '编辑参数'}</button>
        </div>

        <div className="rule-drawer-body">
          <div className="rule-drawer-badges">
            <StatusBadge tone="blue">{rule.operator_label}</StatusBadge>
            <StatusBadge tone={rule.source === 'DOC_EXPLICIT' ? 'green' : 'violet'}>{rule.source === 'DOC_EXPLICIT' ? '原文明确' : 'AI 建议'}</StatusBadge>
            <StatusBadge tone={rule.status === 'APPROVED' ? 'green' : rule.status === 'PENDING' ? 'amber' : 'neutral'}>{ruleStatusLabel(rule.status)}</StatusBadge>
          </div>

          <section className="rule-drawer-section">
            <h3>规则说明</h3>
            <p className="rule-rationale">{rule.rationale || '暂无规则说明。'}</p>
          </section>

          <section className="rule-drawer-section">
            <h3>参数</h3>
            {Object.keys(rule.params || {}).length ? (
              <dl className="rule-parameter-list">
                {Object.entries(rule.params).map(([key, value]) => (
                  <div key={key}><dt>{key}</dt><dd>{formatValue(value)}</dd></div>
                ))}
              </dl>
            ) : <p className="muted-text">这条规则没有可配置参数。</p>}
          </section>

          {rule.sql && (
            <section className="rule-drawer-section">
              <div className="section-heading-actions">
                <h3>编译后的校验条件</h3>
                <CopyButton text={rule.sql} label="复制 SQL" />
              </div>
              <pre className="rule-sql">{rule.sql}</pre>
            </section>
          )}

          {rule.evidence && (
            <section className="rule-drawer-section">
              <h3>原文依据</h3>
              <blockquote className="rule-evidence">
                {evidenceLocation && <span>{evidenceLocation}</span>}
                {rule.evidence.quote || '原文没有提供可展示的引用内容。'}
              </blockquote>
            </section>
          )}

          {editing && (
            <RuleEditForm
              rule={rule}
              onCancel={() => setEditing(false)}
              onSaved={() => {
                setEditing(false)
                onNotice?.(`已保存「${rule.rule_key}」的参数。点页面右上「重新验证（保留规则）」用新参数重跑测试，规则不会被重新生成。`)
                onSaved?.()
              }}
            />
          )}
        </div>
      </aside>
    </div>
  )
}
