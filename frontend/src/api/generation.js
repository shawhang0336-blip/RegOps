/** 规则与产物装配 API（对应后端 `api/generation/`）。 */
import { api, downloadFile } from './client.js'

function scopeQuery({ documentId, packageId } = {}) {
  const params = new URLSearchParams()
  if (packageId) params.set('package_id', packageId)
  else if (documentId) params.set('document_id', documentId)
  const qs = params.toString()
  return qs ? `?${qs}` : ''
}

export function listArtifactsStatus({ documentId, packageId } = {}) {
  return api(`/api/artifacts${scopeQuery({ documentId, packageId })}`)
}

export function listAllTableRules({ documentId, packageId } = {}) {
  return api(`/api/artifacts/rules${scopeQuery({ documentId, packageId })}`)
}

export function getTableArtifacts(tableId) {
  return api(`/api/artifacts/${tableId}`)
}

export function editRuleParams(ruleRowId, payload) {
  return api(`/api/artifacts/rules/${ruleRowId}`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  })
}

export function createGenerationRun({ tableIds, provider = 'live', acceptWarnings = false }) {
  return api('/api/generation/runs', {
    method: 'POST',
    body: JSON.stringify({
      table_ids: tableIds,
      provider,
      accept_warnings: Boolean(acceptWarnings),
    }),
  })
}

export function listGenerationPlans(runId) {
  return api(`/api/generation/plans/${runId}`)
}

export function downloadProductionBundle(runId) {
  return downloadFile(
    `/api/generation/runs/${runId}/production-bundle`,
    `production_${runId.slice(0, 8)}.zip`,
  )
}
