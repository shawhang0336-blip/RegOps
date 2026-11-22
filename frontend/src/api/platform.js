/** 平台底座与系统配置 API（对应后端 `api/platform/`）。 */
import { api } from './client.js'

export function getHealth() {
  return api('/api/health')
}

export function getBlob(uriOrDigest) {
  const digest = String(uriOrDigest || '').replace(/^blob:\/\//, '')
  return api(`/api/blobs/${digest}`)
}

export function getSettings() {
  return api('/api/settings')
}

export function testConnection(kind) {
  return api(`/api/settings/test/${kind}`, {
    method: 'POST',
    body: JSON.stringify({}),
  })
}

export function saveExecutionBudget(value) {
  return api('/api/settings/execution_budget', {
    method: 'PUT',
    body: JSON.stringify({ value }),
  })
}

export function buildEventStreamUrl({ runId = '', documentId = '', topics = '' } = {}) {
  const params = new URLSearchParams()
  if (runId) params.set('run_id', runId)
  if (documentId) params.set('document_id', documentId)
  if (topics) params.set('topics', topics)
  const query = params.toString()
  return `/api/events/stream${query ? `?${query}` : ''}`
}
