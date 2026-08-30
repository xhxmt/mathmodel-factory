export const PROJECT_SNAPSHOT_VIEW_STATES = Object.freeze({
  LOADING: 'loading',
  READY: 'ready',
  EMPTY: 'empty',
  LEGACY_UNAVAILABLE: 'legacy_unavailable',
  AUTH_ERROR: 'auth_error',
  API_ERROR: 'api_error',
  UNKNOWN: 'unknown',
})

const KNOWN_STATES = new Set(Object.values(PROJECT_SNAPSHOT_VIEW_STATES))
const ACTION_SEVERITY = Object.freeze({
  expired: 0,
  critical: 1,
  warning: 2,
  guarded: 3,
  info: 4,
})
const STATUS_REASON_CODES = Object.freeze({
  loading: 'SNAPSHOT_LOADING',
  empty: 'SNAPSHOT_EMPTY',
  legacy_unavailable: 'LEGACY_SNAPSHOT_UNAVAILABLE',
  auth_error: 'SNAPSHOT_AUTH_ERROR',
  api_error: 'SNAPSHOT_API_ERROR',
  unknown: 'UNKNOWN_SNAPSHOT_STATE',
})
const EMPTY_LIST = Object.freeze([])

function hasOwn(value, key) {
  return Object.prototype.hasOwnProperty.call(value, key)
}

function deepFreeze(value, seen = new WeakSet()) {
  if (value === null || typeof value !== 'object' || seen.has(value)) return value
  seen.add(value)
  for (const key of Reflect.ownKeys(value)) deepFreeze(value[key], seen)
  return Object.freeze(value)
}

function isPlainDataTree(value, seen = new WeakSet()) {
  if (value === null || typeof value !== 'object') {
    return typeof value !== 'function' && typeof value !== 'symbol'
  }
  if (seen.has(value)) return true
  seen.add(value)
  if (!Array.isArray(value)) {
    const prototype = Object.getPrototypeOf(value)
    if (prototype !== Object.prototype && prototype !== null) return false
  }
  return Reflect.ownKeys(value).every((key) => isPlainDataTree(value[key], seen))
}

function clonePlainData(value) {
  const cloned = structuredClone(value)
  if (!isPlainDataTree(cloned)) throw new TypeError('section data is not deeply freezable')
  return cloned
}

function normalizedSnapshotId(value) {
  if (typeof value !== 'string') return null
  const normalized = value.trim()
  return normalized || null
}

function normalizedCoordinate(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const snapshotId = normalizedSnapshotId(value.snapshot_id)
  if (!snapshotId || !Number.isSafeInteger(value.revision) || value.revision < 0) return null
  return { snapshot_id: snapshotId, revision: value.revision }
}

function coordinatesEqual(left, right) {
  return left.snapshot_id === right.snapshot_id && left.revision === right.revision
}

function pageCoordinate(snapshot) {
  const hasNested = hasOwn(snapshot, 'coordinate')
  const hasFlat = hasOwn(snapshot, 'snapshot_id') || hasOwn(snapshot, 'revision')
  if (!hasNested && !hasFlat) return null

  const nested = hasNested ? normalizedCoordinate(snapshot.coordinate) : null
  const flat = hasFlat
    ? normalizedCoordinate({ snapshot_id: snapshot.snapshot_id, revision: snapshot.revision })
    : null
  if ((hasNested && !nested) || (hasFlat && !flat)) return null
  if (nested && flat && !coordinatesEqual(nested, flat)) return null
  return deepFreeze(nested || flat)
}

function sectionUsesPageCoordinate(section, coordinate) {
  const hasNested = hasOwn(section, 'coordinate')
  const hasFlat = hasOwn(section, 'snapshot_id') || hasOwn(section, 'revision')
  if (!hasNested && !hasFlat) return true

  const candidates = []
  if (hasNested) candidates.push(normalizedCoordinate(section.coordinate))
  if (hasFlat) {
    candidates.push(
      normalizedCoordinate({ snapshot_id: section.snapshot_id, revision: section.revision }),
    )
  }
  return candidates.every(
    (candidate) => candidate !== null && coordinatesEqual(candidate, coordinate),
  )
}

function statusReason(snapshot, state) {
  if (typeof snapshot.reason_code === 'string' && snapshot.reason_code.trim()) {
    return snapshot.reason_code.trim()
  }
  return STATUS_REASON_CODES[state]
}

function statusProjection(state, reasonCode) {
  const status = deepFreeze({ state, reason_code: reasonCode })
  const actionCenter = deepFreeze({
    mode: 'status',
    clear: false,
    interactive: false,
    actions: EMPTY_LIST,
    coordinate: null,
    snapshot_id: null,
    revision: null,
  })
  return deepFreeze({
    state,
    reason_code: reasonCode,
    status,
    coordinate: null,
    snapshot_id: null,
    revision: null,
    sections: EMPTY_LIST,
    actionCenter,
  })
}

function unknownProjection(reasonCode) {
  return statusProjection(PROJECT_SNAPSHOT_VIEW_STATES.UNKNOWN, reasonCode)
}

