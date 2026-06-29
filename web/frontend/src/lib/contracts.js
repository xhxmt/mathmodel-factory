/**
 * @typedef {Object} ProjectStatus
 * @property {string} base_name
 * @property {string} status
 * @property {number} current_step
 * @property {number} progress_percent
 * @property {boolean} is_running
 * @property {number|null} pid
 * @property {boolean} consultation_pending
 * @property {string|null} consultation_gate
 * @property {string|null} last_updated
 */

/**
 * @typedef {Object} Artifact
 * @property {string} path
 * @property {string} name
 * @property {string} type
 * @property {string} group
 * @property {number|null} size
 * @property {string|null} mtime
 */

/**
 * @typedef {Object} CloudConfig
 * @property {boolean} enabled
 * @property {string} env_file
 * @property {number} threshold_time
 * @property {string[]} solver_types
 * @property {string} project_id
 * @property {string} region
 * @property {string} service_name
 */

function numberOr(value, fallback) {
  const n = Number(value)
  return Number.isFinite(n) ? n : fallback
}

function stringOrNull(value) {
  if (value === undefined || value === null || value === '') return null
  return String(value)
}

function fileName(path) {
  const clean = String(path || '')
  const parts = clean.split('/').filter(Boolean)
  return parts[parts.length - 1] || clean
}

function normalizeSolverTypes(value) {
  if (Array.isArray(value)) return value.map(String).filter(Boolean)
  if (typeof value === 'string') return value.split(',').map((s) => s.trim()).filter(Boolean)
  return []
}

/** @returns {ProjectStatus} */
export function normalizeProjectStatus(raw = {}) {
  return {
    base_name: String(raw.base_name || ''),
    status: String(raw.status || 'unknown'),
    current_step: numberOr(raw.current_step, -1),
    progress_percent: numberOr(raw.progress_percent, 0),
    is_running: Boolean(raw.is_running),
    pid: raw.pid === undefined || raw.pid === null || raw.pid === '' ? null : numberOr(raw.pid, null),
    consultation_pending: Boolean(raw.consultation_pending),
    consultation_gate: stringOrNull(raw.consultation_gate),
    last_updated: stringOrNull(raw.last_updated),
  }
}

/** @returns {Artifact} */
export function normalizeArtifact(raw = {}) {
  const path = String(raw.path || '')
  return {
    path,
    name: String(raw.name || fileName(path)),
    type: String(raw.type || 'text'),
    group: String(raw.group || 'other'),
    size: raw.size === undefined || raw.size === null || raw.size === '' ? null : numberOr(raw.size, null),
    mtime: stringOrNull(raw.mtime),
  }
}

export function normalizeStepsPayload(raw = {}) {
  const steps = Array.isArray(raw.steps) ? raw.steps : []
  return {
    ...raw,
    current_step: numberOr(raw.current_step, -1),
    open_issues: numberOr(raw.open_issues, 0),
    paper_available: Boolean(raw.paper_available),
    steps: steps.map((step) => ({
      ...step,
      artifacts: Array.isArray(step?.artifacts) ? step.artifacts.map(normalizeArtifact) : [],
    })),
    editorial_gate: raw.editorial_gate
      ? {
          ...raw.editorial_gate,
          ready: Boolean(raw.editorial_gate.ready),
          artifacts: Array.isArray(raw.editorial_gate.artifacts)
            ? raw.editorial_gate.artifacts.map(normalizeArtifact)
            : [],
        }
      : null,
  }
}

/** @returns {CloudConfig} */
export function normalizeCloudConfig(raw = {}) {
  return {
    enabled: Boolean(raw.enabled),
    env_file: String(raw.env_file || ''),
    threshold_time: numberOr(raw.threshold_time, 300),
    solver_types: normalizeSolverTypes(raw.solver_types),
    project_id: String(raw.project_id || ''),
    region: String(raw.region || ''),
    service_name: String(raw.service_name || ''),
  }
}
