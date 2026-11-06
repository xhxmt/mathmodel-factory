import { isStepDone, nextStepAfter, stepLabelOverride, stepOrder, stepPosition } from './stageCatalog.js'

// The canonical 16-step modeling workflow (Setup = step 0).
// kind: 'setup' | 'normal' | 'human' (human intervention) | 'gate' (QA gate)
export const STEPS = [
  { index: 0,  kind: 'setup',  name: '解析赛题',        en: 'Setup',                icon: 'file-text' },
  { index: 1,  kind: 'normal', name: '背景调研·方法预选', en: 'Research',             icon: 'search' },
  { index: 2,  kind: 'normal', name: '并行建模提案',     en: 'Proposals',            icon: 'layers' },
  { index: 3,  kind: 'human',  name: '方法选择',         en: 'Method Selection',     icon: 'git-branch' },
  { index: 4,  kind: 'normal', name: '完整模型构建',     en: 'Model Build',          icon: 'beaker' },
  { index: 5,  kind: 'normal', name: '完整求解',         en: 'Full Solve',           icon: 'cpu' },
  { index: 6,  kind: 'normal', name: '灵敏度·鲁棒性',    en: 'Sensitivity',          icon: 'activity' },
  { index: 7,  kind: 'normal', name: '模型评价',         en: 'Evaluation',           icon: 'check-circle' },
  { index: 8,  kind: 'normal', name: '可视化打磨',       en: 'Visualization',        icon: 'bar-chart' },
  { index: 9,  kind: 'normal', name: '论文初稿',         en: 'Draft',                icon: 'book-open' },
  { index: 10, kind: 'gate',   name: '关卡一·数值代码核验', en: 'Gate 1 · Consistency', icon: 'shield' },
  { index: 11, kind: 'normal', name: '建设性评审',       en: 'Review',               icon: 'message-square' },
  { index: 12, kind: 'normal', name: '修订',             en: 'Revision',             icon: 'edit' },
  { index: 13, kind: 'gate',   name: '条件数学预审',     en: 'Math Precheck',        icon: 'scale' },
  { index: 14, kind: 'human',  name: '摘要',             en: 'Abstract',             icon: 'edit' },
  { index: 15, kind: 'normal', name: '引用审计·排版·去AI腔', en: 'Polish',            icon: 'sparkles' },
  { index: 16, kind: 'gate',   name: '最终审计·编译交付', en: 'Final Audit & Delivery', icon: 'package' },
]

export const EDITORIAL_GATE_STEP = {
  key: '8_5',
  index: 8.5,
  kind: 'gate',
  name: '阅卷入口设计',
  en: 'Reviewer Entry',
  icon: 'eye',
}

export const TOTAL_STEPS = 16

// status of a step given the "last completed step" index from the backend.
export function stepStatus(index, currentStep) {
  if (currentStep == null) return 'pending'
  if (index <= currentStep) return 'done'
  if (index === currentStep + 1) return 'active'
  return 'pending'
}

export function stepByIndex(index, project = null) {
  const step = STEPS[index] || null
  const override = step && stepLabelOverride(index, project)
  return override ? { ...step, ...override } : step
}

// STEPS in the order this project runs them (Stage catalog v2 runs 14, 15
// before 13), with the catalog's Step names.
export function stepsInOrder(project = null) {
  return stepOrder(project).map((index) => stepByIndex(index, project))
}

export function stepConfigKey(step) {
  if (step && step.key) return `step_${step.key}`
  return `step_${step.index}`
}

export const VERDICT_LABEL = {
  PASS: '通过',
  REOPEN_REVISION_TEXT: '重开·文本修订',
  REOPEN_REVISION_MODEL: '重开·模型修订',
}

// Built-in (hardcoded) model chain per step, for display as the "default" hint.
// overridable=false: the runner does not route this step through dispatch_step
// (Step 2 is parallel multi-stream; Step 16 is pure PDF compile — no model).
// apiOk=true: a non-agentic API model (DeepSeek/Qwen/Gemini) writes this step's
// single markdown artifact cleanly (judge/review/eval). Other steps need an
// agentic backend (claude/codex/agy) to read files / run solvers.
export const STEP_MODEL_META = {
  0:  { overridable: false, apiOk: false, default: '解析（无模型选择）' },
  1:  { overridable: true,  apiOk: false, default: 'Claude' },
  2:  { overridable: false, apiOk: false, default: 'Codex×N + Claude（并行）' },
  3:  { overridable: true,  apiOk: false, default: 'Claude' },
  4:  { overridable: true,  apiOk: false, default: 'Gemini → Claude' },
  5:  { overridable: true,  apiOk: false, default: 'Codex → Claude' },
  6:  { overridable: true,  apiOk: false, default: 'Codex → Claude' },
  7:  { overridable: true,  apiOk: true,  default: 'Claude → Codex' },
  8:  { overridable: true,  apiOk: false, default: 'Claude → Codex' },
  '8_5': { overridable: true, apiOk: false, default: 'Claude → Codex' },
  9:  { overridable: true,  apiOk: false, default: 'Claude → Codex' },
  10: { overridable: true,  apiOk: false, default: 'Codex → Claude' },
  11: { overridable: true,  apiOk: true,  default: 'Codex → Claude' },
  12: { overridable: true,  apiOk: false, default: 'Claude → Codex' },
  13: { overridable: true,  apiOk: true,  default: 'Codex → Claude' },
  14: { overridable: true,  apiOk: false, default: 'Claude → Codex' },
  15: { overridable: true,  apiOk: false, default: 'Codex → Claude' },
  16: { overridable: false, apiOk: false, default: '编译打包（无模型选择）' },
}

export function stepModelMeta(index) {
  return STEP_MODEL_META[index] || STEP_MODEL_META[String(index)] || { overridable: true, apiOk: false, default: '' }
}


// Native current_step/source_step_id already denotes the active Step. Legacy
// snapshots instead carry the last completed Step. Preserve that distinction.
export function hasNativeStepCursor(project) {
  return Boolean(project && (project.scheduler_generation || project.source_step_id != null || project.last_completed_step != null))
}

export function projectStepIndex(project = {}) {
  if (project.status === 'completed') return 16
  if (hasNativeStepCursor(project) && project.active_subtask === 'reviewer_entry_gate') return 8.5
  const raw = hasNativeStepCursor(project)
    ? (project.source_step_id ?? (project.last_completed_step != null ? nextStepAfter(project.last_completed_step, project) : project.current_step) ?? 0)
    : Number(project.current_step ?? -1) + 1
  const index = Number(raw)
  return Number.isFinite(index) ? Math.min(16, Math.max(0, index)) : 0
}

export function projectStepName(project = {}) {
  if (project.status === 'completed') return '已完成'
  const index = projectStepIndex(project)
  return index === 8.5 ? EDITORIAL_GATE_STEP.name : (stepByIndex(index, project)?.name || '')
}

export function projectStepStatus(index, project, legacyCurrentStep) {
  if (!hasNativeStepCursor(project)) return stepStatus(index, legacyCurrentStep)
  if (project.status === 'completed') return 'done'
  const completed = project.last_completed_step != null
    ? Number(project.last_completed_step)
    : stepOrder(project)[Math.floor(stepPosition(projectStepIndex(project), project)) - 1] ?? -1
  if (isStepDone(index, completed, project)
    || (index === 8.5 && project.source_step_id != null && stepPosition(project.source_step_id, project) > stepPosition(8, project))) return 'done'
  return index === projectStepIndex(project) ? 'active' : 'pending'
}
