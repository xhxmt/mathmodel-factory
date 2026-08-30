import assert from 'node:assert/strict'
import test from 'node:test'

import { createPhase6ProjectSnapshotController } from '../src/composables/usePhase6ProjectSnapshot.js'
import {
  Phase6SnapshotClientError,
  createPhase6SnapshotRequestCoordinator,
  requestPhase6ProjectSnapshot,
  resolvePhase6SnapshotDeadlineMs,
} from '../src/lib/phase6SnapshotClient.js'
import {
  buildPhase6SnapshotErrorViewModel,
  buildVerifiedPhase6SnapshotViewModel,
} from '../src/lib/phase6SnapshotProjection.js'

const SNAPSHOT_ID = 'b'.repeat(64)

function readyPayload(projectId = 'demo', revision = 7, actions = []) {
  return {
    schema_version: 'phase6-project-snapshot-web-v1',
    state: 'ready',
    project_id: projectId,
    snapshot_id: SNAPSHOT_ID,
    revision,
    server_revision: revision,
    coordinate: { snapshot_id: SNAPSHOT_ID, revision },
    sections: [{ key: 'summary', data: { ok: true } }],
    actions,
    authoritative: false,
    authority_transferred: false,
    dispatch_performed: false,
  }
}

function deferred() {
  let resolve
  let reject
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

function abortError(message = 'transport aborted') {
  const error = new Error(message)
  error.name = 'AbortError'
  return error
}

function nextTurn() {
  return new Promise((resolve) => setImmediate(resolve))
}

function fakeClock() {
  let now = 0
  let nextId = 1
  const timers = new Map()
  return {
    setTimeout(callback, delay) {
      const id = nextId++
      timers.set(id, { at: now + delay, callback })
      return id
    },
    clearTimeout(id) {
      timers.delete(id)
    },
    advance(milliseconds) {
      now += milliseconds
      const scheduled = [...timers.entries()].sort((left, right) => left[1].at - right[1].at)
      for (const [id, timer] of scheduled) {
        if (timer.at <= now) {
          timers.delete(id)
          timer.callback()
        }
      }
    },
    pending() {
      return timers.size
    },
  }
}

test('deadline configuration is strict and has a bounded default', () => {
  assert.equal(resolvePhase6SnapshotDeadlineMs(), 15_000)
  assert.equal(resolvePhase6SnapshotDeadlineMs('2500'), 2500)
  for (const value of [0, -1, 1.5, '0', '1.5', ' 20 ', 300_001, Infinity]) {
    assert.throws(() => resolvePhase6SnapshotDeadlineMs(value), TypeError)
  }
})

test('real deadline settles a transport that ignores AbortSignal as timeout', async () => {
  const clock = fakeClock()
  let observedSignal
  const controller = createPhase6ProjectSnapshotController({
    logger: null,
    deadlineMs: 25,
    clock,
    request: async (_project, { signal }) => {
      observedSignal = signal
      return new Promise(() => {})
    },
  })
  const pending = controller.load('demo')
  await nextTurn()
  assert.equal(controller.loading.value, true)
  clock.advance(25)
  const result = await pending
  assert.equal(observedSignal.aborted, true)
  assert.equal(result.state, 'api_error')
  assert.equal(result.reason_code, 'PHASE6_SNAPSHOT_UNAVAILABLE')
  assert.equal(controller.loading.value, false)
  assert.equal(clock.pending(), 0)
})

test('deadline covers a fetch that never returns response headers', async () => {
  const clock = fakeClock()
  let fetchSignal
  const pending = requestPhase6ProjectSnapshot('demo', {
    deadlineMs: 30,
    clock,
    tokenProvider: () => '',
    fetchImpl: async (_url, { signal }) => {
      fetchSignal = signal
      return new Promise(() => {})
    },
  })
  await nextTurn()
  clock.advance(30)
  await assert.rejects(pending, (error) => {
    assert.equal(error.code, 'PHASE6_SNAPSHOT_UNAVAILABLE')
    assert.equal(error.requestReason, 'timeout')
    return true
  })
  assert.equal(fetchSignal.aborted, true)
  assert.equal(clock.pending(), 0)
})

test('deadline covers response headers and a body parser that never resolves', async () => {
  const clock = fakeClock()
  let fetchSignal
  const pending = requestPhase6ProjectSnapshot('demo', {
    deadlineMs: 30,
    clock,
    tokenProvider: () => '',
    fetchImpl: async (_url, { signal }) => {
      fetchSignal = signal
      return { ok: true, status: 200, json: async () => new Promise(() => {}) }
    },
  })
  await nextTurn()
  clock.advance(30)
  await assert.rejects(pending, (error) => {
    assert.equal(error.code, 'PHASE6_SNAPSHOT_UNAVAILABLE')
    assert.equal(error.requestReason, 'timeout')
    return true
  })
  assert.equal(fetchSignal.aborted, true)
  assert.equal(clock.pending(), 0)
})

test('timeout classification wins over a synchronous abort-listener rejection', async () => {
  const clock = fakeClock()
  const coordinator = createPhase6SnapshotRequestCoordinator({
    deadlineMs: 20,
    clock,
    request: async (_project, { signal }) => new Promise((_resolve, reject) => {
      signal.addEventListener('abort', () => reject(abortError()), { once: true })
    }),
  })
  const pending = coordinator.load('demo')
  await nextTurn()
  clock.advance(20)
  const result = await pending
  assert.equal(result.applied, true)
  assert.equal(result.reason, 'timeout')
  assert.equal(result.error.requestReason, 'timeout')
  assert.equal(clock.pending(), 0)
})

test('one stale retry shares the original total deadline', async () => {
  const clock = fakeClock()
  const first = deferred()
  const calls = []
  const coordinator = createPhase6SnapshotRequestCoordinator({
    deadlineMs: 100,
    clock,
    request: async (_project, { expectedRevision }) => {
      calls.push(expectedRevision)
      if (calls.length === 1) return first.promise
      return new Promise(() => {})
    },
  })
  const pending = coordinator.load('demo', { expectedRevision: 7 })
  await nextTurn()
  clock.advance(60)
  first.reject(new Phase6SnapshotClientError('PHASE6_SNAPSHOT_STALE', { status: 409 }))
  await nextTurn()
  assert.deepEqual(calls, [7, null])
  clock.advance(39)
  assert.equal(clock.pending(), 1)
  clock.advance(1)
  const result = await pending
  assert.equal(result.applied, true)
  assert.equal(result.reason, 'timeout')
  assert.equal(result.retried, true)
  assert.equal(calls.length, 2)
  assert.equal(clock.pending(), 0)
})

test('cancel reasons are distinct and ignored old work cannot overwrite a new generation', async () => {
  const clock = fakeClock()
  for (const reason of ['user_cancel', 'navigation', 'reset']) {
    const ignored = deferred()
    const coordinator = createPhase6SnapshotRequestCoordinator({
      deadlineMs: 100,
      clock,
      request: async () => ignored.promise,
    })
    const canceled = coordinator.load(`cancel-${reason}`)
    await nextTurn()
    coordinator.cancel(reason)
    const canceledResult = await canceled
    assert.equal(canceledResult.reason, 'stale_generation')
    assert.equal(canceledResult.cancellationReason, reason)
    assert.equal(clock.pending(), 0)
    ignored.resolve(readyPayload(`cancel-${reason}`))
  }

  const lateOld = deferred()
  const controller = createPhase6ProjectSnapshotController({
    logger: null,
    deadlineMs: 50,
    clock,
    request: async (project) => (
      project === 'old' ? lateOld.promise : readyPayload('new', 9)
    ),
  })
  const oldLoad = controller.load('old')
  await nextTurn()
  const newest = await controller.load('new')
  assert.equal(newest.project_id, 'new')
  assert.equal(await oldLoad, null)
  clock.advance(100)
  lateOld.resolve(readyPayload('old', 1))
  await nextTurn()
  assert.equal(controller.viewModel.value.project_id, 'new')
  assert.equal(controller.viewModel.value.revision, 9)
  assert.equal(controller.loading.value, false)
  assert.equal(clock.pending(), 0)
})

test('duplicate action IDs fail closed independently of input order or payload equality', () => {
  const duplicateCases = [
    [
      { id: 'review', severity: 'warning', payload: { tab: 'one' } },
      { id: ' review ', severity: 'warning', payload: { tab: 'one' } },
    ],
    [
      { id: ' review ', severity: 'critical', payload: { tab: 'two' } },
      { id: 'review', severity: 'warning', payload: { tab: 'one' } },
    ],
    [
      { id: 'review', severity: 'warning', payload: { tab: 'one' } },
      { id: 'review', severity: 'critical', payload: { tab: 'two' } },
    ],
  ]

  for (const actions of duplicateCases) {
    const view = buildVerifiedPhase6SnapshotViewModel(readyPayload('demo', 7, actions), 'demo')
    assert.equal(view.state, 'unknown')
    assert.equal(view.reason_code, 'INVALID_ACTION_DATA')
    assert.deepEqual(view.actionCenter.actions, [])
    assert.equal(view.actionCenter.interactive, false)
  }

  const unique = buildVerifiedPhase6SnapshotViewModel(readyPayload('demo', 7, [
    { id: 'later', severity: 'warning' },
    { id: 'first', severity: 'critical' },
  ]), 'demo')
  assert.equal(unique.state, 'ready')
  assert.deepEqual(unique.actionCenter.actions.map(({ id }) => id), ['first', 'later'])
})

test('unknown adapter, backend, and local errors collapse to one safe public code', () => {
  const secret = 'Bearer should-not-render /srv/private/store.db SELECT token FROM acl'
  const cases = [
    Object.assign(new Error(secret), { code: 'SQL_DRIVER_STACK_WITH_PATH', status: 500 }),
    Object.assign(new Error(secret), { status: 502 }),
    new TypeError(secret),
  ]
  for (const error of cases) {
    const view = buildPhase6SnapshotErrorViewModel(error)
    assert.equal(view.state, 'api_error')
    assert.equal(view.reason_code, 'PHASE6_SNAPSHOT_UNAVAILABLE')
    assert.equal(JSON.stringify(view).includes(secret), false)
  }

  const known = buildPhase6SnapshotErrorViewModel(
    new Phase6SnapshotClientError('PHASE6_SNAPSHOT_TAMPERED', { status: 503 }),
  )
  assert.equal(known.state, 'unknown')
  assert.equal(known.reason_code, 'PHASE6_SNAPSHOT_TAMPERED')
})

test('a current-generation AbortError settles loading as a safe API error', async () => {
  const logs = []
  const secret = 'timeout at /srv/private/snapshot.db with Bearer credential'
  const controller = createPhase6ProjectSnapshotController({
    request: async () => { throw abortError(secret) },
    logger: { warn: (...fields) => logs.push(fields) },
  })

  const result = await controller.load('demo')
  assert.equal(controller.loading.value, false)
  assert.equal(result.state, 'api_error')
  assert.equal(result.reason_code, 'PHASE6_SNAPSHOT_UNAVAILABLE')
  assert.equal(JSON.stringify(controller.viewModel.value).includes(secret), false)
  assert.equal(JSON.stringify(logs).includes(secret), false)
})

test('old-generation AbortError and completion cannot overwrite a newer load', async () => {
  const newer = deferred()
  const controller = createPhase6ProjectSnapshotController({
    logger: null,
    request: async (projectId, { signal }) => {
      if (projectId === 'old') {
        return new Promise((_resolve, reject) => {
          signal.addEventListener('abort', () => reject(abortError()), { once: true })
        })
      }
      return newer.promise
    },
  })

  const oldLoad = controller.load('old')
  await nextTurn()
  const newLoad = controller.load('new')
  await nextTurn()
  assert.equal(controller.loading.value, true)

  newer.resolve(readyPayload('new', 8))
  const [oldResult, newResult] = await Promise.all([oldLoad, newLoad])
  assert.equal(oldResult, null)
  assert.equal(newResult.project_id, 'new')
  assert.equal(controller.loading.value, false)
  assert.equal(controller.viewModel.value.project_id, 'new')

  const ignoredOld = deferred()
  const completionFence = createPhase6ProjectSnapshotController({
    logger: null,
    request: async (projectId) => (
      projectId === 'ignored-old' ? ignoredOld.promise : readyPayload('newest', 9)
    ),
  })
  const ignoredLoad = completionFence.load('ignored-old')
  await nextTurn()
  await completionFence.load('newest')
  ignoredOld.resolve(readyPayload('ignored-old', 1))
  assert.equal(await ignoredLoad, null)
  assert.equal(completionFence.viewModel.value.project_id, 'newest')
  assert.equal(completionFence.viewModel.value.revision, 9)
})

test('consecutive requests, user cancel, and navigation stop always settle loading', async () => {
  const pendingCancel = deferred()
  const cancelController = createPhase6ProjectSnapshotController({
    logger: null,
    request: async () => pendingCancel.promise,
  })
  const canceled = cancelController.load('cancel-me')
  await nextTurn()
  assert.equal(cancelController.loading.value, true)
  cancelController.cancel()
  assert.equal(cancelController.loading.value, false)
  assert.equal(await canceled, null)
  assert.notEqual(cancelController.viewModel.value.state, 'ready')
  pendingCancel.resolve(readyPayload('cancel-me'))
  await nextTurn()
  assert.notEqual(cancelController.viewModel.value.state, 'ready')

  const pendingUnmount = deferred()
  const navigationController = createPhase6ProjectSnapshotController({
    logger: null,
    request: async () => pendingUnmount.promise,
  })
  const navigating = navigationController.load('navigate-away')
  await nextTurn()
  navigationController.stop()
  assert.equal(navigationController.loading.value, false)
  assert.equal(await navigating, null)
  assert.notEqual(navigationController.viewModel.value.state, 'ready')
  pendingUnmount.resolve(readyPayload('navigate-away'))
  await nextTurn()
  assert.notEqual(navigationController.viewModel.value.state, 'ready')
})

test('one stale retry is bounded and a stale old generation never retries', async () => {
  const expectedRevisions = []
  const controller = createPhase6ProjectSnapshotController({
    logger: null,
    request: async (_projectId, { expectedRevision }) => {
      expectedRevisions.push(expectedRevision)
      if (expectedRevisions.length === 1) return readyPayload('demo', 7)
      if (expectedRevisions.length === 2) {
        throw new Phase6SnapshotClientError('PHASE6_SNAPSHOT_STALE', {
          status: 409,
          serverRevision: 8,
        })
      }
      return readyPayload('demo', 8)
    },
  })
  await controller.load('demo')
  const refreshed = await controller.load('demo')
  assert.equal(refreshed.revision, 8)
  assert.deepEqual(expectedRevisions, [null, 7, null])

  const staleOld = deferred()
  let oldCalls = 0
  const coordinator = createPhase6SnapshotRequestCoordinator({
    request: async (projectId) => {
      if (projectId === 'old') {
        oldCalls += 1
        return staleOld.promise
      }
      return readyPayload('new')
    },
  })
  const old = coordinator.load('old', { expectedRevision: 7 })
  await nextTurn()
  await coordinator.load('new')
  staleOld.reject(new Phase6SnapshotClientError('PHASE6_SNAPSHOT_STALE', { status: 409 }))
  const oldResult = await old
  assert.equal(oldResult.applied, false)
  assert.equal(oldCalls, 1)
})
