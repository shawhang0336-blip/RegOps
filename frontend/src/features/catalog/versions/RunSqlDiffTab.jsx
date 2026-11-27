import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { diffRuns } from '../../../api/delivery.js'
import CopyButton from '../../../components/CopyButton.jsx'
import SelectDropdown from '../../../components/SelectDropdown.jsx'
import { DetailSkeleton } from '../../../components/Skeleton.jsx'
import { StatusBadge } from '../../../components/StatusBadge.jsx'
import { regulatorMeta } from '../../../utils/activeDocument.js'
import { formatTime } from '../../../utils/dateTime.js'
import { STATUS_META } from '../../../utils/labels.js'

const RUN_DIFF_ATTR_LABELS = {
  params: '规则参数',
  status: '审批状态',
  source: '规则来源',
  sql: 'Oracle 生产 WHERE SQL',
}

function formatRunDiffVal(attr, val) {
  if (val === null || val === undefined || val === '') return '（空）'
  if (attr === 'status' || attr === 'source') {
    return STATUS_META[val]?.label ? `${STATUS_META[val].label} (${val})` : String(val)
  }
  if (typeof val === 'object') return JSON.stringify(val, null, 2)
  return String(val)
}

export default function RunSqlDiffTab({ runs, onError }) {
  const navigate = useNavigate()
  const [tableFilter, setTableFilter] = useState('ALL')
  const [baseRunId, setBaseRunId] = useState('')
  const [targetRunId, setTargetRunId] = useState('')
  const [diffData, setDiffData] = useState(null)
  const [busy, setBusy] = useState(false)

  // 统计每张报表有多少个历史验证批次，优先推荐可同表对比的报表
  const tableGroups = useMemo(() => {
    const map = new Map()
    for (const run of runs) {
      for (const tbl of run.tables || []) {
        if (!tbl.table_code) continue
        const cur = map.get(tbl.table_code) || {
          code: tbl.table_code,
          name: tbl.table_name || tbl.table_code,
          count: 0,
        }
        cur.count += 1
        map.set(tbl.table_code, cur)
      }
    }
    return [...map.values()].sort((a, b) => b.count - a.count || a.code.localeCompare(b.code))
  }, [runs])

  const comparableTableCount = useMemo(
    () => tableGroups.filter((g) => g.count >= 2).length,
    [tableGroups],
  )

  const tableFilterOptions = useMemo(
    () => [
      {
        value: 'ALL',
        label: '全部验证批次',
        meta: `${runs.length} 批次`,
        description: '显示当前范围内全部验证批次',
      },
      ...tableGroups.map((g) => ({
        value: g.code,
        badge: g.count >= 2 ? `${g.count} 批可对比` : '1 批',
        label: g.name,
        meta: g.code,
      })),
    ],
    [tableGroups, runs.length],
  )

  const candidateRuns = useMemo(() => {
    if (tableFilter === 'ALL') return runs
    return runs.filter((r) => (r.tables || []).some((t) => t.table_code === tableFilter))
  }, [runs, tableFilter])

  // 默认智能挑选“有同表历史批次”的一对，避免盲目凑两张毫不相干的表
  useEffect(() => {
    if (!candidateRuns.length) {
      setBaseRunId('')
      setTargetRunId('')
      setDiffData(null)
      return
    }
    if (candidateRuns.length === 1) {
      setTargetRunId(candidateRuns[0].id)
      setBaseRunId('')
      setDiffData(null)
      return
    }
    let defaultTarget = candidateRuns[0].id
    let defaultBase = candidateRuns[1].id
    if (tableFilter === 'ALL') {
      const multiTable = tableGroups.find((g) => g.count >= 2)
      if (multiTable) {
        const pair = candidateRuns.filter((r) => (r.tables || []).some((t) => t.table_code === multiTable.code))
        if (pair.length >= 2) {
          defaultTarget = pair[0].id
          defaultBase = pair[1].id
        }
      }
    }
    setTargetRunId((prev) => (prev && candidateRuns.some((r) => r.id === prev) ? prev : defaultTarget))
    setBaseRunId((prev) => (prev && candidateRuns.some((r) => r.id === prev) ? prev : defaultBase))
  }, [candidateRuns, tableFilter, tableGroups])

  const executeDiff = useCallback(async (bId, tId) => {
    if (!bId || !tId || bId === tId) {
      setDiffData(null)
      return
    }
    setBusy(true)
    onError?.('')
    try {
      const data = await diffRuns(bId, tId)
      setDiffData(data)
    } catch (err) {
      onError?.(err.message || '对比验证批次规则差异失败')
    } finally {
      setBusy(false)
    }
  }, [onError])

  useEffect(() => {
    if (baseRunId && targetRunId && baseRunId !== targetRunId) {
      executeDiff(baseRunId, targetRunId)
    } else {
      setDiffData(null)
    }
  }, [baseRunId, targetRunId, executeDiff])

  const runOptions = useMemo(
    () =>
      candidateRuns.map((r) => {
        const rm = r.regulator ? regulatorMeta(r.regulator) : null
        const tablesText = (r.tables || []).map((t) => t.table_code).join('、') || '未命名表'
        const statusLabel = STATUS_META[r.status]?.label || r.status
        return {
          value: r.id,
          badge: rm ? rm.shortLabel : `#${r.id.slice(0, 6)}`,
          label: `${tablesText} · #${r.id.slice(0, 8)}`,
          tag: statusLabel,
          meta: formatTime(r.created_at),
          description: r.package_name || `规则验证批次 ${r.id.slice(0, 8)}`,
        }
      }),
    [candidateRuns],
  )

  return (
    <section className="panel ver-tab-panel">
      <div className="panel-heading">
        <div>
          <h2>规则与 Oracle 生产 SQL 对比</h2>
          <p>
            按同报表智能配对两次历史验证批次，逐条高亮新增、移除与改参后的 Oracle WHERE 生产校验语句。
          </p>
        </div>
        <div className="actions">
          <SelectDropdown
            ariaLabel="按报表筛选可对比批次"
            size="sm"
            minWidth={230}
            value={tableFilter}
            options={tableFilterOptions}
            onChange={setTableFilter}
          />
        </div>
      </div>

      {candidateRuns.length < 2 ? (
        <div className="ver-guidance-card">
          <div className="ver-guidance-badge">待形成多批次历史</div>
          <h3>
            {tableFilter === 'ALL'
              ? `当前范围内仅有 ${candidateRuns.length} 个验证批次，尚无法进行跨批次规则对比`
              : `报表「${tableFilter}」目前仅有 ${candidateRuns.length} 次验证记录`}
          </h3>
          <p>
            规则与 SQL 对比用于核对同一张报表在规则调整或制度更新前后的生产校验 SQL 差异。
            在步骤二修改规则并再次执行验证后，系统将在此自动配对新旧两次验证批次并逐条高亮 SQL 变化。
            {comparableTableCount > 0 && tableFilter !== 'ALL'
              ? `（当前范围内有 ${comparableTableCount} 张报表已有多次运行记录，可切换上方下拉框查看。）`
              : ''}
          </p>
        </div>
      ) : (
        <>
          <div className="ver-pair-bar">
            <div className="ver-pair-slot">
              <span className="ver-pair-label">基线批次（旧版）</span>
              <SelectDropdown
                ariaLabel="选择基线验证批次"
                value={baseRunId}
                options={runOptions}
                disabled={busy}
                onChange={setBaseRunId}
              />
            </div>

            <button
              type="button"
              className="button secondary sm ver-swap-btn"
              title="互换基线与目标批次"
              disabled={busy || !baseRunId || !targetRunId}
              onClick={() => {
                const prev = baseRunId
                setBaseRunId(targetRunId)
                setTargetRunId(prev)
              }}
            >
              ⇄ 互换
            </button>

            <div className="ver-pair-slot">
              <span className="ver-pair-label">目标批次（新版）</span>
              <SelectDropdown
                ariaLabel="选择目标验证批次"
                value={targetRunId}
                options={runOptions}
                disabled={busy}
                onChange={setTargetRunId}
              />
            </div>
          </div>

          {baseRunId && targetRunId && baseRunId === targetRunId && (
            <div className="action-callout" style={{ marginBottom: 12 }}>
              <div>
                <strong>左右选择了同一个验证批次</strong>
                <p>请选择两个不同的历史验证批次以对比规则与 SQL 变化。</p>
              </div>
            </div>
          )}

          {busy ? (
            <DetailSkeleton blocks={4} />
          ) : diffData ? (
            <>
              {Array.isArray(diffData.shared_tables) && diffData.shared_tables.length === 0 && (
                <div className="action-callout danger" style={{ marginBottom: 12 }}>
                  <div>
                    <strong>提示：所选两个批次没有重叠的监管报表</strong>
                    <p>
                      基线批次报表：{(diffData.base_run?.tables || []).map((t) => t.table_code).join('、') || '未知'}；
                      目标批次报表：{(diffData.target_run?.tables || []).map((t) => t.table_code).join('、') || '未知'}。
                      建议在右上角先按单张报表筛选后再做对比。
                    </p>
                  </div>
                </div>
              )}

              <div className="audit-metrics" style={{ marginBottom: 14 }}>
                <div>
                  <span>新增规则</span>
                  <strong className="ver-count-add">+{diffData.summary.added}</strong>
                  <small>基线共 {diffData.base_run?.rule_count ?? 0} 条</small>
                </div>
                <div>
                  <span>移除规则</span>
                  <strong className="ver-count-del">-{diffData.summary.removed}</strong>
                  <small>目标共 {diffData.target_run?.rule_count ?? 0} 条</small>
                </div>
                <div>
                  <span>参数 / SQL 变更</span>
                  <strong>{diffData.summary.modified} 条</strong>
                  <small>保持不变 {diffData.summary.unchanged} 条</small>
                </div>
                <div>
                  <span>审计溯源联动</span>
                  <div className="ver-audit-links">
                    <button
                      type="button"
                      className="button ghost sm"
                      onClick={() => navigate(`/audit?run=${encodeURIComponent(diffData.base_run.id)}`)}
                    >
                      基线审计 →
                    </button>
                    <button
                      type="button"
                      className="button ghost sm"
                      onClick={() => navigate(`/audit?run=${encodeURIComponent(diffData.target_run.id)}`)}
                    >
                      目标审计 →
                    </button>
                  </div>
                </div>
              </div>

              {diffData.modified.length > 0 && (
                <div className="ver-rule-diff-section">
                  <h3>参数或 Oracle SQL 变更规则 ({diffData.modified.length})</h3>
                  <div className="ver-sql-diff-list">
                    {diffData.modified.map((item) => (
                      <article className="ver-sql-diff-card" key={`${item.table_code}-${item.rule_key}`}>
                        <div className="ver-sql-diff-head">
                          <div className="actions">
                            <strong>{item.table_code}</strong>
                            <code>{item.field}</code>
                            <StatusBadge tone="amber">{item.operator}</StatusBadge>
                            <span className="muted-text">{item.rule_key}</span>
                          </div>
                          {item.target?.sql && (
                            <CopyButton text={item.target.sql} label="复制新 SQL" className="button ghost sm" />
                          )}
                        </div>
                        <div className="ver-sql-diff-changes">
                          {item.changes.map((c, idx) => (
                            <div className="ver-sql-change-block" key={idx}>
                              <span className="ver-sql-attr-label">{RUN_DIFF_ATTR_LABELS[c.attr] || c.attr}</span>
                              <div className="ver-sql-before-after">
                                <div className="ver-sql-box is-old">
                                  <em>旧版</em>
                                  <pre>{formatRunDiffVal(c.attr, c.old)}</pre>
                                </div>
                                <div className="ver-sql-box is-new">
                                  <em>新版</em>
                                  <pre>{formatRunDiffVal(c.attr, c.new)}</pre>
                                </div>
                              </div>
                            </div>
                          ))}
                        </div>
                      </article>
                    ))}
                  </div>
                </div>
              )}

              {diffData.added.length > 0 && (
                <div className="ver-rule-diff-section">
                  <h3>新增规则 ({diffData.added.length})</h3>
                  <div className="ver-sql-diff-list">
                    {diffData.added.map((r) => (
                      <article className="ver-sql-diff-card is-add" key={r.rule_hash}>
                        <div className="ver-sql-diff-head">
                          <div className="actions">
                            <StatusBadge tone="green">新增</StatusBadge>
                            <strong>{r.table_code}</strong>
                            <code>{r.field}</code>
                            <StatusBadge tone="blue">{r.operator}</StatusBadge>
                            <span className="muted-text">{r.rule_key}</span>
                          </div>
                          {r.sql && <CopyButton text={r.sql} label="复制 SQL" className="button ghost sm" />}
                        </div>
                        {r.rationale && <p className="ver-sql-rationale">{r.rationale}</p>}
                        <pre className="ver-sql-single is-new">{r.sql || JSON.stringify(r.params, null, 2)}</pre>
                      </article>
                    ))}
                  </div>
                </div>
              )}

              {diffData.removed.length > 0 && (
                <div className="ver-rule-diff-section">
                  <h3>移除规则 ({diffData.removed.length})</h3>
                  <div className="ver-sql-diff-list">
                    {diffData.removed.map((r) => (
                      <article className="ver-sql-diff-card is-del" key={r.rule_hash}>
                        <div className="ver-sql-diff-head">
                          <div className="actions">
                            <StatusBadge tone="red">移除</StatusBadge>
                            <strong>{r.table_code}</strong>
                            <code>{r.field}</code>
                            <StatusBadge tone="neutral">{r.operator}</StatusBadge>
                            <span className="muted-text">{r.rule_key}</span>
                          </div>
                          {r.sql && <CopyButton text={r.sql} label="复制原 SQL" className="button ghost sm" />}
                        </div>
                        {r.rationale && <p className="ver-sql-rationale">{r.rationale}</p>}
                        <pre className="ver-sql-single is-old">{r.sql || JSON.stringify(r.params, null, 2)}</pre>
                      </article>
                    ))}
                  </div>
                </div>
              )}

              {diffData.added.length === 0 &&
                diffData.removed.length === 0 &&
                diffData.modified.length === 0 && (
                  <div className="empty">两个批次的有效规则集合与编译后 Oracle WHERE SQL 完全一致。</div>
                )}
            </>
          ) : null}
        </>
      )}
    </section>
  )
}
