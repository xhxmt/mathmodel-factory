// Stage catalog awareness for the workspace views.
//
// A project's `stage_catalog_version` decides the order its Steps run in.
// v1 runs Steps 0-16 in numeric order.  v2 (factory-stage-catalog-v2) runs
// ... 11 -> 12 -> 14 -> 15 -> content freeze -> 13 final Judge -> 16, so a
// Step number is not its position: "done", "next" and "which phase" must be
// answered by position in the project's own order, never by comparing ids.

export const CATALOG_V2 = 'factory-stage-catalog-v2'

const V1_ORDER = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16]
const V2_ORDER = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 14, 15, 13, 16]

export function isCatalogV2(project) {
  return Boolean(project && project.stage_catalog_version === CATALOG_V2)
}

export function stepOrder(project) {
  return isCatalogV2(project) ? V2_ORDER : V1_ORDER
}

// Position of a Step in the project's order; 8.5 (reviewer entry) sits right
// after Step 8 in both catalogs, and -1 (nothing done) before everything.
export function stepPosition(step, project) {
  if (step === '8_5' || Number(step) === 8.5) return stepOrder(project).indexOf(8) + 0.5
  const value = Number(step)
  if (!Number.isFinite(value) || value < 0) return -1
  const position = stepOrder(project).indexOf(value)
  return position === -1 ? Infinity : position
}

export function isStepDone(step, completedStep, project) {
  if (completedStep == null) return false
  return stepPosition(step, project) <= stepPosition(completedStep, project)
}

// The Step that runs after `step` (or after nothing, for -1).
export function nextStepAfter(step, project) {
  const order = stepOrder(project)
  const position = stepPosition(step, project)
  if (position === Infinity) return 16
  const next = order[Math.floor(position) + 1]
  return next === undefined ? 16 : next
}

// The canonical v1 definitions; `STAGES` (projectState.js) and
// `CONTEST_PHASES` (workspaceUi.js) re-export these.
export const STAGES_V1 = [
  { id: 1, name: '题意与数据', steps: [0, 1] },
  { id: 2, name: '候选与选模', steps: [2, 3] },
  { id: 3, name: '模型构建', steps: [4] },
  { id: 4, name: '模型求解', steps: [5] },
  { id: 5, name: '验证与评价', steps: [6, 7] },
  { id: 6, name: '图表与入口', steps: [8] },
  { id: 7, name: '初稿与核验', steps: [9, 10] },
  { id: 8, name: '评审与修订', steps: [11, 12, 13] },
  { id: 9, name: '摘要与定稿', steps: [14, 15] },
  { id: 10, name: '终审与交付', steps: [16] },
]

const STAGES_V2 = [
  ...STAGES_V1.slice(0, 7),
  { id: 8, name: '评审与修订', steps: [11, 12] },
  { id: 9, name: '摘要与定稿', steps: [14, 15] },
  { id: 10, name: '终审与交付', steps: [13, 16] },
]

export function stagesFor(project) {
  return isCatalogV2(project) ? STAGES_V2 : STAGES_V1
}

export const CONTEST_PHASES_V1 = [
  { id: 1, key: 'problem_understanding', label: '题意与数据', steps: [0, 1] },
  { id: 2, key: 'model_tournament', label: '模型竞赛', steps: [2, 3], humanGate: 'step3' },
  { id: 3, key: 'model_and_solve', label: '建模与求解', steps: [4, 5] },
  { id: 4, key: 'validation', label: '结果验证', steps: [6, 7] },
  { id: 5, key: 'paper_construction', label: '论文构建', steps: [8, '8_5', 9] },
  { id: 6, key: 'deterministic_paper_audit', label: '确定性审计', steps: [10] },
  { id: 7, key: 'review_and_revision', label: '审稿与修订', steps: [11, 12, 13, 14, 15], humanGate: 'content_freeze' },
  { id: 8, key: 'final_audit_and_delivery', label: '最终交付', steps: [16] },
]

const CONTEST_PHASES_V2 = [
  ...CONTEST_PHASES_V1.slice(0, 6),
  { id: 7, key: 'review_and_revision', label: '审稿与修订', steps: [11, 12, 14, 15], humanGate: 'content_freeze' },
  { id: 8, key: 'final_audit_and_delivery', label: '最终审计与交付', steps: [13, 16] },
]

export function contestPhasesFor(project) {
  return isCatalogV2(project) ? CONTEST_PHASES_V2 : CONTEST_PHASES_V1
}

// v2 renames the two Steps whose job changed; everything else keeps its name.
const V2_STEP_LABELS = {
  13: { name: '最终审计（唯一一次 Judge）', en: 'Final Judge' },
  16: { name: '验收·交付', en: 'Acceptance & Delivery' },
}

export function stepLabelOverride(index, project) {
  return isCatalogV2(project) ? V2_STEP_LABELS[Number(index)] || null : null
}

// Short timeline captions of the review/delivery Steps, per catalog: v1's
// Step 13 is the math precheck and Step 16 the final audit; in v2 Step 13 is
// the final Judge and Step 16 only accepts its judgment.
const STEP_CAPTIONS_V1 = { 13: 'PRECHECK', 16: 'FINAL AUDIT' }
const STEP_CAPTIONS_V2 = { 13: 'FINAL JUDGE', 16: 'ACCEPTANCE' }

export function stepCaption(index, project) {
  const captions = isCatalogV2(project) ? STEP_CAPTIONS_V2 : STEP_CAPTIONS_V1
  return captions[Number(index)] || null
}

// "11–13" for consecutive Steps (the v1 label), "13、16" when a group skips
// Steps, as v2's final-audit group does.
export function stepRangeLabel(steps) {
  const numeric = steps.filter((item) => item !== '8_5').map(Number)
  if (numeric.length === 1) return String(numeric[0])
  const consecutive = numeric.every((value, index) => index === 0 || value === numeric[index - 1] + 1)
  return consecutive ? `${numeric[0]}–${numeric.at(-1)}` : numeric.join('、')
}
