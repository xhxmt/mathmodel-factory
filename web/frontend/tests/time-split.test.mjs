import assert from 'node:assert/strict'
import test from 'node:test'
import { timeSplitPresentation, timingPresentation } from '../src/lib/workspaceUi.js'

test('the timing view splits work from external waits', () => {
  const split = { working_seconds: 7200, waiting_human_seconds: 1800, waiting_provider_seconds: 1800,
    waiting_external_seconds: 3600, stopped_seconds: 0 }
  const view = timeSplitPresentation(split)
  assert.ok(view.working && view.waitingExternal && view.waitingHuman && view.waitingProvider)
  assert.notEqual(view.working, view.waitingExternal)
  assert.equal(timingPresentation({ configured: true, time_split: split }).split.working, view.working)
})

test('no split for an unconfigured clock', () => {
  assert.equal(timeSplitPresentation(undefined), null)
  assert.equal(timingPresentation({}).split, null)
})
