/**
 * @typedef {Object} ProjectStatus
 * @property {string} base_name
 * @property {string} run_id
 * @property {string} problem_key
 * @property {string} problem_title
 * @property {string} storage_scope
 * @property {boolean} archived
 * @property {string} status
 * @property {number} current_step
 * @property {number|null} revision
 * @property {number|null} last_completed_step
 * @property {string|null} scheduler_generation
 * @property {string|null} stage_catalog_version
 * @property {number|null} last_completed_stage
 * @property {number|null} active_stage
 * @property {string|null} active_stage_name
 * @property {string|null} active_subtask
 * @property {number|null} source_step_id
 * @property {Object|null} pending_action
 * @property {number} progress_percent
 * @property {boolean} is_running
 * @property {number|null} pid
 * @property {boolean} consultation_pending
 * @property {string|null} consultation_gate
 * @property {boolean} selection_pending
 * @property {string|null} selection_gate
 * @property {number|null} selection_deadline
 * @property {string|null} contest_profile
 * @property {Object|null} contest_phase
 * @property {number|null} contest_deadline_at
 * @property {number|null} content_freeze_at
 * @property {number|null} delivery_freeze_at
 * @property {number|null} remaining_seconds
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
 * @property {number|null} revision
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
    execution_state: stringOrNull(raw.execution_state),
    recorded_workflow_state: stringOrNull(raw.recorded_workflow_state),
    workflow_error: stringOrNull(raw.workflow_error),
    evidence_validity: String(raw.evidence_validity || 'UNAVAILABLE'),
    evidence_errors: Array.isArray(raw.evidence_errors) ? raw.evidence_errors.map(String) : [],
    scientific_verdict: String(raw.scientific_verdict || 'UNAVAILABLE'),
    raw_scientific_verdict: stringOrNull(raw.raw_scientific_verdict),
    review_mode: stringOrNull(raw.review_mode),
    score_available: raw.score_available === true,
    official_score: raw.official_score == null ? null : numberOr(raw.official_score, null),
    diagnostic_score: raw.diagnostic_score == null ? null : numberOr(raw.diagnostic_score, null),
    delivery_allowed: raw.delivery_allowed === true,
    display_status: String(raw.display_status || ''),
    reason_code: String(raw.reason_code || ''),
    reason_summary: String(raw.reason_summary || ''),
    suggested_actions: Array.isArray(raw.suggested_actions) ? raw.suggested_actions.map(String) : [],
    evidence: Array.isArray(raw.evidence) ? raw.evidence : [],
    diagnostic_reason_code: stringOrNull(raw.diagnostic_reason_code),
    diagnostic_badge: stringOrNull(raw.diagnostic_badge),
    diagnostic_priority: raw.diagnostic_priority == null ? 999 : numberOr(raw.diagnostic_priority, 999),
    base_name: String(raw.base_name || ''),
    run_id: String(raw.run_id || raw.base_name || ''),
    problem_key: String(raw.problem_key || `project:${raw.base_name || ''}`),
    problem_title: String(raw.problem_title || raw.base_name || ''),
    storage_scope: String(raw.storage_scope || ''),
    archived: Boolean(raw.archived),
    status: String(raw.status || 'unknown'),
    current_step: numberOr(raw.current_step, -1),
    revision: raw.revision == null ? null : numberOr(raw.revision, null),
    last_completed_step: raw.last_completed_step == null ? null : numberOr(raw.last_completed_step, null),
    scheduler_generation: stringOrNull(raw.scheduler_generation),
    stage_catalog_version: stringOrNull(raw.stage_catalog_version),
    last_completed_stage: raw.last_completed_stage == null ? null : numberOr(raw.last_completed_stage, null),
    active_stage: raw.active_stage == null ? null : numberOr(raw.active_stage, null),
    active_stage_name: stringOrNull(raw.active_stage_name),
    active_subtask: stringOrNull(raw.active_subtask),
    source_step_id: raw.source_step_id == null ? null : numberOr(raw.source_step_id, null),
    pending_action: raw.pending_action && typeof raw.pending_action === 'object' ? raw.pending_action : null,
    progress_percent: numberOr(raw.progress_percent, 0),
    is_running: Boolean(raw.is_running),
    pid: raw.pid === undefined || raw.pid === null || raw.pid === '' ? null : numberOr(raw.pid, null),
    consultation_pending: Boolean(raw.consultation_pending),
    consultation_gate: stringOrNull(raw.consultation_gate),
    selection_pending: Boolean(raw.selection_pending),
    selection_gate: stringOrNull(raw.selection_gate),
    selection_deadline: raw.selection_deadline === undefined || raw.selection_deadline === null || raw.selection_deadline === '' ? null : numberOr(raw.selection_deadline, null),
    contest_profile: stringOrNull(raw.contest_profile),
    contest_phase: raw.contest_phase && typeof raw.contest_phase === 'object' ? raw.contest_phase : null,
    contest_deadline_at: raw.contest_deadline_at == null ? null : numberOr(raw.contest_deadline_at, null),
    content_freeze_at: raw.content_freeze_at == null ? null : numberOr(raw.content_freeze_at, null),
    delivery_freeze_at: raw.delivery_freeze_at == null ? null : numberOr(raw.delivery_freeze_at, null),
    remaining_seconds: raw.remaining_seconds == null ? null : numberOr(raw.remaining_seconds, null),
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
  const openIssueItems = Array.isArray(raw.open_issue_items)
    ? raw.open_issue_items.map((issue) => ({
        id: String(issue?.id || ''),
        step: String(issue?.step || ''),
        severity: String(issue?.severity || ''),
        status: String(issue?.status || ''),
        location: String(issue?.location || ''),
        issue: String(issue?.issue || ''),
        required_action: String(issue?.required_action || ''),
      }))
    : []
  return {
    ...raw,
    current_step: numberOr(raw.current_step, -1),
    open_issues: numberOr(raw.open_issues, openIssueItems.length),
    open_issue_items: openIssueItems,
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

export function normalizeContestDashboard(raw = {}) {
  const timing = raw.timing && typeof raw.timing === 'object' ? raw.timing : {}
  const delivery = raw.delivery && typeof raw.delivery === 'object' ? raw.delivery : {}
  const evidence = raw.evidence && typeof raw.evidence === 'object' ? raw.evidence : {}
  const canonical = evidence.canonical && typeof evidence.canonical === 'object' ? evidence.canonical : {}
  const solver = evidence.solver && typeof evidence.solver === 'object' ? evidence.solver : {}
  return {
    schema_version: String(raw.schema_version || 'contest-dashboard-v1'),
    base_name: String(raw.base_name || ''),
    current_step: numberOr(raw.current_step, -1),
    timing: {
      ...timing,
      configured: Boolean(timing.configured),
      remaining_seconds: Math.max(0, numberOr(timing.remaining_seconds, 0)),
      recent_step_average_seconds: Math.max(0, numberOr(timing.recent_step_average_seconds, 0)),
      projected_content_finish_at: numberOr(timing.projected_content_finish_at, 0),
      content_slack_seconds: numberOr(timing.content_slack_seconds, 0),
      recent_steps: Array.isArray(timing.recent_steps) ? timing.recent_steps : [],
      risk_level: String(timing.risk_level || 'unconfigured'),
      mode: String(timing.mode || 'legacy'),
      mode_label: String(timing.mode_label || '未配置比赛时钟'),
      recommendation: String(timing.recommendation || ''),
    },
    gates: raw.gates && typeof raw.gates === 'object' ? raw.gates : {},
    audits: Array.isArray(raw.audits) ? raw.audits : [],
    evidence: {
      ...evidence,
      canonical: {
        ...canonical,
        available: Boolean(canonical.available),
        items: Array.isArray(canonical.items) ? canonical.items : [],
      },
      solver: {
        ...solver,
        total: Math.max(0, numberOr(solver.total, 0)),
        receipt_ready: Math.max(0, numberOr(solver.receipt_ready, 0)),
        failed: Math.max(0, numberOr(solver.failed, 0)),
        status_counts: solver.status_counts && typeof solver.status_counts === 'object' ? solver.status_counts : {},
      },
      role_statuses: evidence.role_statuses && typeof evidence.role_statuses === 'object' ? evidence.role_statuses : {},
    },
    delivery: {
      ...delivery,
      ready: Boolean(delivery.ready),
      checks: Array.isArray(delivery.checks) ? delivery.checks : [],
      blocking_count: Math.max(0, numberOr(delivery.blocking_count, 0)),
      pending_count: Math.max(0, numberOr(delivery.pending_count, 0)),
      release: delivery.release && typeof delivery.release === 'object' ? delivery.release : {},
      attachments: Array.isArray(delivery.attachments) ? delivery.attachments : [],
    },
    actions: Array.isArray(raw.actions) ? raw.actions : [],
  }
}

/** @returns {CloudConfig} */
export function normalizeCloudConfig(raw = {}) {
  return {
    enabled: Boolean(raw.enabled),
    revision: raw.revision == null ? null : numberOr(raw.revision, null),
    env_file: String(raw.env_file || ''),
    threshold_time: numberOr(raw.threshold_time, 300),
    solver_types: normalizeSolverTypes(raw.solver_types),
    project_id: String(raw.project_id || ''),
    region: String(raw.region || ''),
    service_name: String(raw.service_name || ''),
  }
}