function buildSections(rawSections, coordinate) {
  if (rawSections === undefined) return { sections: EMPTY_LIST, reason_code: null }
  if (!Array.isArray(rawSections)) {
    return { sections: EMPTY_LIST, reason_code: 'INVALID_SECTION_DATA' }
  }

  const seenKeys = new Set()
  const sections = []
  for (const rawSection of rawSections) {
    if (!rawSection || typeof rawSection !== 'object' || Array.isArray(rawSection)) {
      return { sections: EMPTY_LIST, reason_code: 'INVALID_SECTION_IDENTITY' }
    }
    const key = typeof rawSection.key === 'string' ? rawSection.key.trim() : ''
    if (!key || seenKeys.has(key)) {
      return { sections: EMPTY_LIST, reason_code: 'INVALID_SECTION_IDENTITY' }
    }
    if (!sectionUsesPageCoordinate(rawSection, coordinate)) {
      return { sections: EMPTY_LIST, reason_code: 'MIXED_SNAPSHOT_COORDINATE' }
    }
    if (!hasOwn(rawSection, 'data')) {
      return { sections: EMPTY_LIST, reason_code: 'INVALID_SECTION_DATA' }
    }

    let data
    try {
      data = deepFreeze(clonePlainData(rawSection.data))
    } catch {
      return { sections: EMPTY_LIST, reason_code: 'INVALID_SECTION_DATA' }
    }
    seenKeys.add(key)
    sections.push(
      deepFreeze({
        key,
        data,
        coordinate,
        snapshot_id: coordinate.snapshot_id,
        revision: coordinate.revision,
      }),
    )
  }
  return { sections: deepFreeze(sections), reason_code: null }
}

function buildActions(rawActions, coordinate) {
  if (rawActions === undefined) return { actions: EMPTY_LIST, reason_code: null }
  if (!Array.isArray(rawActions)) {
    return { actions: EMPTY_LIST, reason_code: 'INVALID_ACTION_DATA' }
  }

  const seenIds = new Set()
  const actions = []
  for (const rawAction of rawActions) {
    if (!rawAction || typeof rawAction !== 'object' || Array.isArray(rawAction)) {
      return { actions: EMPTY_LIST, reason_code: 'INVALID_ACTION_DATA' }
    }
    const id = typeof rawAction.id === 'string' ? rawAction.id.trim() : ''
    if (!id || seenIds.has(id)) {
      return { actions: EMPTY_LIST, reason_code: 'INVALID_ACTION_DATA' }
    }
    if (!sectionUsesPageCoordinate(rawAction, coordinate)) {
      return { actions: EMPTY_LIST, reason_code: 'MIXED_SNAPSHOT_COORDINATE' }
    }

    let action
    try {
      action = clonePlainData(rawAction)
    } catch {
      return { actions: EMPTY_LIST, reason_code: 'INVALID_ACTION_DATA' }
    }
    action.id = id
    action.severity = hasOwn(ACTION_SEVERITY, action.severity) ? action.severity : 'info'
    action.coordinate = coordinate
    action.snapshot_id = coordinate.snapshot_id
    action.revision = coordinate.revision
    seenIds.add(id)
    actions.push(deepFreeze(action))
  }
  actions.sort((left, right) => {
    const severityOrder = ACTION_SEVERITY[left.severity] - ACTION_SEVERITY[right.severity]
    if (severityOrder) return severityOrder
    return left.id < right.id ? -1 : left.id > right.id ? 1 : 0
  })
  return { actions: deepFreeze(actions), reason_code: null }
}

function buildProjectSnapshotViewModelUnchecked(snapshot) {
  const source = snapshot && typeof snapshot === 'object' ? snapshot : {}
  const requestedState = typeof source.state === 'string' ? source.state : ''
  if (!KNOWN_STATES.has(requestedState)) {
    return unknownProjection('UNKNOWN_SNAPSHOT_STATE')
  }
  if (requestedState !== PROJECT_SNAPSHOT_VIEW_STATES.READY) {
    return statusProjection(requestedState, statusReason(source, requestedState))
  }

  const coordinate = pageCoordinate(source)
  if (!coordinate) return unknownProjection('INVALID_SNAPSHOT_COORDINATE')

  const sectionResult = buildSections(source.sections, coordinate)
  if (sectionResult.reason_code) return unknownProjection(sectionResult.reason_code)

  const actionResult = buildActions(source.actions, coordinate)
  if (actionResult.reason_code) return unknownProjection(actionResult.reason_code)

  const hasActions = actionResult.actions.length > 0
  const hasInteractiveActions = actionResult.actions.some((action) => action.disabled !== true)
  const actionCenter = deepFreeze({
    mode: hasActions ? 'actions' : 'clear',
    clear: !hasActions,
    interactive: hasInteractiveActions,
    actions: actionResult.actions,
    coordinate,
    snapshot_id: coordinate.snapshot_id,
    revision: coordinate.revision,
  })
  return deepFreeze({
    state: PROJECT_SNAPSHOT_VIEW_STATES.READY,
    reason_code: null,
    status: null,
    coordinate,
    snapshot_id: coordinate.snapshot_id,
    revision: coordinate.revision,
    sections: sectionResult.sections,
    actionCenter,
  })
}

export function buildProjectSnapshotViewModel(snapshot = {}) {
  try {
    return buildProjectSnapshotViewModelUnchecked(snapshot)
  } catch {
    return unknownProjection('INVALID_SNAPSHOT_DATA')
  }
}
