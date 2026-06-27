import { ref } from 'vue'
import { authMe } from '../lib/api.js'

const isAuthenticated = ref(false)
const username = ref('')

export function useAuth() {
  async function bootstrap() {
    const token = localStorage.getItem('access_token')
    const user = localStorage.getItem('username')
    if (!token || !user) return false
    await authMe()
    isAuthenticated.value = true
    username.value = user
    return true
  }

  function login(data) {
    isAuthenticated.value = true
    username.value = data.username
  }

  function logout() {
    localStorage.removeItem('access_token')
    localStorage.removeItem('username')
    isAuthenticated.value = false
    username.value = ''
  }

  return { isAuthenticated, username, bootstrap, login, logout }
}
