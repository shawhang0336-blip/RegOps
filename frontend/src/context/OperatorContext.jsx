import { createContext, useContext, useEffect, useState } from 'react'

const STORAGE_KEY = 'regops_operator_name'
const THEME_KEY = 'regops_theme_mode'

const OperatorContext = createContext({
  operator: '',
  setOperator: () => {},
  requireOperator: () => false,
  theme: 'light',
  setTheme: () => {},
  toggleTheme: () => {},
})

export function OperatorProvider({ children }) {
  const [operator, setOperatorState] = useState(() => {
    try {
      return window.localStorage.getItem(STORAGE_KEY) || '合规审核员'
    } catch {
      return '合规审核员'
    }
  })

  const [theme, setThemeState] = useState(() => {
    try {
      const saved = window.localStorage.getItem(THEME_KEY)
      if (saved === 'dark' || saved === 'light') return saved
      return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
    } catch {
      return 'light'
    }
  })

  function setOperator(value) {
    setOperatorState(value)
    try {
      window.localStorage.setItem(STORAGE_KEY, value)
    } catch {
      // ignore storage errors
    }
  }

  function setTheme(nextTheme) {
    setThemeState(nextTheme)
    try {
      window.localStorage.setItem(THEME_KEY, nextTheme)
    } catch {
      // ignore
    }
  }

  function toggleTheme() {
    setTheme(theme === 'dark' ? 'light' : 'dark')
  }

  useEffect(() => {
    try {
      if (operator) window.localStorage.setItem(STORAGE_KEY, operator)
    } catch {
      // ignore
    }
  }, [operator])

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme)
  }, [theme])

  // Global shortcut Alt+D to toggle dark mode anywhere
  useEffect(() => {
    function onKeyDown(e) {
      if (e.altKey && (e.key === 'd' || e.key === 'D')) {
        e.preventDefault()
        setThemeState((prev) => {
          const next = prev === 'dark' ? 'light' : 'dark'
          try {
            window.localStorage.setItem(THEME_KEY, next)
          } catch {
            // ignore
          }
          return next
        })
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [])

  function requireOperator(onError) {
    const trimmed = (operator || '').trim()
    if (!trimmed) {
      if (onError) onError('请先在页面右上角填写「当前操作人 / 审批人」身份（填写后全站自动记忆）。')
      return ''
    }
    return trimmed
  }

  return (
    <OperatorContext.Provider
      value={{ operator, setOperator, requireOperator, theme, setTheme, toggleTheme }}
    >
      {children}
    </OperatorContext.Provider>
  )
}

export function useOperator() {
  return useContext(OperatorContext)
}
