import { useEffect, useId, useMemo, useRef, useState } from 'react'

/**
 * 现代化自定义下拉选择器（替代原生 OS <select>）：
 * - 支持富文本项（主标题、副标题、前置标签徽章、右侧统计标签）
 * - 完整键盘导航（ArrowUp / ArrowDown / Enter / Escape）与点击外部自动收起
 * - 适配明暗双主题与 compact / default 两种尺寸
 */
export default function SelectDropdown({
  value = '',
  options = [],
  onChange,
  placeholder = '请选择...',
  ariaLabel = '下拉选择',
  disabled = false,
  size = 'default',
  minWidth = 220,
  align = 'left',
  className = '',
}) {
  const [open, setOpen] = useState(false)
  const [highlightIndex, setHighlightIndex] = useState(-1)
  const rootRef = useRef(null)
  const listboxId = useId()

  const selectedIndex = useMemo(
    () => options.findIndex((opt) => String(opt.value) === String(value)),
    [options, value],
  )
  const selectedOption = selectedIndex >= 0 ? options[selectedIndex] : null

  useEffect(() => {
    if (!open) return undefined
    setHighlightIndex(selectedIndex >= 0 ? selectedIndex : 0)

    function handlePointerDown(event) {
      if (rootRef.current && !rootRef.current.contains(event.target)) {
        setOpen(false)
      }
    }
    function handleKeyDown(event) {
      if (event.key === 'Escape') {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', handlePointerDown)
    document.addEventListener('keydown', handleKeyDown)
    return () => {
      document.removeEventListener('mousedown', handlePointerDown)
      document.removeEventListener('keydown', handleKeyDown)
    }
  }, [open, selectedIndex])

  function commitSelect(option) {
    if (!option || option.disabled) return
    setOpen(false)
    if (String(option.value) !== String(value)) {
      onChange?.(option.value, option)
    }
  }

  function handleTriggerKeyDown(event) {
    if (disabled) return
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault()
      if (!open) {
        setOpen(true)
        return
      }
      const delta = event.key === 'ArrowDown' ? 1 : -1
      setHighlightIndex((prev) => {
        if (!options.length) return -1
        const start = prev < 0 ? 0 : prev
        return (start + delta + options.length) % options.length
      })
    } else if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault()
      if (!open) {
        setOpen(true)
      } else if (highlightIndex >= 0 && options[highlightIndex]) {
        commitSelect(options[highlightIndex])
      }
    }
  }

  return (
    <div
      ref={rootRef}
      className={`ui-dropdown ${size === 'sm' ? 'ui-dropdown-sm' : ''} ${open ? 'is-open' : ''} ${className}`}
      style={minWidth ? { minWidth } : undefined}
    >
      <button
        type="button"
        className="ui-dropdown-trigger"
        aria-label={ariaLabel}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={listboxId}
        disabled={disabled}
        onClick={() => !disabled && setOpen((prev) => !prev)}
        onKeyDown={handleTriggerKeyDown}
      >
        <span className="ui-dropdown-value">
          {selectedOption ? (
            <>
              {selectedOption.badge && <span className="ui-dropdown-badge">{selectedOption.badge}</span>}
              <span className="ui-dropdown-label" title={selectedOption.label}>
                {selectedOption.label}
              </span>
              {selectedOption.meta && <span className="ui-dropdown-meta">{selectedOption.meta}</span>}
            </>
          ) : (
            <span className="ui-dropdown-placeholder">{placeholder}</span>
          )}
        </span>
        <svg
          className="ui-dropdown-chevron"
          width="14"
          height="14"
          viewBox="0 0 16 16"
          fill="none"
          aria-hidden="true"
        >
          <path
            d="M4 6.25L8 10.25L12 6.25"
            stroke="currentColor"
            strokeWidth="1.6"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </button>

      {open && (
        <div
          id={listboxId}
          role="listbox"
          aria-label={ariaLabel}
          className={`ui-dropdown-menu ${align === 'right' ? 'align-right' : ''}`}
        >
          {options.length > 0 ? (
            options.map((opt, idx) => {
              const isSelected = String(opt.value) === String(value)
              const isHighlighted = idx === highlightIndex
              return (
                <button
                  key={String(opt.value)}
                  type="button"
                  role="option"
                  aria-selected={isSelected}
                  disabled={opt.disabled}
                  className={`ui-dropdown-option ${isSelected ? 'is-selected' : ''} ${isHighlighted ? 'is-highlighted' : ''}`}
                  onMouseEnter={() => setHighlightIndex(idx)}
                  onClick={() => commitSelect(opt)}
                >
                  <div className="ui-dropdown-option-main">
                    <div className="ui-dropdown-option-top">
                      {opt.badge && <span className="ui-dropdown-badge">{opt.badge}</span>}
                      <span className="ui-dropdown-option-label">{opt.label}</span>
                      {opt.tag && <span className="ui-dropdown-tag">{opt.tag}</span>}
                    </div>
                    {opt.description && (
                      <div className="ui-dropdown-option-desc">{opt.description}</div>
                    )}
                  </div>
                  <span className="ui-dropdown-option-right">
                    {opt.meta && <span className="ui-dropdown-option-meta">{opt.meta}</span>}
                    {isSelected && (
                      <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                        <path
                          d="M3.5 8.5L6.5 11.5L12.5 5"
                          stroke="currentColor"
                          strokeWidth="1.8"
                          strokeLinecap="round"
                          strokeLinejoin="round"
                        />
                      </svg>
                    )}
                  </span>
                </button>
              )
            })
          ) : (
            <div className="ui-dropdown-empty">暂无可选项</div>
          )}
        </div>
      )}
    </div>
  )
}
