import { useEffect, useState } from 'react'
import { v2 } from '../api.js'
import PageHeader from './PageHeader.jsx'
import StatusMessage from './StatusMessage.jsx'
import { StatusBadge } from './SharedUI.jsx'

const SERVICE_META = {
  mysql: {
    title: 'MySQL 元数据与事件库',
    desc: '存储规范原始块、表级元数据、码值字典与不可变事件因果账本',
  },
  oracle: {
    title: 'Oracle 独立审计沙箱',
    desc: '执行双表物理建表、合成数据装载与确定性校验 SQL 审计',
  },
  llm: {
    title: 'GLM 结构化赋值服务',
    desc: '驱动元数据提取、交叉复核、RuleIR 算子赋值与变异样本规划',
  },
}

const BUDGET_META = {
  max_rules_per_table: {
    label: '单表规则数量上限',
    desc: 'AI 生成的显性 + 拓展规则总数不得超过此上限',
    unit: '条 / 表',
  },
  max_cases_per_table: {
    label: '单表沙箱用例上限',
    desc: '黄金正向样本与变异负样本的合成总行数上限',
    unit: '行 / 表',
  },
  max_retries: {
    label: '最大自愈回边次数',
    desc: '沙箱审计发现误报/漏报时允许 AI 自动修正的最大轮数',
    unit: '轮',
  },
  max_llm_concurrency: {
    label: '模型最大并发度',
    desc: '单阶段扇出调用 GLM 接口的最大并发工作线程数',
    unit: '并发',
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
        eyebrow="SYSTEM · 基础设施连通性与确定性预算围栏"
        title="系统与执行预算设置"
        description="管理元数据库、Oracle 沙箱与大模型连接状态（敏感密钥仅驻留服务端环境变量），配置防失控的确定性执行预算。"
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
            <h2>确定性执行预算围栏 (Execution Budget)</h2>
            <p>AI 可以根据表复杂度自主规划规则与样本数量，但程序内核会强制拦截任何超出以下硬上限的赋值请求。</p>
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
