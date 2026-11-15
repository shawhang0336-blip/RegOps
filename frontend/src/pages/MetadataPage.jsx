import { useCallback, useEffect, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { v2 } from '../api.js'
import PageHeader from './PageHeader.jsx'
import StatusMessage from './StatusMessage.jsx'
import {
  EvidenceCard,
  RevisionDiffView,
  StatusBadge,
  issueLabel,
  issueSeverity,
  groupValidationIssues,
  friendlyError,
  formatTime,
} from './SharedUI.jsx'

const emptyField = {
  field_cn: '',
  field_en: '',
  field_definition: '',
  check_text_raw: '',
  data_type: 'VARCHAR',
  length: 128,
  decimal_places: 0,
  required: false,
  code_reference_text: '',
  confidence: 1,
  evidence_json: { source: 'HUMAN' },
}

// 类型是原文写的，还是程序推断的，可信度差一个量级，界面上必须一眼能分开
const TYPE_BASIS_META = {
  SOURCE: { label: '原文有依据', tone: 'green' },
  INFERRED: { label: '类型是推断', tone: 'amber' },
  MANUAL: { label: '人工确认', tone: 'blue' },
  UNKNOWN: { label: '待确认', tone: 'neutral' },
}

function typeBasisMeta(basis) {
  return TYPE_BASIS_META[String(basis || 'UNKNOWN').toUpperCase()] || TYPE_BASIS_META.UNKNOWN
}

export default function MetadataPage({ embedded = false, onOpenCodeSets }) {
  const navigate = useNavigate()
  const [params] = useSearchParams()

  const [tables, setTables] = useState([])
  const [detail, setDetail] = useState(null)
  const [baselineFields, setBaselineFields] = useState({})
  const [unresolvedRefs, setUnresolvedRefs] = useState([])
  const [codeSets, setCodeSets] = useState([])
  const [editingFieldId, setEditingFieldId] = useState(null)
  const [editReason, setEditReason] = useState('人工复核修正元数据')
  const [newField, setNewField] = useState(emptyField)
  const [showAdd, setShowAdd] = useState(false)
  const [tab, setTab] = useState(params.get('tab') || 'fields')
  const [fieldSearch, setFieldSearch] = useState('')
  const [onlyIssues, setOnlyIssues] = useState(false)
  const [onlyInferred, setOnlyInferred] = useState(false)
  const [showTypeImport, setShowTypeImport] = useState(false)
  const [typeImportFile, setTypeImportFile] = useState(null)
  const [typeImportResult, setTypeImportResult] = useState(null)
  const [readiness, setReadiness] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  async function load(preferredId) {
    const documentId = params.get('document_id')
    const scopeQuery = documentId ? `?document_id=${encodeURIComponent(documentId)}` : ''
    if (embedded && !documentId) {
      setTables([])
      setDetail(null)
      return
    }
    const [tableList, unresolvedList, codeSetList, readinessPayload] = await Promise.all([
      v2(`/metadata/tables${scopeQuery}`),
      v2(`/code-sets/references/unresolved${scopeQuery}`).catch(() => []),
      v2(`/code-sets${scopeQuery}`).catch(() => []),
      v2(`/metadata/readiness${scopeQuery}`).catch(() => null),
    ])
    setTables(tableList)
    setUnresolvedRefs(unresolvedList)
    setCodeSets(codeSetList)
    setReadiness(readinessPayload)

    const requestedId = params.get('table')
    const currentId = detail?.id
    const targetId = preferredId || (tableList.some((table) => table.id === requestedId) ? requestedId : null) || (tableList.some((table) => table.id === currentId) ? currentId : null) || tableList[0]?.id
    if (targetId) {
      await select(targetId, false)
    }
  }

  useEffect(() => {
    setDetail(null)
    load().catch((err) => setError(err.message))
  }, [params.get('document_id')])

  async function select(id, resetTab = false) {
    const data = await v2(`/metadata/tables/${id}`)
    setDetail(data)
    setBaselineFields(Object.fromEntries((data.fields || []).map((field) => [field.id, { ...field }])))
    setEditingFieldId(null)
    if (resetTab) setTab('fields')
  }

  async function removeTable(table) {
    const published = Boolean(table.published_version)
    const warning = published
      ? `\n\n注意：这张表已定稿（v${table.published_version}），删掉后之前生成过的运行仍能靠快照复现，但表本身会从列表里消失。`
      : ''
    if (
      !window.confirm(
        `删除报表「${table.table_name}」（${table.table_code}）？\n\n会连带删掉 ${table.field_count} 个字段和字段上的码值引用，不可恢复。${warning}`
      )
    ) {
      return
    }
    setBusy(true)
    setError('')
    try {
      await v2(`/metadata/tables/${table.id}`, {
        method: 'DELETE',
        body: JSON.stringify({ expected_version: table.current_version }),
      })
      setNotice(`已删除「${table.table_code}」。`)
      if (detail?.id === table.id) setDetail(null)
      await load()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  function changeFieldProp(id, key, value) {
    setDetail({
      ...detail,
      fields: detail.fields.map((field) => (field.id === id ? { ...field, [key]: value } : field)),
    })
  }

  const saveField = useCallback(
    async (field) => {
      if (!field || !detail) return
      // 只提交真正改过的项：把没动的类型一起提交会让后端把"原文声明"错标成人工口径
      const base = baselineFields[field.id] || {}
      const candidates = {
        field_cn: field.field_cn,
        field_en: field.field_en,
        field_definition: field.field_definition,
        check_text_raw: field.check_text_raw,
        data_type: field.data_type,
        length: Number(field.length),
        decimal_places: Number(field.decimal_places),
        required: Boolean(field.required),
        code_reference_text: field.code_reference_text,
        type_basis: field.type_basis,
      }
      const changes = {}
      Object.entries(candidates).forEach(([key, value]) => {
        const before = key === 'required' ? Boolean(base[key]) : base[key]
        if (value !== before && !(key === 'type_basis' && !value)) {
          changes[key] = value
        }
      })
      if (Object.keys(changes).length === 0) {
        setNotice('字段内容没有变化，无需保存。')
        return
      }
      setBusy(true)
      setError('')
      try {
        const body = await v2(`/metadata/tables/${detail.id}/fields/${field.id}`, {
          method: 'PATCH',
          body: JSON.stringify({
            expected_version: detail.current_version,
            reason: editReason || '人工复核元数据',
            changes,
          }),
        })
        setDetail(body)
        setBaselineFields(Object.fromEntries((body.fields || []).map((item) => [item.id, { ...item }])))
        await load(body.id)
        setNotice(`字段「${field.field_cn} (${field.field_en})」已保存，当前表升级为草稿 v${body.current_version}。`)
      } catch (err) {
        setError(err.message)
      } finally {
        setBusy(false)
      }
    },
    [detail, editReason, baselineFields]
  )

  const isPublished = detail?.status === 'PUBLISHED'
  const activeField = detail?.fields?.find((f) => f.id === editingFieldId) || null
  const filteredFields = (detail?.fields || []).filter((f) => {
    if (!fieldSearch.trim()) return true
    const q = fieldSearch.toLowerCase()
    return (
      (f.field_cn || '').toLowerCase().includes(q) ||
      (f.field_en || '').toLowerCase().includes(q) ||
      (f.field_definition || '').toLowerCase().includes(q) ||
      (f.code_reference_text || '').toLowerCase().includes(q)
    )
  })

  // Keyboard shortcuts for field inspector drawer: Ctrl+S save, Esc close, Alt+Up/Down switch field
  useEffect(() => {
    if (!activeField || tab !== 'fields') return undefined

    function onKeyDown(e) {
      if ((e.metaKey || e.ctrlKey) && (e.key === 's' || e.key === 'S')) {
        e.preventDefault()
        if (!isPublished && !busy) {
          saveField(activeField)
        }
        return
      }
      if (e.key === 'Escape') {
        e.preventDefault()
        setEditingFieldId(null)
        return
      }
      if (e.altKey && (e.key === 'ArrowDown' || e.key === 'ArrowUp')) {
        e.preventDefault()
        const idx = filteredFields.findIndex((f) => f.id === activeField.id)
        if (idx === -1) return
        const nextIdx =
          e.key === 'ArrowDown'
            ? Math.min(filteredFields.length - 1, idx + 1)
            : Math.max(0, idx - 1)
        setEditingFieldId(filteredFields[nextIdx]?.id || activeField.id)
      }
    }

    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [activeField, tab, isPublished, busy, filteredFields, saveField])

  async function removeField(field) {
    if (!window.confirm(`确认删除字段「${field.field_cn} (${field.field_en})」吗？`)) return
    setBusy(true)
    setError('')
    try {
      const body = await v2(`/metadata/tables/${detail.id}/fields/${field.id}`, {
        method: 'DELETE',
        body: JSON.stringify({
          expected_version: detail.current_version,
          reason: '人工删除字段',
          changes: {},
        }),
      })
      setDetail(body)
      setEditingFieldId(null)
      await load(body.id)
      setNotice(`已删除字段 ${field.field_en}。`)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  async function addField() {
    if (!newField.field_cn.trim() || !newField.field_en.trim()) {
      setError('请填写新字段的中文名称和英文列名。')
      return
    }
    setBusy(true)
    setError('')
    try {
      const body = await v2(`/metadata/tables/${detail.id}/fields`, {
        method: 'POST',
        body: JSON.stringify({
          expected_version: detail.current_version,
          reason: '人工补充字段',
          field: {
            ...newField,
            length: Number(newField.length),
            decimal_places: Number(newField.decimal_places),
          },
        }),
      })
      setDetail(body)
      setShowAdd(false)
      setNewField(emptyField)
      await load(body.id)
      setNotice(`已新增字段「${newField.field_cn} (${newField.field_en})」。`)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  async function publish() {
    setBusy(true)
    setError('')
    try {
      const body = await v2(`/metadata/tables/${detail.id}/publish`, {
        method: 'POST',
        body: JSON.stringify({
          expected_version: detail.current_version,
        }),
      })
      setDetail(body)
      await load(body.id)
      setNotice(`已正式发布 ${body.table_name} (${body.table_code}) v${body.published_version}，现可前往「生成与预检」启动规则装配。`)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  async function downloadTypeTemplate() {
    const documentId = params.get('document_id')
    const query = documentId ? `?document_id=${encodeURIComponent(documentId)}` : ''
    const response = await fetch(`/api/v2/metadata/types/template${query}`)
    if (!response.ok) {
      setError('模板下载失败，请稍后重试。')
      return
    }
    const blob = await response.blob()
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = '字段类型修正模板.csv'
    document.body.appendChild(link)
    link.click()
    document.body.removeChild(link)
    URL.revokeObjectURL(url)
  }

  async function submitTypeImport(dryRun) {
    if (!typeImportFile) {
      setError('请先选择要导入的文件（CSV 或 XLSX）。')
      return
    }
    setBusy(true)
    setError('')
    setTypeImportResult(null)
    try {
      const form = new FormData()
      form.append('file', typeImportFile)
      form.append('dry_run', dryRun ? 'true' : 'false')
      const documentId = params.get('document_id')
      if (documentId) form.append('document_id', documentId)
      const result = await v2('/metadata/types/import', { method: 'POST', body: form })
      setTypeImportResult(result)
      if (!dryRun) {
        setNotice(
          result.updated > 0
            ? `已更新 ${result.updated} 个字段的类型，${result.errors.length} 行未导入。`
            : '没有需要更新的字段。'
        )
        await load(detail?.id)
      }
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  async function createDraft() {
    setBusy(true)
    setError('')
    try {
      const body = await v2(`/metadata/tables/${detail.id}/draft`, {
        method: 'POST',
        body: JSON.stringify({
          expected_version: detail.current_version,
        }),
      })
      await load(body.id)
      setNotice(`已从发布基线派生可编辑草稿版本 v${body.current_version}。`)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  function getCodeRefMeta(field) {
    const text = (field.code_reference_text || '').trim()
    if (!text || ['无', 'N/A', 'NONE', '无引用'].includes(text.toUpperCase())) {
      return null
    }
    const matchedSet =
      (field.code_set_id && codeSets.find((s) => s.id === field.code_set_id)) ||
      codeSets.find((s) => s.name === text || s.code === text.toUpperCase())
    if (matchedSet) {
      return {
        text: matchedSet.name,
        label: text,
        status: (matchedSet.enabled_value_count ?? matchedSet.value_count ?? 0) > 0 ? 'RESOLVED' : 'UNRESOLVED',
        codeSetId: matchedSet.id,
      }
    }
    const unresolved = unresolvedRefs.find((r) => r.field_id === field.id)
    if (unresolved) {
      return { text, label: text, status: 'UNRESOLVED', codeSetId: unresolved.code_set_id }
    }
    return { text, label: text, status: 'DRAFT', codeSetId: null }
  }

  const detailIssues = (detail?.validations || []).flatMap((v) =>
    Array.isArray(v.issues) ? groupValidationIssues(v.issues) : []
  )
  const blockingIssues = detailIssues.filter((issue) => String(issue.severity || '').toUpperCase() === 'ERROR')
  const advisoryIssues = detailIssues.filter((issue) => {
    const severity = String(issue.severity || '').toUpperCase()
    return severity === 'WARNING' || severity === 'INFO'
  })
  const totalIssues = blockingIssues.length + advisoryIssues.length

  const detailBasis = (detail?.fields || []).reduce(
    (acc, field) => {
      const key = String(field.type_basis || 'UNKNOWN').toUpperCase()
      if (key === 'SOURCE') acc.source += 1
      else if (key === 'INFERRED') acc.inferred += 1
      else if (key === 'MANUAL') acc.manual += 1
      else acc.unknown += 1
      return acc
    },
    { source: 0, inferred: 0, manual: 0, unknown: 0 }
  )

  const visibleTables = tables
    .filter((table) => (onlyIssues ? (table.review_blocking_count || 0) > 0 : true))
    .filter((table) => (onlyInferred ? (table.type_inferred_count || 0) > 0 : true))

  const inferredTableCount = tables.filter((table) => (table.type_inferred_count || 0) > 0).length
  const allInferredTableCount = tables.filter((table) => table.type_all_inferred).length

  const readinessById = Object.fromEntries((readiness?.tables || []).map((item) => [item.table_id, item]))
  // 有码值缺陷（空码表、引用解析不到）的表——补数据要去码表页
  const codeDefectCount = (readiness?.tables || []).filter(
    (item) => item.code_sets?.applicable && (item.code_sets.defects || []).length > 0
  ).length

  const pageActions = detail && (
    <>
      {isPublished ? (
        <>
          <button type="button" className="button secondary" disabled={busy} onClick={createDraft}>新建修改版本</button>
          <button type="button" className="button primary" onClick={() => navigate(`/generation?table=${detail.id}`)}>前往生成 →</button>
        </>
      ) : (
        <>
          <button type="button" className="button secondary" onClick={() => setShowTypeImport(!showTypeImport)}>
            {showTypeImport ? '收起批量导入' : '批量导入类型修正'}
          </button>
          <button type="button" className="button secondary" onClick={() => setShowAdd(!showAdd)}>{showAdd ? '收起新增面板' : '+ 新增字段'}</button>
          <button type="button" className="button primary" disabled={busy} onClick={publish}>发布此表</button>
        </>
      )}
    </>
  )

  return (
    <>
      {!embedded && <PageHeader
        eyebrow="第 1 步 · 共 3 步"
        title="核对报表字段"
        description="检查系统识别的字段、类型和校验要求；确认无误后发布，即可生成校验规则。"
        actions={pageActions}
      />}
      {embedded && <div className="embedded-page-actions">{pageActions}</div>}

      <StatusMessage error={error} notice={notice} onClose={() => { setError(''); setNotice('') }} />

      <section className="workspace-grid metadata-layout">
        <div className="panel list-panel">
          <div className="panel-heading">
            <div>
              <h2>报表清单</h2>
              <p>
                共 {tables.length} 张表
                {onlyIssues ? `（${visibleTables.length} 张有待复核项）` : ''}
                {inferredTableCount ? ` · ${inferredTableCount} 张含推断类型` : ''}
              </p>
            </div>
            <StatusBadge tone="neutral">{tables.length}</StatusBadge>
          </div>

          <div className="list-filters">
            <label className="inline-option">
              <input type="checkbox" checked={onlyIssues} onChange={(e) => setOnlyIssues(e.target.checked)} />
              只看有待复核项的报表
            </label>
            <label className="inline-option">
              <input type="checkbox" checked={onlyInferred} onChange={(e) => setOnlyInferred(e.target.checked)} />
              只看类型需要确认的报表
              {allInferredTableCount > 0 && <span className="muted-text">（{allInferredTableCount} 张整表推断）</span>}
            </label>
          </div>


          {readiness && (
            <div className="readiness-summary">
              <div className="readiness-summary-main">
                <strong>可直接生成 {readiness.summary.ready_all} / {readiness.summary.table_count} 张</strong>
                <span className="muted-text">
                  DDL 就绪 {readiness.summary.ready.ddl} · 规则原料齐 {readiness.summary.ready.rule_materials} · 码值齐备{' '}
                  {readiness.summary.ready.code_sets}
                  {readiness.summary.not_applicable?.code_sets > 0 &&
                    `（另 ${readiness.summary.not_applicable.code_sets} 张无码值引用，不涉及）`}
                  {' · '}校验原文覆盖 {readiness.summary.check_text_coverage}%
                </span>
              </div>
              <p className="readiness-hint">
                不用在这里点发布：去第 2 步选表，能用的会直接定稿并生成，不能用会告诉你缺什么。
              </p>
              {onOpenCodeSets && codeDefectCount > 0 && (
                <p className="readiness-hint">
                  有 {codeDefectCount} 张表的码值需要补（空码表或码值引用解析不到），发布解决不了。
                  <button type="button" className="link-button" onClick={onOpenCodeSets}>
                    去补码值 →
                  </button>
                </p>
              )}
              {readiness.summary.ready.code_sets + (readiness.summary.not_applicable?.code_sets || 0) <
                readiness.summary.ready.ddl && (
                <p className="readiness-hint">
                  卡口在码值：能建表 {readiness.summary.ready.ddl} 张、码值齐备只有 {readiness.summary.ready.code_sets} 张。
                  这些是空码表或码值引用解析不到，需要回原文补码值。
                </p>
              )}
            </div>
          )}
          {visibleTables.length > 0 ? (
            <div className="list-stack">
              {visibleTables.map((table) => (
                <div
                  key={table.id}
                  role="button"
                  tabIndex={0}
                  className={`list-row ${detail?.id === table.id ? 'selected' : ''}`}
                  onClick={() => select(table.id, false)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter' || event.key === ' ') {
                      event.preventDefault()
                      select(table.id, false)
                    }
                  }}
                >
                  <button
                    type="button"
                    className="row-delete"
                    title={table.published_version ? '已定稿的表需要先确认' : '删除这张报表'}
                    onClick={(event) => {
                      event.stopPropagation()
                      removeTable(table)
                    }}
                  >
                    ×
                  </button>
                  <div className="list-row-top">
                    <strong>{table.table_name}</strong>
                    <StatusBadge status={table.status} />
                  </div>
                  <span>
                    <code>{table.table_code}</code> · {table.field_count} 个字段
                  </span>
                  <em>
                    当前 v{table.current_version}
                    {table.published_version ? ` · 已发布 v${table.published_version}` : ' · 尚未发布'}
                  </em>
                  {readinessById[table.id] && (
                    <div className="readiness-row">
                      {['ddl', 'rule_materials', 'code_sets'].map((key) => {
                        const section = readinessById[table.id][key]
                        const label = key === 'ddl' ? 'DDL' : key === 'rule_materials' ? '规则原料' : '码值'
                        // 码值只看「有没有需要人补的东西」：发布是自动的，
                        // 「待发布」不占状态位，否则用户还要为一个不用他管的动作操心
                        const defects = section.defects || []
                        const problems = key === 'code_sets' ? defects : section.blockers || []
                        // 这张表根本没有这一项（例如没有码值引用的报表）
                        const na = section.applicable === false
                        return (
                          <span
                            key={key}
                            className={`ready-chip ${na ? 'na' : problems.length ? 'bad' : 'ok'}`}
                            title={
                              na
                                ? `${label} 不适用：这张表没有引用任何码表`
                                : problems.length
                                  ? problems.map((item) => item.message).join('；')
                                  : key === 'code_sets'
                                    ? '码值齐备，不需要补充；码表会在发布时自动发出'
                                    : `${label} 可以生成`
                            }
                          >
                            {label} {na ? '—' : problems.length ? `✕${problems.length}` : '✓'}
                          </span>
                        )
                      })}
                    </div>
                  )}
                  <div className="type-basis-line">
                    <span className="basis-pill basis-source">{table.type_source_count || 0} 原文有依据</span>
                    <span className={`basis-pill basis-inferred ${table.type_inferred_count ? 'warn' : ''}`}>
                      {table.type_inferred_count || 0} 推断
                    </span>
                    {(table.type_manual_count || 0) > 0 && (
                      <span className="basis-pill basis-manual">{table.type_manual_count} 人工</span>
                    )}
                    {table.type_all_inferred && <span className="basis-pill basis-inferred warn">整表类型均无原文依据</span>}
                  </div>
                  {(table.review_blocking_count || 0) > 0 ? (
                    <em className="danger-text">
                      {table.review_blocking_count} 项需要处理
                      {(table.review_field_count || 0) > 0 ? ` · 涉及 ${table.review_field_count} 个字段` : ''}
                      {(table.review_advisory_count || 0) > 0 ? `（另有 ${table.review_advisory_count} 条提示）` : ''}
                    </em>
                  ) : (table.review_advisory_count || 0) > 0 ? (
                    <em className="muted-text">{table.review_advisory_count} 条提示，不影响使用</em>
                  ) : null}
                </div>
              ))}
            </div>
          ) : (
            <div className="empty">
              {onlyIssues
                ? '当前没有带待复核项的报表。'
                : onlyInferred
                  ? '当前没有带推断类型的报表。'
                  : '当前文件还没有字段结果，可点击上方“解析当前文件”；新文件上传后会自动解析。'}
            </div>
          )}
        </div>

        <div className="panel metadata-detail">
          {detail ? (
            <>
              <div className="panel-heading">
                <div>
                  <div className="actions">
                    <h2>{detail.table_name}</h2>
                    <code>{detail.table_code}</code>
                    <StatusBadge status={detail.status} />
                  </div>
                  <p>
                    {detail.table_definition || '暂无表级业务说明'} · 当前版本 v{detail.current_version}
                    {detail.published_version ? `（已发布版本 v${detail.published_version}）` : ''}
                  </p>
                  <div className="type-basis-line">
                    <span className="basis-pill basis-source">{detailBasis.source} 个字段类型有原文依据</span>
                    <span className={`basis-pill basis-inferred ${detailBasis.inferred ? 'warn' : ''}`}>
                      {detailBasis.inferred} 个类型是推断
                    </span>
                    {(detailBasis.manual > 0 || detailBasis.unknown > 0) && (
                      <span className="basis-pill basis-manual">{detailBasis.manual + detailBasis.unknown} 个人工确认</span>
                    )}
                  </div>
                </div>
                <div className="actions">
                  <input
                    type="search"
                    placeholder="检索字段中英文名/定义..."
                    value={fieldSearch}
                    onChange={(e) => setFieldSearch(e.target.value)}
                  />
                </div>
              </div>

              {showTypeImport && !isPublished && (
                <div className="type-import-panel">
                  <div className="type-import-head">
                    <div>
                      <strong>批量修正字段类型</strong>
                      <p className="muted-text">
                        表格按「报表代码 / 字段英文名 / 目标类型 / 目标长度 / 目标小数位」填写，只写要改的行；导入后这些字段标为「人工确认」。
                      </p>
                    </div>
                    <div className="actions">
                      <button type="button" className="button secondary sm" onClick={downloadTypeTemplate}>
                        下载模板（含当前字段）
                      </button>
                      <label className="button secondary sm file-button">
                        选择文件
                        <input
                          type="file"
                          accept=".csv,.xlsx"
                          onChange={(e) => {
                            setTypeImportFile(e.target.files?.[0] || null)
                            setTypeImportResult(null)
                          }}
                        />
                      </label>
                      <span className="muted-text">{typeImportFile ? typeImportFile.name : '未选择文件'}</span>
                    </div>
                  </div>
                  <div className="actions" style={{ marginTop: 10 }}>
                    <button type="button" className="button secondary sm" disabled={busy || !typeImportFile} onClick={() => submitTypeImport(true)}>
                      先预览（不改数据）
                    </button>
                    <button type="button" className="button primary sm" disabled={busy || !typeImportFile} onClick={() => submitTypeImport(false)}>
                      确认导入
                    </button>
                  </div>
                  {typeImportResult && (
                    <div className="type-import-result">
                      <div className="actions">
                        <StatusBadge tone={typeImportResult.updated > 0 ? 'green' : 'neutral'}>
                          {typeImportResult.dry_run ? '预览结果' : '已导入'} · {typeImportResult.updated} 行
                        </StatusBadge>
                        <span className="muted-text">
                          无变化 {typeImportResult.unchanged} 行 · 未导入 {typeImportResult.errors.length} 行
                        </span>
                      </div>
                      {(typeImportResult.details || []).length > 0 && (
                        <ul className="type-import-list">
                          {typeImportResult.details.slice(0, 12).map((item) => (
                            <li key={`${item.line}-${item.field_en}`}>
                              <code>{item.table_code}.{item.field_en}</code>
                              <span>
                                {Object.entries(item.changes)
                                  .map(([key, value]) => `${key}=${value}`)
                                  .join('，')}
                              </span>
                            </li>
                          ))}
                          {typeImportResult.details.length > 12 && (
                            <li className="muted-text">另有 {typeImportResult.details.length - 12} 行…</li>
                          )}
                        </ul>
                      )}
                      {(typeImportResult.errors || []).length > 0 && (
                        <ul className="type-import-list danger-text">
                          {typeImportResult.errors.slice(0, 12).map((item) => (
                            <li key={`err-${item.line}`}>
                              第 {item.line} 行：{item.message}
                            </li>
                          ))}
                        </ul>
                      )}
                    </div>
                  )}
                </div>
              )}

              {showAdd && !isPublished && (
                <div className="add-field-form">
                  <label>
                    <span>中文名称 *</span>
                    <input
                      placeholder="例：申报号码"
                      value={newField.field_cn}
                      onChange={(e) => setNewField({ ...newField, field_cn: e.target.value })}
                    />
                  </label>
                  <label>
                    <span>英文列名 *</span>
                    <input
                      placeholder="例：RPTNO"
                      value={newField.field_en}
                      onChange={(e) => setNewField({ ...newField, field_en: e.target.value.toUpperCase() })}
                    />
                  </label>
                  <label>
                    <span>数据类型</span>
                    <select
                      value={newField.data_type}
                      onChange={(e) => setNewField({ ...newField, data_type: e.target.value })}
                    >
                      <option value="VARCHAR">VARCHAR</option>
                      <option value="NUMBER">NUMBER</option>
                      <option value="DATE">DATE</option>
                      <option value="CHAR">CHAR</option>
                    </select>
                  </label>
                  <label>
                    <span>字段长度</span>
                    <input
                      type="number"
                      min="1"
                      value={newField.length}
                      onChange={(e) => setNewField({ ...newField, length: e.target.value })}
                    />
                  </label>
                  <label>
                    <span>字段定义</span>
                    <input
                      placeholder="业务含义说明"
                      value={newField.field_definition}
                      onChange={(e) => setNewField({ ...newField, field_definition: e.target.value })}
                    />
                  </label>
                  <label>
                    <span>原始校验规则</span>
                    <input
                      placeholder="例：必填，22位定长字符"
                      value={newField.check_text_raw}
                      onChange={(e) => setNewField({ ...newField, check_text_raw: e.target.value })}
                    />
                  </label>
                  <label>
                    <span>码值表引用</span>
                    <input
                      placeholder="无引用可留空"
                      value={newField.code_reference_text}
                      onChange={(e) => setNewField({ ...newField, code_reference_text: e.target.value })}
                    />
                  </label>
                  <div className="actions" style={{ alignSelf: 'end' }}>
                    <label className="check-label" style={{ paddingTop: 0 }}>
                      <input
                        type="checkbox"
                        checked={newField.required}
                        onChange={(e) => setNewField({ ...newField, required: e.target.checked })}
                      />
                      必填
                    </label>
                    <button type="button" className="button primary" disabled={busy} onClick={addField}>
                      确认添加
                    </button>
                  </div>
                </div>
              )}

              {totalIssues > 0 && tab === 'fields' && (
                <div className="review-guide">
                  <span>
                    {blockingIssues.length > 0
                      ? `这张表有 ${blockingIssues.length} 项需要处理${advisoryIssues.length ? `，另有 ${advisoryIssues.length} 条提示` : ''}。`
                      : `这张表有 ${advisoryIssues.length} 条提示，没有阻断项，可以直接发布。`}
                  </span>
                  <button type="button" className="button secondary sm" onClick={() => setTab('validation')}>查看核对明细</button>
                </div>
              )}

              <div className="tab-bar">
                <button
                  type="button"
                  className={tab === 'fields' ? 'active' : ''}
                  onClick={() => setTab('fields')}
                >
                  字段清单 ({detail.fields.length})
                </button>
                <button
                  type="button"
                  className={tab === 'validation' ? 'active' : ''}
                  onClick={() => setTab('validation')}
                >
                  待处理问题
                  <StatusBadge tone={blockingIssues.length > 0 ? 'red' : totalIssues > 0 ? 'amber' : 'green'}>
                    {blockingIssues.length > 0
                      ? `${blockingIssues.length} 待处理`
                      : totalIssues > 0
                        ? `${totalIssues} 提示`
                        : '通过'}
                  </StatusBadge>
                </button>
                <button
                  type="button"
                  className={tab === 'revisions' ? 'active' : ''}
                  onClick={() => setTab('revisions')}
                >
                  变更记录 ({detail.revisions.length})
                </button>
              </div>

              {tab === 'fields' && (
                <>
                  {activeField && (
                    <div className="field-editor-drawer">
                      <div className="panel-heading">
                        <div>
                          <h2>
                            查看或修改字段：{activeField.field_cn} (<code>{activeField.field_en}</code>)
                          </h2>
                          <p>
                            {isPublished
                              ? '已发布内容不可直接修改；如需调整，请先点击“新建修改版本”。'
                              : '修改后请点击保存；字段原文和自动识别依据可在下方查看。'}
                          </p>
                        </div>
                        <div className="actions">
                          {!isPublished && (
                            <>
                              <input
                                placeholder="修改原因说明..."
                                value={editReason}
                                onChange={(e) => setEditReason(e.target.value)}
                              />
                              <button
                                type="button"
                                className="button danger sm"
                                disabled={busy}
                                onClick={() => removeField(activeField)}
                              >
                                删除字段
                              </button>
                              <button
                                type="button"
                                className="button primary sm"
                                disabled={busy}
                                onClick={() => saveField(activeField)}
                              >
                                保存修改
                              </button>
                            </>
                          )}
                          <button
                            type="button"
                            className="button secondary sm"
                            onClick={() => setEditingFieldId(null)}
                          >
                            收起
                          </button>
                        </div>
                      </div>

                      <div className="field-editor-grid">
                        <label>
                          <span>中文名称</span>
                          <input disabled={isPublished} value={activeField.field_cn} onChange={(e) => changeFieldProp(activeField.id, 'field_cn', e.target.value)} />
                        </label>
                        <label>
                          <span>英文列名</span>
                          <input disabled={isPublished} value={activeField.field_en} onChange={(e) => changeFieldProp(activeField.id, 'field_en', e.target.value)} />
                        </label>
                        <label>
                          <span>数据类型与长度</span>
                          <div className="actions field-type-inputs">
                            <select disabled={isPublished} value={activeField.data_type} onChange={(e) => changeFieldProp(activeField.id, 'data_type', e.target.value)}>
                              <option value="VARCHAR">文本</option>
                              <option value="NUMBER">数值</option>
                              <option value="DATE">日期</option>
                              <option value="CHAR">定长文本</option>
                            </select>
                            <input type="number" disabled={isPublished} style={{ width: 92 }} value={activeField.length} onChange={(e) => changeFieldProp(activeField.id, 'length', e.target.value)} title="字段长度" />
                            <input type="number" disabled={isPublished} style={{ width: 76 }} value={activeField.decimal_places} onChange={(e) => changeFieldProp(activeField.id, 'decimal_places', e.target.value)} title="小数位数" />
                          </div>
                        </label>
                        <label className="span-2">
                          <span>关联码值表</span>
                          <input disabled={isPublished} placeholder="无关联码值表可留空" value={activeField.code_reference_text} onChange={(e) => changeFieldProp(activeField.id, 'code_reference_text', e.target.value)} />
                        </label>
                        <label>
                          <span>类型依据</span>
                          <select
                            disabled={isPublished}
                            value={String(activeField.type_basis || 'UNKNOWN').toUpperCase()}
                            onChange={(e) => changeFieldProp(activeField.id, 'type_basis', e.target.value)}
                            title="改了类型/长度会自动记为人工确认"
                          >
                            <option value="SOURCE">原文有依据</option>
                            <option value="INFERRED">类型是推断</option>
                            <option value="MANUAL">人工确认</option>
                            <option value="UNKNOWN">待确认</option>
                          </select>
                        </label>
                        <label className="check-label">
                          <input type="checkbox" disabled={isPublished} checked={Boolean(activeField.required)} onChange={(e) => changeFieldProp(activeField.id, 'required', e.target.checked)} />
                          必填字段
                        </label>
                        <label className="span-3">
                          <span>业务定义</span>
                          <textarea disabled={isPublished} value={activeField.field_definition} onChange={(e) => changeFieldProp(activeField.id, 'field_definition', e.target.value)} />
                        </label>
                        <label className="span-3">
                          <span>规范中的校验要求</span>
                          <textarea disabled={isPublished} value={activeField.check_text_raw} onChange={(e) => changeFieldProp(activeField.id, 'check_text_raw', e.target.value)} />
                        </label>
                      </div>

                      <details className="technical-details">
                        <summary>查看自动识别初值与原文依据</summary>
                        <EvidenceCard field={activeField} />
                      </details>
                    </div>
                  )}

                  <div className="table-wrap">
                    <table>
                      <thead>
                        <tr>
                          <th>字段名称</th>
                          <th>类型 / 必填</th>
                          <th>关联码值表</th>
                          <th>业务定义与校验要求</th>
                          <th>操作</th>
                        </tr>
                      </thead>
                      <tbody>
                        {filteredFields.map((field) => {
                          const codeMeta = getCodeRefMeta(field)
                          const isEditing = editingFieldId === field.id
                          return (
                            <tr
                              key={field.id}
                              className={isEditing ? 'field-row-active' : ''}
                            >
                              <td>
                                <strong>{field.field_cn}</strong>
                                <code>{field.field_en}</code>
                              </td>
                              <td>
                                <code>
                                  {field.data_type}({field.length}
                                  {field.decimal_places ? `,${field.decimal_places}` : ''})
                                </code>
                                <StatusBadge tone={typeBasisMeta(field.type_basis).tone}>
                                  {typeBasisMeta(field.type_basis).label}
                                </StatusBadge>
                                <StatusBadge tone={field.required ? 'amber' : 'neutral'}>
                                  {field.required ? '必填' : '选填'}
                                </StatusBadge>
                              </td>
                              <td>
                                {codeMeta ? (
                                  <div className="row-actions">
                                    <span title={codeMeta.label || codeMeta.text}>{codeMeta.text}</span>
                                    <StatusBadge status={codeMeta.status} />
                                    <button
                                      type="button"
                                      className="button-link"
                                      onClick={() =>
                                        navigate(
                                          codeMeta.codeSetId
                                            ? `/code-sets?set=${codeMeta.codeSetId}`
                                            : `/code-sets?name=${encodeURIComponent(codeMeta.text)}`
                                        )
                                      }
                                    >
                                      维护码值 →
                                    </button>
                                  </div>
                                ) : (
                                  <span className="muted-text">无码值引用</span>
                                )}
                              </td>
                              <td style={{ maxWidth: 420 }}>
                                <strong>{field.field_definition || '—'}</strong>
                                <small>{field.check_text_raw || '无附加校验描述'}</small>
                              </td>
                              <td>
                                <button
                                  type="button"
                                  className={`button sm ${isEditing ? 'primary' : 'secondary'}`}
                                  onClick={() =>
                                    setEditingFieldId(isEditing ? null : field.id)
                                  }
                                >
                                  {isEditing ? '收起' : '查看 / 修改'}
                                </button>
                              </td>
                            </tr>
                          )
                        })}
                      </tbody>
                    </table>
                  </div>
                </>
              )}

              {tab === 'validation' && (
                <div className="validation-list">
                  {detail.validations.length > 0 ? (
                    detail.validations.map((item, index) => {
                      const issues = Array.isArray(item.issues) ? groupValidationIssues(item.issues) : []
                      return (
                        <div className="validation-group-card" key={index}>
                          <div className="validation-group-header">
                            <div className="actions">
                              <StatusBadge status={item.validator_kind} />
                              <strong>第 {item.round || 1} 轮验证</strong>
                              <span className="muted-text">{formatTime(item.created_at)}</span>
                            </div>
                            <StatusBadge status={item.status} />
                          </div>
                          {issues.length === 0 ? (
                            <div className="validation-pass-banner">
                              未发现任何异常或分歧，全部字段均通过该轮校验。
                            </div>
                          ) : (
                            <div className="issue-card-list">
                              {issues.map((issue, idx) => (
                                <div
                                  key={idx}
                                  className={`issue-card severity-${issueSeverity(issue).toLowerCase()}`}
                                >
                                  <StatusBadge status={issueSeverity(issue)} />
                                  <div>
                                    <strong>
                                      {issue.field_en ? `字段 ${issue.field_en}` : '整表提示'} · {issueLabel(issue.code)}
                                    </strong>
                                    <p>{friendlyError(issue.message || '请对照原文确认该字段的识别结果。')}</p>
                                    {issue.field_en && detail.fields.some((field) => field.field_en === issue.field_en) && (
                                      <button
                                        type="button"
                                        className="button-link"
                                        onClick={() => {
                                          setFieldSearch(issue.field_en)
                                          setEditingFieldId(detail.fields.find((field) => field.field_en === issue.field_en)?.id || null)
                                          setTab('fields')
                                        }}
                                      >
                                        定位并处理这个字段 →
                                      </button>
                                    )}
                                  </div>
                                </div>
                              ))}
                            </div>
                          )}
                        </div>
                      )
                    })
                  ) : (
                    <div className="empty">当前表暂无验证记录。</div>
                  )}
                </div>
              )}

              {tab === 'revisions' && (
                <div className="revision-list">
                  {detail.revisions.map((item, index) => (
                    <RevisionDiffView key={index} revision={item} />
                  ))}
                </div>
              )}
            </>
          ) : (
            <div className="empty">当前文件还没有可核对的字段。</div>
          )}
        </div>
      </section>
    </>
  )
}
