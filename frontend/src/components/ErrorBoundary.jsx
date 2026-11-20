import React from 'react'

export default class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props)
    this.state = { error: null }
  }

  static getDerivedStateFromError(error) {
    return { error }
  }

  componentDidCatch(error, info) {
    // 保留控制台输出便于排查
    console.error('[RegOps ErrorBoundary]', error, info)
  }

  render() {
    if (!this.state.error) return this.props.children
    return (
      <section className="panel error-boundary-card" role="alert">
        <div className="panel-heading">
          <div>
            <h2>页面渲染遇到异常</h2>
            <p>这不会影响已保存在后端数据库中的规范、规则或运行记录。</p>
          </div>
        </div>
        <pre className="error-boundary-detail">{String(this.state.error?.message || this.state.error)}</pre>
        <div className="actions">
          <button
            type="button"
            className="button primary"
            onClick={() => {
              this.setState({ error: null })
              window.location.reload()
            }}
          >
            刷新页面
          </button>
          <button
            type="button"
            className="button secondary"
            onClick={() => {
              this.setState({ error: null })
              window.location.assign('/documents')
            }}
          >
            返回第一步
          </button>
        </div>
      </section>
    )
  }
}
