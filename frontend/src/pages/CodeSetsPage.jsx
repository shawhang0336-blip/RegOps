import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { v2 } from '../api.js'
import { useOperator } from '../context/OperatorContext.jsx'
import PageHeader from './PageHeader.jsx'
import StatusMessage from './StatusMessage.jsx'
import { SourceLocatorTag, StatusBadge } from './SharedUI.jsx'

export default function CodeSetsPage() {
  const [params] = useSearchParams()
  const { requireOperator } = useOperator()

  const [sets, setSets] = useState([])
  const [unresolved, setUnresolved] = useState([])
  const [detail, setDetail] = useState(null)
  const [newSet, setNewSet] = useState({ code: '', name: '', definition: '' })
  const [newValue, setNewValue] = useState({ value: '', label_cn: '', label_en: '' })
  const [filterText, setFilterText] = useState('')
  const [setSearch, setSetSearch] = useState('')
  const [includeInline, setIncludeInline] = useState(true)
  const [dragging, setDragging] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  async function load(preferredId) {
    const [setList, unresolvedList] = await Promise.all([
      v2('/code-sets'),
      v2('/code-sets/references/unresolved').catch(() => []),
    ])
    setSets(setList)
    setUnresolved(unresolvedList)

    const paramSetId = params.get('set')
    const paramName = params.get('name')
    const matchedByName = paramName
      ? setList.find((s) => s.name === paramName || s.code === paramName.toUpperCase())?.id
      : null

    const targetId = preferredId || paramSetId || matchedByName || detail?.id || setList[0]?.id
    if (targetId) {
      await select(targetId)
    }
  }

  useEffect(() => {
    load().catch((err) => setError(err.message))
  }, [])

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
      setNotice(
        `已按文档解析流水线从「${result.file_name}」并行提取 ${result.code_set_count} 张码值字典（共 ${result.total_value_count} 项码值），自动绑定消解 ${result.resolved_reference_count} 处字段码值引用。`
      )
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
    const editor = requireOperator(setError)
    if (!editor) return
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
          editor,
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
    const editor = requireOperator(setError)
    if (!editor) return
    setBusy(true)
    setError('')
    try {
      const body = await v2(`/code-sets/${detail.id}/values/${value.id}`, {
        method: 'PATCH',
        body: JSON.stringify({
          expected_version: detail.current_version,
          editor,
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
    const editor = requireOperator(setError)
    if (!editor) return
    setBusy(true)
    setError('')
    try {
      const body = await v2(`/code-sets/${detail.id}/publish`, {
        method: 'POST',
        body: JSON.stringify({
          expected_version: detail.current_version,
          editor,
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

  async function importExcel(event) {
    const editor = requireOperator(setError)
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!editor || !file) return
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
      <PageHeader
        eyebrow="STAGE 03 · 码值文档解析、公共字典与未决引用消解"
        title="公共码值中心"
        description="采用与规范文件中心同构的「文件切块 → 结构预检 → 并行提取」流水线，支持直接上传 PDF/DOCX/XLSX 批量解析公共代码表、制度附录及字段内联枚举。"
        actions={
          <div className="actions">
            <label className="check-label" style={{ paddingTop: 0 }}>
              <input
                type="checkbox"
                checked={includeInline}
                onChange={(e) => setIncludeInline(e.target.checked)}
              />
              同步提取表内字段枚举
            </label>
            <label className="button primary">
              {busy ? '正在解析码值文档...' : '+ 上传文件解析码值 (.pdf/.docx/.xlsx)'}
              <input
                className="file-input"
                type="file"
                accept=".pdf,.docx,.xlsx"
                disabled={busy}
                onChange={onDocInputChange}
              />
            </label>
          </div>
        }
      />

      <StatusMessage error={error} notice={notice} onClose={() => { setError(''); setNotice('') }} />

      <label
        className={`upload-dropzone ${dragging ? 'dragging' : ''}`}
        onDragOver={(e) => { e.preventDefault(); setDragging(true) }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
      >
        <div className="upload-dropzone-text">
          <strong>拖拽码值文件或监管规范文档到此处，按文档流水线批量解析码值字典</strong>
          <span>支持：监管规范 PDF（第九章代码表/内联枚举）、统计制度 PDF（附录一～九代码表）、Word/Excel 多表码值文件，提取后自动与表元数据字段引用对齐消解</span>
        </div>
        <span className="button secondary">选择码值文件</span>
        <input
          className="file-input"
          type="file"
          accept=".pdf,.docx,.xlsx"
          disabled={busy}
          onChange={onDocInputChange}
        />
      </label>

      {unresolved.length > 0 && (
        <div className="unresolved-banner">
          <div className="unresolved-banner-header">
            <strong>发现 {unresolved.length} 处未解决的字段码值引用（可上传对应码值文档自动消解，或点击下方标签手动补充）</strong>
            <StatusBadge status="UNRESOLVED">{unresolved.length} 项待消解</StatusBadge>
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
                <span className="muted-text">点击维护此码值表 →</span>
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
                    <em className="danger-text">
                      关联 {set.unresolved_reference_count} 个未决字段引用
                    </em>
                  ) : (
                    <em>版本 v{set.current_version} · {set.source_type}</em>
                  )}
                </button>
              ))}
            </div>
          ) : (
            <div className="empty">暂无匹配码值表，可在上方上传文件解析或下方手动新建。</div>
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
                    <StatusBadge status={detail.source_type} />
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
                    单表追加 Excel (.xlsx)
                    <input
                      className="file-input"
                      type="file"
                      accept=".xlsx"
                      disabled={busy}
                      onChange={importExcel}
                    />
                  </label>
                  <button type="button" className="button primary" disabled={busy} onClick={publish}>
                    发布码值版本 (v{detail.current_version})
                  </button>
                </div>
              </div>

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
            <div className="empty">请从左侧选择一张码值字典表，或直接在上方拖拽上传文件自动解析码值。</div>
          )}
        </div>
      </section>
    </>
  )
}
