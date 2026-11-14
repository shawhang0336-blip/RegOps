import { useEffect, useState } from 'react'
import { v2 } from '../api.js'
import PageHeader from './PageHeader.jsx'
import StatusMessage from './StatusMessage.jsx'
import { StatusBadge } from './SharedUI.jsx'

const SERVICE_META = {
  mysql: {
    title: 'MySQL 管理数据库',
    desc: '保存规范内容、报表字段、码值和操作记录',
  },
  oracle: {
    title: 'Oracle 验证数据库',
    desc: '实际建表、写入测试数据并执行校验规则',
  },
  llm: {
    title: 'AI 服务',
    desc: '帮助识别字段、复核内容并生成规则参数',
  },
}

const BUDGET_META = {
  max_rules_per_table: {
    label: '单表规则数量上限',
    desc: 'AI 生成的显性 + 拓展规则总数不得超过此上限',
    unit: '条 / 表',
  },
  max_cases_per_table: {
    label: '每张报表的测试数据上限',
    desc: '正常与异常测试数据的总行数上限',
    unit: '行 / 表',
  },
  max_retries: {
    label: '失败后的自动修正次数',
    desc: '验证发现误拦截或漏拦截时，允许自动修正的最大轮数',
    unit: '轮',
  },
  max_llm_concurrency: {
    label: 'AI 同时处理任务数',
    desc: '同一阶段允许并行处理的 AI 任务数量',
    unit: '个',
  },
}

export default function SettingsPage() {
  const [settings, setSettings] = useState(null)
  const [budget, setBudget] = useState({})
  const [results, setResults] = useState({})
  const [testingKind, setTestingKind] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  async function load() {
    const body = await v2('/settings')
    setSettings(body)
    setBudget(body.execution_budget || {})
  }

  useEffect(() => {
    load().catch((err) => setError(err.message))
  }, [])

  async function test(kind) {
    setError('')
    setTestingKind(kind)
    try {
      const result = await v2(`/settings/test/${kind}`, {
        method: 'POST',
        body: JSON.stringify({}),
      })
      setResults({ ...results, [kind]: result })
    } catch (err) {
      setResults({ ...results, [kind]: { ok: false, error: err.message } })
    } finally {
      setTestingKind('')
    }
  }

  async function saveBudget() {
    setError('')
    try {
      await v2('/settings/execution_budget', {
        method: 'PUT',
        body: JSON.stringify({
          value: Object.fromEntries(
            Object.entries(budget).map(([key, value]) => [key, Number(value)])
          ),
        }),
      })
      setNotice('确定性执行预算已保存，后续所有 AI 生成计划均严格受此上限约束。')
    } catch (err) {
      setError(err.message)
    }
  }

  function renderTestResult(kind, res) {
    if (!res) return null
    if (!res.ok) {
      return <div className="connection-result error">连接失败：{res.error}</div>
    }
    return (
      <div className="connection-result success">
        <strong>连通性校验通过</strong>
        {kind === 'mysql' && (
          <div>
            版本：{res.version} · 用户：{res.user} · 字符集：{res.charset}
          </div>
        )}
        {kind === 'oracle' && (
          <div>
            沙箱用户：{res.user} · 容器实例：{res.container}
          </div>
        )}
        {kind === 'llm' && (
          <div>
            响应模型：{res.model} · 请求 ID：<code>{res.request_id}</code>
          </div>
        )}
      </div>
    )
  }

  return (
    <>
      <PageHeader
        eyebrow="运行环境"
        title="系统设置"
        description="检查数据库和 AI 服务连接，并设置单次生成的数量上限。"
      />

      <StatusMessage error={error} notice={notice} onClose={() => { setError(''); setNotice('') }} />

      <section className="settings-grid">
        {['mysql', 'oracle', 'llm'].map((kind) => {
          const meta = SERVICE_META[kind]
          const configured =
            kind === 'llm'
              ? settings?.llm?.credential_configured
              : settings?.[kind]?.configured
          return (
            <div className="panel setting-card" key={kind}>
              <div className="panel-heading" style={{ marginBottom: 4 }}>
                <div>
                  <h2>{meta.title}</h2>
                  <p>{meta.desc}</p>
                </div>
                <StatusBadge tone={configured ? 'green' : 'amber'}>
                  {configured ? '已配置' : '未配置'}
                </StatusBadge>
              </div>

              <div className="connection-kv">
                {kind === 'mysql' && (
                  <span>
                    驱动类型：<code>{settings?.mysql?.driver || 'sqlite/mysql'}</code>
                  </span>
                )}
                {kind === 'oracle' && (
                  <span>
                    沙箱状态：{configured ? '已绑定 Oracle 连接串' : '未配置 ORACLE_SANDBOX_URL'}
                  </span>
                )}
                {kind === 'llm' && (
                  <>
                    <span>
                      模型标识：<code>{settings?.llm?.model || '—'}</code>
                    </span>
                    <span>
                      接口端点：<code>{settings?.llm?.base_url || '—'}</code>
                    </span>
                  </>
                )}
              </div>

              <div className="actions" style={{ marginTop: 'auto' }}>
                <button
                  type="button"
                  className="button secondary"
                  disabled={testingKind === kind}
                  onClick={() => test(kind)}
                >
                  {testingKind === kind ? '正在测试连通性...' : '测试连接状态'}
                </button>
              </div>

              {renderTestResult(kind, results[kind])}
            </div>
          )
        })}
      </section>

      <section className="panel budget-panel">
        <div className="panel-heading">
          <div>
            <h2>生成数量上限</h2>
            <p>AI 可以按报表复杂度决定具体数量，但不会超过这里设置的上限。</p>
          </div>
          <button type="button" className="button primary" onClick={saveBudget}>
            保存预算配置
          </button>
        </div>

        <div className="budget-fields">
          {Object.entries(budget).map(([key, value]) => {
            const meta = BUDGET_META[key] || { label: key, desc: '', unit: '' }
            return (
              <label key={key}>
                <span>
                  {meta.label} {meta.unit && <small>({meta.unit})</small>}
                </span>
                <input
                  type="number"
                  min="1"
                  value={value}
                  onChange={(event) => setBudget({ ...budget, [key]: event.target.value })}
                />
                {meta.desc && <small>{meta.desc}</small>}
              </label>
            )
          })}
        </div>
      </section>
    </>
  )
}
