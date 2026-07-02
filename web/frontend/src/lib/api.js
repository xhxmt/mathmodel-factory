// Centralized API client, endpoint helpers, and formatters.
import axios from 'axios'
import {
  normalizeArtifact,
  normalizeAuthUser,
  normalizeCloudConfig,
  normalizeProjectRequest,
  normalizeProjectStatus,
  normalizeStepsPayload,
} from './contracts.js'

const api = axios.create()

// Hard ceiling so a hung backend cannot leave requests pending forever.
api.defaults.timeout = 15000

api.interceptors.request.use((config) => {
  const token = localStorage.getItem('access_token')
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

let onUnauthorized = () => {}
export function setUnauthorizedHandler(fn) { onUnauthorized = fn }

// Surfaced for 5xx / network / timeout failures so the UI can toast a single
// generic message. Callers may still inspect the rejected error themselves.
let onServerError = () => {}
export function setServerErrorHandler(fn) { onServerError = fn }

function serverErrorMessage(err) {
  if (err?.response?.data?.detail) return err.response.data.detail
  if (err?.code === 'ECONNABORTED') return '请求超时，请稍后重试'
  if (err?.code === 'ERR_NETWORK') return '网络连接失败'
  return '服务暂时不可用'
}

api.interceptors.response.use(
  (r) => r,
  (err) => {
    if (err.response?.status === 401) onUnauthorized()
    else if (err.response?.status >= 500 || err.code === 'ECONNABORTED' || err.code === 'ERR_NETWORK') {
      onServerError(serverErrorMessage(err))
    }
    return Promise.reject(err)
  }
)

export default api

// ---- auth ----
let authMeOverride = null
export function __setAuthMeForTest(fn) { authMeOverride = fn }

export async function authLogin(username, password) {
  const { data } = await api.post('/api/auth/login', { username, password })
  return Object.assign({}, data, normalizeAuthUser(data))
}
export async function authRegister(payload) {
  const { data } = await api.post('/api/auth/register', payload)
  return normalizeAuthUser(data)
}
export async function authMe() {
  if (authMeOverride) return normalizeAuthUser(await authMeOverride())
  const { data } = await api.get('/api/auth/me')
  return normalizeAuthUser(data)
}
export async function authWsTicket() {
  const { data } = await api.post('/api/auth/ws-ticket')
  return data
}

export const AdminUsers = {
  list: () => api.get('/api/admin/users').then((r) => (Array.isArray(r.data) ? r.data.map(normalizeAuthUser) : [])),
  approve: (username, reason = '') => api.post(`/api/admin/users/${username}/approve`, { reason }).then((r) => normalizeAuthUser(r.data)),
  reject: (username, reason = '') => api.post(`/api/admin/users/${username}/reject`, { reason }).then((r) => normalizeAuthUser(r.data)),
  disable: (username, reason = '') => api.post(`/api/admin/users/${username}/disable`, { reason }).then((r) => normalizeAuthUser(r.data)),
  delete: (username) => api.delete(`/api/admin/users/${username}`).then((r) => r.data),
}

export const ProjectRequests = {
  list: () => api.get('/api/project-requests').then((r) => (Array.isArray(r.data) ? r.data.map(normalizeProjectRequest) : [])),
  create: (payload) => api.post('/api/project-requests', payload).then((r) => normalizeProjectRequest(r.data)),
  approve: (id, note = '') => api.post(`/api/admin/project-requests/${id}/approve`, { note }).then((r) => normalizeProjectRequest(r.data)),
  reject: (id, note = '') => api.post(`/api/admin/project-requests/${id}/reject`, { note }).then((r) => normalizeProjectRequest(r.data)),
}

// ---- projects ----
export const Projects = {
  list: () => api.get('/api/projects').then((r) => (Array.isArray(r.data) ? r.data.map(normalizeProjectStatus) : [])),
  status: (b) => api.get(`/api/projects/${b}/status`).then((r) => normalizeProjectStatus(r.data)),
  diagnostics: (b) => api.get(`/api/projects/${b}/diagnostics`).then((r) => r.data),
  checkpoint: (b) => api.get(`/api/projects/${b}/checkpoint`).then((r) => r.data),
  logs: (b, lines = 250, signal) => api.get(`/api/projects/${b}/logs`, { params: { lines }, signal }).then((r) => r.data),
  steps: (b, signal) => api.get(`/api/projects/${b}/steps`, { signal }).then((r) => normalizeStepsPayload(r.data)),
  files: (b) => api.get(`/api/projects/${b}/files`).then((r) => ({
    ...r.data,
    files: Array.isArray(r.data?.files) ? r.data.files.map(normalizeArtifact) : [],
  })),
  file: (b, path) => api.get(`/api/projects/${b}/file`, { params: { path } }).then((r) => r.data),
  consultation: (b) => api.get(`/api/projects/${b}/consultation`).then((r) => r.data),
  answer: (b, answer) => api.post(`/api/projects/${b}/consultation/answer`, { answer }).then((r) => r.data),
  modelingDirections: (b) => api.get(`/api/projects/${b}/modeling-directions`).then((r) => r.data),
  selectModelingDirection: (b, directionId) => api.post(`/api/projects/${b}/modeling-directions/selection`, { direction_id: directionId }).then((r) => r.data),
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
  projectConfig: (b) => api.get(`/api/projects/${b}/cloud/config`).then((r) => normalizeCloudConfig(r.data)),
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
