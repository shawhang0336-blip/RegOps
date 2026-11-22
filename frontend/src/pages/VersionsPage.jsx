import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api.js'
import { DetailSkeleton, ListSkeleton } from '../components/Skeleton.jsx'
import PageHeader from './PageHeader.jsx'
import StatusMessage from './StatusMessage.jsx'
import { RevisionDiffView, StatusBadge, formatTime } from './SharedUI.jsx'

const IMPACT_META = {
  BREAKING: { label: '破坏性变更', tone: 'red' },
  RULE_IMPACT: { label: '规则受影响', tone: 'amber' },
  METADATA_ONLY: { label: '仅描述变更', tone: 'blue' },
  NONE: { label: '无变化', tone: 'neutral' },
}

const CHANGE_KIND_META = {
  ADDED: { label: '新增报表', tone: 'green' },
  REMOVED: { label: '下线报表', tone: 'red' },
  MODIFIED: { label: '结构/校验变更', tone: 'amber' },
  UNCHANGED: { label: '无变化', tone: 'neutral' },
}

const ATTR_LABELS = {
  data_type: '数据类型',
  length: '列宽',
  decimal_places: '小数位',
  required: '必填',
  check_text_raw: '校验原文',
  code_reference_text: '码表引用',
  field_cn: '中文名',
}

