const PUBLIC_ERROR_CODES = new Set([
  'PHASE6_SNAPSHOT_DISABLED',
  'PHASE6_SNAPSHOT_STALE',
  'PHASE6_SNAPSHOT_NOT_FOUND',
  'PHASE6_SOURCE_INELIGIBLE',
  'PHASE6_SNAPSHOT_EXPIRED',
  'PHASE6_GRANT_EXPIRED',
  'PHASE6_GRANT_REVOKED',
  'PHASE6_SNAPSHOT_TAMPERED',
  'PHASE6_SNAPSHOT_INCONSISTENT',
  'PHASE6_SNAPSHOT_UNAVAILABLE',
  'PHASE6_CONFIGURATION_INVALID',
])

export const DEFAULT_PHASE6_SNAPSHOT_DEADLINE_MS = 15_000
export const MAX_PHASE6_SNAPSHOT_DEADLINE_MS = 300_000

const DEFAULT_DEADLINE_CLOCK = Object.freeze({
  setTimeout(callback, delay) {
    return globalThis.setTimeout(callback, delay)
  },
  clearTimeout(timer) {
    globalThis.clearTimeout(timer)
  },
})

export class Phase6SnapshotClientError extends Error {
  constructor(code, { status = 0, serverRevision = null, requestReason = null } = {}) {
    super(code)
    this.name = 'Phase6SnapshotClientError'
    this.code = code
    this.status = status
    this.serverRevision = serverRevision
    this.requestReason = requestReason
  }
}

export function resolvePhase6SnapshotDeadlineMs(value) {
  if (value === undefined || value === null || value === '') {
    return DEFAULT_PHASE6_SNAPSHOT_DEADLINE_MS
  }
  const parsed = typeof value === 'string' && /^[1-9][0-9]*$/.test(value)
    ? Number(value)
    : value
  if (!Number.isSafeInteger(parsed) || parsed <= 0 || parsed > MAX_PHASE6_SNAPSHOT_DEADLINE_MS) {
    throw new TypeError(
      `Phase 6 snapshot deadline must be an integer from 1 to ${MAX_PHASE6_SNAPSHOT_DEADLINE_MS} ms`,
    )
  }
  return parsed
}

function requestCancellationError(reason) {
  const error = new Error('Phase 6 snapshot request canceled')
  error.name = 'AbortError'
  error.requestReason = reason
  return error
}

function requestTimeoutError() {
  return new Phase6SnapshotClientError('PHASE6_SNAPSHOT_UNAVAILABLE', {
    requestReason: 'timeout',
  })
}

function createRequestDeadline({
  deadlineMs,
  externalSignal,
  clock = DEFAULT_DEADLINE_CLOCK,
} = {}) {
  const budgetMs = resolvePhase6SnapshotDeadlineMs(deadlineMs)
  if (!clock || typeof clock.setTimeout !== 'function' || typeof clock.clearTimeout !== 'function') {
    throw new TypeError('Phase 6 snapshot deadline clock is invalid')
  }

  const controller = new AbortController()
  let disposed = false
  let terminationError = null
  let rejectTermination
  const termination = new Promise((_resolve, reject) => {
    rejectTermination = reject
  })
  void termination.catch(() => {})

  function terminate(error) {
    if (disposed || terminationError) return
    terminationError = error
    rejectTermination(error)
    if (!controller.signal.aborted) controller.abort(error)
  }

  const timer = clock.setTimeout(() => terminate(requestTimeoutError()), budgetMs)
  const handleExternalAbort = () => terminate(requestCancellationError('external_cancel'))
  if (externalSignal) {
    if (externalSignal.aborted) handleExternalAbort()
    else externalSignal.addEventListener('abort', handleExternalAbort, { once: true })
  }

  return Object.freeze({
    signal: controller.signal,
    run(task) {
      if (typeof task !== 'function') throw new TypeError('deadline task must be a function')
      if (terminationError) return Promise.reject(terminationError)
      return Promise.race([Promise.resolve().then(task), termination])
    },
    cancel(reason = 'user_cancel') {
      terminate(requestCancellationError(reason))
    },
    dispose() {
      if (disposed) return
      disposed = true
      clock.clearTimeout(timer)
      externalSignal?.removeEventListener('abort', handleExternalAbort)
    },
  })
}

export function phase6SnapshotUrl(baseName, expectedRevision = null) {
  const path = `/api/projects/${encodeURIComponent(String(baseName))}/phase6-snapshot`
  if (expectedRevision === null || expectedRevision === undefined) return path
  if (!Number.isSafeInteger(expectedRevision) || expectedRevision < 0) {
    throw new TypeError('expectedRevision must be a non-negative safe integer')
  }
  return `${path}?expected_revision=${expectedRevision}`
}

async function responseBody(response, deadline) {
  try {
    return await deadline.run(() => response.json())
  } catch (error) {
    if (error?.name === 'AbortError' || error?.requestReason === 'timeout') throw error
    return null
  }
}

