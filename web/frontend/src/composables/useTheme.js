// Theme: follow the system unless the user picked one; the choice is stored
// per browser.  `data-theme` always holds the resolved theme: a blocking
// script in index.html sets it before the first paint, and this module keeps
// it in sync with the user's choice and with system changes.
import { ref } from 'vue'

const KEY = 'pf_theme'

function stored() {
  try {
    const value = localStorage.getItem(KEY)
    return value === 'light' || value === 'dark' ? value : null
  } catch {
    return null
  }
}

const media = typeof window !== 'undefined' && window.matchMedia
  ? window.matchMedia('(prefers-color-scheme: dark)')
  : null
const choice = ref(stored())
const theme = ref(choice.value || (media?.matches ? 'dark' : 'light'))

function apply() {
  const root = document.documentElement
  theme.value = choice.value || (media?.matches ? 'dark' : 'light')
  root.setAttribute('data-theme', theme.value)
}
media?.addEventListener?.('change', apply)
apply()

export function useTheme() {
  function set(value) {
    choice.value = value === 'light' || value === 'dark' ? value : null
    try {
      if (choice.value) localStorage.setItem(KEY, choice.value)
      else localStorage.removeItem(KEY)
    } catch { /* storage unavailable: keep the in-memory choice */ }
    apply()
  }
  function toggle() {
    set(theme.value === 'dark' ? 'light' : 'dark')
  }
  return { theme, set, toggle }
}
