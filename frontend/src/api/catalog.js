/** 监管资产目录 API：文档 / 表字段元数据 / 码表（对应后端 `api/catalog/`）。 */
import { api, downloadFile } from './client.js'

function querySuffix(entries) {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(entries || {})) {
    if (value !== undefined && value !== null && value !== '') {
      params.set(key, String(value))
    }
  }
  const qs = params.toString()
  return qs ? `?${qs}` : ''
}

// ── 文档接入与切块 (/api/documents/*) ─────────────────────────────────────────

export function listDocuments() {
  return api('/api/documents')
}

export function getDocument(documentId) {
  return api(`/api/documents/${documentId}`)
}

export function uploadDocument(file) {
  const form = new FormData()
  form.append('file', file)
  return api('/api/documents', { method: 'POST', body: form })
}

export function deleteDocument(documentId) {
  return api(`/api/documents/${documentId}`, { method: 'DELETE' })
}

export function getDocumentProgress(documentId) {
  return api(`/api/documents/${documentId}/progress`)
}

export function cancelDocument(documentId, reason = '由操作员在文档工作台主动中止') {
  return api(`/api/documents/${documentId}/cancel`, {
    method: 'POST',
    body: JSON.stringify({ reason }),
  })
}

export function prepareDocument(documentId) {
  return api(`/api/documents/${documentId}/prepare`, {
    method: 'POST',
    body: JSON.stringify({}),
  })
}

export function reextractDocument(documentId) {
  return api(`/api/documents/${documentId}/reextract`, {
    method: 'POST',
    body: JSON.stringify({}),
  })
}

export function analyzeDocumentStructure(documentId) {
  return api(`/api/documents/${documentId}/analyze-structure`, {
    method: 'POST',
    body: JSON.stringify({}),
  })
}

export function getDocumentArchitecture(documentId) {
  return api(`/api/documents/${documentId}/architecture`)
}

export function getRawBlock(documentId, blockId) {
  return api(`/api/documents/${documentId}/blocks/${blockId}`)
}

export function adjustBlockStructure(documentId, blockId, changes) {
  return api(`/api/documents/${documentId}/blocks/${blockId}/structure`, {
    method: 'PATCH',
    body: JSON.stringify(changes),
  })
}

export function extractDocumentParallel(documentId, blockIds, targets = ['metadata', 'code_sets']) {
  return api(`/api/documents/${documentId}/extract-parallel`, {
    method: 'POST',
    body: JSON.stringify({ block_ids: blockIds, targets }),
  })
}

export function diffDocuments(baseId, targetId) {
  return api(`/api/documents/diff${querySuffix({ base_id: baseId, target_id: targetId })}`)
}

// ── 报表与字段元数据 (/api/metadata/*) ───────────────────────────────────────

export function listMetadataTables({ documentId, status } = {}) {
  return api(`/api/metadata/tables${querySuffix({ document_id: documentId, status })}`)
}

export function getMetadataTable(tableId) {
  return api(`/api/metadata/tables/${tableId}`)
}

export function getMetadataReadiness({ documentId } = {}) {
  return api(`/api/metadata/readiness${querySuffix({ document_id: documentId })}`)
}

export function deleteMetadataTable(tableId, payload) {
  return api(`/api/metadata/tables/${tableId}`, {
    method: 'DELETE',
    body: JSON.stringify(payload),
  })
}

export function editMetadataField(tableId, fieldId, payload) {
  return api(`/api/metadata/tables/${tableId}/fields/${fieldId}`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  })
}

export function deleteMetadataField(tableId, fieldId, payload) {
  return api(`/api/metadata/tables/${tableId}/fields/${fieldId}`, {
    method: 'DELETE',
    body: JSON.stringify(payload),
  })
}

export function addMetadataField(tableId, payload) {
  return api(`/api/metadata/tables/${tableId}/fields`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function downloadFieldTypeTemplate({ documentId } = {}) {
  return downloadFile(
    `/api/metadata/types/template${querySuffix({ document_id: documentId })}`,
    '字段类型修正模板.csv',
  )
}

export function importFieldTypes(formData) {
  return api('/api/metadata/types/import', { method: 'POST', body: formData })
}

// ── 公共与内联码表 (/api/code-sets/*) ────────────────────────────────────────

export function listCodeSets({ documentId, includePlaceholders } = {}) {
  return api(`/api/code-sets${querySuffix({
    document_id: documentId,
    include_placeholders: includePlaceholders ? 'true' : undefined,
  })}`)
}

export function getCodeSet(codeSetId) {
  return api(`/api/code-sets/${codeSetId}`)
}

export function createCodeSet(payload) {
  return api('/api/code-sets', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function deleteCodeSet(codeSetId) {
  return api(`/api/code-sets/${codeSetId}`, { method: 'DELETE' })
}

export function uploadCodeSetDocument(formData) {
  return api('/api/code-sets/upload-file', { method: 'POST', body: formData })
}

export function listUnresolvedCodeReferences({ documentId } = {}) {
  return api(`/api/code-sets/references/unresolved${querySuffix({ document_id: documentId })}`)
}

export function relinkCodeReferences() {
  return api('/api/code-sets/references/relink', {
    method: 'POST',
    body: JSON.stringify({}),
  })
}

export function getCodeSetReadiness({ documentId, referencedOnly } = {}) {
  return api(`/api/code-sets/readiness${querySuffix({
    document_id: documentId,
    referenced_only: referencedOnly ? 'true' : undefined,
  })}`)
}

export function addCodeValue(codeSetId, payload) {
  return api(`/api/code-sets/${codeSetId}/values`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function editCodeValue(codeSetId, valueId, payload) {
  return api(`/api/code-sets/${codeSetId}/values/${valueId}`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  })
}

export function importCodeValues(codeSetId, formData) {
  return api(`/api/code-sets/${codeSetId}/import`, { method: 'POST', body: formData })
}
