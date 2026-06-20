// Theme (dark default) persisted to localStorage and applied to <html>.
import { ref } from 'vue'

const KEY = 'pf_theme'
const theme = ref(localStorage.getItem(KEY) || 'dark')

function apply() {
  document.documentElement.setAttribute('data-theme', theme.value)
}
apply()

export function useTheme() {
  function set(v) {
    theme.value = v
    localStorage.setItem(KEY, v)
    apply()
  }
  function toggle() {
    set(theme.value === 'dark' ? 'light' : 'dark')
  }
  return { theme, set, toggle }
}
