import { useCallback, useEffect, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { v2 } from '../api.js'
import { useOperator } from '../context/OperatorContext.jsx'
import PageHeader from './PageHeader.jsx'
import StatusMessage from './StatusMessage.jsx'
import {
  EvidenceCard,
  RevisionDiffView,
  StatusBadge,
  ISSUE_LABELS,
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

export default function MetadataPage({ embedded = false }) {
  const navigate = useNavigate()
  const [params] = useSearchParams()
  const { requireOperator } = useOperator()

  const [tables, setTables] = useState([])
  const [detail, setDetail] = useState(null)
  const [unresolvedRefs, setUnresolvedRefs] = useState([])
  const [codeSets, setCodeSets] = useState([])
  const [editingFieldId, setEditingFieldId] = useState(null)
  const [editReason, setEditReason] = useState('人工复核修正元数据')
  const [newField, setNewField] = useState(emptyField)
  const [showAdd, setShowAdd] = useState(false)
  const [tab, setTab] = useState(params.get('tab') || 'fields')
  const [fieldSearch, setFieldSearch] = useState('')
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
    const [tableList, unresolvedList, codeSetList] = await Promise.all([
      v2(`/metadata/tables${scopeQuery}`),
      v2(`/code-sets/references/unresolved${scopeQuery}`).catch(() => []),
      v2(`/code-sets${scopeQuery}`).catch(() => []),
    ])
    setTables(tableList)
    setUnresolvedRefs(unresolvedList)
    setCodeSets(codeSetList)

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
    setEditingFieldId(null)
    if (resetTab) setTab('fields')
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
      const editor = requireOperator(setError)
      if (!editor) return
      setBusy(true)
      setError('')
      try {
        const body = await v2(`/metadata/tables/${detail.id}/fields/${field.id}`, {
          method: 'PATCH',
          body: JSON.stringify({
            expected_version: detail.current_version,
            editor,
            reason: editReason || '人工复核元数据',
            changes: {
              field_cn: field.field_cn,
              field_en: field.field_en,
              field_definition: field.field_definition,
              check_text_raw: field.check_text_raw,
              data_type: field.data_type,
              length: Number(field.length),
              decimal_places: Number(field.decimal_places),
              required: Boolean(field.required),
              code_reference_text: field.code_reference_text,
            },
          }),
        })
        setDetail(body)
        await load(body.id)
        setNotice(`字段「${field.field_cn} (${field.field_en})」已保存，当前表升级为草稿 v${body.current_version}。`)
      } catch (err) {
        setError(err.message)
      } finally {
        setBusy(false)
      }
    },
    [detail, editReason, requireOperator]
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
    const editor = requireOperator(setError)
    if (!editor || !window.confirm(`确认删除字段「${field.field_cn} (${field.field_en})」吗？`)) return
    setBusy(true)
    setError('')
    try {
      const body = await v2(`/metadata/tables/${detail.id}/fields/${field.id}`, {
        method: 'DELETE',
        body: JSON.stringify({
          expected_version: detail.current_version,
          editor,
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
    const editor = requireOperator(setError)
    if (!editor) return
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
          editor,
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
    const editor = requireOperator(setError)
    if (!editor) return
    setBusy(true)
    setError('')
    try {
      const body = await v2(`/metadata/tables/${detail.id}/publish`, {
        method: 'POST',
        body: JSON.stringify({
          expected_version: detail.current_version,
          approver: editor,
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

  async function createDraft() {
    const editor = requireOperator(setError)
    if (!editor) return
    setBusy(true)
    setError('')
    try {
      const body = await v2(`/metadata/tables/${detail.id}/draft`, {
        method: 'POST',
        body: JSON.stringify({
          expected_version: detail.current_version,
          approver: editor,
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
    const unresolved = unresolvedRefs.find((r) => r.field_id === field.id)
    const matchedSet = codeSets.find((s) => s.name === text || s.code === text.toUpperCase())
    if (unresolved) {
      return {
        text,
        status: 'UNRESOLVED',
        codeSetId: unresolved.code_set_id || matchedSet?.id,
      }
    }
    if (matchedSet) {
      return {
        text,
        status: matchedSet.enabled_value_count > 0 ? 'RESOLVED' : 'UNRESOLVED',
        codeSetId: matchedSet.id,
      }
    }
    return { text, status: 'DRAFT', codeSetId: null }
  }

  const totalIssues = (detail?.validations || []).reduce(
    (acc, v) => acc + (Array.isArray(v.issues) ? groupValidationIssues(v.issues).length : 0),
    0
  )

  const pageActions = detail && (
    <>
      {isPublished ? (
        <>
          <button type="button" className="button secondary" disabled={busy} onClick={createDraft}>新建修改版本</button>
          <button type="button" className="button primary" onClick={() => navigate(`/generation?table=${detail.id}`)}>前往生成 →</button>
        </>
      ) : (
        <>
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
              <p>共 {tables.length} 张表</p>
            </div>
            <StatusBadge tone="neutral">{tables.length}</StatusBadge>
          </div>

          {tables.length > 0 ? (
            <div className="list-stack">
              {tables.map((table) => (
                <button
                  type="button"
                  className={`list-row ${detail?.id === table.id ? 'selected' : ''}`}
                  key={table.id}
                  onClick={() => select(table.id, false)}
                >
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
                </button>
              ))}
            </div>
          ) : (
            <div className="empty">
              当前文件还没有字段结果，可点击上方“解析当前文件”；新文件上传后会自动解析。
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
                  <span>这张表有 {totalIssues} 项需要核对，请先查看具体问题。</span>
                  <button type="button" className="button secondary sm" onClick={() => setTab('validation')}>查看待核对问题</button>
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
                  <StatusBadge tone={totalIssues > 0 ? 'amber' : 'green'}>
                    {totalIssues > 0 ? `${totalIssues} 项提示` : '通过'}
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
                                <StatusBadge tone={field.required ? 'amber' : 'neutral'}>
                                  {field.required ? '必填' : '选填'}
                                </StatusBadge>
                              </td>
                              <td>
                                {codeMeta ? (
                                  <div className="row-actions">
                                    <span>{codeMeta.text}</span>
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
                                      {issue.field_en ? `字段 ${issue.field_en}` : '整表提示'} · {ISSUE_LABELS[issue.code] || issue.code}
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
