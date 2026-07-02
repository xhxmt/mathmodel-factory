import { computed, ref } from 'vue'
import { authMe } from '../lib/api.js'

const isAuthenticated = ref(false)
const username = ref('')
const role = ref('')
const status = ref('')
const displayName = ref('')
const isAdmin = computed(() => role.value === 'admin')

function applyUser(data) {
  username.value = data.username || ''
  role.value = data.role || ''
  status.value = data.status || ''
  displayName.value = data.display_name || ''
}

export function useAuth() {
  async function bootstrap() {
    const token = localStorage.getItem('access_token')
    const storedUser = localStorage.getItem('username')
    if (!token || !storedUser) return false
    const me = await authMe()
    applyUser(me)
    isAuthenticated.value = true
    return true
  }

  function login(data) {
    isAuthenticated.value = true
    localStorage.setItem('username', data.username)
    applyUser(data)
  }

  function logout() {
    localStorage.removeItem('access_token')
    localStorage.removeItem('username')
    isAuthenticated.value = false
    username.value = ''
    role.value = ''
    status.value = ''
    displayName.value = ''
  }

  return { isAuthenticated, username, role, status, displayName, isAdmin, bootstrap, login, logout }
}