export function normalizeAuthUser(raw = {}) {
  return {
    username: String(raw.username || ''),
    role: String(raw.role || 'user'),
    status: String(raw.status || ''),
    display_name: String(raw.display_name || ''),
  }
}

export function normalizeShowcasePaper(raw = {}) {
  return {
    base_name: String(raw.base_name || ''),
    title: String(raw.title || raw.base_name || ''),
    collection: String(raw.collection || 'Paper Factory'),
    updated_at: stringOrNull(raw.updated_at),
    size_bytes: Math.max(0, numberOr(raw.size_bytes, 0)),
    pdf_url: String(raw.pdf_url || ''),
  }
}

export function normalizeProjectRequest(raw = {}) {
  return {
    id: numberOr(raw.id, 0),
    requester: String(raw.requester || ''),
    base_name: String(raw.base_name || ''),
    problem_path: String(raw.problem_path || ''),
    no_start: Boolean(raw.no_start),
    consult: Boolean(raw.consult),
    status: String(raw.status || 'pending'),
    created_at: numberOr(raw.created_at, 0),
    decided_at: raw.decided_at === undefined || raw.decided_at === null || raw.decided_at === '' ? null : numberOr(raw.decided_at, null),
    decided_by: stringOrNull(raw.decided_by),
    decision_note: stringOrNull(raw.decision_note),
    launched_at: raw.launched_at === undefined || raw.launched_at === null || raw.launched_at === '' ? null : numberOr(raw.launched_at, null),
    launched_base_name: stringOrNull(raw.launched_base_name),
    launch_output: stringOrNull(raw.launch_output),
    failure_reason: stringOrNull(raw.failure_reason),
  }
}
