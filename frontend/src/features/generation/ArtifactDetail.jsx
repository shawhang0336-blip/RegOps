import CopyButton from '../../components/CopyButton.jsx'
import { StatusBadge } from '../../components/StatusBadge.jsx'

export function ProductStatusBar({ detail, activeView, onSelect, onShowRules }) {
  const hasRun = Boolean(detail.run)
  const running = hasRun && !['COMPLETED', 'FAILED'].includes(detail.run.status)
  const activeRules = detail.rules.filter((rule) => rule.status !== 'SUPERSEDED')
  const pendingRules = activeRules.filter((rule) => rule.status === 'PENDING')
  const cards = [
    {
      key: 'ddl',
      label: '表结构 DDL',
      value: !hasRun ? '待生成' : detail.ddl ? '已生成' : '未生成',
      hint: detail.ddl ? '点击查看 Oracle 建表 SQL' : '查看表结构产物状态',
      tone: !hasRun ? 'neutral' : detail.ddl ? 'green' : 'red',
    },
    {
      key: 'codes',
      label: '码表',
      value: !hasRun ? '待生成' : detail.code_sets.length ? `${detail.code_sets.length} 张已迁移` : '无关联码表',
      hint: detail.code_sets.length ? '点击查看码值和 Oracle 迁移数量' : '查看码表产物状态',
      tone: !hasRun || !detail.code_sets.length ? 'neutral' : 'green',
    },
    {
      key: 'rules',
      label: '校验规则',
      value: !hasRun ? '待生成' : `${activeRules.length} 条`,
      hint: pendingRules.length ? `${pendingRules.length} 条待确认` : '规则是当前工作台的主要内容',
      tone: !hasRun ? 'neutral' : activeRules.length ? 'green' : 'red',
    },
    {
      key: 'audit',
      label: '验证结果',
      value: detail.audit
        ? (detail.audit.all_passed ? '全部通过' : '验证未过')
        : detail.precheck
          ? (detail.precheck.all_passed ? '空跑通过' : '空跑未过')
          : running ? '验证中' : '未验证',
      hint: detail.audit
        ? `${detail.audit.case_count || 0} 条验证用例`
        : detail.precheck
          ? `${detail.precheck.rule_count || 0} 条规则 · ${detail.precheck.statement_count || 0} 条 SQL`
          : '查看验证产物状态',
      tone: detail.audit
        ? (detail.audit.all_passed ? 'green' : 'red')
        : detail.precheck
          ? (detail.precheck.all_passed ? 'green' : 'red')
          : running ? 'blue' : 'neutral',
    },
  ]

  return (
    <div className="product-status-grid" aria-label="当前报表产物状态">
      {cards.map((card) => (
        <button
          type="button"
          key={card.key}
          className={`product-status-card tone-${card.tone} ${activeView === card.key ? 'active' : ''}`}
          aria-pressed={activeView === card.key}
          onClick={() => (card.key === 'rules' ? onShowRules() : onSelect(activeView === card.key ? null : card.key))}
        >
          <span>{card.label}</span>
          <strong>{card.value}</strong>
          <small>{card.hint}</small>
        </button>
      ))}
    </div>
  )
}

export function AuditView({ audit }) {
  if (!audit) return <div className="empty">还没有跑过验证。</div>
  const passed = audit.all_passed
  return (
    <div className="artifact-block">
      <div className={`audit-banner ${passed ? 'ok' : 'bad'}`}>
        <strong>{passed ? '验证通过' : '验证未通过'}</strong>
        <span>
          {audit.case_count} 条用例 · {audit.active_rule_count} 条规则 · 覆盖率{' '}
          {audit.coverage_rate != null ? `${Math.round(audit.coverage_rate * 100)}%` : '—'}
        </span>
      </div>
      <div className="audit-metrics">
        <div><span>漏报（该拦没拦）</span><strong>{audit.false_negative_count ?? '—'}</strong></div>
        <div><span>误报（不该拦却拦）</span><strong>{audit.false_positive_count ?? '—'}</strong></div>
        <div><span>共触发</span><strong>{audit.co_triggered_count ?? '—'}</strong></div>
        <div><span>覆盖完整</span><strong>{audit.coverage_complete ? '是' : '否'}</strong></div>
      </div>
    </div>
  )
}

export function ArtifactDetail({ detail, view, onClose }) {
  return (
    <div className="artifact-inline-detail">
      <div className="artifact-inline-heading">
        <strong>{view === 'ddl' ? 'Oracle 表结构' : view === 'codes' ? '码表迁移' : '验证结果'}</strong>
        <div className="actions">
          {view === 'ddl' && detail.ddl && <CopyButton text={detail.ddl} label="复制 DDL" />}
          <button type="button" className="button-link" onClick={onClose}>收起</button>
        </div>
      </div>
      {view === 'ddl' && (
        detail.ddl ? <pre className="sql-pre">{detail.ddl}</pre> : <div className="empty">还没有生成表结构。</div>
      )}
      {view === 'codes' && (
        detail.code_sets.length > 0 ? (
          <div className="table-wrap">
            <table className="data-table">
              <thead><tr><th>码表</th><th>码值数</th><th>迁移到 Oracle</th><th>目标表</th></tr></thead>
              <tbody>
                {detail.code_sets.map((set) => (
                  <tr key={set.code}>
                    <td><strong>{set.name}</strong><small>{set.code}</small></td>
                    <td>{set.value_count}</td>
                    <td><StatusBadge tone={set.oracle_records > 0 ? 'green' : 'neutral'}>{set.oracle_records} 条</StatusBadge></td>
                    <td><code>{set.oracle_table || '—'}</code></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <div className="empty">这张表没有关联码表。</div>
      )}
      {view === 'audit' && (
        detail.audit ? <AuditView audit={detail.audit} /> : detail.precheck ? (
          <>
            <div className={`audit-banner ${detail.precheck.all_passed ? 'ok' : 'bad'}`}>
              <strong>{detail.precheck.all_passed ? 'Oracle 空表预检通过' : 'Oracle 空表预检未通过'}</strong>
              <span>{detail.precheck.rule_count} 条规则 · {detail.precheck.statement_count} 条 SQL 均已在空表执行</span>
            </div>
            <p className="muted-text">空跑只验证建表、规则 SQL 可执行；完整规则有效性仍需在第三步生成 AI 测试数据后审计。</p>
          </>
        ) : <AuditView audit={null} />
      )}
    </div>
  )
}
