import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { api, downloadFile } from '../api.js'
import { useFeedback } from '../context/FeedbackContext.jsx'
import { DetailSkeleton, ListSkeleton } from '../components/Skeleton.jsx'
import PageHeader from './PageHeader.jsx'
import StatusMessage from './StatusMessage.jsx'
import { RevisionDiffView, StatusBadge, groupValidationIssues } from './SharedUI.jsx'
import FieldEditorDrawer from './metadata/FieldEditorDrawer.jsx'
import { AddFieldForm, TypeImportPanel, ValidationTabView } from './metadata/MetadataPanels.jsx'
import { emptyField, tableGate, typeBasisMeta } from './metadata/metadataHelpers.js'

export default function MetadataPage({ embedded = false, onOpenCodeSets, onReadinessChange, reportFilter, onReportFilterChange }) {
  const navigate = useNavigate()
  const [params] = useSearchParams()
  const { confirm } = useFeedback()

  const [tables, setTables] = useState([])
  const [selectedId, setSelectedId] = useState(null)
  const [detail, setDetail] = useState(null)
  const [loading, setLoading] = useState(true)
  const [detailLoading, setDetailLoading] = useState(false)
  const [baselineFields, setBaselineFields] = useState({})
  const requestRef = useRef(0)
  const loadRequestRef = useRef(0)
  const [unresolvedRefs, setUnresolvedRefs] = useState([])
  const [codeSets, setCodeSets] = useState([])
  const [editingFieldId, setEditingFieldId] = useState(null)
  const [highlightFieldId, setHighlightFieldId] = useState(null)
  const [editReason, setEditReason] = useState('人工复核修正元数据')
  const [newField, setNewField] = useState(emptyField)
  const [showAdd, setShowAdd] = useState(false)
  const [tab, setTab] = useState(params.get('tab') || 'fields')
  const [fieldSearch, setFieldSearch] = useState('')
  const [tableSearch, setTableSearch] = useState('')
  const [onlyIssues, setOnlyIssues] = useState(false)
  const [onlyInferred, setOnlyInferred] = useState(false)
  const [localReportFilter, setLocalReportFilter] = useState('all')
  const activeReportFilter = reportFilter ?? localReportFilter
  const changeReportFilter = onReportFilterChange || setLocalReportFilter
  const [showTypeImport, setShowTypeImport] = useState(false)
  const [typeImportFile, setTypeImportFile] = useState(null)
  const [typeImportResult, setTypeImportResult] = useState(null)
  const [readiness, setReadiness] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  async function load(preferredId) {
    const loadId = ++loadRequestRef.current
    const documentId = params.get('document_id')
    const scopeQuery = documentId ? `?document_id=${encodeURIComponent(documentId)}` : ''
    if (embedded && !documentId) {
      setTables([])
      setDetail(null)
      onReadinessChange?.(null)
      return
    }
    const [tableList, unresolvedList, codeSetList, readinessPayload] = await Promise.all([
      api(`/api/metadata/tables${scopeQuery}`),
      api(`/api/code-sets/references/unresolved${scopeQuery}`).catch(() => []),
      api(`/api/code-sets${scopeQuery}`).catch(() => []),
      api(`/api/metadata/readiness${scopeQuery}`).catch(() => null),
    ])
    if (loadId !== loadRequestRef.current) return
    setTables(tableList)
    setUnresolvedRefs(unresolvedList)
    setCodeSets(codeSetList)
    setReadiness(readinessPayload)
    onReadinessChange?.(readinessPayload)

    const requestedId = params.get('table')
    const currentId = detail?.id
    const targetId = preferredId || (tableList.some((table) => table.id === requestedId) ? requestedId : null) || (tableList.some((table) => table.id === currentId) ? currentId : null) || tableList[0]?.id
    if (targetId) {
      await select(targetId, false)
    } else {
      setDetail(null)
      setSelectedId(null)
    }
  }

  const documentIdParam = params.get('document_id')
  const tableParam = params.get('table')
  const fieldParam = params.get('field')

  useEffect(() => {
    setSelectedId(null)
    setDetail(null)
    setLoading(true)
    load()
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false))
  }, [documentIdParam])

  useEffect(() => {
    if (!tableParam || !tables.length) return
    if (!tables.some((table) => table.id === tableParam)) return
    if (detail?.id !== tableParam) {
      select(tableParam, false)
    } else if (fieldParam && (detail.fields || []).some((field) => field.id === fieldParam)) {
      setHighlightFieldId(fieldParam)
    }
  }, [tableParam, fieldParam, tables])

  useEffect(() => {
    if (!highlightFieldId) return
    const row = document.getElementById(`field-row-${highlightFieldId}`)
    if (row) row.scrollIntoView({ block: 'center', behavior: 'smooth' })
  }, [highlightFieldId, detail?.id])

  async function select(id, resetTab = false) {
    const reqId = ++requestRef.current
    setSelectedId(id)
    setDetailLoading(true)
    try {
      const data = await api(`/api/metadata/tables/${id}`)
      if (reqId !== requestRef.current) return
      setDetail(data)
      setBaselineFields(Object.fromEntries((data.fields || []).map((field) => [field.id, { ...field }])))
      setEditingFieldId(null)
      const requestedField = params.get('field')
      setHighlightFieldId(
        requestedField && (data.fields || []).some((field) => field.id === requestedField) ? requestedField : null,
      )
      if (resetTab) setTab('fields')
    } catch (err) {
      if (reqId === requestRef.current) setError(err.message)
    } finally {
      if (reqId === requestRef.current) setDetailLoading(false)
    }
  }

  // 从码值缺陷直接跳到那张码表（第一步的码值表 tab），并选中它
  function openCodeSet(codeSetId) {
    if (!codeSetId) return
    const next = new URLSearchParams()
    next.set('view', 'codes')
    const documentId = params.get('document_id')
    if (documentId) next.set('document_id', documentId)
    next.set('set', codeSetId)
    navigate(`/documents?${next.toString()}`)
  }

  async function removeTable(table) {
    const published = Boolean(table.published_version)
    const confirmed = await confirm({
      title: `删除报表「${table.table_name}」？`,
      message: `表代码 ${table.table_code}，将连带删除 ${table.field_count} 个字段和字段上的码值引用，操作不可恢复。`,
      details: published
        ? `注意：这张表已定稿（v${table.published_version}），删除后历史运行仍可依靠快照复现，但表本身会从清单移除。`
        : '',
      confirmLabel: '确认删除',
      tone: 'danger',
    })
    if (!confirmed) return
    setBusy(true)
    setError('')
    try {
      await api(`/api/metadata/tables/${table.id}`, {
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
      const parsedLength = Number(field.length)
      const parsedDecimals = field.data_type === 'NUMBER' ? Number(field.decimal_places ?? 0) : 0
      if (!String(field.field_cn || '').trim() || !String(field.field_en || '').trim()) {
        setError('字段中文名称和英文列名不能为空。')
        return
      }
      if (!Number.isInteger(parsedLength) || parsedLength <= 0) {
        setError('字段长度必须是大于 0 的整数。')
        return
      }
      if (!Number.isInteger(parsedDecimals) || parsedDecimals < 0 || (field.data_type === 'NUMBER' && parsedDecimals > parsedLength)) {
        setError('小数位数必须是非负整数，且不能大于字段总长度。')
        return
      }
      // 只提交真正改过的项：把没动的类型一起提交会让后端把"原文声明"错标成人工口径
      const base = baselineFields[field.id] || {}
      const candidates = {
        field_cn: field.field_cn,
        field_en: field.field_en,
        field_definition: field.field_definition,
        check_text_raw: field.check_text_raw,
        data_type: field.data_type,
        length: parsedLength,
        decimal_places: parsedDecimals,
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
        const body = await api(`/api/metadata/tables/${detail.id}/fields/${field.id}`, {
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

  const activeField = detail?.fields?.find((f) => f.id === editingFieldId) || null
  const isFieldDirty = useCallback(
    (field) => {
      if (!field) return false
      const base = baselineFields[field.id]
      if (!base) return false
      const compare = {
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
      return Object.entries(compare).some(([key, value]) => {
        const before = key === 'required'
          ? Boolean(base[key])
          : (key === 'length' || key === 'decimal_places')
            ? Number(base[key])
            : base[key]
        return value !== before && !(key === 'type_basis' && !value)
      })
    },
    [baselineFields],
  )
  const activeFieldDirty = isFieldDirty(activeField)

  const switchOrCloseField = useCallback(
    async (nextFieldId) => {
      if (activeField && activeField.id !== nextFieldId && isFieldDirty(activeField)) {
        const discard = await confirm({
          title: '放弃未保存的字段修改？',
          message: `字段「${activeField.field_cn} (${activeField.field_en})」有尚未保存的改动。`,
          details: '选择“放弃修改”将恢复该字段到上次保存的状态。',
          confirmLabel: '放弃修改',
          cancelLabel: '继续编辑',
          tone: 'danger',
        })
        if (!discard) return
        const base = baselineFields[activeField.id]
        if (base && detail) {
          setDetail((prev) =>
            prev
              ? { ...prev, fields: prev.fields.map((item) => (item.id === activeField.id ? { ...base } : item)) }
              : prev,
          )
        }
      }
      setEditingFieldId(nextFieldId)
    },
    [activeField, baselineFields, confirm, detail, isFieldDirty],
  )

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
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'

    function onKeyDown(e) {
      if ((e.metaKey || e.ctrlKey) && (e.key === 's' || e.key === 'S')) {
        e.preventDefault()
        if (!busy) {
          saveField(activeField)
        }
        return
      }
      if (e.key === 'Escape') {
        e.preventDefault()
        switchOrCloseField(null)
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
        switchOrCloseField(filteredFields[nextIdx]?.id || activeField.id)
      }
    }

    window.addEventListener('keydown', onKeyDown)
    return () => {
      document.body.style.overflow = previousOverflow
      window.removeEventListener('keydown', onKeyDown)
    }
  }, [activeField, tab, busy, filteredFields, saveField, switchOrCloseField])

  async function removeField(field) {
    const confirmed = await confirm({
      title: `删除字段「${field.field_cn}」？`,
      message: `确定从当前表删除字段 ${field.field_en} 吗？删除后表版本将升为草稿。`,
      confirmLabel: '确认删除',
      tone: 'danger',
    })
    if (!confirmed) return
    setBusy(true)
    setError('')
    try {
      const body = await api(`/api/metadata/tables/${detail.id}/fields/${field.id}`, {
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
    const parsedLength = Number(newField.length)
    const parsedDecimals = newField.data_type === 'NUMBER' ? Number(newField.decimal_places ?? 0) : 0
    if (!Number.isInteger(parsedLength) || parsedLength <= 0) {
      setError('字段长度必须是大于 0 的整数。')
      return
    }
    if (!Number.isInteger(parsedDecimals) || parsedDecimals < 0 || (newField.data_type === 'NUMBER' && parsedDecimals > parsedLength)) {
      setError('小数位数必须是非负整数，且不能大于字段长度。')
      return
    }
    setBusy(true)
    setError('')
    try {
      const body = await api(`/api/metadata/tables/${detail.id}/fields`, {
        method: 'POST',
        body: JSON.stringify({
          expected_version: detail.current_version,
          reason: '人工补充字段',
          field: {
            ...newField,
            length: parsedLength,
            decimal_places: parsedDecimals,
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

  async function downloadTypeTemplate() {
    setError('')
    const documentId = params.get('document_id')
    const query = documentId ? `?document_id=${encodeURIComponent(documentId)}` : ''
    try {
      await downloadFile(`/api/metadata/types/template${query}`, '字段类型修正模板.csv')
    } catch (err) {
      setError(err.message || '模板下载失败，请稍后重试。')
    }
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
      const result = await api('/api/metadata/types/import', { method: 'POST', body: form })
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

  const readinessById = Object.fromEntries((readiness?.tables || []).map((item) => [item.table_id, item]))
  const visibleTables = tables
    .filter((table) => (onlyIssues ? (table.review_blocking_count || 0) > 0 : true))
    .filter((table) => (onlyInferred ? (table.type_inferred_count || 0) > 0 : true))
    .filter((table) => activeReportFilter === 'all' || tableGate(table, readinessById[table.id]).key === activeReportFilter)
    .filter((table) => {
      const q = tableSearch.trim().toLowerCase()
      return !q || (table.table_name || '').toLowerCase().includes(q) || (table.table_code || '').toLowerCase().includes(q)
    })

  const inferredTableCount = tables.filter((table) => (table.type_inferred_count || 0) > 0).length
  const allInferredTableCount = tables.filter((table) => table.type_all_inferred).length
  const pageActions = detail && (
    <>
      {/* 没有「发布」这一步了——去第 2 步选表就会定稿。表本身随时可改。 */}
      <button type="button" className="button secondary" onClick={() => setShowTypeImport(!showTypeImport)}>
        {showTypeImport ? '收起批量导入' : '批量导入类型修正'}
      </button>
      <button type="button" className="button secondary" onClick={() => setShowAdd(!showAdd)}>{showAdd ? '收起新增面板' : '+ 新增字段'}</button>
      <button
        type="button"
        className="button secondary"
        onClick={() => {
          const qs = new URLSearchParams({ table: detail.id })
          const docId = params.get('document_id') || detail.document_id
          if (docId) qs.set('document_id', docId)
          navigate(`/generation?${qs.toString()}`)
        }}
      >
        在第二步查看
      </button>
    </>
  )

  return (
    <>
      {!embedded && <PageHeader
        eyebrow="第 1 步 · 共 3 步"
        title="核对报表字段"
        description="检查系统识别的字段、类型和校验要求；能用的去第 2 步选表即可，不能用会告诉你缺什么。"
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

          <div className="list-filters report-list-filters">
            <input type="search" placeholder="搜索报表名称或代码..." value={tableSearch} onChange={(e) => setTableSearch(e.target.value)} />
            <select aria-label="报表生成状态" value={activeReportFilter} onChange={(e) => changeReportFilter(e.target.value)}>
              <option value="all">全部报表</option>
              <option value="ready">只看可生成</option>
              <option value="blocked">只看需处理</option>
            </select>
            <label className="inline-option">
              <input type="checkbox" checked={onlyIssues} onChange={(e) => setOnlyIssues(e.target.checked)} />
              只看字段复核问题
            </label>
            <label className="inline-option">
              <input type="checkbox" checked={onlyInferred} onChange={(e) => setOnlyInferred(e.target.checked)} />
              只看类型需确认
              {allInferredTableCount > 0 && <span className="muted-text">（{allInferredTableCount} 张整表推断）</span>}
            </label>
          </div>
          {loading ? (
            <ListSkeleton rows={6} />
          ) : visibleTables.length > 0 ? (
            <div className="list-stack">
              {visibleTables.map((table) => {
                const gate = tableGate(table, readinessById[table.id])
                return (
                <div
                  key={table.id}
                  role="button"
                  tabIndex={0}
                  className={`list-row ${(selectedId || detail?.id) === table.id ? 'selected' : ''}`}
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
                    <StatusBadge tone={gate.tone}>{gate.label}</StatusBadge>
                  </div>
                  <span>
                    <code>{table.table_code}</code> · {table.field_count} 个字段
                  </span>
                  {gate.reason ? (
                    <em className="danger-text" title={gate.reason}>{gate.reason}</em>
                  ) : (table.review_advisory_count || 0) > 0 ? (
                    <em className="muted-text">{table.review_advisory_count} 条提示，不影响使用</em>
                  ) : (
                    <em className="muted-text">结构、校验信息和关联码表均可用</em>
                  )}
                  {(table.type_inferred_count || 0) > 0 && (
                    <span className="report-type-hint">{table.type_inferred_count} 个字段类型需确认{table.type_all_inferred ? ' · 整表推断' : ''}</span>
                  )}
                </div>
                )
              })}
            </div>
          ) : (
            <div className="empty">
              {tableSearch || activeReportFilter !== 'all'
                ? '没有符合当前搜索和状态条件的报表。'
                : onlyIssues
                  ? '当前没有带待复核项的报表。'
                  : onlyInferred
                    ? '当前没有带推断类型的报表。'
                    : '当前文件还没有字段结果，可点击上方“重新解析”；新文件上传后会自动解析。'}
            </div>
          )}
        </div>

        <div className={`panel metadata-detail ${detailLoading && detail ? 'panel-transitioning' : ''}`}>
          {(loading || detailLoading) && !detail ? (
            <DetailSkeleton blocks={5} />
          ) : detail ? (
            <>
              <div className="panel-heading">
                <div>
                  <div className="actions">
                    <h2>{detail.table_name}</h2>
                    <code>{detail.table_code}</code>
                    {(() => {
                      const gate = tableGate(detail, readinessById[detail.id])
                      return <StatusBadge tone={gate.tone}>{gate.label}</StatusBadge>
                    })()}
                  </div>
                  <p>{detail.table_definition || '暂无表级业务说明'} · 版本 v{detail.current_version}</p>
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

              {readinessById[detail.id] && (
                <div className="detail-readiness-grid" aria-label="当前报表生成条件">
                  {['ddl', 'rule_materials', 'code_sets'].map((key) => {
                    const section = readinessById[detail.id][key]
                    const label = key === 'ddl' ? '表结构' : key === 'rule_materials' ? '校验信息' : '关联码表'
                    const na = section.applicable === false
                    const problems = key === 'code_sets' ? section.defects || [] : section.blockers || []
                    const firstProblem = problems[0]
                    return (
                      <div key={key} className={`detail-readiness-item ${na ? 'na' : problems.length ? 'bad' : 'ok'}`}>
                        <span>{label}</span>
                        <strong>{na ? '不涉及' : problems.length ? `需处理 ${problems.length} 项` : '可用'}</strong>
                        {firstProblem && <small title={firstProblem.message}>{firstProblem.message}</small>}
                        {key === 'code_sets' && firstProblem && (
                          <button
                            type="button"
                            className="button-link"
                            onClick={() => firstProblem.code_set_id ? openCodeSet(firstProblem.code_set_id) : onOpenCodeSets?.()}
                          >
                            去处理码表
                          </button>
                        )}
                      </div>
                    )
                  })}
                </div>
              )}

              {showTypeImport && (
                <TypeImportPanel
                  file={typeImportFile}
                  result={typeImportResult}
                  busy={busy}
                  onDownloadTemplate={downloadTypeTemplate}
                  onFileChange={(file) => {
                    setTypeImportFile(file)
                    setTypeImportResult(null)
                  }}
                  onSubmit={submitTypeImport}
                />
              )}

              {showAdd && (
                <AddFieldForm
                  newField={newField}
                  busy={busy}
                  onChange={setNewField}
                  onSubmit={addField}
                />
              )}

              {totalIssues > 0 && tab === 'fields' && (
                <div className="review-guide">
                  <span>
                    {blockingIssues.length > 0
                      ? `这张表有 ${blockingIssues.length} 项需要处理${advisoryIssues.length ? `，另有 ${advisoryIssues.length} 条提示` : ''}。`
                      : `这张表有 ${advisoryIssues.length} 条提示，没有阻断项。`}
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
                  className={`metadata-secondary-tab ${tab === 'revisions' ? 'active' : ''}`}
                  onClick={() => setTab('revisions')}
                >
                  变更记录 ({detail.revisions.length})
                </button>
              </div>

              {tab === 'fields' && (
                <>
                  <FieldEditorDrawer
                    activeField={activeField}
                    filteredFields={filteredFields}
                    isDirty={activeFieldDirty}
                    isPublished={false}
                    busy={busy}
                    editReason={editReason}
                    onEditReasonChange={setEditReason}
                    onSelectField={switchOrCloseField}
                    onChangeProp={changeFieldProp}
                    onSave={saveField}
                    onRemove={removeField}
                    onClose={() => switchOrCloseField(null)}
                  />

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
                              id={`field-row-${field.id}`}
                              className={[
                                'clickable-row',
                                isEditing ? 'field-row-active' : '',
                                highlightFieldId === field.id ? 'field-row-highlight' : '',
                              ]
                                .filter(Boolean)
                                .join(' ')}
                              onClick={(event) => {
                                if (event.target.closest('button, a, input, select, textarea')) return
                                switchOrCloseField(isEditing ? null : field.id)
                              }}
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
                                      onClick={() => {
                                        if (codeMeta.codeSetId) {
                                          openCodeSet(codeMeta.codeSetId)
                                          return
                                        }
                                        const next = new URLSearchParams()
                                        next.set('view', 'codes')
                                        const docId = params.get('document_id') || detail?.document_id
                                        if (docId) next.set('document_id', docId)
                                        next.set('name', codeMeta.text)
                                        navigate(`/documents?${next.toString()}`)
                                      }}
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
                                  onClick={(event) => {
                                    event.stopPropagation()
                                    switchOrCloseField(isEditing ? null : field.id)
                                  }}
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
                <ValidationTabView
                  validations={detail.validations}
                  fields={detail.fields}
                  onLocateField={(fieldEn) => {
                    setFieldSearch(fieldEn)
                    setEditingFieldId(detail.fields.find((field) => field.field_en === fieldEn)?.id || null)
                    setTab('fields')
                  }}
                />
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
