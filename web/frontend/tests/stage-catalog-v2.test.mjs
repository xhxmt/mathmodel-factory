import assert from 'node:assert/strict'
import { projectStepIndex, projectStepName, projectStepStatus, stepsInOrder } from '../src/lib/steps.js'
import { currentStage, currentStepIndex, stepText, workflowStepState } from '../src/lib/projectState.js'
import { phaseForStep } from '../src/lib/workspaceUi.js'
import { contestPhasesFor, nextStepAfter, stepRangeLabel } from '../src/lib/stageCatalog.js'

const V2 = 'factory-stage-catalog-v2'
const base = { scheduler_generation: 'stage_v1', stage_catalog_version: V2 }

// v2 runs 14 and 15 before 13.
assert.deepEqual(stepsInOrder(base).map((s) => s.index).slice(11), [11, 12, 14, 15, 13, 16])
assert.deepEqual(stepsInOrder({}).map((s) => s.index).slice(11), [11, 12, 13, 14, 15, 16])
assert.equal(nextStepAfter(12, base), 14)
assert.equal(nextStepAfter(15, base), 13)
assert.equal(nextStepAfter(13, base), 16)
assert.equal(nextStepAfter(12, {}), 13)

// After Step 15, in the content-freeze gate: 13 is not done, 14/15 are.
const gate = { ...base, status: 'awaiting_selection', last_completed_step: 15, source_step_id: 16,
  active_stage: 10, active_subtask: 'content_freeze_guard', selection_gate: 'content_freeze' }
assert.equal(projectStepStatus(13, gate), 'pending')
assert.equal(projectStepStatus(15, gate), 'done')
assert.equal(workflowStepState(13, { ...gate, status: 'running' }), 'pending')
assert.equal(stepText(gate), '最终审计前 · 内容冻结确认')

// Running the final Judge: Step 13 active, 14/15 done, 16 pending.
const judge = { ...base, status: 'running', last_completed_step: 15, source_step_id: 13,
  active_stage: 10, active_subtask: 'final_judge' }
assert.equal(projectStepIndex(judge), 13)
assert.equal(projectStepName(judge), '最终审计（唯一一次 Judge）')
assert.equal(projectStepStatus(14, judge), 'done')
assert.equal(projectStepStatus(13, judge), 'active')
assert.equal(workflowStepState(16, judge), 'pending')
assert.equal(currentStage(judge).name, '终审与交付')

// After the Judge (last completed 13): 14/15 stay done, 16 is next.
const accept = { ...base, status: 'ready', last_completed_step: 13 }
assert.equal(currentStepIndex(accept), 16)
assert.equal(projectStepIndex(accept), 16)
assert.equal(workflowStepState(14, accept), 'done')
assert.equal(workflowStepState(15, accept), 'done')
assert.equal(projectStepName({ ...accept, source_step_id: 16 }), '验收·交付')

// Contest phases: v2 moves Step 13 into final audit & delivery.
assert.deepEqual(contestPhasesFor(base).map((p) => p.steps).slice(6), [[11, 12, 14, 15], [13, 16]])
assert.equal(phaseForStep(13, base).key, 'final_audit_and_delivery')
assert.equal(phaseForStep(13, {}).key, 'review_and_revision')
assert.equal(stepRangeLabel([13, 16]), '13、16')
assert.equal(stepRangeLabel([11, 12, 13]), '11–13')
assert.equal(stepRangeLabel([8, '8_5', 9]), '8–9')

// v1 projects are unchanged.
const v1 = { scheduler_generation: 'stage_v1', status: 'running', last_completed_step: 12, source_step_id: 13 }
assert.equal(projectStepName(v1), '条件数学预审')
assert.equal(projectStepStatus(12, v1), 'done')
assert.equal(currentStage(v1).name, '评审与修订')
console.log('Stage catalog v2 display: assertions passed')
