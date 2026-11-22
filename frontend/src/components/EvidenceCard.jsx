import { FIELD_LABELS } from '../utils/labels.js'
import { StatusBadge } from './StatusBadge.jsx'

export function SourceLocatorTag({ locator }) {
  if (!locator || typeof locator !== 'object') return <span className="muted-text">—</span>
  const tags = []
  if (locator.section) tags.push(`章节 ${locator.section}`)
  if (locator.sheet) tags.push(`工作表 ${locator.sheet}`)
  if (locator.page) tags.push(`第 ${locator.page} 页`)
  if (locator.table_index !== undefined) tags.push(`表 #${Number(locator.table_index) + 1}`)
  if (locator.file) tags.push(`导入文件: ${locator.file}`)
  if (locator.editor) tags.push(`维护人: ${locator.editor}`)
  if (locator.source) tags.push(locator.source === 'HUMAN' ? '人工录入' : locator.source)
  if (locator.quote) tags.push(`原文: "${String(locator.quote).slice(0, 36)}..."`)

  if (!tags.length) {
    return <code className="code-pill">{JSON.stringify(locator)}</code>
  }
  return (
    <div className="locator-tags">
      {tags.map((item, idx) => (
        <span key={idx} className="locator-pill">{item}</span>
      ))}
    </div>
  )
}

export function EvidenceCard({ field }) {
  const ai = field?.ai_value_json || {}
  const ev = field?.evidence_json || {}
  const compareKeys = ['field_cn', 'field_en', 'data_type', 'length', 'decimal_places', 'required', 'code_reference_text']
  const diffs = compareKeys.filter((key) => ai[key] !== undefined && String(ai[key]) !== String(field[key]))

  return (
    <div className="evidence-card">
      <div className="evidence-card-header">
        <span className="evidence-title">原文溯源与 AI 初值对比</span>
        {field.confidence !== undefined && (
          <StatusBadge tone={field.confidence >= 0.9 ? 'green' : 'amber'}>
            置信度 {Math.round(Number(field.confidence || 0) * 100)}%
          </StatusBadge>
        )}
      </div>
      <div className="evidence-row">
        <span className="evidence-label">证据位置</span>
        <SourceLocatorTag locator={ev} />
      </div>
      {ev.quote && (
        <blockquote className="evidence-quote">{ev.quote}</blockquote>
      )}
      {diffs.length > 0 ? (
        <div className="evidence-diff-box">
          <div className="evidence-label">人工已修正属性（对比 AI 初值）：</div>
          <div className="mini-diff-list">
            {diffs.map((key) => (
              <div key={key} className="mini-diff-item">
                <span>{FIELD_LABELS[key] || key}：</span>
                <del>{String(ai[key] ?? '空')}</del>
                <span className="diff-arrow">→</span>
                <ins>{String(field[key] ?? '空')}</ins>
              </div>
            ))}
          </div>
        </div>
      ) : (
        <p className="evidence-consistent">当前字段属性值与 AI 初始提取结果完全一致，未做人工偏移。</p>
      )}
    </div>
  )
}

export default EvidenceCard
