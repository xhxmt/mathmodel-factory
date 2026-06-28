// Centralized API client, endpoint helpers, and formatters.
import axios from 'axios'

const api = axios.create()

api.interceptors.request.use((config) => {
  const token = localStorage.getItem('access_token')
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

let onUnauthorized = () => {}
export function setUnauthorizedHandler(fn) { onUnauthorized = fn }

api.interceptors.response.use(
  (r) => r,
  (err) => {
    if (err.response?.status === 401) onUnauthorized()
    return Promise.reject(err)
  }
)

export default api

// ---- auth ----
export async function authLogin(username, password) {
  const { data } = await api.post('/api/auth/login', { username, password })
  return data
}
export async function authMe() {
  const { data } = await api.get('/api/auth/me')
  return data
}
export async function authWsTicket() {
  const { data } = await api.post('/api/auth/ws-ticket')
  return data
}

// ---- projects ----
export const Projects = {
  list: () => api.get('/api/projects').then((r) => r.data),
  status: (b) => api.get(`/api/projects/${b}/status`).then((r) => r.data),
  diagnostics: (b) => api.get(`/api/projects/${b}/diagnostics`).then((r) => r.data),
  checkpoint: (b) => api.get(`/api/projects/${b}/checkpoint`).then((r) => r.data),
  logs: (b, lines = 250, signal) => api.get(`/api/projects/${b}/logs`, { params: { lines }, signal }).then((r) => r.data),
  steps: (b, signal) => api.get(`/api/projects/${b}/steps`, { signal }).then((r) => r.data),
  files: (b) => api.get(`/api/projects/${b}/files`).then((r) => r.data),
  file: (b, path) => api.get(`/api/projects/${b}/file`, { params: { path } }).then((r) => r.data),
  consultation: (b) => api.get(`/api/projects/${b}/consultation`).then((r) => r.data),
  answer: (b, answer) => api.post(`/api/projects/${b}/consultation/answer`, { answer }).then((r) => r.data),
  action: (b, action) => api.post(`/api/projects/${b}/action`, { action }).then((r) => r.data),
  create: (payload) => api.post('/api/projects/new', payload).then((r) => r.data),
  rawUrl: (b, path) => `/api/projects/${b}/raw?path=${encodeURIComponent(path)}`,
  paperUrl: (b, download = false) => `/api/projects/${b}/paper${download ? '?download=1' : ''}`,
}

// Fetch an authenticated binary resource as an object URL (for <img>/<iframe>,
// which cannot carry the Bearer header themselves).
export async function fetchBlobUrl(url) {
  const resp = await api.get(url, { responseType: 'blob' })
  return URL.createObjectURL(resp.data)
}

// ---- models (registry + per-step assignment) ----
export const Models = {
  // { registry: [...], config: {...}, agentic_backends: [...], valid_backends: [...] }
  get: () => api.get('/api/models').then((r) => r.data),
  saveRegistry: (models) => api.put('/api/models/registry', { models }).then((r) => r.data),
  // scope = project base_name, or "_default"; steps = { step_N: {primary, fallback} }
  saveConfig: (scope, steps) => api.put('/api/models/config', { scope, steps }).then((r) => r.data),
}

// ---- cloud (GCP Cloud Run solver acceleration) ----
export const Cloud = {
  status: () => api.get('/api/cloud/status').then((r) => r.data),
  projectConfig: (b) => api.get(`/api/projects/${b}/cloud/config`).then((r) => r.data),
  enable: (b) => api.post(`/api/projects/${b}/cloud/enable`).then((r) => r.data),
  disable: (b) => api.post(`/api/projects/${b}/cloud/disable`).then((r) => r.data),
  config: () => api.get('/api/cloud/config').then((r) => r.data),
}

// ---- formatters ----
export function relativeTime(ts) {
  if (!ts) return '—'
  const time = new Date(ts.replace(' ', 'T'))
  if (isNaN(time)) return ts
  const diff = Math.floor((Date.now() - time.getTime()) / 1000)
  if (diff < 0) return '刚刚'
  if (diff < 60) return `${diff}s 前`
  if (diff < 3600) return `${Math.floor(diff / 60)}m 前`
  if (diff < 86400) return `${Math.floor(diff / 3600)}h 前`
  if (diff < 604800) return `${Math.floor(diff / 86400)}d 前`
  return ts.split(' ')[0]
}

export function formatBytes(n) {
  if (n == null) return '—'
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}
