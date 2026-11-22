/** 规则与产物装配 API（对应后端 `api/generation/`）。 */
import { api, downloadFile } from './client.js'

function docQuery(documentId) {
  return documentId ? `?document_id=${encodeURIComponent(documentId)}` : ''
}

export function listArtifactsStatus({ documentId } = {}) {
  return api(`/api/artifacts${docQuery(documentId)}`)
}

export function listAllTableRules({ documentId } = {}) {
  return api(`/api/artifacts/rules${docQuery(documentId)}`)
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
