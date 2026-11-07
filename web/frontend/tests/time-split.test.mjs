import assert from 'node:assert/strict'
import test from 'node:test'
import { timeSplitPresentation, timingPresentation } from '../src/lib/workspaceUi.js'

test('the timing view splits work from external waits', () => {
  // Distinct values per field, so a swapped or mis-keyed mapping fails.
  const split = { working_seconds: 7200, waiting_human_seconds: 1800, waiting_provider_seconds: 600,
    waiting_external_seconds: 2400, stopped_seconds: 90 }
  const view = timeSplitPresentation(split)
  assert.deepEqual(view, {
    working: '2h 00m',
    waitingExternal: '40m',
    waitingHuman: '30m',
    waitingProvider: '10m',
    stopped: '1m',
  })
  assert.deepEqual(timingPresentation({ configured: true, time_split: split }).split, view)
})

test('no split for an unconfigured clock', () => {
  assert.equal(timeSplitPresentation(undefined), null)
  assert.equal(timingPresentation({}).split, null)
})
