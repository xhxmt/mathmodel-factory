import {
  PROJECT_SNAPSHOT_VIEW_STATES,
  buildProjectSnapshotViewModel,
} from './projectSnapshotUi.js'

const SNAPSHOT_ID = /^[0-9a-f]{64}$/
const WEB_SCHEMA = 'phase6-project-snapshot-web-v1'
const PUBLIC_ERROR_CODES = new Set([
  'LEGACY_SNAPSHOT_UNAVAILABLE',
  'PHASE6_CONFIGURATION_INVALID',
  'PHASE6_GRANT_EXPIRED',
  'PHASE6_GRANT_REVOKED',
  'PHASE6_SNAPSHOT_AUTH_ERROR',
  'PHASE6_SNAPSHOT_DISABLED',
  'PHASE6_SNAPSHOT_EXPIRED',
  'PHASE6_SNAPSHOT_INCONSISTENT',
  'PHASE6_SNAPSHOT_NOT_FOUND',
  'PHASE6_SNAPSHOT_STALE',
  'PHASE6_SNAPSHOT_TAMPERED',
  'PHASE6_SNAPSHOT_UNAVAILABLE',
  'PHASE6_SOURCE_INELIGIBLE',
])

function status(state, reasonCode) {
  return buildProjectSnapshotViewModel({ state, reason_code: reasonCode })
}

/**
 * Validate the server boundary before invoking the frozen seven-state
 * projection.  A ready page is shown only for one verified revision and for
 * explicitly non-authoritative shadow data.
 */
function buildVerifiedPhase6SnapshotViewModelUnchecked(payload, expectedProjectId) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    return status(PROJECT_SNAPSHOT_VIEW_STATES.API_ERROR, 'INVALID_API_PAYLOAD')
  }
  if (
    payload.schema_version !== WEB_SCHEMA
    || typeof payload.project_id !== 'string'
    || !payload.project_id
    || (expectedProjectId !== null && payload.project_id !== expectedProjectId)
  ) {
    return status(PROJECT_SNAPSHOT_VIEW_STATES.UNKNOWN, 'INVALID_PROJECT_BINDING')
  }

  if (
    payload.authoritative !== false
    || payload.authority_transferred !== false
    || payload.dispatch_performed !== false
  ) {
    return status(PROJECT_SNAPSHOT_VIEW_STATES.UNKNOWN, 'UNSAFE_SNAPSHOT_AUTHORITY')
  }
  if (payload.state !== PROJECT_SNAPSHOT_VIEW_STATES.READY) {
    return buildProjectSnapshotViewModel(payload)
  }
  if (typeof payload.snapshot_id !== 'string' || !SNAPSHOT_ID.test(payload.snapshot_id)) {
    return status(PROJECT_SNAPSHOT_VIEW_STATES.UNKNOWN, 'UNVERIFIED_SNAPSHOT_IDENTITY')
  }
  if (
    !Number.isSafeInteger(payload.revision)
    || payload.revision < 0
    || payload.server_revision !== payload.revision
  ) {
    return status(PROJECT_SNAPSHOT_VIEW_STATES.UNKNOWN, 'MIXED_SERVER_REVISION')
  }
  if (
    !Array.isArray(payload.actions)
    || payload.actions.some((action) => (
      !action
      || typeof action !== 'object'
      || Array.isArray(action)
      || typeof action.id !== 'string'
      || !action.id.trim()
    ))
  ) {
    return status(PROJECT_SNAPSHOT_VIEW_STATES.UNKNOWN, 'INVALID_ACTION_DATA')
  }
  const actionIds = new Set()
  for (const action of payload.actions) {
    const actionId = action.id.trim()
    if (actionIds.has(actionId)) {
      return status(PROJECT_SNAPSHOT_VIEW_STATES.UNKNOWN, 'INVALID_ACTION_DATA')
    }
    actionIds.add(actionId)
  }

  const view = buildProjectSnapshotViewModel(payload)
  if (view.state !== PROJECT_SNAPSHOT_VIEW_STATES.READY) return view
  return Object.freeze({
    ...view,
    schema_version: WEB_SCHEMA,
    project_id: payload.project_id,
    server_revision: payload.server_revision,
    authoritative: false,
    authority_transferred: false,
    dispatch_performed: false,
  })
}

export function buildVerifiedPhase6SnapshotViewModel(payload, expectedProjectId = null) {
  try {
    return buildVerifiedPhase6SnapshotViewModelUnchecked(payload, expectedProjectId)
  } catch {
    return status(PROJECT_SNAPSHOT_VIEW_STATES.UNKNOWN, 'INVALID_API_PAYLOAD')
  }
}

export function buildPhase6SnapshotErrorViewModel(error) {
  const statusCode = Number(error?.status || 0)
  const suppliedCode = typeof error?.code === 'string' ? error.code : ''
  const code = PUBLIC_ERROR_CODES.has(suppliedCode)
    ? suppliedCode
    : 'PHASE6_SNAPSHOT_UNAVAILABLE'
  if (statusCode === 401 || statusCode === 403 || code === 'PHASE6_SNAPSHOT_AUTH_ERROR') {
    return status(PROJECT_SNAPSHOT_VIEW_STATES.AUTH_ERROR, 'PHASE6_SNAPSHOT_AUTH_ERROR')
  }
  if (
    statusCode === 404
    || code === 'PHASE6_SNAPSHOT_DISABLED'
    || code === 'PHASE6_SNAPSHOT_NOT_FOUND'
    || code === 'PHASE6_SOURCE_INELIGIBLE'
  ) {
    const unavailableCode = code === 'PHASE6_SNAPSHOT_UNAVAILABLE'
      ? 'PHASE6_SNAPSHOT_NOT_FOUND'
      : code
    return status(PROJECT_SNAPSHOT_VIEW_STATES.LEGACY_UNAVAILABLE, unavailableCode)
  }
  if (code === 'PHASE6_SNAPSHOT_TAMPERED' || code === 'PHASE6_SNAPSHOT_INCONSISTENT') {
    return status(PROJECT_SNAPSHOT_VIEW_STATES.UNKNOWN, code)
  }
  return status(PROJECT_SNAPSHOT_VIEW_STATES.API_ERROR, code)
}