export default function VersionsPage() {
  const navigate = useNavigate()
  const [activeTab, setActiveTab] = useState('doc-diff')

  // Tab 1: 跨版本制度文档 Diff
  const [documents, setDocuments] = useState([])
  const [baseDocId, setBaseDocId] = useState('')
  const [targetDocId, setTargetDocId] = useState('')
  const [docDiff, setDocDiff] = useState(null)
  const [docDiffFilter, setDocDiffFilter] = useState('CHANGED')
  const [diffBusy, setDiffBusy] = useState(false)
  const [genBusy, setGenBusy] = useState(false)

  // Tab 2: 跨验证批次规则与 SQL Diff
  const [runs, setRuns] = useState([])
  const [baseRunId, setBaseRunId] = useState('')
  const [targetRunId, setTargetRunId] = useState('')
  const [runDiff, setRunDiff] = useState(null)
  const [runDiffBusy, setRunDiffBusy] = useState(false)

  // Tab 3: 单表修订记录（原有功能完整保留）
  const [tables, setTables] = useState([])
  const [selectedId, setSelectedId] = useState(null)
  const [detail, setDetail] = useState(null)
  const [loading, setLoading] = useState(true)
  const [detailLoading, setDetailLoading] = useState(false)
  const [tableSearch, setTableSearch] = useState('')
  const [filterKind, setFilterKind] = useState('ALL')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const requestRef = useRef(0)

  async function selectTable(id) {
    const reqId = ++requestRef.current
    setSelectedId(id)
    setDetailLoading(true)
    try {
      const data = await api(`/api/metadata/tables/${id}`)
      if (reqId === requestRef.current) setDetail(data)
    } catch (err) {
      if (reqId === requestRef.current) setError(err.message)
    } finally {
      if (reqId === requestRef.current) setDetailLoading(false)
    }
  }

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    Promise.all([
      api('/api/metadata/tables').catch(() => []),
      api('/api/documents').catch(() => []),
      api('/api/runs').catch(() => []),
    ])
      .then(async ([tableList, docList, runList]) => {
        if (cancelled) return
        setTables(tableList)
        setDocuments(docList)
        setRuns(runList)
        if (docList.length >= 2) {
          setBaseDocId(docList[1].id)
          setTargetDocId(docList[0].id)
        } else if (docList.length === 1) {
          setBaseDocId(docList[0].id)
          setTargetDocId(docList[0].id)
        }
        if (runList.length >= 2) {
          setBaseRunId(runList[1].id)
          setTargetRunId(runList[0].id)
        } else if (runList.length === 1) {
          setBaseRunId(runList[0].id)
          setTargetRunId(runList[0].id)
        }
        if (tableList[0]?.id && !cancelled) {
          await selectTable(tableList[0].id)
        }
      })
      .catch((err) => {
        if (!cancelled) setError(err.message)
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [])

  async function compareDocuments() {
    if (!baseDocId || !targetDocId) return
    setDiffBusy(true)
    setError('')
    try {
      const data = await api(`/api/documents/diff?base_id=${encodeURIComponent(baseDocId)}&target_id=${encodeURIComponent(targetDocId)}`,
      )
      setDocDiff(data)
    } catch (err) {
      setError(err.message)
    } finally {
      setDiffBusy(false)
    }
  }

  async function runIncrementalGeneration() {
    const affectedIds = docDiff?.affected_table_ids || []
    if (!affectedIds.length) return
    setGenBusy(true)
    setError('')
    try {
      const created = await api('/api/generation/runs', {
        method: 'POST',
        body: JSON.stringify({ table_ids: affectedIds, provider: 'live', accept_warnings: true }),
      })
      setNotice(`已对 ${affectedIds.length} 张受影响报表发起增量规则生成与验证。`)
      navigate(`/runs?run=${created.id}`)
    } catch (err) {
      setError(err.message)
    } finally {
      setGenBusy(false)
    }
  }

  async function compareRuns() {
    if (!baseRunId || !targetRunId) return
    setRunDiffBusy(true)
    setError('')
    try {
      const data = await api(
        `/api/runs/diff?base_run_id=${encodeURIComponent(baseRunId)}&target_run_id=${encodeURIComponent(targetRunId)}`,
      )
      setRunDiff(data)
    } catch (err) {
      setError(err.message)
    } finally {
      setRunDiffBusy(false)
    }
  }

  const visibleDocTables = (docDiff?.tables || []).filter((item) => {
    if (docDiffFilter === 'CHANGED') return item.change_kind !== 'UNCHANGED'
    if (docDiffFilter === 'BREAKING') return item.impact === 'BREAKING'
    return true
  })

  const visibleTables = tables.filter((table) => {
    const q = tableSearch.trim().toLowerCase()
    if (!q) return true
    return (
      (table.table_name || '').toLowerCase().includes(q) ||
      (table.table_code || '').toLowerCase().includes(q)
    )
  })

  const revisions = (detail?.revisions || []).filter(
    (item) => filterKind === 'ALL' || item.revision_kind === filterKind,
  )

  return (
    <>
      <PageHeader
        eyebrow="监管版本运营台"
        title="制度差异与变更追踪"
        description="对比新旧监管制度文档的报表与字段差异、定位受影响规则并一键增量重跑，或对比两次验证批次的规则与 Oracle SQL 变化。"
        actions={
          activeTab === 'revisions' && detail ? (
            <button
              type="button"
              className="button secondary"
              onClick={() => {
                const qs = new URLSearchParams({ view: 'fields', table: detail.id })
                if (detail.document_id) qs.set('document_id', detail.document_id)
                navigate(`/documents?${qs.toString()}`)
              }}
            >
              打开此表 →
            </button>
          ) : null
        }
      />

      <StatusMessage error={error} notice={notice} onClose={() => { setError(''); setNotice('') }} />

      <nav className="validation-tab-bar" style={{ marginBottom: 16 }}>
        <button type="button" className={activeTab === 'doc-diff' ? 'active' : ''} onClick={() => setActiveTab('doc-diff')}>
          制度文档版本对比 & 影响面
        </button>
        <button type="button" className={activeTab === 'run-diff' ? 'active' : ''} onClick={() => setActiveTab('run-diff')}>
          验证批次规则与 SQL 对比
        </button>
        <button type="button" className={activeTab === 'revisions' ? 'active' : ''} onClick={() => setActiveTab('revisions')}>
          单表修订记录 ({tables.length})
        </button>
      </nav>

      {activeTab === 'doc-diff' && (
        <section className="panel">
          <div className="panel-heading">
            <div>
              <h2>跨版本监管制度对比</h2>
              <p>选择基线版本与新版本文档，自动比对每张表的增删改、字段类型/长度/必填/校验变更，并分级标注影响面。</p>
            </div>
            <div className="actions">
              <button
                type="button"
                className="button primary"
                disabled={diffBusy || !baseDocId || !targetDocId}
                onClick={compareDocuments}
              >
                {diffBusy ? '正在比对...' : '执行版本对比'}
              </button>
            </div>
          </div>

          <div className="list-filter-bar" style={{ marginBottom: 14 }}>
            <label style={{ flex: 1 }}>
              <small className="muted-text">基线文档（旧版）</small>
              <select value={baseDocId} onChange={(e) => setBaseDocId(e.target.value)} style={{ width: '100%' }}>
                <option value="">请选择基线文档</option>
                {documents.map((doc) => (
                  <option key={doc.id} value={doc.id}>
                    {doc.file_name}（{doc.spec_version || 'UNKNOWN'} · {doc.block_count} 块）
                  </option>
                ))}
              </select>
            </label>
            <label style={{ flex: 1 }}>
              <small className="muted-text">目标文档（新版）</small>
              <select value={targetDocId} onChange={(e) => setTargetDocId(e.target.value)} style={{ width: '100%' }}>
                <option value="">请选择新版文档</option>
                {documents.map((doc) => (
                  <option key={doc.id} value={doc.id}>
                    {doc.file_name}（{doc.spec_version || 'UNKNOWN'} · {doc.block_count} 块）
                  </option>
                ))}
              </select>
            </label>
          </div>

          {docDiff ? (
            <>
              <div className="audit-metrics" style={{ marginBottom: 14 }}>
                <div><span>新增 / 下线报表</span><strong>+{docDiff.summary.added_tables} / -{docDiff.summary.removed_tables}</strong></div>
                <div><span>变更报表</span><strong>{docDiff.summary.modified_tables} 张</strong><small>无变化 {docDiff.summary.unchanged_tables} 张</small></div>
                <div><span>破坏性变更</span><strong>{docDiff.summary.breaking_tables} 张</strong><small>规则受影响 {docDiff.summary.rule_impact_tables} 张</small></div>
                <div><span>建议增量重跑</span><strong>{docDiff.summary.affected_table_count} 张</strong></div>
              </div>

              {docDiff.affected_table_ids?.length > 0 && (
                <div className="action-callout" style={{ marginBottom: 14 }}>
                  <div>
                    <strong>识别到 {docDiff.affected_table_ids.length} 张受影响报表需要重新装配规则</strong>
                    <p>仅对新增与受影响的报表发起 Step 2 增量规则生成，未变更报表保持历史产物不动。</p>
                  </div>
                  <button
                    type="button"
                    className="button primary"
                    disabled={genBusy}
                    onClick={runIncrementalGeneration}
                  >
                    {genBusy ? '正在发起...' : `一键增量生成 (${docDiff.affected_table_ids.length} 张)`}
                  </button>
                </div>
              )}

              <div className="list-filter-bar" style={{ marginBottom: 10 }}>
                <select value={docDiffFilter} onChange={(e) => setDocDiffFilter(e.target.value)}>
                  <option value="CHANGED">仅看有变化报表</option>
                  <option value="BREAKING">仅看破坏性变更</option>
                  <option value="ALL">全部报表 ({docDiff.tables.length})</option>
                </select>
              </div>

              {visibleDocTables.length > 0 ? (
                <div className="list-stack">
                  {visibleDocTables.map((tbl) => {
                    const cMeta = CHANGE_KIND_META[tbl.change_kind] || { label: tbl.change_kind, tone: 'neutral' }
                    const iMeta = IMPACT_META[tbl.impact] || { label: tbl.impact, tone: 'neutral' }
                    return (
                      <details className="run-section" key={`${tbl.change_kind}-${tbl.table_code}`} open={tbl.impact === 'BREAKING'}>
                        <summary style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8 }}>
                          <span>
                            <strong>{tbl.table_name || tbl.table_code}</strong> <code>{tbl.table_code}</code>
                            {' · '}字段 {tbl.field_counts.base} → {tbl.field_counts.target}
                          </span>
                          <span className="actions">
                            <StatusBadge tone={cMeta.tone}>{cMeta.label}</StatusBadge>
                            <StatusBadge tone={iMeta.tone}>{iMeta.label}</StatusBadge>
                          </span>
                        </summary>
                        <div style={{ marginTop: 10 }}>
                          {tbl.added_fields.length > 0 && (
                            <p className="muted-text">
                              <strong>新增字段 ({tbl.added_fields.length})：</strong>
                              {tbl.added_fields.map((f) => `${f.field_en}(${f.data_type}(${f.length})${f.required ? ',必填' : ''})`).join('、')}
                            </p>
                          )}
                          {tbl.removed_fields.length > 0 && (
                            <p className="danger-text">
                              <strong>删除字段 ({tbl.removed_fields.length})：</strong>
                              {tbl.removed_fields.map((f) => f.field_en).join('、')}
                            </p>
                          )}
                          {tbl.modified_fields.length > 0 && (
                            <div className="table-wrap" style={{ marginTop: 8 }}>
                              <table>
                                <thead>
                                  <tr><th>字段</th><th>影响等级</th><th>具体变化（旧 → 新）</th></tr>
                                </thead>
                                <tbody>
                                  {tbl.modified_fields.map((mf) => {
                                    const fm = IMPACT_META[mf.impact] || { label: mf.impact, tone: 'neutral' }
                                    return (
                                      <tr key={mf.field_en}>
                                        <td><code>{mf.field_en}</code><small>{mf.field_cn}</small></td>
                                        <td><StatusBadge tone={fm.tone}>{fm.label}</StatusBadge></td>
                                        <td>
                                          {mf.changes.map((c, idx) => (
                                            <div key={idx}>
                                              <strong>{ATTR_LABELS[c.attr] || c.attr}：</strong>
                                              <code>{String(c.old)}</code> → <code>{String(c.new)}</code>
                                            </div>
                                          ))}
                                        </td>
                                      </tr>
                                    )
                                  })}
                                </tbody>
                              </table>
                            </div>
                          )}
                          {tbl.change_kind === 'UNCHANGED' && <p className="muted-text">该表字段结构与校验原文完全一致。</p>}
                        </div>
                      </details>
                    )
                  })}
                </div>
              ) : (
                <div className="empty">当前筛选条件下没有差异报表。</div>
              )}
            </>
          ) : (
            <div className="empty">选择两份已导入的监管规范文档，点击「执行版本对比」即可查看字段级 Diff 与影响面分析。</div>
          )}
        </section>
      )}

      {activeTab === 'run-diff' && (
        <section className="panel">
          <div className="panel-heading">
            <div>
              <h2>验证批次规则与 Oracle SQL 对比</h2>
              <p>选择两个历史验证批次，逐条对比新增、删除和参数/SQL 发生变化的规则。</p>
            </div>
            <button
              type="button"
              className="button primary"
              disabled={runDiffBusy || !baseRunId || !targetRunId}
              onClick={compareRuns}
            >
              {runDiffBusy ? '正在比对...' : '对比规则差异'}
            </button>
          </div>

          <div className="list-filter-bar" style={{ marginBottom: 14 }}>
            <label style={{ flex: 1 }}>
              <small className="muted-text">基线批次</small>
              <select value={baseRunId} onChange={(e) => setBaseRunId(e.target.value)} style={{ width: '100%' }}>
                <option value="">请选择基线批次</option>
                {runs.map((r) => (
                  <option key={r.id} value={r.id}>
                    {r.id.slice(0, 8)} · {(r.tables || []).map((t) => t.table_code).join(', ')} · {formatTime(r.created_at)} ({r.status})
                  </option>
                ))}
              </select>
            </label>
            <label style={{ flex: 1 }}>
              <small className="muted-text">目标批次</small>
              <select value={targetRunId} onChange={(e) => setTargetRunId(e.target.value)} style={{ width: '100%' }}>
                <option value="">请选择目标批次</option>
                {runs.map((r) => (
                  <option key={r.id} value={r.id}>
                    {r.id.slice(0, 8)} · {(r.tables || []).map((t) => t.table_code).join(', ')} · {formatTime(r.created_at)} ({r.status})
                  </option>
                ))}
              </select>
            </label>
          </div>

          {runDiff ? (
            <>
              <div className="audit-metrics" style={{ marginBottom: 14 }}>
                <div><span>新增规则</span><strong>+{runDiff.summary.added}</strong></div>
                <div><span>移除规则</span><strong>-{runDiff.summary.removed}</strong></div>
                <div><span>参数 / SQL 变更</span><strong>{runDiff.summary.modified}</strong></div>
                <div><span>保持不变</span><strong>{runDiff.summary.unchanged}</strong></div>
              </div>

              {runDiff.modified.length > 0 && (
                <div className="table-wrap" style={{ marginBottom: 14 }}>
                  <h3 style={{ marginBottom: 8 }}>变更规则 ({runDiff.modified.length})</h3>
                  <table>
                    <thead><tr><th>表 / 字段</th><th>规则 / 算子</th><th>差异详情（旧 → 新）</th></tr></thead>
                    <tbody>
                      {runDiff.modified.map((item) => (
                        <tr key={`${item.table_code}-${item.rule_key}`}>
                          <td><strong>{item.table_code}</strong><small>{item.field}</small></td>
                          <td><code>{item.operator}</code><small>{item.rule_key}</small></td>
                          <td>
                            {item.changes.map((c, idx) => (
                              <div key={idx} style={{ marginBottom: 4 }}>
                                <strong>{c.attr}：</strong>
                                <code>{typeof c.old === 'object' ? JSON.stringify(c.old) : String(c.old)}</code>
                                {' → '}
                                <code>{typeof c.new === 'object' ? JSON.stringify(c.new) : String(c.new)}</code>
                              </div>
                            ))}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}

              {runDiff.added.length > 0 && (
                <div className="table-wrap" style={{ marginBottom: 14 }}>
                  <h3 style={{ marginBottom: 8 }}>新增规则 ({runDiff.added.length})</h3>
                  <table>
                    <thead><tr><th>表 / 字段</th><th>规则 / 算子</th><th>编译后 Oracle WHERE</th></tr></thead>
                    <tbody>
                      {runDiff.added.map((r) => (
                        <tr key={r.rule_hash}>
                          <td><strong>{r.table_code}</strong><small>{r.field}</small></td>
                          <td><code>{r.operator}</code><small>{r.rule_key}</small></td>
                          <td><code>{r.sql || JSON.stringify(r.params)}</code></td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}

              {runDiff.removed.length > 0 && (
                <div className="table-wrap">
                  <h3 style={{ marginBottom: 8 }}>移除规则 ({runDiff.removed.length})</h3>
                  <table>
                    <thead><tr><th>表 / 字段</th><th>规则 / 算子</th><th>原 Oracle WHERE</th></tr></thead>
                    <tbody>
                      {runDiff.removed.map((r) => (
                        <tr key={r.rule_hash}>
                          <td><strong>{r.table_code}</strong><small>{r.field}</small></td>
                          <td><code>{r.operator}</code><small>{r.rule_key}</small></td>
                          <td><code>{r.sql || JSON.stringify(r.params)}</code></td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}

              {runDiff.added.length === 0 && runDiff.removed.length === 0 && runDiff.modified.length === 0 && (
                <div className="empty">两个批次的有效规则集合与编译 SQL 完全一致。</div>
              )}
            </>
          ) : (
            <div className="empty">选择两个历史验证批次，点击「对比规则差异」查看新增/移除/改参规则与 SQL 变化。</div>
          )}
        </section>
      )}

      {activeTab === 'revisions' && (
        <section className="workspace-grid versions-layout">
          <div className="panel list-panel">
            <div className="panel-heading">
              <div>
                <h2>选择报表</h2>
                <p>共 {tables.length} 张表</p>
              </div>
              <StatusBadge tone="neutral">{tables.length}</StatusBadge>
            </div>

            <div className="list-filters" style={{ marginBottom: 10 }}>
              <input
                type="search"
                placeholder="搜索报表名称或代码..."
                value={tableSearch}
                onChange={(e) => setTableSearch(e.target.value)}
              />
            </div>

            {loading ? (
              <ListSkeleton rows={6} />
            ) : visibleTables.length > 0 ? (
              <div className="list-stack">
                {visibleTables.map((table) => (
                  <button
                    type="button"
                    className={`list-row ${(selectedId || detail?.id) === table.id ? 'selected' : ''}`}
                    key={table.id}
                    onClick={() => selectTable(table.id)}
                  >
                    <div className="list-row-top">
                      <strong>{table.table_name}</strong>
                      <StatusBadge status={table.status} />
                    </div>
                    <span>
                      <code>{table.table_code}</code> · 当前 v{table.current_version}
                    </span>
                    <em>{table.published_version ? `已发布 v${table.published_version}` : '未发布'}</em>
                  </button>
                ))}
              </div>
            ) : (
              <div className="empty">
                {tableSearch ? '没有匹配的报表。' : '暂无元数据表版本记录。'}
              </div>
            )}
          </div>

          <div className={`panel ${detailLoading && detail ? 'panel-transitioning' : ''}`}>
            {detailLoading && !detail ? (
              <DetailSkeleton blocks={4} />
            ) : detail ? (
              <>
                <div className="panel-heading">
                  <div>
                    <div className="actions">
                      <h2>{detail.table_name}</h2>
                      <code>{detail.table_code}</code>
                      <StatusBadge status={detail.status} />
                    </div>
                    <p>
                      当前版本 v{detail.current_version} · 发布基线{' '}
                      {detail.published_version ? `v${detail.published_version}` : '未发布'} · 共{' '}
                      {detail.revisions.length} 条变更记录
                    </p>
                  </div>
                  <div className="actions">
                    <select
                      value={filterKind}
                      onChange={(e) => setFilterKind(e.target.value)}
                      aria-label="筛选变更类型"
                    >
                      <option value="ALL">全部变更类型 ({detail.revisions.length})</option>
                      <option value="HUMAN_EDIT">人工修改</option>
                      <option value="HUMAN_ADD">新增字段</option>
                      <option value="HUMAN_DELETE">删除字段</option>
                      <option value="DRAFT_CREATED">创建修改版本</option>
                      <option value="PUBLISHED">发布记录</option>
                      <option value="AI_EXTRACTION">自动识别</option>
                      <option value="AI_REVIEW">AI 复核</option>
                    </select>
                  </div>
                </div>

                {revisions.length > 0 ? (
                  <div className="revision-list">
                    {revisions.map((item, index) => (
                      <RevisionDiffView key={index} revision={item} />
                    ))}
                  </div>
                ) : (
                  <div className="empty">当前筛选条件下没有变更记录。</div>
                )}
              </>
            ) : (
              <div className="empty">请从左侧选择一张表查看版本演进与字段级差异。</div>
            )}
          </div>
        </section>
      )}
    </>
  )
}
