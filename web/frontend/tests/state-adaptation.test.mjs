import test from 'node:test'
import assert from 'node:assert/strict'
import { createProjectStore } from '../src/composables/useProjects.js'
import { normalizeProjectStatus } from '../src/lib/contracts.js'
import { stepText, currentStepIndex, projectProgress, workflowStepState, primaryControl } from '../src/lib/projectState.js'
import { statusLabel } from '../src/lib/status.js'
import { buildWorkspaceActions } from '../src/lib/workspaceUi.js'

const project = (values = {}) => normalizeProjectStatus({ base_name: 'fixture', status: 'running', execution_state: 'running',
  revision: 17, current_step: 3, source_step_id: 3, last_completed_step: 2, last_updated: '2026-09-08T00:00:00Z', ...values })

test('native Step 3 remains current during human selection', () => {
  const p = project({ status: 'awaiting_selection', execution_state: 'awaiting_selection', selection_pending: true, selection_gate: 'step3' })
  assert.equal(currentStepIndex(p), 3)
  assert.equal(stepText(p), 'Step 3 · 方法选择')
  assert.equal(workflowStepState(2, p), 'done')
  assert.equal(workflowStepState(3, p), 'attention')
  assert.equal(workflowStepState(4, p), 'pending')
  assert.equal(primaryControl(p).kind, 'navigate')
  assert.equal(primaryControl(p).tab, 'selection')
})
test('active final audit never implies completion or 100 percent', () => {
  const p = project({ current_step: 16, source_step_id: 16, last_completed_step: 15 })
  assert.equal(currentStepIndex(p), 16)
  assert.doesNotMatch(stepText(p), /已完成/)
  assert.equal(workflowStepState(16, p), 'live')
  assert.ok(projectProgress(p) < 100)
  assert.equal(projectProgress({ ...p, status: 'completed', execution_state: 'completed' }), 100)
})
test('content-freeze and reviewer-entry subtask names override checkpoint approximation', () => {
  assert.match(stepText(project({ source_step_id: 16, active_subtask: 'content_freeze_guard' })), /前置.*内容冻结/)
  assert.match(stepText(project({ source_step_id: 8, active_subtask: 'reviewer_entry_gate' })), /8\.5/)
})
test('artifact-only evidence invalidation updates lists and websocket patches without a revision bump', () => {
  const before = project({ evidence_validity: 'VALID', scientific_verdict: 'PASS', delivery_allowed: true })
  const after = { ...before, evidence_validity: 'INVALID', scientific_verdict: 'UNAVAILABLE', evidence_errors: ['input changed'], delivery_allowed: false }
  for (const method of ['applyProjects', 'patchProject']) {
    const store = createProjectStore({ projectsApi: {} })
    store.applyProjects([before])
    if (method === 'applyProjects') store.applyProjects([after]); else store.patchProject(after)
    assert.equal(store.projects.value[0].revision, before.revision)
    assert.equal(store.projects.value[0].delivery_allowed, false)
    assert.equal(store.projects.value[0].evidence_validity, 'INVALID')
    assert.deepEqual(store.projects.value[0].evidence_errors, ['input changed'])
  }
})
test('selection and consultation both enter pending counters and new-pending notifications', () => {
  const store = createProjectStore({ projectsApi: {} }), notified = []
  store.applyProjects([])
  store.applyProjects([project({ base_name: 'select', selection_pending: true }), project({ base_name: 'consult', consultation_pending: true })], base => notified.push(base))
  assert.equal(store.counts.value.needs, 2)
  assert.deepEqual(notified.sort(), ['consult', 'select'])
  assert.equal(store.others.value.length, 0)
  store.patchProject(project({ base_name: 'select', selection_pending: false }))
  assert.equal(store.counts.value.needs, 1)
})
test('interrupted worker routes to diagnosis rather than dispatching resume', () => {
  const p = project({ status: 'interrupted', execution_state: 'interrupted', recorded_workflow_state: 'running', is_running: false })
  assert.equal(primaryControl(p).kind, 'navigate')
  assert.equal(primaryControl(p).tab, 'diagnostics')
  assert.equal(workflowStepState(3, p), 'blocked')
  assert.ok(buildWorkspaceActions({}, {}, p).some(action => action.id === 'workflow-interrupted'))
})
test('new status labels and diagnostic fields survive normalization', () => {
  assert.equal(statusLabel('interrupted'), '已中断')
  assert.equal(statusLabel('retrying'), '重试中')
  assert.equal(statusLabel('archiving'), '归档中')
  const p = project({ reason_code: 'RUNNER_EXIT_UNVERIFIED', suggested_actions: ['open_runner_log'], diagnostic_badge: '运行中断' })
  assert.equal(p.reason_code, 'RUNNER_EXIT_UNVERIFIED')
  assert.equal(p.diagnostic_badge, '运行中断')
  assert.deepEqual(p.suggested_actions, ['open_runner_log'])
})
test('legacy completed-checkpoint cursor remains supported', () => {
  assert.equal(currentStepIndex(normalizeProjectStatus({ current_step: 3, status: 'ready' })), 4)
})
