import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { v2 } from '../api.js'
import PageHeader from './PageHeader.jsx'
import StatusMessage from './StatusMessage.jsx'
import { ISSUE_LABELS, SourceLocatorTag, StatusBadge, friendlyError, issueSeverity } from './SharedUI.jsx'

export default function CodeSetsPage({ embedded = false, onDocumentChange }) {
  const [params] = useSearchParams()

  const [sets, setSets] = useState([])
  const [unresolved, setUnresolved] = useState([])
  const [detail, setDetail] = useState(null)
  const [newSet, setNewSet] = useState({ code: '', name: '', definition: '' })
  const [newValue, setNewValue] = useState({ value: '', label_cn: '', label_en: '' })
  const [filterText, setFilterText] = useState('')
  const [setSearch, setSetSearch] = useState('')
  const [includeInline, setIncludeInline] = useState(true)
  const [dragging, setDragging] = useState(false)
  const [readiness, setReadiness] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  async function load(preferredId) {
    const documentId = params.get('document_id')
    const scopeQuery = documentId ? `?document_id=${encodeURIComponent(documentId)}` : ''
    if (embedded && !documentId) {
      setSets([])
      setDetail(null)
      return
    }
    const readinessQuery = documentId
      ? `?document_id=${encodeURIComponent(documentId)}&referenced_only=true`
      : '?referenced_only=true'
    const [setList, unresolvedList, readinessPayload] = await Promise.all([
      v2(`/code-sets${scopeQuery}`),
      v2(`/code-sets/references/unresolved${scopeQuery}`).catch(() => []),
      v2(`/code-sets/readiness${readinessQuery}`).catch(() => null),
    ])
    setSets(setList)
    setUnresolved(unresolvedList)
    setReadiness(readinessPayload)

    const paramSetId = params.get('set')
    const paramName = params.get('name')
    const matchedByName = paramName
      ? setList.find((s) => s.name === paramName || s.code === paramName.toUpperCase())?.id
      : null

    const currentId = detail?.id
    const targetId = preferredId || (setList.some((set) => set.id === paramSetId) ? paramSetId : null) || matchedByName || (setList.some((set) => set.id === currentId) ? currentId : null) || setList[0]?.id
    if (targetId) {
      await select(targetId)
    }
  }

  useEffect(() => {
    setDetail(null)
    load().catch((err) => setError(err.message))
  }, [params.get('document_id')])

  async function select(id) {
    const data = await v2(`/code-sets/${id}`)
    setDetail(data)
  }

  async function handleDocumentCodeUpload(file) {
    if (!file) return
    const form = new FormData()
    form.append('file', file)
    form.append('include_inline', includeInline ? 'true' : 'false')
    setBusy(true)
    setError('')
    try {
      const result = await v2('/code-sets/upload-file', { method: 'POST', body: form })
      const firstId = result.code_set_ids?.[0]
      await load(firstId)
      if (onDocumentChange && result.document_id) onDocumentChange(result.document_id)
      setNotice(
        `已从「${result.file_name}」提取 ${result.code_set_count} 张码值字典（共 ${result.total_value_count} 项码值）；全库已绑定 ${result.resolved_reference_count} 处字段码值引用。`
      )
      if (result.errors?.length) {
        setError(`${result.errors.length} 个码值块未完成：${result.errors[0].error}`)
      }
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  function onDocInputChange(event) {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (file) handleDocumentCodeUpload(file)
  }

  function onDrop(event) {
    event.preventDefault()
    setDragging(false)
    if (busy) return
    const file = event.dataTransfer.files?.[0]
    if (file) handleDocumentCodeUpload(file)
  }

  async function createSet() {
    if (!newSet.code.trim() || !newSet.name.trim()) {
      setError('请填写码值表代码和中文名称。')
      return
    }
    setBusy(true)
    setError('')
    try {
      const body = await v2('/code-sets', {
        method: 'POST',
        body: JSON.stringify({ ...newSet, source_type: 'MANUAL' }),
      })
      setNewSet({ code: '', name: '', definition: '' })
      await load(body.id)
      setNotice(`已创建码值表「${body.name} (${body.code})」。`)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  async function addValue() {
    if (!newValue.value.trim() || !newValue.label_cn.trim()) {
      setError('请填写码值编码和中文含义。')
      return
    }
    setBusy(true)
    setError('')
    try {
      const body = await v2(`/code-sets/${detail.id}/values`, {
        method: 'POST',
        body: JSON.stringify({
          expected_version: detail.current_version,
          ...newValue,
        }),
      })
      setDetail(body)
      setNewValue({ value: '', label_cn: '', label_en: '' })
      await load(body.id)
      setNotice(`已添加码值「${newValue.value} - ${newValue.label_cn}」，关联未决引用状态已自动刷新。`)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  async function toggle(value) {
    setBusy(true)
    setError('')
    try {
      const body = await v2(`/code-sets/${detail.id}/values/${value.id}`, {
        method: 'PATCH',
        body: JSON.stringify({
          expected_version: detail.current_version,
          reason: '人工启停码值项',
          changes: { enabled: !value.enabled },
        }),
      })
      setDetail(body)
      await load(body.id)
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
      const body = await v2(`/code-sets/${detail.id}/publish`, {
        method: 'POST',
        body: JSON.stringify({
          expected_version: detail.current_version,
          reason: '正式发布码值表版本',
          changes: {},
        }),
      })
      setDetail(body)
      await load(body.id)
      setNotice(`已发布码值表「${body.name}」v${body.published_version}，所有关联字段的码值引用状态已同步更新。`)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  async function publishReadyCodeSets() {
    const documentId = params.get('document_id')
    const target = readiness?.summary?.ready_and_referenced || 0
    if (!target) return
    if (!window.confirm(`将发布 ${target} 张被引用的码表。发布后这些码值会进入阶段 2 的产出包。继续？`)) {
      return
    }
    setBusy(true)
    setError('')
    try {
      const result = await v2('/code-sets/publish-batch', {
        method: 'POST',
        body: JSON.stringify({ document_id: documentId || undefined, referenced_only: true }),
      })
      setNotice(`已发布 ${result.published} 张码表，跳过 ${result.skipped} 张。`)
      await load(detail?.id)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  async function importExcel(event) {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!file) return
    const form = new FormData()
    form.append('file', file)
    form.append('expected_version', detail.current_version)
    setBusy(true)
    setError('')
    try {
      const body = await v2(`/code-sets/${detail.id}/import`, { method: 'POST', body: form })
      setDetail(body.code_set)
      await load(body.code_set.id)
      setNotice(`已从 Excel 成功导入 ${body.imported} 个码值项。`)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  const filteredSets = sets.filter((s) => {
    if (!setSearch.trim()) return true
    const q = setSearch.toLowerCase()
    return (
      (s.name || '').toLowerCase().includes(q) ||
      (s.code || '').toLowerCase().includes(q) ||
      (s.source_type || '').toLowerCase().includes(q)
    )
  })

  const filteredValues = (detail?.values || []).filter((item) => {
    if (!filterText.trim()) return true
    const q = filterText.toLowerCase()
    return (
      (item.value || '').toLowerCase().includes(q) ||
      (item.label_cn || '').toLowerCase().includes(q) ||
      (item.label_en || '').toLowerCase().includes(q)
    )
  })

  return (
    <>
      {!embedded && <PageHeader
        eyebrow="第 1 步 · 共 3 步"
        title="维护码值"
        description="检查字段可使用的取值，补充缺失内容并发布；已就绪的码值才能用于生成和验证。"
      />}

      <StatusMessage error={error} notice={notice} onClose={() => { setError(''); setNotice('') }} />

      <div
        className={`upload-dropzone ${dragging ? 'dragging' : ''}`}
        onDragOver={(e) => { e.preventDefault(); setDragging(true) }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
      >
        <div className="upload-dropzone-text">
          <strong>上传码值文件，系统会自动识别并关联字段</strong>
          <span>支持 PDF、Word、Excel；也可以上传完整规范，只提取其中的码值表</span>
        </div>
        <label className="button primary">
          {busy ? '正在识别...' : '选择码值文件'}
          <input className="file-input" type="file" accept=".pdf,.docx,.xlsx" disabled={busy} onChange={onDocInputChange} />
        </label>
      </div>
      <label className="inline-option code-upload-option">
        <input type="checkbox" checked={includeInline} onChange={(e) => setIncludeInline(e.target.checked)} />
        同时提取字段说明中列出的取值
      </label>

      {readiness && readiness.summary.ready_and_referenced > 0 && (
        <div className="readiness-summary">
          <div className="readiness-summary-main">
            <strong>
              还有 {readiness.summary.ready_and_referenced} 张被引用的码表可以发布
            </strong>
            <span className="muted-text">
              已发布 {readiness.summary.published} / 被引用 {readiness.summary.referenced} 张 ·
              卡点：{readiness.summary.blocker_codes.length
                ? readiness.summary.blocker_codes.map(([code, count]) => `${code} ${count}`).join('、')
                : '无'}
            </span>
          </div>
          <button type="button" className="button secondary sm" disabled={busy} onClick={publishReadyCodeSets}>
            批量发布可发布的码表
          </button>
          {readiness.summary.blocker_codes.length > 0 && (
            <p className="readiness-hint">
              空占位码表（没有码值）和被引用字段更短的码值不会被发布——阶段 2 只认已发布码表，
              发进去的脏数据会直接进产出包。
            </p>
          )}
        </div>
      )}

      {unresolved.length > 0 && (
        <div className="unresolved-banner">
          <div className="unresolved-banner-header">
            <strong>还有 {unresolved.length} 项码值需要补充</strong>
            <StatusBadge status="UNRESOLVED">{unresolved.length} 项待处理</StatusBadge>
          </div>
          <div className="unresolved-chips">
            {unresolved.map((item) => (
              <button
                key={item.id}
                type="button"
                className="unresolved-chip"
                onClick={() => select(item.code_set_id)}
              >
                <strong>{item.reference_text}</strong>
                <span className="muted-text">去补充 →</span>
              </button>
            ))}
          </div>
        </div>
      )}


      <section className="workspace-grid code-layout">
        <div className="panel list-panel">
          <div className="panel-heading">
            <div>
              <h2>码值字典表</h2>
              <p>共 {sets.length} 张字典表</p>
            </div>
            <StatusBadge tone="neutral">{sets.length}</StatusBadge>
          </div>

          <div style={{ marginBottom: 10 }}>
            <input
              type="search"
              placeholder="检索码值表名称或代码..."
              value={setSearch}
              onChange={(e) => setSetSearch(e.target.value)}
            />
          </div>

          {filteredSets.length > 0 ? (
            <div className="list-stack">
              {filteredSets.map((set) => (
                <button
                  type="button"
                  className={`list-row ${detail?.id === set.id ? 'selected' : ''}`}
                  key={set.id}
                  onClick={() => select(set.id)}
                >
                  <div className="list-row-top">
                    <strong>{set.name}</strong>
                    <StatusBadge status={set.status} />
                  </div>
                  <span>
                    <code>{set.code}</code> · 启用 {set.enabled_value_count ?? set.value_count}/{set.value_count} 项
                  </span>
                  {set.unresolved_reference_count > 0 ? (
                    <em className="danger-text">还有 {set.unresolved_reference_count} 个字段待关联</em>
                  ) : (
                    <em>{set.published_version ? `已发布 v${set.published_version}` : '尚未发布'} · {set.value_count} 项取值</em>
                  )}
                </button>
              ))}
            </div>
          ) : (
            <div className="empty">当前文件没有已识别的码表，可点击上方“解析当前文件”，或上传码值文件。</div>
          )}

          <details className="inline-form" open={!sets.length}>
            <summary>+ 手动新建码值字典表</summary>
            <input
              placeholder="字典英文代码（例：CURRENCY_CODE）"
              value={newSet.code}
              onChange={(e) => setNewSet({ ...newSet, code: e.target.value.toUpperCase() })}
            />
            <input
              placeholder="字典中文名称（需与字段码值引用一致）"
              value={newSet.name}
              onChange={(e) => setNewSet({ ...newSet, name: e.target.value })}
            />
            <textarea
              placeholder="业务定义与适用范围说明"
              value={newSet.definition}
              onChange={(e) => setNewSet({ ...newSet, definition: e.target.value })}
            />
            <button type="button" className="button primary" disabled={busy} onClick={createSet}>
              创建码值表
            </button>
          </details>
        </div>

        <div className="panel">
          {detail ? (
            <>
              <div className="panel-heading">
                <div>
                  <div className="actions">
                    <h2>{detail.name}</h2>
                    <code>{detail.code}</code>
                    <StatusBadge status={detail.status} />
                    <StatusBadge status={detail.source_type}>{detail.source_type === 'MANUAL' ? '人工维护' : '文档识别'}</StatusBadge>
                  </div>
                  <p>
                    {detail.definition || '暂无字典定义'} · 当前版本 v{detail.current_version}
                    {detail.published_version ? `（已发布 v${detail.published_version}）` : ''}
                  </p>
                </div>
                <div className="actions">
                  <input
                    type="search"
                    placeholder="过滤码值或中文含义..."
                    value={filterText}
                    onChange={(e) => setFilterText(e.target.value)}
                  />
                  <label className="button secondary">
                    追加 Excel 取值
                    <input
                      className="file-input"
                      type="file"
                      accept=".xlsx"
                      disabled={busy}
                      onChange={importExcel}
                    />
                  </label>
                  <button type="button" className="button primary" disabled={busy} onClick={publish}>
                    发布此码值表
                  </button>
                </div>
              </div>

              {detail.validations?.length > 0 && (
                <details className="technical-details">
                  <summary>查看自动检查结果</summary>
                <div className="architecture-result">
                  <div className="architecture-summary">
                    <strong>程序检查与 AI 独立复核</strong>
                    {detail.validations.slice(0, 4).map((validation, index) => (
                      <StatusBadge key={`${validation.validator_kind}-${validation.created_at}-${index}`} tone={validation.status === 'PASSED' ? 'green' : 'amber'}>
                        {validation.validator_kind === 'AI_REVIEW' ? 'AI 复核' : '程序检查'} · {validation.status === 'PASSED' ? '通过' : '需核对'} · {validation.issues?.length || 0} 项
                      </StatusBadge>
                    ))}
                  </div>
                  {detail.validations.slice(0, 4).flatMap((validation) => validation.issues || []).slice(0, 8).map((issue, index) => (
                    <p className="architecture-warning" key={`${issue.code}-${index}`}>
                      {issueSeverity(issue) === 'ERROR' ? '错误' : '提示'} / {ISSUE_LABELS[issue.code] || issue.code}{issue.value ? ` / ${issue.value}` : ''}：{friendlyError(issue.message || '请对照原文确认该码值。')}
                    </p>
                  ))}
                </div>
                </details>
              )}

              <div className="add-value-form">
                <input
                  placeholder="码值编码 *（例：USD / 100）"
                  value={newValue.value}
                  onChange={(e) => setNewValue({ ...newValue, value: e.target.value })}
                />
                <input
                  placeholder="中文含义 *（例：美元）"
                  value={newValue.label_cn}
                  onChange={(e) => setNewValue({ ...newValue, label_cn: e.target.value })}
                />
                <input
                  placeholder="英文含义（选填，例：US Dollar）"
                  value={newValue.label_en}
                  onChange={(e) => setNewValue({ ...newValue, label_en: e.target.value })}
                />
                <button type="button" className="button primary" disabled={busy} onClick={addValue}>
                  + 添加码值项
                </button>
              </div>

              {filteredValues.length > 0 ? (
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>#</th>
                        <th>码值编码</th>
                        <th>中文含义</th>
                        <th>英文含义 / 映射</th>
                        <th>当前状态</th>
                        <th>来源溯源</th>
                        <th>操作</th>
                      </tr>
                    </thead>
                    <tbody>
                      {filteredValues.map((value, idx) => (
                        <tr key={value.id}>
                          <td className="row-index">{idx + 1}</td>
                          <td>
                            <code>{value.value}</code>
                          </td>
                          <td>
                            <strong>{value.label_cn}</strong>
                            {value.definition ? <small style={{ display: 'block' }}>{value.definition}</small> : null}
                          </td>
                          <td>{value.label_en || '—'}</td>
                          <td>
                            <StatusBadge tone={value.enabled ? 'green' : 'neutral'}>
                              {value.enabled ? '已启用' : '已停用'}
                            </StatusBadge>
                          </td>
                          <td>
                            <SourceLocatorTag locator={value.source_locator} />
                          </td>
                          <td>
                            <button
                              type="button"
                              className="button secondary sm"
                              disabled={busy}
                              onClick={() => toggle(value)}
                            >
                              {value.enabled ? '停用' : '启用'}
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <div className="empty" style={{ marginTop: 14 }}>
                  当前码值表尚无码值项，请在上方上传码值文档解析、手动添加或通过 Excel 批量导入。
                </div>
              )}
            </>
          ) : (
            <div className="empty">当前文件还没有可查看的码值。</div>
          )}
        </div>
      </section>
    </>
  )
}
