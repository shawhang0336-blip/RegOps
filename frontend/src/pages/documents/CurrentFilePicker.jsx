import { useEffect, useRef, useState } from 'react'
import { formatTime } from '../SharedUI.jsx'

export default function CurrentFilePicker({ documents, currentId, busy, onSelect }) {
  const [open, setOpen] = useState(false)
  const boxRef = useRef(null)

  useEffect(() => {
    if (!open) return undefined
    const onPointerDown = (event) => {
      if (boxRef.current && !boxRef.current.contains(event.target)) setOpen(false)
    }
    const onKeyDown = (event) => {
      if (event.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onPointerDown)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('mousedown', onPointerDown)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [open])

  const current = documents.find((doc) => doc.id === currentId)
  const disabled = busy || !documents.length

  return (
    <div className="file-picker" ref={boxRef}>
      <button
        type="button"
        className={`file-picker-trigger ${open ? 'open' : ''}`}
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <span className="file-picker-name">{current ? current.file_name : '还没有选择文件'}</span>
        <span className="file-picker-caret" aria-hidden="true" />
      </button>
      {open && (
        <ul className="file-picker-menu" role="listbox">
          {documents.map((doc) => (
            <li key={doc.id}>
              <button
                type="button"
                role="option"
                aria-selected={doc.id === currentId}
                className={`file-picker-option ${doc.id === currentId ? 'selected' : ''}`}
                onClick={() => {
                  onSelect(doc.id)
                  setOpen(false)
                }}
              >
                <span className="file-picker-option-name">{doc.file_name}</span>
                <span className="file-picker-option-meta">
                  {[String(doc.file_type || '').toUpperCase(), `${doc.block_count ?? 0} 个内容块`, formatTime(doc.created_at)]
                    .filter(Boolean)
                    .join(' · ')}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
