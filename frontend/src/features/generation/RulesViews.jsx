import { useState } from 'react'
import { submitRunApprovals } from '../../api/delivery.js'
import { StatusBadge } from '../../components/StatusBadge.jsx'
import { formatTime } from '../../utils/dateTime.js'
import RuleDrawer from './RuleDrawer.jsx'
import {
  ACTIVE_RUN_STATUSES,
  formatParams,
  groupStatusMeta,
  ruleStatusLabel,
  sortRules,
} from './artifactHelpers.js'

export function RuleTable({ rows, showTable = false, decisions = {}, onDecision, canApprove, onOpenRule, activeKey }) {
  return (
    <div className="table-wrap rule-table-wrap">
      <table className={`data-table rule-table ${showTable ? 'with-table-col' : ''}`}>
        <thead>
          <tr>
            {showTable && <th className="rule-table-col">报表</th>}
            <th>字段</th><th>规则说明</th><th>算子</th><th>参数</th><th>来源</th><th>状态</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((rule) => (
            <tr
              key={rule.row_id}
              className={`clickable-row ${activeKey === rule.row_id ? 'active-rule' : ''}`}
              onClick={(event) => {
                if (event.target.closest('button, a, input, select, textarea')) return
                onOpenRule(rule)
              }}
            >
              {showTable && (
                <td className="rule-table-col">
                  <strong>{rule._tableName}</strong>
                  <small><code>{rule._tableCode}</code></small>
                </td>
              )}
              <td><code className="rule-field">{rule.field}</code></td>
              <td><div className="rule-description" title={rule.rationale || rule.sql || ''}>{rule.rationale || rule.sql || '—'}</div></td>
              <td><StatusBadge tone="blue">{rule.operator_label}</StatusBadge></td>
              <td className="rule-param-cell" title={formatParams(rule.params)}>{formatParams(rule.params, true)}</td>
              <td>
                <StatusBadge tone={rule.source === 'DOC_EXPLICIT' ? 'green' : 'violet'}>
                  {rule.source === 'DOC_EXPLICIT' ? '原文明确' : 'AI 建议'}
                </StatusBadge>
              </td>
              <td>
                <StatusBadge tone={rule.status === 'APPROVED' ? 'green' : rule.status === 'PENDING' ? 'amber' : 'neutral'}>
                  {ruleStatusLabel(rule.status)}
                </StatusBadge>
              </td>
              <td>
                <div className="rule-row-actions">
                  {canApprove?.(rule) && onDecision && (
                    <>
                      <button type="button" className={`button sm ${decisions[rule.rule_hash] === 'APPROVED' ? 'primary' : 'secondary'}`} onClick={(e) => { e.stopPropagation(); onDecision(rule.rule_hash, 'APPROVED') }}>批准</button>
                      <button type="button" className={`button sm ${decisions[rule.rule_hash] === 'REJECTED' ? 'danger' : 'secondary'}`} onClick={(e) => { e.stopPropagation(); onDecision(rule.rule_hash, 'REJECTED') }}>拒绝</button>
                    </>
                  )}
                  <button type="button" className="button-link" onClick={(e) => { e.stopPropagation(); onOpenRule(rule) }}>查看 / 编辑</button>
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length === 0 && <div className="empty table-empty">没有符合筛选条件的规则。</div>}
    </div>
  )
}

export function RulesView({ rules, runId, runStatus, onSaved, onContinue, onNotice, onError }) {
  const [operatorFilter, setOperatorFilter] = useState('all')
  const [fieldSearch, setFieldSearch] = useState('')
  const [activeRuleRowId, setActiveRuleRowId] = useState(null)
  const [decisions, setDecisions] = useState({})
  const [approvalBusy, setApprovalBusy] = useState(false)
  const [showSuperseded, setShowSuperseded] = useState(false)
  const runActive = ACTIVE_RUN_STATUSES.includes(runStatus)
  const pendingRules = rules.filter((rule) => rule.status === 'PENDING')
  const decidedCount = pendingRules.filter((rule) => decisions[rule.rule_hash]).length

  function setAllDecisions(decision) {
    setDecisions(Object.fromEntries(pendingRules.map((rule) => [rule.rule_hash, decision])))
  }

  async function submitApprovals() {
    if (pendingRules.some((rule) => !decisions[rule.rule_hash])) {
      onError?.('请先对每条待确认规则选择“批准”或“拒绝”。')
      return
    }
    setApprovalBusy(true)
    try {
      const body = await submitRunApprovals(
        runId,
        pendingRules.map((rule) => ({ rule_hash: rule.rule_hash, decision: decisions[rule.rule_hash] })),
      )
      setDecisions({})
      if (body.status === 'WAITING_APPROVAL') {
        onNotice?.('本表规则已确认；整批仍等待其他报表处理。')
        onSaved?.()
      } else {
        onNotice?.('规则确认完成，AI 将继续生成测试数据并执行 Oracle 验证。')
        onContinue?.()
      }
    } catch (err) {
      onError?.(err.message)
    } finally {
      setApprovalBusy(false)
    }
  }

  if (!rules.length) {
    return <div className="empty">这张表还没有生成校验规则。</div>
  }

  const activeRules = rules.filter((rule) => rule.status !== 'SUPERSEDED')
  const supersededRules = rules.filter((rule) => rule.status === 'SUPERSEDED')
  const source = showSuperseded ? rules : activeRules

  const sorted = sortRules(source)
  const operatorCounts = {}
  activeRules.forEach((rule) => {
    operatorCounts[rule.operator] = (operatorCounts[rule.operator] || 0) + 1
  })
  const visible = sorted.filter((rule) => {
    if (operatorFilter !== 'all' && rule.operator !== operatorFilter) return false
    const q = fieldSearch.trim().toLowerCase()
    if (q && !rule.field.toLowerCase().includes(q) && !rule.rule_key.toLowerCase().includes(q)) return false
    return true
  })
  const activeIndex = visible.findIndex((rule) => rule.row_id === activeRuleRowId)
  const activeRule = activeIndex >= 0 ? visible[activeIndex] : null

  return (
    <div className="rules-workspace">
      <div className="rules-section-heading">
        <div>
          <h3>校验规则 <span>{activeRules.length} 条</span></h3>
          <p>点击“查看 / 编辑”在右侧抽屉中检查参数、SQL 和原文依据，可连续切换上一条或下一条。</p>
        </div>
        <span>当前显示 {visible.length} 条</span>
      </div>
      {pendingRules.length > 0 && runStatus === 'WAITING_APPROVAL' && (
        <div className="action-callout rule-approval-callout">
          <div>
            <strong>本表有 {pendingRules.length} 条规则待确认</strong>
            <p>已选择 {decidedCount} / {pendingRules.length} 条。批准或拒绝都在第二步完成，整批处理完后自动进入 AI 测试。</p>
          </div>
          <div className="actions">
            <button type="button" className="button secondary sm" disabled={approvalBusy} onClick={() => setAllDecisions('APPROVED')}>全部批准</button>
            <button type="button" className="button secondary sm" disabled={approvalBusy} onClick={() => setAllDecisions('REJECTED')}>全部拒绝</button>
            <button type="button" className="button primary sm" disabled={approvalBusy || decidedCount !== pendingRules.length} onClick={submitApprovals}>
              {approvalBusy ? '正在提交...' : '提交确认并继续测试'}
            </button>
          </div>
        </div>
      )}
      <div className="list-filter-bar rule-filter-bar">
        <select aria-label="规则算子" value={operatorFilter} onChange={(e) => { setOperatorFilter(e.target.value); setActiveRuleRowId(null) }}>
          <option value="all">全部算子 ({activeRules.length})</option>
          {Object.entries(operatorCounts).map(([op, count]) => (
            <option key={op} value={op}>{rules.find((rule) => rule.operator === op)?.operator_label || op} ({count})</option>
          ))}
        </select>
        <input
          type="search"
          placeholder="按字段或规则名检索..."
          value={fieldSearch}
          onChange={(e) => { setFieldSearch(e.target.value); setActiveRuleRowId(null) }}
        />
        {supersededRules.length > 0 && (
          <button
            type="button"
            className={`button sm ${showSuperseded ? 'secondary' : 'ghost'}`}
            aria-pressed={showSuperseded}
            title="被去重替换的重复规则，默认不参与校验"
            onClick={() => { setShowSuperseded(!showSuperseded); setActiveRuleRowId(null) }}
          >
            {showSuperseded ? '隐藏' : '显示'}已替换 ({supersededRules.length})
          </button>
        )}
      </div>

      <RuleTable
        rows={visible}
        decisions={decisions}
        onDecision={(hash, decision) => setDecisions({ ...decisions, [hash]: decision })}
        canApprove={(rule) => rule.status === 'PENDING' && runStatus === 'WAITING_APPROVAL'}
        onOpenRule={(rule) => setActiveRuleRowId(rule.row_id)}
        activeKey={activeRule?.row_id}
      />

      {activeRule && (
        <RuleDrawer
          key={activeRule.row_id}
          rule={activeRule}
          index={activeIndex}
          total={visible.length}
          canEdit={!runActive && activeRule.status !== 'SUPERSEDED'}
          editDisabledReason={runActive ? '当前运行批次仍在执行中，不能修改规则参数' : ''}
          onPrevious={activeIndex > 0 ? () => setActiveRuleRowId(visible[activeIndex - 1].row_id) : null}
          onNext={activeIndex < visible.length - 1 ? () => setActiveRuleRowId(visible[activeIndex + 1].row_id) : null}
          onClose={() => setActiveRuleRowId(null)}
          onSaved={onSaved}
          onNotice={onNotice}
        />
      )}
    </div>
  )
}

export function AllRulesView({ overview, loading, refreshedAt, refreshing, onRefresh, onFocus, onNotice, onError, onSaved, onGoList }) {
  const [tableSearch, setTableSearch] = useState('')
  const [queueOpen, setQueueOpen] = useState(false)
  const [activeRuleId, setActiveRuleId] = useState(null)
  const [decisions, setDecisions] = useState({})
  const [approvalBusy, setApprovalBusy] = useState(false)

  if (loading) {
    return <div className="empty">正在加载全部规则...</div>
  }
  const groups = (overview?.tables || []).map((group) => {
    const rows = group.rules.map((rule) => ({
      ...rule,
      _tableId: group.table.id,
      _tableCode: group.table.table_code,
      _tableName: group.table.table_name,
      _runId: group.run.id,
      _runStatus: group.run.status,
    }))
    return {
      ...group,
      rows,
      aiCount: rows.filter((rule) => rule.status !== 'SUPERSEDED' && rule.source === 'AI_EXPANDED').length,
      docCount: rows.filter((rule) => rule.status !== 'SUPERSEDED' && rule.source === 'DOC_EXPLICIT').length,
    }
  })

  if (!groups.length) {
    return (
      <div className="empty">
        <p>还没有生成过任何校验规则。</p>
        <button type="button" className="button primary" onClick={onGoList}>去报表清单选择报表生成</button>
      </div>
    )
  }

  const totals = {
    tables: groups.length,
    active: groups.reduce((sum, group) => sum + group.active_count, 0),
    pending: groups.reduce((sum, group) => sum + group.pending_count, 0),
    ai: groups.reduce((sum, group) => sum + group.aiCount, 0),
  }
  const anyRunning = groups.some((group) => ACTIVE_RUN_STATUSES.includes(group.run.status))
  const pendingRows = sortRules(
    groups.flatMap((group) => group.rows).filter((rule) => rule.status === 'PENDING' && rule._runStatus === 'WAITING_APPROVAL'),
  )
  const pendingTableCount = new Set(pendingRows.map((rule) => rule._tableId)).size
  const decidedCount = pendingRows.filter((rule) => decisions[rule.rule_hash]).length

  const q = tableSearch.trim().toLowerCase()
  const visibleGroups = groups.filter((group) => {
    if (!q) return true
    return group.table.table_code.toLowerCase().includes(q) || String(group.table.table_name).toLowerCase().includes(q)
  })

  const activeIndex = pendingRows.findIndex((rule) => rule.row_id === activeRuleId)
  const activeRule = activeIndex >= 0 ? pendingRows[activeIndex] : null

  function setAllDecisions(decision) {
    setDecisions(Object.fromEntries(pendingRows.map((rule) => [rule.rule_hash, decision])))
  }

  async function submitApprovals() {
    if (pendingRows.some((rule) => !decisions[rule.rule_hash])) {
      onError?.('请先对每条待确认规则选择“批准”或“拒绝”。')
      return
    }
    setApprovalBusy(true)
    try {
      const byRun = new Map()
      pendingRows.forEach((rule) => {
        const list = byRun.get(rule._runId) || []
        list.push({ rule_hash: rule.rule_hash, decision: decisions[rule.rule_hash] })
        byRun.set(rule._runId, list)
      })
      const results = []
      try {
        for (const [runId, list] of byRun) {
          results.push(await submitRunApprovals(runId, list))
        }
      } catch (err) {
        if (results.length > 0) onSaved?.()
        throw err
      }
      setDecisions({})
      if (results.some((body) => body.status === 'WAITING_APPROVAL')) {
        onNotice?.('规则已确认；仍有其他待确认项处理完后才会进入 AI 测试。')
      } else {
        onNotice?.('规则确认完成，AI 将继续生成测试数据并执行 Oracle 验证，可到第三步查看。')
      }
      onSaved?.()
    } catch (err) {
      onError?.(err.message)
    } finally {
      setApprovalBusy(false)
    }
  }

  return (
    <div className="rules-workspace rules-overview">
      <div className="rules-section-heading">
        <div>
          <h3>全部校验规则 <span>{totals.active} 条 · {totals.tables} 张表</span></h3>
          <p>按表汇总最近一次生成的规则；点规则条数或表名进入这张表的规则明细。</p>
        </div>
        <div className="actions">
          {refreshedAt && (
            <small className="refresh-stamp">
              {anyRunning && <span className="live-dot" aria-hidden="true" />}
              更新于 {refreshedAt.toLocaleTimeString('zh-CN', { hour12: false })}
            </small>
          )}
          <button type="button" className="button ghost sm" disabled={refreshing} onClick={onRefresh}>
            {refreshing ? '刷新中...' : '刷新规则'}
          </button>
        </div>
      </div>

      {pendingRows.length > 0 && (
        <div className="action-callout rule-approval-callout">
          <div>
            <strong>共 {pendingRows.length} 条规则待确认，分布在 {pendingTableCount} 张表</strong>
            <p>已选择 {decidedCount} / {pendingRows.length} 条。全部决定后一次提交，确认完成后自动进入 AI 测试。</p>
          </div>
          <div className="actions">
            <button type="button" className="button secondary sm" onClick={() => setQueueOpen(!queueOpen)}>
              {queueOpen ? '收起队列' : '展开队列'}
            </button>
            <button type="button" className="button secondary sm" disabled={approvalBusy} onClick={() => setAllDecisions('APPROVED')}>全部批准</button>
            <button type="button" className="button secondary sm" disabled={approvalBusy} onClick={() => setAllDecisions('REJECTED')}>全部拒绝</button>
            <button type="button" className="button primary sm" disabled={approvalBusy || decidedCount !== pendingRows.length} onClick={submitApprovals}>
              {approvalBusy ? '正在提交...' : '提交确认并继续测试'}
            </button>
          </div>
        </div>
      )}

      {pendingRows.length > 0 && queueOpen && (
        <section className="pending-queue">
          <RuleTable
            rows={pendingRows}
            showTable
            decisions={decisions}
            onDecision={(hash, decision) => setDecisions({ ...decisions, [hash]: decision })}
            canApprove={() => true}
            onOpenRule={(rule) => setActiveRuleId(rule.row_id)}
            activeKey={activeRule?.row_id}
          />
        </section>
      )}

      <div className="list-filter-bar rule-filter-bar">
        <input
          type="search"
          placeholder="按报表名称或表代码检索..."
          value={tableSearch}
          onChange={(e) => setTableSearch(e.target.value)}
        />
        <span className="rule-list-hint">AI 建议 {totals.ai} 条 · 待确认 {totals.pending} 条</span>
      </div>

      <div className="table-wrap report-table-wrap">
        <table className="data-table report-table rules-table-summary">
          <thead>
            <tr>
              <th>报表</th>
              <th className="num-col">校验规则</th>
              <th className="num-col">AI 建议</th>
              <th className="num-col">待确认</th>
              <th className="num-col">已替换</th>
              <th>生成状态</th>
              <th>Oracle 空跑</th>
              <th>最近生成</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {visibleGroups.map((group) => {
              const [tone, label] = groupStatusMeta(group.run.status)
              return (
                <tr key={group.table.id}>
                  <td className="report-name-cell">
                    <button type="button" className="report-name-button" onClick={() => onFocus(group.table.id)}>
                      {group.table.table_name}
                    </button>
                    <small><code>{group.table.table_code}</code></small>
                  </td>
                  <td className="num-col">
                    <button
                      type="button"
                      className="rule-count-link"
                      title={`查看 ${group.table.table_name} 的 ${group.active_count} 条校验规则`}
                      onClick={() => onFocus(group.table.id)}
                    >
                      {group.active_count} 条
                    </button>
                  </td>
                  <td className="num-col">
                    {group.aiCount > 0 ? <StatusBadge tone="violet">{group.aiCount} 条</StatusBadge> : <span className="muted-text">—</span>}
                  </td>
                  <td className="num-col">
                    {group.pending_count > 0 ? <StatusBadge tone="amber">{group.pending_count} 条</StatusBadge> : <span className="muted-text">—</span>}
                  </td>
                  <td className="num-col">
                    {group.superseded_count > 0 ? <span className="muted-text">{group.superseded_count} 条</span> : <span className="muted-text">—</span>}
                  </td>
                  <td><StatusBadge tone={tone}>{label}</StatusBadge></td>
                  <td>
                    {group.precheck ? (
                      <StatusBadge tone={group.precheck.all_passed ? 'green' : 'red'}>
                        {group.precheck.all_passed ? '通过' : '未通过'}
                      </StatusBadge>
                    ) : <span className="muted-text">未空跑</span>}
                  </td>
                  <td>{group.run.created_at ? <small>{formatTime(group.run.created_at)}</small> : '—'}</td>
                  <td>
                    <button type="button" className="button secondary sm" onClick={() => onFocus(group.table.id)}>查看规则</button>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
        {visibleGroups.length === 0 && <div className="empty table-empty">没有符合检索条件的报表。</div>}
      </div>

      {activeRule && (
        <RuleDrawer
          key={activeRule.row_id}
          rule={activeRule}
          index={activeIndex}
          total={pendingRows.length}
          canEdit={!ACTIVE_RUN_STATUSES.includes(activeRule._runStatus) && activeRule.status !== 'SUPERSEDED'}
          editDisabledReason={ACTIVE_RUN_STATUSES.includes(activeRule._runStatus) ? '当前运行批次仍在执行中，不能修改规则参数' : ''}
          onPrevious={activeIndex > 0 ? () => setActiveRuleId(pendingRows[activeIndex - 1].row_id) : null}
          onNext={activeIndex < pendingRows.length - 1 ? () => setActiveRuleId(pendingRows[activeIndex + 1].row_id) : null}
          onClose={() => setActiveRuleId(null)}
          onSaved={onSaved}
          onNotice={onNotice}
        />
      )}
    </div>
  )
}
