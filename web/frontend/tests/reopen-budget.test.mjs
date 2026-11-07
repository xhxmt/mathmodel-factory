import assert from 'node:assert/strict'
import test from 'node:test'
import { actionLabel, reopenBudgetLines } from '../src/lib/diagnostics.js'

test('the two ways out of a budget pause have labels', () => {
  assert.equal(actionLabel('grant_semantic_reopen'), '批准一次回退')
  assert.equal(actionLabel('revert_upstream_edits_and_resume'), '已撤销上游改动，恢复运行')
})

test('budget lines name the spent limit, the causes and the history', () => {
  const lines = reopenBudgetLines({
    exhausted: 'stage', stage: 9, stage_count: 2, stage_limit: 2, run_count: 2, run_limit: 5,
    causes: [{ flag: 'MODEL_DIRTY', owner_stage: 1, cause_artifact: 'viable_streams.md' }],
    history: [{ revision: 3, stage: 9, owner_stage: 1 }],
  })
  assert.equal(lines[0], '已用尽：Stage 9 发起的回退次数')
  assert.equal(lines[1], 'Stage 9：2/2 · 整次运行：2/5')
  assert.ok(lines.includes('待回退原因：viable_streams.md · MODEL_DIRTY · 归属 Stage 1'))
  assert.ok(lines.includes('已发生：r3 · Stage 9 → Stage 1'))
  assert.equal(reopenBudgetLines({ exhausted: 'run', stage: 9, stage_count: 0, stage_limit: 2, run_count: 5, run_limit: 5 })[0], '已用尽：整次运行的回退总数')
  assert.deepEqual(reopenBudgetLines(null), [])
})
