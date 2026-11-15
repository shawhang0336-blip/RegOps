import { createContext, useContext, useEffect, useState } from 'react'

const THEME_KEY = 'regops_theme_mode'

const ThemeContext = createContext({
  theme: 'light',
  setTheme: () => {},
  toggleTheme: () => {},
})

export function ThemeProvider({ children }) {
  const [theme, setThemeState] = useState(() => {
    try {
      const saved = window.localStorage.getItem(THEME_KEY)
      if (saved === 'dark' || saved === 'light') return saved
      return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
    } catch {
      return 'light'
    }
  })

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

  return (
    <ThemeContext.Provider value={{ theme, setTheme, toggleTheme }}>
      {children}
    </ThemeContext.Provider>
  )
}

export function useTheme() {
  return useContext(ThemeContext)
}
