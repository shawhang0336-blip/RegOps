const ACTIVE_DOC_KEY = 'regops_active_document_id'

export function getSavedDocumentId() {
  try {
    return window.localStorage.getItem(ACTIVE_DOC_KEY) || ''
  } catch {
    return ''
  }
}

export function setSavedDocumentId(documentId) {
  try {
    if (documentId) window.localStorage.setItem(ACTIVE_DOC_KEY, documentId)
    else window.localStorage.removeItem(ACTIVE_DOC_KEY)
  } catch {
    // ignore storage errors
  }
}