function publicError(response, body) {
  const rawDetail = body?.detail
  const rawCode = typeof rawDetail === 'object' ? rawDetail?.code : rawDetail
  let code = PUBLIC_ERROR_CODES.has(rawCode) ? rawCode : 'PHASE6_SNAPSHOT_UNAVAILABLE'
  if (response.status === 401 || response.status === 403) code = 'PHASE6_SNAPSHOT_AUTH_ERROR'
  const rawRevision = typeof rawDetail === 'object' ? rawDetail?.server_revision : null
  const serverRevision = Number.isSafeInteger(rawRevision) && rawRevision >= 0 ? rawRevision : null
  return new Phase6SnapshotClientError(code, { status: response.status, serverRevision })
}

export async function requestPhase6ProjectSnapshot(
  baseName,
  {
    expectedRevision = null,
    signal,
    deadlineMs,
    deadlineContext = null,
    clock,
    fetchImpl = globalThis.fetch,
    tokenProvider = () => globalThis.localStorage?.getItem('access_token') || '',
  } = {},
) {
  if (typeof fetchImpl !== 'function') {
    throw new Phase6SnapshotClientError('PHASE6_SNAPSHOT_UNAVAILABLE')
  }
  const token = tokenProvider()
  const headers = { Accept: 'application/json' }
  if (token) headers.Authorization = `Bearer ${token}`
  const ownsDeadline = deadlineContext === null
  const deadline = deadlineContext || createRequestDeadline({
    deadlineMs,
    externalSignal: signal,
    clock,
  })
  try {
    let response
    try {
      response = await deadline.run(() => fetchImpl(
        phase6SnapshotUrl(baseName, expectedRevision),
        {
          method: 'GET',
          headers,
          credentials: 'same-origin',
          cache: 'no-store',
          signal: deadline.signal,
        },
      ))
    } catch (error) {
      if (error?.name === 'AbortError' || error?.requestReason === 'timeout') throw error
      throw new Phase6SnapshotClientError('PHASE6_SNAPSHOT_UNAVAILABLE')
    }
    const body = await responseBody(response, deadline)
    if (!response.ok) throw publicError(response, body)
    if (!body || typeof body !== 'object' || Array.isArray(body)) {
      throw new Phase6SnapshotClientError('PHASE6_SNAPSHOT_UNAVAILABLE', {
        status: response.status,
      })
    }
    return body
  } finally {
    if (ownsDeadline) deadline.dispose()
  }
}

function stalePayload(payload, expectedRevision) {
  return expectedRevision !== null
    && expectedRevision !== undefined
    && payload?.server_revision !== expectedRevision
}

/**
 * Coordinate one active request.  A stale server revision is refreshed once,
 * never recursively.  Aborting is an optimization; the generation fence is
 * the correctness boundary when a transport ignores AbortSignal.
 */
export function createPhase6SnapshotRequestCoordinator({
  request = requestPhase6ProjectSnapshot,
  deadlineMs = DEFAULT_PHASE6_SNAPSHOT_DEADLINE_MS,
  clock = DEFAULT_DEADLINE_CLOCK,
} = {}) {
  const requestDeadlineMs = resolvePhase6SnapshotDeadlineMs(deadlineMs)
  let generation = 0
  let activeRequest = null

  function cancel(reason = 'user_cancel') {
    generation += 1
    const canceled = activeRequest
    activeRequest = null
    canceled?.deadline.cancel(reason)
  }

  async function load(baseName, { expectedRevision = null } = {}) {
    if (!baseName) return { applied: false, reason: 'missing_project' }
    cancel('superseded')
    const requestGeneration = generation
    const deadline = createRequestDeadline({ deadlineMs: requestDeadlineMs, clock })
    activeRequest = { generation: requestGeneration, deadline }
    let retried = false

    const requestWithinBudget = (revision) => deadline.run(() => request(baseName, {
      expectedRevision: revision,
      signal: deadline.signal,
      deadlineMs: requestDeadlineMs,
      deadlineContext: deadline,
    }))

    try {
      let payload
      try {
        payload = await requestWithinBudget(expectedRevision)
        if (stalePayload(payload, expectedRevision)) {
          throw new Phase6SnapshotClientError('PHASE6_SNAPSHOT_STALE', {
            status: 409,
            serverRevision: payload?.server_revision,
          })
        }
      } catch (error) {
        if (error?.name === 'AbortError' || error?.requestReason === 'timeout') throw error
        if (error?.code !== 'PHASE6_SNAPSHOT_STALE') throw error
        if (requestGeneration !== generation) {
          return { applied: false, reason: 'stale_generation', retried: false }
        }
        retried = true
        payload = await requestWithinBudget(null)
      }

      if (requestGeneration !== generation) {
        return { applied: false, reason: 'stale_generation', retried }
      }
      return { applied: true, payload, retried }
    } catch (error) {
      if (requestGeneration !== generation) {
        return {
          applied: false,
          reason: 'stale_generation',
          cancellationReason: error?.requestReason || 'superseded',
          retried,
        }
      }
      if (error?.requestReason === 'timeout') {
        return { applied: true, reason: 'timeout', error, retried }
      }
      if (error?.name === 'AbortError') {
        return {
          applied: false,
          reason: 'aborted',
          cancellationReason: error?.requestReason || 'transport_abort',
          retried,
        }
      }
      return { applied: true, error, retried }
    } finally {
      deadline.dispose()
      if (activeRequest?.generation === requestGeneration) activeRequest = null
    }
  }

  return Object.freeze({ cancel, load })
}
