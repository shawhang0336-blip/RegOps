import { useState } from 'react'

export default function CopyButton({ text, label = '复制', copiedLabel = '已复制', className = 'button secondary sm' }) {
  const [copied, setCopied] = useState(false)

  async function handleCopy() {
    if (!text) return
    try {
      await navigator.clipboard.writeText(text)
    } catch {
      const area = document.createElement('textarea')
      area.value = text
      area.style.position = 'fixed'
      area.style.opacity = '0'
      document.body.appendChild(area)
      area.select()
      document.execCommand('copy')
      document.body.removeChild(area)
    }
    setCopied(true)
    window.setTimeout(() => setCopied(false), 1800)
  }

  return (
    <button type="button" className={className} disabled={!text} onClick={handleCopy}>
      {copied ? copiedLabel : label}
    </button>
  )
}
